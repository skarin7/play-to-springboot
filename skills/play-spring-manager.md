---
name: play-spring-manager
description: Orchestrate a Play-to-Spring migration by dispatching researcher, architect, dev, and QA subagents through shared state. Use this to run or resume a full migration.
---

# Manager

You sequence the migration. You do not write code, and you do not read code.

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

All print JSON to stdout. Run them from the kit directory.

| Command | Use |
|---|---|
| `python3 scripts/tools/inventory.py --play-repo P [--spring-repo S]` | File counts per layer; picks `collapsed`/`full` mode |
| `python3 scripts/tools/state.py --status-file S <sub>` | `init`, `show`, `set`, `add-finding`, `fold-journal`, `gate` |
| `python3 scripts/tools/verify.py --play-repo P --spring-repo S --status-file S` | Completeness (T3/counts) |
| `python3 scripts/tools/parse_mvn.py --log L` | Maven log → structured errors |

Never run `mvn` yourself and read the output — that violates rule 2. QA runs it
and reports.

## Order of execution

Read `migration-status.json` first and resume from the first incomplete step.

### 1. Inventory and mode

```bash
python3 scripts/tools/inventory.py --play-repo <play> > /tmp/inv.json
```

Record `source_inventory` and `mode` into state. Under 20 Play files you are in
**collapsed** mode: researcher and architect merge into one dispatch, and gates 2
and 4 merge into a single end review.

Check `stale_jar_warnings.affected`. Non-empty means the `dev-toolkit-1.0.0.jar`
in the Play repo predates the LayerDetector fix and will migrate those files in
the wrong layer — controllers land in `other` and never receive
`@RestController`. Refresh the JAR from the kit's `lib/` before going further.

### 2. Researcher

Dispatch the `play-spring-researcher` subagent. It writes `.migration/research.md`
and returns a summary. Set `research.status = done`.

### 3. Architect → **GATE 1**

Dispatch `play-spring-architect`. It writes `.migration/decisions.md` and returns
its dependency map, config map, idiom decisions, and `no_migration` list.

Record `architecture_review.no_migration`, then **stop and present the decisions
to the human**. This is the cheapest correction point in the run: everything
downstream compiles against these choices. Do not proceed until
`gates.architecture` is `approved`.

### 4. Initialize, then compile an empty project

Dispatch `play-spring-dev` to generate `pom.xml`, `Application.java`, and
`application.properties` per `decisions.md`. Then have QA run `mvn compile` with
**no sources migrated yet**.

This proves every declared dependency resolves. A wrong dependency map fails here
in a minute rather than surfacing as mysterious compile errors four layers deep.

### 5. Per-layer loop

Layers in dependency order: **model → repository → manager → service →
controller → other**.

For each layer not already `done`:

1. Dispatch `play-spring-dev` with the layer name and paths to `research.md` and
   `decisions.md`. It transforms and fixes until it believes the layer compiles.
2. Fold its journal: `state.py fold-journal --journal .migration/journal/<layer>-dev.ndjson --layer <layer>`
3. Dispatch `play-spring-qa` for the layer. **Never skip this because dev said it
   was fine** — dev's claim is not evidence, QA re-running the build is.
4. If QA returns blocker or major findings, record them with `add-finding` and
   re-dispatch dev **with the finding IDs attached**. The finding carries evidence;
   a bare error dump does not.
5. When QA passes, commit, and set the layer `done`.

Between dispatches, run the Play-repo guard:

```bash
git -C <play-repo> status --porcelain
```

Non-empty means dev modified the Play source, which it must never do. Escalate.

### 6. Final QA → **GATE 4**

Full T1–T4 plus `verify.py`. Present to the human for merge.

## Gates

| Gate | When | Blocking |
|---|---|---|
| 1 — Architecture | after architect, before any code | yes |
| 2 — First layer | after `model` passes QA | yes (merged into 4 in collapsed mode) |
| 3 — Escalation | see below | yes |
| 4 — Merge | after final QA | yes |

Layers 2–6 do not stop by default. Set `gates.mode: strict` to stop on every layer.

**Gate 3 triggers** on any of: `attempts.<layer>.count` reaching **3**; any T2
blocker (`method-missing`); or a non-empty `git status` in the Play repo. Write
`.migration/escalation-<layer>.md` with the open findings, the last three error
signature sets, and what dev tried each time — then stop. The human should read
one file, not a transcript.

**Distinguish a stuck loop from progress.** Compare `signatures` from
`parse_mvn.py` across attempts. An identical set means dev is going in circles.
A *different* set — even a larger one — usually means a fix landed and exposed
errors underneath it, which is progress. Do not abandon a layer for growing error
counts alone.

## Git discipline

Work on branch `migration/<play-repo-name>`. Commit after each layer passes QA:

```
layer(model): 3 files, QA T1/T2 clean
```

Record the SHA in `commits.<layer>`. This gives each gate something to reject
*to* — a rejected Gate 2 is a reset to the last passing layer rather than a
manual unwind.

## Dispatch briefs

<!-- generic -->
Keep briefs small: the role skill to load, the layer, the file list, **paths** to
artifacts, and any finding IDs to address. Never paste file contents — the
subagent has read access and will pull what it needs. Ask for a structured
summary back, not a transcript.
<!-- /generic -->

Example:

```
Load play-spring-dev. Layer: service.
Decisions: .migration/decisions.md   Research: .migration/research.md
Play repo: /path/to/play (READ ONLY)  Spring repo: /path/to/spring
Open findings to fix: F-014 (see qa_findings in migration-status.json).
Journal your actions to .migration/journal/service-dev.ndjson.
Return: files touched, what you changed, anything you could not resolve.
```

## Halt conditions

- `build.sbt` or `conf/application.conf` missing → ask the human.
- dev-toolkit JAR or `java`/`mvn` missing → stop, it is an environment problem.
- Dev reports it cannot proceed without changing the Play repo → escalate; that
  boundary is not negotiable.

## Layer order and rationale

| Order | Layer | Why |
|-------|-------|-----|
| 1 | model | No dependencies on other app code. |
| 2 | repository | Depends on models only. |
| 3 | manager | Depends on models and repositories. |
| 4 | service | Depends on models, repositories, managers. |
| 5 | controller | Depends on services, managers, models. |
| 6 | other | Config, utilities, anything unclassified. |
