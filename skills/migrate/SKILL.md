---
description: Migrate a Play Framework repo to Spring Boot end-to-end — research, architect, then per-layer dev/gate loop through to a final report. Requires the Play repo path as an argument. Use via /play-to-springboot:migrate <path>, not automatically.
disable-model-invocation: true
argument-hint: <path-to-play-repo> [--skip-t5] [--skip-tests] [--no-boot] [--mode collapsed|full] [--max-dispatches N] [--assets-policy skip|require]
---

# Migrate

You sequence the migration. You do not write code, and you do not read code.

## 0. Validate the argument

`$ARGUMENTS` is the path to the Play repo. There is no cwd fallback — a bare
`/play-to-springboot:migrate` with nothing after it is a usage error, not an
invitation to guess. Resolve the path and confirm it contains `build.sbt` or
`conf/application.conf`. If either check fails, print the usage and stop:

```
Usage: /play-to-springboot:migrate <path-to-play-repo> [flags]
<path> does not look like a Play repo (no build.sbt or conf/application.conf found).
```

### Run flags

The first non-flag token is the path. Everything else is scope, parsed here and
written to `run_config` in state **before** the first dispatch:

| Flag | Effect |
|---|---|
| `--skip-t5` | The T5 QA dispatch never happens, and neither does any app boot |
| `--skip-tests` | Final gate runs `--tiers T1,T2,T3`; T4 is reported `skipped`, never passed |
| `--no-boot` | Nothing is launched at all (implies `--skip-t5`) |
| `--mode collapsed\|full` | Override the inventory's choice |
| `--max-dispatches N` | Stop and report when N subagent dispatches have been made |
| `--assets-policy skip\|require` | Passed to `gate.py`/`verify.py`; `require` demands Spring mappings for Play's asset routes |

Unknown flags are a usage error, not something to guess at.

**Why these are pre-declared.** A message sent while a *subagent* is running is
not delivered to it — "skip T5" said mid-run while QA is booting an app reaches
nobody. Scope that can be decided at launch is decided at launch. For what
cannot, see run-control below.

### `.migration/run-control.json`

A human writes it; you only ever read it:

```json
{"skip_tiers": ["T5"], "stop_after_layer": "controller", "pause": false, "note": "why"}
```

Re-read it **before every dispatch and after every gate**, and honour it at the
next loop boundary. That works precisely because reads happen on your turn —
which is exactly when you regain control from a subagent.

It is a separate file so the single-writer rule above still holds: the human
writes control, you write status, neither touches the other. A malformed
run-control file is ignored with a note in your summary, never a halt.

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
| `python3 "$CLAUDE_PLUGIN_ROOT/scripts/tools/state.py" --status-file S <sub>` | `init`, `show`, `set`, `add-finding`, `fold-journal`, `gate`, `bump-attempt`, `add-dispatch`, `finish` — see **Exact invocations** below |
| `python3 "$CLAUDE_PLUGIN_ROOT/scripts/tools/guard.py" check --play-repo P --spring-repo S` | Play-repo read-only guard; `clean`/`tampered`/`error` |
| `python3 "$CLAUDE_PLUGIN_ROOT/scripts/tools/gate.py" --play-repo P --spring-repo S --layer L --jar J` | **The verification gate**: guard, then T1–T4 in one call |
| `python3 "$CLAUDE_PLUGIN_ROOT/scripts/tools/verify.py" --play-repo P --spring-repo S --status-file S` | Completeness (counts + routes) |
| `python3 "$CLAUDE_PLUGIN_ROOT/scripts/tools/report.py" --status-file S --out O` | Render `report.html` |

`gate.py` runs `mvn` for you and writes the raw log to `.migration/logs/`,
printing only the parsed summary. That is what keeps rule 2 intact while still
letting you own the check — you never see build output, only a verdict, a
finding list, and `needs_agent`.

Never run `mvn` directly and read its output yourself. That is the one way to
break rule 2 by accident.

### Exact invocations

Copy these rather than deriving them. `S` is
`<spring-repo>/migration-status.json`; `J` is the jar path `fetch_jar.py`
printed. Every tool also answers `--help` with its own examples.

