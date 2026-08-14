---
name: Gap report
about: The plugin had no rule for something in your Play repo
title: "[gap] "
labels: gap
---

<!--
Generate the report first:

  python3 "$CLAUDE_PLUGIN_ROOT/scripts/tools/gap_report.py" render --spring-repo <spring-repo>

It writes .migration/gap-report.md. Open it and read it before pasting — it is
redacted (your class names and paths are replaced with salted hashes, framework
symbols and Maven coordinates are kept), but it is your call what leaves your
machine. Delete any line you are not comfortable sharing; a partial report is
still useful.
-->

## Report

<!-- paste the contents of .migration/gap-report.md here -->

## Anything the report could not say

<!--
Optional, and only what you are comfortable writing in the open.

The single most useful thing: what *should* the plugin have done? A gap report
says an agent improvised; it cannot say what the right answer was. If you know
the Spring equivalent of the Play idiom it tripped on, say so — that is the line
that turns a report into a fix.
-->

## Repo shape

- Play version:
- Java version:
- Roughly how many Java files:
- Anything unusual about the layout:
