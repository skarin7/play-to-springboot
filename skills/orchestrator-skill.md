---
name: play-spring-orchestrator
description: Run the full migration: Transform (CLI) then Validation (compile + fix until clean).
---

# Orchestrator Skill

## Goal

You are the **orchestrator agent**. Run the full Play-to-Spring migration in three steps, processing files **per-layer in dependency order** and tracking progress in `migration-status.json`.

1. **Initialize** the Spring project (pom.xml, Application.java, application.properties).
2. **Transform + Validate per layer** — for each layer, transform files via CLI then compile and fix until clean before moving to the next layer.

Setup has already created the Spring directory structure and copied `dev-toolkit-1.0.0.jar` to the Play repo root.

## Agent usage and verification

Running “orchestrator” **workflow steps** (layers, CLI, `mvn compile`) is separate from loading **sibling skill files**:

| Mechanism | What it proves |
|-----------|----------------|
| `java -jar dev-toolkit-1.0.0.jar migrate-app ...` | The **toolkit** ran (transform). |
| `mvn compile` + Spring fixes | Validation happened; **not** proof that **play-spring-builder** was read. |
| Read tool on `play-spring-builder/SKILL.md` / `play-spring-transformer/SKILL.md` | Those **Cursor skills** were actually loaded. |

**Recommended behavior for the orchestrator agent:** At the start of a full migration, **Read** `play-spring-transformer/SKILL.md` before any `migrate-app` usage, and **Read** `play-spring-builder/SKILL.md` before init and before each layer’s compile-fix loop. Mention in the reply that those files were read.

**For the user:** To force skill usage, say explicitly: *“Read and follow play-spring-orchestrator, play-spring-transformer, and play-spring-builder from `.cursor/skills/`.”*

**Subagents:** If you use delegated agents, they appear as **separate runs** in the UI. One continuous thread without delegations usually means **no subagents**.

## State tracking — `migration-status.json`

Maintain this file at `<spring-repo>/migration-status.json`. Read it at the start of each run; write it after each action. This lets you resume from where you left off.

### Source inventory (baseline for “everything migrated?”)

After **initialize** completes (or once before the first `migrate-app` if you prefer), record **`source_inventory`**: recursive counts of Play **`*.java`** under the app source tree (typically `app/`), **including subfolders**. Use the **same layer rules as dev-toolkit** (`LayerDetector` — path segments, case-insensitive):

| Layer key | Path rule (any segment in path under `app/`) |
|-----------|-----------------------------------------------|
| `controller` | contains `/controllers/` |
| `service` | contains `/service/` or `/services/` |
| `model` | contains `/models/` **or** filename ends with `Model.java` / `model.java` |
| `manager` | contains `/db/` |
| `repository` | contains `/repositories/` or `/dao/` |
| `other` | any `*.java` under `app/` not matching above |

Also set **`total_java_files`** = sum of all `*.java` under `app/` (or your Play Java root). This baseline lets the **validator** (Builder skill) compare Play vs Spring after all layers compile.

See **Builder skill** for example commands and the **post-migration verification** step.

### Full schema (illustrative)

```json
{
  "current_step": "initialize | transform_validate | verify | done",
  "initialize": {
    "status": "pending | done | failed",
    "pom_generated": false,
    "application_java_generated": false,
    "application_properties_generated": false,
    "error": null
  },
  "source_inventory": {
    "captured_at": "ISO-8601 timestamp",
    "play_java_root": "app",
    "total_java_files": 0,
    "by_layer": {
      "model": 0,
      "repository": 0,
      "manager": 0,
      "service": 0,
      "controller": 0,
      "other": 0
    }
  },
  "layers": {
    "model":      { "status": "pending | in_progress | done", "files_migrated": 0, "files_failed": [], "validate_iteration": 0, "last_error_count": null },
    "repository": { "status": "pending", "files_migrated": 0, "files_failed": [], "validate_iteration": 0, "last_error_count": null },
    "manager":    { "status": "pending", "files_migrated": 0, "files_failed": [], "validate_iteration": 0, "last_error_count": null },
    "service":    { "status": "pending", "files_migrated": 0, "files_failed": [], "validate_iteration": 0, "last_error_count": null },
    "controller": { "status": "pending", "files_migrated": 0, "files_failed": [], "validate_iteration": 0, "last_error_count": null },
    "other":      { "status": "pending", "files_migrated": 0, "files_failed": [], "validate_iteration": 0, "last_error_count": null }
  },
  "migration_verification": {
    "status": "pending | passed | failed | needs_review",
    "checked_at": null,
    "spring_java_total": null,
    "notes": null,
    "layer_comparison": {
      "model": { "play_expected": null, "spring_actual": null, "delta": null },
      "repository": { "play_expected": null, "spring_actual": null, "delta": null },
      "manager": { "play_expected": null, "spring_actual": null, "delta": null },
      "service": { "play_expected": null, "spring_actual": null, "delta": null },
      "controller": { "play_expected": null, "spring_actual": null, "delta": null },
      "other": { "play_expected": null, "spring_actual": null, "delta": null }
    }
  }
}
```

