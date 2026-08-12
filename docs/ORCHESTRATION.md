# Running a migration

Step-by-step for the operator. Diagrams in [FLOW.md](FLOW.md); schema and role
contract in [STATE-CONTRACT.md](STATE-CONTRACT.md).

## Phase 0 — one-time setup

```bash
# Build the transformer, if you have the toolkit source
cd /path/to/java-dev-toolkit && mvn -q package
cp target/dev-toolkit-1.0.0.jar /path/to/play-to-spring-kit/lib/

# Prepare the workspace (idempotent; safe on every re-run)
cd /path/to/play-to-spring-kit
python3 scripts/migration_orchestrator.py setup --play-repo ../your-play-app
```

This creates `spring-<basename>/`, copies the JAR into the Play repo, installs
`.claude/skills/`, `.claude/agents/`, and `.cursor/skills/`, writes
`workspace.yaml`, seeds `route-map.json` from `conf/routes`, creates
`.migration/journal/`, and puts the Spring repo on a `migration/<name>` branch.

Check the inventory before starting:

```bash
python3 scripts/migration_orchestrator.py status --play-repo ../your-play-app
```

If `stale_jar_warnings.affected` is non-empty, the JAR in the Play repo predates
the LayerDetector fix and will place those files in the wrong layer. Refresh it
from `lib/` first.

## Phase 1 — run the manager

Open the **Play repo** in Claude Code:

> Run the play-spring-manager skill for this workspace; resume from
> migration-status.json if present.

The manager works through: inventory → researcher → architect → **Gate 1**.

## Gate 1 — approve the approach

You get the architect's `decisions.md`: dependency map, config map, idiom
decisions (notably the async policy), the `no_migration` list, and any concerns.

This is the cheapest correction point in the run. Everything downstream compiles
against these choices, so a wrong call here repeats in every layer.

Approve, or send it back with corrections.

## Phase 2 — initialize and dependency-check

Dev generates `pom.xml`, `Application.java`, and `application.properties`. QA then
compiles the project with **no sources migrated**. Zero code, but it proves every
declared dependency resolves — a bad dependency map fails here in a minute rather
than surfacing as strange compile errors four layers deep.

## Phase 3 — the layer loop

Order: **model → repository → manager → service → controller → other**.

Per layer: dev transforms and fixes → QA runs T1/T2 (plus T3 after controllers) →
findings go back to dev with evidence attached → when QA passes, the manager
commits.

**Gate 2** fires after the `model` layer: read the three or so generated files. If
the idiom is wrong, it is wrong in three files rather than in all of them.

Layers after that do not stop by default. Set `gates.mode: strict` in
`migration-status.json` to review every layer.

## Gate 3 — escalation

The manager stops and writes `.migration/escalation-<layer>.md` when any of:

- a layer reaches 3 attempts
- QA reports a T2 blocker (a public method disappeared)
- `git -C <play-repo> status --porcelain` is non-empty (dev touched the Play repo)

The file holds the open findings, the last three error-signature sets, and what
dev tried. Read that rather than the transcript.

## Gate 4 — merge

Final T1–T4 plus completeness and route parity. Counts do not need to match
exactly: files in `no_migration` are subtracted from the baseline, and extra
Spring files (config, error handlers) are expected.

## Resuming

Re-run the same manager invocation. Completed layers are skipped, the transformer
skips files already present in the target, and a dev subagent killed mid-layer is
picked up from its journal.

## Checking on it yourself

```bash
# counts, gate status, open findings
python3 scripts/migration_orchestrator.py status --play-repo ../your-play-app

# completeness and route parity
python3 scripts/migration_orchestrator.py verify --play-repo ../your-play-app

# structural preservation for one layer
java -jar dev-toolkit-1.0.0.jar signature <play>/app             > /tmp/p.json
java -jar dev-toolkit-1.0.0.jar signature <spring>/src/main/java > /tmp/s.json
python3 scripts/tools/signature_diff.py --play /tmp/p.json --spring /tmp/s.json --layer service
```

## Layer order

| Order | Layer | Why |
|-------|-------|-----|
| 1 | model | No dependencies on other app code. |
| 2 | repository | Depends on models only. |
| 3 | manager | Depends on models and repositories. |
| 4 | service | Depends on models, repositories, managers. |
| 5 | controller | Depends on services, managers, models. |
| 6 | other | Config, utilities, anything unclassified. |

Classification is by path segment: `controllers/`, `service/`|`services/`,
`models/` or `*Model.java`, `db/`, `repositories/`|`dao/`, else `other`. Note that
`db/` is tested before `repositories/`, so `db/repositories/Foo.java` classifies
as `manager`.
