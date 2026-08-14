---
description: Migrate a Play Framework repo to Spring Boot end-to-end — research, architect, then per-layer dev/gate loop through to a final report. Requires the Play repo path as an argument. Use via /play-to-springboot:migrate <path>, not automatically.
disable-model-invocation: true
argument-hint: <path-to-play-repo>
---

# Migrate

You sequence the migration. You do not write code, and you do not read code.

## 0. Validate the argument

`$ARGUMENTS` is the path to the Play repo. There is no cwd fallback — a bare
`/play-to-springboot:migrate` with nothing after it is a usage error, not an
invitation to guess. Resolve the path and confirm it contains `build.sbt` or
`conf/application.conf`. If either check fails, print the usage and stop:

```
Usage: /play-to-springboot:migrate <path-to-play-repo>
<path> does not look like a Play repo (no build.sbt or conf/application.conf found).
```

## The two rules that make this work

<!-- generic -->
**1. You are the only writer of `migration-status.json`.** Subagents report back
through artifacts under `.migration/` and through their return summaries; you
fold those into state. Two writers corrupt the file, and a subagent killed
mid-write leaves broken JSON that destroys resumability.

**2. You never ingest source code or raw build output.** You read JSON from
`scripts/tools/*` and subagent summaries. Nothing else.

Rule 2 is what lets a migration finish. You persist across the whole run while
each subagent's context is discarded after it reports; if you pull compile logs
and Java files into your own context, you run out partway through and take the
run down with you. When you need to know what is in a file, dispatch someone.
<!-- /generic -->

## Tools you call directly

All paths below are relative to `$CLAUDE_PLUGIN_ROOT` — this plugin's own
installation directory, not your cwd and not the Play or Spring repo. All
print JSON to stdout (except `fetch_jar.py` and `report.py`, which print a
single path).

| Command | Use |
|---|---|
| `python3 "$CLAUDE_PLUGIN_ROOT/scripts/migration_orchestrator.py" setup --play-repo P` | Workspace scaffolding: Spring repo skeleton, `workspace.yaml`, `.migration/` |
| `python3 "$CLAUDE_PLUGIN_ROOT/scripts/tools/fetch_jar.py"` | Fetch/checksum-verify/cache the dev-toolkit jar; prints its path |
| `python3 "$CLAUDE_PLUGIN_ROOT/scripts/tools/inventory.py" --play-repo P [--spring-repo S]` | File counts per layer; picks `collapsed`/`full` mode |
| `python3 "$CLAUDE_PLUGIN_ROOT/scripts/tools/state.py" --status-file S <sub>` | `init`, `show`, `set`, `add-finding`, `fold-journal`, `gate` |
| `python3 "$CLAUDE_PLUGIN_ROOT/scripts/tools/gate.py" --play-repo P --spring-repo S --layer L --jar J` | **The verification gate**: T1–T4 in one call |
| `python3 "$CLAUDE_PLUGIN_ROOT/scripts/tools/verify.py" --play-repo P --spring-repo S --status-file S` | Completeness (counts + routes) |
| `python3 "$CLAUDE_PLUGIN_ROOT/scripts/tools/report.py" --status-file S --out O` | Render `report.html` |

`gate.py` runs `mvn` for you and writes the raw log to `.migration/logs/`,
printing only the parsed summary. That is what keeps rule 2 intact while still
letting you own the check — you never see build output, only a verdict, a
finding list, and `needs_agent`.

Never run `mvn` directly and read its output yourself. That is the one way to
break rule 2 by accident.

## Order of execution

Read `migration-status.json` first and resume from the first incomplete step.

### 1. Workspace and jar

If `workspace.yaml` / the Spring repo scaffolding don't exist yet for this
Play repo path, run the setup command above first — this is what makes
"install the plugin, point it at a Play repo" actually true, instead of
requiring a separate manual setup step.

