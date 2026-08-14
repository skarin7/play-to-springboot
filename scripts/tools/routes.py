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

Two things are *not* controller mappings and must not be reported as missing
ones:

* **Play's built-in asset routes** (``GET /assets/*file ->
  controllers.Assets.at``). Spring serves static content through configuration
  -- ``spring.mvc.static-path-pattern``, a ``WebMvcConfigurer`` resource
  handler, or just the framework default -- none of which is an annotation this
  file can see. Reporting them as missing forces someone to hand-write a
  passthrough controller whose only purpose is to be found by a regex.
* **Twirl view handlers** (``views.html.*``). Templates are out of scope for the
  migration entirely; see the ``out_of_scope`` block in migration-status.json.

Both are classified rather than dropped: they appear in the result under
``out_of_scope`` / ``matched_by_static`` so a reader can see the decision was
made, not that the route vanished. ``assets_policy="require"`` restores the
strict behaviour for a project that really did hand-migrate its assets.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable, NamedTuple

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
    # Spring's own catch-all, {*file}, means the same as Play's *file. It has to
    # collapse before *both* rules below: the Play wildcard rule would rewrite
    # its inner *file and leave {**}, and the generic {id} rule would then flatten
    # that to a single-segment {} -- making /assets/** unmatchable by construction.
    p = re.sub(r"\{\*\w+\}", "**", p)       # Spring catch-all: {*file}
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


# --------------------------------------------------------------------------- out of scope


# Play's built-in asset controllers, plus Twirl view handlers. Matched against
# the handler string in conf/routes, which is the only place this information
# exists -- the path alone cannot tell you /assets/*file is framework glue.
_OUT_OF_SCOPE_HANDLERS = (
    ("controllers.Assets.", "Play built-in asset controller; Spring serves static "
                            "content from configuration, not from a mapping"),
    ("controllers.ExternalAssets.", "Play external asset controller; static content "
                                    "is served from configuration in Spring"),
    ("Assets.versioned", "Play asset fingerprinting; no Spring controller equivalent"),
    ("Assets.at", "Play static asset handler; no Spring controller equivalent"),
    ("views.html.", "Twirl view handler; templates are out of scope for this migration"),
)


def out_of_scope_reason(route: Route) -> str | None:
    """Why this Play route has no Spring *mapping* by design, or None."""
    handler = route.handler or ""
    for needle, reason in _OUT_OF_SCOPE_HANDLERS:
        if needle in handler:
            return reason
    return None


# --------------------------------------------------------------------------- static handlers


_STATIC_PATH_PATTERN_RE = re.compile(
    r"^\s*spring\.mvc\.static-path-pattern\s*[:=]\s*(\S+)", re.MULTILINE
)
_YAML_STATIC_PATTERN_RE = re.compile(
    r"static-path-pattern\s*:\s*[\"']?([^\s\"']+)", re.MULTILINE
)
_RESOURCE_HANDLER_RE = re.compile(r"addResourceHandler\s*\(([^)]*)\)")
_ROUTER_GET_RE = re.compile(r"\bGET\s*\(\s*\"([^\"]+)\"")

# Spring Boot serves /** from classpath:/static, /public, /resources and
# /META-INF/resources unless something overrides the pattern. A project that
# never mentions static resources still serves them -- which is precisely why
# annotation scanning alone concludes, wrongly, that /assets/*file is missing.
SPRING_DEFAULT_STATIC_PATTERN = "/**"


def parse_static_resource_handlers(spring_repo: Path) -> list[str]:
    """
    Return the URL patterns this Spring project serves static content at.

    Three mechanisms, none of them visible to annotation scanning:
    ``spring.mvc.static-path-pattern`` in properties/yml, an explicit
    ``addResourceHandler(...)`` in a ``WebMvcConfigurer``, and a
    ``RouterFunctions`` GET route. Falls back to Boot's own default, because a
    project that configures nothing still serves ``/**``.
    """
    if not spring_repo.is_dir():
        return [SPRING_DEFAULT_STATIC_PATTERN]

    patterns: list[str] = []
    resources = spring_repo / "src" / "main" / "resources"
    for name in ("application.properties", "application.yml", "application.yaml"):
        f = resources / name
        if not f.is_file():
            continue
        text = f.read_text(encoding="utf-8", errors="replace")
        patterns += [m.strip().strip('"\'') for m in _STATIC_PATH_PATTERN_RE.findall(text)]
        if name != "application.properties":
            patterns += [m.strip() for m in _YAML_STATIC_PATTERN_RE.findall(text)]

    source_root = spring_repo / "src" / "main" / "java"
    if source_root.is_dir():
        for java_file in sorted(source_root.rglob("*.java")):
            text = java_file.read_text(encoding="utf-8", errors="replace")
            if "addResourceHandler" in text:
                for args in _RESOURCE_HANDLER_RE.findall(text):
                    patterns += _ANNOTATION_PATH_RE.findall(args)
            if "RouterFunctions" in text or "RouterFunction" in text:
                patterns += _ROUTER_GET_RE.findall(text)

    if not patterns:
        patterns = [SPRING_DEFAULT_STATIC_PATTERN]
    # Deduplicate on the normalized form, keeping declaration order.
    seen: set[str] = set()
    out: list[str] = []
    for p in patterns:
        n = normalize_path(p)
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


