#!/usr/bin/env python3
"""
Run the verification tiers for a layer and return one verdict.

    python3 scripts/tools/gate.py --play-repo P --spring-repo S --layer service
    python3 scripts/tools/gate.py --play-repo P --spring-repo S --final

This is the manager's own check, not an agent's. Every tier below is
deterministic -- a subprocess call and a comparison -- so wrapping them in an
agent dispatch bought nothing but a round trip per layer, which is most of the
wall-clock cost of a run.

    T1  compile        mvn compile, parsed by parse_mvn.py
    T2  preservation   dev-toolkit signature, diffed by signature_diff.py
    T3  route parity   routes.py
    T4  tests          mvn test

T5 -- endpoint response parity -- is deliberately not here. It needs both
applications booted and answering, which is a QA dispatch rather than a
subprocess call. See ``endpoint_diff.py``.

Raw Maven output never reaches stdout: it goes to a log file under
``--log-dir`` and only the parsed summary is printed. That keeps the manager's
context free of build noise, which is the one thing that decides whether a long
migration finishes.

``needs_agent`` in the output is the remaining reason to dispatch QA. It is set
only when a result needs interpreting rather than reporting -- an error in a
layer already marked done, a build failure this parser could not describe, a
file that would not parse. Everything else, dev can act on directly from the
findings.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from layers import LAYER_ORDER, classify
    from parse_mvn import summarize
    from routes import compare_routes, parse_play_routes, parse_spring_mappings
    from signature_diff import diff as signature_diff
    from signature_diff import load_tree
except ImportError:
    from .layers import LAYER_ORDER, classify
    from .parse_mvn import summarize
    from .routes import compare_routes, parse_play_routes, parse_spring_mappings
    from .signature_diff import diff as signature_diff
    from .signature_diff import load_tree

MVN_TIMEOUT = 900
JAVA_TIMEOUT = 300


def iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def skipped(reason: str) -> dict[str, Any]:
    """An unrun tier must never read as a passing one."""
    return {"status": "skipped", "reason": reason}


def run(cmd: list[str], cwd: Path | None = None, timeout: int = MVN_TIMEOUT):
    return subprocess.run(
        cmd, cwd=str(cwd) if cwd else None, capture_output=True,
        text=True, timeout=timeout,
    )


# --------------------------------------------------------------------------- T1


def tier_compile(
    spring_repo: Path, layer: str, log_dir: Path, goal: str = "compile"
) -> dict[str, Any]:
    log_path = log_dir / f"mvn-{goal}-{layer}.log"
    try:
        proc = run(["mvn", "-B", goal], cwd=spring_repo)
    except FileNotFoundError:
        return {"status": "error", "reason": "mvn not on PATH"}
    except subprocess.TimeoutExpired:
        return {"status": "error", "reason": f"mvn {goal} exceeded {MVN_TIMEOUT}s"}

    log = (proc.stdout or "") + (proc.stderr or "")
    log_path.write_text(log, encoding="utf-8")
    parsed = summarize(log)

    return {
        "status": "passed" if proc.returncode == 0 else "failed",
        "exit_code": proc.returncode,
        "log": str(log_path),
        "error_count": parsed["error_count"],
        "files_affected": parsed["files_affected"],
        "dependency_errors": parsed["dependency_errors"],
        "signatures": parsed["signatures"],
        "by_file": parsed["by_file"],
        "unparsed_tail": parsed["unparsed_tail"],
    }


def compile_findings(t1: dict[str, Any], layer: str) -> list[dict[str, Any]]:
    """
    One finding per affected file, not per error line.

    Errors cluster: a single missing import produces a dozen lines in one file.
    A per-line dump is unreadable and makes dev fix symptoms in order rather
    than the cause.
    """
    findings = []
    for path, errors in sorted((t1.get("by_file") or {}).items()):
        messages = sorted({e["message"] for e in errors})
        findings.append(
            {
                "layer": layer,
                "file": path,
                "tier": "T1",
                "severity": "blocker",
                "category": "compile-error",
                "evidence": f"{len(errors)} error(s): " + "; ".join(messages[:4]),
                "suggested_fix": "see the full log at " + str(t1.get("log", "")),
            }
        )
    for dep in t1.get("dependency_errors") or []:
        findings.append(
            {
                "layer": layer,
                "file": "pom.xml",
                "tier": "T1",
                "severity": "blocker",
                "category": "dependency-error",
                "evidence": dep[:400],
                "suggested_fix": "the dependency map in decisions.md is wrong; "
                                 "this belongs to the architect, not to dev",
            }
        )
    return findings


# --------------------------------------------------------------------------- T2


def extract_signatures(jar: Path, source_root: Path, out: Path) -> str | None:
    """Returns an error string, or None on success."""
    if not source_root.is_dir():
        out.write_text('{"files":{}}', encoding="utf-8")
        return None
    try:
        proc = run(
            ["java", "-jar", str(jar), "signature", str(source_root), "-o", str(out)],
            timeout=JAVA_TIMEOUT,
        )
    except FileNotFoundError:
        return "java not on PATH"
    except subprocess.TimeoutExpired:
        return f"signature extraction exceeded {JAVA_TIMEOUT}s"
    if proc.returncode != 0:
        return (proc.stderr or proc.stdout or "signature extraction failed")[:400]
    return None


def tier_preservation(
    play_root: Path,
    spring_root: Path,
    jar: Path,
    layer: str,
    cache_dir: Path,
    no_migration: set[str],
    layer_only: bool,
    refresh_cache: bool = False,
) -> dict[str, Any]:
    if not jar.is_file():
        return {"status": "error", "reason": f"dev-toolkit JAR not found at {jar}"}

    # The Play tree is read-only for the whole run, so its signatures are
    # extracted once and reused by every later layer. Only the Spring side moves.
    play_json = cache_dir / "play-signatures.json"
    if refresh_cache or not play_json.is_file():
        err = extract_signatures(jar, play_root, play_json)
        if err:
            return {"status": "error", "reason": f"Play side: {err}"}

    spring_json = cache_dir / "spring-signatures.json"
    err = extract_signatures(jar, spring_root, spring_json)
    if err:
        return {"status": "error", "reason": f"Spring side: {err}"}

    return signature_diff(
        load_tree(play_json),
        load_tree(spring_json),
        layer,
        no_migration=no_migration,
        layer_only=layer_only,
    )


# --------------------------------------------------------------------------- T3


def tier_routes(play_repo: Path, spring_root: Path, layer: str) -> dict[str, Any]:
    play_routes, notes = parse_play_routes(play_repo / "conf" / "routes")
    parity = compare_routes(play_routes, parse_spring_mappings(spring_root))
    parity["notes"] = notes
    parity["findings"] = [
        {
            "layer": layer,
            "file": missing.get("handler", "") or "controllers",
            "tier": "T3",
            "severity": "blocker",
            "category": "route-missing",
            "evidence": f"Play route {missing['verb']} {missing['path']} has no "
                        f"Spring mapping at the same verb and path",
            "suggested_fix": "add the mapping annotation to the migrated handler",
        }
        for missing in parity["missing"]
    ]
    return parity


# --------------------------------------------------------------------------- T4


def test_findings(t4: dict[str, Any], layer: str) -> list[dict[str, Any]]:
    if t4.get("status") != "failed":
        return []
    return [
        {
            "layer": layer,
            "file": path,
            "tier": "T4",
            "severity": "major",
            "category": "test-failure",
            "evidence": f"{len(errors)} failure(s) in {path}",
            "suggested_fix": "see " + str(t4.get("log", "")),
        }
        for path, errors in sorted((t4.get("by_file") or {}).items())
    ] or [
        {
            "layer": layer,
            "file": "(suite)",
            "tier": "T4",
            "severity": "major",
            "category": "test-failure",
            "evidence": f"mvn test exited {t4.get('exit_code')}",
            "suggested_fix": "see " + str(t4.get("log", "")),
        }
    ]


# --------------------------------------------------------------------------- verdict


def escalation_reasons(
    tiers: dict[str, Any], findings: list[dict[str, Any]], done_layers: set[str], layer: str
) -> list[str]:
    """
    When is a QA agent actually worth a dispatch?

    Only when the scripted result needs interpreting rather than reporting.
    Every other finding here already carries the evidence dev needs.
    """
    reasons: list[str] = []

    t1 = tiers.get("T1") or {}
    if t1.get("unparsed_tail"):
        reasons.append(
            "the build failed with [ERROR] lines this parser could not classify"
        )

    # A finding in a layer already signed off means the current layer broke
    # something upstream. Attributing that is judgment, and dev left to itself
    # will thrash in the wrong file.
    foreign = sorted(
        {
            classify(f["file"])
            for f in findings
            if f["tier"] == "T1" and classify(f["file"]) in done_layers
            and classify(f["file"]) != layer
        }
    )
    if foreign:
        reasons.append(
            "compile errors land in already-completed layer(s): " + ", ".join(foreign)
        )

    t2 = tiers.get("T2") or {}
    if t2.get("parse_errors"):
        reasons.append(
            f"{len(t2['parse_errors'])} file(s) would not parse, so T2 could not "
            "judge them"
        )

    for tier_name, tier in tiers.items():
        if tier.get("status") == "error":
            reasons.append(f"{tier_name} could not run: {tier.get('reason')}")

    return reasons


def verdict(tiers: dict[str, Any], findings: list[dict[str, Any]]) -> str:
    if any(t.get("status") in ("failed", "error") for t in tiers.values()):
        return "failed"
    if any(f["severity"] in ("blocker", "major") for f in findings):
        return "needs_review"
    return "passed"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the scripted verification tiers for a layer (JSON to stdout)."
    )
    parser.add_argument("--play-repo", type=Path, required=True)
    parser.add_argument("--spring-repo", type=Path, required=True)
    parser.add_argument("--layer", default="final")
    parser.add_argument("--play-java-root", default="app")
    parser.add_argument(
        "--final",
        action="store_true",
        help="Final pass: full-tree T2, plus T3 and T4.",
    )
    parser.add_argument("--jar", type=Path, default=None)
    parser.add_argument("--status-file", type=Path, default=None)
    parser.add_argument(
        "--tiers",
        default=None,
        help="Comma-separated override, e.g. T1,T2. Default follows the schedule.",
    )
    parser.add_argument("--log-dir", type=Path, default=None)
    parser.add_argument(
        "--refresh-cache",
        action="store_true",
        help="Re-extract the Play signatures instead of reusing the cached set.",
    )
    args = parser.parse_args()

    play_repo = args.play_repo.expanduser().resolve()
    spring_repo = args.spring_repo.expanduser().resolve()
    play_root = play_repo / args.play_java_root
    spring_root = spring_repo / "src" / "main" / "java"
    layer = "final" if args.final else args.layer

    migration_dir = spring_repo / ".migration"
    log_dir = args.log_dir or (migration_dir / "logs")
    cache_dir = migration_dir / "cache"
    for d in (log_dir, cache_dir):
        d.mkdir(parents=True, exist_ok=True)

    jar = args.jar or (play_repo / "dev-toolkit-1.0.0.jar")
    if not jar.is_file():
        fallback = Path(__file__).resolve().parents[2] / "lib" / "dev-toolkit-1.0.0.jar"
        if fallback.is_file():
            jar = fallback

    no_migration: set[str] = set()
    done_layers: set[str] = set()
    status_file = args.status_file or (spring_repo / "migration-status.json")
    if status_file.is_file():
        try:
            state = json.loads(status_file.read_text(encoding="utf-8"))
            no_migration = set(
                (state.get("architecture_review") or {}).get("no_migration") or []
            )
            done_layers = {
                name
                for name, entry in (state.get("layers") or {}).items()
                if isinstance(entry, dict) and entry.get("status") == "done"
            }
        except json.JSONDecodeError as e:
            print(f"[warn] could not read {status_file}: {e}", file=sys.stderr)

    # Tier schedule. T3 cannot pass before controllers exist and T4 cannot run
    # before the whole project compiles, so running them earlier manufactures
    # failures that teach the reader to ignore the gate.
    if args.tiers:
        selected = {t.strip().upper() for t in args.tiers.split(",") if t.strip()}
    else:
        selected = {"T1", "T2"}
        if args.final:
            selected |= {"T3", "T4"}
        elif layer == "controller":
            selected.add("T3")

    tiers: dict[str, Any] = {}
    findings: list[dict[str, Any]] = []

    if "T1" in selected:
        tiers["T1"] = tier_compile(spring_repo, layer, log_dir)
        findings.extend(compile_findings(tiers["T1"], layer))
    else:
        tiers["T1"] = skipped("not selected")

    if "T2" in selected:
        tiers["T2"] = tier_preservation(
            play_root, spring_root, jar, layer, cache_dir, no_migration,
            layer_only=not args.final and layer in LAYER_ORDER,
            refresh_cache=args.refresh_cache,
        )
        findings.extend(tiers["T2"].get("findings") or [])
    else:
        tiers["T2"] = skipped("not selected")

    if "T3" in selected:
        tiers["T3"] = tier_routes(play_repo, spring_root, layer)
        findings.extend(tiers["T3"].get("findings") or [])
    else:
        tiers["T3"] = skipped("route parity is meaningless before the controller layer")

    if "T4" in selected:
        tiers["T4"] = tier_compile(spring_repo, layer, log_dir, goal="test")
        findings.extend(test_findings(tiers["T4"], layer))
    else:
        tiers["T4"] = skipped("tests run at final only")

    reasons = escalation_reasons(tiers, findings, done_layers, layer)
    result = {
        "layer": layer,
        "checked_at": iso_now(),
        "tiers_run": sorted(selected),
        "status": verdict(tiers, findings),
        "tiers": {
            name: {k: v for k, v in tier.items() if k not in ("by_file", "findings")}
            for name, tier in tiers.items()
        },
        "findings": findings,
        "needs_agent": bool(reasons),
        "agent_reason": reasons,
    }

    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