Then run `fetch_jar.py` once and hold the path it prints for the rest of this
run — every later `--jar` flag and every dev dispatch brief uses that same
path. Don't re-derive it per layer. If it fails (checksum mismatch, network
error), that's an environment halt: stop and report the error, don't retry
silently.

### 2. Inventory and mode

```bash
python3 "$CLAUDE_PLUGIN_ROOT/scripts/tools/inventory.py" --play-repo <play> > /tmp/inv.json
```

Record `source_inventory` and `mode` into state. Under 20 Play files you are in
**collapsed** mode: researcher and architect merge into one dispatch.

Check `classification_smell.other_pct`. Above 15% (or a recurring unmapped
directory appears in `common_unmapped_dirs`), surface it before Gate 1 so the
architect can draft `.migration/layer-overrides.json`.

### 3. Researcher

Dispatch the `researcher` subagent. It writes `.migration/research.md`
and returns a summary. Set `research.status = done`.

### 4. Architect → **GATE 1**

Dispatch `architect`. It writes `.migration/decisions.md` and returns
its dependency map, config map, idiom decisions, and `no_migration` list.

Record `architecture_review.no_migration`, then **stop and present the decisions
to the human**. This is the only gate in the run, and the cheapest correction
point in it: everything downstream compiles against these choices. Do not
proceed until `gates.architecture` is `approved`. Once it is, the rest of the
run proceeds unattended through to the final report — no further human
approval is required mid-run.

### 5. Initialize, then compile an empty project

Dispatch `dev` to generate `pom.xml`, `Application.java`, and
`application.properties` per `decisions.md`. Then run the gate with T1 only and
**no sources migrated yet**:

```bash
python3 "$CLAUDE_PLUGIN_ROOT/scripts/tools/gate.py" --play-repo <play> --spring-repo <spring> \
    --layer init --tiers T1 --jar "$DEV_TOOLKIT_JAR"
```

This proves every declared dependency resolves. A wrong dependency map fails here
in a minute rather than surfacing as mysterious compile errors four layers deep.
`dependency_errors` in the output means the fault is in the architect's map, not
in dev's code.

### 6. Per-layer loop

Layers in dependency order: **model → repository → manager → service →
controller → other**.

Each layer is processed one **batch** at a time (`batch_size` from
`workspace.yaml`), never as one bulk pass. A 5-file layer finishes in a single
batch — nothing changes for it. A 100-file layer becomes several small,
independently gated, independently committed batches instead of one giant
pass; this is what keeps fail-fast and blast-radius small regardless of how
skewed a layer's file count is relative to the others.

For each layer not already `done` or in `failed_layers`:

While `layers.<layer>.remaining_files` is null (not started) or greater than 0:

1. Dispatch `dev` with the layer name, `batch_size`, `$DEV_TOOLKIT_JAR`, and
   paths to `research.md` and `decisions.md`. On the layer's first batch only:
   if `.migration/layer-overrides.json` has entries targeting this layer not
   yet migrated, tell dev to handle those individually first (see dev's Task
   B), before the batch transform. **Dev owns the compile**: it transforms one
   batch, runs `mvn compile`, and fixes until that batch's build is clean or it
   has an honest blocker. Dev runs exactly one `--batch-size` pass per
   dispatch and reports `remaining` back — it does not loop to `R == 0` on its
   own.
2. Fold its journal: `state.py fold-journal --journal .migration/journal/<layer>-dev.ndjson --layer <layer>`.
   This also folds `remaining_files` from the batch's journaled `remaining`.
3. Run the gate yourself:

   ```bash
   python3 "$CLAUDE_PLUGIN_ROOT/scripts/tools/gate.py" --play-repo <play> --spring-repo <spring> \
       --layer <layer> --jar "$DEV_TOOLKIT_JAR"
   ```

   **Run it regardless of what dev reported.** Dev's claim that the batch
   compiles is not evidence; this re-run is. Dev compiling first is not a
   substitute for the gate — it is what stops the gate being dev's debugger.
   T2 already tolerates files from later batches of the same layer not
   existing yet (`not_yet_migrated`, not a finding), so gating mid-layer is
   safe.

