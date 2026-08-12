#!/usr/bin/env python3
"""
T2 structural preservation: compare Play signatures against Spring signatures.

    java -jar dev-toolkit-1.0.0.jar signature <play>/app        > play.json
    java -jar dev-toolkit-1.0.0.jar signature <spring>/src/main/java > spring.json
    python3 scripts/tools/signature_diff.py --play play.json --spring spring.json \\
        --layer service

Answers the one question file counting cannot: did the code survive, or was it
hollowed out to make the build pass? A method rewritten as ``return null;`` still
counts as a migrated file.

**Only two conditions are reported**, and the narrowness is the point. Migration
legitimately rewrites bodies -- ``Result`` becomes ``ResponseEntity``, Guice
becomes constructor injection -- so a broad diff would flag every correctly
migrated file. False positives on a blocker-severity check teach the reviewer to
wave findings through, which costs more than the check gains.

    blocker : a public method in Play has no counterpart in Spring
    major   : a method kept its name but lost >60% of its statements AND now
              has fewer than 3

Everything else is silent. Thresholds are tunable for projects whose migration
legitimately collapses more logic than usual.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

DEFAULT_DROP_RATIO = 0.6
DEFAULT_MIN_STATEMENTS = 3


def load_tree(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("files", data)


def index_by_class(tree: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """
    Key signatures by class name rather than path.

    Migration moves files around -- package layout changes, directories get
    reorganised -- so matching on path would report every relocated class as
    missing. The class name is what stays stable.
    """
    out: dict[str, dict[str, Any]] = {}
    for entry in tree.values():
        if not isinstance(entry, dict) or entry.get("parse_error"):
            continue
        name = entry.get("class") or ""
        if name:
            out[name] = entry
    return out


def parse_errors(tree: dict[str, Any]) -> list[dict[str, str]]:
    return [
        {"path": entry.get("path", key), "error": entry["parse_error"]}
        for key, entry in tree.items()
        if isinstance(entry, dict) and entry.get("parse_error")
    ]


def public_methods(signature: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        m for m in signature.get("methods", []) if m.get("visibility") == "public"
    ]


def statements_by_name(signature: dict[str, Any]) -> dict[str, int]:
    """
    Total statements per method name.

    Aggregating overloads avoids false positives when a migration merges or
    splits overloads: what matters is whether the logic under that name survived.
    """
    totals: dict[str, int] = {}
    for m in signature.get("methods", []):
        name = m.get("name", "")
        totals[name] = totals.get(name, 0) + int(m.get("statements", 0))
    return totals


def compare_class(
    class_name: str,
    play_sig: dict[str, Any],
    spring_sig: dict[str, Any],
    layer: str,
    drop_ratio: float,
    min_statements: int,
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    spring_names = {m.get("name") for m in spring_sig.get("methods", [])}
    spring_path = spring_sig.get("path", "")

    for method in public_methods(play_sig):
        name = method.get("name")
        if name not in spring_names:
            findings.append(
                {
                    "layer": layer,
                    "file": spring_path or f"{class_name}.java",
                    "tier": "T2",
                    "severity": "blocker",
                    "category": "method-missing",
                    "evidence": (
                        f"{class_name}.{name}(arity {method.get('arity')}) is public "
                        f"in Play and absent from the Spring class"
                    ),
                    "suggested_fix": (
                        f"port {name} from the Play source, or record it in "
                        f"decisions.md if it is intentionally dropped"
                    ),
                }
            )

    play_totals = statements_by_name(play_sig)
    spring_totals = statements_by_name(spring_sig)
    for name, before in play_totals.items():
        if name not in spring_totals or before == 0:
            continue
        after = spring_totals[name]
        if after < before * (1 - drop_ratio) and after < min_statements:
            findings.append(
                {
                    "layer": layer,
                    "file": spring_path or f"{class_name}.java",
                    "tier": "T2",
                    "severity": "major",
                    "category": "logic-dropped",
                    "evidence": (
                        f"{class_name}.{name}: {before} statements in Play -> "
                        f"{after} in Spring"
                    ),
                    "suggested_fix": (
                        "restore the ported logic, or confirm the collapse is "
                        "intentional per decisions.md"
                    ),
                }
            )
    return findings


def diff(
    play_tree: dict[str, Any],
    spring_tree: dict[str, Any],
    layer: str,
    drop_ratio: float = DEFAULT_DROP_RATIO,
    min_statements: int = DEFAULT_MIN_STATEMENTS,
    no_migration: set[str] | None = None,
) -> dict[str, Any]:
    no_migration = no_migration or set()
    play_classes = index_by_class(play_tree)
    spring_classes = index_by_class(spring_tree)

    findings: list[dict[str, Any]] = []
    compared: list[str] = []
    not_yet_migrated: list[str] = []

    for class_name, play_sig in sorted(play_classes.items()):
        if class_name in no_migration or f"{class_name}.java" in no_migration:
            continue
        spring_sig = spring_classes.get(class_name)
        if spring_sig is None:
            # Absent entirely -- a completeness question for verify.py, not a
            # preservation question. Reporting it here would double-count, and
            # during a layered migration most classes are legitimately absent
            # until their layer runs.
            not_yet_migrated.append(class_name)
            continue
        compared.append(class_name)
        findings.extend(
            compare_class(
                class_name, play_sig, spring_sig, layer, drop_ratio, min_statements
            )
        )

    return {
        "tier": "T2",
        "layer": layer,
        "classes_compared": len(compared),
        "classes_absent_from_spring": sorted(not_yet_migrated),
        "parse_errors": parse_errors(play_tree) + parse_errors(spring_tree),
        "findings": findings,
        "status": "failed" if any(f["severity"] == "blocker" for f in findings)
        else ("needs_review" if findings else "passed"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="T2 structural preservation check (JSON to stdout)."
    )
    parser.add_argument("--play", type=Path, required=True, help="Play signature JSON")
    parser.add_argument("--spring", type=Path, required=True, help="Spring signature JSON")
    parser.add_argument("--layer", default="unknown")
    parser.add_argument("--drop-ratio", type=float, default=DEFAULT_DROP_RATIO)
    parser.add_argument("--min-statements", type=int, default=DEFAULT_MIN_STATEMENTS)
    parser.add_argument(
        "--status-file",
        type=Path,
        default=None,
        help="Read architecture_review.no_migration from here.",
    )
    args = parser.parse_args()

    no_migration: set[str] = set()
    if args.status_file and args.status_file.is_file():
        try:
            data = json.loads(args.status_file.read_text(encoding="utf-8"))
            no_migration = set(
                (data.get("architecture_review") or {}).get("no_migration") or []
            )
        except json.JSONDecodeError as e:
            print(f"[warn] could not read status file: {e}", file=sys.stderr)

    result = diff(
        load_tree(args.play),
        load_tree(args.spring),
        args.layer,
        args.drop_ratio,
        args.min_statements,
        no_migration,
    )
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
