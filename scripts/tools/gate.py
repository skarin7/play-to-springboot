#!/usr/bin/env python3
"""
Run the verification tiers for a layer and return one verdict.

    python3 scripts/tools/gate.py --play-repo P --spring-repo S --layer service --jar J
    python3 scripts/tools/gate.py --play-repo P --spring-repo S --final --jar J

``--jar`` is the path ``scripts/tools/fetch_jar.py`` printed for this run --
a checksum-verified, version-pinned jar. There is no fallback path resolution
here; a caller that hasn't fetched the jar is an error, not a guess.

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

Before any tier runs, the Play-repo guard runs (``guard.py``). A non-clean
guard short-circuits everything: the result is ``{"status": "halt"}`` with exit
**4**, deliberately distinct from ``failed`` (exit 1), because a halt is an
integrity violation rather than a migration attempt that did not work. Running
tiers against a Play tree we can no longer trust would produce verdicts nobody
should act on.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from guard import evaluate as guard_evaluate
    from layers import LAYER_ORDER, classify, load_overrides
    from parse_mvn import summarize
    from routes import (
        compare_routes,
        parse_play_routes,
        parse_spring_mappings,
        parse_static_resource_handlers,
    )
    from signature_diff import EXEMPTIONS_FILE
    from signature_diff import diff as signature_diff
    from signature_diff import load_exemptions, load_tree
    from workspace import find_workspace_yaml, parse_workspace_yaml, timeout as ws_timeout
except ImportError:
    from .guard import evaluate as guard_evaluate
    from .layers import LAYER_ORDER, classify, load_overrides
    from .parse_mvn import summarize
    from .routes import (
        compare_routes,
        parse_play_routes,
        parse_spring_mappings,
        parse_static_resource_handlers,
    )
    from .signature_diff import EXEMPTIONS_FILE
    from .signature_diff import diff as signature_diff
    from .signature_diff import load_exemptions, load_tree
    from .workspace import (
        find_workspace_yaml,
        parse_workspace_yaml,
        timeout as ws_timeout,
    )

MVN_TIMEOUT = 900
JAVA_TIMEOUT = 300

EXIT_PASSED = 0
EXIT_NOT_PASSED = 1
EXIT_HALT = 4


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


def _as_text(chunk: Any) -> str:
    if chunk is None:
        return ""
    if isinstance(chunk, bytes):
        return chunk.decode("utf-8", errors="replace")
    return str(chunk)


def tier_compile(
    spring_repo: Path, layer: str, log_dir: Path, goal: str = "compile",
    timeout: int = MVN_TIMEOUT,
) -> dict[str, Any]:
    log_path = log_dir / f"mvn-{goal}-{layer}.log"
    try:
        proc = run(["mvn", "-B", goal], cwd=spring_repo, timeout=timeout)
    except FileNotFoundError:
        return {"status": "error", "reason": "mvn not on PATH"}
    except subprocess.TimeoutExpired as e:
        # Write what the build managed to say before it was killed. Returning
        # without this made the finding cite a log path that did not exist --
        # the one output the reader needs after a timeout is the tail of the
        # build that timed out.
        partial = _as_text(e.stdout) + _as_text(e.stderr)
        try:
            log_path.write_text(partial, encoding="utf-8")
        except OSError:
            pass
        return {
            "status": "error",
            "reason": f"mvn {goal} exceeded {timeout}s",
            "log": str(log_path),
            "partial": True,
            "unparsed_tail": summarize(partial)["unparsed_tail"] if partial else [],
        }

    log = (proc.stdout or "") + (proc.stderr or "")
    log_path.write_text(log, encoding="utf-8")
    parsed = summarize(log)

    return {
        "status": "passed" if proc.returncode == 0 else "failed",
        "exit_code": proc.returncode,
        "log": str(log_path),
        "partial": False,
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


def extract_signatures(
    jar: Path, source_root: Path, out: Path, timeout: int = JAVA_TIMEOUT
) -> str | None:
    """Returns an error string, or None on success."""
    if not source_root.is_dir():
        out.write_text('{"files":{}}', encoding="utf-8")
        return None
    try:
        proc = run(
            ["java", "-jar", str(jar), "signature", str(source_root), "-o", str(out)],
            timeout=timeout,
        )
    except FileNotFoundError:
        return "java not on PATH"
    except subprocess.TimeoutExpired:
        return f"signature extraction exceeded {timeout}s"
    if proc.returncode != 0:
        return (proc.stderr or proc.stdout or "signature extraction failed")[:400]
    return None


def exemptions_state(spring_repo: Path, approved_sha: str | None) -> dict[str, Any]:
    """
    Report whether the approved exemption set is still the one in effect.

    Exemptions suppress blockers, so they are exactly the lever an agent under
    pressure reaches for. The mechanism stays honest by being *visible*: the
    manager records the sha of the file the human approved at Gate 1, and every
    gate re-hashes it. An edit afterwards is not blocked -- it is reported,
    escalated to QA, and rendered red.
    """
    path = spring_repo / ".migration" / EXEMPTIONS_FILE
    if not path.is_file():
        return {"file": None, "sha256": None, "approved_sha256": approved_sha,
                "modified_after_gate": False}
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return {
        "file": str(path),
        "sha256": digest,
        "approved_sha256": approved_sha,
        "modified_after_gate": bool(approved_sha) and digest != approved_sha,
    }


def journal_anomalies_state(status: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Report journal shrinks recorded by ``state.py fold-journal``.

    A journal shorter than its last recorded offset is not blocked --
    ``fold_journal`` already recovers by replaying from the top, because a
    reused filename on a fresh run looks identical to a truncated one and the
    manager must not stall on it. But the two are not the same event, and only
    a human or QA can tell them apart, so every such shrink is carried here
    into the gate the same way an exemptions-file edit is: reported and
    escalated, never silent.
    """
    return list(status.get("journal_anomalies") or [])


