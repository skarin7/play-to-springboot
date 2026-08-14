#!/usr/bin/env python3
"""
Render a self-contained HTML report from migration-status.json.

    python3 scripts/tools/report.py --status-file <spring-repo>/migration-status.json \
        --out <spring-repo>/.migration/report.html

Almost a pure function of the status file: every field it reads (``layers``,
``failed_layers``, ``qa_findings``, ``endpoint_verification``, ``commits``,
``attempts``, ``run_metrics``) is written by the manager. The one exception is
token accounting, which is measured here from Claude Code's session
transcripts at render time and rendered without being written back -- the
manager stays the only writer of migration-status.json. One HTML file, inline
CSS, no external assets, no server: it has to open standalone in a browser
days after the run that produced it.

No inline diffs -- this shows commit hashes and subjects (read from the
Spring repo's own git log when available) so a human follows the commit
itself for code detail, rather than the report trying to reproduce it.
"""

from __future__ import annotations

import argparse
import html
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SEVERITY_ORDER = {"blocker": 0, "major": 1, "minor": 2}


def iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def commit_subject(spring_repo: Path, sha: str) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "-C", str(spring_repo), "log", "-1", "--format=%s", sha],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    subject = proc.stdout.strip()
    return subject or None


def esc(value: Any) -> str:
    if value is None:
        return ""
    return html.escape(str(value))


def fmt_duration(seconds: Any) -> str:
    """Seconds as h/m/s. A bare '4913' tells a reader nothing at a glance."""
    try:
        total = int(seconds)
    except (TypeError, ValueError):
        return ""
    if total < 0:
        return ""
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def fmt_n(value: Any) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return ""


def collect_tokens(project: Path | None, session: str | None) -> dict[str, Any] | None:
    """
    Fold measured token usage out of Claude Code's session transcripts.

    Imported from token_report rather than reimplemented: the parsing rule
    (assistant ``usage`` blocks, ``isSidechain`` marks a subagent turn) has one
    home. Returns None whenever the transcripts are not reachable -- a report
    that fails to render because a log directory moved is worse than one that
    renders without the cost section.
    """
    if project is None:
        return None
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        import token_report  # noqa: PLC0415  (optional, resolved at render time)
    except ImportError:
        return None

    tdir = token_report.transcript_dir(Path(project))
    if not tdir.is_dir():
        return None
    files = ([tdir / f"{session}.jsonl"] if session else sorted(tdir.glob("*.jsonl")))
    files = [f for f in files if f.is_file()]
    if not files:
        return None

    prices = {
        "input": token_report.DEFAULT_INPUT_PRICE,
        "output": token_report.DEFAULT_OUTPUT_PRICE,
        "cache_write": token_report.DEFAULT_CACHE_WRITE_PRICE,
        "cache_read": token_report.DEFAULT_CACHE_READ_PRICE,
    }
    by_agent = {"main": token_report.blank(), "subagent": token_report.blank()}
    for jsonl in files:
        for label, usage in token_report.iter_usage(jsonl):
            token_report.accumulate(by_agent[label], usage)

    total = token_report.blank()
    for entry in by_agent.values():
        for k in total:
            total[k] += entry[k]
    if not total["turns"]:
        return None

    main_in = by_agent["main"]["input"] + by_agent["main"]["cache_read"]
    sub_in = by_agent["subagent"]["input"] + by_agent["subagent"]["cache_read"]
    return {
        "scope": "session" if session else "project",
        "session": session,
        "sessions_counted": len(files),
        "by_agent": {k: dict(v, cost_usd=round(token_report.cost(v, prices), 2))
                     for k, v in by_agent.items()},
        "total": dict(total, cost_usd=round(token_report.cost(total, prices), 2)),
        "manager_input_share_pct": (
            round(main_in / (main_in + sub_in) * 100, 1) if (main_in + sub_in) else None
        ),
        "prices_usd_per_mtok": prices,
    }


