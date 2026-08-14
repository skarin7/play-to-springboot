#!/usr/bin/env python3
"""
Layer classification for Play/Spring Java sources.

Two classifiers live here on purpose:

``classify``
    Segment-based. What the layer rules are *meant* to express: a path belongs
    to the controller layer when some directory in it is literally named
    ``controllers``.

``classify_legacy``
    The substring-matching version that shipped in dev-toolkit JARs before the
    segment-matching fix. Retained as a regression guard: it is what the JAR's
    ``LayerDetector`` implemented before that fix, and comparing against it is
    how a future LayerDetector regression would be caught before it silently
    mis-migrates a layer.

The two disagree on Play's default scaffold layout (``app/controllers/X.java``,
``package controllers;``). ``LayerDetector`` receives paths relative to ``app/``,
so that file arrived as ``controllers/X.java`` -- which does not contain the
substring ``/controllers/`` -- and fell through to OTHER. It then migrated in the
``other`` layer and never received ``@RestController``.

``divergences`` reports every file the two classifiers disagree about. On the
current dev-toolkit JAR the list is empty.
"""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
from typing import Iterable, NamedTuple

LAYER_ORDER: tuple[str, ...] = (
    "model",
    "repository",
    "manager",
    "service",
    "controller",
    "other",
)
"""Dependency order for migration. Not the same as classification order below."""

def _posix_lower(relative_path: str) -> str:
    return str(relative_path).replace("\\", "/").lower()


def classify(relative_path: str, overrides: dict[str, str] | None = None) -> str:
    """
    Segment-based classification. Correct for both flat and packaged layouts.

    ``relative_path`` may be relative to the repo root or to the Java source
    root; segment matching makes the distinction irrelevant, which is precisely
    what the substring version got wrong.

    ``overrides`` is the human-authored correction map from
    ``.migration/layer-overrides.json`` (see ``load_overrides``), for repos whose
    layout doesn't use the conventional segment names. An exact-path entry wins
    outright; otherwise the longest directory-prefix entry (a key ending in
    ``/`` that the path starts with) wins; otherwise fall through to the segment
    rules below unchanged.

    The if-chain below mirrors ``LayerDetector.classify`` branch for branch,
    including the ``*Model.java`` convention sharing a branch with ``models/``
    (so ``db/UserModel.java`` is a model, not a manager). The two implementations
    must agree exactly -- if they drift, inventory counts stop predicting what
    ``migrate-app`` will do, which is the failure this module exists to prevent.
    """
    path = _posix_lower(relative_path)

    if overrides:
        if path in overrides:
            return overrides[path]
        prefix_matches = [
            key for key in overrides if key.endswith("/") and path.startswith(key)
        ]
        if prefix_matches:
            return overrides[max(prefix_matches, key=len)]

    parts = PurePosixPath(path).parts
    directories = set(parts[:-1])
    file_name = parts[-1] if parts else ""

    if "controllers" in directories:
        return "controller"
    if "service" in directories or "services" in directories:
        return "service"
    if "models" in directories or file_name.endswith("model.java"):
        return "model"
    if "db" in directories:
        return "manager"
    if "repositories" in directories or "dao" in directories:
        return "repository"
    return "other"


def load_overrides(path: Path) -> dict[str, str]:
    """
    Read the human-authored layer-override map, or ``{}`` if absent.

    Absence is not an error -- most repos never need this file. Keys are
    normalized through the same ``_posix_lower`` used everywhere else in this
    module, so matching is consistent regardless of how the human typed the path.
    """
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {_posix_lower(k): v for k, v in data.items()}


def classify_legacy(path_relative_to_source_root: str) -> str:
    """
    Replicates LayerDetector.classify exactly, bug included.

    Takes the path form the JAR actually receives: relative to the Play Java
    source root (``app/``), not the repo root. Feeding it a repo-root-relative
    path will mask the bug rather than reveal it.
    """
    path = _posix_lower(path_relative_to_source_root)
    if "/controllers/" in path:
        return "controller"
    if "/service/" in path or "/services/" in path:
        return "service"
    if "/models/" in path or path.endswith("model.java"):
        return "model"
    if "/db/" in path:
        return "manager"
    if "/repositories/" in path or "/dao/" in path:
        return "repository"
    return "other"


class Divergence(NamedTuple):
    """One file the two classifiers disagree about."""

    path: str
    correct: str
    jar_actual: str

    def describe(self) -> str:
        return (
            f"{self.path}: rules say '{self.correct}', "
            f"dev-toolkit JAR will treat it as '{self.jar_actual}'"
        )


def divergences(paths_relative_to_source_root: Iterable[str]) -> list[Divergence]:
    """
    Files where the shipped JAR will pick a different layer than the rules mean.

    Each entry is a file that will migrate in the wrong layer -- wrong
    dependency order, and wrong transformer behavior (a controller classified
    OTHER gets no ``@RestController``).
    """
    out: list[Divergence] = []
    for rel in paths_relative_to_source_root:
        correct = classify(rel)
        actual = classify_legacy(rel)
        if correct != actual:
            out.append(Divergence(path=str(rel), correct=correct, jar_actual=actual))
    return out


def empty_counts() -> dict[str, int]:
    return {layer: 0 for layer in LAYER_ORDER}
