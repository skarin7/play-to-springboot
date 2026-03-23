# Play-to-Spring Migration Kit

**Independent, reusable** kit to migrate any **Play Framework (Java)** repo to **Spring Boot**.

- **One `setup.sh`** scaffolds the Spring directory structure, workspace, skills, and copies the dev-toolkit JAR to the Play repo root.
- **LLM/agent** initializes the Spring project (`pom.xml`, `Application.java`, `application.properties`) by reading the Play project.
- **CLI** does ~70% deterministic migration; **LLM/agent** fixes the rest until the build is clean.

## Quick start

### 1. Build the dev-toolkit JAR (one time)

```bash
cd /path/to/java-dev-toolkit
mvn -q package
cp target/dev-toolkit-1.0.0.jar /path/to/play-to-spring-kit/lib/
```

### 2. Run setup (one time per Play repo)

```bash
cd /path/to/play-to-spring-kit
./setup.sh /path/to/<play-repo>
```

This will:

- Create **`spring-<basename>`** directory structure (no `pom.xml` or source files — the agent generates those).
- Copy **`dev-toolkit-1.0.0.jar`** to the Play repo root (`<play-repo>/dev-toolkit-1.0.0.jar`).
- Install **Cursor Agent skills** into `<play-repo>/.cursor/skills/`.
- Install under **`<play-repo>/.cursor/`**: **`skills/`** (Agent), **`config/`**, **`docs/`** — standard Cursor layout; **`kit_path`** in `workspace.yaml` points at **`.cursor`**.
- Write **`workspace.yaml`** in the workspace directory (parent of Play + Spring by default).
- **Progress** is tracked in **`<spring-repo>/migration-status.json`** by the orchestrator agent (not created by `setup.sh`).

### 3. Run the migration

From the **Play repo** directory:

```bash
cd /path/to/<play-repo>
java -jar dev-toolkit-1.0.0.jar migrate-app
```

Defaults: source = `.`, target = `../spring-<basename>`.

### 4. Build until clean

```bash
cd ../spring-<basename> && mvn compile
```

Fix errors, re-run `mvn compile`, repeat until it passes. Use the **Builder** and **Transformer** skills in Cursor to automate this.

## Python autonomous orchestrator (CLI)

For a **stdlib-only** driver that runs `migrate-app` + `mvn compile` per layer, optional **`cursor-agent`** for compile fixes, and persists state only in **`migration-status.json`**, see **[scripts/README.md](scripts/README.md)** (`migration_orchestrator.py`). **Only `--play-repo`** is required: it runs **`setup.sh`** automatically, then infers the Spring repo and **`migration-status.json`** from **`workspace.yaml`** (or **`spring-<play-basename>`**). Run it from this kit directory (e.g. **`cd play-to-spring-kit && python3 scripts/migration_orchestrator.py --play-repo ../your-play-app`**) so relative play paths resolve as you expect; **`setup.sh`** is still located via the script path, not cwd.

## Architecture & autonomous pipeline

See **[docs/play_to_spring_migration.md](docs/play_to_spring_migration.md)** for the full architecture: orchestrator + skills + **`dev-toolkit-1.0.0.jar`**, state file, layer order, and failure handling.

**After `setup.sh`, simplest autonomous trigger:** open the Play repo in Cursor → **Agent** chat → attach skill **`play-spring-orchestrator`** → send: *“Execute the full play-spring-orchestrator migration loop for this repo; resume from migration-status.json if present.”* (Details and longer prompt in **§2.1** of that doc.)

## Orchestration (Cursor agent flow)

See **[docs/ORCHESTRATION.md](docs/ORCHESTRATION.md)** for the step-by-step guide.

One **orchestrator agent** runs three steps:

1. **Initialize:** Read `build.sbt` + `application.conf` → generate `pom.xml`, `Application.java`, `application.properties` in the Spring repo.
2. **Transform:** `java -jar dev-toolkit-1.0.0.jar migrate-app` (from Play repo).
3. **Validate:** `cd ../spring-<basename> && mvn compile`; fix all errors; repeat until success.

All commands are in the skills; the agent runs CLI directly.

## Requirements

- **Bash**
- **Maven** (for the Spring project)
- **Java 17+** (for Spring Boot 3 and the dev-toolkit JAR)

## Layout after setup

```
workspace/
├── <play-repo>/                      # Your Play repo
│   ├── dev-toolkit-1.0.0.jar         # Copied by setup
│   └── .cursor/                      # Cursor + kit reference
│       ├── skills/                   # Agent skills (play-spring-*)
│       ├── config/                   # e.g. workspace.example.yaml
│       └── docs/                     # ORCHESTRATION, play_to_spring_migration, …
├── spring-<basename>/                # Spring Boot project (directory structure by setup; pom.xml etc. by agent)
│   ├── migration-status.json         # Created by orchestrator agent (resumable state)
│   ├── src/main/java/
│   ├── src/main/resources/
│   ├── src/test/java/
│   └── src/test/resources/
├── workspace.yaml
└── route-map.json                    # Optional placeholder from setup
```

## Cursor Agent skills

Setup copies skills into `<play-repo>/.cursor/skills/` so Cursor discovers them:

- **play-spring-orchestrator** — the single entry point: Initialize → Transform → Validate.
- **play-spring-transformer** — run dev-toolkit CLI to migrate classes.
- **play-spring-builder** — initialize Spring project (pom.xml, Application.java, application.properties) + compile + fix loop.

## Using the kit on another repo

1. Copy the **play-to-spring-kit** folder (or clone it).
2. Put `dev-toolkit-1.0.0.jar` in `play-to-spring-kit/lib/`.
3. Run `./setup.sh <path-to-any-play-repo>`.
4. From the Play repo: `java -jar dev-toolkit-1.0.0.jar migrate-app`.
5. Build and fix: `cd ../spring-<basename> && mvn compile`.