def render_run_cost(rm: dict[str, Any] | None) -> str:
    """Wall clock, per-dispatch cost, and measured token totals."""
    if not rm:
        return "<p class=\"empty\">No run metrics recorded.</p>"

    started, finished = rm.get("started_at"), rm.get("finished_at")
    duration = fmt_duration(rm.get("duration_seconds"))
    dispatches = rm.get("dispatches") or []
    tokens = rm.get("tokens")

    dispatch_ms = sum(int(d.get("duration_ms") or 0) for d in dispatches)
    dispatch_tokens = sum(int(d.get("tokens") or 0) for d in dispatches)

    stats = ["<div class=\"summary-box\">"]
    stats.append(
        f"<div class=\"stat\"><span class=\"n\">{esc(duration) or '—'}</span>"
        "<span class=\"label\">wall clock</span></div>"
    )
    stats.append(
        f"<div class=\"stat\"><span class=\"n\">{len(dispatches)}</span>"
        "<span class=\"label\">subagent dispatches</span></div>"
    )
    if dispatch_ms:
        stats.append(
            f"<div class=\"stat\"><span class=\"n\">{esc(fmt_duration(dispatch_ms // 1000))}</span>"
            "<span class=\"label\">in subagents</span></div>"
        )
    # Whether the subagents show up in the transcript at all is a property of
    # the harness, not of the run: with in-process subagents the session file
    # carries only main-thread turns. Left alone that renders as "manager share
    # of input: 100%", which is not a true reading of a run that spent a third
    # of its tokens inside dispatches.
    transcript_has_subagents = bool(
        tokens and (tokens["by_agent"].get("subagent") or {}).get("turns")
    )
    dispatch_supplements = bool(dispatch_tokens) and not transcript_has_subagents

    if tokens:
        t = tokens["total"]
        # Cache reads are billed at a fraction of fresh input and, on a long
        # run, outnumber everything else by an order of magnitude. Folded into
        # one headline they turn an 8-file migration into "10.8M tokens", which
        # is true and useless. Split, so the number a reader compares between
        # runs is the one that actually tracks work done.
        fresh = t["input"] + t["cache_write"] + t["output"]
        if dispatch_supplements:
            fresh += dispatch_tokens
        stats.append(
            f"<div class=\"stat\"><span class=\"n\">{esc(fmt_n(fresh))}</span>"
            "<span class=\"label\">tokens, excl. cache reads</span></div>"
        )
        stats.append(
            f"<div class=\"stat\"><span class=\"n\">{esc(fmt_n(t['cache_read']))}</span>"
            "<span class=\"label\">cache reads</span></div>"
        )
        stats.append(
            f"<div class=\"stat\"><span class=\"n\">${t['cost_usd']:.2f}</span>"
            "<span class=\"label\">est. cost (main thread)</span></div>"
        )
    elif dispatch_tokens:
        stats.append(
            f"<div class=\"stat\"><span class=\"n\">{esc(fmt_n(dispatch_tokens))}</span>"
            "<span class=\"label\">subagent tokens</span></div>"
        )
    stats.append("</div>")

    meta = (
        f"<p class=\"meta\">Started {esc(started) or '—'} · "
        f"finished {esc(finished) or 'not stamped'}</p>"
    )

    if dispatches:
        rows = []
        for i, d in enumerate(dispatches, 1):
            rows.append(
                "<tr>"
                f"<td>{i}</td>"
                f"<td>{esc(d.get('role'))}</td>"
                f"<td>{esc(d.get('layer') or '—')}</td>"
                f"<td>{esc(d.get('mode') or '—')}</td>"
                f"<td>{esc(fmt_duration((d.get('duration_ms') or 0) // 1000)) or '—'}</td>"
                f"<td>{esc(fmt_n(d.get('tokens'))) or '—'}</td>"
                f"<td>{esc(fmt_n(d.get('tool_uses'))) or '—'}</td>"
                "</tr>"
            )
        dispatch_table = (
            "<table><thead><tr><th>#</th><th>role</th><th>layer</th><th>mode</th>"
            "<th>duration</th><th>tokens</th><th>tool uses</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table>"
        )
    else:
        dispatch_table = "<p class=\"empty\">No dispatches recorded.</p>"

    if tokens:
        rows = []
        for label in ("main", "subagent"):
            a = tokens["by_agent"].get(label) or {}
            if not a.get("turns"):
                continue
            rows.append(
                "<tr>"
                f"<td>{esc(label)}</td>"
                f"<td>{esc(fmt_n(a['turns']))}</td>"
                f"<td>{esc(fmt_n(a['input']))}</td>"
                f"<td>{esc(fmt_n(a['cache_read']))}</td>"
                f"<td>{esc(fmt_n(a['cache_write']))}</td>"
                f"<td>{esc(fmt_n(a['output']))}</td>"
                f"<td>${a['cost_usd']:.2f}</td>"
                "</tr>"
            )
        t = tokens["total"]
        rows.append(
            "<tr><td><strong>total</strong></td>"
            f"<td><strong>{esc(fmt_n(t['turns']))}</strong></td>"
            f"<td><strong>{esc(fmt_n(t['input']))}</strong></td>"
            f"<td><strong>{esc(fmt_n(t['cache_read']))}</strong></td>"
            f"<td><strong>{esc(fmt_n(t['cache_write']))}</strong></td>"
            f"<td><strong>{esc(fmt_n(t['output']))}</strong></td>"
            f"<td><strong>${t['cost_usd']:.2f}</strong></td></tr>"
        )
        if dispatch_supplements:
            # One number per dispatch, with no input/output split to report --
            # that is all a returning subagent hands over.
            rows.insert(
                -1,
                "<tr><td>subagent<br><span class=\"label\">from dispatch records</span></td>"
                f"<td>{esc(len(dispatches))} dispatches</td>"
                f"<td colspan=\"4\">{esc(fmt_n(dispatch_tokens))} total "
                "(no input/output split reported)</td><td>—</td></tr>",
            )
            # Cache reads excluded on both sides: include them and the manager's
            # share is ~99% on every run long enough to matter, which measures
            # the cache rather than the handoff discipline it is there to watch.
            main_fresh = t["input"] + t["cache_write"] + t["output"]
            combined = main_fresh + dispatch_tokens
            share = round(main_fresh / combined * 100, 1) if combined else None
            share_basis = (
                "Manager share of tokens, excluding cache reads: {}%. The subagent "
                "figure comes from the dispatch records because this harness does not "
                "write subagent turns into the session transcript; the cost column "
                "above therefore prices the main thread only."
            )
        else:
            share = tokens.get("manager_input_share_pct")
            share_basis = (
                "Manager share of input: {}%. Expect this to fall as the repo grows "
                "— a rising share means the manager is ingesting source or build logs."
            )
        share_note = (
            f"<p class=\"meta\">{share_basis.format(esc(share))}</p>"
            if share is not None else ""
        )
        scope_note = (
            "<p class=\"meta\">Measured from this session's transcript."
            if tokens.get("scope") == "session" else
            f"<p class=\"meta\">Measured across {tokens.get('sessions_counted')} "
            "session transcript(s) in this project — may include work unrelated to "
            "this migration."
        ) + " Cost is list-price arithmetic, not a bill.</p>"
        token_table = (
            "<table><thead><tr><th>agent</th><th>turns</th><th>input</th>"
            "<th>cache read</th><th>cache write</th><th>output</th><th>USD</th>"
            f"</tr></thead><tbody>{''.join(rows)}</tbody></table>{share_note}{scope_note}"
        )
    else:
        token_table = (
            "<p class=\"empty\">No token accounting — session transcripts were not "
            "reachable at render time. Re-run report.py with --token-project "
            "(and --session) to fill this in.</p>"
        )

    return (
        f"{''.join(stats)}{meta}"
        "<h3>Dispatches</h3>"
        f"{dispatch_table}"
        "<h3>Tokens</h3>"
        f"{token_table}"
    )