### Resumability

1. Read `<spring-repo>/migration-status.json` on start.
2. Skip steps/layers already marked `"done"`.
3. Resume from the first incomplete layer.
4. If `current_step` is **`verify`**, run the **migration verification** step (Builder skill) before setting `done`.

## Order of execution

### Step 1 — Initialize Spring project

**Skip if** `initialize.status = "done"`.

Generate `pom.xml`, `Application.java`, and `application.properties` by reading the Play project (see **Builder skill** for details).

**After:** Update `migration-status.json` → `initialize.status = "done"`, `current_step = "transform_validate"`, and populate **`source_inventory`** (see state tracking above).

### Step 2 — Transform + Validate per layer

Process layers in dependency order: **model → repository → manager → service → controller → other**.

For each layer:

#### 2a. Transform the layer

**Skip if** this layer's `status = "done"`.

```bash
java -jar dev-toolkit-1.0.0.jar migrate-app --layer <layer>
```

This transforms **all** files in the layer. The CLI skips files that already exist in the target (idempotent). If there are too many files, use `--batch-size`:

```bash
java -jar dev-toolkit-1.0.0.jar migrate-app --layer <layer> --batch-size 15
```

Parse CLI output for file count and errors: `"migrate-app done: N files, M errors, R remaining"`.

- If `R remaining > 0`, run the command again to process the next batch.
- Record `files_migrated` and `files_failed` in status.

#### 2b. Validate the layer

```bash
cd ../spring-<basename> && mvn compile
```

**Loop:**
1. Run `mvn compile`. Increment `validate_iteration`.
2. If errors:
   - Parse error count. Record `last_error_count`.
   - Fix errors in the Spring project (imports, Play→Spring mapping, missing deps in `pom.xml`, missing files).
   - Run `mvn compile` again.
3. **Stuck detection:** If `last_error_count` hasn't decreased for 3 consecutive iterations, escalate to the user.
4. Repeat until `mvn compile` exits 0.

**After:** Update layer `status = "done"` in `migration-status.json`. Move to the next layer.

#### When to use `--batch-size`

- **Small layer (< 20 files):** Transform all at once (`--layer X` without `--batch-size`), validate, done.
- **Large layer (20+ files):** If the first full transform produces many compile errors (> 20), consider re-running subsequent layers with `--batch-size 15` to keep errors manageable. The CLI skips already-migrated files, so this is safe.

### Step 3 — Migration verification (after all layers compile)

**Skip if** `migration_verification.status` is already `passed` and you are not forcing a re-check.

After **every layer** is `done` and **`mvn compile`** is green for the full project:

1. Set `current_step` to **`verify`**.
2. Follow **Builder skill → Step 3: Migration completeness verification**:
   - Re-count Spring `*.java` under `src/main/java` (recursive).
   - Compare to **`source_inventory`** (Play counts and per-layer expectations).
   - Write results to **`migration_verification`** (status, counts, deltas, notes).
3. **Interpretation:** Perfect equality Play vs Spring is not always required (hand-written Spring files, deleted Play-only glue like `Module`/`Filters`, tests in one tree only). Use **`needs_review`** when counts diverge but reasons are explainable; **`failed`** when large unexplained gaps suggest unmigrated sources.
4. Set `current_step` to **`done`** only after verification is recorded.

### Completion

After verification, merge into `migration-status.json` (do not wipe earlier fields), for example:

```json
{
  "current_step": "done",
  "migration_verification": { "status": "passed | failed | needs_review", "...": "..." }
}
```

## Summary

| Step | Action |
|------|--------|
| 1 | Initialize: read `build.sbt` + `application.conf` → generate `pom.xml`, `Application.java`, `application.properties`. Record **`source_inventory`** from Play `app/**/*.java`. |
| 2 | For each layer (model → repository → manager → service → controller → other): transform with `migrate-app --layer X`, then `mvn compile` + fix until clean. |
| 3 | **Verify migration completeness** against `source_inventory`; update **`migration_verification`**; then mark **`done`**. |

## Layer order and rationale

| Order | Layer | Why |
|-------|-------|-----|
| 1 | model | No dependencies on other app code. Compiles independently. |
| 2 | repository | Depends on models only. |
| 3 | manager | Depends on models and repositories. |
| 4 | service | Depends on models, repositories, managers. |
| 5 | controller | Depends on services, managers, models. |
| 6 | other | Config, utilities, anything not classified above. |

## Halt conditions

- **Initialize:** If `build.sbt` or `application.conf` is missing, ask the user.
- **Transform:** Only halt if CLI can't start (JAR/Java missing). Partial failures → retry + proceed to validate.
- **Validation:** Escalate if error count doesn't decrease for 3 iterations.

## Optional after completion

- Run `mvn test` in the Spring repo and fix test failures.
- Re-run **Step 3** if you add new Play sources or re-run `migrate-app` for a subset of layers, so `migration_verification` stays truthful.
