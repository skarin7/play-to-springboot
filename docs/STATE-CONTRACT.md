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
is skipped; the completed lines before it are still good. That bounded damage is
the whole reason for NDJSON over one JSON array — truncate an array mid-write and
every entry in the file is unreadable, not just the last.

**Torn lines.** A killed writer leaves a line with no terminator, so a plain
append lands on that same line and welds two entries into something neither side
can parse. Dev is told to *start* each append with a newline, which keeps the
torn line isolated. Because that relies on an agent following an instruction, the
reader does not depend on it: `salvage_collided_line` recovers the trailing entry
from a welded line, accepting only a suffix that parses *and* carries an
`action`, so a nested object inside one well-formed entry is never mistaken for a
second entry. Without the salvage the counters drift *low* — a lost `migrated`
line reads as a layer with work still left.

Folding is idempotent. One journal covers a whole layer but the manager folds
after *every* batch and every re-dispatch, so a plain replay counts the same
lines again — a three-file controller layer re-dispatched twice reported nine
files migrated and six compile iterations. `journal_offsets` records how many
lines of each journal have been consumed, keyed by file name, and the next fold
starts from there. Two edges it handles: a truncated *final* line leaves the
offset short of itself, because the next append lands on that same line and
completes it; and a journal shorter than its recorded offset is replayed from
the top, since it cannot be the file that offset was measured against.

Journal lines:

```json
{"layer":"service","action":"migrated","count":3,"remaining":85}
{"layer":"service","action":"failed","file":"ContentService.java"}
{"layer":"service","action":"compiled","error_count":7}
```

`remaining` on a `"migrated"` line is the dev-toolkit's own `R` from
`migrate-app --batch-size N`, folded into `layers.<layer>.remaining_files`.
Dev runs exactly one `--batch-size` pass per dispatch — the manager, not dev,
decides whether `remaining_files > 0` warrants another dispatch for the same
layer. See "Batching a layer" below.

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
  // collapsed mode: artifact is ".migration/decisions.md" (its `## Survey` section) — no separate research.md is written

  "architecture_review": {
    "status": "pending | approved | revise",
    "decisions": ".migration/decisions.md",
    "layer_overrides": ".migration/layer-overrides.json",  // optional, see below
    "no_migration": ["Module.java"],   // subtracted by verify.py and signature_diff.py
    "concerns": [],
    // T2 suppressions approved at Gate 1, and the sha of the file as approved.
    // gate.py re-hashes every run and reports exemptions.modified_after_gate.
    "exemptions": [ { "class", "method", "replacement", "reason" } ],
    "exemptions_sha256": null,
    "exemptions_modified_after_gate": false
  },

  // What this migration does not translate. Seeded by inventory.py, confirmed
  // by the architect, rendered in the report. Views are invisible to every
  // *.java tool, so without this block an exclusion is indistinguishable from
  // an omission.
  "out_of_scope": { "captured_at", "policy": "left-in-place", "total_files",
                    "categories": { "twirl_templates": { "count", "samples" },
                                    "scala_sources": {}, "static_assets": {},
                                    "i18n_messages": {} } },

  // Scope declared at launch, from the skill's flags. Pre-declared because a
  // message sent while a subagent runs never reaches it.
  "run_config": { "skip_t5", "skip_tests", "no_boot", "mode_override",
                  "max_dispatches", "assets_policy": "skip | require",
                  "raw_arguments" },

  "source_inventory": { "captured_at", "play_java_root", "total_java_files", "by_layer" },

  "layers": {
    "<layer>": { "status", "files_migrated", "files_failed",
                 "validate_iteration", "last_error_count", "failure_reason",
                 "remaining_files",   // R from the last batch's migrate-app run; null until started
                 "batches_completed" }
  },

  "qa_findings": [ { "id": "F-001", "layer", "file", "tier": "T1|T2|T3|T4|T5",
                     "severity": "blocker | major | minor",
                     "category", "evidence", "suggested_fix",
                     "status": "open | fixed | accepted", "created_at" } ],

  "endpoint_verification": { "status", "checked_at", "probes_compared",
                             "not_captured_after", "artifact": ".migration/endpoint-diff.json" },

  "attempts": { "<layer>": { "count", "error_signatures": [], "last_findings": [] } },
                          // count moves only via state.py bump-attempt

  "journal_offsets": { "<layer>-dev.ndjson": 12 },  // lines already folded, so a
                                                    // re-fold does not recount them

  "gates": { "mode": "milestone | strict",  // legacy; unread now that only the
                                             // architecture gate blocks — see "Gates" below
             "architecture": { "human": "approved", "at": "..." },
             "layer.<name>": { "human": "pending | approved", "at": "..." } },

  "commits": { "<layer>": [ { "batch": 1, "sha": "<sha>" } ] },

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

