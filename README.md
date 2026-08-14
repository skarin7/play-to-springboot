# Play-to-Spring Boot

A Claude Code plugin that migrates a **Play Framework (Java)** repo to
**Spring Boot** using four agent roles coordinated through shared state, with
a single upfront human review gate.

Deterministic work is done by tools; judgment is done by agents; sequencing
lives in skill documents rather than in orchestration code.

## How it works

| Role | Writes code | Job |
|---|---|---|
| **migrate** (main thread) | no | Owns state, sequences layers, dispatches subagents, enforces the gate, commits |
| **researcher** | no | Surveys the Play repo before anything is built |
| **architect** | no | Decides the dependency, config, and idiom mapping — before dev starts |
| **dev** | **yes** | Runs the transformer, compiles, fixes compile errors. The only role that writes source |
| **qa** | no | Verifies endpoint responses before and after; rules on results a script cannot judge. Never fixes |

```
migrate → researcher → architect ──── GATE 1 (you approve the approach)
                                        ↓
   dev writes pom/Application → gate.py compiles the EMPTY project (dependency check)
                                        ↓
   per layer:  dev → gate.py ──findings──┐   model → repository → manager
                 ↑────────────────────────┘   → service → controller → other
               passes → commit → next layer, unattended
     (a layer that exhausts retries joins failed_layers; the run keeps going)
                                        ↓
        gate.py --final + QA endpoint diff → report.py → report.html + chat summary
```

Sequential by design: the layer dependency order is real, and verifying
half-migrated code is noise. Gate 1 is the only stop — once you approve the
architecture, the rest of the run is unattended through to the report.

Diagrams of the full flow, the dev/gate correction loop, endpoint parity, and who
writes what: **[docs/FLOW.md](docs/FLOW.md)**.

### Why the roles are split this way

- The **researcher** runs first because the standard failure of coding agents is
  confident output that ignores how the codebase actually works.
- The **architect** gate exists because a wrong `pom.xml` is not one bug — it is
  the same bug in every layer, found one layer at a time.
- **Dev owns the compile.** Nobody should be dispatched to discover a missing
  import. The migrate skill re-runs the build in the gate regardless: dev's
  report is a claim, the re-run is evidence.
- **The verification gate is a script, not an agent.** T1–T4 are deterministic,
  so the migrate skill runs them itself via `scripts/tools/gate.py`. Wrapping
  them in a dispatch cost a round trip per layer and returned the same finding
  the script had already produced.
- **QA never fixes**, because an agent that fixes what it checks stops finding
  things. It is dispatched for the judgment a script cannot supply: endpoint
  response parity, and attributing a failure to the layer that actually caused it.

### Why dev-toolkit does the transform, not the agent

The dev-toolkit jar is a real Java-AST tool: it parses Play source and
emits Spring source by rule (`@Singleton` → `@Component`/`@Service`/
`@RestController`, `Result` → `ResponseEntity<T>`, Guice field injection →
constructor injection, package/import rewriting). The dev agent runs it rather
than freehand-porting every file, for reasons distinct from "agents are
fallible":

- **Reproducible.** Same Play file in, same Spring file out, every run,
  independent of which model session touches it — and testable
  (`scripts/tools/test_tools.py`) in a way free-form LLM output isn't.
- **Independent of the check that grades it.** T2 (`signature_diff.py`) exists
  to answer "did the logic survive." If the same model both wrote the
  translation and were the only thing checking it, that check would be the
  model grading its own homework. A separate deterministic tool doing the
  mechanical part keeps the check meaningful.
- **Cheap at the file counts this runs at.** Hundreds of files of pure
  boilerplate substitution carry zero judgment — spending agent tokens on them
  buys nothing.
- **Consistent across a long run.** A model asked "migrate this file" a
  hundred times over a multi-hour session can drift in style file to file. A
  rule applied by code does not drift.

The agent is reserved for what a rule genuinely can't decide: fixing the
compile errors the transform's output produces, porting logic the CLI can't
handle (hand-rolled algorithms, the `F.Promise`/`CompletionStage` idiom
choice), and QA's judgment calls (T5 endpoint diffing, cross-layer error
attribution). Everything mechanical stays in the tool; everything that
requires reading code and deciding stays with the model.