def tier_preservation(
    play_root: Path,
    spring_root: Path,
    jar: Path,
    layer: str,
    cache_dir: Path,
    no_migration: set[str],
    layer_only: bool,
    refresh_cache: bool = False,
    overrides: dict[str, str] | None = None,
    exemptions: dict[str, dict[str, str]] | None = None,
    timeout: int = JAVA_TIMEOUT,
) -> dict[str, Any]:
    if not jar.is_file():
        return {"status": "error", "reason": f"dev-toolkit JAR not found at {jar}"}

    # The Play tree is read-only for the whole run, so its signatures are
    # extracted once and reused by every later layer. Only the Spring side moves.
    play_json = cache_dir / "play-signatures.json"
    if refresh_cache or not play_json.is_file():
        err = extract_signatures(jar, play_root, play_json, timeout)
        if err:
            return {"status": "error", "reason": f"Play side: {err}"}

    spring_json = cache_dir / "spring-signatures.json"
    err = extract_signatures(jar, spring_root, spring_json, timeout)
    if err:
        return {"status": "error", "reason": f"Spring side: {err}"}

    return signature_diff(
        load_tree(play_json),
        load_tree(spring_json),
        layer,
        no_migration=no_migration,
        layer_only=layer_only,
        overrides=overrides,
        exemptions=exemptions,
    )


# --------------------------------------------------------------------------- T3


def tier_routes(
    play_repo: Path, spring_repo: Path, spring_root: Path, layer: str,
    assets_policy: str = "skip",
) -> dict[str, Any]:
    play_routes, notes = parse_play_routes(play_repo / "conf" / "routes")
    parity = compare_routes(
        play_routes,
        parse_spring_mappings(spring_root),
        static_routes=parse_static_resource_handlers(spring_repo),
        assets_policy=assets_policy,
    )
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
    tiers: dict[str, Any],
    findings: list[dict[str, Any]],
    done_layers: set[str],
    layer: str,
    overrides: dict[str, str] | None = None,
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
            classify(f["file"], overrides)
            for f in findings
            if f["tier"] == "T1" and classify(f["file"], overrides) in done_layers
            and classify(f["file"], overrides) != layer
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