def render_layers_table(layers: dict[str, Any], failed_layers: set[str]) -> str:
    rows = []
    for layer, entry in layers.items():
        entry = entry or {}
        status = entry.get("status", "pending")
        row_class = "failed-row" if layer in failed_layers else ""
        rows.append(
            f"<tr class=\"{row_class}\">"
            f"<td>{esc(layer)}</td>"
            f"<td><span class=\"badge badge-{esc(status)}\">{esc(status)}</span></td>"
            f"<td>{esc(entry.get('files_migrated', 0))}</td>"
            f"<td>{esc(len(entry.get('files_failed') or []))}</td>"
            f"<td>{esc(entry.get('batches_completed', 0))}</td>"
            f"<td>{esc(entry.get('remaining_files'))}</td>"
            f"<td>{esc(entry.get('last_error_count'))}</td>"
            f"<td>{esc(entry.get('failure_reason'))}</td>"
            f"</tr>"
        )
    return (
        "<table><thead><tr>"
        "<th>Layer</th><th>Status</th><th>Files migrated</th><th>Files failed</th>"
        "<th>Batches</th><th>Remaining</th><th>Last error count</th><th>Failure reason</th>"
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
    )


def render_findings_table(findings: list[dict[str, Any]]) -> str:
    if not findings:
        return "<p class=\"empty\">No QA findings.</p>"
    ordered = sorted(findings, key=lambda f: SEVERITY_ORDER.get(f.get("severity"), 9))
    rows = []
    for f in ordered:
        sev = f.get("severity", "")
        rows.append(
            f"<tr>"
            f"<td>{esc(f.get('id'))}</td>"
            f"<td><span class=\"badge badge-sev-{esc(sev)}\">{esc(sev)}</span></td>"
            f"<td>{esc(f.get('layer'))}</td>"
            f"<td>{esc(f.get('file'))}</td>"
            f"<td>{esc(f.get('tier'))}</td>"
            f"<td>{esc(f.get('category'))}</td>"
            f"<td>{esc(f.get('evidence'))}</td>"
            f"<td>{esc(f.get('suggested_fix'))}</td>"
            f"<td>{esc(f.get('status'))}</td>"
            f"</tr>"
        )
    return (
        "<table><thead><tr>"
        "<th>ID</th><th>Severity</th><th>Layer</th><th>File</th><th>Tier</th>"
        "<th>Category</th><th>Evidence</th><th>Suggested fix</th><th>Status</th>"
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
    )


