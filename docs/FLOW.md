# Migration flow

Diagrams of how a run is sequenced, how the dev/gate loop corrects itself, who is
allowed to write what, and how endpoint parity is proved.

Rendered PNGs sit alongside this file for viewers without mermaid support.

For the operator walkthrough see [ORCHESTRATION.md](ORCHESTRATION.md); for the
schema and gate rules see [STATE-CONTRACT.md](STATE-CONTRACT.md).

---

## 1. End to end

```mermaid
flowchart TD
    START([setup.sh]) --> INV["inventory.py<br/>counts, mode, JAR version check"]
    INV --> RES[researcher<br/>reads Play repo]
    RES --> ARCH[architect<br/>decides mapping]
    ARCH --> G1{{"GATE 1 — human<br/>approve the approach"}}

    G1 -->|revise| ARCH
    G1 -->|approved| INIT["dev<br/>pom.xml, Application.java, properties"]

    INIT --> EMPTY["gate.py --tiers T1 on the EMPTY project<br/>proves every dependency resolves"]
    EMPTY -->|dependency error| ARCH
    EMPTY -->|clean| LOOP

    LOOP[["per-layer loop<br/>model → repository → manager<br/>→ service → controller → other"]]
    LOOP --> G2{{"GATE 2 — human<br/>after the first layer"}}
    G2 -->|reject| RESET["git reset to last passing layer"]
    RESET --> ARCH
    G2 -->|approved| MORE{more layers?}
    MORE -->|yes| LOOP
    MORE -->|no| FINAL["gate.py --final: T1–T4 full tree<br/>verify.py completeness"]

    FINAL --> T5["QA agent: T5 endpoint parity<br/>boot Play, capture, boot Spring, capture, diff"]
    T5 --> G4{{"GATE 4 — human<br/>approve the merge"}}
    G4 -->|approved| DONE([done])
    G4 -->|reject| LOOP

    style G1 fill:#fff3cd,stroke:#b8860b,color:#000
    style G2 fill:#fff3cd,stroke:#b8860b,color:#000
    style G4 fill:#fff3cd,stroke:#b8860b,color:#000
    style EMPTY fill:#d1ecf1,stroke:#0c5460,color:#000
    style T5 fill:#fde7e9,stroke:#c62828,color:#000
```

[PNG](flow-1-end-to-end.png)

The empty-project compile is worth its own box. Zero sources, but it fails a bad
dependency map in a minute instead of surfacing as strange compile errors four
layers deep.

T5 is the other box worth pausing on. T1–T4 prove the code compiles, kept its
methods, and answers at the right paths. Only T5 proves it returns the same
thing — and it is the one tier that needs both applications running, which is why
it is a QA dispatch rather than a subprocess.

---

## 2. The dev ↔ gate loop

This is the self-correcting part. Dev writes **and compiles**; the manager runs
the scripted gate; findings with evidence go back to dev. QA is dispatched only
when the gate cannot rule on its own result.

```mermaid
flowchart TD
    subgraph MGR["manager — owns state, never reads source"]
        DISPATCH["dispatch dev<br/>layer + paths + finding IDs"]
        FOLD["fold journal into state"]
        GUARD{"git status<br/>on Play repo<br/>empty?"}
        GATE["gate.py --layer X<br/>T1 compile · T2 signatures<br/>T3 routes (controller only)"]
        NEED{"needs_agent ?"}
        RECORD["state.py add-finding"]
        COUNT{"attempts<br/>&lt; 3 ?"}
        COMMIT["commit layer<br/>state.py set status=done"]
        ESC["write escalation-LAYER.md<br/>STOP"]
    end

    subgraph DEV["dev — the only role that writes source"]
        D1["read decisions.md,<br/>Play source, migrated sibling"]
        D2["migrate-app --layer X"]
        D3["mvn compile, fix errors<br/>until clean or honest blocker"]
        D4["append to journal.ndjson"]
    end

    subgraph QA["QA — dispatched only on ambiguity"]
        Q1["attribute errors to<br/>the layer that caused them"]
        Q2["read unclassifiable build failures<br/>and unparseable files"]
        Q3["emit findings with evidence"]
    end

    DISPATCH --> D1 --> D2 --> D3 --> D4
    D4 --> FOLD --> GUARD
    GUARD -->|"NOT empty — dev touched Play"| ESC
    GUARD -->|empty| GATE
    GATE --> NEED

    NEED -->|"no — findings speak for themselves"| VERDICT{"status ?"}
    NEED -->|"yes"| Q1
    Q1 --> Q2 --> Q3 --> RECORD

    VERDICT -->|"failed / needs_review"| RECORD
    VERDICT -->|passed| COMMIT

    RECORD --> COUNT
    COUNT -->|yes| DISPATCH
    COUNT -->|"no — 3 attempts"| ESC

    style DEV fill:#e8f5e9,stroke:#2e7d32,color:#000
    style QA fill:#fde7e9,stroke:#c62828,color:#000
    style MGR fill:#e3f2fd,stroke:#1565c0,color:#000
    style ESC fill:#fff3cd,stroke:#b8860b,color:#000
    style GATE fill:#d1ecf1,stroke:#0c5460,color:#000
```

