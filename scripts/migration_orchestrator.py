#!/usr/bin/env python3
"""
Workspace bootstrap and deterministic status reporting for a Play -> Spring migration.

This script no longer orchestrates. Sequencing, model selection, retry policy,
and failure handling now live in the ``migrate`` skill
(``skills/migrate/SKILL.md``) and are carried out by the coding agent; what
remains here is the deterministic work that agents do badly and scripts do
exactly: preparing the workspace and reporting counts. The ``migrate`` skill
calls ``setup`` itself on first run -- this is exposed for manual/scripted use
and debugging.

    python3 scripts/migration_orchestrator.py setup  --play-repo ../my-play-app
    python3 scripts/migration_orchestrator.py status --play-repo ../my-play-app
    python3 scripts/migration_orchestrator.py verify --play-repo ../my-play-app

Then run ``/play-to-springboot:migrate ../my-play-app`` in Claude Code.

Previously this file drove the whole migration itself: it picked a Cursor model
through a four-level precedence chain, capped LLM calls, retried layers, guessed
at stuck loops, and shelled out to ``cursor-agent`` with the API key on the
command line. All of that is gone. The agent owns those decisions now, which is
the point of the change -- and the API key is no longer visible in ``ps``.

Exit codes: 0 OK, 1 error, 3 initialize not done.
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent / "tools"
sys.path.insert(0, str(TOOLS))

from state import read_status  # noqa: E402


def scripts_dir() -> Path:
    return Path(__file__).resolve().parent


def kit_root() -> Path:
    """Kit root (lib/, skills/, agents/, config/). Resolved from this file, not cwd."""
    return scripts_dir().parent


def abs_path(p: Path) -> Path:
    return p.expanduser().resolve(strict=False)


def parse_workspace_yaml(yaml_path: Path) -> dict[str, str]:
    """Minimal key: value reader for the kit-generated workspace.yaml (no PyYAML)."""
    out: dict[str, str] = {}
    if not yaml_path.is_file():
        return out
    for line in yaml_path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or ":" not in s:
            continue
        key, _, val = s.partition(":")
        key = key.strip()
        val = val.split("#", 1)[0].strip().strip('"').strip("'")
        if key in ("play_repo", "spring_repo", "migration_root", "batch_size",
                   "base_package", "kit_path"):
            out[key] = val
    return out


def resolve_spring_repo(play_repo: Path, workspace_dir: Path,
                        spring_name: str | None) -> Path:
    ws = parse_workspace_yaml(workspace_dir / "workspace.yaml")
    if ws.get("spring_repo"):
        spr = Path(ws["spring_repo"]).expanduser()
        return spr.resolve() if spr.is_absolute() else (workspace_dir / spr).resolve()
    if spring_name:
        return (workspace_dir / spring_name).resolve()
    return (workspace_dir / f"spring-{play_repo.name}").resolve()


def run_tool(name: str, args: list[str]) -> int:
    """Run a helper from scripts/tools and stream its output through."""
    cmd = [sys.executable, str(TOOLS / name), *args]
    print(f"$ {' '.join(shlex.quote(c) for c in cmd)}", file=sys.stderr)
    return subprocess.run(cmd).returncode


def cmd_setup(args) -> int:
    play_repo = abs_path(args.play_repo)
    workspace = abs_path(args.workspace) if args.workspace else play_repo.parent
    setup_sh = scripts_dir() / "setup.sh"
    if not setup_sh.is_file():
        print(f"ERROR: missing {setup_sh}", file=sys.stderr)
        return 1
    cmd = ["bash", str(setup_sh), str(play_repo), "--workspace", str(workspace)]
    if args.spring_name:
        cmd += ["--spring-name", args.spring_name]
    return subprocess.run(cmd, cwd=str(kit_root())).returncode


def cmd_status(args) -> int:
    play_repo = abs_path(args.play_repo)
    workspace = abs_path(args.workspace) if args.workspace else play_repo.parent
    spring_repo = (abs_path(args.spring_repo) if args.spring_repo
                   else resolve_spring_repo(play_repo, workspace, args.spring_name))

    rc = run_tool("inventory.py", ["--play-repo", str(play_repo),
                                   "--spring-repo", str(spring_repo)])

    status_path = spring_repo / "migration-status.json"
    if not status_path.is_file():
        print(f"\nNo status file yet at {status_path}."
              f"\nThe migrate skill creates it on first run.",
              file=sys.stderr)
        return rc

    status = read_status(status_path)
    print("\n--- migration-status.json ---", file=sys.stderr)
    print(f"mode:          {status.get('mode')}", file=sys.stderr)
    print(f"current_step:  {status.get('current_step')}", file=sys.stderr)
    print(f"initialize:    {status['initialize'].get('status')}", file=sys.stderr)
    print(f"architecture:  {status['architecture_review'].get('status')}", file=sys.stderr)
    for layer, entry in status["layers"].items():
        print(f"  {layer:<11} {entry.get('status')}"
              f"  files={entry.get('files_migrated')}", file=sys.stderr)
    open_findings = [f for f in status.get("qa_findings", [])
                     if f.get("status") == "open"]
    if open_findings:
        print(f"\nopen findings: {len(open_findings)}", file=sys.stderr)
        for f in open_findings:
            print(f"  [{f.get('severity')}] {f.get('id')} {f.get('file')}: "
                  f"{f.get('evidence')}", file=sys.stderr)
    return rc


def cmd_verify(args) -> int:
    play_repo = abs_path(args.play_repo)
    workspace = abs_path(args.workspace) if args.workspace else play_repo.parent
    spring_repo = (abs_path(args.spring_repo) if args.spring_repo
                   else resolve_spring_repo(play_repo, workspace, args.spring_name))
    status_path = spring_repo / "migration-status.json"

    if status_path.is_file():
        status = read_status(status_path)
        if status["initialize"].get("status") != "done":
            print("Initialize is not done; run /play-to-springboot:migrate first.",
                  file=sys.stderr)
            return 3

    tool_args = ["--play-repo", str(play_repo), "--spring-repo", str(spring_repo)]
    if status_path.is_file():
        tool_args += ["--status-file", str(status_path)]
    if args.skip_routes:
        tool_args.append("--skip-routes")
    return run_tool("verify.py", tool_args)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.strip().split("\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Orchestration lives in the migrate skill, not here.\n"
            "Run this for workspace setup and for deterministic counts.\n"
        ),
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    for name, help_text in (
        ("setup", "Prepare the workspace (idempotent)."),
        ("status", "Inventory plus a summary of migration-status.json."),
        ("verify", "Completeness and route parity."),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--play-repo", type=Path, required=True)
        p.add_argument("--workspace", type=Path, default=None)
        p.add_argument("--spring-repo", type=Path, default=None)
        p.add_argument("--spring-name", default=None)
        if name == "verify":
            p.add_argument("--skip-routes", action="store_true")

    args = parser.parse_args()
    return {"setup": cmd_setup, "status": cmd_status, "verify": cmd_verify}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