def render_endpoint_verification(ev: dict[str, Any] | None) -> str:
    if not ev:
        return "<p class=\"empty\">T5 endpoint parity has not run yet.</p>"
    not_captured = ev.get("not_captured_after") or []
    return (
        "<table><tbody>"
        f"<tr><th>Status</th><td>{esc(ev.get('status'))}</td></tr>"
        f"<tr><th>Checked at</th><td>{esc(ev.get('checked_at'))}</td></tr>"
        f"<tr><th>Probes compared</th><td>{esc(ev.get('probes_compared'))}</td></tr>"
        f"<tr><th>Not captured after</th><td>{esc(', '.join(not_captured) if not_captured else '(none)')}</td></tr>"
        f"<tr><th>Artifact</th><td>{esc(ev.get('artifact'))}</td></tr>"
        "</tbody></table>"
    )


def render_exemptions(arch: dict[str, Any]) -> str:
    """
    T2 suppressions, and whether the approved set is still the one in effect.

    Rendered even when empty, and rendered loudly when the file changed after
    approval: an exemption is a blocker that was decided not to be one, so the
    reviewer needs to see the decision, not just its effect.
    """
    entries = arch.get("exemptions") or []
    modified = bool(arch.get("exemptions_modified_after_gate"))
    warning = ""
    if modified:
        warning = (
            "<p class=\"alert\"><strong>signature-exemptions.json changed after "
            "Gate 1 approval.</strong> The suppressions below are not the set the "
            "human approved.</p>"
        )
    if not entries:
        return warning + (
            "<p class=\"empty\">No project-specific T2 exemptions. "
            "Framework-glue defaults still apply — see any <code>suppressed</code> "
            "entries in the gate output.</p>"
        )
    rows = []
    for e in entries:
        if isinstance(e, str):
            e = {"method": e}
        rows.append(
            f"<tr>"
            f"<td>{esc(e.get('class'))}</td>"
            f"<td>{esc(e.get('method'))}</td>"
            f"<td>{esc(e.get('replacement'))}</td>"
            f"<td>{esc(e.get('reason'))}</td>"
            f"</tr>"
        )
    return warning + (
        "<table><thead><tr><th>Class</th><th>Method</th><th>Replaced by</th>"
        "<th>Reason</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
    )


