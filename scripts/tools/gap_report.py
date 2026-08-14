#!/usr/bin/env python3
"""
Turn one migration's gaps into a report safe to hand to the plugin's author.

    python3 scripts/tools/gap_report.py show      --spring-repo S
    python3 scripts/tools/gap_report.py render    --spring-repo S [--out gap-report.md]
    python3 scripts/tools/gap_report.py aggregate --dir ./received-reports

A **gap** is not a finding. A finding says *this migration is wrong*. A gap says
*the plugin had no rule for this, so an agent improvised*. The second kind is
what the plugin author needs and never sees: it lives in a subagent's summary,
in one workspace, on someone else's machine, and dies there. Every defect this
kit has fixed so far was found by reading a transcript by hand.

Redaction is the whole design, because the input is somebody's proprietary
source tree:

**Framework symbols pass through. Everything else is hashed.**
``play.libs.Akka.system`` is Play's public API -- it is the thing worth
reporting, and it belongs to Lightbend, not to the user. ``com.acme.Pricing``
is the user's business and is never needed to act on a gap, so it becomes
``<class:a1b2c3d4>``. The hash is salted per install, so one user's repeated
gaps aggregate together while the same class name in two companies never
collides into a shared identity.

Nothing here uploads anything. There is no network code in this file at all.
``render`` writes a Markdown file the user can read in full and choose to
attach to an issue; that choice is the point. A migration tool that reads
private source and phones home -- even redacted -- is one nobody installs
twice.

``aggregate`` is the author's side: point it at a directory of reports people
sent you and it ranks gaps by how many distinct installs hit them. Two installs
is the signal that a workspace-local override should become a shipped default.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import secrets
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

GAPS_FILE = "gaps.jsonl"
SALT_FILE = "install-salt"
SCHEMA_VERSION = 1

# Package prefixes whose symbols are public API of a framework, not of a user.
# These pass through verbatim: they are exactly what makes a gap actionable,
# and they identify Lightbend/Pivotal/the JDK rather than whoever ran this.
FRAMEWORK_PREFIXES = (
    "play.", "controllers.Assets", "views.html.", "akka.", "scala.",
    "com.typesafe.", "com.google.inject.", "javax.", "jakarta.", "java.",
    "org.springframework.", "org.slf4j.", "org.hibernate.", "com.fasterxml.jackson.",
    "org.junit.", "org.mockito.", "ch.qos.logback.", "io.netty.", "org.apache.",
    "org.mongodb.", "org.neo4j.", "redis.", "sbt.", "org.scalatest.",
)

# Gap kinds. A closed set: an open one turns the corpus into free text, and
# free text cannot be counted, which is the entire purpose of collecting it.
GAP_KINDS = (
    "unmapped_dependency",    # a build.sbt dep with no decided Spring counterpart
    "unhandled_idiom",        # a Play API the transformer and the mapping table miss
    "tier_blind_spot",        # something no tier could verify, and we knew it
    "tool_error",             # a helper crashed or could not run
    "agent_improvised",       # no rule existed; an agent chose something anyway
    "layout_surprise",        # repo shape the classifier did not expect
    "boot_failure",           # T5 could not start an app
)

_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+")
# Maven/Ivy coordinates are public artifacts; a version tells the author which
# Play generation produced the gap.
_COORD_RE = re.compile(r"^[\w.\-]+:[\w.\-]+(?::[\w.\-]+)?$")

# Bare words in free text. ``what_i_did`` is written by a language model and can
# contain anything it happened to be looking at -- a class name without a
# package, an internal ticket id, an API key pasted from a config file. Dotted
# identifiers alone are not enough of a net.
_WORD_RE = re.compile(r"[A-Za-z0-9_@$][A-Za-z0-9_@$\-]*")

# Simple type and keyword names that are framework vocabulary rather than
# anyone's code. Unknown CamelCase is assumed to be the user's, so this list
# being incomplete costs readability, never safety.
SAFE_SIMPLE_NAMES = frozenset({
    # this kit's own vocabulary
    "T1", "T2", "T3", "T4", "T5", "GET", "POST", "PUT", "DELETE", "PATCH",
    "HEAD", "OPTIONS", "HTTP", "HTTPS", "JSON", "XML", "YAML", "PATH", "JVM",
    "API", "URL", "URI", "CLI", "SQL", "TODO", "NOTE", "OK",
    # Play
    "Play", "Result", "Controller", "Action", "Assets", "Filters", "Module",
    "ErrorHandler", "EssentialFilter", "Http", "Context", "WSClient", "Promise",
    "Twirl", "Guice", "Akka", "Ebean", "Anorm", "Scala", "Java", "Maven",
    # Spring / jakarta / JDK
    "Spring", "SpringBoot", "RestController", "Service", "Component",
    "Repository", "Configuration", "Bean", "Autowired", "ResponseEntity",
    "RequestMapping", "GetMapping", "PostMapping", "PutMapping",
    "DeleteMapping", "PatchMapping", "PathVariable", "RequestBody",
    "RequestParam", "ControllerAdvice", "ExceptionHandler", "Filter",
    "FilterChain", "WebMvcConfigurer", "RouterFunction", "RequestContextHolder",
    "PreDestroy", "PostConstruct", "Async", "Transactional", "Entity",
    "CompletableFuture", "CompletionStage", "Optional", "String", "List", "Map",
    "Jackson", "SLF4J", "Logback", "Hibernate", "Mongo", "MongoDB", "Neo4j",
    "Redis", "Thymeleaf", "Docker",
})

# Belt and braces: shapes that are secrets no matter what else the heuristic
# concludes. Checked first so a key can never fall through as "just a word".
_SECRET_RES = (
    re.compile(r"\b(?:AKIA|ASIA|AIza|ghp_|gho_|github_pat_|xox[baprs]-|sk-)[A-Za-z0-9_\-]{4,}"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]+\.?[A-Za-z0-9_\-]*"),  # JWT
    re.compile(r"\b[A-Fa-f0-9]{32,}\b"),                                          # hex secret/hash
    # [^\s<] keeps this from eating a placeholder an earlier pattern produced.
    re.compile(r"(?i)\b(?:password|passwd|secret|token|api[_-]?key|bearer)\b\s*[:=]?\s*[^\s<]\S*"),
)


def _looks_like_an_identifier(word: str) -> bool:
    """
    CamelCase, digits mixed into letters, underscores, or a long shout.

    Sentence prose survives: 'Hand-ported', 'The', 'Spring' have one leading
    capital and nothing else, so they are kept. 'RealtimeScoringEngine',
    'AKIA1234', 'JIRA', and 'rate_card_v2' do not.
    """
    if word in SAFE_SIMPLE_NAMES:
        return False
    if "_" in word or "$" in word:
        return True
    has_alpha = any(c.isalpha() for c in word)
    has_digit = any(c.isdigit() for c in word)
    if has_alpha and has_digit:
        return True
    if word.isupper() and len(word) >= 4:
        return True
    # An internal capital after the first character: FooBar, iOSThing.
    return any(c.isupper() for c in word[1:])


def iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- salt


def salt_path() -> Path:
    base = os.environ.get("CLAUDE_PLUGIN_DATA") or str(Path.home() / ".cache" / "play-to-springboot")
    return Path(base) / SALT_FILE


def install_salt() -> str:
    """
    A random per-install value, created once and reused.

    It is what makes hashing useful rather than merely safe: the same class
    hashes to the same token across this user's runs (so "hit four times" is
    visible) and to a different token on any other machine (so two companies'
    ``UserService`` never merge into one identity).
    """
    path = salt_path()
    try:
        if path.is_file():
            existing = path.read_text(encoding="utf-8").strip()
            if existing:
                return existing
        path.parent.mkdir(parents=True, exist_ok=True)
        value = secrets.token_hex(16)
        path.write_text(value + "\n", encoding="utf-8")
        return value
    except OSError:
        # An unwritable cache dir must not stop a report being produced; a
        # per-process salt still redacts, it just cannot correlate across runs.
        return secrets.token_hex(16)


def install_id(salt: str) -> str:
    """Stable, anonymous, derived from the salt -- never from a hostname or user."""
    return hashlib.sha256(("install:" + salt).encode()).hexdigest()[:12]


# --------------------------------------------------------------------------- redaction


def is_framework(symbol: str) -> bool:
    return symbol.startswith(FRAMEWORK_PREFIXES)


def hash_token(token: str, salt: str, kind: str = "sym") -> str:
    digest = hashlib.sha256((salt + "|" + token).encode()).hexdigest()[:8]
    return f"<{kind}:{digest}>"


def redact_symbol(symbol: str, salt: str) -> str:
    """One dotted identifier: framework verbatim, anything else hashed."""
    if is_framework(symbol):
        return symbol
    if _COORD_RE.match(symbol):
        return symbol
    return hash_token(symbol, salt, "class")


def redact_text(text: str, salt: str) -> str:
    """
    Redact free text without trusting a word of it.

    Four passes, narrowest net last:

    1. **Secrets by shape** -- an API key is a secret whatever else it looks
       like, so it is caught before any other rule can decide it is a word.
    2. **Paths** -- they carry usernames, employers, and project names.
    3. **Dotted identifiers** -- framework verbatim, everything else hashed.
    4. **Bare words** -- because ``what_i_did`` is model-written prose that may
       name a class with no package at all. Anything shaped like an identifier
       is hashed; ordinary prose survives.

    Pass 4 is what makes this honest rather than approximate. Without it a
    line like "copied logic from Scoring, API key AKIA1234" passes through
    untouched: neither token contains a dot.
    """
    if not text:
        return ""
    for pattern in _SECRET_RES:
        text = pattern.sub(lambda m: hash_token(m.group(0), salt, "redacted"), text)
    # Absolute paths: keep the shape, never the tree.
    text = re.sub(r"(/[\w.\-]+){2,}", lambda m: hash_token(m.group(0), salt, "path"), text)
    text = re.sub(r"[A-Za-z]:\\[\w\\.\-]+", lambda m: hash_token(m.group(0), salt, "path"), text)
    text = _IDENT_RE.sub(lambda m: redact_symbol(m.group(0), salt), text)

    def _word(match: re.Match) -> str:
        word = match.group(0)
        if word.startswith("<") or _is_placeholder_fragment(word):
            return word
        # @Async and Async are the same piece of framework vocabulary.
        if word.lstrip("@") in SAFE_SIMPLE_NAMES:
            return word
        if _looks_like_an_identifier(word):
            return hash_token(word, salt, "sym")
        return word

    return _WORD_RE.sub(_word, text)


# Deliberately not "secret"/"token": the secret patterns match those very
# words, so a placeholder containing one would be re-matched by the next pass.
_PLACEHOLDER_FRAGMENTS = frozenset({"class", "path", "sym", "redacted"})


def _is_placeholder_fragment(word: str) -> bool:
    """Don't re-redact the ``<class:a1b2c3d4>`` tokens earlier passes produced."""
    return word in _PLACEHOLDER_FRAGMENTS or re.fullmatch(r"[0-9a-f]{8}", word) is not None