## Install

Self-hosted marketplace (until this plugin lands in the public
`claude-community` marketplace review):

```
/plugin marketplace add skarin7/play-to-springboot
/plugin install play-to-springboot@play-to-springboot-marketplace
```

Or point Claude Code straight at a local clone for development:

```bash
claude --plugin-dir /path/to/play-to-springboot
```

**Requirements:** Java 17+, Maven, Python 3.10+, network access on first run
(to fetch the dev-toolkit jar — cached after that).

## Quick start

```
/play-to-springboot:migrate /path/to/your-play-app
```

That's the whole thing. The first run scaffolds the workspace (Spring repo
skeleton, `workspace.yaml`, `.migration/`) and fetches the checksum-verified
dev-toolkit jar automatically — no separate setup step. It stops once, at
Gate 1, with the architect's decisions for your review; after you approve,
the rest of the run — every layer, the final verification, the report — goes
unattended.

Check back on a run any time without re-running it:

```
/play-to-springboot:report /path/to/your-play-app
```

opens/regenerates `.migration/report.html` — a self-contained, offline page
with per-layer status, the full QA findings table, T5 endpoint-parity results,
and commit references.

## Verification tiers

| Tier | What | When | Who runs it |
|---|---|---|---|
| **T1** compile | `mvn compile` exits 0 | every layer | dev, then `gate.py` |
| **T2** structural preservation | signature diff Play vs Spring | every layer, scoped to that layer | `gate.py` |
| **T3** route parity | `conf/routes` vs Spring mappings | after `controller`, and final | `gate.py` |
| **T4** tests | `mvn test` | final | `gate.py --final` |
| **T5** endpoint responses | responses captured from both apps, diffed | final | **QA agent** |

Two of these earn their place by catching what the others cannot.

**T2** catches the stub-out. A file can compile, keep every method, and still
have had its body replaced with `return null` — file counting scores that as
success. It reports exactly two things: a public method that disappeared, and a
method body that collapsed to near-nothing. The narrowness is deliberate;
migration legitimately rewrites bodies, and blocker-severity false positives
teach the reviewer to wave findings through.

**T5** catches everything structural checks are blind to. T1–T4 prove the code
builds, kept its methods, and answers at the right paths. Only T5 proves it
returns the same thing. Boot Play, capture per-route responses, boot Spring,
capture, diff. Volatile values (timestamps, ids, durations) are compared for
presence and type rather than equality; field ordering is never a difference.
What remains is judgment, which is why T5 is the tier with an agent attached.

