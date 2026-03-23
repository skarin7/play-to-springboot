#!/usr/bin/env python3
"""
Autonomous Play → Spring migration orchestrator.
Single state file: migration-status.json (see play-to-spring-kit skills).

Only --play-repo is required (or PLAY_REPO): the script always runs the kit
workspace bootstrap first (idempotent: skills, JAR, workspace.yaml, Spring dirs),
then resolves Spring repo from workspace.yaml or spring-<play-basename>.

Intended usage: cd into the kit clone (play-to-spring-kit) and run
``python3 scripts/migration_orchestrator.py --play-repo ../your-play-app``.
Kit paths come from ``__file__`` (not cwd); ``--play-repo`` / ``--workspace`` are
resolved relative to the shell's current working directory.

Exit codes: 0 OK, 1 unexpected error, 2 stuck compile (no cursor), 3 init not done,
            4 budget exhausted, 5 fail-fast failure.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import time
from typing import Any

# ---------------------------------------------------------------------------
# Defaults (override via env / CLI)
# ---------------------------------------------------------------------------

DEFAULT_CURSOR_MODEL = "composer-2"


def abs_path(p: Path) -> Path:
    """Expand ~ and resolve to absolute (relative segments use process cwd)."""
    return p.expanduser().resolve(strict=False)


def scripts_dir() -> Path:
    """Directory containing this script and ``setup.sh``."""
    return Path(__file__).resolve().parent


def kit_root() -> Path:
    """play-to-spring-kit root (``lib/``, ``skills/``, ``config/``, …)."""
    return scripts_dir().parent


def parse_workspace_yaml(yaml_path: Path) -> dict[str, str]:
    """Minimal key: value reader for kit-generated workspace.yaml (no PyYAML)."""
    out: dict[str, str] = {}
    if not yaml_path.is_file():
        return out
    for line in yaml_path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if ":" not in s:
            continue
        key, _, val = s.partition(":")
        key = key.strip()
        val = val.split("#", 1)[0].strip().strip('"').strip("'")
        if key in (
            "play_repo",
            "spring_repo",
            "migration_root",
            "batch_size",
            "base_package",
            "kit_path",
        ):
            out[key] = val
    return out


def resolve_spring_repo(
    play_repo: Path,
    workspace_dir: Path,
    spring_name: str | None,
) -> Path:
    """
    Same layout as kit bootstrap: read workspace.yaml in workspace_dir, else
    <workspace_dir>/spring-<play_basename> or <workspace_dir>/<spring_name>.
    """
    ws_yaml = workspace_dir / "workspace.yaml"
    data = parse_workspace_yaml(ws_yaml)
    if data.get("spring_repo"):
        spr = Path(data["spring_repo"]).expanduser()
        spr = spr.resolve() if spr.is_absolute() else (workspace_dir / spr).resolve()
        declared = data.get("play_repo")
        if declared:
            try:
                decl_p = Path(declared).expanduser().resolve()
                if decl_p != play_repo.resolve():
                    print(
                        "[warn] workspace.yaml play_repo does not match --play-repo; "
                        f"using spring_repo from yaml ({spr}).",
                        file=sys.stderr,
                    )
            except OSError:
                pass
        return spr
    if spring_name:
        return (workspace_dir / spring_name).resolve()
    return (workspace_dir / f"spring-{play_repo.name}").resolve()


def resolve_status_path(
    spring_repo: Path,
    cli_path: Path | None,
) -> Path:
    """
    Precedence: --status-file, then MIGRATION_STATUS_FILE, else
    <spring-repo>/migration-status.json
    """
    if cli_path is not None:
        return abs_path(cli_path)
    env_p = os.environ.get("MIGRATION_STATUS_FILE")
    if env_p:
        return abs_path(Path(env_p))
    return (spring_repo / "migration-status.json").resolve(strict=False)


def run_setup_sh(
    play_repo: Path,
    workspace_dir: Path,
    spring_name: str | None,
    dry_run: bool,
) -> int:
    """Run kit install/bootstrap (idempotent). Called automatically at orchestrator start."""
    setup_sh = scripts_dir() / "setup.sh"
    if not setup_sh.is_file():
        print(f"ERROR: kit install script missing at {setup_sh}", file=sys.stderr)
        return 1
    # Always pass --workspace so setup and this script use the same directory (default = parent of play).
    cmd = [
        "bash",
        str(setup_sh),
        str(play_repo),
        "--workspace",
        str(workspace_dir),
    ]
    if spring_name:
        cmd.extend(["--spring-name", spring_name])
    if dry_run:
        print("[dry-run]", " ".join(shlex.quote(a) for a in cmd), file=sys.stderr)
        return 0
    p = subprocess.run(
        cmd,
        cwd=str(kit_root()),
        capture_output=True,
        text=True,
    )
    if p.stdout:
        print(p.stdout, end="" if p.stdout.endswith("\n") else "\n")
    if p.stderr:
        print(p.stderr, end="" if p.stderr.endswith("\n") else "\n", file=sys.stderr)
    return p.returncode


LAYER_ORDER = (
    "model",
    "repository",
    "manager",
    "service",
    "controller",
    "other",
)

MIGRATE_DONE_RE = re.compile(
    r"migrate-app done:\s*(\d+)\s*files,\s*(\d+)\s*errors,\s*(\d+)\s*remaining",
    re.IGNORECASE,
)

# Maven: [ERROR] /path/File.java:[10,20] error: message
MVN_ERROR_RE = re.compile(
    r"\[ERROR\]\s+(.+\.java):\[(\d+),[^\]]+\]\s*(.+)",
)

COMPILATION_ERROR_LINE_RE = re.compile(r"^\[ERROR\]\s+COMPILATION ERROR", re.MULTILINE)


def iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def classify_layer(relative_path: str) -> str:
    """LayerDetector-aligned order: controller → service → model → manager → repository → other."""
    p = relative_path.replace("\\", "/").lower()
    if "/controllers/" in p:
        return "controller"
    if "/service/" in p or "/services/" in p:
        return "service"
    if "/models/" in p or p.endswith("model.java"):
        return "model"
    if "/db/" in p:
        return "manager"
    if "/repositories/" in p or "/dao/" in p:
        return "repository"
    return "other"


def scan_play_java(play_root: Path, java_subdir: str = "app") -> dict[str, Any]:
    root = play_root / java_subdir
    by_layer: dict[str, int] = {k: 0 for k in LAYER_ORDER}
    total = 0
    if not root.is_dir():
        return {
            "captured_at": iso_now(),
            "play_java_root": java_subdir,
            "total_java_files": 0,
            "by_layer": by_layer,
        }
    for f in root.rglob("*.java"):
        if not f.is_file():
            continue
        try:
            rel = f.relative_to(play_root)
        except ValueError:
            rel = f
        layer = classify_layer(str(rel))
        by_layer[layer] = by_layer.get(layer, 0) + 1
        total += 1
    return {
        "captured_at": iso_now(),
        "play_java_root": java_subdir,
        "total_java_files": total,
        "by_layer": by_layer,
    }


def scan_spring_java(spring_root: Path) -> tuple[int, dict[str, int]]:
    src = spring_root / "src" / "main" / "java"
    by_layer: dict[str, int] = {k: 0 for k in LAYER_ORDER}
    total = 0
    if not src.is_dir():
        return 0, by_layer
    for f in src.rglob("*.java"):
        if not f.is_file():
            continue
        try:
            rel = f.relative_to(src)
        except ValueError:
            rel = f
        layer = classify_layer(str(rel))
        by_layer[layer] = by_layer.get(layer, 0) + 1
        total += 1
    return total, by_layer


def default_layer_entry() -> dict[str, Any]:
    return {
        "status": "pending",
        "retry_count": 0,
        "llm_calls": 0,
        "errors_history": [],
        "files_migrated": 0,
        "files_failed": [],
        "validate_iteration": 0,
        "last_error_count": None,
        "failure_reason": None,
    }


def default_autonomous() -> dict[str, Any]:
    return {
        "total_llm_calls": 0,
        "max_total_llm_calls": int(os.environ.get("MAX_TOTAL_LLM_CALLS", "50")),
        "cursor_model": DEFAULT_CURSOR_MODEL,
        "max_files_per_cursor_session": int(
            os.environ.get("MAX_FILES_PER_CURSOR_SESSION", "10")
        ),
        "last_errors_path": None,
    }


def merge_status(raw: dict[str, Any]) -> dict[str, Any]:
    """Ensure all expected keys exist (backward compatible)."""
    out = dict(raw)
    out.setdefault("current_step", "transform_validate")
    out.setdefault("initialize", {})
    init = out["initialize"]
    init.setdefault("status", "pending")
    init.setdefault("pom_generated", False)
    init.setdefault("application_java_generated", False)
    init.setdefault("application_properties_generated", False)
    init.setdefault("error", None)

    out.setdefault("layers", {})
    for layer in LAYER_ORDER:
        le = {**default_layer_entry(), **out["layers"].get(layer, {})}
        out["layers"][layer] = le

    out.setdefault("failed_layers", [])
    out.setdefault("autonomous", {})
    auto = {**default_autonomous(), **out["autonomous"]}
    # env can tighten max if JSON has higher? Use max of JSON and env default from first run — keep JSON value if set
    if "max_total_llm_calls" not in raw.get("autonomous", {}):
        auto["max_total_llm_calls"] = int(os.environ.get("MAX_TOTAL_LLM_CALLS", "50"))
    out["autonomous"] = auto

    out.setdefault("source_inventory", None)
    out.setdefault("migration_verification", None)
    return out


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def parse_migrate_output(text: str) -> tuple[int, int, int] | None:
    m = MIGRATE_DONE_RE.search(text)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def parse_mvn_errors(log: str) -> list[dict[str, Any]]:
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


def count_compilation_errors(log: str) -> int:
    return len(COMPILATION_ERROR_LINE_RE.findall(log))


def normalize_errors(errors: list[dict[str, Any]]) -> list[str]:
    sigs = []
    for e in errors:
        fp = e.get("file", "")
        line = e.get("line", 0)
        msg = (e.get("message") or "").strip()
        sigs.append(f"{fp}:{line}:{msg}")
    return sorted(sigs)


def is_looping(current: list[str], history: list[list[str]]) -> bool:
    if len(history) < 1:
        return False
    if current == history[-1]:
        return True
    if len(history) >= 2 and current == history[-2]:
        return True
    if len(history) >= 1 and len(current) > len(history[-1]) + 2:
        return True
    return False


def resolve_model(args: argparse.Namespace, status: dict[str, Any]) -> str:
    if getattr(args, "cursor_model", None):
        return args.cursor_model
    for key in ("CURSOR_MODEL", "MIGRATION_CURSOR_MODEL"):
        v = os.environ.get(key)
        if v:
            return v.strip()
    m = status.get("autonomous", {}).get("cursor_model")
    if m:
        return str(m)
    return DEFAULT_CURSOR_MODEL


@dataclass
class Guardrails:
    max_retries_per_layer: int = 5
    max_total_llm_calls: int = 50
    max_errors_llm: int = 10
    max_files_per_fix: int = 3
    timeout_layer_mins: int = 30
    max_files_per_cursor_session: int = 10
    cursor_agent_timeout_sec: int = 1800


def load_guardrails(args: argparse.Namespace, status: dict[str, Any]) -> Guardrails:
    auto = status.get("autonomous", {})
    return Guardrails(
        max_retries_per_layer=int(
            os.environ.get("MAX_RETRIES_PER_LAYER", str(args.max_retries_per_layer))
        ),
        max_total_llm_calls=int(
            auto.get(
                "max_total_llm_calls",
                int(os.environ.get("MAX_TOTAL_LLM_CALLS", str(args.max_total_llm_calls))),
            )
        ),
        max_errors_llm=int(
            os.environ.get("MAX_ERRORS_TO_SEND_LLM", str(args.max_errors_llm))
        ),
        max_files_per_fix=int(os.environ.get("MAX_FILES_PER_FIX", str(args.max_files_fix))),
        timeout_layer_mins=int(
            os.environ.get("TIMEOUT_PER_LAYER_MINS", str(args.timeout_layer_mins))
        ),
        max_files_per_cursor_session=int(
            auto.get(
                "max_files_per_cursor_session",
                int(
                    os.environ.get(
                        "MAX_FILES_PER_CURSOR_SESSION",
                        str(args.max_files_cursor_session),
                    )
                ),
            )
        ),
        cursor_agent_timeout_sec=int(
            os.environ.get("CURSOR_AGENT_TIMEOUT_SEC", "1800")
        ),
    )


def run_cmd(
    argv: list[str],
    cwd: Path | None,
    dry_run: bool,
) -> subprocess.CompletedProcess[str]:
    if dry_run:
        print("[dry-run]", " ".join(argv), file=sys.stderr)
        return subprocess.CompletedProcess(argv, 0, "", "")
    return subprocess.run(
        argv,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=None,
    )


def run_mvn_compile(spring_repo: Path, dry_run: bool) -> tuple[int, str]:
    if dry_run:
        print("[dry-run] mvn -q compile", file=sys.stderr)
        return 0, ""
    p = subprocess.run(
        ["mvn", "-q", "compile"],
        cwd=str(spring_repo),
        capture_output=True,
        text=True,
        timeout=3600,
    )
    out = (p.stdout or "") + (p.stderr or "")
    return p.returncode, out


def group_errors_by_file(errors: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    g: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in errors:
        # normalize path key: take basename or full path as in log
        key = e.get("file", "unknown")
        g[key].append(e)
    return dict(g)


def build_error_batches(
    errors: list[dict[str, Any]],
    max_files: int,
) -> list[list[dict[str, Any]]]:
    """Batch errors by file; up to max_files distinct files per batch."""
    by_file = group_errors_by_file(errors)
    files = list(by_file.keys())
    batches: list[list[dict[str, Any]]] = []
    for i in range(0, len(files), max_files):
        chunk_files = files[i : i + max_files]
        batch: list[dict[str, Any]] = []
        for fp in chunk_files:
            batch.extend(by_file[fp])
        batches.append(batch)
    if not batches and errors:
        batches.append(errors[:])
    return batches


def call_cursor_agent(
    model: str,
    prompt: str,
    api_key: str,
    timeout_sec: int,
    dry_run: bool,
) -> subprocess.CompletedProcess[str]:
    argv = [
        "cursor-agent",
        "-p",
        "-m",
        model,
        "--output-format",
        "json",
        "--api-key",
        api_key,
        prompt,
    ]
    if dry_run:
        print("[dry-run]", "cursor-agent ...", file=sys.stderr)
        return subprocess.CompletedProcess(argv, 0, "{}", "")
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout_sec,
    )


def run_migrate_layer(
    play_repo: Path,
    jar: Path,
    spring_repo: Path,
    layer: str,
    batch_size: int | None,
    dry_run: bool,
) -> tuple[str, int, int, int]:
    argv = [
        "java",
        "-jar",
        str(jar),
        "migrate-app",
        "--layer",
        layer,
        "--target",
        str(spring_repo),
    ]
    if batch_size:
        argv.extend(["--batch-size", str(batch_size)])
    proc = run_cmd(argv, play_repo, dry_run)
    out = (proc.stdout or "") + (proc.stderr or "")
    parsed = parse_migrate_output(out)
    if parsed is None:
        return out, 0, 0, -1
    n, m, r = parsed
    return out, n, m, r


def migrate_until_done(
    play_repo: Path,
    jar: Path,
    spring_repo: Path,
    layer: str,
    batch_size: int | None,
    dry_run: bool,
) -> tuple[int, int]:
    """Returns (total_files_processed, total_errors)."""
    total_n = 0
    total_m = 0
    prev_r = None
    while True:
        _, n, m, r = run_migrate_layer(
            play_repo, jar, spring_repo, layer, batch_size, dry_run
        )
        total_n += n
        total_m += m
        if dry_run:
            break
        if r < 0:
            break
        if r == 0:
            break
        if prev_r is not None and r >= prev_r:
            print(
                f"[warn] migrate-app remaining did not decrease ({prev_r} -> {r}), stopping.",
                file=sys.stderr,
            )
            break
        prev_r = r
    return total_n, total_m


def run_verification(
    status: dict[str, Any],
    play_repo: Path,
    spring_repo: Path,
) -> dict[str, Any]:
    inv = status.get("source_inventory") or {}
    by_play = inv.get("by_layer") or {k: 0 for k in LAYER_ORDER}
    spring_total, by_spring = scan_spring_java(spring_repo)
    layer_comp: dict[str, Any] = {}
    worst = "passed"
    notes: list[str] = []
    for layer in LAYER_ORDER:
        exp = int(by_play.get(layer, 0) or 0)
        act = int(by_spring.get(layer, 0) or 0)
        delta = act - exp
        layer_comp[layer] = {
            "play_expected": exp,
            "spring_actual": act,
            "delta": delta,
        }
        if exp > 0 and act < exp - 5:
            worst = "failed"
            notes.append(f"{layer}: spring count much lower than play ({act} vs {exp})")
        elif exp > 0 and act < exp:
            if worst == "passed":
                worst = "needs_review"
            notes.append(f"{layer}: fewer Spring files than Play ({act} vs {exp})")
    return {
        "status": worst,
        "checked_at": iso_now(),
        "spring_java_total": spring_total,
        "notes": "; ".join(notes) if notes else "Counts within tolerance or Play empty.",
        "layer_comparison": layer_comp,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Play → Spring migration orchestrator (migration-status.json only). "
            "Runs kit workspace bootstrap first; only --play-repo is required."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Typical run from the kit directory (relative paths use your shell cwd):\n"
            "  cd path/to/play-to-spring-kit\n"
            "  python3 scripts/migration_orchestrator.py --play-repo ../my-play-app\n"
            "\n"
            "Kit bootstrap and lib/ are resolved from this script's location, not from cwd."
        ),
    )
    pr = os.environ.get("PLAY_REPO")
    sr = os.environ.get("SPRING_REPO")
    parser.add_argument(
        "--play-repo",
        type=Path,
        default=Path(pr) if pr else None,
        help="Play project root (required unless PLAY_REPO). Kit bootstrap runs for this path first.",
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        default=None,
        help="Migration workspace (default: parent of play repo). Resolved relative to cwd if relative.",
    )
    parser.add_argument(
        "--spring-name",
        default=None,
        help="Spring directory name under workspace if no workspace.yaml (default: spring-<play-basename>).",
    )
    parser.add_argument(
        "--spring-repo",
        type=Path,
        default=Path(sr) if sr else None,
        help="Spring project root (optional: derived from workspace.yaml or spring-<basename>).",
    )
    parser.add_argument(
        "--jar",
        type=Path,
        default=None,
        help="dev-toolkit JAR (default: <play-repo>/dev-toolkit-1.0.0.jar).",
    )
    parser.add_argument(
        "--status-file",
        type=Path,
        default=None,
        help="State file (default: <spring-repo>/migration-status.json). Overrides MIGRATION_STATUS_FILE.",
    )
    parser.add_argument("--batch-size", type=int, default=None)
    parser.add_argument(
        "--cursor-model",
        default=os.environ.get("CURSOR_MODEL")
        or os.environ.get("MIGRATION_CURSOR_MODEL")
        or None,
        help=f"cursor-agent -m (default {DEFAULT_CURSOR_MODEL} or JSON).",
    )
    parser.add_argument("--no-cursor", action="store_true")
    parser.add_argument("--fail-fast", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-inventory", action="store_true")
    parser.add_argument("--refresh-inventory", action="store_true")
    parser.add_argument("--max-retries-per-layer", type=int, default=5)
    parser.add_argument("--max-total-llm-calls", type=int, default=50)
    parser.add_argument("--max-errors-llm", type=int, default=10)
    parser.add_argument("--max-files-fix", type=int, default=3)
    parser.add_argument("--timeout-layer-mins", type=int, default=30)
    parser.add_argument("--max-files-cursor-session", type=int, default=10)

    args = parser.parse_args()

    if not args.play_repo:
        print("ERROR: --play-repo (or PLAY_REPO) required.", file=sys.stderr)
        return 1

    play_repo = abs_path(args.play_repo)
    workspace_dir = abs_path(args.workspace) if args.workspace else play_repo.parent

    rc = run_setup_sh(
        play_repo,
        workspace_dir,
        args.spring_name,
        args.dry_run,
    )
    if rc != 0:
        return 1

    if args.spring_repo:
        spring_repo = abs_path(args.spring_repo)
    elif os.environ.get("SPRING_REPO"):
        spring_repo = abs_path(Path(os.environ["SPRING_REPO"]))
    else:
        spring_repo = resolve_spring_repo(play_repo, workspace_dir, args.spring_name)

    status_path = resolve_status_path(spring_repo, args.status_file)

    if not status_path.is_file():
        print(
            f"ERROR: status file not found: {status_path}\n"
            f"  Expected under Spring repo after builder/orchestrator creates it: {spring_repo}",
            file=sys.stderr,
        )
        return 1

    raw = json.loads(status_path.read_text(encoding="utf-8"))
    status = merge_status(raw)

    if status["initialize"].get("status") != "done":
        print(
            "Initialize is not done. Run builder/agent to generate pom, Application, properties first.",
            file=sys.stderr,
        )
        return 3

    jar = abs_path(args.jar) if args.jar else (play_repo / "dev-toolkit-1.0.0.jar")
    if not jar.is_file():
        print(f"ERROR: JAR not found: {jar}", file=sys.stderr)
        return 1

    use_cursor = not args.no_cursor and bool(os.environ.get("CURSOR_API_KEY"))
    api_key = os.environ.get("CURSOR_API_KEY", "")
    model = resolve_model(args, status)
    status["autonomous"]["cursor_model"] = model
    gr = load_guardrails(args, status)
    status["autonomous"]["max_total_llm_calls"] = gr.max_total_llm_calls
    status["autonomous"]["max_files_per_cursor_session"] = gr.max_files_per_cursor_session

    mig_dir = spring_repo / ".migration"
    mig_dir.mkdir(parents=True, exist_ok=True)

    # Source inventory
    if not args.skip_inventory and (
        args.refresh_inventory
        or not status.get("source_inventory")
        or not status["source_inventory"].get("captured_at")
    ):
        status["source_inventory"] = scan_play_java(play_repo)
        atomic_write_json(status_path, status)

    status["current_step"] = "transform_validate"
    layer_start_time: dict[str, float] = {}

    try:
        for layer in LAYER_ORDER:
            le = status["layers"][layer]
            if le.get("status") == "done":
                continue

            if layer not in layer_start_time:
                layer_start_time[layer] = time.monotonic()

            # Transform
            le["status"] = "in_progress"
            atomic_write_json(status_path, status)

            n_add, m_err = migrate_until_done(
                play_repo, jar, spring_repo, layer, args.batch_size, args.dry_run
            )
            le["files_migrated"] = int(le.get("files_migrated", 0)) + n_add
            if m_err:
                print(f"[warn] migrate-app reported {m_err} errors for layer {layer}", file=sys.stderr)

            # Compile / fix loop
            while True:
                elapsed_mins = (time.monotonic() - layer_start_time[layer]) / 60.0
                if elapsed_mins > gr.timeout_layer_mins:
                    le["status"] = "timeout"
                    le["failure_reason"] = "timeout"
                    status["failed_layers"].append(
                        {
                            "layer": layer,
                            "reason": "timeout",
                            "last_errors_summary": None,
                        }
                    )
                    atomic_write_json(status_path, status)
                    if args.fail_fast:
                        return 5
                    break

                le["status"] = "compiling"
                atomic_write_json(status_path, status)

                code, log = run_mvn_compile(spring_repo, args.dry_run)
                le["validate_iteration"] = int(le.get("validate_iteration", 0)) + 1
                err_count = count_compilation_errors(log) or len(parse_mvn_errors(log))
                le["last_error_count"] = err_count

                if code == 0:
                    le["status"] = "done"
                    le["failure_reason"] = None
                    le["errors_history"] = []
                    le.pop("compile_error_counts", None)
                    atomic_write_json(status_path, status)
                    break

                errors = parse_mvn_errors(log)
                if not errors and log:
                    # fallback: one blob
                    errors = [{"file": "unknown", "line": 0, "message": log[-4000:]}]

                norm = normalize_errors(errors)
                hist: list[list[str]] = list(le.get("errors_history") or [])

                if is_looping(norm, hist):
                    le["status"] = "loop_detected"
                    le["failure_reason"] = "loop_detected"
                    status["failed_layers"].append(
                        {
                            "layer": layer,
                            "reason": "loop_detected",
                            "last_errors_summary": norm[:20],
                            "errors_history_tail": hist[-2:] if len(hist) >= 2 else hist,
                        }
                    )
                    atomic_write_json(status_path, status)
                    if args.fail_fast:
                        return 5
                    break

                # Stuck without cursor: three consecutive compiles where error count never goes down
                if not use_cursor:
                    if "compile_error_counts" not in le:
                        le["compile_error_counts"] = []
                    le["compile_error_counts"] = (le["compile_error_counts"] + [err_count])[-5:]
                    cc = le["compile_error_counts"]
                    if len(cc) >= 3 and cc[-1] >= cc[-2] >= cc[-3]:
                        print(
                            "Stuck compile (no cursor): error count not decreasing for 3 runs.",
                            file=sys.stderr,
                        )
                        le["status"] = "failed"
                        le["failure_reason"] = "stuck_compile"
                        atomic_write_json(status_path, status)
                        return 2

                if int(status["autonomous"]["total_llm_calls"]) >= gr.max_total_llm_calls:
                    le["status"] = "budget_exhausted"
                    le["failure_reason"] = "budget_exhausted"
                    status["failed_layers"].append(
                        {"layer": layer, "reason": "budget_exhausted"}
                    )
                    atomic_write_json(status_path, status)
                    return 4

                if int(le.get("retry_count", 0)) >= gr.max_retries_per_layer:
                    le["status"] = "failed"
                    le["failure_reason"] = "max_retries"
                    status["failed_layers"].append(
                        {
                            "layer": layer,
                            "reason": "max_retries",
                            "last_errors_summary": norm[:20],
                        }
                    )
                    atomic_write_json(status_path, status)
                    if args.fail_fast:
                        return 5
                    break

                if use_cursor:
                    le["status"] = "fixing"
                    err_path = mig_dir / f"errors-{layer}.json"
                    err_path.write_text(json.dumps(errors, indent=2), encoding="utf-8")
                    status["autonomous"]["last_errors_path"] = str(err_path)
                    atomic_write_json(status_path, status)

                    batches = build_error_batches(
                        errors, gr.max_files_per_cursor_session
                    )
                    if not batches:
                        batches = [errors[: gr.max_errors_llm]]

                    for batch in batches:
                        top = batch[: gr.max_errors_llm]
                        prompt = f"""Fix Spring Boot compilation errors for layer "{layer}".
