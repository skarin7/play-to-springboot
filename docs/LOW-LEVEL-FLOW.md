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

## Phase 1 — Inventory & mode

| | |
|---|---|
| Action | count Play files, classify by layer, check dev-toolkit JAR freshness |
| Tool | `python3 scripts/tools/inventory.py --play-repo <play>` |
| Stored | `source_inventory`, `mode` — via `state.py set` |
| Subagent | none — manager runs this itself |

`mode` = `collapsed` if under 20 Play files (researcher + architect merge into
one dispatch, Gate 2 and Gate 4 merge into one review) else `full`. A stale JAR
stops the run here until refreshed from `lib/`.

`classification_smell.other_pct` above 15% (or a recurring directory name in
`common_unmapped_dirs`) is surfaced to the human before Gate 1, so the
architect can draft `.migration/layer-overrides.json` — see Phase 3.

## Phase 2 — Research

| | |
|---|---|
| Action | read Play repo: deps, config, routes, code patterns |
| Tool | none scripted — subagent reads source directly |
| Stored | artifact `.migration/research.md`; `research.status = done` |
| Subagent | `play-spring-researcher` |

Manager reads only the ~30-line return summary, never the artifact itself.

## Phase 3 — Architecture → GATE 1

| | |
|---|---|
| Action | decide dependency map, config map, idiom choices, no-migration list, layer overrides if flagged |
| Tool | none scripted |
| Stored | artifacts `.migration/decisions.md`, optional `.migration/layer-overrides.json`; `architecture_review.{status,no_migration,layer_overrides,concerns}` |
| Subagent | `play-spring-architect` |

Manager stops, presents decisions to the human. `state.py gate --name
architecture --value approved` records `gates.architecture`. Not approved →
back to architect with the human's feedback (free text, no structured field for
this).

## Phase 4 — Init + empty-project check

| | |
|---|---|
| Action | generate `pom.xml`, `Application.java`, `application.properties`; prove dependencies resolve before any code exists |
| Tool | dev writes files from `decisions.md`; manager runs `gate.py --layer init --tiers T1` |
| Stored | `initialize.{status,pom_generated,application_java_generated,application_properties_generated}` |
| Subagent | `play-spring-dev` (generation only) |

`dependency_errors` here means the architect's map is wrong, not dev's code —
routes back to architect.

## Phase 5 — Per-layer loop

Order: **model → repository → manager → service → controller → other**. Repeat
5a–5f for each layer.

### 5a. Transform + compile

| | |
|---|---|
| Action | route any `.migration/layer-overrides.json` entries for this layer individually first, migrate the rest, compile, fix until clean or an honest blocker |
| Tool | `java -jar dev-toolkit-1.0.0.jar transform --input <play-file> --output <spring-file> --layer <layer>` per override entry, then `migrate-app --layer <layer> --target <spring>` (skips files whose output already exists), then `mvn compile` — all run by dev itself |
| Stored | append-only `.migration/journal/<layer>-dev.ndjson`; folded in via `state.py fold-journal` |
| Subagent | `play-spring-dev` |

Override routing exists because the JAR's `LayerDetector` has no override hook —
`migrate-app` always auto-detects. Transforming the override-mapped file first,
at its corrected layer, creates the output file so the bulk pass's "skip if
output exists" check naturally leaves it alone when it later reaches the file's
*original* auto-detected layer.

### 5b. Gate

| | |
|---|---|
| Action | re-verify T1 + T2 (T3 too once `layer == controller`) — dev's compile claim is not evidence |
| Tool | `python3 scripts/tools/gate.py --layer <layer>` (runs `mvn compile` → `parse_mvn.py`, dev-toolkit `signature` → `signature_diff.py`, and `routes.py` at controller) |
| Stored | `layers.<layer>`, `qa_findings`, `attempts.<layer>` — via `state.py set` / `add-finding` |
| Subagent | none — manager runs the script itself |

### 5c. Act on verdict

- `passed` → `git commit`, SHA recorded in `commits.<layer>`, `layers.<layer>.status = done`.
- `failed` / `needs_review`, `needs_agent: false` → `add-finding`, re-dispatch **dev** with finding IDs attached.
- `needs_agent: true` → dispatch **`play-spring-qa`** with the gate output path and `agent_reason` (cross-layer error attribution, unparseable build tail, unparseable T2 file, or a tier returning `error`).

### 5d. Play-repo guard

Every dispatch cycle: `git -C <play> status --porcelain`. Non-empty means dev
touched Play source → Gate 3 escalation, never silently continued.

### 5e. Gate 2 — first layer only (`model`)

| | |
|---|---|
| Action | human reviews the ~3 model files before the idiom repeats across every later layer |
| Tool | none scripted — manager presents files in chat |
| Stored | `state.py gate --name layer.model --value approved\|rejected` → `gates["layer.model"]` |
| Subagent | none for the review itself |

Approved → continue to `repository`. Rejected → `git reset` to the pre-model
commit, re-dispatch **architect** with the human's objection (free text), Gate 1
fires again, dev regenerates `model`, Gate 2 fires again.

Layers 2 through 6 (`repository` … `other`) do **not** stop by default. Set
`gates.mode: strict` to force a stop after every layer.

### 5f. Gate 3 — escalation (any layer, any time)

Fires when `attempts.<layer>.count` reaches 3, or a T2 `blocker` lands, or the
Play-repo guard trips. Manager writes `.migration/escalation-<layer>.md` (open
findings, last 3 error-signature sets, what dev tried each time) and stops. No
subagent, no tool beyond the write.

## Phase 6 — Final gate → GATE 4

| | |
|---|---|
| Action | full-tree structural + route + test check, then live endpoint diff |
| Tool | `gate.py --final` (full-tree T2 + T3 + T4), `verify.py` (completeness), then QA-driven `endpoint_diff.py capture` / `diff` (T5, both apps booted) |
| Stored | `migration_verification`, `endpoint_verification` |
| Subagent | `play-spring-qa` — the only tier that is always an agent dispatch, never a bare script call |

Manager presents both the `gate.py --final` output and the T5 diff together.
`state.py gate --name merge --value approved\|rejected`. Rejected → back into
the layer loop, targeted at whichever layer the QA findings implicate — not a
restart from scratch. Approved → done, branch `migration/<play-repo-name>`
ready to merge.

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
