#!/usr/bin/env python3
"""
Read/modify/write migration-status.json -- the single source of truth.

**Only the manager runs this.** Subagents report via artifacts under
``.migration/`` and via append-only journals; the manager folds those in. Two
writers corrupt the file, and a subagent killed mid-write leaves broken JSON
that destroys resumability.

Every write is atomic (temp file + replace) and defaults-merging, so a status
file written by an older version keeps all of its fields.

    python3 scripts/tools/state.py init        --status-file S
    python3 scripts/tools/state.py show        --status-file S [--path layers.model]
    python3 scripts/tools/state.py set         --status-file S --path layers.model.status --value done
    python3 scripts/tools/state.py add-finding --status-file S --json '{...}'
    python3 scripts/tools/state.py fold-journal --status-file S --journal J --layer model
    python3 scripts/tools/state.py gate        --status-file S --name architecture --value approved

``--status-file`` may go before or after the subcommand; both orders are
accepted. ``--help`` on any subcommand lists its real flags.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from layers import LAYER_ORDER
except ImportError:
    from .layers import LAYER_ORDER


def iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def default_layer_entry() -> dict[str, Any]:
    return {
        "status": "pending",
        "files_migrated": 0,
        "files_failed": [],
        "validate_iteration": 0,
        "last_error_count": None,
        "failure_reason": None,
        "remaining_files": None,
        "batches_completed": 0,
    }


def merge_status(raw: dict[str, Any]) -> dict[str, Any]:
    """
    Fill in every expected key without touching what is already there.

    Additive by construction: fields written by earlier versions of the kit
    survive untouched, so an in-flight migration can be resumed after an upgrade.
    """
    out = dict(raw)
    out.setdefault("current_step", "research")
    out.setdefault("mode", None)  # collapsed | full, chosen from inventory

    # Scope decided at launch, from the skill's arguments. Pre-declared because
    # a message sent mid-run reaches the manager but never a subagent already
    # running -- "skip T5" said out loud while QA is booting an app is heard by
    # nobody. What can be decided up front is decided up front; what cannot goes
    # through .migration/run-control.json, which the manager re-reads at loop
    # boundaries.
    rc = out.setdefault("run_config", {})
    rc.setdefault("skip_t5", False)
    rc.setdefault("skip_tests", False)
    rc.setdefault("no_boot", False)
    rc.setdefault("mode_override", None)     # collapsed | full
    rc.setdefault("max_dispatches", None)
    rc.setdefault("assets_policy", "skip")   # skip | require
    rc.setdefault("raw_arguments", None)

    init = out.setdefault("initialize", {})
    init.setdefault("status", "pending")
    init.setdefault("pom_generated", False)
    init.setdefault("application_java_generated", False)
    init.setdefault("application_properties_generated", False)
    init.setdefault("error", None)

    layers = out.setdefault("layers", {})
    for layer in LAYER_ORDER:
        layers[layer] = {**default_layer_entry(), **layers.get(layer, {})}

    research = out.setdefault("research", {})
    research.setdefault("status", "pending")
    research.setdefault("captured_at", None)
    research.setdefault("artifact", ".migration/research.md")

    arch = out.setdefault("architecture_review", {})
    arch.setdefault("status", "pending")  # pending | approved | revise
    arch.setdefault("decisions", ".migration/decisions.md")
    arch.setdefault("no_migration", [])
    arch.setdefault("concerns", [])
    # T2 suppressions the human approved at Gate 1, and the sha of the file as
    # approved. gate.py re-hashes on every run: a set edited afterwards is
    # reported rather than trusted, because suppressing blockers is the one
    # power here worth watching.
    arch.setdefault("exemptions", [])
    arch.setdefault("exemptions_sha256", None)
    arch.setdefault("exemptions_modified_after_gate", False)

    out.setdefault("qa_findings", [])
    out.setdefault("failed_layers", [])
    out.setdefault("commits", {})

    gates = out.setdefault("gates", {})
    gates.setdefault("mode", "milestone")  # milestone | strict

    attempts = out.setdefault("attempts", {})
    for layer in LAYER_ORDER:
        entry = attempts.get(layer, {})
        entry.setdefault("count", 0)
        entry.setdefault("error_signatures", [])
        entry.setdefault("last_findings", [])
        attempts[layer] = entry

    # How far each dev journal has been folded, keyed by file name. Without it
    # every re-fold of an append-only journal counts the same lines again.
    out.setdefault("journal_offsets", {})

    out.setdefault("source_inventory", None)
    out.setdefault("migration_verification", None)

    # What this migration does not translate: Twirl templates, static assets,
    # i18n bundles. Seeded from inventory.py, confirmed by the architect at
    # Gate 1. It lives in state rather than in decisions.md because a policy
    # only prose records is a policy the report cannot show and the tiers
    # cannot honour -- which is how three Twirl templates got hand-ported into
    # a template engine nobody chose.
    oos = out.setdefault("out_of_scope", {})
    oos.setdefault("captured_at", None)
    oos.setdefault("policy", "left-in-place")
    oos.setdefault("total_files", 0)
    oos.setdefault("categories", {})
    # T5. None rather than {} so an unrun endpoint check is distinguishable from
    # one that ran and found nothing.
    out.setdefault("endpoint_verification", None)

    # What the run itself cost: wall clock, and one row per subagent dispatch.
    # A run that finished clean still has a price, and without this the report
    # can only say that it worked -- not whether it worked cheaply enough to be
    # worth running on a repo ten times the size. `tokens` is folded in by
    # report.py from the session transcripts; the dispatch rows come from the
    # manager, which is the only participant that sees a subagent's usage line.
    rm = out.setdefault("run_metrics", {})
    rm.setdefault("started_at", None)
    rm.setdefault("finished_at", None)
    rm.setdefault("duration_seconds", None)
    rm.setdefault("session_id", None)          # for token_report --session
    rm.setdefault("transcript_project", None)  # manager cwd, for the slug
    rm.setdefault("dispatches", [])
    rm.setdefault("tokens", None)
    return out


def read_status(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return merge_status({})
    return merge_status(json.loads(path.read_text(encoding="utf-8")))


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def get_path(data: dict[str, Any], dotted: str) -> Any:
    cur: Any = data
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            raise KeyError(f"no such path: {dotted}")
        cur = cur[part]
    return cur


def set_path(data: dict[str, Any], dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    cur: Any = data
    for part in parts[:-1]:
        cur = cur.setdefault(part, {})
        if not isinstance(cur, dict):
            raise TypeError(f"cannot descend into non-object at {part} in {dotted}")
    cur[parts[-1]] = value


def coerce(value: str) -> Any:
    """Let --value carry JSON when it parses, plain string otherwise."""
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def next_finding_id(status: dict[str, Any]) -> str:
    used = {
        f.get("id", "")
        for f in status.get("qa_findings", [])
        if isinstance(f, dict)
    }
    n = 1
    while f"F-{n:03d}" in used:
        n += 1
    return f"F-{n:03d}"


def salvage_collided_line(line: str) -> dict[str, Any] | None:
    """
    Recover the trailing entry from a line two writes collided on.

    A dev killed mid-append leaves a partial line with no newline of its own, so
    the next dev's append lands on that same line:

        {"layer":"service","action":"fail{"layer":"service","action":"migrated",...}

    The torn prefix is gone for good, but the suffix is a whole entry, and
    dropping the line loses a batch result that really happened -- counters then
    drift *low*, which is worse than noisy because a short count reads as a
    layer that still has work left.

    Only a suffix that parses *and* carries an "action" is accepted, so a nested
    object inside a single well-formed entry is never mistaken for a second one.
    Scanning left to right takes the earliest such boundary, which is the real
    one: anything earlier belongs to the torn prefix and cannot parse.
    """
    for i in range(1, len(line)):
        if line[i] != "{":
            continue
        try:
            entry = json.loads(line[i:])
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict) and "action" in entry:
            return entry
    return None


def fold_journal(status: dict[str, Any], journal: Path, layer: str | None) -> int:
    """
    Replay an append-only NDJSON journal into state.

    This is the crash-recovery path: a dev subagent killed partway through a
    layer leaves its completed actions on disk, so the manager can resume at the
    right file instead of losing everything the dead context held. Malformed
    trailing lines (the usual signature of a process killed mid-write) are
    skipped rather than aborting the fold.

    Folding is idempotent. The journal is append-only and one file covers a whole
    layer, but the manager folds after *every* batch and every re-dispatch, so a
    plain replay re-adds every line it already counted -- a three-file layer
    re-dispatched twice reported nine files migrated. ``journal_offsets`` records
    how far each journal has been consumed and the next fold starts there.
    """
    if not journal.is_file():
        return 0
    lines = journal.read_text(encoding="utf-8").splitlines()
    offsets = status.setdefault("journal_offsets", {})
    key = journal.name
    start = int(offsets.get(key, 0))
    if start > len(lines):
        # The journal shrank, so it is not the file we measured -- a fresh run
        # reusing the name, or one truncated by hand. Replay it from the top
        # rather than trusting an offset into a file that no longer exists.
        start = 0
    folded = 0
    consumed = start
    for i in range(start, len(lines)):
        line = lines[i].strip()
        if not line:
            consumed = i + 1
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            entry = salvage_collided_line(line)
            if entry is None:
                print(
                    f"[warn] skipping malformed journal line: {line[:80]}",
                    file=sys.stderr,
                )
                if i == len(lines) - 1:
                    # A half-written trailing line is the signature of a killed
                    # writer. Leave the offset short of it so a later append that
                    # makes the line readable is still folded.
                    break
                consumed = i + 1
                continue
            print(
                f"[warn] recovered the trailing entry from a torn journal line: "
                f"{line[:80]}",
                file=sys.stderr,
            )
        consumed = i + 1
        target = entry.get("layer") or layer
        if not target or target not in status["layers"]:
            continue
        le = status["layers"][target]
        action = entry.get("action")
        if action == "migrated":
            le["files_migrated"] = int(le.get("files_migrated", 0)) + int(
                entry.get("count", 1)
            )
            if "remaining" in entry:
                le["remaining_files"] = entry.get("remaining")
        elif action == "failed":
            failed = le.setdefault("files_failed", [])
            f = entry.get("file")
            if f and f not in failed:
                failed.append(f)
        elif action == "compiled":
            le["validate_iteration"] = int(le.get("validate_iteration", 0)) + 1
            le["last_error_count"] = entry.get("error_count")
        folded += 1
    offsets[key] = consumed
    return folded


def cmd_init(args, status_path: Path) -> int:
    status = read_status(status_path)
    # Stamped once. A resumed run keeps its original start, so the duration in
    # the report is wall clock for the whole migration rather than for whatever
    # fragment of it happened after the last interruption.
    rm = status["run_metrics"]
    if not rm.get("started_at"):
        rm["started_at"] = iso_now()
    if getattr(args, "session_id", None):
        rm["session_id"] = args.session_id
    if getattr(args, "transcript_project", None):
        rm["transcript_project"] = str(Path(args.transcript_project).expanduser())
    atomic_write_json(status_path, status)
    print(f"initialized {status_path}", file=sys.stderr)
    return 0


def cmd_show(args, status_path: Path) -> int:
    status = read_status(status_path)
    data = get_path(status, args.path) if args.path else status
    json.dump(data, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


def cmd_set(args, status_path: Path) -> int:
    status = read_status(status_path)
    set_path(status, args.path, coerce(args.value))
    atomic_write_json(status_path, status)
    return 0


def cmd_add_finding(args, status_path: Path) -> int:
    status = read_status(status_path)
    finding = json.loads(args.json)
    required = {"layer", "file", "tier", "severity"}
    missing = required - set(finding)
    if missing:
        print(f"ERROR: finding missing keys: {sorted(missing)}", file=sys.stderr)
        return 1
    finding.setdefault("id", next_finding_id(status))
    finding.setdefault("status", "open")
    finding.setdefault("created_at", iso_now())
    status["qa_findings"].append(finding)
    atomic_write_json(status_path, status)
    print(finding["id"])
    return 0


def cmd_fold_journal(args, status_path: Path) -> int:
    status = read_status(status_path)
    n = fold_journal(status, args.journal.expanduser().resolve(), args.layer)
    atomic_write_json(status_path, status)
    print(f"folded {n} journal entries", file=sys.stderr)
    return 0


def cmd_add_dispatch(args, status_path: Path) -> int:
    """
    Record one subagent dispatch: role, what it was for, and what it cost.

    The manager is the only participant that ever sees a subagent's usage line
    -- the subagent's own context is discarded when it reports, and the
    transcript can attribute tokens to "some sidechain" but not to "the dev
    dispatch for the controller layer". So the attribution has to be written
    down at the moment the dispatch returns or it is gone.
    """
    status = read_status(status_path)
    entry = json.loads(args.json)
    if not entry.get("role"):
        print("ERROR: dispatch needs at least a 'role'", file=sys.stderr)
        return 1
    entry.setdefault("layer", None)
    entry.setdefault("mode", None)
    entry.setdefault("duration_ms", None)
    entry.setdefault("tokens", None)
    entry.setdefault("tool_uses", None)
    entry.setdefault("at", iso_now())
    rows = status["run_metrics"].setdefault("dispatches", [])
    rows.append(entry)
    atomic_write_json(status_path, status)
    print(len(rows))
    return 0


def cmd_finish(args, status_path: Path) -> int:
    """Close the wall clock. Idempotent: re-running restamps the end."""
    status = read_status(status_path)
    rm = status["run_metrics"]
    rm["finished_at"] = iso_now()
    started = rm.get("started_at")
    if started:
        try:
            t0 = datetime.strptime(started, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            t1 = datetime.strptime(rm["finished_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            rm["duration_seconds"] = int((t1 - t0).total_seconds())
        except ValueError:
            rm["duration_seconds"] = None
    atomic_write_json(status_path, status)
    json.dump({k: rm[k] for k in ("started_at", "finished_at", "duration_seconds")},
              sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


def cmd_gate(args, status_path: Path) -> int:
    status = read_status(status_path)
    status["gates"][args.name] = {"human": args.value, "at": iso_now()}
    atomic_write_json(status_path, status)
    return 0


def cmd_bump_attempt(args, status_path: Path) -> int:
    """
    Count one failed dev attempt on a layer, or reset the count after a pass.

    The escalation trigger is ``attempts.<layer>.count`` reaching 3, but nothing
    used to move it: it stayed 0 for the whole run while a layer burned six gate
    iterations, so the retry cap never fired and no layer ever reached
    ``failed_layers``. Signature sets ride along because a stuck loop is
    identified by repeats, not by the count alone.
    """
    status = read_status(status_path)
    if args.layer not in status["layers"]:
        print(f"ERROR: no such layer: {args.layer}", file=sys.stderr)
        return 1
    entry = status["attempts"].setdefault(
        args.layer, {"count": 0, "error_signatures": [], "last_findings": []}
    )
    if args.reset:
        entry["count"] = 0
        entry["error_signatures"] = []
        entry["last_findings"] = []
    else:
        entry["count"] = int(entry.get("count", 0)) + 1
        if args.signatures:
            sigs = entry.setdefault("error_signatures", [])
            sigs.append(json.loads(args.signatures))
            # Only the last three matter: the skill compares consecutive sets to
            # tell a stuck loop from progress, and older ones just grow the file.
            del sigs[:-3]
        if args.findings:
            entry["last_findings"] = json.loads(args.findings)
    atomic_write_json(status_path, status)
    print(entry["count"])
    return 0


def main() -> int:
    # --status-file is defined on a shared parent and attached to *every*
    # subparser as well as the main parser, so both orders work:
    #
    #     state.py --status-file S show
    #     state.py show --status-file S
    #
    # argparse otherwise accepts only the first, and rejects the second with a
    # message about an unrecognised argument. That is a trap rather than a
    # constraint: the tool's own docstring showed the rejected order for
    # several versions, and every caller who copied it lost a round trip
    # discovering the CLI instead of doing the work.
    # default=SUPPRESS matters: without it the subparser writes its own default
    # (None) over a value the main parser already read, so the "before" order
    # would parse and then lose the path.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--status-file", type=Path, default=argparse.SUPPRESS,
        help="Path to migration-status.json. May appear before or after the "
             "subcommand.",
    )

    parser = argparse.ArgumentParser(
        description=__doc__.split("\n")[1],
        parents=[common],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples (S = <spring-repo>/migration-status.json):\n"
            "  state.py init --status-file S\n"
            "  state.py show --status-file S --path layers.service\n"
            "  state.py set  --status-file S --path layers.service.status --value done\n"
            "  state.py set  --status-file S --path run_config.skip_t5 --value true\n"
            "  state.py add-finding --status-file S --json '{\"layer\":\"service\","
            "\"file\":\"X.java\",\"tier\":\"T2\",\"severity\":\"blocker\","
            "\"category\":\"method-missing\",\"evidence\":\"...\"}'\n"
            "  state.py fold-journal --status-file S \\\n"
            "      --journal .migration/journal/service-dev.ndjson --layer service\n"
            "  state.py gate --status-file S --name architecture --value approved\n"
            "  state.py add-dispatch --status-file S --json '{\"role\":\"dev\","
            "\"layer\":\"service\",\"duration_ms\":195379,\"tokens\":53462}'\n"
            "  state.py finish --status-file S\n"
            "\n"
            "--value parses as JSON when it can (true, 3, [\"a\"]), else as a string.\n"
        ),
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser(
        "init", parents=[common], help="Create/normalise the status file."
    )
    p_init.add_argument(
        "--session-id", default=None,
        help="Claude Code session uuid, so report.py can scope token accounting "
             "to this run instead of every session in the project.",
    )
    p_init.add_argument(
        "--transcript-project", default=None,
        help="Manager cwd, used to find ~/.claude/projects/<slug>/. Defaults to "
             "report.py's own --token-project at render time.",
    )

    p_show = sub.add_parser(
        "show", parents=[common], help="Print the whole file, or one dotted path."
    )
    p_show.add_argument("--path", default=None, help="Dotted path, e.g. layers.model")

    p_set = sub.add_parser("set", parents=[common], help="Set one dotted path.")
    p_set.add_argument("--path", required=True)
    p_set.add_argument("--value", required=True)

    p_find = sub.add_parser(
        "add-finding", parents=[common],
        help="Append a finding; prints the assigned id.",
    )
    p_find.add_argument(
        "--json", required=True,
        help="Requires layer, file, tier, severity. id/status/created_at are filled in.",
    )

    p_fold = sub.add_parser(
        "fold-journal", parents=[common], help="Replay a dev NDJSON journal into state."
    )
    p_fold.add_argument("--journal", type=Path, required=True)
    p_fold.add_argument("--layer", default=None)

    p_disp = sub.add_parser(
        "add-dispatch", parents=[common],
        help="Record one subagent dispatch and its cost; prints the row count.",
    )
    p_disp.add_argument(
        "--json", required=True,
        help='Requires role. e.g. \'{"role":"dev","layer":"service",'
             '"mode":"transform","duration_ms":195379,"tokens":53462,"tool_uses":27}\'',
    )

    sub.add_parser(
        "finish", parents=[common],
        help="Stamp finished_at and compute duration_seconds.",
    )

    p_gate = sub.add_parser("gate", parents=[common], help="Record a human gate decision.")
    p_gate.add_argument("--name", required=True, help="e.g. architecture")
    p_gate.add_argument("--value", required=True, help="approved | revise")

    p_bump = sub.add_parser(
        "bump-attempt", parents=[common],
        help="Count a failed dev attempt on a layer; prints the new count.",
    )
    p_bump.add_argument("--layer", required=True)
    p_bump.add_argument(
        "--reset", action="store_true",
        help="Zero the count instead of incrementing, after a batch passes the gate.",
    )
    p_bump.add_argument(
        "--signatures", default=None,
        help="JSON array of this attempt's T1 error signatures. Last 3 sets kept.",
    )
    p_bump.add_argument(
        "--findings", default=None, help="JSON array of finding ids from this attempt.",
    )

    args = parser.parse_args()
    status_file = getattr(args, "status_file", None)
    if status_file is None:
        parser.error("--status-file is required (before or after the subcommand)")
    status_path = status_file.expanduser().resolve()

    handlers = {
        "init": cmd_init,
        "show": cmd_show,
        "set": cmd_set,
        "add-finding": cmd_add_finding,
        "fold-journal": cmd_fold_journal,
        "gate": cmd_gate,
        "bump-attempt": cmd_bump_attempt,
        "add-dispatch": cmd_add_dispatch,
        "finish": cmd_finish,
    }
    try:
        return handlers[args.cmd](args, status_path)
    except (KeyError, TypeError, json.JSONDecodeError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