def render_out_of_scope(oos: dict[str, Any] | None) -> str:
    """
    What the migration deliberately did not translate.

    Present even when empty. An exclusion nobody can see is indistinguishable
    from an omission, and the reader has no other way to learn that the Twirl
    templates in the source repo were a decision rather than a gap.
    """
    if not oos or not oos.get("categories"):
        return "<p class=\"empty\">No out-of-scope inventory recorded.</p>"
    rows = []
    for name, entry in (oos.get("categories") or {}).items():
        entry = entry or {}
        samples = entry.get("samples") or []
        rows.append(
            f"<tr>"
            f"<td>{esc(name.replace('_', ' '))}</td>"
            f"<td>{esc(entry.get('count', 0))}</td>"
            f"<td>{esc(', '.join(samples)) if samples else '<em>(none)</em>'}</td>"
            f"</tr>"
        )
    return (
        f"<p>Policy: <strong>{esc(oos.get('policy', 'left-in-place'))}</strong> — "
        f"{esc(oos.get('total_files', 0))} file(s) left in the Play repo, not "
        "migrated and not verified by any tier.</p>"
        "<table><thead><tr><th>Category</th><th>Files</th><th>Samples</th>"
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
    )


def render_commits(commits: dict[str, Any], spring_repo: Path | None) -> str:
    if not commits:
        return "<p class=\"empty\">No commits recorded.</p>"
    rows = []
    for layer, entries in commits.items():
        if isinstance(entries, str):
            entries = [{"sha": entries}]
        for entry in entries or []:
            if isinstance(entry, str):
                entry = {"sha": entry}
            sha = entry.get("sha", "")
            subject = commit_subject(spring_repo, sha) if spring_repo and sha else None
            rows.append(
                f"<tr>"
                f"<td>{esc(layer)}</td>"
                f"<td>{esc(entry.get('batch'))}</td>"
                f"<td><code>{esc(sha[:12] if sha else '')}</code></td>"
                f"<td>{esc(subject) if subject else '<em>(subject unavailable)</em>'}</td>"
                f"</tr>"
            )
    return (
        "<table><thead><tr><th>Layer</th><th>Batch</th><th>Commit</th><th>Subject</th>"
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
    )


CSS = """
:root { color-scheme: light dark; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
       max-width: 1100px; margin: 2rem auto; padding: 0 1rem; line-height: 1.5; }
h1 { margin-bottom: 0.2rem; }
.meta { color: #666; margin-bottom: 1.5rem; }
h2 { margin-top: 2.5rem; border-bottom: 1px solid #ccc; padding-bottom: 0.3rem; }
table { border-collapse: collapse; width: 100%; margin-top: 0.75rem; font-size: 0.9rem; }
th, td { border: 1px solid #ccc; padding: 0.4rem 0.6rem; text-align: left; vertical-align: top; }
th { background: rgba(127,127,127,0.15); }
tr.failed-row { background: rgba(220,53,69,0.12); }
.badge { display: inline-block; padding: 0.1rem 0.5rem; border-radius: 0.75rem; font-size: 0.8rem; }
.badge-done { background: #d4edda; color: #155724; }
.badge-failed { background: #f8d7da; color: #721c24; }
.badge-in_progress { background: #fff3cd; color: #856404; }
.badge-pending { background: #e2e3e5; color: #383d41; }
.badge-sev-blocker { background: #f8d7da; color: #721c24; }
.badge-sev-major { background: #fff3cd; color: #856404; }
.badge-sev-minor { background: #e2e3e5; color: #383d41; }
.summary-box { display: flex; gap: 1.5rem; flex-wrap: wrap; margin: 1rem 0; }
.stat { border: 1px solid #ccc; border-radius: 0.5rem; padding: 0.75rem 1.25rem; min-width: 8rem; }
.stat .n { font-size: 1.6rem; font-weight: 600; display: block; }
.stat .label { color: #666; font-size: 0.85rem; }
.empty { color: #666; font-style: italic; }
.alert { background: rgba(220,53,69,0.12); border: 1px solid #dc3545;
         border-radius: 0.4rem; padding: 0.6rem 0.9rem; }
"""