Errors (JSON, top {len(top)}):
{json.dumps(top, indent=2)}

Rules:
- Fix one error category at a time where possible.
- Do not change public method signatures unless required to compile.
- Touch at most {gr.max_files_per_fix} files in this attempt.
- Only edit files under the Spring project; preserve business logic.
- Spring repo root: {spring_repo}
"""
                        proc = call_cursor_agent(
                            model,
                            prompt,
                            api_key,
                            gr.cursor_agent_timeout_sec,
                            args.dry_run,
                        )
                        if proc.returncode != 0:
                            print(proc.stderr or proc.stdout, file=sys.stderr)
                        le["llm_calls"] = int(le.get("llm_calls", 0)) + 1
                        status["autonomous"]["total_llm_calls"] = int(
                            status["autonomous"]["total_llm_calls"]
                        ) + 1

                    le["retry_count"] = int(le.get("retry_count", 0)) + 1
                    hist.append(norm)
                    le["errors_history"] = hist[-20:]
                    atomic_write_json(status_path, status)
                else:
                    # No cursor: no code changes; stuck detection uses compile_error_counts only
                    le["retry_count"] = int(le.get("retry_count", 0)) + 1
                    hist.append(norm)
                    le["errors_history"] = hist[-20:]
                    atomic_write_json(status_path, status)
                # loop: compile again

        # Final compile check
        code, _ = run_mvn_compile(spring_repo, args.dry_run)
        if code != 0:
            print("WARNING: final mvn compile failed after all layers.", file=sys.stderr)

        status["current_step"] = "verify"
        status["migration_verification"] = run_verification(status, play_repo, spring_repo)
        status["current_step"] = "done"
        atomic_write_json(status_path, status)

    except subprocess.TimeoutExpired:
        print("ERROR: subprocess timed out.", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