[PNG](flow-2-dev-gate-loop.png)

### Why the loop closes instead of spinning

**Dev owns the compile; the manager still re-runs it.** Dev fixing its own build
errors is what keeps the loop short — nobody should be dispatched to discover a
missing import. But dev's "the layer compiles" is a claim, and `gate.py` re-runs
`mvn compile` regardless. Dev cannot mark its own work complete.

**The gate is a subprocess, not a dispatch.** All four scripted tiers are
deterministic, so wrapping them in an agent cost a full round trip per layer and
returned the same finding the script had already produced. QA is now dispatched
only for the cases in the `needs_agent` branch — chiefly attributing an error to
a layer that was already signed off, which is the one thing dev left alone gets
persistently wrong.

**Findings carry evidence, not just errors.** A raw error dump produces a guess.
A finding like `ContentService.search: 24 statements in Play -> 1 in Spring`
points at the cause.

**Attempts are bounded.** Three tries, then a human sees one file rather than a
transcript. Repeating a failing approach does not make it work.

---

## 3. A finding's life

```mermaid
sequenceDiagram
    participant M as manager
    participant D as dev
    participant G as gate.py
    participant S as migration-status.json

    M->>D: layer=service, decisions.md, research.md
    D->>D: migrate-app, then mvn compile and fix
    Note over D: cannot port a Play WS call<br/>writes `return null` to clear the build
    D-->>M: "layer builds clean"

    M->>G: --layer service
    G->>G: T1 mvn compile — passes
    G->>G: T2 signature_diff --layer-only
    Note over G: search: 24 statements → 1
    G-->>M: failed · logic-dropped · needs_agent=false

    M->>S: add-finding → F-014
    Note over M: attempts.service.count = 1

    M->>D: fix F-014 (evidence attached)
    D->>D: read Play source, port the WS call properly
    D-->>M: F-014 addressed

    M->>G: --layer service
    G-->>M: passed
    M->>S: F-014 status=fixed, layer done
    M->>M: commit "layer(service): 3 files, gate T1/T2 clean"
```

[PNG](flow-3-finding-lifecycle.png)

Three things this trace shows:

- **T1 passed on the stub.** Counting files would also have passed — a stubbed
  method is still one migrated file. T2 is the only tier that sees it.
- **Dev claimed a clean build and was right.** The claim still bought nothing;
  the gate re-ran the compile anyway.
- **No QA dispatch.** `needs_agent` is false because a `logic-dropped` finding
  already names the class, the method, and both statement counts. Dispatching an
  agent here would return the same sentence one round trip later.

---

## 4. Who writes what

The single-writer rule, and why subagents report instead of writing state.

```mermaid
flowchart LR
    subgraph READ["read-only"]
        PLAY[("Play repo")]
    end

    subgraph WRITE["written during a run"]
        SPRING[("spring-NAME/src/main/java")]
        STATE[("migration-status.json")]
        ART[(".migration/<br/>research.md, decisions.md")]
        JRN[(".migration/journal/<br/>append-only")]
    end

    RESEARCHER[researcher] -.reads.-> PLAY
    ARCHITECT[architect] -.reads.-> ART
    QAA[QA] -.reads.-> SPRING
    QAA -.reads.-> PLAY

    RESEARCHER ==> ART
    ARCHITECT ==> ART
    DEVV[dev] ==> SPRING
    DEVV ==> JRN
    DEVV -.reads.-> PLAY
    MANAGER[manager] ==> STATE
    JRN -.folded in by manager.-> STATE

    style PLAY fill:#eeeeee,stroke:#616161,color:#000
    style STATE fill:#e3f2fd,stroke:#1565c0,color:#000
    style SPRING fill:#e8f5e9,stroke:#2e7d32,color:#000
```

