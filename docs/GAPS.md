# Gaps: how this plugin learns from a repo it has never seen

Every defect this kit has fixed so far was found the same way: run it against a
repo, read the transcript by hand, notice an agent improvised, patch the code.
That works exactly once. The next Play repo has its own surprises, on somebody
else's machine, and the plugin author never sees them.

This is the loop that replaces reading transcripts.

## A gap is not a finding

| | Says | Goes to | Fixed by |
|---|---|---|---|
| **Finding** | *This migration is wrong.* | `qa_findings`, the report | dev, in this run |
| **Gap** | *The plugin had no rule, so I improvised.* | `.migration/gaps.jsonl` | the plugin author, in a later release |

A finding is about the code being produced. A gap is about **this tool's own
blind spots**, and it is the only signal that says a rule is missing rather than
broken.

The distinction matters because the two have opposite lifetimes. A finding is
resolved and closed inside one run. A gap is still true after the run succeeds —
the migration can be perfect and the gap still worth reporting, because the next
person hits the same wall.

## Recording one

Any role may append to `<spring-repo>/.migration/gaps.jsonl`. Append-only, one
JSON object per line, same discipline as the dev journals:

```json
{"kind":"unhandled_idiom","subject":"play.libs.Akka.system()","role":"dev",
 "what_i_did":"hand-ported to @Async","blind_tier":"T2","layer":"service"}
```

| Field | Meaning |
|---|---|
| `kind` | One of the closed set below. Not free text — free text cannot be counted, which is the whole point of collecting it. |
| `subject` | The framework symbol, dependency coordinate, or thing you had no rule for |
| `role` | `researcher` \| `architect` \| `dev` \| `qa` |
| `what_i_did` | **The load-bearing field.** What you chose in the absence of a rule. |
| `blind_tier` | The tier that could not verify your choice, if any |
| `layer` | Optional |

`kind` is one of:

- `unmapped_dependency` — a `build.sbt` entry with no decided Spring counterpart
- `unhandled_idiom` — a Play API the transformer and the mapping table both miss
- `tier_blind_spot` — you knew no tier could verify what you produced
- `tool_error` — a helper crashed or could not run
- `agent_improvised` — no rule existed and you chose anyway
- `layout_surprise` — a repo shape the classifier did not expect
- `boot_failure` — T5 could not start an application

### `what_i_did` is the whole point

"Hand-ported to `@Async`" is worth a release. "Could not find a mapping" is
worth nothing. The improvisation is the data: it is what the fake `apply()`
method and the Thymeleaf template port both were, and neither was visible in any
artifact until somebody read a transcript.

Record it **even when the run succeeds.** Especially then — a gap that produced
a green run is the one nobody will ever find by looking at failures.

## Sharing one

```bash
python3 "$CLAUDE_PLUGIN_ROOT/scripts/tools/gap_report.py" render --spring-repo <spring>
```

Writes `.migration/gap-report.md`. **Read it, then decide.** Nothing is
uploaded — there is no network code in that tool at all. If you want to help,
attach it to an issue.

### What is in it, and what never is

**Framework symbols pass through. Everything else is hashed.**

`play.libs.Akka.system` is Play's public API — it belongs to Lightbend, it is
what makes the gap actionable, and it says nothing about you. `com.acme.Pricing`
is your business, is never needed to fix a gap, and becomes `<class:a1b2c3d4>`.

| Included | Never included |
|---|---|
| Framework symbols (`play.*`, `org.springframework.*`, `akka.*`, …) | Your class, package, or method names |
| Maven/Ivy coordinates (public artifacts) | Absolute paths, usernames, repo names |
| Counts: files per layer, findings per tier, attempts | Source code, finding text, log output |
| Python/OS/arch, plugin version | Hostname, IP, environment variables |

The hash is salted per install, stored once in `$CLAUDE_PLUGIN_DATA`. Two
consequences, both intended: the same class hashes the same way across *your*
runs, so "hit this four times" is visible; and the same class name at two
different companies never collides into one identity.

The full JSON is embedded in the Markdown under a collapsed section, so what you
would be sharing is something you can actually read rather than something you
are asked to trust.

## The author's side

```bash
python3 scripts/tools/gap_report.py aggregate --dir ./received-reports
```

Ranks gaps by **distinct installs**, not raw occurrences — one person running
the same repo forty times is one signal, not forty.

```
3 report(s) from 2 install(s), 4 distinct gap(s).

| Installs | Occurrences | Kind | Subject |
|---|---|---|---|
| **2** | 3 | `unhandled_idiom` | play.libs.Akka.system() |
| 1 | 2 | `unmapped_dependency` | com.typesafe.play:play-mailer_2.13:8.0.1 |
```

### Promotion rule

| Seen by | Lives in | Why |
|---|---|---|
| 1 install | that workspace's override file | Might be one project's local convention |
| **2+ installs** | a shipped default **plus a fixture** | Two independent repos is a pattern, not a coincidence |

The fixture is not optional. A rule promoted without one is a rule that silently
regresses the next time somebody refactors the tool that reads it.

### Where a promoted rule lands

| Gap kind | Lands in |
|---|---|
| `unmapped_dependency`, `unhandled_idiom` | the mapping tables in `agents/architect.md` / `agents/dev.md` |
| `layout_surprise` | `_KNOWN_SEGMENTS` in `inventory.py`, or `layers.py` |
| `tier_blind_spot` | tool code — a check that cannot see something needs new code, not new data |
| `boot_failure`, `tool_error` | `boot.py` preflight, or the failing tool |

Most gaps are the first row, and that is the point: the majority of what looks
like plugin development is data entry once the loop exists.

## What this deliberately does not do

- **No automatic upload.** A migration tool that reads proprietary source and
  phones home — even redacted — is one nobody installs twice.
- **No automatic rule promotion.** An agent that widens its own knowledge base
  is one whose behaviour changes for reasons nobody can reconstruct. Same
  failure mode as an unreviewed exemptions file, which is why that one is
  sha-checked. Human approval and a fixture, every time.
- **No claim that redaction is perfect.** It is a whitelist over a closed field
  set, which is why the report is a file you read rather than a payload you
  trust. If a `what_i_did` line would embarrass you, delete the line — the
  report is yours.
