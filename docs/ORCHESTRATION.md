# Running a migration

Step-by-step for the operator. Diagrams in [FLOW.md](FLOW.md); schema and role
contract in [STATE-CONTRACT.md](STATE-CONTRACT.md).

## Phase 0 — one-time setup

Install the `play-to-springboot` plugin (see the README), then run:

```
/play-to-springboot:migrate /path/to/your-play-app
```

The first run scaffolds the workspace automatically — `spring-<basename>/`,
`workspace.yaml`, `route-map.json` from `conf/routes`, `.migration/journal/`,
and a `migration/<name>` branch on the Spring repo — and fetches the
dev-toolkit jar (checksum-verified against the pin in
`scripts/tools/toolkit-release.json`, cached under `$CLAUDE_PLUGIN_DATA` so
later runs don't re-download it). There is no separate manual setup step and
nothing gets copied into `<play-repo>/.claude/` — skills and agents come from
the installed plugin itself.

Resuming an interrupted run is the same command: `/play-to-springboot:migrate`
reads `migration-status.json` first and picks up from the first incomplete
step.

## Phase 1 — research and architecture

The migrate skill works through: workspace setup → jar fetch → inventory →
researcher → architect → **Gate 1**.

## Gate 1 — approve the approach

You get the architect's `decisions.md`: dependency map, config map, idiom
decisions (notably the async policy), the `no_migration` list, and any concerns.

This is the cheapest correction point in the run. Everything downstream compiles
against these choices, so a wrong call here repeats in every layer.

Approve, or send it back with corrections.

## Phase 2 — initialize and dependency-check

Dev generates `pom.xml`, `Application.java`, and `application.properties`. The
manager then runs `gate.py --tiers T1` with **no sources migrated**. Zero code,
but it proves every declared dependency resolves — a bad dependency map fails
here in a minute rather than surfacing as strange compile errors four layers
deep.

## Phase 3 — the layer loop

Order: **model → repository → manager → service → controller → other**.

Per layer: dev transforms, compiles, and fixes → the manager runs `gate.py`
(T1/T2, plus T3 after controllers) → findings go back to dev with evidence
attached → when the gate passes, the manager commits.

**Dev owns the compile.** It runs `mvn compile` and fixes until the build is
clean. The gate re-runs it anyway — dev's report is a claim, the re-run is
evidence — but nobody gets dispatched to discover a missing import.

**The gate is a script, not an agent.** All four scripted tiers are
deterministic, so the manager runs them itself. A QA subagent is dispatched only
when `gate.py` sets `needs_agent`: errors landing in a layer already finished, a
build failure the parser could not classify, or a file that would not parse.
That removed one full agent round trip per layer.

Gate 1 is the only stop in this run — once you approve the architecture, the
layer loop runs unattended through every layer to the final report. Nothing
pauses for a "review the first layer" or "approve the merge" step; those two
gates existed in an earlier version of this tool and were dropped so a run
that starts overnight can actually finish overnight.

## Layer failure — soft, not a halt

When a layer reaches 3 attempts on its current batch, or the gate reports a
T2 blocker (a public method disappeared), the migrate skill writes
`.migration/escalation-<layer>.md` — the open findings, the last three
error-signature sets, and what dev tried — same as before. What's different:
it does **not** stop the run. The layer is recorded in `failed_layers` and the
loop moves on to the next layer. You read the escalation file afterward, via
the chat summary or `report.html`, not as an interruption mid-run.

Two things still halt the whole run outright, because they mean the tool's
core invariant broke rather than a migration attempt failing:

- `git -C <play-repo> status --porcelain` is non-empty (dev touched the
  read-only Play repo)
- dev reports it cannot proceed without changing the Play repo

## Phase 4 — endpoint parity (T5)

Compile, signatures, and route parity prove the code builds, kept its methods,
and answers at the right paths. None of them prove it **returns the same thing**.

The manager dispatches QA to boot both applications and compare responses:

```bash
python3 scripts/tools/endpoint_diff.py probes \
    --routes ../your-play-app/conf/routes --out .migration/endpoint-probes.json
# boot Play, then:
python3 scripts/tools/endpoint_diff.py capture --base-url http://localhost:9000 \
    --probes .migration/endpoint-probes.json --out .migration/responses-play.json
# boot Spring, then:
python3 scripts/tools/endpoint_diff.py capture --base-url http://localhost:8080 \
    --probes .migration/endpoint-probes.json --out .migration/responses-spring.json
python3 scripts/tools/endpoint_diff.py diff \
    --before .migration/responses-play.json --after .migration/responses-spring.json
```

Parameterless GET routes are probed automatically. Parameterised paths need a
sample value in `path_params`. **POST/PUT/PATCH/DELETE stay disabled by default**:
they need a request body, which `conf/routes` does not record, and identical
starting state in both apps, since the first capture changes what the second one
reads. Give each app a disposable datastore or reset between captures — otherwise
report those routes as unproved rather than enabling them and reading noise.

Timestamps, ids and durations are compared for presence and type, not equality;
two runs of the same app differ there. Field ordering is never a difference.
Everything left over is what QA rules on.

## Final gate and report

`gate.py --final` (full-tree T1–T4) plus `verify.py` for completeness and route
parity, plus the T5 result. Counts do not need to match exactly: files in
`no_migration` are subtracted from the baseline, and extra Spring files (config,
error handlers) are expected.

This doesn't wait for approval — it's not a gate, just the last step. The
migrate skill renders `.migration/report.html` (`scripts/tools/report.py`) and
prints a chat summary: `failed_layers`, and `qa_findings` at `blocker`
severity only. Everything else — clean layers, `major`/`minor` findings — is
in the report, not the chat. Re-open or regenerate it any time with
`/play-to-springboot:report /path/to/your-play-app`, which doesn't touch
migration state.

## Resuming

Re-run `/play-to-springboot:migrate /path/to/your-play-app`. Completed layers
are skipped, the transformer skips files already present in the target, and a
dev subagent killed mid-layer is picked up from its journal.

## Checking on it yourself

Run these from the plugin's own directory (`$CLAUDE_PLUGIN_ROOT`, or wherever
you installed/cloned it):

```bash
# counts, gate status, open findings
python3 scripts/migration_orchestrator.py status --play-repo ../your-play-app

# completeness and route parity
python3 scripts/migration_orchestrator.py verify --play-repo ../your-play-app

# fetch/locate the checksum-verified dev-toolkit jar
JAR=$(python3 scripts/tools/fetch_jar.py)

# the whole gate for one layer — same command the migrate skill runs
python3 scripts/tools/gate.py --play-repo ../your-play-app \
    --spring-repo ../spring-your-play-app --layer service --jar "$JAR"

# structural preservation on its own
java -jar "$JAR" signature <play>/app             > /tmp/p.json
java -jar "$JAR" signature <spring>/src/main/java > /tmp/s.json
python3 scripts/tools/signature_diff.py --play /tmp/p.json --spring /tmp/s.json \
    --layer service --layer-only

# regenerate the report without re-running anything
python3 scripts/tools/report.py \
    --status-file ../spring-your-play-app/migration-status.json \
    --out ../spring-your-play-app/.migration/report.html
```

Raw Maven output from the gate lands in `<spring-repo>/.migration/logs/`; the
command itself prints only the parsed verdict.

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
