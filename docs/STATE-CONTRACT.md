# State contract

How the roles coordinate. Read this before changing `migration-status.json` or
adding a role.

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
    "no_migration": ["Module.java"],   // subtracted by verify.py and signature_diff.py
    "concerns": []
  },

  "source_inventory": { "captured_at", "play_java_root", "total_java_files", "by_layer" },

  "layers": {
    "<layer>": { "status", "files_migrated", "files_failed",
                 "validate_iteration", "last_error_count", "failure_reason" }
  },

  "qa_findings": [ { "id": "F-001", "layer", "file", "tier": "T1|T2|T3|T4",
                     "severity": "blocker | major | minor",
                     "category", "evidence", "suggested_fix",
                     "status": "open | fixed | accepted", "created_at" } ],

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
