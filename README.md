# Play-to-Spring Migration Kit

Migrate a **Play Framework (Java)** repo to **Spring Boot** using five agent
roles coordinated through shared state, with human review gates.

Deterministic work is done by tools; judgment is done by agents; sequencing lives
in skill documents rather than in orchestration code.

## How it works

| Role | Writes code | Job |
|---|---|---|
| **manager** | no | Owns state, sequences layers, dispatches subagents, enforces gates, commits |
| **researcher** | no | Surveys the Play repo before anything is built |
| **architect** | no | Decides the dependency, config, and idiom mapping — before dev starts |
| **dev** | **yes** | Runs the transformer, fixes compile errors. The only role that writes source |
| **qa** | no | Verifies across four tiers, emits findings, never fixes |

```
manager → researcher → architect ──── GATE 1 (you approve the approach)
                                        ↓
   dev writes pom/Application → QA compiles the EMPTY project (dependency check)
                                        ↓
   per layer:  dev → qa ──findings──┐   model → repository → manager
                 ↑─────────────────┘    → service → controller → other
               passes → commit → GATE 2 after the first layer
                                        ↓
              final QA (T1-T4) ──── GATE 4 (you approve the merge)
```

Sequential by design: the layer dependency order is real, and QA on
half-migrated code is noise.

### Why the roles are split this way

- The **researcher** runs first because the standard failure of coding agents is
  confident output that ignores how the codebase actually works.
- The **architect** gate exists because a wrong `pom.xml` is not one bug — it is
  the same bug in every layer, found one layer at a time.
- **QA never fixes**, because an agent that fixes what it checks stops finding
  things. It re-runs the build itself rather than trusting dev's report.

## Quick start

```bash
# 1. Prepare the workspace (idempotent; safe to re-run)
cd /path/to/play-to-spring-kit
python3 scripts/migration_orchestrator.py setup --play-repo ../your-play-app

# 2. Open the Play repo in Claude Code and invoke the manager skill
#    "Run the play-spring-manager skill for this workspace;
#     resume from migration-status.json if present."
```

The manager stops at Gate 1 with the architect's decisions for your review.

**Requirements:** Java 17+, Maven, Python 3.10+, and
`dev-toolkit-1.0.0.jar` in `lib/`.

## QA tiers

The check that matters most is T2. A file can compile, keep every method, and
still have had its body replaced with `return null` — file counting scores that
as success.

| Tier | What | When |
|---|---|---|
| **T1** compile | `mvn compile` exits 0 | every layer |
| **T2** structural preservation | signature diff Play vs Spring | every layer |
| **T3** route parity | `conf/routes` vs Spring mappings | after `controller`, and final |
| **T4** tests | `mvn test` | final |

T2 reports exactly two things — a public method that disappeared, and a method
body that collapsed to near-nothing. The narrowness is deliberate: migration
legitimately rewrites bodies, and blocker-severity false positives teach the
reviewer to wave findings through.

## Deterministic helpers

Agents call these; so can you. All print JSON to stdout.

| Tool | Purpose |
|---|---|
| `scripts/tools/inventory.py` | Per-layer file counts, both trees; picks role mode |
| `scripts/tools/verify.py` | Completeness + T3 route parity |
| `scripts/tools/signature_diff.py` | T2 structural preservation |
| `scripts/tools/parse_mvn.py` | Maven log → structured errors |
| `scripts/tools/state.py` | Atomic single-writer state access |
| `scripts/tools/routes.py` | Play routes / Spring mappings extraction |

```bash
python3 scripts/tools/test_tools.py    # 52 tests, stdlib only
```

## Layout after setup

```
play-to-spring-kit/
├── lib/dev-toolkit-1.0.0.jar
├── skills/play-spring-{manager,researcher,architect,dev,qa}.md
├── agents/play-spring-{researcher,architect,dev,qa}.md   # tool grants
├── scripts/{setup.sh,migration_orchestrator.py,tools/}
└── docs/{STATE-CONTRACT.md,ORCHESTRATION.md,play_to_spring_migration.md}

workspace/
├── <play-repo>/                   # READ-ONLY during migration
│   ├── dev-toolkit-1.0.0.jar
│   ├── .claude/{skills,agents}/   # role isolation enforced here
│   └── .cursor/skills/            # skills only — see caveat below
├── spring-<basename>/
│   ├── migration-status.json      # single source of truth
│   ├── .migration/                # research.md, decisions.md, findings, journals
│   └── src/main/java/
├── workspace.yaml
└── route-map.json                 # populated from conf/routes
```

## Claude Code vs Cursor

Role boundaries are enforced by subagent tool grants: only `play-spring-dev` has
`Edit`/`Write`. **Cursor has no subagent isolation**, so there the roles are
advisory prose and one agent plays all of them, marking its own homework.

The kit installs to both, but the Cursor path is degraded. On it, the
`git -C <play-repo> status --porcelain` guard and script-owned verification are
mandatory rather than belt-and-braces.

## State and resumability

`migration-status.json` is written **only by the manager**. Subagents report
through artifacts under `.migration/` and through append-only journals, which is
what lets a killed subagent be resumed rather than restarted.

Re-running is always safe: completed layers are skipped, and the transformer
skips files that already exist in the target.

Full schema, handoff rules, and gate semantics: **[docs/STATE-CONTRACT.md](docs/STATE-CONTRACT.md)**.

## Notes

- `scripts/migration_orchestrator.py` no longer orchestrates. It does workspace
  setup and status reporting. Sequencing, model choice, and failure handling
  belong to the agent now.
- If `inventory.py` reports `stale_jar_warnings`, the `dev-toolkit-1.0.0.jar` in
  your Play repo predates the LayerDetector segment-matching fix and will migrate
  those files in the wrong layer. Refresh it from `lib/`.