Exactly one gate blocks a run. The earlier design also stopped after the
first layer and again before merge; both were dropped so that, once the
architecture is approved, the whole pipeline runs unattended through to the
final report — the trade being that a human reviews the outcome afterward via
the chat summary and `report.html` instead of being asked to approve
mid-run.

**The escalation trigger** — a layer's per-batch retry budget exhausted —
still fires on any of:

- `attempts.<layer>.count` reaching 3
- any T2 `blocker` (`method-missing`)

`attempts.<layer>.count` is scoped to the layer's **current batch**, not the
whole layer — it resets to 0 every time a batch's gate passes. See "Batching a
layer" below.

The counter moves only through `state.py bump-attempt --layer <layer>`, called
once per re-dispatch, and is zeroed by the same command with `--reset`. Nothing
else writes it: while it was documented but unwritten the trigger never fired,
and a controller layer ran six gate iterations with `count` still reading 0.
`bump-attempt` prints the new count so the manager can compare it against 3
without re-reading state.

On trigger the manager writes `.migration/escalation-<layer>.md` — the batch
index and file range, open findings, the last three error-signature sets, what
dev tried each time — same as before. What changed: it no longer stops the
run. The layer is recorded in `failed_layers` and the manager moves on to the
next layer in dependency order. `report.html` and the end-of-run chat summary
are where a human reads the escalation note back, not something they respond
to in the moment.

Two conditions are **not** softened this way, because they are integrity
violations rather than a layer running out of retries, and still halt the
whole run:

- `git -C <play-repo> status --porcelain` non-empty (dev touched read-only
  Play source)
- dev reporting it cannot proceed without changing the Play repo

### Batching a layer

Layer sizes are badly skewed in a real Play app — `model` might be 5 files,
`service` 100+. Treating a whole layer as one dev↔gate↔commit unit loses
fail-fast and blast-radius control exactly where it matters most: on the big
layers. So the per-layer loop is actually a per-**batch** loop
(`batch_size` from `workspace.yaml`): dev runs one `migrate-app --batch-size
N` pass per dispatch, the manager gates and commits that batch alone, and
loops back for the next batch while `layers.<layer>.remaining_files > 0`. A
layer whose file count is ≤ `batch_size` still finishes in a single batch —
this is not a special case, just the loop terminating after one iteration.

Because `signature_diff.py`'s T2 tier treats a Play class with no Spring
counterpart yet as `classes_absent_from_spring` (not a finding), gating a
layer that is only partially migrated is safe by construction — later
batches' files simply haven't landed yet and are not reported as missing.

### Stuck loop vs progress

Compare `signatures` from `parse_mvn.py` across attempts. An identical set means
dev is going in circles. A *different* set, even a larger one, usually means a
fix landed and exposed errors beneath it.

The superseded `is_looping` heuristic treated any error-count growth beyond two
as a loop and killed the layer, so a fix that resolved an import and revealed
real downstream errors was indistinguishable from thrashing.

## Enforcement, and its limits

Role boundaries are enforced by subagent tool grants in this plugin's
`agents/` directory: dev gets `Edit`/`Write`, the others do not.

One honest gap: **Bash can write.** Researcher and QA need Bash to run `mvn`
and the helper scripts, so the boundary is "does not write application
source", not "cannot write at all". The Play-repo guard is the real backstop.

That guard is `scripts/tools/guard.py`, and it replaced a prose rule that never
worked. The rule was "non-empty `git status --porcelain` means tampered" — but a
Play repo that is not a git repository makes git exit 128 with **empty stdout**,
which read as clean forever. The guard now:

- picks `git` mode only when the Play repo is its own repository root, and a
  **sha256 manifest** otherwise (which also catches gitignored files, and does
  not create a nested repo inside someone's checkout);
- returns an explicit `clean` | `tampered` | `error`, with **no path where an
  empty result means clean** — a missing baseline is `error`;
- exits 0 / 2 / 3 respectively, and `gate.py` runs it before T1 and
  short-circuits to `"status": "halt"` with exit 4 on anything but `clean`.

`error` halts exactly like `tampered`. A guard that cannot run is not a guard
that passed. The plugin's `PreToolUse` hook (see `docs/PERMISSIONS.md`) denies
Play-repo writes before they happen, which is prevention rather than detection —
the guard remains the check that proves it held.