def redact_gap(gap: dict[str, Any], salt: str) -> dict[str, Any]:
    """
    Whitelist the fields that leave this machine. Anything not named here is
    dropped, so a future field added by an agent cannot leak by default.
    """
    kind = gap.get("kind")
    if kind not in GAP_KINDS:
        kind = "agent_improvised"
    return {
        "kind": kind,
        "subject": redact_text(str(gap.get("subject", ""))[:200], salt),
        "role": str(gap.get("role", ""))[:20],
        "what_i_did": redact_text(str(gap.get("what_i_did", ""))[:300], salt),
        "blind_tier": gap.get("blind_tier") if gap.get("blind_tier") in
        ("T1", "T2", "T3", "T4", "T5", None) else None,
        "layer": str(gap.get("layer", ""))[:20],
    }


# --------------------------------------------------------------------------- inputs


def read_gaps(spring_repo: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """Returns (gaps, warnings). A malformed line is skipped, never fatal."""
    path = spring_repo / ".migration" / GAPS_FILE
    gaps: list[dict[str, Any]] = []
    warnings: list[str] = []
    if not path.is_file():
        return gaps, [f"no gap journal at {path}"]
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            warnings.append("skipped a malformed gap line")
            continue
        if isinstance(entry, dict):
            gaps.append(entry)
    return gaps, warnings


def read_status(spring_repo: Path) -> dict[str, Any]:
    path = spring_repo / "migration-status.json"
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def environment() -> dict[str, Any]:
    """Shape of the machine, not its identity."""
    return {
        "python": platform.python_version(),
        "os": platform.system(),
        "arch": platform.machine(),
    }


def shape_of_run(status: dict[str, Any]) -> dict[str, Any]:
    """
    Counts only. No file names, no findings text, no repo name.

    This is also the half the *user* wants -- "why did that take so long" --
    which is what gets the report opened at all.
    """
    layers = status.get("layers") or {}
    findings = status.get("qa_findings") or []
    inventory = status.get("source_inventory") or {}
    play = inventory.get("play") or {}
    out_of_scope = status.get("out_of_scope") or {}

    return {
        "mode": status.get("mode"),
        "play_java_files": play.get("total_java_files"),
        "by_layer": play.get("by_layer") or {},
        "layers": {
            name: {
                "status": (entry or {}).get("status"),
                "files_migrated": (entry or {}).get("files_migrated", 0),
                "batches": (entry or {}).get("batches_completed", 0),
                "attempts": ((status.get("attempts") or {}).get(name) or {}).get("count", 0),
            }
            for name, entry in layers.items()
        },
        "failed_layers": len(status.get("failed_layers") or []),
        "findings_by_tier": dict(Counter(f.get("tier") for f in findings)),
        "findings_by_severity": dict(Counter(f.get("severity") for f in findings)),
        "findings_by_category": dict(Counter(f.get("category") for f in findings)),
        "out_of_scope_total": out_of_scope.get("total_files", 0),
        "exemptions": len((status.get("architecture_review") or {}).get("exemptions") or []),
        "endpoint_verification": (status.get("endpoint_verification") or {}).get("status"),
    }


def plugin_version() -> str | None:
    manifest = Path(__file__).resolve().parents[2] / ".claude-plugin" / "plugin.json"
    try:
        return json.loads(manifest.read_text(encoding="utf-8")).get("version")
    except (OSError, json.JSONDecodeError):
        return None


def build_report(spring_repo: Path) -> dict[str, Any]:
    salt = install_salt()
    gaps, warnings = read_gaps(spring_repo)
    status = read_status(spring_repo)
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": iso_now(),
        "install_id": install_id(salt),
        "plugin_version": plugin_version(),
        "redacted": True,
        "environment": environment(),
        "run": shape_of_run(status),
        "gaps": [redact_gap(g, salt) for g in gaps],
        "gap_counts": dict(Counter(
            g["kind"] for g in (redact_gap(x, salt) for x in gaps)
        )),
        "warnings": warnings,
    }