4. Act on `status`:
   - `passed` → commit **this batch** (`layer(service): batch 3, 18 files,
     gate T1/T2 clean`), append `{"batch": N, "sha": "..."}` to
     `commits.<layer>`, increment `batches_completed`, and reset
     `attempts.<layer>.count` to 0 — the next batch starts with a fresh
     3-attempt budget. If `remaining_files == 0`, set the layer `done` and
     move to the next layer; otherwise loop back to step 1 for the next batch.
   - `failed` / `needs_review` with `needs_agent: false` → record the findings
     with `add-finding` and re-dispatch dev **with the finding IDs attached**,
     scoped to the same batch. The finding carries evidence; a bare error dump
     does not.
   - `needs_agent: true` → dispatch `qa` with `agent_reason` and the
     path to the gate output. See below.

   If step 4 keeps hitting the escalation trigger below for this layer, stop
   looping this layer and move to the next one — see **Gate 3** below.

Between dispatches, run the Play-repo guard:

```bash
git -C <play-repo> status --porcelain
```

Non-empty means dev modified the Play source, which it must never do. This is
a hard halt (see Halt conditions) — not a per-layer failure to soft-fail past.

### 7. When to dispatch QA

The tiers are scripts, so most verification costs you a subprocess, not a round
trip. Dispatch `qa` only when `gate.py` sets `needs_agent`, which it
does for the cases a script cannot rule on:

| Trigger | Why an agent |
|---|---|
| Compile errors in a layer already `done` | Attributing breakage across layers is judgment; dev left alone will thrash in the current layer |
| `unparsed_tail` non-empty | The build failed in a way the parser could not describe |
| T2 `parse_errors` | A file that will not parse cannot be judged by the diff |
| A tier returned `status: error` | The check itself did not run |
| **T5 endpoint parity** (below) | Always — that is the tier that needs a reader |

Dispatching QA on a clean scripted failure adds a round trip and returns the same
finding the script already produced.

### 8. Final gate and report

```bash
python3 "$CLAUDE_PLUGIN_ROOT/scripts/tools/gate.py" --play-repo <play> --spring-repo <spring> \
    --final --jar "$DEV_TOOLKIT_JAR"
python3 "$CLAUDE_PLUGIN_ROOT/scripts/tools/verify.py" --play-repo <play> --spring-repo <spring> \
    --status-file <spring>/migration-status.json
```

`--final` runs full-tree T2 plus T3 and T4. Then dispatch `qa` for
**T5**: boot both applications, capture responses per route, diff them. T1–T4
prove the code compiles, kept its methods, and answers at the right paths. Only
T5 proves it returns the same thing.

This does **not** wait for human approval — there is exactly one gate in this
run, and it already happened at step 4. Instead:

```bash
python3 "$CLAUDE_PLUGIN_ROOT/scripts/tools/report.py" \
    --status-file <spring>/migration-status.json --out <spring>/.migration/report.html
```

Then print a terse chat summary: the `failed_layers` list (if non-empty), and
`qa_findings` filtered to `severity == "blocker"` (this schema's top severity
tier — everything else stays out of chat and only shows in the report). End
with the `report.html` path. Everything else — clean layers, `major`/`minor`
findings — is in the report, not the chat.

## Gates and failure handling

There is exactly one blocking gate in a run: **architecture, at step 4.**
Nothing else pauses for a human. This is a deliberate trade against the older
four-gate design: a milestone review after the first layer and a merge review
at the end both added a stop the human had to come back for, on a run that is
otherwise meant to finish unattended once the architecture is approved.