Mutating verbs are seeded disabled — a POST needs a request body `conf/routes`
does not record, and identical starting state in both apps. See
[docs/ORCHESTRATION.md](docs/ORCHESTRATION.md#phase-4--endpoint-parity-t5).

## Deterministic helpers

Agents call these; so can you (from the plugin's own directory). All print
JSON to stdout except `fetch_jar.py` and `report.py`, which print a path.

| Tool | Purpose |
|---|---|
| `scripts/tools/gate.py` | **T1–T4 in one call**, with a single verdict and findings |
| `scripts/tools/endpoint_diff.py` | T5: seed probes, capture responses, diff them |
| `scripts/tools/inventory.py` | Per-layer file counts, both trees; picks role mode |
| `scripts/tools/verify.py` | Completeness + T3 route parity |
| `scripts/tools/signature_diff.py` | T2 structural preservation |
| `scripts/tools/parse_mvn.py` | Maven log → structured errors |
| `scripts/tools/state.py` | Atomic single-writer state access |
| `scripts/tools/routes.py` | Play routes / Spring mappings extraction |
| `scripts/tools/fetch_jar.py` | Downloads, checksum-verifies, and caches the dev-toolkit jar |
| `scripts/tools/report.py` | Renders the self-contained `report.html` |
| `scripts/tools/token_report.py` | Measured token and cost accounting per run |

```bash
python3 scripts/tools/test_tools.py    # 96 tests, stdlib only
```

## Layout

```
play-to-springboot/                     (plugin root)
├── .claude-plugin/{plugin.json,marketplace.json}
├── LICENSE
├── skills/{migrate,report}/SKILL.md    # /play-to-springboot:migrate, :report
├── agents/{researcher,architect,dev,qa}.md
├── scripts/{setup.sh,migration_orchestrator.py,tools/}
│   └── tools/toolkit-release.json      # pins {version, download_url, sha256}
├── config/workspace.example.yaml
└── docs/{STATE-CONTRACT.md,ORCHESTRATION.md,play_to_spring_migration.md,FLOW.md}

workspace/                              (created per target Play repo)
├── <play-repo>/                        # READ-ONLY during migration — untouched
│                                        # by this plugin except for git status checks
├── spring-<basename>/
│   ├── migration-status.json           # single source of truth
│   ├── .migration/                     # research.md, decisions.md, findings,
│   │                                   # journals, report.html
│   └── src/main/java/
├── workspace.yaml
└── route-map.json                      # populated from conf/routes
```

The dev-toolkit jar lives in `$CLAUDE_PLUGIN_DATA` (fetched once per version,
cached across runs and across target repos) — it is never copied into either
repo.

## State and resumability

`migration-status.json` is written **only by the migrate skill**. Subagents
report through artifacts under `.migration/` and through append-only
journals, which is what lets a killed subagent be resumed rather than
restarted.

Re-running `/play-to-springboot:migrate` is always safe: completed layers are
skipped, the transformer skips files that already exist in the target, and a
layer already recorded in `failed_layers` is retried from scratch rather than
silently re-skipped.

Full schema, handoff rules, and gate semantics: **[docs/STATE-CONTRACT.md](docs/STATE-CONTRACT.md)**.

## Gotchas

Things this design does not, and mostly cannot, guarantee:

- **Orchestration is prose, not code.** The per-layer/per-batch loop, gate
  re-runs, `attempts.<layer>.count` resetting on batch advance, escalation
  triggers — all of it lives in `skills/migrate/SKILL.md` as instructions an
  agent follows, not an enforced state machine. A model that skips a step,
  marks a layer `done` without actually gating its last batch, or forgets to
  reset the attempts counter is not stopped by anything except your review at
  Gate 1 or after the run — or `verify.py`'s completeness check catching the
  shortfall late, at the final gate, rather than fail-fast where it happened.
- **The single-writer rule for `migration-status.json` is a convention, not a
  lock.** Only the migrate skill is supposed to write it; nothing at the
  filesystem level stops a subagent's `Bash` grant from writing it directly
  (see STATE-CONTRACT.md's "Bash can write" gap). Corruption from a second
  writer is a misfollowed-skill risk, not just a crash risk.
- **T1–T4 are deterministic; the transform's edge cases and QA's judgment are
  not.** Reserving agent work for logic-porting and T5 means real
  non-determinism remains exactly where it was moved to on purpose — the same
  endpoint diff can get a different judgment call session to session even
  though the scripts around it can't.
- **T2 is narrow by design, which means real blind spots.** It catches a
  missing method or a >60%-statement collapse, not "compiles, keeps every
  statement, does something subtly different." An inverted condition or an
  off-by-one passes T1–T4 clean; only T5 has a chance, and T5's mutating-verb
  coverage is GET-only by default.
- **A layer that keeps failing doesn't stop the run — you find out at the
  end.** Dropping the per-layer review gate means a stuck layer runs to its
  3-attempt limit and gets recorded in `failed_layers` rather than
  interrupting you immediately; you read about it in the chat summary or
  `report.html`, not in the moment it happened.

## Notes

- `scripts/migration_orchestrator.py` no longer orchestrates. It does workspace
  setup and status reporting. Sequencing, model choice, and failure handling
  belong to the agent now.
- The dev-toolkit jar's provenance is a checksum, not a filename or an
  inspected marker class. `scripts/tools/toolkit-release.json` pins
  `{version, download_url, sha256}`; `fetch_jar.py` refuses to hand back
  anything that doesn't match. See the sibling
  [`java-dev-toolkit`](https://github.com/skarin7/play-to-spring-boot-migration-agent)
  repo for how that release is built and published.
