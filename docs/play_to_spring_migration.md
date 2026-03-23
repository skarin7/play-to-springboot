# Play Framework → Spring Boot Migration

**Autonomous Migration Pipeline — Architecture & Implementation Guide for AI Coding Agents**

This document lives in **play-to-spring-kit** (`docs/play_to_spring_migration.md`). It is the canonical architecture guide for using **`dev-toolkit-1.0.0.jar`** with the orchestrator, transformer, and builder skills.

---

## Table of Contents

1. [Overview & Goals](#1-overview--goals)
2. [Assumed Autonomous Flow (Orchestrator + Skills + JAR)](#2-assumed-autonomous-flow-orchestrator--skills--jar)
3. [System Architecture](#3-system-architecture)
4. [Migration Dependency Order](#4-migration-dependency-order)
5. [State: `migration-status.json`](#5-state-migration-statusjson)
6. [`dev-toolkit-1.0.0.jar` — CLI Contract](#6-dev-toolkit-100jar--cli-contract)
7. [Play → Spring Mapping Reference](#7-play--spring-mapping-reference)
8. [Agent Skills (this kit)](#8-agent-skills-this-kit)
9. [How to Convert: CLI vs LLM](#9-how-to-convert-cli-vs-llm-recommended-approach)
10. [Long Runs & Resumability](#10-long-runs--resumability)
11. [Failure Handling Strategy](#11-failure-handling-strategy)
12. [Recommended Execution Order](#12-recommended-execution-order)
13. [Example: Project-Specific Notes (cms-content-service)](#13-example-project-specific-notes-cms-content-service)
14. [Improvements & Extensions](#14-improvements--extensions)
15. [Related docs in this kit](#15-related-docs-in-this-kit)

---

## 1. Overview & Goals

This document defines the architecture for migrating **Play Framework (Java)** to **Spring Boot** using:

- **`dev-toolkit-1.0.0.jar`** — deterministic bulk migration (`migrate-app`, `transform`).
- **play-to-spring-kit** — setup script + **Markdown skills** for Cursor/agents (`orchestrator`, `transformer`, `builder`).
- **Orchestrator agent** — tagged to follow the **orchestrator skill**; it creates/uses the Spring tree, runs the JAR, and coordinates **Builder** and **Transformer** sub-agents in a loop until the Spring project compiles.

### Design Principles

| Principle | Description |
|-----------|-------------|
| **Deterministic CLI** | **`dev-toolkit-1.0.0.jar`** handles the mechanical bulk of migration (paths, layers, AST transforms). |
| **AI agents** | Handle initialization (`pom.xml`, `Application.java`, `application.properties`), compile fixes, and edge cases the CLI does not fully resolve. |
| **Explicit state** | Progress is persisted in **`migration-status.json`** in the Spring repo; runs are **resumable**. |
| **Skills as contract** | Agent behavior is defined in **this kit’s** skill files — not Python glue scripts. |
| **Read-only Play source** | The Play project is **not** modified. The Spring project is a **parallel** tree (separate directory or repo). |

### Scope: Play Java vs Scala

- This guide targets **Play Java**. Scala projects need additional rules.

---

## 2. Assumed Autonomous Flow (Orchestrator + Skills + JAR)

**Tag the Orchestrator agent** to follow the instructions in **`skills/orchestrator-skill.md`**. That skill drives this loop:

| Step | What happens |
|------|----------------|
| **1. Parallel Spring project** | Setup (`./scripts/setup.sh <play-repo>`) creates **`spring-<basename>/`** next to the Play repo (directory layout). The **Builder** (per **builder skill**) generates **`pom.xml`**, **`Application.java`**, **`application.properties`** from `build.sbt` and `conf/application.conf`. Play and Spring stay **separate** — no mixing `build.sbt` with Maven in one tree. |
| **2. Run JAR to migrate Play → Spring** | From the **Play repo root** (where `dev-toolkit-1.0.0.jar` is copied by setup): **`java -jar dev-toolkit-1.0.0.jar migrate-app`** with optional **`--layer`** and **`--batch-size`**. Writes under **`<spring-repo>/src/main/java`**, preserving package paths. Idempotent: skips outputs that already exist. |
| **3. Coordinate agents** | **Transformer** skill: (re)run **`migrate-app`** / **`transform`** for targeted files, manual Play→Spring fixes when the CLI leaves gaps. **Builder** skill: **`mvn compile`**, parse errors, fix Spring-only (imports, deps, signatures). |
| **4. Loop** | Per layer (dependency order): migrate → compile → fix → update **`migration-status.json`** → next layer or next batch. Repeat until **`current_step: done`** or escalation (stuck errors). |

No separate Python pipeline is required for this flow — the **orchestrator + skills + JAR + Maven** are the pipeline.

### 2.1 Simplest trigger after `scripts/setup.sh` (Cursor)

1. **Open the right folder in Cursor** — the **Play repo** root, or a **multi-root workspace** that includes the Play repo and **`spring-<basename>`**. The agent must see **`<play-repo>/.cursor/skills/`** (created by `scripts/setup.sh`).
2. **Start a new Agent chat** (Agent mode, not a quick inline edit).
3. **Attach the orchestrator skill** — in the chat, use the **Skills** menu / `@` skills picker and choose **`play-spring-orchestrator`**. (Installed names are **`play-spring-orchestrator`**, **`play-spring-transformer`**, **`play-spring-builder`** — use **orchestrator** for the full autonomous loop.)
4. **Paste one prompt** (copy as-is or adjust paths):

```text
Follow the play-spring-orchestrator skill end-to-end for this workspace: read or create migration-status.json in the Spring repo, initialize Spring (pom.xml, Application.java, application.properties) from build.sbt and conf/application.conf if not done, then migrate per layer with `java -jar dev-toolkit-1.0.0.jar migrate-app` from the Play repo root (`--layer` / `--batch-size` as the skill says), then `mvn compile` in the Spring repo and fix errors until clean before the next layer. Do not modify the Play repo. Resume from migration-status.json if the run was interrupted.
```

**Shorter variant** (if the skill is already attached and you trust defaults):

```text
Execute the full play-spring-orchestrator migration loop for this repo; resume from migration-status.json if present.
```

You do **not** need separate chats for Builder/Transformer first — the orchestrator skill tells the agent when to apply those behaviors. Use **play-spring-builder** or **play-spring-transformer** only for focused follow-ups (e.g. “fix only compile errors” or “re-run transform for this one file”).

---

## 3. System Architecture

### 3.1 Component Map

| Layer | Technology | Responsibility |
|-------|-------------|----------------|
| **Orchestration** | Cursor agent + **orchestrator skill** | Reads/writes **`migration-status.json`**, runs layers in order, invokes JAR and Maven, delegates to Builder/Transformer behavior. |
| **Transformation** | **`dev-toolkit-1.0.0.jar`** | JavaParser-based transforms; **`migrate-app`** (bulk) and **`transform`** (single file). |
| **Validation** | **Maven** + **Builder skill** | **`mvn compile`** (and optionally **`mvn test`** later). |
| **Skills** | Markdown in **this repo** (`skills/`) | **Orchestrator**, **Transformer**, **Builder** — portable instructions. |

### 3.2 Workspace Layout

Open a **multi-root workspace** (Play + Spring) or a parent folder containing both:

```
workspace/
├── <play-repo>/              ← Play app (READ-ONLY during migration)
│   ├── dev-toolkit-1.0.0.jar ← copied here by scripts/setup.sh
│   ├── app/
│   ├── conf/
│   ├── build.sbt
│   └── .cursor/
│       ├── skills/           ← Cursor Agent skills (play-spring-*)
│       ├── config/           ← kit templates
│       └── docs/             ← orchestration + migration architecture
├── spring-<basename>/        ← Spring Boot project (agent writes here)
│   ├── migration-status.json
│   ├── pom.xml
│   └── src/main/java/
└── workspace.yaml            ← paths; kit_path = <play-repo>/.cursor
```

**Why parallel trees?** Different build systems (`build.sbt` vs `pom.xml`), clean git, independent compile/test.

### 3.3 Agent Roles (Simplified Swarm)

| Agent / Skill | Responsibility |
|-----------------|----------------|
| **Orchestrator** | Single entry point: initialize flow, per-layer **`migrate-app`**, **`mvn compile`** loop delegation, **`migration-status.json`**, halt/resume rules. |
| **Transformer** | CLI invocations (`migrate-app`, `transform`), manual file-level migration when CLI fails, Play→Spring mapping for remaining issues. |
| **Builder** | Generate/refresh **`pom.xml`** / **`application.properties`** / **`Application.java`**, run **`mvn compile`**, fix compilation errors in the Spring tree only. |

Optional later: QA/tests migration — not part of the minimal three-skill loop.

---

## 4. Migration Dependency Order

Files should follow **layer order** (matches **`LayerDetector`** + orchestrator skill):

| Order | Layer | Contents & rule |
|-------|-------|-----------------|
| 1 | **model** | POJOs / entities — minimal cross-deps. |
| 2 | **repository** | DAOs / repositories — depend on models. |
| 3 | **manager** | e.g. paths under `db/` — managers. |
| 4 | **service** | Services — depend on models, repos, managers. |
| 5 | **controller** | REST/controllers — depend on services. |
| 6 | **other** | Everything else the classifier did not label. |

**Key rule:** Run **`migrate-app --layer …`** in that order. You do **not** need a separate analyzer script — the toolkit classifies paths into layers.

**MongoManager / GraphManager:** Treat as Spring beans (`@Component`), config + `@PreDestroy`; **do not** rewrite query/driver logic for migration.

---

## 5. State: `migration-status.json`

The orchestrator maintains **`<spring-repo>/migration-status.json`** (see **orchestrator skill** for the full schema).

- **Resumability:** On each session, read this file; skip **`initialize`** or **layers** already marked **`done`**.
- **Per-layer fields:** status, files migrated/failed, validate iterations, last error count (for stuck detection).

Legacy **per-file** state machines (every path in one giant JSON) are optional; the kit’s **layer-centric** status is enough for **`dev-toolkit-1.0.0.jar`** + **`migrate-app`** because the CLI is **idempotent** (skips existing outputs).

---

## 6. `dev-toolkit-1.0.0.jar` — CLI Contract

Single shaded JAR built from **java-dev-toolkit**; version pinned as **`dev-toolkit-1.0.0.jar`** in docs and setup.

### 6.1 Placement

- Copied to **`<play-repo>/dev-toolkit-1.0.0.jar`** by **`scripts/setup.sh`** (from **`lib/dev-toolkit-1.0.0.jar`** in this kit).

### 6.2 Commands

**Bulk migration (primary):**

```bash
cd <play-repo>
java -jar dev-toolkit-1.0.0.jar migrate-app [--source .] [--target ../spring-<basename>] [--layer model|repository|manager|service|controller|other] [--batch-size N]
```

- No **`--layer`**: all layers (backward compatible).
- No **`--batch-size`**: all remaining files in scope for that run.
- Output line includes counts, e.g. **`migrate-app done: N files, M errors, R remaining`**.

**Single-file migration:**

```bash
java -jar dev-toolkit-1.0.0.jar transform --input <play-file> --output <spring-file> [--layer ...]
```

### 6.3 What the toolkit does / does not

| Does | Does not |
|------|----------|
| Writes under **`<target>/src/main/java`** preserving **`app/`** package layout | Create Spring Boot scaffolding (**`pom.xml`**, **`@SpringBootApplication`**) — **Builder** does that |
| Layer-aware stereotypes, many Play→Spring AST rewrites (see java-dev-toolkit README) | Guarantee zero manual fixes — **Builder** + **Transformer** close the gap |
| Skips target files that already exist (**idempotent** reruns) | Replace agent orchestration — orchestration is **agent + skills** |

### 6.4 Optional: route hints (not required for JAR)

Some Play repos keep a small script (e.g. **`scripts/routes_to_spring_map.py`**) that emits **route-map-hints.json** for controllers. This is a **documentation aid**, not part of **`dev-toolkit-1.0.0.jar`**.

---

## 7. Play → Spring Mapping Reference

### 7.1 General Mappings

Mechanical rules are implemented in **`dev-toolkit-1.0.0.jar`** (JavaParser). Remaining gaps are fixed by the **Transformer** / **Builder** agents using this table as guidance.

| Play Java | Spring Boot |
|-----------|-------------|
| routes | `@RestController` + `@*Mapping` |
| `Result` / `ok()` | `ResponseEntity` (toolkit + manual completion) |
| `application.conf` | `application.properties` / `application.yml` |
| `play.mvc.Controller` | `@RestController`, drop `extends Controller` |
| `@Inject` / Guice | `@Autowired` / constructor injection |
| `play.Logger` | SLF4J `LoggerFactory` |
| `CompletionStage<Result>` | Often **agent decision** (sync vs async vs WebFlux) |

### 7.2 Manager classes (MongoManager, GraphManager, …)

| Play | Spring |
|------|--------|
| `@Singleton` | `@Component` |
| `ApplicationLifecycle` stop hooks | `@PreDestroy` |
| `Play.application().configuration()` | `@Value` / `@ConfigurationProperties` |

Keep Mongo/Neo4j **driver usage** as-is unless you choose a deliberate refactor later.

### 7.3 Unmapped Patterns

If the CLI cannot safely transform something, the **Transformer** agent fixes it in the Spring tree or marks **needs_human** — **never** drop business logic.

---

## 8. Agent Skills (this kit)

Skills ship under **`skills/`** and are copied into **`<play-repo>/.cursor/skills/`** by **`scripts/setup.sh`**.

| Skill | Path | Role |
|-------|------|------|
| **Orchestrator** | `skills/orchestrator-skill.md` | **Tag this agent first** — full loop, **`migration-status.json`**, layer order. |
| **Transformer** | `skills/transformer-skill.md` | **`dev-toolkit-1.0.0.jar`** commands, **`transform`**, manual fallback. |
| **Builder** | `skills/builder-skill.md` | **`pom.xml`**, **`mvn compile`**, fix loop. |

There is **no** requirement for separate **analyzer**, **validator**, or **QA** skills for the minimal pipeline; compile + optional tests cover validation.

---

## 9. How to Convert: CLI vs LLM (Recommended Approach)

**Best approach: `dev-toolkit-1.0.0.jar` first, LLM for the rest.**

| Approach | Best for |
|----------|----------|
| **`migrate-app` / `transform`** | Bulk and repeatable AST migration |
| **Orchestrator + Builder + Transformer** | Init project, **`mvn compile`** fixes, edge cases |

**LLM-only** file-by-file migration is possible but costly and inconsistent at scale.

---

## 10. Long Runs & Resumability

### Pre-flight

- [ ] **`./scripts/setup.sh`** run; **`dev-toolkit-1.0.0.jar`** present at Play repo root.
- [ ] Spring project initialized (**`pom.xml`** compiles baseline).
- [ ] **`migration-status.json`** present or created on first run.

### Running

- Invoke the **Orchestrator** with the orchestrator skill; process **layer by layer**, optionally **`--batch-size 15`** for huge layers (e.g. many services).
- **Resume:** same workspace — re-read **`migration-status.json`** and continue.

### “Morning report”

Inspect **`migration-status.json`** and last **`mvn compile`** output.

---

## 11. Failure Handling Strategy

| Failure Type | Response |
|--------------|----------|
| **`migrate-app` partial errors** | Log; proceed to **`mvn compile`**; **Transformer**/**Builder** fix or use **`transform`** for single files. |
| **Compile errors** | **Builder** fixes; stuck if error count unchanged for **3** iterations — escalate. |
| **needs_human** | Akka, exotic plugins, etc. — mark and continue other files. |
| **Halt** | **> ~20%** of a layer stuck after real fix attempts — pause and fix toolkit/rules. |

---

## 12. Recommended Execution Order

| Step | Action |
|------|--------|
| 1 | Run **`scripts/setup.sh`** + ensure **`lib/dev-toolkit-1.0.0.jar`** exists before setup copies it to the Play repo. |
| 2 | **Builder:** generate Spring scaffold + **`migration-status.json`** initial shape. |
| 3 | **Orchestrator:** for each layer: **`migrate-app --layer …`** → **`mvn compile`** → fix → update status. |
| 4 | Optional: **`mvn test`**, migrate tests, integration checks. |
| 5 | Tag / branch Spring repo; document manual **needs_human** items. |

---

## 13. Example: Project-Specific Notes (cms-content-service)

*Example* migration for a large Play Java CMS service:

- **Stack:** Play Java, **`build.sbt`**, package **`com.xyz.service`**.
- **Routes:** **`conf/routes`** — map to Spring **`@*Mapping`**; optional route-hints script under the Play repo.
- **Config:** **`conf/application.conf`** → Spring **`application.properties`** / **`application.yml`**.
- **MongoManager / GraphManager:** **`app/.../db/`** — bean + lifecycle only; keep driver code.
- **Dependencies:** Mirror **`build.sbt`** in **`pom.xml`** (Mongo, Neo4j, Kafka, Redis, AWS, etc.).
- **Workspace:** After setup, see **`<play-repo>/.cursor/config/workspace.example.yaml`** as a template for paths (**`play_repo`**, **`spring_repo`**).

---

## 14. Improvements & Extensions

- **Spring Boot 3.x / Java 17+** alignment.
- **Tests:** Migrate JUnit/Mockito; Spring **`@MockBean`** where needed.
- **Secrets:** Never commit real secrets; use env vars.
- **Backup:** Copy **`migration-status.json`** before long sessions.

---

## 15. Related docs in this kit

| Doc | Purpose |
|-----|---------|
| [README.md](../README.md) | Quick start, layout, skills list |
| [ORCHESTRATION.md](./ORCHESTRATION.md) | Step-by-step Cursor orchestration |
| [LOCATION.md](../LOCATION.md) | Where to place the kit on disk |

**Setup recap:**

1. Build or obtain **`dev-toolkit-1.0.0.jar`** → **`lib/dev-toolkit-1.0.0.jar`**.
2. Run **`./scripts/setup.sh /path/to/<play-repo>`** — creates **`spring-<basename>/`**, copies the JAR to **`<play-repo>/`**, populates **`<play-repo>/.cursor/`** (**`skills/`**, **`config/`**, **`docs/`**).
3. Open the workspace; **tag the Orchestrator agent** with the **orchestrator skill** and follow **[§2](#2-assumed-autonomous-flow-orchestrator--skills--jar)**.

**Do not** use a removed **`migrate.sh`** — migration is **`java -jar dev-toolkit-1.0.0.jar migrate-app`** from the Play repo root, coordinated by the orchestrator skill.
