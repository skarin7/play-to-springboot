# Scripts

Deterministic tooling. Sequencing and failure handling live in the
`play-spring-manager` skill, not here.

The split is deliberate: agents count files inconsistently and re-derive regexes
badly, while scripts are exact and cost nothing to run. Judgment goes to the
agent; anything countable goes here.

## `migration_orchestrator.py`

Workspace setup and status reporting. Despite the name, it no longer orchestrates.

```bash
python3 scripts/migration_orchestrator.py setup  --play-repo ../my-play-app
python3 scripts/migration_orchestrator.py status --play-repo ../my-play-app
python3 scripts/migration_orchestrator.py verify --play-repo ../my-play-app
```

Optional: `--workspace`, `--spring-repo`, `--spring-name`, and `--skip-routes`
on `verify`.

Kit paths resolve from the script's own location; `--play-repo` resolves against
your shell's cwd.

Exit codes: `0` OK, `1` error, `3` initialize not done.

### What was removed

This file used to be 954 lines and ran the migration itself: a four-level Cursor
model precedence chain, LLM call budgets, per-layer retry counters, a loop
detector, and `cursor-agent` invocations with the API key passed on the command
line (visible to any local `ps`).

All of it is gone. The coding agent makes those decisions now. The environment
variables that configured it — `CURSOR_API_KEY`, `CURSOR_MODEL`,
`MAX_TOTAL_LLM_CALLS`, `MAX_RETRIES_PER_LAYER`, and the rest — no longer do
anything.

## `setup.sh`

Called by `migration_orchestrator.py setup`; usable directly.

```bash
./scripts/setup.sh <path-to-play-repo> [--workspace <dir>] [--spring-name <name>]
```

Idempotent, and non-destructive: it copies over the kit reference rather than
deleting it, so notes you add under `<play-repo>/.cursor/docs/` and custom agents
under `.claude/agents/` survive re-runs.

## `tools/` — helpers the agents call

Each prints JSON to stdout and does one thing.

| Tool | Purpose |
|---|---|
| `gate.py` | **T1–T4 in one call**; one verdict, findings, and `needs_agent` |
| `endpoint_diff.py` | **T5** endpoint response parity: probes, capture, diff |
| `layers.py` | Layer classification; also detects a stale dev-toolkit JAR |
| `inventory.py` | Per-layer counts for both trees; picks `collapsed`/`full` role mode |
| `parse_mvn.py` | Maven log → structured errors, grouped by file, with signatures |
| `signature_diff.py` | **T2** structural preservation |
| `routes.py` | Play routes and Spring mappings; path normalization |
| `verify.py` | Completeness plus **T3** route parity |
| `state.py` | Atomic single-writer access to `migration-status.json` |
| `token_report.py` | Measured token/cost accounting from Claude Code transcripts |

```bash
python3 scripts/tools/test_tools.py     # 87 tests, stdlib only
```

### `gate.py`

The manager runs this after every dev dispatch, instead of dispatching a QA
agent. All four tiers are deterministic, so an agent added a round trip per layer
and returned the same findings the scripts produced.

```bash
gate.py --play-repo P --spring-repo S --layer service     # T1 + T2 (layer-scoped)
gate.py --play-repo P --spring-repo S --layer controller  # adds T3
gate.py --play-repo P --spring-repo S --final             # full-tree T2, plus T3 and T4
gate.py --play-repo P --spring-repo S --layer init --tiers T1   # empty-project dependency check
```

Raw Maven output goes to `<spring-repo>/.migration/logs/`; stdout carries only
the parsed verdict, which is what lets the manager own the check without
ingesting build output.

Play-side signatures are extracted once and cached under `.migration/cache/` —
the Play tree is read-only for the whole run, so re-extracting it every layer was
pure cost. `--refresh-cache` forces a re-read.

`needs_agent` is the only reason left to dispatch QA: an error landing in a layer
already `done`, a build failure the parser could not classify, a file that would
not parse, or a tier that could not run.

### `endpoint_diff.py`

```bash
endpoint_diff.py probes  --routes <play>/conf/routes --out .migration/endpoint-probes.json
endpoint_diff.py capture --base-url http://localhost:9000 --probes P --out before.json
endpoint_diff.py capture --base-url http://localhost:8080 --probes P --out after.json
endpoint_diff.py diff    --before before.json --after after.json
```

Parameterless GET routes are enabled on seeding; parameterised paths need
`path_params` filled in, and mutating verbs need a body plus identical starting
state in both apps, so they stay disabled until someone supplies both.

Values under volatile keys (`createdAt`, `userId`, `took_ms`, …) are compared for
presence and type, not equality — two runs of the *same* app differ there, and a
tier that always complains is a tier nobody reads. Keys are matched by token, so
`identifier` and `valid` are not treated as volatile.

### `state.py`

**Only the manager runs this.** Subagents report through artifacts and journals;
two writers corrupt the file, and a subagent killed mid-write leaves JSON that
cannot be resumed from.

```bash
state.py --status-file S init
state.py --status-file S show [--path layers.model]
state.py --status-file S set --path layers.model.status --value done
state.py --status-file S add-finding --json '{"layer":"service","file":"X.java","tier":"T2","severity":"blocker",...}'
state.py --status-file S fold-journal --journal .migration/journal/service-dev.ndjson --layer service
state.py --status-file S gate --name architecture --value approved
```

Reads merge defaults, so a status file from an earlier version of the kit keeps
every field it had and gains the new ones.

### `parse_mvn.py` signatures

The `signatures` array is an order-independent identity for a set of compile
errors. Comparing it across attempts distinguishes a genuine stuck loop
(identical set) from progress that exposed deeper errors (different set) — a
distinction the old `is_looping` heuristic could not make, which is why it killed
layers that were still advancing.

### `signature_diff.py` thresholds

Defaults: a method must lose more than 60% of its statements **and** end up with
fewer than 3 before it is reported. Override with `--drop-ratio` and
`--min-statements`, or per project in `decisions.md`.

`--layer-only` restricts the comparison to Play classes belonging to `--layer`,
which is what the per-layer gate uses: without it, every layer re-reports
findings against classes three layers old. Only the Play side is scoped —
migration relocates classes, so filtering the Spring side would report a moved
class as missing.

Loosening them to silence findings defeats the check — the point is that it is
quiet enough to be believed when it does fire.
