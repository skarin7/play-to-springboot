#!/usr/bin/env python3
"""
Start and -- more importantly -- reliably stop the two applications T5 compares.

    python3 scripts/tools/boot.py preflight --app play   --repo P
    python3 scripts/tools/boot.py start     --app play   --repo P --port 9000 \\
        --run-dir <spring>/.migration/run
    python3 scripts/tools/boot.py status    --run-dir <spring>/.migration/run
    python3 scripts/tools/boot.py stop      --run-dir <spring>/.migration/run --app play
    python3 scripts/tools/boot.py stop-all  --run-dir <spring>/.migration/run

T5 is the only tier that needs a running application, and booting one from an
agent used to be two lines of prose: ``sbt run &``. Three things went wrong with
that, and all three are fixed structurally here rather than by asking an agent
to be careful:

**Orphans.** ``(cd x && sbt run &)`` backgrounds a subshell. sbt then forks a
JVM, which forks the application. The agent captured no pid, so nothing was ever
killed -- an sbt/JVM tree survived the run holding ports and CPU. Every process
started here goes into its **own process group** (``start_new_session=True``)
and the pidfile records the group id, so ``stop`` can ``killpg`` the entire
tree, not just the process it happened to hold a handle on.

**Improvisation.** An agent that finds sbt missing will start looking for
another way to run a Play app -- a Docker image, for instance, which downloads
gigabytes and hangs the session. ``preflight`` answers that question *before*
anything launches, and a missing toolchain is a clean ``t5-skipped`` finding.
**There is deliberately no Docker path anywhere in this file.**

**Silent hangs.** ``start`` waits for the app to actually answer on its port and
gives up on a budget, so "it did not boot" is a result rather than a session
that never returns.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MIN_JAVA = 17
DEFAULT_WAIT_TIMEOUT = 180
DEFAULT_PORTS = {"play": 9000, "spring": 8080}
TERM_GRACE = 10

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_PREFLIGHT = 3


def iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def pidfile_for(run_dir: Path, app: str) -> Path:
    return run_dir / f"{app}.pid.json"


# --------------------------------------------------------------------------- preflight


def java_version() -> tuple[int | None, str]:
    exe = shutil.which("java")
    if not exe:
        return None, "java not on PATH"
    try:
        proc = subprocess.run(
            ["java", "-version"], capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, f"java -version failed: {e}"
    text = (proc.stderr or "") + (proc.stdout or "")
    for token in text.replace('"', " ").split():
        head = token.split(".")[0]
        if head.isdigit():
            major = int(head)
            # "1.8.0_412" style: the major version is the second component.
            if major == 1:
                parts = token.split(".")
                major = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 1
            return major, text.strip().splitlines()[0] if text.strip() else ""
    return None, "could not parse java -version"


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def preflight(app: str, repo: Path, port: int | None = None) -> dict[str, Any]:
    """
    Everything that must be true before a boot is worth attempting.

    Answered up front so a missing toolchain becomes one honest finding instead
    of an agent inventing a way around it.
    """
    problems: list[str] = []
    checks: dict[str, Any] = {}

    tool = "sbt" if app == "play" else "mvn"
    tool_path = shutil.which(tool)
    checks[tool] = tool_path
    if not tool_path:
        problems.append(
            f"{tool} is not on PATH; T5 cannot boot the {app} application. "
            "Report this as a t5-skipped finding -- do not look for another way "
            "to run it."
        )

    major, raw = java_version()
    checks["java"] = {"major": major, "version": raw}
    if major is None:
        problems.append("java is not on PATH or its version could not be read")
    elif major < MIN_JAVA:
        problems.append(f"java {major} is older than the required {MIN_JAVA}")

    checks["repo"] = str(repo)
    if not repo.is_dir():
        problems.append(f"no {app} repo at {repo}")

    port = port or DEFAULT_PORTS.get(app, 8080)
    checks["port"] = port
    checks["port_free"] = port_free(port)
    if not checks["port_free"]:
        problems.append(
            f"port {port} is already in use -- something is still running from an "
            "earlier attempt. Run 'boot.py stop-all' first."
        )

    return {
        "app": app,
        "checked_at": iso_now(),
        "status": "ready" if not problems else "blocked",
        "checks": checks,
        "problems": problems,
    }


# --------------------------------------------------------------------------- start


def boot_command(app: str, repo: Path, port: int, fallback: bool) -> list[str]:
    """
    The fallback ladder, as data. No Docker rung exists.
    """
    if app == "play":
        if fallback:
            return ["sbt", f"-Dhttp.port={port}", "run"]
        return ["sbt", "run"] if port == DEFAULT_PORTS["play"] else [
            "sbt", f"-Dhttp.port={port}", "run"
        ]
    if fallback:
        # package once, then run the jar: slower to start, but immune to the
        # plugin-resolution failures that make spring-boot:run flaky offline.
        return ["bash", "-lc",
                f"mvn -B -DskipTests package && java -jar target/*.jar "
                f"--server.port={port}"]
    return ["mvn", "-B", "spring-boot:run", f"-Dspring-boot.run.arguments=--server.port={port}"]


def wait_for_http(port: int, path: str, timeout_s: int) -> bool:
    url = f"http://localhost:{port}{path}"
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(url, timeout=5)
            return True
        except urllib.error.HTTPError:
            # Any HTTP status means something is listening and routing.
            return True
        except (urllib.error.URLError, OSError):
            time.sleep(2.0)
    return False


def start(
    app: str, repo: Path, port: int, run_dir: Path, wait_path: str,
    wait_timeout: int, fallback: bool,
) -> dict[str, Any]:
    run_dir.mkdir(parents=True, exist_ok=True)
    log_path = run_dir / f"{app}-boot.log"
    cmd = boot_command(app, repo, port, fallback)

    log = log_path.open("wb")
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(repo), stdout=log, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            # The whole point: sbt and mvn fork, so the thing worth killing is
            # the group, and a group only exists if we make one.
            start_new_session=True,
        )
    except (OSError, FileNotFoundError) as e:
        log.close()
        return {"app": app, "status": "failed", "reason": f"could not start: {e}",
                "log": str(log_path)}

    record = {
        "app": app,
        "pid": proc.pid,
        "pgid": os.getpgid(proc.pid),
        "port": port,
        "cmd": cmd,
        "cwd": str(repo),
        "log": str(log_path),
        "started_at": iso_now(),
    }
    # Written before the wait, so a killed or crashed manager still leaves
    # something stop-all can find. A pidfile that only appears on success is a
    # pidfile that never covers the case you need it for.
    pidfile_for(run_dir, app).write_text(json.dumps(record, indent=2) + "\n",
                                         encoding="utf-8")

    ready = wait_for_http(port, wait_path, wait_timeout)
    record["status"] = "running" if ready else "not_answering"
    if not ready:
        record["reason"] = (
            f"no HTTP response on port {port}{wait_path} within {wait_timeout}s; "
            f"see {log_path}. The application did not boot -- that is the finding."
        )
    pidfile_for(run_dir, app).write_text(json.dumps(record, indent=2) + "\n",
                                         encoding="utf-8")
    return record


# --------------------------------------------------------------------------- stop


_PROC = Path("/proc")


def _is_zombie(pid: int) -> bool:
    """
    A killed process stays visible until its parent reaps it.

    That matters here because ``killpg(pgid, 0)`` succeeds for a group whose
    every member is a zombie -- so a teardown that worked perfectly would report
    ``still_running``. Zombies hold no port and no CPU; they are stopped.
    """
    try:
        status_text = (_PROC / str(pid) / "stat").read_text(encoding="utf-8")
    except (OSError, ValueError):
        return False
    # "pid (comm) STATE ..." -- comm can contain spaces and parens, so split on
    # the last ')' rather than on whitespace.
    _, _, rest = status_text.rpartition(")")
    fields = rest.split()
    return bool(fields) and fields[0] == "Z"


def _group_pids(pgid: int) -> list[int] | None:
    """Live pids in a process group, or None where /proc is unavailable."""
    if not _PROC.is_dir():
        return None
    pids = []
    for entry in _PROC.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            if os.getpgid(pid) == pgid and not _is_zombie(pid):
                pids.append(pid)
        except (ProcessLookupError, PermissionError, OSError):
            continue
    return pids


def alive(pgid: int) -> bool:
    """True only if the group still has a process that can do something."""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    pids = _group_pids(pgid)
    return True if pids is None else bool(pids)


def reap(pid: int | None) -> None:
    """Clear our own zombie, if that pid happens to be our child."""
    if pid is None:
        return
    try:
        os.waitpid(pid, os.WNOHANG)
    except (ChildProcessError, OSError):
        pass


def stop_group(pgid: int, grace: int = TERM_GRACE, pid: int | None = None) -> str:
    """SIGTERM the group, wait, then SIGKILL whatever is left."""
    if not alive(pgid):
        return "already_stopped"
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return "already_stopped"

    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        reap(pid)
        if not alive(pgid):
            return "terminated"
        time.sleep(0.25)

    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        return "terminated"
    time.sleep(0.5)
    reap(pid)
    return "killed" if not alive(pgid) else "still_running"


def stop(run_dir: Path, app: str) -> dict[str, Any]:
    path = pidfile_for(run_dir, app)
    if not path.is_file():
        return {"app": app, "status": "no_pidfile", "pidfile": str(path)}
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return {"app": app, "status": "unreadable_pidfile", "reason": str(e)}

    pgid = record.get("pgid")
    if not isinstance(pgid, int):
        return {"app": app, "status": "no_pgid", "pidfile": str(path)}

    outcome = stop_group(pgid, pid=record.get("pid"))
    record["status"] = "stopped" if outcome != "still_running" else "still_running"
    record["stop_outcome"] = outcome
    record["stopped_at"] = iso_now()
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return {"app": app, "pgid": pgid, "status": record["status"], "outcome": outcome}


def stop_all(run_dir: Path) -> dict[str, Any]:
    if not run_dir.is_dir():
        return {"status": "nothing_to_stop", "run_dir": str(run_dir), "stopped": []}
    results = [
        stop(run_dir, f.name.removesuffix(".pid.json"))
        for f in sorted(run_dir.glob("*.pid.json"))
    ]
    still = [r for r in results if r.get("status") == "still_running"]
    return {
        "status": "clean" if not still else "processes_survived",
        "run_dir": str(run_dir),
        "stopped": results,
    }


def status(run_dir: Path) -> dict[str, Any]:
    entries = []
    if run_dir.is_dir():
        for f in sorted(run_dir.glob("*.pid.json")):
            try:
                record = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            pgid = record.get("pgid")
            if isinstance(pgid, int) and alive(pgid):
                entries.append({"app": record.get("app"), "pgid": pgid,
                                "port": record.get("port"), "state": "alive"})
    return {"run_dir": str(run_dir), "running": entries, "count": len(entries)}


# --------------------------------------------------------------------------- CLI


def emit(payload: dict[str, Any], code: int) -> int:
    json.dump(payload, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return code


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Boot and stop the applications T5 compares (JSON to stdout).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  boot.py preflight --app play --repo ../play-app\n"
            "  boot.py start --app play --repo ../play-app --port 9000 \\\n"
            "      --run-dir ../spring-app/.migration/run --wait-path /\n"
            "  boot.py start --app spring --repo ../spring-app --port 8080 \\\n"
            "      --run-dir ../spring-app/.migration/run --fallback\n"
            "  boot.py status   --run-dir ../spring-app/.migration/run\n"
            "  boot.py stop-all --run-dir ../spring-app/.migration/run\n"
            "\n"
            "Every process is started in its own process group and stopped with\n"
            "killpg, so nothing survives the run. There is no Docker path here:\n"
            "a missing toolchain is a t5-skipped finding, not an adventure.\n"
        ),
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_pre = sub.add_parser("preflight", help="Check the toolchain before launching.")
    p_pre.add_argument("--app", choices=("play", "spring"), required=True)
    p_pre.add_argument("--repo", type=Path, required=True)
    p_pre.add_argument("--port", type=int, default=None)

    p_start = sub.add_parser("start", help="Launch an app and wait for it to answer.")
    p_start.add_argument("--app", choices=("play", "spring"), required=True)
    p_start.add_argument("--repo", type=Path, required=True)
    p_start.add_argument("--port", type=int, default=None)
    p_start.add_argument("--run-dir", type=Path, required=True)
    p_start.add_argument("--wait-path", default="/")
    p_start.add_argument("--wait-timeout", type=int, default=DEFAULT_WAIT_TIMEOUT)
    p_start.add_argument(
        "--fallback", action="store_true",
        help="Second rung of the ladder: sbt -Dhttp.port=N run, or "
             "mvn package + java -jar.",
    )
    p_start.add_argument(
        "--skip-preflight", action="store_true",
        help="Launch without checking the toolchain first. Rarely what you want.",
    )

    p_stop = sub.add_parser("stop", help="Stop one app by its pidfile.")
    p_stop.add_argument("--run-dir", type=Path, required=True)
    p_stop.add_argument("--app", choices=("play", "spring"), required=True)

    p_all = sub.add_parser("stop-all", help="Stop everything this run started.")
    p_all.add_argument("--run-dir", type=Path, required=True)

    p_status = sub.add_parser("status", help="What is still running.")
    p_status.add_argument("--run-dir", type=Path, required=True)

    args = parser.parse_args()

    if args.cmd == "preflight":
        result = preflight(args.app, args.repo.expanduser().resolve(), args.port)
        return emit(result, EXIT_OK if result["status"] == "ready" else EXIT_PREFLIGHT)

    if args.cmd == "start":
        repo = args.repo.expanduser().resolve()
        port = args.port or DEFAULT_PORTS[args.app]
        if not args.skip_preflight:
            pre = preflight(args.app, repo, port)
            if pre["status"] != "ready":
                return emit(
                    {"app": args.app, "status": "preflight_blocked", **pre},
                    EXIT_PREFLIGHT,
                )
        record = start(
            args.app, repo, port, args.run_dir.expanduser().resolve(),
            args.wait_path, args.wait_timeout, args.fallback,
        )
        return emit(record, EXIT_OK if record.get("status") == "running" else EXIT_FAILED)

    if args.cmd == "stop":
        result = stop(args.run_dir.expanduser().resolve(), args.app)
        return emit(result, EXIT_OK if result.get("status") != "still_running" else EXIT_FAILED)

    if args.cmd == "stop-all":
        result = stop_all(args.run_dir.expanduser().resolve())
        return emit(result, EXIT_OK if result["status"] != "processes_survived" else EXIT_FAILED)

    result = status(args.run_dir.expanduser().resolve())
    return emit(result, EXIT_OK)


if __name__ == "__main__":
    sys.exit(main())
