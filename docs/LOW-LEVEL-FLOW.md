# Low-level run trace

A single migration run, traced step by step, from the moment the manager skill
is triggered on a fresh (post-setup) repo through merge. Each step: what
happens, which tool ran it, where the result is stored, which subagent (if any)
was dispatched.

For the diagrams see [FLOW.md](FLOW.md); for the state schema and gate rules see
[STATE-CONTRACT.md](STATE-CONTRACT.md); for the operator-facing walkthrough see
[ORCHESTRATION.md](ORCHESTRATION.md). This file exists to make the mapping
between steps, tools, state fields, and subagents explicit in one place.

`<play>` / `<spring>` = repo paths. State file = `<spring>/migration-status.json`,
read/written only through `scripts/tools/state.py`.

---

## Phase 0 — Workspace and jar

| | |
|---|---|
| Action | scaffold the Spring repo + `workspace.yaml` if not already done; fetch and checksum-verify the dev-toolkit jar |
| Tool | `scripts/migration_orchestrator.py setup`, `scripts/tools/fetch_jar.py` |
| Stored | nothing in `migration-status.json` — `workspace.yaml` on disk, jar cached under `$CLAUDE_PLUGIN_DATA` |
| Subagent | none — the migrate skill runs both itself |

`fetch_jar.py` prints the jar's resolved path once; that path is held for the
rest of the run and passed to every `gate.py --jar` call and every dev
dispatch. A checksum mismatch or network error here is a hard stop —
environment problem, not a migration problem.

## Phase 1 — Inventory & mode

| | |
|---|---|
| Action | count Play files, classify by layer |
| Tool | `python3 scripts/tools/inventory.py --play-repo <play>` |
| Stored | `source_inventory`, `mode` — via `state.py set` |
| Subagent | none — the migrate skill runs this itself |

`mode` = `collapsed` if under 20 Play files (researcher + architect merge into
one dispatch) else `full`.

`classification_smell.other_pct` above 15% (or a recurring directory name in
`common_unmapped_dirs`) is surfaced to the human before Gate 1, so the
architect can draft `.migration/layer-overrides.json` — see Phase 3.

## Phase 2 — Research

| | |
|---|---|
| Action | read Play repo: deps, config, routes, code patterns |
| Tool | none scripted — subagent reads source directly |
| Stored | artifact `.migration/research.md`; `research.status = done` |
| Subagent | `researcher` |

The migrate skill reads only the ~30-line return summary, never the artifact
itself.

## Phase 3 — Architecture → GATE 1

| | |
|---|---|
| Action | decide dependency map, config map, idiom choices, no-migration list, layer overrides if flagged |
| Tool | none scripted |
| Stored | artifacts `.migration/decisions.md`, optional `.migration/layer-overrides.json`; `architecture_review.{status,no_migration,layer_overrides,concerns}` |
| Subagent | `architect` |

The migrate skill stops, presents decisions to the human. `state.py gate --name
architecture --value approved` records `gates.architecture`. Not approved →
back to architect with the human's feedback (free text, no structured field for
this). This is the only stop in the whole run — everything from here to the
final report proceeds unattended.

## Phase 4 — Init + empty-project check

| | |
|---|---|
| Action | generate `pom.xml`, `Application.java`, `application.properties`; prove dependencies resolve before any code exists |
| Tool | dev writes files from `decisions.md`; migrate skill runs `gate.py --layer init --tiers T1 --jar <jar>` |
| Stored | `initialize.{status,pom_generated,application_java_generated,application_properties_generated}` |
| Subagent | `dev` (generation only) |

`dependency_errors` here means the architect's map is wrong, not dev's code —
routes back to architect.

## Phase 5 — Per-layer loop

Order: **model → repository → manager → service → controller → other**. Repeat
5a–5f for each **batch** within a layer (`batch_size` from `workspace.yaml`),
looping 5a–5g until `layers.<layer>.remaining_files == 0`. A layer under
`batch_size` files just runs the loop once — the batching is not a special
case, it is the only path. This is what keeps a 100-file `service` layer from
behaving differently than a 5-file `model` layer: same small gated unit either
way.

### 5a. Transform + compile (one batch)