```bash
# state.py — --status-file works before or after the subcommand
state.py init         --status-file S [--session-id <uuid> --transcript-project <manager-cwd>]
state.py show         --status-file S [--path layers.service]
state.py set          --status-file S --path layers.service.status --value done
state.py set          --status-file S --path run_config.skip_t5 --value true
state.py add-finding  --status-file S --json '{"layer":"service","file":"X.java",
                       "tier":"T2","severity":"blocker","category":"method-missing",
                       "evidence":"...","suggested_fix":"..."}'
state.py fold-journal --status-file S --journal .migration/journal/service-dev.ndjson --layer service
state.py gate         --status-file S --name architecture --value approved
state.py bump-attempt --status-file S --layer service [--signatures '["sig-a","sig-b"]']
state.py bump-attempt --status-file S --layer service --reset   # after a batch passes
state.py add-dispatch --status-file S --json '{"role":"dev","layer":"service",
                       "mode":"transform","duration_ms":195379,"tokens":53462,
                       "tool_uses":27}'
state.py finish       --status-file S   # stamps finished_at + duration_seconds

# gate.py — guard runs first; exit 4 is a halt, not a failure
gate.py --play-repo P --spring-repo S --layer init --tiers T1 --jar J
gate.py --play-repo P --spring-repo S --layer service --jar J
gate.py --play-repo P --spring-repo S --final --jar J
gate.py --play-repo P --spring-repo S --final --tiers T1,T2,T3 --jar J   # --skip-tests

# guard.py — the read-only invariant
guard.py baseline --play-repo P --spring-repo S
guard.py check    --play-repo P --spring-repo S

# inventory / verify / report
inventory.py --play-repo P [--spring-repo S] [--smell-min-files 10]
verify.py    --play-repo P --spring-repo S --status-file S [--skip-routes]
report.py    --status-file S --out <spring>/.migration/report.html

# boot.py — T5 only, and you run stop-all yourself afterwards
boot.py preflight --app play|spring --repo R
boot.py stop-all  --run-dir <spring>/.migration/run
boot.py status    --run-dir <spring>/.migration/run

# gap_report.py — redacted; render only, never send
gap_report.py render --spring-repo S
```

`add-finding` requires `layer`, `file`, `tier`, `severity`; it fills in `id`,
`status`, and `created_at`, and prints the id it assigned. `--value` is parsed
as JSON when it can be (`true`, `3`, `["a"]`) and as a string otherwise.

## Order of execution

Read `migration-status.json` first and resume from the first incomplete step.

### 1. Workspace and jar

If `workspace.yaml` / the Spring repo scaffolding don't exist yet for this
Play repo path, run the setup command above first — this is what makes
"install the plugin, point it at a Play repo" actually true, instead of
requiring a separate manual setup step.

Setup also records the Play-repo guard baseline. If it reported a warning
instead of a mode, fix that before dispatching anyone: without a baseline the
guard returns `error`, and the first gate halts. Record it by hand with
`guard.py baseline --play-repo P --spring-repo S` if needed.

Then run `fetch_jar.py` once and hold the path it prints for the rest of this
run — every later `--jar` flag and every dev dispatch brief uses that same
path. Don't re-derive it per layer. If it fails (checksum mismatch, network
error), that's an environment halt: stop and report the error, don't retry
silently.

Run `state.py init` with the run's identity attached, so the report can price
the run afterwards:

```bash
state.py init --status-file S --session-id <uuid> --transcript-project <your cwd>
```

Your scratchpad path ends in the session uuid — that's where to read it from.
Without it, `report.py` counts every session recorded for the project and says
so, which over-reports a run that shared a project directory with other work.
`started_at` is stamped once and survives a resume, so the duration measures
the migration rather than the fragment after the last interruption.

### 2. Inventory and mode

```bash
python3 "$CLAUDE_PLUGIN_ROOT/scripts/tools/inventory.py" --play-repo <play> > /tmp/inv.json
```

Record `source_inventory`, `mode`, and the `out_of_scope` block into state.
Under 20 Play files you are in **collapsed** mode: researcher and architect
merge into one dispatch (step 3/4 below).

