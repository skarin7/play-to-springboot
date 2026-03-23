---
name: play-spring-transformer
description: Transform Play Framework Java files to Spring Boot. Use when migrating controllers, services, or any Play class.
---

# Transformer Skill

## Goal

Transform Play Framework classes into Spring Boot by running the **dev-toolkit CLI**. All commands are CLI-only. Use this when the orchestrator runs the Transform step or when you need to (re)run migration for a layer, a batch, or a single file.

**Layer scope** for `migrate-app --layer <name>` matches **dev-toolkit `LayerDetector`** (path under `app/`: `controllers`, `service`/`services`, `models` / `*Model.java`, `db`, `repositories`/`dao`, else `other`). The **`source_inventory.by_layer`** counts in `migration-status.json` use the same rules so the **Builder verification** step can compare Play vs Spring per layer.

## Commands (CLI only)

Run from the **Play repo** directory. Setup copies `dev-toolkit-1.0.0.jar` to the Play repo root.

### Migrate entire app (all layers, all files)

```bash
java -jar dev-toolkit-1.0.0.jar migrate-app
```

Defaults: source = `.` (current dir), target = `../spring-<basename>` where `<basename>` is the Play repo directory name.

### Migrate a single layer

```bash
java -jar dev-toolkit-1.0.0.jar migrate-app --layer model
```

Transforms all files in the specified layer. The CLI **skips files that already exist** in the target (idempotent). Valid layer values: `model`, `repository`, `manager`, `service`, `controller`, `other`.

### Migrate a layer in batches

```bash
java -jar dev-toolkit-1.0.0.jar migrate-app --layer service --batch-size 15
```

Transforms up to 15 files from the layer, skipping already-migrated files. Run the command again to process the next batch:

```bash
# First batch
java -jar dev-toolkit-1.0.0.jar migrate-app --layer service --batch-size 15
# Second batch (skips first 15 already-migrated files)
java -jar dev-toolkit-1.0.0.jar migrate-app --layer service --batch-size 15
```

### CLI output format

The CLI prints a summary line:

```
migrate-app done: N files, M errors, R remaining
```

- `N files` — files processed in this invocation
- `M errors` — files that failed transformation
- `R remaining` — files in scope not yet processed (only applies when `--batch-size` is used)

**Exit codes:**
- **0** — all files processed successfully (or no files in scope)
- **1** — some files had errors (partial success; agent should proceed to validate)

### Single-file transform (retry or targeted)

```bash
java -jar dev-toolkit-1.0.0.jar transform --input <play-file> --output <spring-file> [--layer controller|service|manager|model|repository|other]
```

`--layer` is optional; if omitted, layer is auto-detected from the path.

### Manual fallback (when CLI fails for a file)

If both `migrate-app` and `transform` fail for a specific file, manually migrate it:

1. Read the Play source file.
2. Apply Play→Spring mapping:
   - `@Singleton` → `@Component` (or `@Service`/`@RestController` based on layer)
   - `@Inject` → constructor injection
   - `play.mvc.Result` → `ResponseEntity`
   - `play.mvc.Controller` → `@RestController`
   - `play.Logger` → SLF4J `LoggerFactory`
   - `@BodyParser.Of` → `@RequestBody`
   - `F.Promise` / `CompletionStage` → keep or simplify to synchronous
3. Write the Spring file to the correct path under `../spring-<basename>/src/main/java/`.
4. Preserve business logic verbatim.

## Rules

- Never modify Play source files; only the Spring project is edited.
- Preserve business logic and query logic verbatim unless explicitly told otherwise.
- For `@Singleton` classes (e.g. MongoManager, GraphManager): use `@Component`, constructor injection, `@PreDestroy` for lifecycle—same as other classes.

## Success criteria

- CLI exits 0 (migrate-app or transform), OR
- CLI had partial failures but the agent manually migrated the remaining files.
- All files compile after the Validation step.

## Agent usage and verification

- **Running `migrate-app` or `transform` in the terminal** proves the **dev-toolkit** ran. It does **not** prove this **SKILL.md** file was read by the agent.
- To ensure this skill’s rules (batching, output parsing, manual fallback) are followed, the **user** should ask the agent to **Read** this path explicitly, or the **orchestrator** should Read it before transform steps.
- For compile-fix behavior and `migration-status.json` validation fields, use **play-spring-builder**—see that skill’s “Agent usage and verification” section.
- After all layers finish, **orchestrator Step 3** + **builder Step 3** use **`source_inventory`** and Spring re-counts to confirm migration completeness; keep **`layers.*.files_migrated`** updated from CLI output for audit trails, but rely on inventory comparison for “everything migrated?” checks.