def verdict(
    tiers: dict[str, Any],
    findings: list[dict[str, Any]],
    guard: dict[str, Any] | None = None,
) -> str:
    # The guard is checked first and outranks every tier. A clean compile on a
    # Play tree somebody wrote to is not a result, it is a lie with a green tick.
    if guard is not None and guard.get("status") != "clean":
        return "halt"
    if any(t.get("status") in ("failed", "error") for t in tiers.values()):
        return "failed"
    if any(f["severity"] in ("blocker", "major") for f in findings):
        return "needs_review"
    return "passed"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the scripted verification tiers for a layer (JSON to stdout).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples (J = the path fetch_jar.py printed):\n"
            "  gate.py --play-repo ../play --spring-repo ../spring --layer init "
            "--tiers T1 --jar J\n"
            "  gate.py --play-repo ../play --spring-repo ../spring --layer service --jar J\n"
            "  gate.py --play-repo ../play --spring-repo ../spring --final --jar J\n"
            "  gate.py --play-repo ../play --spring-repo ../spring --final "
            "--tiers T1,T2,T3 --jar J   # --skip-tests\n"
            "\n"
            "Exit codes: 0 passed, 1 failed/needs_review, 4 halt (guard not clean).\n"
            "The Play-repo guard runs before T1 and outranks every tier.\n"
        ),
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
    parser.add_argument(
        "--jar", type=Path, required=True,
        help="Path to the dev-toolkit jar, as printed by scripts/tools/fetch_jar.py.",
    )
    parser.add_argument("--status-file", type=Path, default=None)
    parser.add_argument(
        "--layer-overrides",
        type=Path,
        default=None,
        help="Path to layer-overrides.json (default: <spring-repo>/.migration/layer-overrides.json).",
    )
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
    parser.add_argument(
        "--mvn-timeout", type=int, default=None,
        help=f"Seconds before mvn is killed (default: mvn_timeout from "
             f"workspace.yaml, else {MVN_TIMEOUT}).",
    )
    parser.add_argument(
        "--java-timeout", type=int, default=None,
        help=f"Seconds before signature extraction is killed (default: "
             f"java_timeout from workspace.yaml, else {JAVA_TIMEOUT}).",
    )
    parser.add_argument(
        "--workspace-yaml", type=Path, default=None,
        help="workspace.yaml to read timeout defaults from "
             "(default: <spring-repo>/../workspace.yaml).",
    )
    parser.add_argument(
        "--assets-policy",
        choices=("skip", "require"),
        default="skip",
        help="skip (default): Play's built-in asset routes and Twirl view handlers "
             "are reported out_of_scope rather than missing. require: demand a real "
             "Spring mapping for every Play route.",
    )
    parser.add_argument(
        "--skip-guard",
        action="store_true",
        help="Do not run the Play-repo guard. For tool development only -- a run "
             "that skips it has no read-only invariant.",
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

    jar = args.jar.expanduser().resolve()

    workspace = parse_workspace_yaml(
        args.workspace_yaml or find_workspace_yaml(spring_repo)
    )
    mvn_timeout = ws_timeout(workspace, "mvn_timeout", args.mvn_timeout, MVN_TIMEOUT)
    java_timeout = ws_timeout(workspace, "java_timeout", args.java_timeout, JAVA_TIMEOUT)

    # The guard runs before anything else and can end the call on its own. It is
    # deliberately not a tier: a tier reports on the migration, this reports on
    # whether the migration is still trustworthy enough to report on.
    guard: dict[str, Any] | None = None
    if not args.skip_guard:
        guard = guard_evaluate(play_repo, spring_repo)
        if guard["status"] != "clean":
            halt = {
                "layer": layer,
                "checked_at": iso_now(),
                "status": "halt",
                "halt_reason": guard.get("reason") or guard["status"],
                "guard": guard,
                "tiers_run": [],
                "tiers": {},
                "findings": [],
                "needs_agent": False,
                "agent_reason": [],
            }
            json.dump(halt, sys.stdout, indent=2)
            sys.stdout.write("\n")
            return EXIT_HALT

    no_migration: set[str] = set()
    done_layers: set[str] = set()
    approved_exemptions_sha: str | None = None
    journal_anomalies: list[dict[str, Any]] = []
    status_file = args.status_file or (spring_repo / "migration-status.json")
    if status_file.is_file():
        try:
            state = json.loads(status_file.read_text(encoding="utf-8"))
            no_migration = set(
                (state.get("architecture_review") or {}).get("no_migration") or []
            )
            approved_exemptions_sha = (
                (state.get("architecture_review") or {}).get("exemptions_sha256")
            )
            done_layers = {
                name
                for name, entry in (state.get("layers") or {}).items()
                if isinstance(entry, dict) and entry.get("status") == "done"
            }
            journal_anomalies = journal_anomalies_state(state)
        except json.JSONDecodeError as e:
            print(f"[warn] could not read {status_file}: {e}", file=sys.stderr)

    layer_overrides_path = args.layer_overrides or (migration_dir / "layer-overrides.json")
    overrides = load_overrides(layer_overrides_path)

    exemptions = load_exemptions(migration_dir / EXEMPTIONS_FILE)
    exemptions_info = exemptions_state(spring_repo, approved_exemptions_sha)

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
        tiers["T1"] = tier_compile(spring_repo, layer, log_dir, timeout=mvn_timeout)
        findings.extend(compile_findings(tiers["T1"], layer))
    else:
        tiers["T1"] = skipped("not selected")

    if "T2" in selected:
        tiers["T2"] = tier_preservation(
            play_root, spring_root, jar, layer, cache_dir, no_migration,
            layer_only=not args.final and layer in LAYER_ORDER,
            refresh_cache=args.refresh_cache,
            overrides=overrides,
            exemptions=exemptions,
            timeout=java_timeout,
        )
        findings.extend(tiers["T2"].get("findings") or [])
    else:
        tiers["T2"] = skipped("not selected")

    if "T3" in selected:
        tiers["T3"] = tier_routes(
            play_repo, spring_repo, spring_root, layer, args.assets_policy
        )
        findings.extend(tiers["T3"].get("findings") or [])
    else:
        tiers["T3"] = skipped("route parity is meaningless before the controller layer")

    if "T4" in selected:
        tiers["T4"] = tier_compile(
            spring_repo, layer, log_dir, goal="test", timeout=mvn_timeout
        )
        findings.extend(test_findings(tiers["T4"], layer))
    else:
        tiers["T4"] = skipped("tests run at final only")

    reasons = escalation_reasons(tiers, findings, done_layers, layer, overrides)
    if journal_anomalies:
        # Not a halt -- fold_journal already recovered by replaying from the
        # top -- but a journal shrinking is indistinguishable from tampering
        # without a human or QA looking at it, so it is never silent.
        for a in journal_anomalies:
            reasons.append(
                f"journal {a.get('journal')} shrank since last fold "
                f"(expected at least {a.get('expected_at_least')} lines, found "
                f"{a.get('found')}); replayed from the top -- confirm this is a "
                "reused filename, not a truncated journal"
            )
    if exemptions_info["modified_after_gate"]:
        # Not a halt -- the file may have been edited for a legitimate reason --
        # but never silent. A human approved a specific set; this one is
        # different, and a person has to say whether that is fine.
        reasons.append(
            "signature-exemptions.json changed after Gate 1 approval "
            f"({(exemptions_info['approved_sha256'] or '')[:12]} -> "
            f"{(exemptions_info['sha256'] or '')[:12]}); a suppression set nobody "
            "approved is suppressing blockers"
        )

    result = {
        "layer": layer,
        "checked_at": iso_now(),
        "guard": guard or {"status": "skipped", "reason": "--skip-guard"},
        "exemptions": exemptions_info,
        "journal_anomalies": journal_anomalies,
        "tiers_run": sorted(selected),
        "status": verdict(tiers, findings, guard),
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
    if result["status"] == "halt":
        return EXIT_HALT
    return EXIT_PASSED if result["status"] == "passed" else EXIT_NOT_PASSED


if __name__ == "__main__":
    sys.exit(main())