[PNG](flow-4-write-ownership.png)

- **Only dev writes source**, and only into the Spring tree. Enforced by tool
  grants in `.claude/agents/`; backstopped by the `git status` guard on the Play
  repo after every dispatch.
- **Only the manager writes state.** Two writers corrupt the file, and a subagent
  killed mid-write leaves JSON that cannot be resumed from.
- **Dev's journal is append-only.** A subagent's context dies with it; the
  journal is the only thing that survives, so a dev killed at file 8 of 15 is
  resumed rather than restarted.

---

## 5. Context handoff

Why the manager stays cheap while the work stays expensive.

```mermaid
flowchart TD
    MGR["manager<br/>persists across the entire run"]

    MGR -->|"brief: layer, paths, finding IDs<br/>never file contents"| SUB["subagent<br/>fresh context, discarded after"]
    SUB -->|"pulls from disk itself"| DISK[("source, decisions.md,<br/>compile logs")]
    SUB -->|"structured summary<br/>not a transcript"| MGR

    TOOLS["scripts/tools/*.py<br/>JSON out"] -->|"counts, findings, errors"| MGR
    GATE["gate.py"] -->|"raw mvn log"| DISK
    GATE -->|"parsed verdict only"| MGR

    style MGR fill:#e3f2fd,stroke:#1565c0,color:#000
    style SUB fill:#f3e5f5,stroke:#6a1b9a,color:#000
    style DISK fill:#eeeeee,stroke:#616161,color:#000
```

[PNG](flow-5-context-handoff.png)

**The invariant: the manager ingests no source code and no raw build output.**

Everything expensive — full compile logs, Java files, failed fix attempts — lives
and dies inside a subagent context that is thrown away. The manager sees JSON and
summaries.

This is load-bearing rather than tidy. Cost is dominated by cache reads, which
scale with context size multiplied by turns; a manager that pulls compile logs
into its own context both runs out of room partway through a large repo and costs
an order of magnitude more. Verify it held:

```bash
python3 scripts/tools/token_report.py --project /path/to/play-repo --by-agent
```

A manager share of input tokens that climbs layer over layer means the handoff
rules leaked.

`gate.py` is what lets the manager own verification without owning build output:
the raw Maven log goes to `.migration/logs/`, and only the parsed verdict crosses
into the manager's context.

---

## 6. T5 — endpoint response parity

The tier that proves behaviour rather than structure, and the reason QA still
exists as a role.

```mermaid
flowchart TD
    PROBES["endpoint_diff.py probes<br/>seeded from conf/routes"]
    EDIT["QA fills in path_params;<br/>mutating verbs stay disabled"]
    PROBES --> EDIT

    EDIT --> BOOTP["boot Play<br/>sbt run"]
    BOOTP --> CAPP["capture → responses-play.json"]
    CAPP --> STOPP["stop Play"]
    STOPP --> BOOTS["boot Spring<br/>mvn spring-boot:run"]
    BOOTS --> CAPS["capture → responses-spring.json"]
    CAPS --> DIFF["endpoint_diff.py diff"]

    DIFF --> AUTO["mechanical:<br/>status codes, missing fields,<br/>retyped fields, list lengths"]
    DIFF --> JUDGE["judgment — QA rules:<br/>field ordering · null vs absent<br/>expected value drift"]

    AUTO --> FIND["findings with evidence"]
    JUDGE --> FIND
    FIND --> MGR["manager: add-finding → dev"]

    style JUDGE fill:#fde7e9,stroke:#c62828,color:#000
    style AUTO fill:#d1ecf1,stroke:#0c5460,color:#000
```

[PNG](flow-6-endpoint-parity.png)

**Why values are masked before comparing.** Timestamps, generated ids and
durations differ between two runs of the *same* application. Comparing them for
equality fails every correctly migrated endpoint, and a tier that always
complains is a tier nobody reads by the third layer. They are checked for
presence and type instead — a masked `id` that changes from number to string is
still a finding.

**Why POST is not probed automatically.** `conf/routes` records a verb and a
path, never the shape of the body an endpoint accepts, and a POST changes the
store it writes to — so the second capture is answering a different question than
the first. Enable mutating probes only with a supplied body and either a
disposable datastore per app or a reset between captures. Otherwise a GET-only
comparison is the honest check, and the mutating paths are reported as unproved
rather than as passing.
