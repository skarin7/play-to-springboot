#!/usr/bin/env python3
"""
Render a self-contained HTML report from migration-status.json.

    python3 scripts/tools/report.py --status-file <spring-repo>/migration-status.json \
        --out <spring-repo>/.migration/report.html

Pure function of the status file -- no schema changes needed, every field
this reads (``layers``, ``failed_layers``, ``qa_findings``,
``endpoint_verification``, ``commits``, ``attempts``) already exists. One
HTML file, inline CSS, no external assets, no server: it has to open
standalone in a browser days after the run that produced it.

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
        "</div>"
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

    out_path = args.out.expanduser().resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_report(status, spring_repo, iso_now()), encoding="utf-8")

    print(str(out_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
