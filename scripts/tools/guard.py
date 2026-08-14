#!/usr/bin/env python3
"""
The Play-repo read-only guard: mechanical, and loud when it cannot run.

    python3 scripts/tools/guard.py baseline --play-repo P --spring-repo S
    python3 scripts/tools/guard.py check    --play-repo P --spring-repo S
    python3 scripts/tools/guard.py show     --spring-repo S

The Play tree is read-only for the whole migration. That invariant used to be
prose in the skill -- "non-empty ``git status --porcelain`` means tampered" --
which silently did nothing for any Play repo not under git: git exits 128 with
**empty stdout**, and empty read as clean. A guard whose failure mode is a pass
is not a guard.

Two rules follow from that, and both are structural here rather than advisory:

1. ``evaluate()`` returns an explicit enum -- ``clean`` | ``tampered`` |
   ``error``. There is no code path where an empty or missing result becomes
   ``clean``. No baseline is an ``error``, not a pass.
2. The caller distinguishes them by exit code: 0 clean, 2 tampered, 3 cannot
   run. ``error`` halts the run exactly like ``tampered`` does.

**The Play repo is never git-initialised by this tool.** Writing ``.git/`` into
a tree declared read-only contradicts the thing being enforced, and it breaks
the nesting case: a Play repo inside a larger checkout would become a nested
repo, where without init ``git -C <play> status`` reports the *parent's*
changes instead. So git mode is used only when the Play repo is a repository
root in its own right; everything else falls back to a checksum manifest, which
is strictly more faithful anyway -- it also catches modification of gitignored
files, which ``git status`` cannot see.

Artifacts live on the Spring side (``<spring>/.migration/guard/``), because the
Play side is what we are promising not to write to.
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

EXIT_CLEAN = 0
EXIT_TAMPERED = 2
EXIT_ERROR = 3

# Scope "java": the parts of a Play repo a migration reads and must not write.
# Kept narrow so the hash pass stays fast on large repos -- see --scope all.
JAVA_SCOPE = ("app", "conf", "public", "project", "build.sbt")

# Directories that are build output or tooling state, not source. They change
# on their own (sbt writes target/ just by being run) and hashing them would
# make the guard cry wolf.
_SKIP_DIRS = {".git", "target", "project/target", "node_modules", ".idea", ".bloop",
              ".metals", "logs", ".cache"}


def iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def guard_dir(spring_repo: Path) -> Path:
    return spring_repo / ".migration" / "guard"


def baseline_path(spring_repo: Path) -> Path:
    return guard_dir(spring_repo) / "baseline.json"


def last_check_path(spring_repo: Path) -> Path:
    return guard_dir(spring_repo) / "last-check.json"


# --------------------------------------------------------------------------- mode


def detect_mode(play_repo: Path) -> tuple[str, str]:
    """
    Returns (mode, why).

    ``git`` only when the Play repo is itself the repository root. A Play repo
    nested inside a larger checkout resolves to the *parent's* toplevel, where
    ``git status`` reports the parent's unrelated changes -- so that case takes
    the manifest path, not a guard that fires on someone else's edits.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(play_repo), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return "manifest", f"git unavailable ({e.__class__.__name__})"
    if proc.returncode != 0:
        return "manifest", "not a git repository"
    toplevel = Path(proc.stdout.strip())
    try:
        same = toplevel.resolve() == play_repo.resolve()
    except OSError:
        same = False
    if not same:
        return "manifest", f"nested inside another git repo at {toplevel}"
    return "git", "Play repo is its own git root"


# --------------------------------------------------------------------------- manifest


def _skip(rel: str) -> bool:
    parts = rel.split("/")
    return any(p in _SKIP_DIRS for p in parts) or "/".join(parts[:2]) in _SKIP_DIRS


def iter_scope_files(play_repo: Path, scope: str):
    roots = [play_repo] if scope == "all" else [play_repo / p for p in JAVA_SCOPE]
    seen: set[Path] = set()
    for root in roots:
        if root.is_file():
            if root not in seen:
                seen.add(root)
                yield root
            continue
        if not root.is_dir():
            continue
        for f in sorted(root.rglob("*")):
            if not f.is_file() or f.is_symlink() or f in seen:
                continue
            rel = str(f.relative_to(play_repo)).replace("\\", "/")
            if _skip(rel):
                continue
            seen.add(f)
            yield f


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def build_manifest(play_repo: Path, scope: str = "java") -> dict[str, dict[str, Any]]:
    manifest: dict[str, dict[str, Any]] = {}
    for f in iter_scope_files(play_repo, scope):
        rel = str(f.relative_to(play_repo)).replace("\\", "/")
        stat = f.stat()
        manifest[rel] = {
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "sha256": sha256_of(f),
        }
    return manifest


