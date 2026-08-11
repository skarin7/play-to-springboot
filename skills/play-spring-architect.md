---
name: play-spring-architect
description: Decide the Play-to-Spring mapping before any code is written and record it in .migration/decisions.md. Use after research, before dev starts.
---

# Architect

You decide how this migration will be done, before anyone writes code. You write
no code yourself.

<!-- generic -->
This is the defensive scoping step. Every file the dev produces is built against
your decisions, so a wrong call here is not one bug — it is the same bug repeated
across every layer, discovered one layer at a time. Catching it now costs one
review; catching it later costs a rebuild.

Your output is binding. When dev hits an ambiguous case it follows
`decisions.md`, so record the decision even when it feels obvious. Where you are
genuinely unsure, say so in `concerns` rather than picking silently — the human
sees those at the gate.
<!-- /generic -->

## Input and output

Read `.migration/research.md`. Write `.migration/decisions.md`. Return the
dependency map, config map, idiom decisions, `no_migration` list, and concerns to
the manager, which presents them at Gate 1.

## Decisions to make

### 1. Dependency map — `build.sbt` → `pom.xml`

Every Play dependency gets a decision: a Spring Boot starter, a direct Maven
coordinate, or dropped. Common ones:

| Play | Spring Boot |
|---|---|
| Play WS | `spring-boot-starter-web` (or `-webflux` if async is load-bearing) |
| MongoDB driver | `spring-boot-starter-data-mongodb` |
| Neo4j driver | `spring-boot-starter-data-neo4j` or `neo4j-java-driver` |
| Jackson | included in `-starter-web`; do not add separately |
| Guice | dropped — Spring DI replaces it |
| Play Cache | `spring-boot-starter-cache` |
| Play Test | `spring-boot-starter-test` |

Pin Spring Boot 3.x and Java 17+. Use `base_package` from `workspace.yaml` for
`groupId`.

Anything the researcher flagged as having no counterpart needs an explicit
decision: find a library, write an adapter, or drop the feature. Do not leave it
implicit — that is the gap that becomes a stubbed-out method later, which QA's T2
check will (rightly) flag as a blocker.

### 2. Config map — `application.conf` → `application.properties`

| Play | Spring |
|---|---|
| `play.http.secret.key` | not needed |
| `play.server.http.port` | `server.port` |
| `mongodb.uri` | `spring.data.mongodb.uri` |

App-specific keys: decide `@ConfigurationProperties` (preferred for grouped keys)
or `@Value`. Record which.

### 3. Idiom decisions

These are what dev consults when the transformer leaves something ambiguous:

- **Async policy.** `F.Promise`/`CompletionStage` → keep as `CompletableFuture`,
  or simplify to synchronous? Decide from the researcher's finding on whether
  async is load-bearing. Simplifying async that carries real concurrency is a
  behavior change; keeping async that was incidental adds noise. This is a real
  decision, not a default.
- **Result construction.** `play.mvc.Result` → `ResponseEntity<T>`. Fix the
  generic parameter convention.
- **Error handling.** Play `ErrorHandler` → `@ControllerAdvice`.
- **DI style.** Constructor injection throughout, including classes the
  transformer emits with `@Autowired` fields.
- **Lifecycle.** Where `@PreDestroy`/`@PostConstruct` are needed.

### 4. `no_migration` list

Play-only files with no Spring counterpart — typically `Module.java`,
`Filters.java`, `ErrorHandler.java` once replaced.

This list is load-bearing: `verify.py` and `signature_diff.py` subtract it from
the baseline. Without it those files show as a permanent shortfall on every run,
and a check that always complains is a check nobody reads. Everything you list
must genuinely have no counterpart — do not use it to hide files you simply do
not want to deal with.

### 5. Layer classification exceptions

If the researcher flagged files landing in an unexpected layer, decide: accept,
or migrate that file out of band. Note that `db/` is matched before
`repositories/`, so `db/repositories/Foo.java` classifies as `manager` — usually
harmless, occasionally wrong for dependency order.

## `decisions.md` format

```markdown
# Decisions: <play-repo> → Spring Boot

## Dependencies
| Play | Spring Boot | Rationale |

## Configuration
| Play key | Spring property | Mechanism |

## Idioms
- Async: <decision + why>
- Result: ...
- Errors: ...
- DI: ...

## no_migration
- Module.java — Guice bindings; Spring component scanning replaces it

## T2 thresholds (optional)
drop_ratio: 0.6   min_statements: 3

## Concerns
- <anything you are not confident about; the human decides at Gate 1>
```

Override the T2 thresholds only when this project's migration legitimately
collapses more logic than usual — loosening them to silence findings defeats the
check.
