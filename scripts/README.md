# Migration orchestrator (`migration_orchestrator.py`)

Python **stdlib-only** driver for the Play → Spring pipeline: `dev-toolkit` **`migrate-app`** per layer, **`mvn compile`**, optional **`cursor-agent`** for compile fixes, **`source_inventory`** / **`migration_verification`** counts.

**State:** a single **`migration-status.json`** (no `pipeline_state.json`). Default path is **`<spring-repo>/migration-status.json`**, where **`spring-repo`** is resolved after **`setup.sh`** runs (see below). Override with `--status-file` or `MIGRATION_STATUS_FILE`.

**First step (automatic):** the script always runs **`play-to-spring-kit/setup.sh`** for **`--play-repo`** (idempotent: skills, JAR, **`workspace.yaml`**, Spring dirs). You do not run **`setup.sh`** manually first unless you prefer to.

**Where to run from:** use the kit clone as your shell cwd, e.g. **`cd …/play-to-spring-kit`**, then **`python3 scripts/migration_orchestrator.py --play-repo ../your-play-app`**. The kit root and **`setup.sh`** are found via the script file path (**`__file__`**), not cwd. Relative **`--play-repo`** / **`--workspace`** values are resolved against **cwd**, so paths like **`../cms-content-service`** are correct when you launch from the kit directory.

## Requirements

- Python **3.10+**
- **`java`**, **`mvn`** on `PATH`
- **`dev-toolkit-1.0.0.jar`** at `<play-repo>/` (or pass `--jar`)
- **`cursor-agent`** on `PATH` if using LLM fixes (see Cursor docs for install)

## Cursor / model

1. Create a **Cursor User API Key** (dashboard → Integrations → User API Keys, `sk_...`).
2. `export CURSOR_API_KEY="sk_..."`

**Model** (`cursor-agent -m`):

| Precedence | Source |
|------------|--------|
| 1 (highest) | `--cursor-model` |
| 2 | `CURSOR_MODEL` or `MIGRATION_CURSOR_MODEL` |
| 3 | `migration-status.json` → `autonomous.cursor_model` |
| 4 (default) | **`composer-2`** |

Confirm slugs with `cursor-agent --help` for your CLI version.

**Headless agent** (what the script runs):

```bash
cursor-agent -p -m composer-2 --output-format json --api-key "$CURSOR_API_KEY" "<prompt>"
```

Optional: `CURSOR_AGENT_TIMEOUT_SEC` (default **1800**) per agent invocation.

## Environment variables

| Variable | Purpose |
|----------|---------|
| `PLAY_REPO` | Default `--play-repo` (same directory you pass to `setup.sh`) |
| `SPRING_REPO` | Optional explicit Spring root (skips `workspace.yaml` / `spring-<basename>` resolution) |
| `MIGRATION_STATUS_FILE` | Explicit status file path if set (only when `--status-file` is omitted) |
| `CURSOR_API_KEY` | Required for `cursor-agent` (omit with `--no-cursor`) |
| `CURSOR_MODEL` / `MIGRATION_CURSOR_MODEL` | Model slug |
| `MAX_RETRIES_PER_LAYER` | Default 5 |
| `MAX_TOTAL_LLM_CALLS` | Default 50 (whole run) |
| `MAX_ERRORS_TO_SEND_LLM` | Default 10 (per prompt) |
| `MAX_FILES_PER_FIX` | Default 3 (prompt instruction only) |
| `TIMEOUT_PER_LAYER_MINS` | Default 30 (wall clock per layer) |
| `MAX_FILES_PER_CURSOR_SESSION` | Default 10 (batch errors by file) |

## Paths (after automatic `setup.sh`)

Each run invokes **`setup.sh`** from the kit directory next to **`scripts/`** (same as running **`./setup.sh <play-repo>`** yourself). That writes **`workspace.yaml`** under the **workspace** directory (default: **parent of the play repo**) with absolute **`spring_repo`** and **`play_repo`**.

The orchestrator then resolves:

