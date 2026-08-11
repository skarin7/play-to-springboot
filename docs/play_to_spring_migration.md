# Play Framework → Spring Boot: architecture and mapping reference

Reference material for the migration: what the toolkit does, how layers are
classified, and how Play constructs map to Spring.

For how a run is sequenced, see [ORCHESTRATION.md](ORCHESTRATION.md). For the
role contract and state schema, see [STATE-CONTRACT.md](STATE-CONTRACT.md).

**Scope:** Play **Java**. Scala projects need additional rules.

---

## 1. Design principles

| Principle | What it means |
|---|---|
| **Deterministic CLI first** | `dev-toolkit-1.0.0.jar` handles the mechanical bulk — paths, layers, AST rewrites. |
| **Agents for judgment** | Dependency mapping, config mapping, compile fixes, edge cases. |
| **Scripts for anything countable** | Inventory, verification, signature diffs, error parsing. Agents count inconsistently; scripts are exact and free. |
| **Explicit state** | `migration-status.json`, written only by the manager. Runs resume. |
| **Sequencing in skills** | Not in orchestration code. There is one pipeline, not two. |
| **Play source is read-only** | Spring is a parallel tree. Different build systems, clean git, independent compile. |

---

## 2. Workspace layout

```
workspace/
├── <play-repo>/              ← READ-ONLY during migration
│   ├── dev-toolkit-1.0.0.jar
│   ├── app/  conf/  build.sbt
│   ├── .claude/{skills,agents}/
│   └── .cursor/{skills,config,docs}/
├── spring-<basename>/
│   ├── migration-status.json
│   ├── .migration/           ← research.md, decisions.md, findings, journals
│   ├── pom.xml
│   └── src/main/java/
├── workspace.yaml
└── route-map.json
```

---

## 3. Layer classification and order

Classification is by **path segment**, relative to the Java source root:

| Layer | Rule |
|---|---|
| `controller` | a `controllers` directory |
| `service` | a `service` or `services` directory |
| `model` | a `models` directory, **or** filename ending `Model.java` |
| `manager` | a `db` directory |
| `repository` | a `repositories` or `dao` directory |
| `other` | anything else |

Rules are applied in that order, so a file matching several resolves to the first.
Two consequences worth knowing:

- `db/repositories/Foo.java` classifies as **manager**, not repository.
- `db/UserModel.java` classifies as **model** — the filename convention is
  evaluated before the `db` rule.

Matching is on whole segments. An earlier version substring-matched
`/controllers/` against a path already relative to `app/`, so Play's default
scaffold layout (`app/controllers/HomeController.java`, `package controllers;`)
never matched and every controller migrated as `other` with no `@RestController`.
If `inventory.py` reports `stale_jar_warnings`, the JAR in your Play repo still
has that behavior — refresh it from `lib/`.

### Migration order

| Order | Layer | Why |
|-------|-------|-----|
| 1 | model | POJOs and entities; minimal cross-dependencies. |
| 2 | repository | Depends on models. |
| 3 | manager | Depends on models and repositories. |
| 4 | service | Depends on models, repositories, managers. |
| 5 | controller | Depends on services. |
| 6 | other | Everything unclassified. |

---

## 4. `dev-toolkit-1.0.0.jar` — CLI contract

Single shaded JAR built from **java-dev-toolkit**. `setup.sh` copies it from the
kit's `lib/` to `<play-repo>/dev-toolkit-1.0.0.jar`.

### Bulk migration

```bash
cd <play-repo>
java -jar dev-toolkit-1.0.0.jar migrate-app \
  [--source .] [--target ../spring-<basename>] \
  [--layer model|repository|manager|service|controller|other] [--batch-size N]
```

Without `--layer`, all layers. Without `--batch-size`, everything remaining in
scope. Output: `migrate-app done: N files, M errors, R remaining`. Exit 0 means
all processed; exit 1 means some files failed — compile anyway, then handle them.

### Single file

```bash
java -jar dev-toolkit-1.0.0.jar transform --input <play-file> --output <spring-file> [--layer ...]
```

### Structural signatures (T2 input)

```bash
java -jar dev-toolkit-1.0.0.jar signature <file-or-directory> [-o out.json]
```

