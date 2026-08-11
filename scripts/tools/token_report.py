#!/usr/bin/env python3
"""
Token and cost accounting for a migration run.

    python3 scripts/tools/token_report.py                    # this project, all sessions
    python3 scripts/tools/token_report.py --session <uuid>   # one run
    python3 scripts/tools/token_report.py --by-agent         # split main thread vs subagents

Reads Claude Code's session transcripts under
``~/.claude/projects/<slug>/*.jsonl``. Each assistant message carries a ``usage``
block, so this is measured consumption rather than an estimate.

Why per-agent matters: the point of dispatching subagents is that expensive
context -- compile logs, Java sources -- lives and dies in a disposable context
while the manager stays small. This report is how you confirm that actually
happened. A manager whose share of input tokens keeps climbing across layers
means the handoff rules leaked and the run will not scale to a larger repo.

Cache reads are billed at a fraction of fresh input, so they are reported
separately; a long run should show cache reads dominating input.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

# Opus 5 list pricing, USD per million tokens. Override with --price-* if your
# plan differs; the token counts above are exact regardless.
DEFAULT_INPUT_PRICE = 5.00
DEFAULT_OUTPUT_PRICE = 25.00
DEFAULT_CACHE_WRITE_PRICE = 6.25
DEFAULT_CACHE_READ_PRICE = 0.50


def project_slug(path: Path) -> str:
    """Claude Code flattens the project path, mapping both / and _ to -."""
    return str(path.resolve()).replace("/", "-").replace("_", "-")


def transcript_dir(project: Path) -> Path:
    root = Path.home() / ".claude" / "projects"
    exact = root / project_slug(project)
    if exact.is_dir():
        return exact
    # Fall back to a suffix match on the directory name, so an unexpected
    # slug rule does not silently report "no data".
    tail = project.resolve().name.replace("_", "-")
    for candidate in sorted(root.glob(f"*{tail}")):
        if candidate.is_dir():
            return candidate
    return exact


def iter_usage(jsonl: Path):
    """Yield (agent_label, usage) for each assistant message with usage data."""
    for line in jsonl.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        message = entry.get("message") or {}
        usage = message.get("usage")
        if not usage:
            continue
        # Subagent turns are tagged with the sidechain flag; everything else is
        # the main thread (the manager).
        label = "subagent" if entry.get("isSidechain") else "main"
        yield label, usage


def blank() -> dict[str, int]:
    return {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0, "turns": 0}


def accumulate(target: dict[str, int], usage: dict[str, Any]) -> None:
    target["input"] += int(usage.get("input_tokens") or 0)
    target["output"] += int(usage.get("output_tokens") or 0)
    target["cache_write"] += int(usage.get("cache_creation_input_tokens") or 0)
    target["cache_read"] += int(usage.get("cache_read_input_tokens") or 0)
    target["turns"] += 1


def cost(totals: dict[str, int], prices: dict[str, float]) -> float:
    return (
        totals["input"] / 1e6 * prices["input"]
        + totals["output"] / 1e6 * prices["output"]
        + totals["cache_write"] / 1e6 * prices["cache_write"]
        + totals["cache_read"] / 1e6 * prices["cache_read"]
    ) if totals else 0.0


def fmt(n: int) -> str:
    return f"{n:,}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Token/cost report for a migration run.")
    parser.add_argument("--project", type=Path, default=Path.cwd())
    parser.add_argument("--session", default=None, help="Session UUID (default: all)")
    parser.add_argument("--by-agent", action="store_true",
                        help="Split main thread vs subagents")
    parser.add_argument("--json", action="store_true", help="Machine-readable output")
    parser.add_argument("--price-input", type=float, default=DEFAULT_INPUT_PRICE)
    parser.add_argument("--price-output", type=float, default=DEFAULT_OUTPUT_PRICE)
    parser.add_argument("--price-cache-write", type=float, default=DEFAULT_CACHE_WRITE_PRICE)
    parser.add_argument("--price-cache-read", type=float, default=DEFAULT_CACHE_READ_PRICE)
    args = parser.parse_args()

    prices = {
        "input": args.price_input,
        "output": args.price_output,
        "cache_write": args.price_cache_write,
        "cache_read": args.price_cache_read,
    }

    tdir = transcript_dir(args.project)
    if not tdir.is_dir():
        print(f"No transcripts for {args.project} (looked in {tdir})", file=sys.stderr)
        return 1

    files = (
        [tdir / f"{args.session}.jsonl"] if args.session
        else sorted(tdir.glob("*.jsonl"))
    )
    files = [f for f in files if f.is_file()]
    if not files:
        print(f"No session transcripts found in {tdir}", file=sys.stderr)
        return 1

    by_agent: dict[str, dict[str, int]] = defaultdict(blank)
    by_session: dict[str, dict[str, int]] = defaultdict(blank)

    for jsonl in files:
        for label, usage in iter_usage(jsonl):
            accumulate(by_agent[label], usage)
            accumulate(by_session[jsonl.stem], usage)

    total = blank()
    for entry in by_agent.values():
        for k in total:
            total[k] += entry[k]

    if args.json:
        json.dump(
            {
                "sessions": {k: dict(v, cost_usd=round(cost(v, prices), 2))
                             for k, v in by_session.items()},
                "by_agent": {k: dict(v, cost_usd=round(cost(v, prices), 2))
                             for k, v in by_agent.items()},
                "total": dict(total, cost_usd=round(cost(total, prices), 2)),
            },
            sys.stdout, indent=2,
        )
        sys.stdout.write("\n")
        return 0

    print(f"Project:  {args.project}")
    print(f"Sessions: {len(files)}\n")
    header = f"{'':<12} {'turns':>7} {'input':>12} {'cache rd':>12} {'cache wr':>12} {'output':>10} {'USD':>9}"
    print(header)
    print("-" * len(header))

    def row(label: str, t: dict[str, int]) -> None:
        print(f"{label:<12} {t['turns']:>7} {fmt(t['input']):>12} "
              f"{fmt(t['cache_read']):>12} {fmt(t['cache_write']):>12} "
              f"{fmt(t['output']):>10} {cost(t, prices):>9.2f}")

    if args.by_agent:
        for label in ("main", "subagent"):
            if by_agent.get(label):
                row(label, by_agent[label])
    else:
        for name, t in sorted(by_session.items()):
            row(name[:12], t)

    print("-" * len(header))
    row("TOTAL", total)

    if args.by_agent and by_agent.get("main") and by_agent.get("subagent"):
        main_in = by_agent["main"]["input"] + by_agent["main"]["cache_read"]
        sub_in = by_agent["subagent"]["input"] + by_agent["subagent"]["cache_read"]
        share = main_in / (main_in + sub_in) * 100 if (main_in + sub_in) else 0
        print(f"\nManager share of input: {share:.1f}%")
        print("Expect this to fall as the repo grows. A rising share means the")
        print("manager is ingesting source or build logs -- see docs/STATE-CONTRACT.md.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