def compare_manifest(
    play_repo: Path, recorded: dict[str, dict[str, Any]], scope: str
) -> dict[str, list[str]]:
    """
    Size+mtime fast path: sha256 is recomputed only where those disagree, so a
    re-check on an untouched large repo costs a stat per file rather than a
    read. A touched-but-identical file therefore reports clean, which is the
    behaviour we want -- the invariant is about content, not about timestamps.
    """
    current = {
        str(f.relative_to(play_repo)).replace("\\", "/"): f
        for f in iter_scope_files(play_repo, scope)
    }
    modified: list[str] = []
    deleted: list[str] = []
    added: list[str] = []

    for rel, entry in recorded.items():
        f = current.pop(rel, None)
        if f is None:
            deleted.append(rel)
            continue
        stat = f.stat()
        if stat.st_size == entry.get("size") and stat.st_mtime_ns == entry.get("mtime_ns"):
            continue
        if sha256_of(f) != entry.get("sha256"):
            modified.append(rel)

    added.extend(sorted(current))
    return {"modified": sorted(modified), "deleted": sorted(deleted), "added": added}


# --------------------------------------------------------------------------- git


def git_changes(play_repo: Path) -> tuple[list[str] | None, str]:
    """Returns (changed paths, error). Exactly one of the two is meaningful."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(play_repo), "status", "--porcelain"],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, f"git status could not run: {e}"
    if proc.returncode != 0:
        # This is the original bug in one line: exit != 0 carries empty stdout,
        # and the old check read that as clean.
        return None, (proc.stderr or "git status failed").strip()[:300]
    return [line[3:] for line in proc.stdout.splitlines() if line.strip()], ""


# --------------------------------------------------------------------------- evaluate


def evaluate(play_repo: Path, spring_repo: Path) -> dict[str, Any]:
    """
    The whole guard, as one explicit verdict. Never returns an implicit pass.
    """
    result: dict[str, Any] = {
        "checked_at": iso_now(),
        "play_repo": str(play_repo),
        "status": "error",
        "mode": None,
        "reason": None,
        "changes": {},
    }

    bpath = baseline_path(spring_repo)
    if not bpath.is_file():
        result["reason"] = (
            f"no guard baseline at {bpath}; run 'guard.py baseline' first. "
            "A guard with nothing to compare against is not a passing guard."
        )
        return result

    try:
        baseline = json.loads(bpath.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        result["reason"] = f"guard baseline at {bpath} is unreadable: {e}"
        return result

    mode = baseline.get("mode")
    result["mode"] = mode
    result["baseline_at"] = baseline.get("captured_at")

    if not play_repo.is_dir():
        result["reason"] = f"Play repo not found at {play_repo}"
        return result

    if mode == "git":
        head = baseline.get("head")
        changed, err = git_changes(play_repo)
        if changed is None:
            result["reason"] = err
            return result
        current_head = git_head(play_repo)
        if head and current_head and head != current_head:
            result["status"] = "tampered"
            result["reason"] = f"HEAD moved: {head[:12]} -> {current_head[:12]}"
            result["changes"] = {"head_before": head, "head_after": current_head}
            return result
        if changed:
            result["status"] = "tampered"
            result["reason"] = f"{len(changed)} path(s) changed in the Play repo"
            result["changes"] = {"modified": sorted(changed)}
            return result
        result["status"] = "clean"
        return result

    if mode == "manifest":
        recorded = baseline.get("files")
        if not isinstance(recorded, dict):
            result["reason"] = "guard baseline has no file manifest"
            return result
        scope = baseline.get("scope", "java")
        try:
            changes = compare_manifest(play_repo, recorded, scope)
        except OSError as e:
            result["reason"] = f"could not read the Play tree: {e}"
            return result
        total = sum(len(v) for v in changes.values())
        if total:
            result["status"] = "tampered"
            result["reason"] = (
                f"{len(changes['modified'])} modified, {len(changes['deleted'])} "
                f"deleted, {len(changes['added'])} added under the guarded scope"
            )
            result["changes"] = changes
            return result
        result["status"] = "clean"
        result["files_checked"] = len(recorded)
        return result

    result["reason"] = f"guard baseline has an unknown mode: {mode!r}"
    return result


def git_head(play_repo: Path) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(play_repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def exit_code_for(status: str) -> int:
    return {"clean": EXIT_CLEAN, "tampered": EXIT_TAMPERED}.get(status, EXIT_ERROR)


# --------------------------------------------------------------------------- CLI


def cmd_baseline(args) -> int:
    play_repo = args.play_repo.expanduser().resolve()
    spring_repo = args.spring_repo.expanduser().resolve()
    if not play_repo.is_dir():
        print(f"ERROR: no Play repo at {play_repo}", file=sys.stderr)
        return EXIT_ERROR

    mode, why = detect_mode(play_repo)
    baseline: dict[str, Any] = {
        "captured_at": iso_now(),
        "play_repo": str(play_repo),
        "mode": mode,
        "mode_reason": why,
        "scope": args.scope,
    }
    if mode == "git":
        baseline["head"] = git_head(play_repo)
        changed, err = git_changes(play_repo)
        if changed is None:
            print(f"ERROR: {err}", file=sys.stderr)
            return EXIT_ERROR
        # A dirty starting tree is recorded, not rejected: the guard's question
        # is "did *we* change it", so the baseline is whatever we were handed.
        baseline["dirty_at_baseline"] = sorted(changed)
    else:
        try:
            baseline["files"] = build_manifest(play_repo, args.scope)
        except OSError as e:
            print(f"ERROR: could not read the Play tree: {e}", file=sys.stderr)
            return EXIT_ERROR

    path = baseline_path(spring_repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(baseline, indent=2) + "\n", encoding="utf-8")

    summary = {
        "status": "recorded",
        "mode": mode,
        "mode_reason": why,
        "scope": args.scope,
        "files": len(baseline.get("files") or {}),
        "baseline": str(path),
    }
    json.dump(summary, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return EXIT_CLEAN


def cmd_check(args) -> int:
    play_repo = args.play_repo.expanduser().resolve()
    spring_repo = args.spring_repo.expanduser().resolve()
    result = evaluate(play_repo, spring_repo)

    path = last_check_path(spring_repo)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass  # Reporting the verdict matters more than journaling it.

    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return exit_code_for(result["status"])


def cmd_show(args) -> int:
    spring_repo = args.spring_repo.expanduser().resolve()
    out: dict[str, Any] = {}
    for name, path in (("baseline", baseline_path(spring_repo)),
                       ("last_check", last_check_path(spring_repo))):
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as e:
                out[name] = {"error": str(e)}
                continue
            data.pop("files", None)  # the manifest itself is noise here
            out[name] = data
        else:
            out[name] = None
    json.dump(out, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return EXIT_CLEAN if out.get("baseline") else EXIT_ERROR


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Play-repo read-only guard (JSON to stdout).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  guard.py baseline --play-repo ../play-app --spring-repo ../spring-play-app\n"
            "  guard.py baseline --play-repo ../play-app --spring-repo ../spring-play-app --scope all\n"
            "  guard.py check    --play-repo ../play-app --spring-repo ../spring-play-app\n"
            "  guard.py show     --spring-repo ../spring-play-app\n"
            "\n"
            "Exit codes: 0 clean, 2 tampered, 3 cannot run.\n"
            "'error' is not a pass -- it halts the run exactly like 'tampered'.\n"
        ),
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_base = sub.add_parser("baseline", help="Record the pre-migration state.")
    p_base.add_argument("--play-repo", type=Path, required=True)
    p_base.add_argument("--spring-repo", type=Path, required=True)
    p_base.add_argument(
        "--scope", choices=("java", "all"), default="java",
        help="java (default): app/ conf/ public/ project/ build.sbt. all: the whole repo.",
    )

    p_check = sub.add_parser("check", help="Compare the Play repo against the baseline.")
    p_check.add_argument("--play-repo", type=Path, required=True)
    p_check.add_argument("--spring-repo", type=Path, required=True)

    p_show = sub.add_parser("show", help="Print the recorded baseline and last check.")
    p_show.add_argument("--spring-repo", type=Path, required=True)

    args = parser.parse_args()
    return {"baseline": cmd_baseline, "check": cmd_check, "show": cmd_show}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
