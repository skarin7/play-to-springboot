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

    out.setdefault("source_inventory", None)
    out.setdefault("migration_verification", None)
    # T5. None rather than {} so an unrun endpoint check is distinguishable from
    # one that ran and found nothing.
    out.setdefault("endpoint_verification", None)
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


def fold_journal(status: dict[str, Any], journal: Path, layer: str | None) -> int:
    """
    Replay an append-only NDJSON journal into state.

    This is the crash-recovery path: a dev subagent killed partway through a
    layer leaves its completed actions on disk, so the manager can resume at the
    right file instead of losing everything the dead context held. Malformed
    trailing lines (the usual signature of a process killed mid-write) are
    skipped rather than aborting the fold.
    """
    if not journal.is_file():
        return 0
    folded = 0
    for line in journal.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            print(f"[warn] skipping malformed journal line: {line[:80]}", file=sys.stderr)
            continue
        target = entry.get("layer") or layer
        if not target or target not in status["layers"]:
            continue
        le = status["layers"][target]
        action = entry.get("action")
        if action == "migrated":
            le["files_migrated"] = int(le.get("files_migrated", 0)) + int(
                entry.get("count", 1)
            )
        elif action == "failed":
            failed = le.setdefault("files_failed", [])
            f = entry.get("file")
            if f and f not in failed:
                failed.append(f)
        elif action == "compiled":
            le["validate_iteration"] = int(le.get("validate_iteration", 0)) + 1
            le["last_error_count"] = entry.get("error_count")
        folded += 1
    return folded


def cmd_init(args, status_path: Path) -> int:
    status = read_status(status_path)
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


def cmd_gate(args, status_path: Path) -> int:
    status = read_status(status_path)
    status["gates"][args.name] = {"human": args.value, "at": iso_now()}
    atomic_write_json(status_path, status)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--status-file", type=Path, required=True)
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init")

    p_show = sub.add_parser("show")
    p_show.add_argument("--path", default=None, help="Dotted path, e.g. layers.model")

    p_set = sub.add_parser("set")
    p_set.add_argument("--path", required=True)
    p_set.add_argument("--value", required=True)

    p_find = sub.add_parser("add-finding")
    p_find.add_argument("--json", required=True)

    p_fold = sub.add_parser("fold-journal")
    p_fold.add_argument("--journal", type=Path, required=True)
    p_fold.add_argument("--layer", default=None)

    p_gate = sub.add_parser("gate")
    p_gate.add_argument("--name", required=True)
    p_gate.add_argument("--value", required=True)

    args = parser.parse_args()
    status_path = args.status_file.expanduser().resolve()

    handlers = {
        "init": cmd_init,
        "show": cmd_show,
        "set": cmd_set,
        "add-finding": cmd_add_finding,
        "fold-journal": cmd_fold_journal,
        "gate": cmd_gate,
    }
    try:
        return handlers[args.cmd](args, status_path)
    except (KeyError, TypeError, json.JSONDecodeError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