def render_report(status: dict[str, Any], spring_repo: Path | None, generated_at: str) -> str:
    layers = status.get("layers") or {}
    failed_layers = set(status.get("failed_layers") or [])
    qa_findings = status.get("qa_findings") or []
    endpoint_verification = status.get("endpoint_verification")
    out_of_scope = status.get("out_of_scope")
    architecture_review = status.get("architecture_review") or {}
    commits = status.get("commits") or {}
    run_metrics = status.get("run_metrics") or {}

    done = sum(1 for e in layers.values() if (e or {}).get("status") == "done")
    total = len(layers)
    blockers = sum(1 for f in qa_findings if f.get("severity") == "blocker")

    summary = (
        "<div class=\"summary-box\">"
        f"<div class=\"stat\"><span class=\"n\">{esc(status.get('current_step', '?'))}</span>"
        "<span class=\"label\">current step</span></div>"
        f"<div class=\"stat\"><span class=\"n\">{done}/{total}</span>"
        "<span class=\"label\">layers done</span></div>"
        f"<div class=\"stat\"><span class=\"n\">{len(failed_layers)}</span>"
        "<span class=\"label\">failed layers</span></div>"
        f"<div class=\"stat\"><span class=\"n\">{blockers}</span>"
        "<span class=\"label\">blocker findings</span></div>"
        + (
            f"<div class=\"stat\"><span class=\"n\">"
            f"{esc(fmt_duration(run_metrics.get('duration_seconds')))}</span>"
            "<span class=\"label\">wall clock</span></div>"
            if run_metrics.get("duration_seconds") is not None else ""
        )
        + "</div>"
    )

    failed_note = ""
    if failed_layers:
        failed_note = (
            "<p><strong>Failed layers:</strong> "
            f"{esc(', '.join(sorted(failed_layers)))} "
            "(exhausted retries; the run continued past them — see "
            ".migration/escalation-&lt;layer&gt;.md for each one).</p>"
        )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Migration report</title>
<style>{CSS}</style>
</head>
<body>
<h1>Play → Spring Boot migration report</h1>
<p class="meta">Generated {esc(generated_at)}</p>
{summary}
{failed_note}
<h2>Layers</h2>
{render_layers_table(layers, failed_layers)}
<h2>QA findings</h2>
{render_findings_table(qa_findings)}
<h2>Endpoint parity (T5)</h2>
{render_endpoint_verification(endpoint_verification)}
<h2>T2 exemptions</h2>
{render_exemptions(architecture_review)}
<h2>Out of scope</h2>
{render_out_of_scope(out_of_scope)}
<h2>Commits</h2>
{render_commits(commits, spring_repo)}
<h2>Run cost</h2>
{render_run_cost(run_metrics)}
</body>
</html>
"""


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Example:\n"
            "  report.py --status-file ../spring/migration-status.json \\\n"
            "      --out ../spring/.migration/report.html\n"
            "\n"
            "Prints the path it wrote. Degrades to blanks on fields an older\n"
            "status file does not carry.\n"
        ),
    )
    parser.add_argument("--status-file", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--spring-repo", type=Path, default=None,
        help="Used to look up commit subjects via git log (default: --status-file's parent dir).",
    )
    parser.add_argument(
        "--token-project", type=Path, default=None,
        help="Manager cwd, used to locate ~/.claude/projects/<slug>/*.jsonl for "
             "token accounting. Defaults to run_metrics.transcript_project, then cwd.",
    )
    parser.add_argument(
        "--session", default=None,
        help="Session uuid to scope token accounting to this run. Defaults to "
             "run_metrics.session_id; without one, every session in the project is "
             "counted and the report says so.",
    )
    args = parser.parse_args()

    status_file = args.status_file.expanduser().resolve()
    if not status_file.is_file():
        print(f"ERROR: no status file at {status_file}", file=sys.stderr)
        return 1

    try:
        status = json.loads(status_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"ERROR: could not parse {status_file}: {e}", file=sys.stderr)
        return 1

    spring_repo = (args.spring_repo.expanduser().resolve() if args.spring_repo
                   else status_file.parent)

    # Token accounting is computed here and rendered, never written back: the
    # manager is the only writer of migration-status.json. A stored snapshot in
    # run_metrics.tokens is honoured as a fallback for the case where the
    # transcripts are no longer on this machine.
    rm = status.setdefault("run_metrics", {})
    project = (
        args.token_project or rm.get("transcript_project") or Path.cwd()
    )
    session = args.session or rm.get("session_id")
    rm["tokens"] = collect_tokens(Path(project), session) or rm.get("tokens")

    out_path = args.out.expanduser().resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_report(status, spring_repo, iso_now()), encoding="utf-8")

    print(str(out_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
