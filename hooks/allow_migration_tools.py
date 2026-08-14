#!/usr/bin/env python3
"""
PreToolUse hook: auto-allow this plugin's own commands, deny writes to the Play repo.

Claude Code invokes this with the tool call as JSON on stdin and reads a JSON
decision from stdout. Anything it does not recognise gets no decision at all,
which leaves the user's normal permission prompt in place -- the hook can only
*remove* prompts for shapes it can prove are this plugin's, or *add* a denial.

Why it exists: a migration is a few hundred invocations of half a dozen
commands, and approving them one at a time is both tedious and the reason
people paste over-broad rules like ``Bash(mvn *)`` into settings. Every rule
here is instead scoped to a resolved path.

Why it can also deny: the Play repo is read-only for the whole run, enforced
after the fact by ``guard.py``. A hook that runs *before* the command is the one
place that can stop the write instead of reporting it afterwards, so any command
that would write into the Play repo is denied here. That makes this file a
second enforcement of the same invariant, not a hole in it.

    P2SB_PLAY_REPO    Play repo path      (denied for writes)
    P2SB_SPRING_REPO  Spring repo path    (mvn/git allowed inside it)
    P2SB_CMD_WRAPPER  one wrapper token to strip before matching (default: none)

The wrapper knob exists because some users route commands through a prefix tool
(``rtk git status`` rather than ``git status``). Exactly one leading token is
stripped, and only the one named -- this is not a general "ignore the first
word" rule.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path

ALLOW = "allow"
DENY = "deny"

# git subcommands that cannot modify a repository. Everything else run against
# the Play repo is a write until proven otherwise.
GIT_READ_ONLY = {
    "status", "log", "show", "diff", "rev-parse", "ls-files", "ls-tree",
    "branch", "describe", "cat-file", "blame", "shortlog", "config",
}

MVN_GOALS = {"compile", "test", "package", "spring-boot:run", "clean", "-v", "--version"}


def resolved(path: str | None) -> Path | None:
    if not path:
        return None
    try:
        return Path(path).expanduser().resolve()
    except (OSError, RuntimeError):
        return None


def inside(child: Path | None, parent: Path | None) -> bool:
    """
    Containment on *resolved* paths, so ``<plugin>/scripts/../../etc/passwd``
    cannot pass as a plugin script.
    """
    if child is None or parent is None:
        return False
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def strip_wrapper(tokens: list[str]) -> list[str]:
    wrapper = os.environ.get("P2SB_CMD_WRAPPER", "").strip()
    if wrapper and tokens and tokens[0] == wrapper:
        return tokens[1:]
    return tokens


def touches_play_repo_for_write(tokens: list[str], play_repo: Path | None) -> bool:
    """
    Would this command write inside the read-only Play tree?

    Conservative on purpose: a path argument under the Play repo combined with
    anything that is not a known read-only reader counts as a write. Being
    wrong here costs one permission prompt; being wrong the other way costs the
    invariant the whole tool rests on.
    """
    if play_repo is None or not tokens:
        return False

    program = Path(tokens[0]).name

    if program == "git":
        # git -C <play> <verb>
        for i, token in enumerate(tokens):
            if token == "-C" and i + 1 < len(tokens):
                if inside(resolved(tokens[i + 1]), play_repo):
                    verbs = [
                        t for t in tokens[i + 2:]
                        if not t.startswith("-")
                    ]
                    return not (verbs and verbs[0] in GIT_READ_ONLY)
        return False

    writers = {
        "rm", "mv", "cp", "tee", "truncate", "chmod", "chown", "ln", "mkdir",
        "rmdir", "touch", "sed", "dd", "install", "shred",
    }
    if program in writers:
        return any(inside(resolved(t), play_repo) for t in tokens[1:] if not t.startswith("-"))

    # Redirection into the Play tree, e.g. `echo x > <play>/conf/routes`.
    for i, token in enumerate(tokens):
        if token in (">", ">>") and i + 1 < len(tokens):
            if inside(resolved(tokens[i + 1]), play_repo):
                return True
    return False


def decide(command: str) -> tuple[str, str] | None:
    """Returns (decision, reason), or None to leave the normal prompt alone."""
    try:
        tokens = strip_wrapper(shlex.split(command))
    except ValueError:
        return None  # unparseable quoting: not ours to judge
    if not tokens:
        return None

    plugin_root = resolved(os.environ.get("CLAUDE_PLUGIN_ROOT"))
    plugin_data = resolved(os.environ.get("CLAUDE_PLUGIN_DATA"))
    play_repo = resolved(os.environ.get("P2SB_PLAY_REPO"))
    spring_repo = resolved(os.environ.get("P2SB_SPRING_REPO"))

    if touches_play_repo_for_write(tokens, play_repo):
        return DENY, (
            "The Play repo is read-only for the whole migration. Work in the "
            "Spring repo instead; if you truly cannot proceed without changing "
            "Play source, stop and report that -- it is a halt condition, not "
            "something to work around."
        )

    program = Path(tokens[0]).name

    # python3 <plugin>/scripts/**
    if program in ("python3", "python") and len(tokens) > 1:
        script = resolved(tokens[1])
        if inside(script, plugin_root / "scripts" if plugin_root else None):
            return ALLOW, "this plugin's own tool"
        return None

    # java -jar <under the plugin's data dir>
    if program == "java" and "-jar" in tokens:
        jar_index = tokens.index("-jar")
        if jar_index + 1 < len(tokens):
            jar = resolved(tokens[jar_index + 1])
            if inside(jar, plugin_data):
                return ALLOW, "checksum-verified dev-toolkit jar"
        return None

    # mvn, only inside the Spring repo, only build goals
    if program == "mvn":
        goals = [t for t in tokens[1:] if not t.startswith("-")]
        if goals and all(g in MVN_GOALS for g in goals):
            cwd = resolved(os.getcwd())
            if inside(cwd, spring_repo):
                return ALLOW, "build goal inside the Spring repo"
        return None

    # git against the Spring repo: it is ours to commit to.
    if program == "git":
        for i, token in enumerate(tokens):
            if token == "-C" and i + 1 < len(tokens):
                if inside(resolved(tokens[i + 1]), spring_repo):
                    return ALLOW, "git against the Spring repo"
                if inside(resolved(tokens[i + 1]), play_repo):
                    verbs = [t for t in tokens[i + 2:] if not t.startswith("-")]
                    if verbs and verbs[0] in GIT_READ_ONLY:
                        return ALLOW, "read-only git against the Play repo"
        return None

    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        return 0

    if payload.get("tool_name") != "Bash":
        return 0
    command = (payload.get("tool_input") or {}).get("command", "")
    if not command:
        return 0

    verdict = decide(command)
    if verdict is None:
        return 0

    decision, reason = verdict
    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": decision,
                "permissionDecisionReason": reason,
            }
        },
        sys.stdout,
    )
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