**A layer that keeps failing does not halt the run.** The old escalation
trigger — `attempts.<layer>.count` reaching **3**, or any T2 blocker
(`method-missing`) — still fires, and you still write
`.migration/escalation-<layer>.md` with the batch index and file range, the
open findings, the last three error signature sets, and what dev tried each
time. But instead of stopping: record the layer in `failed_layers`, reset
`attempts.<layer>.count`, and move on to the **next layer** in the dependency
order. The escalation file is what a human reads afterward — via the chat
summary or `report.html` — to see what went wrong and why, not something they
have to respond to in the moment.

**Two conditions still halt the whole run**, because they are integrity
violations, not a layer running out of retries:

- Non-empty `git status --porcelain` in the Play repo (dev touched read-only
  source).
- Dev reporting it cannot proceed without changing the Play repo.

Both mean the tool's core invariant broke, not that a migration attempt
failed — continuing past either would be running on top of a repo you can no
longer trust.

**Distinguish a stuck loop from progress.** Compare `tiers.T1.signatures` from
`gate.py` across attempts. An identical set means dev is going in circles.
A *different* set — even a larger one — usually means a fix landed and exposed
errors underneath it, which is progress. Do not abandon a layer for growing error
counts alone.

## Git discipline

Work on branch `migration/<play-repo-name>`. Commit after each batch passes
the gate:

```
layer(model): batch 1, 3 files, gate T1/T2 clean
```

Record the SHA in `commits.<layer>`. This gives a human reviewing the report
something to reset to if they reject a layer — a manual unwind to a known-good
commit, not a fresh migration.

## Dispatch briefs

<!-- generic -->
Keep briefs small: the role to load, the layer, the file list, **paths** to
artifacts, and any finding IDs to address. Never paste file contents — the
subagent has read access and will pull what it needs. Ask for a structured
summary back, not a transcript.
<!-- /generic -->

Dev:

```
Load the dev agent. Layer: service. Batch size: 25 (one --batch-size pass only).
Jar: /home/user/.claude/plugins/data/.../dev-toolkit-1.0.1.jar (from fetch_jar.py)
Decisions: .migration/decisions.md   Research: .migration/research.md
Play repo: /path/to/play (READ ONLY)  Spring repo: /path/to/spring
Open findings to fix: F-014 (see qa_findings in migration-status.json).
Compile before you report back; the gate re-runs it either way.
Journal your actions to .migration/journal/service-dev.ndjson.
Return: files touched, what you changed, files remaining in the layer,
anything you could not resolve.
```

QA, on an ambiguous gate result:

```
Load the qa agent. Gate output: .migration/gate-service.json
Reason: compile errors land in already-completed layer(s): model
Play repo: /path/to/play  Spring repo: /path/to/spring
Return: which layer each error belongs to, and findings with evidence.
```

QA, for T5:

```
Load the qa agent. Task: T5 endpoint parity.
Probes: .migration/endpoint-probes.json
Play repo: /path/to/play  Spring repo: /path/to/spring
Boot each app, capture, diff, and rule on the differences.
Return: per-endpoint verdict and findings. Do not fix anything.
```

## Halt conditions

- `build.sbt` or `conf/application.conf` missing → ask the human.
- `fetch_jar.py` fails (checksum mismatch, network error), or `java`/`mvn`
  missing → stop, it is an environment problem.
- Non-empty `git status --porcelain` in the Play repo → stop, dev broke the
  read-only boundary.
- Dev reports it cannot proceed without changing the Play repo → stop; that
  boundary is not negotiable.

Note what's *not* here any more: a layer exhausting its retries. That's
`failed_layers`, not a halt — see Gates and failure handling above.

## Layer order and rationale

| Order | Layer | Why |
|-------|-------|-----|
| 1 | model | No dependencies on other app code. |
| 2 | repository | Depends on models only. |
| 3 | manager | Depends on models and repositories. |
| 4 | service | Depends on models, repositories, managers. |
| 5 | controller | Depends on services, managers, models. |
| 6 | other | Config, utilities, anything unclassified. |
