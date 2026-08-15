# Changelog

All notable changes to this plugin are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.2.2] - 2026-08-16

### Security
- The permission hook no longer auto-allows a compound command on the strength
  of its first word. `python3 <plugin>/scripts/gate.py && rm -rf <play>` was
  matched as a plugin script and allowed in full, so the chained command ran
  without a prompt. Allows are now withheld from any command line containing a
  control operator.
- Writes to the read-only Play repo are detected per command rather than from
  the first word only, so `true; rm -rf <play>` is denied instead of prompting.

## [1.2.1] - 2026-08-15

### Added
- Prompt-injection guardrails for untrusted content read out of the source repo,
  plus tamper detection on the run journal.
- Coverage scan from the dev-toolkit jar wired into Gate 1, surfacing files the
  transform skipped because of a gap.
- Per-role model assignment, so each subagent runs on a model matched to its job.
- Motivation section in the README covering why a codebase moves off Play.

### Changed
- Pinned the dev-toolkit jar to release 1.0.1.
- Renamed the orchestrator role, dropped the tier jargon, and added an
  end-to-end flow diagram to the README.
- Merged `research.md` into `decisions.md` for collapsed-mode runs.

## [1.2.0] - 2026-08-15

### Added
- Run-cost reporting: each migration run reports what it cost, not only whether
  it passed.

### Fixed
- Torn journal lines are contained instead of dropping every entry after them.
- Journal folding is now idempotent, and the attempt counter is wired up.

## [1.0.2] - 2026-08-14

### Fixed
- Duplicate hooks registration.

## [1.0.1] - 2026-08-14

### Added
- Gap-report loop and issue template.

### Fixed
- 14 defects found during the first real migration run.
- `toolkit-release.json` pinned to the actual `toolkit-v1.0.0` release.

## [1.0.0] - 2026-08-14

### Added
- Initial release, packaged as a Claude Code plugin.
- Role-based subagents — researcher, architect, dev, qa — coordinated through
  shared state files under `.migration/`.
- Single upfront human gate for the whole run.
- Checksum-verified fetch of the dev-toolkit jar.
- Human-authored layer overrides for non-conventional Play layouts.
- Endpoint response parity verification (T5).
- Measured token and cost accounting per run.
- Flow diagrams for the migration and the dev/QA loop.

[1.2.2]: https://github.com/skarin7/play-to-springboot/releases/tag/v1.2.2
[1.2.1]: https://github.com/skarin7/play-to-springboot/releases/tag/v1.2.1
[1.2.0]: https://github.com/skarin7/play-to-springboot/releases/tag/v1.2.0
[1.0.2]: https://github.com/skarin7/play-to-springboot/releases/tag/v1.0.2
[1.0.1]: https://github.com/skarin7/play-to-springboot/releases/tag/v1.0.1
[1.0.0]: https://github.com/skarin7/play-to-springboot/releases/tag/v1.0.0