`out_of_scope` counts what this migration does not translate — Twirl templates,
static assets, i18n bundles. Pass those counts to the architect and present them
at Gate 1. They are the only record that those files were a decision rather than
an oversight; nothing downstream reads `*.scala.html`, which is exactly how an
earlier run came to hand-port three templates into a template engine nobody
chose.

Check `classification_smell.warn`. When it is `true`, surface it before Gate 1
so the architect can draft `.migration/layer-overrides.json`. When
`warn_suppressed_reason` is set instead, the ratio was high but the sample was
too small to mean anything — mention it, do not act on it.

### 3. Researcher — **full mode only**

If `mode == "collapsed"`, **skip this step entirely** and go to step 4. Do not
dispatch the researcher. Collapsed mode has been chosen by the inventory since
this kit existed and never actually changed what ran; two dispatches over a
sub-20-file repo cost a round trip, a full artifact read, and roughly two
minutes to learn what the architect could read directly in seconds.

In full mode: dispatch the `researcher` subagent. It writes
`.migration/research.md` and returns a summary. Set `research.status = done`.

### 4. Architect → **GATE 1**

Dispatch `architect`. It writes `.migration/decisions.md` (and
`.migration/signature-exemptions.json` if any T2 exemption is warranted) and
returns its dependency map, config map, idiom decisions, `no_migration` list,
out-of-scope counts, and exemptions.

**In collapsed mode, say so in the brief** — `Mode: collapsed. No research.md
exists; read the repo yourself and write decisions.md, with a Survey section at
the top.` The architect writes **one file**: `decisions.md`, opening with a
`## Survey` section (inventory, dependencies, routes) followed by the normal
decisions sections — not a separate `research.md`. Full mode keeps two files
because it has two authors; collapsed mode has one author for both, so one file
is enough. Set `research.status = done` from that same dispatch, and point
`research.artifact` at `decisions.md` (the architect records this — see its
collapsed-mode instructions).

Record `architecture_review.no_migration` and `architecture_review.exemptions`,
then **stop and present the decisions to the human** — including the
out-of-scope counts and every exemption, each with the Spring construct that
replaced the method. An exemption suppresses a `blocker`, so it is approved
explicitly or not at all.

On approval, also record the sha of the exemptions file:

```bash
python3 - <<'PY'
import hashlib, pathlib
p = pathlib.Path("<spring>/.migration/signature-exemptions.json")
print(hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else "")
PY
# then: state.py set --path architecture_review.exemptions_sha256 --value <sha>
```

`gate.py` re-hashes that file on every run and reports
`exemptions.modified_after_gate`. If it ever comes back true, set
`architecture_review.exemptions_modified_after_gate` to `true` (it renders red
in the report) and treat the gate's `needs_agent` as binding — someone widened
the suppression set after the human approved it. This is the only gate in the run, and the cheapest correction
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

**Skip empty layers without dispatching anyone.** Before the loop, read
`source_inventory.play.by_layer`. A layer with 0 Play files is set `done` with
`files_migrated: 0` immediately — no dev dispatch, no gate. Dispatching an agent
to migrate nothing costs a full round trip to be told there was nothing there.

**Collapsed fast path.** When `mode == "collapsed"` *and* every non-empty layer
has no more files than `batch_size`, dispatch dev **once** with the ordered list
of non-empty layers instead of running the loop:

```
Layers, in order: model (2), controller (4), other (2). One pass, all layers.
```

Then gate once with `--tiers T1,T2,T3` and commit once. If that gate comes back
anything but `passed`, **fall back**: re-enter the per-layer loop below at the
first layer that has an open finding, and run it normally from there. The fast
path is an optimisation with a bounded failure mode, not a different algorithm.

