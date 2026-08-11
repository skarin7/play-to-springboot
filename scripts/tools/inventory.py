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
from pathlib import Path
from typing import Any

try:
    from layers import LAYER_ORDER, classify, divergences, empty_counts
except ImportError:  # invoked as a module rather than a script
    from .layers import LAYER_ORDER, classify, divergences, empty_counts


def iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def find_play_java_root(play_repo: Path, override: str | None = None) -> Path | None:
    """Play Java sources live in ``app/``; allow an override for odd layouts."""
    if override:
        candidate = play_repo / override
        return candidate if candidate.is_dir() else None
    candidate = play_repo / "app"
    return candidate if candidate.is_dir() else None


def scan(source_root: Path) -> tuple[list[str], dict[str, int]]:
    """Return (source-root-relative paths, per-layer counts) for *.java under root."""
    if not source_root.is_dir():
        return [], empty_counts()
    rel_paths = sorted(
        str(f.relative_to(source_root))
        for f in source_root.rglob("*.java")
        if f.is_file()
    )
    counts = empty_counts()
    for rel in rel_paths:
        counts[classify(rel)] += 1
    return rel_paths, counts


def inventory_tree(source_root: Path | None, java_root_label: str) -> dict[str, Any]:
    if source_root is None:
        return {
            "java_root": java_root_label,
            "exists": False,
            "total_java_files": 0,
            "by_layer": empty_counts(),
        }
    rel_paths, counts = scan(source_root)
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
    args = parser.parse_args()

    if not args.play_repo and not args.spring_repo:
        print("ERROR: need --play-repo and/or --spring-repo", file=sys.stderr)
        return 1

    out: dict[str, Any] = {"captured_at": iso_now()}

    if args.play_repo:
        play_repo = args.play_repo.expanduser().resolve()
        root = find_play_java_root(play_repo, args.play_java_root)
        label = args.play_java_root or "app"
        out["play"] = inventory_tree(root, label)
        if root is None:
            print(
                f"[warn] no Java source root at {play_repo / label}",
                file=sys.stderr,
            )
        else:
            rel_paths, _ = scan(root)
            # Files that a pre-segment-matching dev-toolkit JAR would place in the
            # wrong layer. Empty on a current JAR; non-empty means the JAR copied
            # into the Play repo predates the fix and will mis-migrate these.
            warnings = divergences(rel_paths)
            out["stale_jar_warnings"] = {
                "note": (
                    "Non-empty means the dev-toolkit JAR in use may predate the "
                    "LayerDetector segment-matching fix. Verify the JAR is current; "
                    "an old one migrates these files in the wrong layer (controllers "
                    "land in 'other' and never receive @RestController)."
                ),
                "affected": [
                    {"path": d.path, "correct_layer": d.correct, "old_jar_layer": d.jar_actual}
                    for d in warnings
                ],
            }
            if warnings:
                print(
                    f"[warn] {len(warnings)} file(s) would be mis-classified by a "
                    f"pre-fix dev-toolkit JAR; confirm the JAR is current",
                    file=sys.stderr,
                )
            out["mode"] = (
                "collapsed"
                if out["play"]["total_java_files"] < args.collapsed_threshold
                else "full"
            )

    if args.spring_repo:
        spring_repo = args.spring_repo.expanduser().resolve()
        src = spring_repo / "src" / "main" / "java"
        out["spring"] = inventory_tree(
            src if src.is_dir() else None, "src/main/java"
        )

    json.dump(out, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
