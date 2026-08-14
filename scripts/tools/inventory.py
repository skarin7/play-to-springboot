#!/usr/bin/env python3
"""
Count Java sources per layer in the Play and/or Spring tree.

Prints JSON to stdout. Deterministic and exact -- this is the baseline every
completeness check compares against, so it must never be an agent's estimate.

    python3 scripts/tools/inventory.py --play-repo ../my-play-app
    python3 scripts/tools/inventory.py --spring-repo ../spring-my-play-app
    python3 scripts/tools/inventory.py --play-repo ../p --spring-repo ../spring-p

Paths are classified relative to the Java *source root* (``app/`` for Play,
``src/main/java`` for Spring), which is the same form dev-toolkit's
LayerDetector receives. That makes the ``classification_warnings`` block a
truthful prediction of what the JAR will do -- see tools/layers.py.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

try:
    from layers import LAYER_ORDER, classify, empty_counts, load_overrides
except ImportError:  # invoked as a module rather than a script
    from .layers import LAYER_ORDER, classify, empty_counts, load_overrides

# Segment names the classifier actually recognizes. A directory outside this
# set that keeps showing up among files landing in "other" is a strong signal
# the repo uses non-conventional naming -- see classification_smell().
_KNOWN_SEGMENTS = {
    "controllers", "service", "services", "models", "db", "repositories", "dao",
}


def iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def find_play_java_root(play_repo: Path, override: str | None = None) -> Path | None:
    """Play Java sources live in ``app/``; allow an override for odd layouts."""
    if override:
        candidate = play_repo / override
        return candidate if candidate.is_dir() else None
    candidate = play_repo / "app"
    return candidate if candidate.is_dir() else None


def scan(
    source_root: Path,
    overrides: dict[str, str] | None = None,
    exclude: set[str] | None = None,
) -> tuple[list[str], dict[str, int]]:
    """Return (source-root-relative paths, per-layer counts) for *.java under root."""
    if not source_root.is_dir():
        return [], empty_counts()
    exclude = exclude or set()
    rel_paths = sorted(
        str(f.relative_to(source_root))
        for f in source_root.rglob("*.java")
        if f.is_file()
        and str(f.relative_to(source_root)) not in exclude
        and f.name not in exclude
    )
    counts = empty_counts()
    for rel in rel_paths:
        counts[classify(rel, overrides)] += 1
    return rel_paths, counts


def classification_smell(
    rel_paths: list[str], threshold: float = 0.15
) -> dict[str, Any]:
    """
    Statistical check that classification looks wrong, computed from data
    inventory already has -- no new classifier, just a smell test.

    A high fraction of files landing in "other", or a directory name that
    keeps recurring among them, means this repo likely doesn't use the
    conventional segment names and a human should consider drafting
    ``.migration/layer-overrides.json`` at Gate 1. Always computed against the
    raw (non-overridden) classification -- that is the signal this exists to
    surface, not to hide.
    """
    total = len(rel_paths)
    other_paths = [p for p in rel_paths if classify(p) == "other"]
    other_pct = (len(other_paths) / total) if total else 0.0

    dir_counts: dict[str, int] = {}
    for p in other_paths:
        for part in PurePosixPath(p.replace("\\", "/").lower()).parts[:-1]:
            if part not in _KNOWN_SEGMENTS:
                dir_counts[part] = dir_counts.get(part, 0) + 1
    common = sorted(dir_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:5]

    return {
        "other_pct": round(other_pct, 4),
        "common_unmapped_dirs": [[name, count] for name, count in common],
        "warn": other_pct >= threshold,
        "threshold": threshold,
    }


def inventory_tree(
    source_root: Path | None,
    java_root_label: str,
    overrides: dict[str, str] | None = None,
    exclude: set[str] | None = None,
) -> dict[str, Any]:
    if source_root is None:
        return {
            "java_root": java_root_label,
            "exists": False,
            "total_java_files": 0,
            "by_layer": empty_counts(),
        }
    rel_paths, counts = scan(source_root, overrides, exclude)
    return {
        "java_root": java_root_label,
        "exists": True,
        "total_java_files": len(rel_paths),
        "by_layer": counts,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Per-layer Java source counts for Play and Spring trees (JSON to stdout)."
    )
    parser.add_argument("--play-repo", type=Path, default=None)
    parser.add_argument("--spring-repo", type=Path, default=None)
    parser.add_argument(
        "--play-java-root",
        default=None,
        help="Java source dir under the Play repo (default: app).",
    )
    parser.add_argument(
        "--collapsed-threshold",
        type=int,
        default=20,
        help="Play files below this pick 'collapsed' role mode (default: 20).",
    )
    parser.add_argument(
        "--other-threshold",
        type=float,
        default=0.15,
        help="Warn when this fraction (or more) of Play files land in 'other' (default: 0.15).",
    )
    parser.add_argument(
        "--layer-overrides",
        type=Path,
        default=None,
        help="Path to layer-overrides.json. When given, by_layer counts reflect "
             "the corrected mapping instead of the raw segment guess.",
    )
    parser.add_argument(
        "--status-file",
        type=Path,
        default=None,
        help="Read architecture_review.no_migration from here, excluded from counts.",
    )
    args = parser.parse_args()

    if not args.play_repo and not args.spring_repo:
        print("ERROR: need --play-repo and/or --spring-repo", file=sys.stderr)
        return 1

    overrides = load_overrides(args.layer_overrides) if args.layer_overrides else {}

    exclude: set[str] = set()
    if args.status_file and args.status_file.is_file():
        try:
            data = json.loads(args.status_file.read_text(encoding="utf-8"))
            exclude = set(
                (data.get("architecture_review") or {}).get("no_migration") or []
            )
        except json.JSONDecodeError as e:
            print(f"[warn] could not read status file: {e}", file=sys.stderr)

    out: dict[str, Any] = {"captured_at": iso_now()}

    if args.play_repo:
        play_repo = args.play_repo.expanduser().resolve()
        root = find_play_java_root(play_repo, args.play_java_root)
        label = args.play_java_root or "app"
        out["play"] = inventory_tree(root, label, overrides, exclude)
        if root is None:
            print(
                f"[warn] no Java source root at {play_repo / label}",
                file=sys.stderr,
            )
        else:
            rel_paths, _ = scan(root)
            out["mode"] = (
                "collapsed"
                if out["play"]["total_java_files"] < args.collapsed_threshold
                else "full"
            )
            smell = classification_smell(rel_paths, args.other_threshold)
            out["classification_smell"] = smell
            if smell["warn"]:
                print(
                    f"[warn] {smell['other_pct']:.0%} of Play files classify as "
                    f"'other'; common unmapped dirs: {smell['common_unmapped_dirs']}. "
                    f"Consider drafting .migration/layer-overrides.json at Gate 1.",
                    file=sys.stderr,
                )

    if args.spring_repo:
        spring_repo = args.spring_repo.expanduser().resolve()
        src = spring_repo / "src" / "main" / "java"
        out["spring"] = inventory_tree(
            src if src.is_dir() else None, "src/main/java", overrides
        )

    json.dump(out, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