# A pattern that matches everything proves nothing about any particular route.
# Boot's default is exactly that, so it is reported for context but never used
# to excuse a missing route -- otherwise every unmigrated GET controller would
# come back "served by static resources" and T3 would go permanently green.
_UNIVERSAL_PATTERNS = {"/**", "**", "/"}


def _static_match(normalized_route: str, normalized_pattern: str) -> bool:
    """Does a *specific* static resource pattern cover this route path?"""
    if normalized_pattern in _UNIVERSAL_PATTERNS:
        return False
    if normalized_pattern == normalized_route:
        return True
    if normalized_pattern.endswith("/**"):
        prefix = normalized_pattern[: -len("/**")]
        return normalized_route == prefix or normalized_route.startswith(prefix + "/")
    return False


def compare_routes(
    play_routes: Iterable[Route],
    spring_routes: Iterable[Route],
    static_routes: Iterable[str] = (),
    assets_policy: str = "skip",
) -> dict:
    """
    Missing Play routes are the failure. Extra Spring routes are not: actuator
    endpoints, error handlers, and new health checks legitimately appear.

    Before a route counts as missing it gets two more chances, because two
    classes of Play route have no Spring *mapping* by design:

    ``out_of_scope``       framework glue (assets, Twirl views) -- classified by
                           handler, always reported, never a finding under the
                           default ``assets_policy="skip"``.
    ``matched_by_static``  covered by a Spring static-resource pattern that
                           annotation scanning cannot see.

    ``missing`` therefore keeps only genuine gaps, which is what makes it worth
    acting on. Set ``assets_policy="require"`` to demand real mappings for
    everything, for a project that deliberately hand-migrated its assets.
    """
    play_list = list(play_routes)
    spring_keys = {r.key() for r in spring_routes}
    static_patterns = list(static_routes)

    matched: list[Route] = []
    out_of_scope: list[dict[str, Any]] = []
    matched_by_static: list[dict[str, Any]] = []
    missing: list[Route] = []

    for r in play_list:
        if r.key() in spring_keys:
            matched.append(r)
            continue

        reason = out_of_scope_reason(r) if assets_policy != "require" else None
        if reason:
            out_of_scope.append(
                {"verb": r.verb, "path": r.path, "handler": r.handler, "reason": reason}
            )
            continue

        if assets_policy != "require" and r.verb in ("GET", "HEAD"):
            normalized = normalize_path(r.path)
            hit = next(
                (p for p in static_patterns if _static_match(normalized, p)), None
            )
            if hit:
                matched_by_static.append(
                    {"verb": r.verb, "path": r.path, "handler": r.handler,
                     "pattern": hit}
                )
                continue

        missing.append(r)

    return {
        "play_route_count": len(play_list),
        "spring_mapping_count": len(spring_keys),
        "matched": len(matched),
        "missing": [
            {"verb": r.verb, "path": r.path, "handler": r.handler} for r in missing
        ],
        "out_of_scope": out_of_scope,
        "matched_by_static": matched_by_static,
        "static_patterns": static_patterns,
        "assets_policy": assets_policy,
        "status": "passed" if not missing else "failed",
    }


def main() -> int:
    """Emit a route map as JSON. Used by setup.sh to seed route-map.json."""
    import argparse
    import json
    import sys

    parser = argparse.ArgumentParser(
        description="Play/Spring route extraction (JSON).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  routes.py --routes-file ../play-app/conf/routes\n"
            "  routes.py --routes-file ../play-app/conf/routes "
            "--spring-src ../spring-app/src/main/java\n"
            "  routes.py --spring-repo ../spring-app   # static resource patterns only\n"
        ),
    )
    parser.add_argument("--routes-file", type=Path, default=None, help="Play conf/routes")
    parser.add_argument("--spring-src", type=Path, default=None, help="Spring src/main/java")
    parser.add_argument(
        "--spring-repo", type=Path, default=None,
        help="Spring project root; reports the static-resource patterns it serves.",
    )
    args = parser.parse_args()

    out: dict = {"play_routes": [], "spring_endpoints": [], "notes": []}
    if args.routes_file:
        play, notes = parse_play_routes(args.routes_file)
        out["play_routes"] = [
            {"verb": r.verb, "path": r.path, "normalized": normalize_path(r.path),
             "handler": r.handler, "out_of_scope": out_of_scope_reason(r)}
            for r in play
        ]
        out["notes"] = notes
    if args.spring_repo:
        out["static_patterns"] = parse_static_resource_handlers(
            args.spring_repo.expanduser().resolve()
        )
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