| | |
|---|---|
| Action | on the layer's first batch, route any `.migration/layer-overrides.json` entries for this layer individually first; then migrate **one batch**, compile, fix until that batch is clean or an honest blocker |
| Tool | `java -jar <jar> transform --input <play-file> --output <spring-file> --layer <layer>` per override entry, then **one** `migrate-app --layer <layer> --batch-size <N> --target <spring>` call (skips files whose output already exists; dev does not loop this itself), then `mvn compile` — all run by dev itself, against the jar path `fetch_jar.py` resolved in Phase 0 |
| Stored | append-only `.migration/journal/<layer>-dev.ndjson`, including `remaining` (the CLI's `R`) on each `migrated` line; folded in via `state.py fold-journal` |
| Subagent | `dev` |

Override routing exists because the JAR's `LayerDetector` has no override hook —
`migrate-app` always auto-detects. Transforming the override-mapped file first,
at its corrected layer, creates the output file so the bulk pass's "skip if
output exists" check naturally leaves it alone when it later reaches the file's
*original* auto-detected layer.

### 5b. Gate

| | |
|---|---|
| Action | re-verify T1 + T2 (T3 too once `layer == controller`) for this batch — dev's compile claim is not evidence |
| Tool | `python3 scripts/tools/gate.py --layer <layer>` (runs `mvn compile` → `parse_mvn.py`, dev-toolkit `signature` → `signature_diff.py`, and `routes.py` at controller) |
| Stored | `layers.<layer>` (incl. `remaining_files`), `qa_findings`, `attempts.<layer>` — via `state.py set` / `add-finding` / `bump-attempt` |
| Subagent | none — manager runs the script itself |

T2's `classes_absent_from_spring` treats files from batches not yet migrated
as not-yet-migrated, not a finding — gating mid-layer is safe by construction.

### 5c. Act on verdict

- `passed` → `git commit` **this batch**, `{"batch": N, "sha": "..."}` appended
  to `commits.<layer>`, `attempts.<layer>.count` reset to 0 via
  `state.py bump-attempt --reset`, `batches_completed` incremented. If
  `remaining_files == 0`, `layers.<layer>.status = done`; otherwise continue to 5g.
- `failed` / `needs_review`, `needs_agent: false` → `add-finding`,
  `state.py bump-attempt --layer <layer> --signatures <T1 signatures>`, re-dispatch
  **dev** for the same batch with finding IDs attached. `bump-attempt` is the only
  thing that moves the counter the escalation trigger reads.
- `needs_agent: true` → dispatch **`qa`** with the gate output path and `agent_reason` (cross-layer error attribution, unparseable build tail, unparseable T2 file, or a tier returning `error`).

### 5d. Play-repo guard

`gate.py` runs `guard.py check` before T1 on every gate, and the manager can run
it directly between dispatches. It reports `clean` | `tampered` | `error`
(exit 0 / 2 / 3); anything but `clean` short-circuits the gate to
`"status": "halt"` with exit 4 — a hard halt (see 5f), not a per-layer failure.

`error` halts too. The predecessor of this check was `git status --porcelain`
with "non-empty means tampered", which passed silently forever on a Play repo
that was not a git repository: git exits 128 with empty stdout. Empty is not
evidence; only `clean` is.

There is no per-layer human review gate any more. Layer `model` finishing used
to stop the run for a review before the idiom repeated across every later
layer; that gate was dropped along with the merge gate, so all six layers
(`model` … `other`) run back-to-back with no stop between them.

### 5e. Escalation — a layer's retry budget runs out

Fires when `attempts.<layer>.count` (scoped to the **current batch**, reset on
every batch that passes — see 5c) reaches 3, or a T2 `blocker` lands. The
migrate skill writes `.migration/escalation-<layer>.md` (batch index and file
range, open findings, last 3 error-signature sets, what dev tried each time),
records the layer in `failed_layers`, resets `attempts.<layer>.count`, and
**moves on to the next layer** — it does not stop the run. A human reads the
escalation file afterward, via the chat summary or `report.html`.

### 5f. Hard halts — not the same as 5e

Two conditions still stop the whole run, because they mean the tool's
invariant broke rather than a migration attempt failing:

- the Play-repo guard (5d) returns anything but `clean` — `tampered` (dev wrote
  to read-only source) or `error` (the guard could not run)
- dev reports it cannot proceed without changing the Play repo

Neither writes an escalation file to read later; both need a human's
attention right away.

### 5g. Next batch or next layer

If the batch just committed leaves `layers.<layer>.remaining_files > 0`,
return to 5a for the next batch of the same layer (same `--batch-size`; the
CLI skips already-migrated files, so this is idempotent). Otherwise the layer
is `done` and the loop advances to the next layer in order.

## Phase 6 — Final gate and report

| | |
|---|---|
| Action | full-tree structural + route + test check, then live endpoint diff, then render the report |
| Tool | `gate.py --final` (full-tree T2 + T3 + T4), `verify.py` (completeness), QA-driven `endpoint_diff.py capture` / `diff` (T5, both apps booted), then `report.py` |
| Stored | `migration_verification`, `endpoint_verification` — `report.html` is derived, not stored in state |
| Subagent | `qa` — the only tier that is always an agent dispatch, never a bare script call |

This is not a gate — nothing here asks for approval. The migrate skill runs
`report.py` against the final state and prints a chat summary: `failed_layers`
and `blocker`-severity `qa_findings` only. Everything else, including every
layer that finished clean, is in `report.html`, not the chat. There's no
reject-and-loop-back step any more; a human who wants a layer redone re-runs
`/play-to-springboot:migrate` after fixing whatever the report pointed at,
which resumes from `migration-status.json` rather than restarting.

---

## Tier schedule inside the per-layer loop

| Tier | model | repository | manager | service | controller | other | final |
|---|---|---|---|---|---|---|---|
| T1 compile | v | v | v | v | v | v | v (full-tree) |
| T2 preservation | v (layer-only) | v | v | v | v | v | v (full-tree) |
| T3 route parity | – | – | – | – | v | – | v |
| T4 tests | – | – | – | – | – | – | v |
| T5 endpoint parity | – | – | – | – | – | – | v (QA dispatch) |

T3 joins only once `controller` exists — route parity is meaningless before
that. T5 needs a live, fully-booted app answering real HTTP requests, which no
single layer can provide on its own, so it never runs inside the loop.

## Tool vs. subagent, in one line

Every `gate.py` / `state.py` / `inventory.py` / `verify.py` call is the
manager's own deterministic work — no subagent involved. A subagent
(`researcher` / `architect` / `dev` / `qa`) is dispatched only for judgment or
code-writing: reading unfamiliar source, deciding a mapping, writing Java, or
interpreting an ambiguous gate result.
