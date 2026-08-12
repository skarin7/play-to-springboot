#!/usr/bin/env python3
"""
T5 endpoint parity: what the API *returns*, before and after the migration.

    # 1. boot the Play app, then:
    python3 scripts/tools/endpoint_diff.py capture --base-url http://localhost:9000 \\
        --probes .migration/endpoint-probes.json --out .migration/responses-play.json

    # 2. boot the Spring app, then:
    python3 scripts/tools/endpoint_diff.py capture --base-url http://localhost:8080 \\
        --probes .migration/endpoint-probes.json --out .migration/responses-spring.json

    # 3.
    python3 scripts/tools/endpoint_diff.py diff \\
        --before .migration/responses-play.json \\
        --after  .migration/responses-spring.json

    # probes can be seeded from conf/routes:
    python3 scripts/tools/endpoint_diff.py probes --routes <play>/conf/routes \\
        --out .migration/endpoint-probes.json

T3 proves an endpoint *exists* -- that some Spring handler answers GET /content.
It says nothing about what comes back. A controller can be reachable, compile,
and keep every method while returning an empty body because a field mapping was
dropped. This tier is the one that would notice.

Comparison is structural by default. Values that legitimately differ between two
runs of the same app -- timestamps, generated ids, durations -- are compared for
presence and type, not equality, or the diff drowns in noise on the first run.
The remaining judgment (is this reordering meaningful? is null-vs-absent a real
change here?) is why the QA agent reads this rather than the manager.

Mutating verbs
--------------

POST/PUT/PATCH/DELETE probes work -- ``fetch`` sends ``body`` with the declared
verb -- but they are seeded **disabled**, because two things this tool cannot
supply have to be true first:

1. **A request body.** ``conf/routes`` records the verb and path, never the
   shape of what the endpoint accepts. Someone who has read the controller
   fills that in.
2. **Identical starting state for both captures.** A POST changes the store it
   writes to, so the second app is answering a different question than the
   first. Point each app at its own disposable datastore, or reset and reseed
   between the two captures.

Probes execute in file order, so a seeding POST followed by the GET that reads
it back works if both those conditions hold. Where they do not, a GET-only
comparison is the honest check, and the mutating paths stay a manual review.

Stdlib only: no requests dependency to install into someone's migration.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

try:
    from routes import normalize_path, parse_play_routes
except ImportError:
    from .routes import normalize_path, parse_play_routes

DEFAULT_TIMEOUT = 15

# Keys whose values change between two runs of the same application. Compared
# for presence and type only -- an equality check on these produces a failing
# diff for every correctly migrated endpoint, which is worse than no check.
VOLATILE_TOKENS = {
    "id", "uuid", "guid", "time", "timestamp", "datetime", "date", "created",
    "updated", "modified", "duration", "elapsed", "took", "latency", "nonce",
    "token", "etag", "session", "trace", "version", "revision",
}

# Split on separators and on camelCase humps, so createdAt, created_at, and
# CreatedAt all tokenize the same way. Matching whole tokens rather than
# substrings keeps "identifier" and "valid" out of the volatile set.
_HUMP_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_SEPARATOR_RE = re.compile(r"[^A-Za-z0-9]+")


def key_tokens(key: str) -> list[str]:
    return [t.lower() for t in _SEPARATOR_RE.split(_HUMP_RE.sub(" ", key)) if t]


def is_volatile(key: str) -> bool:
    return any(token in VOLATILE_TOKENS for token in key_tokens(key))


# --------------------------------------------------------------------------- probes


def build_probes(routes_file: Path, include_parameterised: bool = False) -> dict[str, Any]:
    """
    Seed a probe list from ``conf/routes``.

    Only parameterless GET routes are probed automatically: everything else
    needs a sample value or a request body that only a human or the architect
    can supply. Parameterised routes are emitted as disabled entries with a
    ``path_params`` stub so filling them in is an edit, not a rewrite.
    """
    routes, notes = parse_play_routes(routes_file)
    probes: list[dict[str, Any]] = []
    for route in routes:
        normalized = normalize_path(route.path)
        parameterised = "{}" in normalized or "**" in normalized
        mutating = route.verb not in ("GET", "HEAD")
        enabled = not parameterised and not mutating
        probe: dict[str, Any] = {
            "name": f"{route.verb} {route.path}",
            "verb": route.verb,
            "path": route.path,
            "enabled": enabled or include_parameterised,
        }
        if parameterised:
            probe["path_params"] = {}
            probe["note"] = "fill path_params with sample values, then enable"
        if mutating:
            probe["note"] = (
                "mutating verb: supply a body and point both apps at disposable "
                "state before enabling"
            )
            probe["body"] = None
        probes.append(probe)
    return {"probes": probes, "notes": notes}


def resolve_path(probe: dict[str, Any]) -> str:
    path = probe["path"]
    for name, value in (probe.get("path_params") or {}).items():
        path = path.replace(f":{name}", str(value)).replace(f"{{{name}}}", str(value))
        path = re.sub(rf"\${name}<[^>]*>", str(value), path)
    return path


# --------------------------------------------------------------------------- capture


def wait_for(base_url: str, path: str, attempts: int, delay: float) -> bool:
    for _ in range(attempts):
        try:
            urllib.request.urlopen(base_url.rstrip("/") + path, timeout=5)
            return True
        except urllib.error.HTTPError:
            # Any HTTP response means the server is up; 404 on the health path
            # is still a live server.
            return True
        except (urllib.error.URLError, OSError):
            time.sleep(delay)
    return False


def fetch(base_url: str, probe: dict[str, Any], timeout: int) -> dict[str, Any]:
    url = base_url.rstrip("/") + resolve_path(probe)
    body = probe.get("body")
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=probe["verb"])
    if data is not None:
        request.add_header("Content-Type", "application/json")

    record: dict[str, Any] = {"name": probe["name"], "verb": probe["verb"], "url": url}
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            record["status"] = response.status
            record["content_type"] = (response.headers.get("Content-Type") or "").split(";")[0]
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        # A 4xx/5xx is a real response and must be captured: if Play returned
        # 404 for a path, Spring returning 200 is a difference worth seeing.
        record["status"] = e.code
        record["content_type"] = (e.headers.get("Content-Type") or "").split(";")[0]
        raw = e.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        record["status"] = None
        record["error"] = str(e)
        return record

    record["body_length"] = len(raw)
    try:
        record["json"] = json.loads(raw)
    except json.JSONDecodeError:
        record["text"] = raw[:2000]
    return record


def capture(
    base_url: str, probes: list[dict[str, Any]], timeout: int
) -> list[dict[str, Any]]:
    return [
        fetch(base_url, probe, timeout)
        for probe in probes
        if probe.get("enabled", True)
    ]


# --------------------------------------------------------------------------- diff


def shape(value: Any, path: str = "") -> dict[str, str]:
    """
    Flatten a JSON value into {path: type}, with volatile leaves typed only.

    List elements collapse to index ``[]`` so a three-item list and a four-item
    list compare as the same shape; length is reported separately, where it
    reads as one difference rather than as N missing fields.
    """
    out: dict[str, str] = {}
    if isinstance(value, dict):
        for key in sorted(value):
            out.update(shape(value[key], f"{path}.{key}" if path else key))
    elif isinstance(value, list):
        out[f"{path}[]" if path else "[]"] = "array"
        for item in value:
            out.update(shape(item, f"{path}[]" if path else "[]"))
    else:
        out[path or "(root)"] = type(value).__name__
    return out


def compare_values(before: Any, after: Any, path: str = "") -> list[str]:
    """Value differences at non-volatile leaves only."""
    diffs: list[str] = []
    if isinstance(before, dict) and isinstance(after, dict):
        for key in sorted(set(before) & set(after)):
            if is_volatile(key):
                continue
            diffs.extend(compare_values(before[key], after[key],
                                        f"{path}.{key}" if path else key))
    elif isinstance(before, list) and isinstance(after, list):
        if len(before) != len(after):
            diffs.append(f"{path or '(root)'}: {len(before)} item(s) -> {len(after)}")
        for i, (b, a) in enumerate(zip(before, after)):
            diffs.extend(compare_values(b, a, f"{path}[{i}]"))
    elif before != after:
        diffs.append(f"{path or '(root)'}: {before!r} -> {after!r}")
    return diffs


def compare_one(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    name = before.get("name", "")
    findings: list[dict[str, Any]] = []

    def finding(severity: str, category: str, evidence: str, fix: str) -> None:
        findings.append(
            {
                "layer": "controller",
                "file": name,
                "tier": "T5",
                "severity": severity,
                "category": category,
                "evidence": evidence,
                "suggested_fix": fix,
            }
        )

    if after.get("status") is None:
        finding(
            "blocker", "endpoint-unreachable",
            f"{name}: Play answered {before.get('status')}, Spring did not respond "
            f"({after.get('error')})",
            "check the handler is mapped and the app booted cleanly",
        )
        return {"name": name, "status": "failed", "findings": findings}

    if before.get("status") != after.get("status"):
        finding(
            "blocker", "status-changed",
            f"{name}: HTTP {before.get('status')} -> {after.get('status')}",
            "the handler returns a different outcome than the Play original",
        )

    if before.get("content_type") != after.get("content_type"):
        finding(
            "major", "content-type-changed",
            f"{name}: {before.get('content_type')!r} -> {after.get('content_type')!r}",
            "check the produces/serialisation setting on the handler",
        )

    has_json = "json" in before and "json" in after
    value_diffs: list[str] = []
    added: list[str] = []
    if has_json:
        before_shape, after_shape = shape(before["json"]), shape(after["json"])
        missing = sorted(set(before_shape) - set(after_shape))
        added = sorted(set(after_shape) - set(before_shape))
        retyped = sorted(
            f"{k}: {before_shape[k]} -> {after_shape[k]}"
            for k in set(before_shape) & set(after_shape)
            if before_shape[k] != after_shape[k]
        )
        if missing:
            finding(
                "blocker", "field-missing",
                f"{name}: field(s) absent from the Spring response: "
                + ", ".join(missing[:12]),
                "the response model dropped fields the Play version returned",
            )
        if retyped:
            finding(
                "major", "field-retyped",
                f"{name}: " + "; ".join(retyped[:12]),
                "check the serialisation of these fields",
            )
        value_diffs = compare_values(before["json"], after["json"])
        if value_diffs:
            finding(
                "major", "value-changed",
                f"{name}: " + "; ".join(value_diffs[:12]),
                "confirm whether these differences are expected for this endpoint",
            )
    elif "text" in before and "text" in after:
        if before["text"] != after["text"]:
            finding(
                "major", "body-changed",
                f"{name}: non-JSON body differs "
                f"({before.get('body_length')} bytes -> {after.get('body_length')})",
                "compare the two bodies by hand; this tier cannot judge free text",
            )
    elif ("json" in before) != ("json" in after):
        finding(
            "blocker", "body-kind-changed",
            f"{name}: {'JSON' if 'json' in before else 'text'} -> "
            f"{'JSON' if 'json' in after else 'text'}",
            "the handler changed representation",
        )

    return {
        "name": name,
        "status": "failed" if any(f["severity"] == "blocker" for f in findings)
        else ("needs_review" if findings else "passed"),
        # Extra fields are reported, not flagged: a Spring response may
        # legitimately gain fields the Play version did not carry.
        "added_fields": added,
        "value_diff_count": len(value_diffs),
        "findings": findings,
    }


def diff_captures(
    before: list[dict[str, Any]], after: list[dict[str, Any]]
) -> dict[str, Any]:
    after_by_name = {r.get("name"): r for r in after}
    results: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    not_captured: list[str] = []

    for record in before:
        counterpart = after_by_name.get(record.get("name"))
        if counterpart is None:
            not_captured.append(record.get("name", ""))
            continue
        outcome = compare_one(record, counterpart)
        results.append(outcome)
        findings.extend(outcome["findings"])

    status = "passed"
    if any(f["severity"] == "blocker" for f in findings) or not_captured:
        status = "failed"
    elif findings:
        status = "needs_review"

    return {
        "tier": "T5",
        "status": status,
        "probes_compared": len(results),
        "not_captured_after": not_captured,
        "endpoints": results,
        "findings": findings,
        # Field ordering, null-versus-absent, and "expected" value drift are not
        # decidable here. A QA agent reads these and rules on them.
        "needs_agent": bool(findings),
    }


# --------------------------------------------------------------------------- cli


def load_probes(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["probes"] if isinstance(data, dict) else data


def emit(payload: Any, out: Path | None) -> None:
    text = json.dumps(payload, indent=2) + "\n"
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(f"wrote {out}", file=sys.stderr)
    else:
        sys.stdout.write(text)


def main() -> int:
    parser = argparse.ArgumentParser(description="T5 endpoint response parity.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_probes = sub.add_parser("probes", help="Seed a probe list from conf/routes")
    p_probes.add_argument("--routes", type=Path, required=True)
    p_probes.add_argument("--out", type=Path, default=None)
    p_probes.add_argument("--include-parameterised", action="store_true")

    p_capture = sub.add_parser("capture", help="Record responses from a running app")
    p_capture.add_argument("--base-url", required=True)
    p_capture.add_argument("--probes", type=Path, required=True)
    p_capture.add_argument("--out", type=Path, default=None)
    p_capture.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    p_capture.add_argument("--wait-path", default="/",
                           help="Path polled until the app answers.")
    p_capture.add_argument("--wait-attempts", type=int, default=30)
    p_capture.add_argument("--wait-delay", type=float, default=2.0)

    p_diff = sub.add_parser("diff", help="Compare two captures")
    p_diff.add_argument("--before", type=Path, required=True)
    p_diff.add_argument("--after", type=Path, required=True)
    p_diff.add_argument("--out", type=Path, default=None)

    args = parser.parse_args()

    if args.command == "probes":
        emit(build_probes(args.routes, args.include_parameterised), args.out)
        return 0

    if args.command == "capture":
        if not wait_for(args.base_url, args.wait_path, args.wait_attempts, args.wait_delay):
            print(
                f"ERROR: nothing answering at {args.base_url} after "
                f"{args.wait_attempts} attempts",
                file=sys.stderr,
            )
            return 1
        records = capture(args.base_url, load_probes(args.probes), args.timeout)
        emit({"base_url": args.base_url, "responses": records}, args.out)
        return 0

    before = json.loads(args.before.read_text(encoding="utf-8"))
    after = json.loads(args.after.read_text(encoding="utf-8"))
    result = diff_captures(
        before.get("responses", before), after.get("responses", after)
    )
    emit(result, args.out)
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
