---
description: Regenerate the migration report.html for a target Play repo without re-running any migration step. Use via /play-to-springboot:report <path>, not automatically.
disable-model-invocation: true
argument-hint: <path-to-play-repo>
---

# Report

Regenerates `.migration/report.html` from the current `migration-status.json`.
No orchestration, no gates, no side effects on migration state — this is a
read-then-render command for checking back on a run days later.

## 1. Validate the argument

`$ARGUMENTS` is the same Play repo path you'd pass to
`/play-to-springboot:migrate`. If it's empty, print usage and stop:

```
Usage: /play-to-springboot:report <path-to-play-repo>
```

## 2. Resolve the Spring repo

Same resolution `scripts/migration_orchestrator.py status` uses: look for
`workspace.yaml` in the Play repo's parent directory (or wherever
`/play-to-springboot:migrate` was told to put the workspace, if you know it
was overridden). If `workspace.yaml` has a `spring_repo:` key, use it.
Otherwise the default is `<workspace-dir>/spring-<play-repo-basename>`.

## 3. Check state exists

If `<spring-repo>/migration-status.json` doesn't exist, say so plainly and
stop — there's nothing to report yet, and this skill does not create it:

```
No migration-status.json at <spring-repo>. Run /play-to-springboot:migrate <path> first.
```

## 4. Render

```bash
python3 "$CLAUDE_PLUGIN_ROOT/scripts/tools/report.py" \
    --status-file <spring-repo>/migration-status.json \
    --out <spring-repo>/.migration/report.html
```

Print the resulting path. Don't touch `migration-status.json` — this command
reads it, nothing more.
