# State contract

How the roles coordinate. Read this before changing `migration-status.json` or
adding a role. Diagrams: [FLOW.md](FLOW.md).

## Single writer

**Only the manager writes `migration-status.json`**, through
`scripts/tools/state.py`. No subagent touches it.

Two writers corrupt the file. A subagent killed mid-write leaves broken JSON,
which destroys the resume path — the one thing that makes a long migration
survivable.

Subagents report through three channels instead:

| Channel | Mechanism | Who | Purpose |
|---|---|---|---|
| **Artifact** | a file under `.migration/` | researcher, architect, QA | The detailed output nobody needs in context until they do |
| **Journal** | append-only NDJSON in `.migration/journal/` | dev | Crash recovery |
| **Return summary** | text back to the manager | all | What the manager acts on |

## Who verifies what

The four scripted tiers are deterministic — a subprocess call and a comparison —
so they are the manager's own work, not an agent's:

| Tier | Runs | Who |
|---|---|---|
| **T1** compile | `mvn compile` → `parse_mvn.py` | dev while fixing; the manager again via `gate.py` |
| **T2** preservation | `signature_diff.py` | manager, `gate.py` |
| **T3** route parity | `routes.py` / `verify.py` | manager, `gate.py` |
| **T4** tests | `mvn test` | manager, `gate.py --final` |
| **T5** endpoint parity | `endpoint_diff.py`, both apps booted | **QA agent** |

`scripts/tools/gate.py` runs T1–T4 in one call and prints a single verdict. Raw
Maven output goes to `.migration/logs/`; only the parsed summary reaches stdout,
which is what lets the manager own the check without breaking the invariant
below.

**Dev owns the compile.** It runs `mvn compile` and fixes until the build is
clean or it has an honest blocker. The manager re-runs T1 in the gate regardless
— dev's report is a claim, the re-run is evidence. Dev compiling first is not a
substitute for the gate; it is what stops the gate being dev's debugger.

### When QA is dispatched

Wrapping deterministic tiers in an agent bought one round trip per layer and
returned the same finding the script had already produced. QA is dispatched only
where a result needs interpreting, signalled by `needs_agent` in the gate output:

- compile errors landing in a layer already marked `done` (cross-layer attribution)
- `unparsed_tail` non-empty — the build failed in a way the parser could not classify
- T2 `parse_errors` — a file that will not parse is unexamined, not passing
- any tier returning `status: error` — the check itself did not run
- **T5**, always: judging field ordering, null-versus-absent, and expected value
  drift is the judgment a script cannot supply

### T5 and mutating verbs

`endpoint_diff.py probes` seeds parameterless GET routes enabled and everything
else disabled. POST/PUT/PATCH/DELETE need two things `conf/routes` does not
record: a request body, and identical starting state in both applications. Give
each app its own disposable datastore, or reset and reseed between the two
captures. Where that is not possible, a GET-only comparison is the honest check
and the mutating paths stay a manual review — recorded as such, not as a pass.

### Why the journal is append-only

A subagent's context dies with it. If dev is killed at file 8 of 15, everything
it knew is gone — except what it appended to disk. The manager folds the journal
in (`state.py fold-journal`) and resumes at the right place.

Append-only, rather than the subagent updating a progress file, because appending
has no read-modify-write step to be interrupted halfway. A truncated final line
is skipped; the completed lines before it are still good.

Journal lines:

```json
{"layer":"service","action":"migrated","count":3}
{"layer":"service","action":"failed","file":"ContentService.java"}
{"layer":"service","action":"compiled","error_count":7}
```

## Context handoff

<!-- generic -->
Subagent contexts do not merge. Each starts cold and returns one summary; the
parent sees only that summary. So handoff is deliberate, in three directions:

- **Push** — the manager's dispatch brief: which skill to load, the layer, the
  file list, **paths** to artifacts, finding IDs. Never file contents.
- **Pull** — the subagent reads artifacts and source itself, off disk.
- **Return** — a structured summary, not a transcript.

### The invariant

**The manager ingests no source code and no raw build output.** It reads JSON
from `scripts/tools/*` and subagent summaries.

The manager persists across the whole run while subagent contexts are discarded
after each report. A manager that pulls compile logs and Java files into its own
context runs out partway through and takes the run with it. Everything expensive
happens somewhere disposable.

Size budgets for briefs, summaries, and total manager context are **measured
after the first real run**, not guessed at in advance. Record them here once
there are numbers. Until then the invariant above is the rule.
<!-- /generic -->

## Schema

`state.py` merges defaults on every read, so a file written by an older version
keeps all its fields and gains the new ones. Legacy keys (`autonomous`,
`retry_count`, `errors_history`, `failed_layers`) are preserved untouched.