Otherwise, for each layer not already `done` or in `failed_layers`:

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
     `commits.<layer>`, increment `batches_completed`, and reset the attempt
     budget with `state.py bump-attempt --layer <layer> --reset` — the next
     batch starts with a fresh 3-attempt budget. If `remaining_files == 0`,
     set the layer `done` and move to the next layer; otherwise loop back to
     step 1 for the next batch.
   - `failed` / `needs_review` with `needs_agent: false` → record the findings
     with `add-finding`, count the attempt with
     `state.py bump-attempt --layer <layer> --signatures '<tiers.T1.signatures>'`,
     and re-dispatch dev **with the finding IDs attached**, scoped to the same
     batch. The finding carries evidence; a bare error dump does not.
     `bump-attempt` prints the new count: at **3**, stop re-dispatching this
     layer and follow the escalation path in **Gate 3**. Nothing else moves
     that counter, so skipping this call means the layer retries forever.
   - `needs_agent: true` → dispatch `qa` with `agent_reason` and the
     path to the gate output. See below.

   If step 4 keeps hitting the escalation trigger below for this layer, stop
   looping this layer and move to the next one — see **Gate 3** below.

The gate runs the Play-repo guard for you, before T1, and reports it as
`guard` at the top of its output. You can also run it directly between
dispatches:

```bash
python3 "$CLAUDE_PLUGIN_ROOT/scripts/tools/guard.py" check \
    --play-repo <play> --spring-repo <spring>
```

**Empty output is not evidence. Only `"status": "clean"` is.** The old form of
this check was `git status --porcelain` with "non-empty means tampered", which
silently passed forever on any Play repo not under git — git exits 128 with
empty stdout, and empty read as clean.

Three statuses, and only one of them lets the run continue:

| `status` | Exit | Meaning |
|---|---|---|
| `clean` | 0 | Play tree matches the baseline. Continue. |
| `tampered` | 2 | Dev wrote to read-only source. **Halt.** |
| `error` | 3 | The guard could not run (no baseline, unreadable tree). **Halt.** |

`error` halts exactly like `tampered`: a guard that cannot run is a guard that
is not running, and there is nothing left enforcing the invariant. `gate.py`
short-circuits on a non-clean guard with `"status": "halt"` and exit 4 — do not
read that as a layer failure to soft-fail past.

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
python3 "$CLAUDE_PLUGIN_ROOT/scripts/tools/state.py" finish \
    --status-file <spring>/migration-status.json
python3 "$CLAUDE_PLUGIN_ROOT/scripts/tools/report.py" \
    --status-file <spring>/migration-status.json --out <spring>/.migration/report.html \
    --token-project <your cwd> --session <uuid>
```

`finish` closes the wall clock; run it *before* the report or the report shows
a run that never ended. `report.py` then measures token usage from the session
transcripts itself and renders a **Run cost** section — wall clock, one row per
dispatch, and main-thread vs subagent token totals with a list-price estimate.
It reads those transcripts, it never writes them, and it does not write the
status file either: you stay the only writer. If the transcripts aren't
reachable the section says so rather than rendering a zero, because a zero
reads as "this run was free". The `--token-project` / `--session` flags default
to whatever `init` recorded, so passing them again is belt and braces.

Then print a terse chat summary: the `failed_layers` list (if non-empty),
`qa_findings` filtered to `severity == "blocker"` (this schema's top severity
tier — everything else stays out of chat and only shows in the report), and
**one line for out-of-scope work**, e.g. `Out of scope: 3 Twirl templates, 7
static assets left in the Play repo (not migrated)`, and **one line for what the
run cost**, e.g. `Run: 14m 20s wall clock, 4 dispatches, 512k tokens (~$1.40)`.
End with the `report.html` path. Everything else — clean layers, `major`/`minor`
findings — is in the report, not the chat.

The out-of-scope line is not optional. A human who is not told which files were
never in scope will read the report as a complete migration.

### Gap report

Finally, if `.migration/gaps.jsonl` has any entries:

```bash
python3 "$CLAUDE_PLUGIN_ROOT/scripts/tools/gap_report.py" render --spring-repo <spring>
```

Add one line to the chat summary: `N gap(s) recorded — .migration/gap-report.md
(redacted; share it if you want them fixed upstream)`. Then stop. **Do not send
it anywhere**, do not open an issue, do not summarise its contents into chat —
it is the user's to read and share or not.

Gaps are the kit's own blind spots: places an agent had no rule and improvised.
They are worth surfacing even on a completely clean run, because a gap that
produced a green migration is the one nobody finds by looking at failures. See
[docs/GAPS.md](../../docs/GAPS.md).

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
time. But instead of stopping: record the layer in `failed_layers`, reset the
counter with `state.py bump-attempt --layer <layer> --reset`, and move on to the
**next layer** in the dependency order. The escalation file is what a human reads afterward — via the chat
summary or `report.html` — to see what went wrong and why, not something they
have to respond to in the moment.

**Two conditions still halt the whole run**, because they are integrity
violations, not a layer running out of retries:

- `guard.py check` returns anything other than `clean` — `tampered` (dev touched
  read-only source) or `error` (the guard could not run at all).
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

**Record every dispatch as it returns.** The tool result carries that
subagent's `duration_ms` and `subagent_tokens`; copy them straight across:

```bash
state.py add-dispatch --status-file S --json '{"role":"dev","layer":"service",
    "mode":"transform","duration_ms":195379,"tokens":53462,"tool_uses":27}'
