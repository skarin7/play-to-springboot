#!/usr/bin/env python3
"""
Turn a Maven build log into structured error JSON.

    mvn compile 2>&1 | python3 scripts/tools/parse_mvn.py
    python3 scripts/tools/parse_mvn.py --log build.log

Exists so the manager never has to read raw Maven output: it reads this JSON
instead. Grouping by file is what lets a fix be dispatched with evidence
attached rather than a wall of text.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from typing import Any

# [ERROR] /abs/path/File.java:[10,20] cannot find symbol
MVN_ERROR_RE = re.compile(r"\[ERROR\]\s+(.+\.java):\[(\d+),[^\]]+\]\s*(.+)")

COMPILATION_ERROR_LINE_RE = re.compile(r"^\[ERROR\]\s+COMPILATION ERROR", re.MULTILINE)

# Maven prints this when a declared dependency cannot be resolved -- a different
# failure class from a compile error, and one the architect gate is meant to
# catch via the empty-project compile.
DEPENDENCY_ERROR_RE = re.compile(
    r"\[ERROR\].*?(Could not resolve dependencies|Failed to execute goal|"
    r"non-resolvable|Could not find artifact)(.*)",
    re.IGNORECASE,
)


def parse_errors(log: str) -> list[dict[str, Any]]:
    """One entry per `[ERROR] file:[line,col] message` line, in log order."""
    errors: list[dict[str, Any]] = []
    for line in log.splitlines():
        m = MVN_ERROR_RE.search(line)
        if m:
            errors.append(
                {
                    "file": m.group(1),
                    "line": int(m.group(2)),
                    "message": m.group(3).strip(),
                }
            )
    return errors


def parse_dependency_errors(log: str) -> list[str]:
    return [m.group(0).strip() for m in DEPENDENCY_ERROR_RE.finditer(log)]


def count_compilation_error_blocks(log: str) -> int:
    return len(COMPILATION_ERROR_LINE_RE.findall(log))


def group_by_file(errors: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in errors:
        grouped[e.get("file", "unknown")].append(e)
    return dict(grouped)


def error_signatures(errors: list[dict[str, Any]]) -> list[str]:
    """
    Stable, order-independent identity for a set of errors.

    The manager compares consecutive signature sets to tell a real stuck loop
    (identical set) from progress that exposed deeper errors (different set).
    The old is_looping() heuristic could not make that distinction and killed
    layers that were actually advancing.
    """
    return sorted(
        f"{e.get('file','')}:{e.get('line',0)}:{(e.get('message') or '').strip()}"
        for e in errors
    )


def summarize(log: str) -> dict[str, Any]:
    errors = parse_errors(log)
    dep_errors = parse_dependency_errors(log)
    by_file = group_by_file(errors)
    # A log carrying [ERROR] lines that none of the patterns matched is a failure
    # we cannot describe -- keep a tail so it still reaches a human. A log with no
    # [ERROR] at all is a clean build and needs no tail.
    unexplained = "[ERROR]" in log and not errors and not dep_errors
    return {
        "error_count": len(errors),
        "compilation_error_blocks": count_compilation_error_blocks(log),
        "dependency_errors": dep_errors,
        "files_affected": sorted(by_file.keys()),
        "errors": errors,
        "by_file": by_file,
        "signatures": error_signatures(errors),
        "unparsed_tail": log[-4000:] if unexplained else "",
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Maven log -> structured error JSON (stdout)."
    )
    parser.add_argument(
        "--log", type=argparse.FileType("r"), default=sys.stdin,
        help="Log file (default: stdin).",
    )
    args = parser.parse_args()
    json.dump(summarize(args.log.read()), sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