# --------------------------------------------------------------------------- render


def _or_dash(value: Any) -> str:
    """A report from a run that never got far still has to read cleanly."""
    return "—" if value in (None, "", {}) else str(value)


def render_markdown(report: dict[str, Any]) -> str:
    run = report.get("run") or {}
    env = report.get("environment") or {}
    lines = [
        "## Play-to-Spring Boot migration — gap report",
        "",
        f"- plugin `{_or_dash(report.get('plugin_version'))}` · install "
        f"`{report.get('install_id')}` · {report.get('generated_at')}",
        f"- {_or_dash(run.get('play_java_files'))} Play Java files, mode "
        f"`{_or_dash(run.get('mode'))}`, python {_or_dash(env.get('python'))} on "
        f"{_or_dash(env.get('os'))}",
        "",
        "Class and package names from this repo are replaced with salted hashes.",
        "Framework symbols (`play.*`, `org.springframework.*`, Maven coordinates)",
        "are shown as-is. No source, file paths, or finding text is included.",
        "",
        "### Gaps",
        "",
    ]

    gaps = report.get("gaps") or []
    if not gaps:
        lines.append("_None recorded._")
    else:
        lines += ["| Kind | Subject | Role | Blind tier | What the agent did |",
                  "|---|---|---|---|---|"]
        for g in gaps:
            lines.append(
                f"| `{g['kind']}` | {g['subject'] or '—'} | {g['role'] or '—'} "
                f"| {g['blind_tier'] or '—'} | {g['what_i_did'] or '—'} |"
            )

    lines += ["", "### Run shape", "", "```json",
              json.dumps(run, indent=2), "```", ""]

    if report.get("warnings"):
        lines += ["### Warnings", ""] + [f"- {w}" for w in report["warnings"]] + [""]

    lines += [
        "<details><summary>Raw JSON (what you would be sharing)</summary>",
        "",
        "```json",
        json.dumps(report, indent=2),
        "```",
        "",
        "</details>",
        "",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- aggregate


def load_reports(directory: Path) -> list[dict[str, Any]]:
    reports = []
    for path in sorted(directory.rglob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and "gaps" in data:
            reports.append(data)
    return reports


def aggregate(reports: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """
    Rank gaps by how many *distinct installs* hit them.

    Distinct installs, not raw occurrences: one user running the same repo forty
    times is one signal, not forty. Two installs is the line where a
    workspace-local override has earned promotion to a shipped default.
    """
    installs: dict[tuple[str, str], set[str]] = defaultdict(set)
    occurrences: Counter = Counter()
    versions: dict[tuple[str, str], set[str]] = defaultdict(set)
    report_list = list(reports)

    for report in report_list:
        who = report.get("install_id", "unknown")
        for gap in report.get("gaps") or []:
            key = (gap.get("kind", "?"), gap.get("subject", ""))
            installs[key].add(who)
            occurrences[key] += 1
            if report.get("plugin_version"):
                versions[key].add(report["plugin_version"])

    ranked = sorted(
        (
            {
                "kind": kind,
                "subject": subject,
                "installs": len(who),
                "occurrences": occurrences[(kind, subject)],
                "plugin_versions": sorted(versions[(kind, subject)]),
                "promote": len(who) >= 2,
            }
            for (kind, subject), who in installs.items()
        ),
        key=lambda r: (-r["installs"], -r["occurrences"], r["kind"]),
    )

    return {
        "reports": len(report_list),
        "installs": len({r.get("install_id") for r in report_list}),
        "distinct_gaps": len(ranked),
        "promotable": [r for r in ranked if r["promote"]],
        "ranked": ranked,
    }


def render_aggregate(summary: dict[str, Any]) -> str:
    lines = [
        f"{summary['reports']} report(s) from {summary['installs']} install(s), "
        f"{summary['distinct_gaps']} distinct gap(s).",
        "",
        f"{len(summary['promotable'])} gap(s) seen by 2+ installs — these have earned "
        "a shipped default plus a fixture:",
        "",
        "| Installs | Occurrences | Kind | Subject |",
        "|---|---|---|---|",
    ]
    for row in summary["ranked"]:
        marker = "**" if row["promote"] else ""
        lines.append(
            f"| {marker}{row['installs']}{marker} | {row['occurrences']} "
            f"| `{row['kind']}` | {row['subject'] or '—'} |"
        )
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- CLI


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Redacted gap reporting for the play-to-springboot plugin.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  gap_report.py show      --spring-repo ../spring-app\n"
            "  gap_report.py render    --spring-repo ../spring-app "
            "--out gap-report.md\n"
            "  gap_report.py aggregate --dir ./received-reports\n"
            "\n"
            "Nothing is uploaded: there is no network code in this tool. 'render'\n"
            "writes a file you can read in full and choose to attach to an issue.\n"
            "Class and package names are replaced with per-install salted hashes;\n"
            "framework symbols and Maven coordinates are kept, because those are\n"
            "what make a gap actionable and they are nobody's private data.\n"
        ),
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_show = sub.add_parser("show", help="Print the redacted report as JSON.")
    p_show.add_argument("--spring-repo", type=Path, required=True)

    p_render = sub.add_parser("render", help="Write the report as Markdown.")
    p_render.add_argument("--spring-repo", type=Path, required=True)
    p_render.add_argument(
        "--out", type=Path, default=None,
        help="Default: <spring-repo>/.migration/gap-report.md",
    )

    p_agg = sub.add_parser(
        "aggregate", help="Plugin author's side: rank gaps across received reports."
    )
    p_agg.add_argument("--dir", type=Path, required=True)
    p_agg.add_argument("--json", action="store_true", help="Emit JSON instead of a table.")

    args = parser.parse_args()

    if args.cmd == "aggregate":
        directory = args.dir.expanduser().resolve()
        if not directory.is_dir():
            print(f"ERROR: no such directory: {directory}", file=sys.stderr)
            return 1
        summary = aggregate(load_reports(directory))
        if args.json:
            json.dump(summary, sys.stdout, indent=2)
            sys.stdout.write("\n")
        else:
            sys.stdout.write(render_aggregate(summary))
        return 0

    spring_repo = args.spring_repo.expanduser().resolve()
    report = build_report(spring_repo)

    if args.cmd == "show":
        json.dump(report, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0

    out = args.out or (spring_repo / ".migration" / "gap-report.md")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_markdown(report), encoding="utf-8")
    print(str(out))
    print(
        f"{len(report['gaps'])} gap(s) recorded. Read it before sharing — it is "
        "redacted, and it is yours to send or not.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
