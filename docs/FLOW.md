# Migration flow

Diagrams of how a run is sequenced, how the dev/QA loop corrects itself, and who
is allowed to write what.

For the operator walkthrough see [ORCHESTRATION.md](ORCHESTRATION.md); for the
schema and gate rules see [STATE-CONTRACT.md](STATE-CONTRACT.md).

---

## 1. End to end

```mermaid
flowchart TD
    START([setup.sh]) --> INV["inventory.py<br/>counts, mode, stale-JAR check"]
    INV --> RES[researcher<br/>reads Play repo]
    RES --> ARCH[architect<br/>decides mapping]
    ARCH --> G1{{"GATE 1 — human<br/>approve the approach"}}

    G1 -->|revise| ARCH
    G1 -->|approved| INIT["dev<br/>pom.xml, Application.java, properties"]

    INIT --> EMPTY["QA: mvn compile on the EMPTY project<br/>proves every dependency resolves"]
    EMPTY -->|dependency error| ARCH
    EMPTY -->|clean| LOOP

    LOOP[["per-layer loop<br/>model → repository → manager<br/>→ service → controller → other"]]
    LOOP --> G2{{"GATE 2 — human<br/>after the first layer"}}
    G2 -->|reject| RESET["git reset to last passing layer"]
    RESET --> ARCH
    G2 -->|approved| MORE{more layers?}
    MORE -->|yes| LOOP
    MORE -->|no| FINAL["QA final: T1 + T3 + T4<br/>verify.py completeness"]

    FINAL --> G4{{"GATE 4 — human<br/>approve the merge"}}
    G4 -->|approved| DONE([done])
    G4 -->|reject| LOOP

    style G1 fill:#fff3cd,stroke:#b8860b,color:#000
    style G2 fill:#fff3cd,stroke:#b8860b,color:#000
    style G4 fill:#fff3cd,stroke:#b8860b,color:#000
    style EMPTY fill:#d1ecf1,stroke:#0c5460,color:#000
```

The empty-project compile is worth its own box. Zero sources, but it fails a bad
dependency map in a minute instead of surfacing as strange compile errors four
layers deep.

---

## 2. The dev ↔ QA loop

This is the self-correcting part. Dev writes; QA verifies and hands back findings
with evidence; the manager re-dispatches with those findings attached.

```mermaid
flowchart TD
    subgraph MGR["manager — owns state, never reads source"]
        DISPATCH["dispatch dev<br/>layer + paths + finding IDs"]
        FOLD["fold journal into state"]
        GUARD{"git status<br/>on Play repo<br/>empty?"}
        RECORD["state.py add-finding"]
        COUNT{"attempts<br/>&lt; 3 ?"}
        COMMIT["commit layer<br/>state.py set status=done"]
        ESC["write escalation-LAYER.md<br/>STOP"]
    end

    subgraph DEV["dev — the only role that writes source"]
        D1["read decisions.md,<br/>Play source, migrated sibling"]
        D2["migrate-app --layer X"]
        D3["mvn compile, fix errors"]
        D4["append to journal.ndjson"]
    end

    subgraph QA["QA — verifies, never fixes"]
        Q1["T1 mvn compile"]
        Q2["T2 signature_diff"]
        Q3["T3 route parity<br/>controller layer only"]
        Q4["emit findings + error signatures"]
    end

    DISPATCH --> D1 --> D2 --> D3 --> D4
    D4 --> FOLD --> GUARD
    GUARD -->|"NOT empty — dev touched Play"| ESC
    GUARD -->|empty| Q1
    Q1 --> Q2 --> Q3 --> Q4

    Q4 -->|"blocker or major"| RECORD
    Q4 -->|"clean"| COMMIT

    RECORD --> COUNT
    COUNT -->|yes| DISPATCH
    COUNT -->|"no — 3 attempts"| ESC

    style DEV fill:#e8f5e9,stroke:#2e7d32,color:#000
    style QA fill:#fde7e9,stroke:#c62828,color:#000
    style MGR fill:#e3f2fd,stroke:#1565c0,color:#000
    style ESC fill:#fff3cd,stroke:#b8860b,color:#000
```

### Why the loop closes instead of spinning

**QA never trusts dev's claim.** "The layer compiles" is not evidence; QA re-runs
`mvn compile` itself. Dev cannot mark its own work complete.

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
    participant Q as QA
    participant S as migration-status.json

    M->>D: layer=service, decisions.md, research.md
    D->>D: migrate-app, then fix compile errors
    Note over D: cannot port a Play WS call<br/>writes `return null` to clear the build
    D-->>M: "layer builds"

    M->>Q: verify layer=service
    Q->>Q: T1 mvn compile — passes
    Q->>Q: T2 signature_diff
    Note over Q: search: 24 statements → 1
    Q-->>M: blocker, logic-dropped, with evidence

    M->>S: add-finding → F-014
    Note over M: attempts.service.count = 1

    M->>D: fix F-014 (evidence attached)
    D->>D: read Play source, port the WS call properly
    D-->>M: F-014 addressed

    M->>Q: re-verify layer=service
    Q->>Q: T1 passes, T2 passes
    Q-->>M: clean
    M->>S: F-014 status=fixed, layer done
    M->>M: commit "layer(service): 3 files, QA T1/T2 clean"
```

T1 passed on the stub. Counting files would also have passed — a stubbed method
is still one migrated file. T2 is the only tier that sees it.

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

    style MGR fill:#e3f2fd,stroke:#1565c0,color:#000
    style SUB fill:#f3e5f5,stroke:#6a1b9a,color:#000
    style DISK fill:#eeeeee,stroke:#616161,color:#000
```

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