1. **`--spring-repo`** or **`SPRING_REPO`** — if set, used as-is.
2. Else **`spring_repo`** from **`<workspace>/workspace.yaml`**.
3. Else **`<workspace>/spring-<play-dir-name>`**, or **`<workspace>/<--spring-name>`** if you pass **`--spring-name`**.

**`--workspace`** / **`--spring-name`** — only when you use a non-default layout; they are forwarded to **`setup.sh`** the same way as manual **`./setup.sh --workspace … --spring-name …`**.

## CLI usage

Minimal (**only `--play-repo`**; **`setup.sh`** runs automatically). From inside the kit:

```bash
cd /path/to/play-to-spring-kit
python3 scripts/migration_orchestrator.py --play-repo ../cms-content-service
```

Or with an absolute play path from any cwd:

```bash
python3 /path/to/play-to-spring-kit/scripts/migration_orchestrator.py \
  --play-repo /path/to/cms-content-service
```

Custom workspace / Spring folder name (forwarded to **`setup.sh`**):

```bash
python3 scripts/migration_orchestrator.py \
  --play-repo /path/to/play-project \
  --workspace /path/to/workspace-dir \
  --spring-name my-spring-app
```

Explicit overrides:

```bash
python3 scripts/migration_orchestrator.py \
  --play-repo /path/to/play-project \
  --spring-repo /path/to/spring-project \
  --status-file /path/to/spring-project/migration-status.json
```

**Status file precedence:** `--status-file` → `MIGRATION_STATUS_FILE` → **`<spring-repo>/migration-status.json`**.

Common flags:

- `--batch-size N` — passed to `migrate-app`
- `--no-cursor` — compile only; exit **2** if errors repeat (stuck detection) without improvement
- `--fail-fast` — exit on first layer `loop_detected` / `max_retries` / `timeout`
- `--dry-run` — print actions, no subprocess side effects (limited)
- `--skip-inventory` — do not scan Play `app/` for `source_inventory`
- `--refresh-inventory` — recompute `source_inventory` even if present

## Init gate

If `initialize.status` in JSON is not **`done`**, the script exits **3** and **does not** modify the file. Generate `pom.xml`, `Application.java`, and `application.properties` first (builder / agent).

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | Finished (see `migration_verification` in JSON) |
| 1 | Unexpected error |
| 2 | Stuck compile with `--no-cursor` (error count not decreasing for 3 runs) |
| 3 | Initialize not done |
| 4 | `budget_exhausted` (`total_llm_calls` ≥ max) |
| 5 | `--fail-fast` stopped on layer failure |

## Resuming

Re-run the same command. Layers with `status: done` are skipped. State is written after major steps (atomic replace).

## Overnight run

```bash
nohup python3 /path/to/play-to-spring-kit/scripts/migration_orchestrator.py \
  --play-repo "$PLAY_REPO" \
  > orchestrator.log 2>&1 &
echo $! >> orchestrator.log
tail -f orchestrator.log
```

## Gitignore (Spring repo)

Ignore generated diagnostics:

```
.migration/
```

The script writes `errors-<layer>.json` under `<spring-repo>/.migration/`.

## `migration-status.json` extensions

The script merges backward-compatible fields:

- **`autonomous`**: `total_llm_calls`, `max_total_llm_calls`, `cursor_model`, `max_files_per_cursor_session`, `last_errors_path`
- **Per-layer** (in addition to existing keys): `retry_count`, `llm_calls`, `errors_history`, `failure_reason`, optional `compile_error_counts` (no-cursor stuck detection)
- **`failed_layers`**: append-only summary entries on terminal failure
- **`source_inventory`**: Play `app/**/*.java` counts by layer (LayerDetector rules)
- **`migration_verification`**: after all layers processed, Spring vs Play file counts

Layer order: **model → repository → manager → service → controller → other**.

## Session resume (`cursor-agent`)

Not implemented in v1. For long-lived fixes, see `cursor-agent ls` / `cursor-agent resume` in Cursor docs; future versions may persist `session_id`.
