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
    segment-matching fix. Retained to detect a **stale JAR**: a Play repo may
    still have an old ``dev-toolkit-1.0.0.jar`` copied into it from a previous
    setup run, and that JAR will classify differently from these rules.

The two disagree on Play's default scaffold layout (``app/controllers/X.java``,
``package controllers;``). ``LayerDetector`` receives paths relative to ``app/``,
so that file arrived as ``controllers/X.java`` -- which does not contain the
substring ``/controllers/`` -- and fell through to OTHER. It then migrated in the
``other`` layer and never received ``@RestController``.

``divergences`` reports every file the two classifiers disagree about. On a
current JAR the list is empty; a non-empty list means the JAR in use predates
the fix and will mis-migrate those files.
"""

from __future__ import annotations

import zipfile
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


def classify(relative_path: str) -> str:
    """
    Segment-based classification. Correct for both flat and packaged layouts.

    ``relative_path`` may be relative to the repo root or to the Java source
    root; segment matching makes the distinction irrelevant, which is precisely
    what the substring version got wrong.

    The if-chain below mirrors ``LayerDetector.classify`` branch for branch,
    including the ``*Model.java`` convention sharing a branch with ``models/``
    (so ``db/UserModel.java`` is a model, not a manager). The two implementations
    must agree exactly -- if they drift, inventory counts stop predicting what
    ``migrate-app`` will do, which is the failure this module exists to prevent.
    """
    path = _posix_lower(relative_path)
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


# Shipped in the same commit as the LayerDetector segment-matching fix, so its
# presence in a JAR is a reliable marker that the fix is in. The JAR filename is
# hardcoded dev-toolkit-1.0.0.jar and never bumped, so the name cannot tell fixed
# from unfixed builds -- and stale copies of the toolkit source still exist.
_FIX_MARKER_CLASS = "com/phenom/devtoolkit/SignatureExtractor.class"


def jar_has_layer_fix(jar_path: Path) -> bool | None:
    """
    True if the JAR post-dates the LayerDetector fix, False if it predates it,
    None if it cannot be inspected (missing file, not a zip).

    A zip entry lookup, so this costs nothing -- no JVM start-up.
    """
    try:
        with zipfile.ZipFile(jar_path) as jar:
            return _FIX_MARKER_CLASS in jar.namelist()
    except (OSError, zipfile.BadZipFile):
        return None


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