```jsonc
{
  "current_step": "research | architecture | initialize | transform_validate | verify | done",
  "mode": "collapsed | full",          // from inventory; <20 files -> collapsed

  "initialize": { "status", "pom_generated", "application_java_generated",
                  "application_properties_generated", "error" },

  "research": { "status", "captured_at", "artifact": ".migration/research.md" },

  "architecture_review": {
    "status": "pending | approved | revise",
    "decisions": ".migration/decisions.md",
    "layer_overrides": ".migration/layer-overrides.json",  // optional, see below
    "no_migration": ["Module.java"],   // subtracted by verify.py and signature_diff.py
    "concerns": []
  },

  "source_inventory": { "captured_at", "play_java_root", "total_java_files", "by_layer" },

  "layers": {
    "<layer>": { "status", "files_migrated", "files_failed",
                 "validate_iteration", "last_error_count", "failure_reason" }
  },

  "qa_findings": [ { "id": "F-001", "layer", "file", "tier": "T1|T2|T3|T4|T5",
                     "severity": "blocker | major | minor",
                     "category", "evidence", "suggested_fix",
                     "status": "open | fixed | accepted", "created_at" } ],

  "endpoint_verification": { "status", "checked_at", "probes_compared",
                             "not_captured_after", "artifact": ".migration/endpoint-diff.json" },

  "attempts": { "<layer>": { "count", "error_signatures": [], "last_findings": [] } },

  "gates": { "mode": "milestone | strict",
             "architecture": { "human": "approved", "at": "..." },
             "layer.<name>": { "human": "pending | approved", "at": "..." } },

  "commits": { "<layer>": "<sha>" },

  "migration_verification": { "status", "checked_at", "play_java_total",
                              "spring_java_total", "excluded_from_baseline",
                              "layer_comparison", "notes" }
}
```

### `qa_findings` is the feedback channel

QA never fixes; it emits findings. The manager attaches finding IDs to dev's next
dispatch, so a fix arrives with evidence — "24 statements in Play, 1 in Spring" —
rather than a raw error dump. That is what makes the loop self-correcting instead
of a retry.

`no_migration` is load-bearing in the other direction: without it, Play-only glue
like `Module.java` shows as a permanent shortfall on every run, and a check that
always complains is a check nobody reads.

### `layer_overrides` corrects classification, not completeness

`.migration/layer-overrides.json` is optional and human-authored, drafted at
Gate 1 when inventory's `classification_smell` flags a high `other_pct` or a
recurring unmapped directory — a Play repo that doesn't use the conventional
`controllers/`/`service/`/`models/`/`db/`/`repositories/`/`dao/` segment names.
Both exact paths and directory prefixes are allowed; exact always wins, longest
prefix wins otherwise. Paths are relative to the Java source root — `app/` for
Play, the same form `LayerDetector`/`classify()` already receive everywhere
else in this module — not to the repo root:

```json
{
  "web/": "controller",
  "utils/PricingHelper.java": "service"
}
```

`layers.py`'s `load_overrides()` reads it and `classify()` applies it before
falling through to the segment rules. It is threaded into `gate.py` (T2 layer
scoping, cross-layer error attribution), `verify.py` (completeness counts), and
`signature_diff.py` (T2 standalone). No JAR/`LayerDetector` change is involved —
the dev-toolkit JAR has no override hook — so routing an override-mapped file to
its corrected layer happens in the dev skill: `transform --layer <corrected>`
runs for that file individually *before* the bulk `migrate-app` pass, which then
skips it because the output already exists.

## Gates

| Gate | When | Blocking |
|---|---|---|
| 1 — Architecture | after architect, before any code | yes |
| 2 — First layer | after `model` passes QA | yes (merges into 4 in `collapsed` mode) |
| 3 — Escalation | conditional, below | yes |
| 4 — Merge | after final QA | yes |

`gates.mode: milestone` (default) stops at the table above. `strict` stops after
every layer. On a 15-file project strict means six stops for about two files
each, which is why it is not the default.

**Gate 3 triggers** on any of:

- `attempts.<layer>.count` reaching 3
- any T2 `blocker` (`method-missing`)
- `git -C <play-repo> status --porcelain` non-empty

On trigger the manager writes `.migration/escalation-<layer>.md` — open findings,
the last three error-signature sets, what dev tried each time — and stops. One
file to read, not a transcript.

### Stuck loop vs progress

Compare `signatures` from `parse_mvn.py` across attempts. An identical set means
dev is going in circles. A *different* set, even a larger one, usually means a
fix landed and exposed errors beneath it.

The superseded `is_looping` heuristic treated any error-count growth beyond two
as a loop and killed the layer, so a fix that resolved an import and revealed
real downstream errors was indistinguishable from thrashing.

## Enforcement, and its limits

Role boundaries are enforced by subagent tool grants in `.claude/agents/`: dev
gets `Edit`/`Write`, the others do not.

Two honest gaps:

1. **Bash can write.** Researcher and QA need Bash to run `mvn` and the helper
   scripts, so the boundary is "does not write application source", not "cannot
   write at all". The `git status` guard on the Play repo is the real backstop.
2. **Cursor has no subagent isolation.** On that path the roles are advisory
   prose and one agent plays all of them, marking its own homework. The
   `git status` guard and script-owned verification are **mandatory** there, not
   belt-and-braces. Claude Code is where the model actually holds.