Emits per class: public method names, arity, visibility, coarse return kind, and
statement counts. Deliberately coarse — exact types change legitimately during
migration, so recording them would flag every correctly migrated file. Feed the
output to `scripts/tools/signature_diff.py`.

### What the toolkit does and does not

| Does | Does not |
|------|----------|
| Writes under `<target>/src/main/java`, preserving `app/` package layout | Create Spring scaffolding (`pom.xml`, `@SpringBootApplication`) — **dev** does that |
| Layer-aware stereotypes and many Play→Spring AST rewrites | Guarantee zero manual fixes — **dev** closes the gap |
| Skips target files that already exist (idempotent re-runs) | Sequence the migration — that is the **manager** skill |

---

## 5. Play → Spring mapping reference

Mechanical rules are implemented in the JAR. This table guides **dev** for what
the transformer leaves behind, and **architect** when recording decisions.

### General

| Play Java | Spring Boot |
|-----------|-------------|
| routes file | `@RestController` + `@*Mapping` |
| `Result` / `ok()` | `ResponseEntity` |
| `application.conf` | `application.properties` / `application.yml` |
| `play.mvc.Controller` | `@RestController`, drop `extends Controller` |
| `@Inject` / Guice | constructor injection |
| `play.Logger` | SLF4J `LoggerFactory` |
| `@BodyParser.Of` | `@RequestBody` |
| `CompletionStage<Result>` | **architect decision** — sync, `CompletableFuture`, or WebFlux |

The last row is a real decision, not a default. Simplifying async that carries
genuine concurrency changes behavior; preserving async that was incidental adds
noise. The architect decides it from the researcher's findings and records it in
`decisions.md`.

### Manager classes (MongoManager, GraphManager, …)

| Play | Spring |
|------|--------|
| `@Singleton` | `@Component` |
| `ApplicationLifecycle` stop hooks | `@PreDestroy` |
| `Play.application().configuration()` | `@Value` / `@ConfigurationProperties` |

Keep Mongo/Neo4j **driver usage** as-is. Migration is not the time for a
deliberate persistence refactor.

### Dependencies

| Play | Spring Boot |
|---|---|
| Play WS | `spring-boot-starter-web` (or `-webflux`) |
| MongoDB driver | `spring-boot-starter-data-mongodb` |
| Neo4j driver | `spring-boot-starter-data-neo4j` or `neo4j-java-driver` |
| Jackson | included in `-starter-web` |
| Guice | dropped — Spring DI replaces it |
| Play Cache | `spring-boot-starter-cache` |
| Play Test | `spring-boot-starter-test` |

### Unmapped patterns

If the CLI cannot safely transform something, dev ports it by hand from the Play
source, or reports it unresolved. **Business logic is never dropped to make the
build pass** — QA's T2 check compares statement counts against the Play source
and reports a hollowed-out method as a blocker.

---

## 6. Play-only files

`Module.java`, `Filters.java`, and `ErrorHandler.java` typically have no Spring
counterpart: component scanning, filter beans, and `@ControllerAdvice` replace
them.

The architect records these in `architecture_review.no_migration`, and both
`verify.py` and `signature_diff.py` subtract them from the baseline. Without that
list they show as a permanent shortfall on every run — and a check that always
complains is a check nobody reads.

---

## 7. Verification

Compiling proves the code builds. It does not prove the program survived.

| Tier | Catches |
|---|---|
| **T1** compile | syntax, types, missing dependencies |
| **T2** signature diff | a method deleted, or hollowed out to `return null` |
| **T3** route parity | an endpoint that compiles but is unreachable — no mapping annotation |
| **T4** tests | behavioral regressions |

File counting catches none of T2 or T3: a stubbed method is still one file, and a
controller missing `@GetMapping` still compiles.

---

## 8. Resumability

- `migration-status.json` records completed layers; they are skipped on re-run.
- `migrate-app` skips target files that already exist.
- Dev appends to `.migration/journal/<layer>-dev.ndjson` as it works, so a
  subagent killed mid-layer is resumed rather than restarted — its context is
  gone, but its journal is not.
- The manager commits after each layer passes QA, so a rejected review gate is a
  reset rather than a manual unwind.
