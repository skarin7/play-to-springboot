# Permissions

A migration is a few hundred invocations of about six commands. Approving them
one at a time is tedious enough that the usual response is to paste something
like `Bash(mvn *)` into settings — which grants far more than the run needs, for
far longer than the run lasts.

This plugin handles it in three layers. Each one is scoped to a resolved path,
and each one reinforces the read-only Play invariant rather than eroding it.

## Layer 1 — the plugin's PreToolUse hook (automatic)

`hooks/allow_migration_tools.py`, registered from `plugin.json`, runs before
every `Bash` call and returns one of three things:

| Decision | When |
|---|---|
| `allow` | The command is provably this plugin's: `python3 <plugin>/scripts/**`, `java -jar <under $CLAUDE_PLUGIN_DATA>`, `mvn -B <build goal>` with the cwd inside the Spring repo, `git -C <spring>`, read-only `git -C <play>` |
| `deny` | The command would **write into the Play repo** |
| *(nothing)* | Everything else — your normal permission prompt appears, unchanged |

Paths are compared **after resolution**, so `<plugin>/scripts/../../../etc/x`
does not pass as a plugin script.

The `deny` half is the point worth keeping. `guard.py` detects a Play-repo write
*after* it happened; the hook is the only place that can stop it before. Losing
the hook costs convenience, losing the deny costs the invariant.

### Environment

The hook reads four variables, all optional — an unset one simply narrows what
it can decide:

| Variable | Meaning |
|---|---|
| `CLAUDE_PLUGIN_ROOT` | Set by Claude Code; where the plugin's `scripts/` live |
| `CLAUDE_PLUGIN_DATA` | Set by Claude Code; where `fetch_jar.py` caches the toolkit jar |
| `P2SB_PLAY_REPO` | Play repo, denied for writes |
| `P2SB_SPRING_REPO` | Spring repo, where `mvn` and `git` are allowed |
| `P2SB_CMD_WRAPPER` | One leading token to strip before matching (e.g. `rtk`), default empty |

`P2SB_CMD_WRAPPER` exists for setups that route commands through a prefix tool —
`rtk git status` rather than `git status`. Exactly one token is stripped, and
only the one you name.

## Layer 2 — a generated allow list

```bash
python3 scripts/migration_orchestrator.py setup --play-repo <play>   # once
bash scripts/setup.sh <play> --print-permissions
```

That prints a paste-ready `permissions` block with **this workspace's** resolved
absolute paths, including `deny` entries for the Play repo. Generate it per
workspace; do not copy one from a previous run. A stale rule matches nothing and
silently prompts for everything, which reads as "the plugin asks for permission
at random".

## Layer 3 — approve at the prompt

Nothing here is required. Without either layer above, the run still works; you
approve commands as they come. The layers exist to remove prompts for commands
whose shape is already known, not to unlock anything the run could not otherwise
do.

## What is deliberately not granted

- **No blanket `Bash(mvn *)`.** Only `-B compile|test|package|spring-boot:run`,
  and only with the cwd inside the Spring repo.
- **No write access to the Play repo, at any layer.** It is denied in the hook
  and in the generated `deny` list, and `guard.py` checks it again at every gate.
- **No `java -jar` for arbitrary jars.** Only the checksum-verified toolkit jar
  under `$CLAUDE_PLUGIN_DATA`.
- **No Docker.** Nothing in this plugin pulls or runs an image; `boot.py` has no
  Docker path at all.
