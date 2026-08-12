#!/usr/bin/env python3
"""
Migration completeness check: Play vs Spring per-layer counts.

    python3 scripts/tools/verify.py --play-repo ../p --spring-repo ../spring-p \
        [--status-file S] [--no-migration Module.java --no-migration Filters.java]

Files the architect declared unmigratable (Play-only glue such as ``Module.java``
or ``Filters.java``) are subtracted from the Play baseline before comparing.
Without that, those files show as a permanent negative delta on every run and
train the reader to ignore the check.

Counting alone cannot prove a migration is correct -- a file stubbed out to
``return null`` still counts as one file. That is what the T2 signature check is
for. This tool answers only "is anything missing entirely?".
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from layers import LAYER_ORDER, classify, empty_counts
    from routes import compare_routes, parse_play_routes, parse_spring_mappings
except ImportError:
    from .layers import LAYER_ORDER, classify, empty_counts
    from .routes import compare_routes, parse_play_routes, parse_spring_mappings


def iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def scan_counts(source_root: Path, exclude: set[str]) -> tuple[dict[str, int], int]:
    counts = empty_counts()
    total = 0
    if not source_root.is_dir():
        return counts, 0
    for f in sorted(source_root.rglob("*.java")):
        if not f.is_file():
            continue
        rel = str(f.relative_to(source_root))
        if rel in exclude or f.name in exclude:
            continue
        counts[classify(rel)] += 1
        total += 1
    return counts, total


def compare(
    play_counts: dict[str, int],
    spring_counts: dict[str, int],
) -> tuple[str, dict[str, Any], list[str]]:
    """
    Returns (status, per-layer comparison, notes).

    A layer with fewer Spring files than Play files means sources went missing;
    since ``no_migration`` files are already excluded from the Play baseline,
    any shortfall here is unexplained by construction. Extra Spring files are
    normal (hand-written config, split classes) and are not a failure.
    """
    layer_comparison: dict[str, Any] = {}
    notes: list[str] = []
    status = "passed"

    for layer in LAYER_ORDER:
        expected = int(play_counts.get(layer, 0))
        actual = int(spring_counts.get(layer, 0))
        delta = actual - expected
        layer_comparison[layer] = {
            "play_expected": expected,
            "spring_actual": actual,
            "delta": delta,
        }
        if expected > 0 and actual == 0:
            status = "failed"
            notes.append(f"{layer}: nothing migrated ({expected} Play files, 0 Spring)")
        elif actual < expected:
            if status != "failed":
                status = "needs_review"
            notes.append(
                f"{layer}: {expected - actual} file(s) short ({actual} vs {expected})"
            )

    return status, layer_comparison, notes


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Play vs Spring per-layer completeness comparison (JSON to stdout)."
    )
    parser.add_argument("--play-repo", type=Path, required=True)
    parser.add_argument("--spring-repo", type=Path, required=True)
    parser.add_argument("--play-java-root", default="app")
    parser.add_argument(
        "--no-migration",
        action="append",
        default=[],
        help="Play file excluded from the baseline; repeatable. "
             "Path relative to the Java root, or a bare filename.",
    )
    parser.add_argument(
        "--status-file",
        type=Path,
        default=None,
        help="Read architecture_review.no_migration from here, in addition to --no-migration.",
    )
    parser.add_argument(
        "--skip-routes",
        action="store_true",
        help="Skip T3 route parity (meaningless before the controller layer runs).",
    )
    parser.add_argument(
        "--routes-file",
        type=Path,
        default=None,
        help="Play routes file (default: <play-repo>/conf/routes).",
    )
    args = parser.parse_args()

    exclude = set(args.no_migration)
    if args.status_file and args.status_file.is_file():
        try:
            data = json.loads(args.status_file.read_text(encoding="utf-8"))
            exclude |= set(
                (data.get("architecture_review") or {}).get("no_migration") or []
            )
        except json.JSONDecodeError as e:
            print(f"[warn] could not read status file: {e}", file=sys.stderr)

    play_root = args.play_repo.expanduser().resolve() / args.play_java_root
    spring_root = args.spring_repo.expanduser().resolve() / "src" / "main" / "java"

    if not play_root.is_dir():
        print(f"ERROR: no Play Java root at {play_root}", file=sys.stderr)
        return 1

    play_counts, play_total = scan_counts(play_root, exclude)
    spring_counts, spring_total = scan_counts(spring_root, set())

    status, layer_comparison, notes = compare(play_counts, spring_counts)

    result = {
        "status": status,
        "checked_at": iso_now(),
        "play_java_total": play_total,
        "spring_java_total": spring_total,
        "excluded_from_baseline": sorted(exclude),
        "layer_comparison": layer_comparison,
        "notes": "; ".join(notes) if notes else "All layers accounted for.",
    }

    # T3 route parity. Reported as skipped rather than passing when not run --
    # an unrun check must never read as a green one.
    if args.skip_routes:
        result["route_parity"] = {"status": "skipped", "reason": "--skip-routes"}
    else:
        routes_file = args.routes_file or (
            args.play_repo.expanduser().resolve() / "conf" / "routes"
        )
        play_routes, route_notes = parse_play_routes(routes_file)
        spring_routes = parse_spring_mappings(spring_root)
        parity = compare_routes(play_routes, spring_routes)
        parity["notes"] = route_notes
        result["route_parity"] = parity
        if parity["status"] != "passed":
            result["status"] = "failed"
            result["notes"] += (
                f"; {len(parity['missing'])} Play route(s) have no Spring mapping"
            )

    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
