# Play-to-Spring Orchestration: Steps for Cursor

One **orchestrator agent** runs the migration in two phases, processing files **per-layer in dependency order**. Setup creates the Spring directory structure, copies `dev-toolkit-1.0.0.jar` to the Play repo root, and installs skills to `.cursor/skills/`.

**Flow:** Orchestrator → (1) Initialize Spring project → (2) For each layer: transform via CLI → compile + fix until clean → **(3) Verify migration completeness** against `migration-status.json` **`source_inventory`**.

The Spring repo’s **`migration-status.json`** should include **`source_inventory`**: recursive **`*.java`** counts under Play **`app/`** (totals and **per-layer** buckets aligned with dev-toolkit **`LayerDetector`**). After the final green **`mvn compile`**, the **Builder** skill’s verification step compares those baselines to Spring **`src/main/java`** counts and records **`migration_verification`**. See `skills/orchestrator-skill.md` and `skills/builder-skill.md`.

## Orchestrator agent (single entry point)

Invoke the **Orchestrator** skill (e.g. `/play-spring-orchestrator`). The orchestrator:

1. **Initialize:** Read Play project's `build.sbt` and `conf/application.conf`; generate `pom.xml`, `Application.java`, and `application.properties` in the Spring repo.
2. **Transform + Validate per layer:** For each layer (model → repository → manager → service → controller → other):
   - Transform all files in the layer: `java -jar dev-toolkit-1.0.0.jar migrate-app --layer <layer>`
   - Validate: `cd ../spring-<basename> && mvn compile`; fix errors; repeat until clean.
   - Move to the next layer only when the current one compiles cleanly.
3. **Verify:** Re-count Spring Java sources vs **`source_inventory`**; update **`migration_verification`** in `migration-status.json`; then mark migration done.

**Placeholders:** `<play-repo>` = the Play project directory name; `spring-<basename>` = the Spring project directory created by setup.

---

## Phase 0: One-time setup (before any migration)

Do this once per Play repo.

### 0.1 Build the transformer JAR (java-dev-toolkit)

```bash
cd /path/to/java-dev-toolkit
mvn -q package
# JAR is at: target/dev-toolkit-1.0.0.jar
```

### 0.2 Put the JAR in the kit and run setup

```bash
cp /path/to/java-dev-toolkit/target/dev-toolkit-1.0.0.jar /path/to/play-to-spring-kit/lib/

cd /path/to/play-to-spring-kit
./scripts/setup.sh /path/to/<play-repo>
```

Setup creates the Spring directory structure (no `pom.xml` or source files — the agent generates those).

### 0.3 Open in Cursor

- Open the **Play repo** (skills in `.cursor/skills/`; JAR at repo root).
- Or open the **workspace directory** (parent of Play repo and `spring-<basename>`).

---

## Phase 1: Initialize Spring project (LLM)

The builder agent reads the Play project and generates:

1. **`pom.xml`** — read `build.sbt` to map dependencies to Maven equivalents (e.g. MongoDB driver → `spring-boot-starter-data-mongodb`).
2. **`application.properties`** — read `conf/application.conf` and map Play config keys to Spring equivalents.
3. **`Application.java`** — `@SpringBootApplication` with `main()` in the base package.

---

## Phase 2: Transform + Validate per layer (CLI + LLM)

Process layers in dependency order: **model → repository → manager → service → controller → other**.

For each layer, from the **Play repo** directory:

### 2a. Transform

```bash
java -jar dev-toolkit-1.0.0.jar migrate-app --layer model
```

The CLI skips files that already exist in the target (idempotent). For large layers, use batching:

```bash
java -jar dev-toolkit-1.0.0.jar migrate-app --layer service --batch-size 15
```

CLI output: `"migrate-app done: N files, M errors, R remaining"`.

### 2b. Validate

```bash
cd ../spring-<basename> && mvn compile
```

If errors: fix in Spring project (imports, Play→Spring mapping, missing deps in `pom.xml`), re-run `mvn compile`, repeat until it passes. Then move to the next layer.

### Layer order

| Order | Layer | Why |
|-------|-------|-----|
| 1 | model | No dependencies on other app code. |
| 2 | repository | Depends on models only. |
| 3 | manager | Depends on models and repositories. |
| 4 | service | Depends on models, repositories, managers. |
| 5 | controller | Depends on services, managers, models. |
| 6 | other | Config, utilities, anything not classified above. |

---

## State tracking — `migration-status.json`

The orchestrator maintains `<spring-repo>/migration-status.json` to track progress and enable resumability. On re-invoke, the orchestrator skips completed steps/layers and resumes from the first incomplete one.

---

## Summary

| Step | Action |
|------|--------|
| 1 | Initialize: read `build.sbt` + `application.conf` → generate `pom.xml`, `Application.java`, `application.properties`. |
| 2 | For each layer: `migrate-app --layer X` → `mvn compile` + fix until clean → next layer. |

## End-to-end checklist

- [ ] **Phase 0:** Build dev-toolkit JAR; put in `play-to-spring-kit/lib/`; run `./scripts/setup.sh <play-repo>`.
- [ ] **Phase 1:** Agent reads `build.sbt` + `application.conf` → generates `pom.xml`, `Application.java`, `application.properties`.
- [ ] **Phase 2:** For each layer: `migrate-app --layer <layer>` → `mvn compile` → fix → repeat until clean → next layer.
