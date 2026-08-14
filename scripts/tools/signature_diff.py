#!/usr/bin/env python3
"""
T2 structural preservation: compare Play signatures against Spring signatures.

    JAR=$(python3 scripts/tools/fetch_jar.py)
    java -jar "$JAR" signature <play>/app        > play.json
    java -jar "$JAR" signature <spring>/src/main/java > spring.json
    python3 scripts/tools/signature_diff.py --play play.json --spring spring.json \\
        --layer service --layer-only

Answers the one question file counting cannot: did the code survive, or was it
hollowed out to make the build pass? A method rewritten as ``return null;`` still
counts as a migrated file.

``--layer-only`` restricts the comparison to Play classes that classify into
``--layer``. During the per-layer loop that is what you want: a finding then
names the layer that actually produced it, instead of re-reporting a class three
layers old every time the gate runs. Omit it for the final full-tree pass.

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

Exemptions
----------

The blocker rule matches **names**, and some Play-to-Spring changes are
mandatory changes of *shape*: an ``EssentialFilter.apply`` becomes a
``Filter.doFilter``, an ``ErrorHandler.onServerError`` becomes a
``@ControllerAdvice`` handler. The name is gone because the framework requires
it to be gone. Reported as a blocker, the cheapest way to clear it is to add a
public method with the old name that nothing calls -- which is how a check
meant to catch hollowed-out code ends up *creating* dead code.

So a named set of framework-glue methods is suppressed rather than blocked, and
suppression is quiet but never invisible: every suppressed method is listed
under ``suppressed`` in this tool's output, in the gate JSON, and in the report.
The mechanism is deliberately awkward to reach for -- the architect authors
project-specific entries in ``.migration/signature-exemptions.json``, they are
approved at Gate 1, and ``gate.py`` re-hashes the file on every run so an edit
made after approval is visible rather than silent.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

try:
    from layers import classify, load_overrides
except ImportError:
    from .layers import classify, load_overrides

DEFAULT_DROP_RATIO = 0.6
DEFAULT_MIN_STATEMENTS = 3

EXEMPTIONS_FILE = "signature-exemptions.json"

# Play framework glue whose Spring counterpart is a different interface, not a
# renamed method. Keys are class-name patterns ("*Filter" matches any class name
# ending in Filter); values map method name -> the Spring construct that took
# the job over. Shipped as defaults because these are properties of the two
# frameworks, not of anyone's project.
DEFAULT_EXEMPTIONS: dict[str, dict[str, str]] = {
    "Filters": {
        "apply": "jakarta.servlet.Filter.doFilter",
        "filters": "jakarta.servlet.Filter.doFilter / FilterRegistrationBean",
    },
    "*Filter": {
        "apply": "jakarta.servlet.Filter.doFilter",
        "filters": "jakarta.servlet.Filter.doFilter / FilterRegistrationBean",
    },
    "*ErrorHandler": {
        "onClientError": "@ControllerAdvice / @ExceptionHandler",
        "onServerError": "@ControllerAdvice / @ExceptionHandler",
        "onBadRequest": "@ControllerAdvice / @ExceptionHandler",
    },
    "Module": {
        "configure": "Spring component scanning / @Configuration",
        "bindings": "Spring component scanning / @Configuration",
    },
    "*Lifecycle": {
        "addStopHook": "@PreDestroy",
    },
}


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


def filter_by_layer(
    tree: dict[str, Any], layer: str, overrides: dict[str, str] | None = None
) -> dict[str, Any]:
    """
    Keep only entries whose path classifies into ``layer``.

    Applied to the *Play* side only. The Spring side must stay whole: migration
    relocates classes, and a service that landed under a different directory
    would otherwise read as missing rather than as moved.
    """
    return {
        key: entry
        for key, entry in tree.items()
        if isinstance(entry, dict)
        and classify(entry.get("path") or key, overrides) == layer
    }


def parse_errors(tree: dict[str, Any]) -> list[dict[str, str]]:
    return [
        {"path": entry.get("path", key), "error": entry["parse_error"]}
        for key, entry in tree.items()
        if isinstance(entry, dict) and entry.get("parse_error")
    ]


def _class_pattern_matches(pattern: str, class_name: str) -> bool:
    if pattern == class_name:
        return True
    if pattern.startswith("*") and class_name.endswith(pattern[1:]):
        return True
    if pattern.endswith("*") and class_name.startswith(pattern[:-1]):
        return True
    return False


def load_exemptions(path: Path | None) -> dict[str, dict[str, str]]:
    """
    Merge project exemptions over the shipped defaults.

    A user entry for a class replaces the default entry for that class rather
    than adding to it, so a project can *narrow* an exemption it disagrees with
    -- suppression should never be harder to remove than it was to add.
    """
    merged = {k: dict(v) for k, v in DEFAULT_EXEMPTIONS.items()}
    if path is None or not path.is_file():
        return merged
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"[warn] could not read {path}: {e}", file=sys.stderr)
        return merged

    entries = data.get("exemptions", data) if isinstance(data, dict) else {}
    for class_pattern, methods in (entries or {}).items():
        if not isinstance(methods, dict):
            continue
        merged[class_pattern] = {
            name: str(value.get("replacement", value) if isinstance(value, dict) else value)
            for name, value in methods.items()
        }
    return merged


def is_exempt(
    class_name: str, method_name: str, exemptions: dict[str, dict[str, str]]
) -> str | None:
    """Returns the replacement construct, or None if this is a real gap."""
    for pattern, methods in exemptions.items():
        if _class_pattern_matches(pattern, class_name) and method_name in methods:
            return methods[method_name]
    return None


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
    exemptions: dict[str, dict[str, str]] | None = None,
    suppressed: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    exemptions = exemptions if exemptions is not None else DEFAULT_EXEMPTIONS
    spring_names = {m.get("name") for m in spring_sig.get("methods", [])}
    spring_path = spring_sig.get("path", "")

    for method in public_methods(play_sig):
        name = method.get("name")
        if name in spring_names:
            continue

        replacement = is_exempt(class_name, name or "", exemptions)
        if replacement is not None:
            # Recorded, not blocked. The method is genuinely gone and genuinely
            # should be -- what replaced it is a different interface, and the
            # reader needs to see that judgment was applied, not that the check
            # forgot to look.
            if suppressed is not None:
                suppressed.append(
                    {
                        "class": class_name,
                        "method": name,
                        "layer": layer,
                        "replacement": replacement,
                        "reason": "framework interface change; see signature-exemptions.json",
                    }
                )
            continue

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
                    f"port {name} from the Play source. If the Spring counterpart "
                    f"is a different interface, the architect adds an entry to "
                    f".migration/signature-exemptions.json naming the replacement "
                    f"— do not add a method solely to satisfy this check"
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
    layer_only: bool = False,
    overrides: dict[str, str] | None = None,
    exemptions: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    no_migration = no_migration or set()
    exemptions = exemptions if exemptions is not None else DEFAULT_EXEMPTIONS
    scoped_play = filter_by_layer(play_tree, layer, overrides) if layer_only else play_tree
    play_classes = index_by_class(scoped_play)
    spring_classes = index_by_class(spring_tree)

    findings: list[dict[str, Any]] = []
    suppressed: list[dict[str, Any]] = []
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
                class_name, play_sig, spring_sig, layer, drop_ratio, min_statements,
                exemptions, suppressed,
            )
        )

    return {
        "tier": "T2",
        "layer": layer,
        "scope": "layer" if layer_only else "full-tree",
        "classes_compared": len(compared),
        "classes_absent_from_spring": sorted(not_yet_migrated),
        "parse_errors": parse_errors(scoped_play) + parse_errors(spring_tree),
        "suppressed": suppressed,
        "findings": findings,
        "status": "failed" if any(f["severity"] == "blocker" for f in findings)
        else ("needs_review" if findings else "passed"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="T2 structural preservation check (JSON to stdout).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  signature_diff.py --play play.json --spring spring.json "
            "--layer service --layer-only\n"
            "  signature_diff.py --play play.json --spring spring.json --layer final \\\n"
            "      --status-file ../spring-app/migration-status.json \\\n"
            "      --exemptions ../spring-app/.migration/signature-exemptions.json\n"
            "\n"
            "Exit codes: 0 passed, 1 needs_review or failed.\n"
        ),
    )
    parser.add_argument("--play", type=Path, required=True, help="Play signature JSON")
    parser.add_argument("--spring", type=Path, required=True, help="Spring signature JSON")
    parser.add_argument("--layer", default="unknown")
    parser.add_argument(
        "--layer-only",
        action="store_true",
        help="Compare only Play classes belonging to --layer. Use during the "
             "per-layer loop; omit for the final full-tree pass.",
    )
    parser.add_argument("--drop-ratio", type=float, default=DEFAULT_DROP_RATIO)
    parser.add_argument("--min-statements", type=int, default=DEFAULT_MIN_STATEMENTS)
    parser.add_argument(
        "--status-file",
        type=Path,
        default=None,
        help="Read architecture_review.no_migration from here.",
    )
    parser.add_argument(
        "--layer-overrides",
        type=Path,
        default=None,
        help="Path to .migration/layer-overrides.json (default: none).",
    )
    parser.add_argument(
        "--exemptions",
        type=Path,
        default=None,
        help=f"Path to .migration/{EXEMPTIONS_FILE}. Project entries merge over the "
             "shipped framework-glue defaults.",
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

    overrides = load_overrides(args.layer_overrides) if args.layer_overrides else {}

    result = diff(
        load_tree(args.play),
        load_tree(args.spring),
        args.layer,
        args.drop_ratio,
        args.min_statements,
        no_migration,
        args.layer_only,
        overrides,
        load_exemptions(args.exemptions),
    )
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
