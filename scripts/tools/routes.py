#!/usr/bin/env python3
"""
T3 route parity: Play ``conf/routes`` versus Spring request mappings.

The only check that proves the HTTP surface survived migration. A controller can
compile, keep every method, preserve every statement, and still be unreachable
because the transformer left the method without a mapping annotation. Nothing in
T1 or T2 would notice.

Path comparison is structural, not literal: Play's ``/content/:id`` and Spring's
``/content/{id}`` are the same route, so parameter names are erased before
comparing. Otherwise every parameterised route would report as missing.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, NamedTuple

HTTP_VERBS = ("GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS")

# conf/routes:  GET   /content/:id   controllers.ContentController.show(id: String)
_PLAY_ROUTE_RE = re.compile(
    r"^\s*(" + "|".join(HTTP_VERBS) + r")\s+(\S+)\s+(.+?)\s*$"
)

# Play sub-router include:  ->  /api  api.Routes
_PLAY_INCLUDE_RE = re.compile(r"^\s*->\s+(\S+)\s+(\S+)")

_MAPPING_ANNOTATIONS = {
    "GetMapping": "GET",
    "PostMapping": "POST",
    "PutMapping": "PUT",
    "DeleteMapping": "DELETE",
    "PatchMapping": "PATCH",
}

_CLASS_MAPPING_RE = re.compile(r"@RequestMapping\s*\(([^)]*)\)")
_METHOD_MAPPING_RE = re.compile(
    r"@(" + "|".join(_MAPPING_ANNOTATIONS) + r"|RequestMapping)\s*(?:\(([^)]*)\))?"
)
_CLASS_DECL_RE = re.compile(r"\b(?:class|interface)\s+(\w+)")
_REQUEST_METHOD_RE = re.compile(r"RequestMethod\.(\w+)")
# First string literal inside an annotation's parentheses; covers both
# @GetMapping("/x") and @GetMapping(value = "/x", produces = "...").
_ANNOTATION_PATH_RE = re.compile(r'"([^"]*)"')


class Route(NamedTuple):
    verb: str
    path: str
    handler: str

    def key(self) -> tuple[str, str]:
        return (self.verb, normalize_path(self.path))


def normalize_path(path: str) -> str:
    """
    Reduce a route path to its comparable shape.

    Play writes ``/content/:id`` and ``$id<[^/]+>``; Spring writes
    ``/content/{id}``. Both denote one path parameter, so every parameter
    collapses to ``{}``. Play's ``*file`` and Spring's ``**`` both mean "rest of
    path" and collapse to ``**``.
    """
    p = path.strip()
    if not p.startswith("/"):
        p = "/" + p
    p = re.sub(r"\$\w+<[^>]*>", "{}", p)   # Play regex param: $id<[0-9]+>
    p = re.sub(r":\w+", "{}", p)            # Play simple param: :id
    p = re.sub(r"\*\w+", "**", p)           # Play wildcard: *file
    p = re.sub(r"\{[^}]*\}", "{}", p)       # Spring param: {id}
    p = re.sub(r"/{2,}", "/", p)
    if len(p) > 1 and p.endswith("/"):
        p = p[:-1]
    return p


def parse_play_routes(routes_file: Path) -> tuple[list[Route], list[str]]:
    """Returns (routes, notes). Notes carry sub-router includes, which this
    check cannot follow into."""
    routes: list[Route] = []
    notes: list[str] = []
    if not routes_file.is_file():
        return routes, [f"no routes file at {routes_file}"]

    for raw in routes_file.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        include = _PLAY_INCLUDE_RE.match(line)
        if include:
            notes.append(
                f"sub-router include not followed: {include.group(1)} -> {include.group(2)}"
            )
            continue
        m = _PLAY_ROUTE_RE.match(line)
        if m:
            routes.append(Route(verb=m.group(1), path=m.group(2), handler=m.group(3)))
    return routes, notes


def _annotation_path(args: str | None) -> str:
    if not args:
        return "/"
    found = _ANNOTATION_PATH_RE.search(args)
    return found.group(1) if found else "/"


def _join(base: str, sub: str) -> str:
    if not sub or sub == "/":
        return base or "/"
    if not base or base == "/":
        return sub
    return base.rstrip("/") + "/" + sub.lstrip("/")


def parse_spring_mappings(source_root: Path) -> list[Route]:
    """
    Scan Spring sources for request mappings.

    Text-based rather than AST-based: only annotations matter here, and this
    avoids a second JVM round trip per QA run. Class-level @RequestMapping is
    treated as a prefix for the mappings that follow it in the same file.
    """
    routes: list[Route] = []
    if not source_root.is_dir():
        return routes

    for java_file in sorted(source_root.rglob("*.java")):
        text = java_file.read_text(encoding="utf-8", errors="replace")
        class_name = ""
        base_path = ""

        for match in re.finditer(
            r"@RequestMapping\s*\(([^)]*)\)\s*(?:@\w+\s*(?:\([^)]*\))?\s*)*"
            r"(?:public\s+|final\s+|abstract\s+)*(?:class|interface)\s+(\w+)",
            text,
        ):
            base_path = _annotation_path(match.group(1))
            class_name = match.group(2)
            break

        if not class_name:
            decl = _CLASS_DECL_RE.search(text)
            class_name = decl.group(1) if decl else java_file.stem

        for match in _METHOD_MAPPING_RE.finditer(text):
            annotation, args = match.group(1), match.group(2)

            if annotation == "RequestMapping":
                # Skip the class-level annotation already consumed as the prefix.
                after = text[match.end():match.end() + 200]
                if re.match(r"\s*(?:@\w+\s*(?:\([^)]*\))?\s*)*"
                            r"(?:public\s+|final\s+|abstract\s+)*(?:class|interface)\s",
                            after):
                    continue
                verbs = [
                    v for v in _REQUEST_METHOD_RE.findall(args or "") if v in HTTP_VERBS
                ]
                # A @RequestMapping without an explicit method answers every verb.
                verbs = verbs or list(HTTP_VERBS)
            else:
                verbs = [_MAPPING_ANNOTATIONS[annotation]]

            sub_path = _annotation_path(args)
            full = _join(base_path, sub_path)
            for verb in verbs:
                routes.append(Route(verb=verb, path=full, handler=class_name))

    return routes


def compare_routes(
    play_routes: Iterable[Route],
    spring_routes: Iterable[Route],
) -> dict:
    """
    Missing Play routes are the failure. Extra Spring routes are not: actuator
    endpoints, error handlers, and new health checks legitimately appear.
    """
    play_list = list(play_routes)
    spring_keys = {r.key() for r in spring_routes}

    missing = [r for r in play_list if r.key() not in spring_keys]
    matched = [r for r in play_list if r.key() in spring_keys]

    return {
        "play_route_count": len(play_list),
        "spring_mapping_count": len(spring_keys),
        "matched": len(matched),
        "missing": [
            {"verb": r.verb, "path": r.path, "handler": r.handler} for r in missing
        ],
        "status": "passed" if not missing else "failed",
    }


def main() -> int:
    """Emit a route map as JSON. Used by setup.sh to seed route-map.json."""
    import argparse
    import json
    import sys

    parser = argparse.ArgumentParser(description="Play/Spring route extraction (JSON).")
    parser.add_argument("--routes-file", type=Path, default=None, help="Play conf/routes")
    parser.add_argument("--spring-src", type=Path, default=None, help="Spring src/main/java")
    args = parser.parse_args()

    out: dict = {"play_routes": [], "spring_endpoints": [], "notes": []}
    if args.routes_file:
        play, notes = parse_play_routes(args.routes_file)
        out["play_routes"] = [
            {"verb": r.verb, "path": r.path, "normalized": normalize_path(r.path),
             "handler": r.handler}
            for r in play
        ]
        out["notes"] = notes
    if args.spring_src:
        out["spring_endpoints"] = [
            {"verb": r.verb, "path": r.path, "normalized": normalize_path(r.path),
             "handler": r.handler}
            for r in parse_spring_mappings(args.spring_src)
        ]
    json.dump(out, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