```

You are the only participant who ever sees those numbers — the subagent's
context is discarded when it reports, and the transcript can tell the report
that *some* sidechain spent 53k tokens but not which layer bought them. Write
it down at the moment it returns or the attribution is gone. This costs one
subprocess per dispatch and is what turns "the migration passed" into "the
migration passed, in 12 minutes, for 340k tokens".

Dev:

```
Load the dev agent. Mode: transform. Layer: service.
Batch size: 25 (one --batch-size pass only).
Jar: /home/user/.claude/plugins/data/.../dev-toolkit-1.0.1.jar (from fetch_jar.py)
Decisions: .migration/decisions.md   Research: .migration/research.md
(Collapsed mode: no research.md — Survey section is inside decisions.md.)
Play repo: /path/to/play (READ ONLY)  Spring repo: /path/to/spring
Bash timeout: 900000 for mvn, 600000 for java -jar. Read your context once.
Compile before you report back; the gate re-runs it either way.
Journal your actions to .migration/journal/service-dev.ndjson.
Return: files touched, what you changed, files remaining in the layer,
anything you could not resolve.
```

`Mode: fix` instead, when re-dispatching against findings:

```
Load the dev agent. Mode: fix. Layer: service.
Open findings to fix: F-014, F-015 (see qa_findings in migration-status.json).
Read only the finding evidence, the files they name, and a Grep of decisions.md.
Jar / repos / journal: as above.
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
Boot method: scripts/tools/boot.py only — preflight, start, stop. Never `sbt run &`.
Ports: Play 9000, Spring 8080.  Run dir: /path/to/spring/.migration/run
Wait budget: --wait-timeout 180 per app; Bash tool timeout 300000 on every start.
No Docker. Do not pull images. A missing toolchain is a t5-skipped finding.
Run boot.py stop-all before reporting back, whatever the outcome.
Return: per-endpoint verdict, findings, and the boot.py status count. Do not fix anything.
```

**After that dispatch returns — every time, whatever it returned — run teardown
yourself:**

```bash
python3 "$CLAUDE_PLUGIN_ROOT/scripts/tools/boot.py" stop-all --run-dir <spring>/.migration/run
python3 "$CLAUDE_PLUGIN_ROOT/scripts/tools/boot.py" status   --run-dir <spring>/.migration/run
```

A subagent that died never runs its own teardown, so teardown cannot live only
inside the thing being torn down. If `status` reports anything still running,
say so in the chat summary — a leftover sbt/JVM tree holds ports and CPU long
after the run, and the user will feel it as a machine that got slow for no
reason.

If `run_config.skip_t5` or `run_config.no_boot` is set, the dispatch and both
`boot.py` calls are skipped entirely, and `endpoint_verification` records
`{"status": "skipped", "reason": "--skip-t5"}` — never anything that reads as
passing.

## Halt conditions

- `build.sbt` or `conf/application.conf` missing → ask the human.
- `fetch_jar.py` fails (checksum mismatch, network error), or `java`/`mvn`
  missing → stop, it is an environment problem.
- `guard.py check` returns `tampered` → stop, dev broke the read-only boundary.
- `guard.py check` returns `error`, or `gate.py` returns `"status": "halt"` →
  stop. An unrunnable guard is not a passing one.
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
