#!/usr/bin/env python3
"""
Tests for the deterministic helpers. Stdlib unittest, no dependencies.

    python3 scripts/tools/test_tools.py

These cover the pieces every later stage trusts blindly: layer classification,
Maven error parsing, and state round-tripping. If any of these are wrong,
verification silently reports success on a broken migration.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import endpoint_diff  # noqa: E402
import fetch_jar  # noqa: E402
import gate  # noqa: E402
import parse_mvn  # noqa: E402
import report  # noqa: E402
import routes as routes_mod  # noqa: E402
import signature_diff  # noqa: E402
import state  # noqa: E402
import verify  # noqa: E402
from layers import classify, classify_legacy, divergences  # noqa: E402


class TestClassify(unittest.TestCase):
    """Paths here are relative to the Java source root, as the JAR receives them."""

    def test_packaged_layout(self):
        cases = {
            "com/acme/controllers/UserController.java": "controller",
            "com/acme/services/UserService.java": "service",
            "com/acme/service/Legacy.java": "service",
            "com/acme/models/User.java": "model",
            "com/acme/db/MongoManager.java": "manager",
            "com/acme/repositories/UserRepository.java": "repository",
            "com/acme/dao/UserDao.java": "repository",
            "com/acme/utils/JsonUtils.java": "other",
            "Module.java": "other",
        }
        for path, expected in cases.items():
            with self.subTest(path=path):
                self.assertEqual(classify(path), expected)

    def test_flat_layout_is_classified_correctly(self):
        """Play's default scaffold puts controllers at the source-root top level."""
        self.assertEqual(classify("controllers/HomeController.java"), "controller")
        self.assertEqual(classify("services/SearchService.java"), "service")
        self.assertEqual(classify("models/User.java"), "model")

    def test_model_filename_convention(self):
        self.assertEqual(classify("com/acme/search/SearchResultModel.java"), "model")
        self.assertEqual(classify("com/acme/search/searchresultmodel.java"), "model")

    def test_directory_rules_beat_filename_convention(self):
        # A *Model.java living under controllers/ is still controller-layer;
        # directory rules are checked first, matching LayerDetector's if-chain.
        self.assertEqual(classify("com/acme/controllers/ViewModel.java"), "controller")

    def test_filename_not_matched_on_directory_names(self):
        self.assertEqual(classify("com/acme/model/Thing.java"), "other")

    def test_model_filename_convention_outranks_db(self):
        # Mirrors LayerDetector: the *Model.java branch is evaluated before /db/.
        # These two implementations must stay in lockstep or inventory counts
        # stop predicting what migrate-app does.
        self.assertEqual(classify("com/acme/db/UserModel.java"), "model")

    def test_partial_directory_names_do_not_match(self):
        self.assertEqual(classify("com/acme/servicehelpers/Helper.java"), "other")
        self.assertEqual(classify("com/acme/mycontrollers/X.java"), "other")

    def test_classification_precedence_matches_jar(self):
        # /db/ is tested before /repositories/, so a repository nested under db/
        # lands in the manager layer. Documented so the ordering is deliberate.
        self.assertEqual(classify("com/acme/db/repositories/Foo.java"), "manager")
        self.assertEqual(classify_legacy("com/acme/db/repositories/Foo.java"), "manager")

    def test_windows_separators(self):
        self.assertEqual(classify(r"com\acme\controllers\X.java"), "controller")

    def test_case_insensitive(self):
        self.assertEqual(classify("com/acme/Controllers/X.java"), "controller")


class TestLegacyDivergence(unittest.TestCase):
    """
    Locks in the LayerDetector bug so a toolkit fix is detected here first.

    LayerDetector.classify substring-matches '/controllers/' against a path that
    is already relative to app/. A top-level controllers/ directory therefore has
    no leading slash, never matches, and falls through to OTHER -- so it migrates
    in the 'other' layer and never receives @RestController. Verified against
    dev-toolkit-1.0.0.jar.
    """

    def test_flat_controller_diverges(self):
        path = "controllers/HomeController.java"
        self.assertEqual(classify(path), "controller")
        self.assertEqual(classify_legacy(path), "other")

    def test_packaged_controller_agrees(self):
        path = "com/acme/controllers/AcmeController.java"
        self.assertEqual(classify(path), classify_legacy(path))

    def test_divergences_reports_only_mismatches(self):
        paths = [
            "controllers/HomeController.java",       # diverges
            "com/acme/controllers/Acme.java",        # agrees
            "com/acme/models/User.java",             # agrees
            "services/SearchService.java",           # diverges
        ]
        found = divergences(paths)
        self.assertEqual(
            {d.path for d in found},
            {"controllers/HomeController.java", "services/SearchService.java"},
        )
        for d in found:
            self.assertEqual(d.jar_actual, "other")


class TestParseMvn(unittest.TestCase):
    LOG = """\
[INFO] Compiling 3 source files
[ERROR] COMPILATION ERROR :
[ERROR] /repo/src/main/java/com/acme/UserService.java:[12,30] cannot find symbol
[ERROR] /repo/src/main/java/com/acme/UserService.java:[18,5] incompatible types
[ERROR] /repo/src/main/java/com/acme/Content.java:[7,1] package play.mvc does not exist
[INFO] BUILD FAILURE
"""

    def test_every_error_line_parsed_once(self):
        errors = parse_mvn.parse_errors(self.LOG)
        self.assertEqual(len(errors), 3)
        self.assertEqual(errors[0]["line"], 12)
        self.assertEqual(errors[0]["message"], "cannot find symbol")
        self.assertTrue(errors[0]["file"].endswith("UserService.java"))

    def test_grouping_by_file(self):
        grouped = parse_mvn.group_by_file(parse_mvn.parse_errors(self.LOG))
        self.assertEqual(len(grouped), 2)
        self.assertEqual(
            len(grouped["/repo/src/main/java/com/acme/UserService.java"]), 2
        )

    def test_signatures_are_order_independent(self):
        errors = parse_mvn.parse_errors(self.LOG)
        self.assertEqual(
            parse_mvn.error_signatures(errors),
            parse_mvn.error_signatures(list(reversed(errors))),
        )

    def test_signatures_differ_when_errors_differ(self):
        a = parse_mvn.error_signatures(parse_mvn.parse_errors(self.LOG))
        b = parse_mvn.error_signatures(
            parse_mvn.parse_errors(self.LOG.replace("[12,30]", "[99,30]"))
        )
        self.assertNotEqual(a, b)

    def test_unparsed_failure_keeps_a_tail(self):
        summary = parse_mvn.summarize("[ERROR] something exploded with no file ref\n")
        self.assertEqual(summary["error_count"], 0)
        self.assertIn("exploded", summary["unparsed_tail"])

    def test_dependency_errors_detected(self):
        log = "[ERROR] Failed to execute goal on project x: Could not resolve dependencies\n"
        self.assertTrue(parse_mvn.summarize(log)["dependency_errors"])

    def test_clean_log_is_empty(self):
        summary = parse_mvn.summarize("[INFO] BUILD SUCCESS\n")
        self.assertEqual(summary["error_count"], 0)
        self.assertEqual(summary["unparsed_tail"], "")


class TestState(unittest.TestCase):
    def test_merge_preserves_unknown_and_existing_fields(self):
        original = {
            "current_step": "transform_validate",
            "layers": {"model": {"status": "done", "files_migrated": 7}},
            "some_legacy_field": {"keep": "me"},
        }
        merged = state.merge_status(original)
        self.assertEqual(merged["layers"]["model"]["status"], "done")
        self.assertEqual(merged["layers"]["model"]["files_migrated"], 7)
        self.assertEqual(merged["some_legacy_field"], {"keep": "me"})
        self.assertEqual(merged["current_step"], "transform_validate")
        # new keys default in
        self.assertEqual(merged["qa_findings"], [])
        self.assertEqual(merged["architecture_review"]["status"], "pending")

    def test_round_trip_is_stable(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "migration-status.json"
            first = state.merge_status({"layers": {"model": {"status": "done"}}})
            state.atomic_write_json(p, first)
            second = state.read_status(p)
            state.atomic_write_json(p, second)
            self.assertEqual(second, state.read_status(p))

    def test_set_and_get_path(self):
        s = state.merge_status({})
        state.set_path(s, "layers.model.status", "done")
        self.assertEqual(state.get_path(s, "layers.model.status"), "done")

    def test_finding_ids_do_not_collide(self):
        s = state.merge_status({})
        for _ in range(3):
            s["qa_findings"].append({"id": state.next_finding_id(s)})
        self.assertEqual([f["id"] for f in s["qa_findings"]], ["F-001", "F-002", "F-003"])

    def test_fold_journal_resumes_after_crash(self):
        with tempfile.TemporaryDirectory() as d:
            journal = Path(d) / "model-dev.ndjson"
            journal.write_text(
                json.dumps({"layer": "model", "action": "migrated", "count": 3}) + "\n"
                + json.dumps({"layer": "model", "action": "failed", "file": "Odd.java"}) + "\n"
                + json.dumps({"layer": "model", "action": "compiled", "error_count": 2}) + "\n"
                + '{"layer": "model", "action": "migr',  # killed mid-write
                encoding="utf-8",
            )
            s = state.merge_status({})
            folded = state.fold_journal(s, journal, "model")
            self.assertEqual(folded, 3)  # malformed trailing line skipped, not fatal
            self.assertEqual(s["layers"]["model"]["files_migrated"], 3)
            self.assertEqual(s["layers"]["model"]["files_failed"], ["Odd.java"])
            self.assertEqual(s["layers"]["model"]["last_error_count"], 2)


class TestVerify(unittest.TestCase):
    def test_missing_layer_fails(self):
        play = {"model": 3, "controller": 3, "service": 0, "repository": 0,
                "manager": 0, "other": 0}
        spring = {"model": 3, "controller": 0, "service": 0, "repository": 0,
                  "manager": 0, "other": 0}
        status, comparison, notes = verify.compare(play, spring)
        self.assertEqual(status, "failed")
        self.assertEqual(comparison["controller"]["delta"], -3)
        self.assertTrue(any("nothing migrated" in n for n in notes))

    def test_shortfall_needs_review(self):
        play = {"model": 5, "controller": 0, "service": 0, "repository": 0,
                "manager": 0, "other": 0}
        spring = {"model": 4, "controller": 0, "service": 0, "repository": 0,
                  "manager": 0, "other": 0}
        status, _, notes = verify.compare(play, spring)
        self.assertEqual(status, "needs_review")
        self.assertTrue(any("short" in n for n in notes))

    def test_small_shortfall_is_not_silently_tolerated(self):
        """The old ±5 tolerance let real losses through; one missing file counts."""
        play = {"model": 10, "controller": 0, "service": 0, "repository": 0,
                "manager": 0, "other": 0}
        spring = {"model": 9, "controller": 0, "service": 0, "repository": 0,
                  "manager": 0, "other": 0}
        status, _, _ = verify.compare(play, spring)
        self.assertNotEqual(status, "passed")

    def test_extra_spring_files_are_fine(self):
        play = {"model": 3, "controller": 0, "service": 0, "repository": 0,
                "manager": 0, "other": 0}
        spring = {"model": 3, "controller": 0, "service": 0, "repository": 0,
                  "manager": 0, "other": 2}
        status, _, _ = verify.compare(play, spring)
        self.assertEqual(status, "passed")

    def test_no_migration_exclusion(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "app"
            (root / "com" / "acme").mkdir(parents=True)
            (root / "com" / "acme" / "Thing.java").write_text("//x", encoding="utf-8")
            (root / "Module.java").write_text("//x", encoding="utf-8")

            counts, total = verify.scan_counts(root, set())
            self.assertEqual(total, 2)

            counts, total = verify.scan_counts(root, {"Module.java"})
            self.assertEqual(total, 1)
            self.assertEqual(counts["other"], 1)


class TestRoutes(unittest.TestCase):
    """T3. Path shapes differ between the frameworks; the routes must still match."""

    PLAY_ROUTES = """\
# Content API
GET     /content              controllers.ContentController.list(category: String)
GET     /content/:id          controllers.ContentController.show(id: String)
POST    /content              controllers.ContentController.create()
DELETE  /content/:id          controllers.ContentController.delete(id: String)
GET     /assets/*file         controllers.Assets.at(path="/public", file)
->      /api                  api.Routes

GET     /health               controllers.HealthController.ping()
"""

    SPRING_CONTROLLER = """\
package com.acme.controllers;
import org.springframework.web.bind.annotation.*;
@RestController
@RequestMapping("/content")
public class ContentController {
    @GetMapping
    public String list(@RequestParam String category) { return ""; }
    @GetMapping("/{id}")
    public String show(@PathVariable String id) { return ""; }
    @PostMapping
    public String create() { return ""; }
    @DeleteMapping("/{id}")
    public String delete(@PathVariable String id) { return ""; }
}
"""

    def _play(self, text):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "routes"
            f.write_text(text, encoding="utf-8")
            return routes_mod.parse_play_routes(f)

    def _spring(self, files):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for name, text in files.items():
                p = root / name
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(text, encoding="utf-8")
            return routes_mod.parse_spring_mappings(root)

    def test_play_parsing_skips_comments_blanks_and_includes(self):
        parsed, notes = self._play(self.PLAY_ROUTES)
        self.assertEqual(len(parsed), 6)
        self.assertEqual(parsed[0].verb, "GET")
        self.assertTrue(any("sub-router" in n for n in notes))

    def test_path_normalization_equates_play_and_spring_params(self):
        self.assertEqual(
            routes_mod.normalize_path("/content/:id"),
            routes_mod.normalize_path("/content/{id}"),
        )
        # Parameter *names* must not matter, only position.
        self.assertEqual(
            routes_mod.normalize_path("/u/:userId/p/:postId"),
            routes_mod.normalize_path("/u/{uid}/p/{pid}"),
        )
        # Play regex-constrained parameters are still one parameter.
        self.assertEqual(
            routes_mod.normalize_path("/item/$id<[0-9]+>"),
            routes_mod.normalize_path("/item/{id}"),
        )
        # Wildcards.
        self.assertEqual(
            routes_mod.normalize_path("/assets/*file"),
            routes_mod.normalize_path("/assets/**"),
        )

    def test_trailing_slash_is_not_a_difference(self):
        self.assertEqual(
            routes_mod.normalize_path("/content/"), routes_mod.normalize_path("/content")
        )

    def test_class_level_prefix_is_joined(self):
        mappings = self._spring({"C.java": self.SPRING_CONTROLLER})
        keys = {r.key() for r in mappings}
        self.assertIn(("GET", "/content"), keys)
        self.assertIn(("GET", "/content/{}"), keys)
        self.assertIn(("POST", "/content"), keys)
        self.assertIn(("DELETE", "/content/{}"), keys)

    def test_missing_mapping_is_detected(self):
        play, _ = self._play(self.PLAY_ROUTES)
        spring = self._spring({"C.java": self.SPRING_CONTROLLER})
        result = routes_mod.compare_routes(play, spring)
        missing = {(m["verb"], m["path"]) for m in result["missing"]}
        self.assertIn(("GET", "/health"), missing)
        self.assertIn(("GET", "/assets/*file"), missing)
        self.assertEqual(result["status"], "failed")

    def test_full_parity_passes(self):
        play, _ = self._play(
            "GET  /content      controllers.C.list()\n"
            "POST /content      controllers.C.create()\n"
        )
        spring = self._spring({"C.java": self.SPRING_CONTROLLER})
        result = routes_mod.compare_routes(play, spring)
        self.assertEqual(result["missing"], [])
        self.assertEqual(result["status"], "passed")

    def test_extra_spring_mappings_are_allowed(self):
        """Actuator, error handlers, and new health checks legitimately appear."""
        play, _ = self._play("GET /content controllers.C.list()\n")
        spring = self._spring({"C.java": self.SPRING_CONTROLLER})
        self.assertEqual(routes_mod.compare_routes(play, spring)["status"], "passed")

    def test_value_attribute_form_is_understood(self):
        mappings = self._spring({"V.java": """\
package x;
@RestController
public class V {
    @GetMapping(value = "/thing", produces = "application/json")
    public String t() { return ""; }
}
"""})
        self.assertIn(("GET", "/thing"), {r.key() for r in mappings})

    def test_request_mapping_with_explicit_method(self):
        mappings = self._spring({"R.java": """\
package x;
@RestController
public class R {
    @RequestMapping(value = "/legacy", method = RequestMethod.POST)
    public String l() { return ""; }
}
"""})
        keys = {r.key() for r in mappings}
        self.assertIn(("POST", "/legacy"), keys)
        self.assertNotIn(("GET", "/legacy"), keys)

    def test_controller_without_class_level_mapping(self):
        mappings = self._spring({"H.java": """\
package x;
@RestController
public class H {
    @GetMapping("/health")
    public String ping() { return ""; }
}
"""})
        self.assertIn(("GET", "/health"), {r.key() for r in mappings})

    def test_missing_routes_file_is_reported_not_crashed(self):
        parsed, notes = routes_mod.parse_play_routes(Path("/nonexistent/routes"))
        self.assertEqual(parsed, [])
        self.assertTrue(notes)


def sig(path, cls, methods, fields=()):
    """Build a signature node of the shape dev-toolkit's `signature` emits."""
    return {
        "path": path,
        "class": cls,
        "methods": [
            {
                "name": n,
                "arity": a,
                "visibility": v,
                "returns": r,
                "statements": s,
            }
            for n, a, v, r, s in methods
        ],
        "fields": list(fields),
    }


class TestSignatureDiff(unittest.TestCase):
    """
    T2. The false-positive tests matter more than the detection tests: a noisy
    blocker check trains reviewers to wave findings through, which costs more
    than the check gains.
    """

    PLAY = {
        "ContentService.java": sig(
            "ContentService.java", "ContentService",
            [("search", 2, "public", "reference", 5),
             ("reindex", 0, "public", "void", 3),
             ("audit", 0, "private", "void", 2)],
        )
    }

    def test_stubbed_method_is_major(self):
        spring = {
            "ContentService.java": sig(
                "ContentService.java", "ContentService",
                [("search", 2, "public", "reference", 1),
                 ("reindex", 0, "public", "void", 3),
                 ("audit", 0, "private", "void", 2)],
            )
        }
        result = signature_diff.diff(self.PLAY, spring, "service")
        majors = [f for f in result["findings"] if f["severity"] == "major"]
        self.assertEqual(len(majors), 1)
        self.assertEqual(majors[0]["category"], "logic-dropped")
        self.assertIn("5 statements in Play -> 1", majors[0]["evidence"])

    def test_missing_public_method_is_blocker(self):
        spring = {
            "ContentService.java": sig(
                "ContentService.java", "ContentService",
                [("search", 2, "public", "reference", 5)],
            )
        }
        result = signature_diff.diff(self.PLAY, spring, "service")
        blockers = [f for f in result["findings"] if f["severity"] == "blocker"]
        self.assertEqual(len(blockers), 1)
        self.assertIn("reindex", blockers[0]["evidence"])
        self.assertEqual(result["status"], "failed")

    def test_faithful_migration_is_silent(self):
        """Same structure, different types/annotations -> nothing to report."""
        spring = {
            "com/acme/ContentService.java": sig(
                "com/acme/ContentService.java", "ContentService",
                [("search", 2, "public", "reference", 5),
                 ("reindex", 0, "public", "void", 3),
                 ("audit", 0, "private", "void", 2)],
            )
        }
        result = signature_diff.diff(self.PLAY, spring, "service")
        self.assertEqual(result["findings"], [])
        self.assertEqual(result["status"], "passed")

    def test_moderate_rewrite_is_not_flagged(self):
        # 5 -> 3 statements is a legitimate simplification: above the min-statement
        # floor, so it stays silent. Only a collapse to near-nothing is reported.
        spring = {
            "ContentService.java": sig(
                "ContentService.java", "ContentService",
                [("search", 2, "public", "reference", 3),
                 ("reindex", 0, "public", "void", 3),
                 ("audit", 0, "private", "void", 2)],
            )
        }
        self.assertEqual(signature_diff.diff(self.PLAY, spring, "service")["findings"], [])

    def test_private_method_removal_is_not_reported(self):
        # Private helpers are implementation detail; migration may inline them.
        spring = {
            "ContentService.java": sig(
                "ContentService.java", "ContentService",
                [("search", 2, "public", "reference", 5),
                 ("reindex", 0, "public", "void", 3)],
            )
        }
        self.assertEqual(signature_diff.diff(self.PLAY, spring, "service")["findings"], [])

    def test_relocated_class_matched_by_name_not_path(self):
        """Migration reorganises packages; path matching would flag every move."""
        spring = {
            "totally/different/place/ContentService.java": sig(
                "totally/different/place/ContentService.java", "ContentService",
                [("search", 2, "public", "reference", 5),
                 ("reindex", 0, "public", "void", 3)],
            )
        }
        result = signature_diff.diff(self.PLAY, spring, "service")
        self.assertEqual(result["classes_compared"], 1)
        self.assertEqual(result["findings"], [])

    def test_unmigrated_class_is_not_a_finding(self):
        """During a layered run most classes are legitimately absent; that is
        verify.py's completeness question, not a preservation failure."""
        result = signature_diff.diff(self.PLAY, {}, "service")
        self.assertEqual(result["findings"], [])
        self.assertEqual(result["classes_absent_from_spring"], ["ContentService"])
        self.assertEqual(result["status"], "passed")

    def test_no_migration_classes_are_skipped(self):
        play = {"Module.java": sig("Module.java", "Module",
                                   [("configure", 0, "public", "void", 4)])}
        result = signature_diff.diff(play, {}, "other", no_migration={"Module.java"})
        self.assertEqual(result["classes_absent_from_spring"], [])
        self.assertEqual(result["findings"], [])

    def test_parse_errors_surface_without_becoming_findings(self):
        spring = {"Broken.java": {"path": "Broken.java", "parse_error": "bad syntax"}}
        result = signature_diff.diff(self.PLAY, spring, "service")
        self.assertEqual(len(result["parse_errors"]), 1)
        self.assertEqual(result["parse_errors"][0]["error"], "bad syntax")
        # The broken file must not masquerade as a class whose methods vanished.
        self.assertEqual([f for f in result["findings"] if f["severity"] == "blocker"], [])

    def test_overloads_aggregate_by_name(self):
        play = {"O.java": sig("O.java", "O",
                              [("go", 0, "public", "void", 4),
                               ("go", 1, "public", "void", 4)])}
        # Merged into a single 8-statement method: no logic lost.
        spring = {"O.java": sig("O.java", "O", [("go", 1, "public", "void", 8)])}
        self.assertEqual(signature_diff.diff(play, spring, "other")["findings"], [])

    def test_thresholds_are_tunable(self):
        spring = {
            "ContentService.java": sig(
                "ContentService.java", "ContentService",
                [("search", 2, "public", "reference", 3),
                 ("reindex", 0, "public", "void", 3),
                 ("audit", 0, "private", "void", 2)],
            )
        }
        # Silent at defaults; a stricter floor makes the same drop reportable.
        self.assertEqual(signature_diff.diff(self.PLAY, spring, "service")["findings"], [])
        strict = signature_diff.diff(
            self.PLAY, spring, "service", drop_ratio=0.3, min_statements=4
        )
        self.assertEqual(len(strict["findings"]), 1)


class TestLayerScopedT2(unittest.TestCase):
    """
    --layer-only. Without it, every layer re-reports findings from classes that
    belong to layers already signed off, and the manager cannot tell which layer
    produced what.
    """

    PLAY = {
        "services/ContentService.java": sig(
            "services/ContentService.java", "ContentService",
            [("search", 1, "public", "reference", 8)],
        ),
        "models/Content.java": sig(
            "models/Content.java", "Content",
            [("getTitle", 0, "public", "reference", 4)],
        ),
    }

    SPRING = {
        # Both hollowed out, so an unscoped diff reports two findings.
        "service/ContentService.java": sig(
            "service/ContentService.java", "ContentService",
            [("search", 1, "public", "reference", 1)],
        ),
        "model/Content.java": sig(
            "model/Content.java", "Content",
            [("getTitle", 0, "public", "reference", 1)],
        ),
    }

    def test_unscoped_reports_every_layer(self):
        result = signature_diff.diff(self.PLAY, self.SPRING, "service")
        self.assertEqual(result["scope"], "full-tree")
        self.assertEqual(len(result["findings"]), 2)

    def test_scoped_reports_only_this_layer(self):
        result = signature_diff.diff(
            self.PLAY, self.SPRING, "service", layer_only=True
        )
        self.assertEqual(result["scope"], "layer")
        self.assertEqual(len(result["findings"]), 1)
        self.assertIn("ContentService", result["findings"][0]["evidence"])

    def test_spring_side_is_never_scoped(self):
        """
        Migration relocates classes. Filtering the Spring side by layer would
        report a correctly migrated class as missing whenever it moved into a
        directory that classifies differently.
        """
        spring_moved = {
            "web/ContentService.java": sig(   # classifies as 'other'
                "web/ContentService.java", "ContentService",
                [("search", 1, "public", "reference", 8)],
            )
        }
        result = signature_diff.diff(
            self.PLAY, spring_moved, "service", layer_only=True
        )
        self.assertEqual(result["findings"], [])
        self.assertEqual(result["classes_compared"], 1)

    def test_scoped_parse_errors_do_not_leak_across_layers(self):
        play = dict(self.PLAY)
        play["models/Broken.java"] = {
            "path": "models/Broken.java", "parse_error": "unbalanced braces"
        }
        scoped = signature_diff.diff(play, self.SPRING, "service", layer_only=True)
        self.assertEqual(scoped["parse_errors"], [])
        unscoped = signature_diff.diff(play, self.SPRING, "model", layer_only=True)
        self.assertEqual(len(unscoped["parse_errors"]), 1)


class TestGateVerdict(unittest.TestCase):
    """
    When the gate escalates to a QA agent. Every escalation is a round trip, so
    the trigger list has to stay short: findings a script already explained do
    not need an agent to restate them.
    """

    def test_clean_run_needs_no_agent(self):
        tiers = {"T1": {"status": "passed"}, "T2": {"status": "passed"}}
        self.assertEqual(gate.escalation_reasons(tiers, [], set(), "service"), [])

    def test_ordinary_compile_failure_needs_no_agent(self):
        tiers = {"T1": {"status": "failed", "unparsed_tail": ""}}
        findings = [{"tier": "T1", "file": "services/ContentService.java",
                     "severity": "blocker"}]
        self.assertEqual(
            gate.escalation_reasons(tiers, findings, {"model"}, "service"), []
        )

    def test_error_in_a_completed_layer_escalates(self):
        """Cross-layer attribution is judgment; dev alone thrashes in the wrong file."""
        tiers = {"T1": {"status": "failed", "unparsed_tail": ""}}
        findings = [{"tier": "T1", "file": "models/Content.java", "severity": "blocker"}]
        reasons = gate.escalation_reasons(tiers, findings, {"model"}, "service")
        self.assertEqual(len(reasons), 1)
        self.assertIn("model", reasons[0])

    def test_unparsed_build_failure_escalates(self):
        tiers = {"T1": {"status": "failed", "unparsed_tail": "[ERROR] plugin blew up"}}
        reasons = gate.escalation_reasons(tiers, [], set(), "service")
        self.assertTrue(any("could not classify" in r for r in reasons))

    def test_t2_parse_error_escalates(self):
        """An unparseable file is unexamined by T2, not passing."""
        tiers = {"T1": {"status": "passed"},
                 "T2": {"status": "passed",
                        "parse_errors": [{"path": "X.java", "error": "eof"}]}}
        reasons = gate.escalation_reasons(tiers, [], set(), "service")
        self.assertTrue(any("would not parse" in r for r in reasons))

    def test_tier_that_could_not_run_escalates(self):
        tiers = {"T1": {"status": "passed"},
                 "T2": {"status": "error", "reason": "dev-toolkit JAR not found"}}
        reasons = gate.escalation_reasons(tiers, [], set(), "service")
        self.assertTrue(any("T2 could not run" in r for r in reasons))

    def test_verdict_ranks_failure_above_review(self):
        self.assertEqual(gate.verdict({"T1": {"status": "passed"}}, []), "passed")
        self.assertEqual(
            gate.verdict({"T1": {"status": "passed"}},
                         [{"severity": "major"}]), "needs_review")
        self.assertEqual(
            gate.verdict({"T1": {"status": "failed"}},
                         [{"severity": "major"}]), "failed")
        # A tier that errored is not a pass, even with no findings.
        self.assertEqual(gate.verdict({"T2": {"status": "error"}}, []), "failed")

    def test_skipped_tier_is_not_a_pass(self):
        self.assertEqual(gate.skipped("no controllers yet")["status"], "skipped")
        self.assertEqual(gate.verdict({"T3": gate.skipped("x")}, []), "passed")

    def test_compile_findings_group_by_file(self):
        """One finding per file, not per error line: errors cluster."""
        t1 = {
            "log": "/tmp/x.log",
            "by_file": {
                "A.java": [{"line": 3, "message": "cannot find symbol"},
                           {"line": 9, "message": "cannot find symbol"},
                           {"line": 12, "message": "bad operand"}],
                "B.java": [{"line": 1, "message": "package does not exist"}],
            },
            "dependency_errors": ["[ERROR] Could not resolve dependencies for x"],
        }
        findings = gate.compile_findings(t1, "service")
        self.assertEqual(len(findings), 3)          # 2 files + 1 dependency
        by_file = {f["file"]: f for f in findings}
        self.assertIn("3 error(s)", by_file["A.java"]["evidence"])
        self.assertEqual(by_file["pom.xml"]["category"], "dependency-error")
        # A dependency failure is the architect's problem, not dev's.
        self.assertIn("architect", by_file["pom.xml"]["suggested_fix"])


class TestEndpointProbes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.routes = Path(self.tmp.name) / "routes"
        self.routes.write_text(
            "GET     /                 controllers.HomeController.index\n"
            "GET     /v1/count         controllers.CountController.count\n"
            "GET     /v1/item/:id      controllers.ItemController.show(id: String)\n"
            "POST    /v1/item          controllers.ItemController.create\n",
            encoding="utf-8",
        )
        self.addCleanup(self.tmp.cleanup)

    def test_only_parameterless_gets_are_enabled(self):
        probes = endpoint_diff.build_probes(self.routes)["probes"]
        enabled = [p["name"] for p in probes if p["enabled"]]
        self.assertEqual(enabled, ["GET /", "GET /v1/count"])

    def test_parameterised_route_carries_a_stub_to_fill_in(self):
        probes = endpoint_diff.build_probes(self.routes)["probes"]
        param = next(p for p in probes if ":id" in p["path"])
        self.assertEqual(param["path_params"], {})
        self.assertFalse(param["enabled"])

    def test_mutating_route_is_disabled_and_says_why(self):
        """
        POST needs a body and identical starting state in both apps -- neither of
        which conf/routes records, so it cannot be probed automatically.
        """
        probes = endpoint_diff.build_probes(self.routes)["probes"]
        post = next(p for p in probes if p["verb"] == "POST")
        self.assertFalse(post["enabled"])
        self.assertIn("state", post["note"])
        self.assertIn("body", post)

    def test_path_params_are_substituted(self):
        self.assertEqual(
            endpoint_diff.resolve_path(
                {"path": "/v1/item/:id", "path_params": {"id": "42"}}
            ),
            "/v1/item/42",
        )
        self.assertEqual(
            endpoint_diff.resolve_path(
                {"path": "/v1/item/$id<[0-9]+>", "path_params": {"id": "7"}}
            ),
            "/v1/item/7",
        )


def response(name, status=200, body=None, text=None, content_type="application/json"):
    record = {"name": name, "verb": "GET", "url": "http://x" + name,
              "status": status, "content_type": content_type}
    if body is not None:
        record["json"] = body
        record["body_length"] = len(json.dumps(body))
    if text is not None:
        record["text"] = text
        record["body_length"] = len(text)
    return record


class TestEndpointDiff(unittest.TestCase):
    """
    T5. As with T2, the false-positive tests carry more weight: a tier that
    flags every timestamp is a tier nobody reads by the third layer.
    """

    def test_identical_responses_pass(self):
        before = [response("/a", body={"title": "x", "count": 3})]
        after = [response("/a", body={"title": "x", "count": 3})]
        result = endpoint_diff.diff_captures(before, after)
        self.assertEqual(result["status"], "passed")
        self.assertFalse(result["needs_agent"])

    def test_volatile_values_are_not_findings(self):
        """Two runs of the same app differ here; equality would fail always."""
        before = [response("/a", body={
            "id": "abc", "createdAt": "2024-01-01T00:00:00Z",
            "took_ms": 12, "title": "x"})]
        after = [response("/a", body={
            "id": "zzz", "createdAt": "2025-06-02T11:00:00Z",
            "took_ms": 40, "title": "x"})]
        self.assertEqual(endpoint_diff.diff_captures(before, after)["status"], "passed")

    def test_volatile_matching_is_by_token_not_substring(self):
        self.assertTrue(endpoint_diff.is_volatile("createdAt"))
        self.assertTrue(endpoint_diff.is_volatile("created_at"))
        self.assertTrue(endpoint_diff.is_volatile("userId"))
        # Substring matching would swallow these, and they carry real values.
        self.assertFalse(endpoint_diff.is_volatile("identifier"))
        self.assertFalse(endpoint_diff.is_volatile("valid"))
        self.assertFalse(endpoint_diff.is_volatile("title"))

    def test_volatile_key_retyped_is_still_caught(self):
        """Masking the value must not mask a serialisation change."""
        before = [response("/a", body={"id": 7})]
        after = [response("/a", body={"id": "7"})]
        result = endpoint_diff.diff_captures(before, after)
        self.assertEqual(
            [f["category"] for f in result["findings"]], ["field-retyped"]
        )

    def test_missing_field_is_a_blocker(self):
        before = [response("/a", body={"title": "x", "author": "me"})]
        after = [response("/a", body={"title": "x"})]
        result = endpoint_diff.diff_captures(before, after)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["findings"][0]["category"], "field-missing")
        self.assertIn("author", result["findings"][0]["evidence"])

    def test_extra_field_is_reported_not_failed(self):
        before = [response("/a", body={"title": "x"})]
        after = [response("/a", body={"title": "x", "etagVersion": 2})]
        result = endpoint_diff.diff_captures(before, after)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["endpoints"][0]["added_fields"], ["etagVersion"])

    def test_status_change_is_a_blocker(self):
        result = endpoint_diff.diff_captures(
            [response("/a", status=200, body={"t": 1})],
            [response("/a", status=500, body={"t": 1})],
        )
        self.assertEqual(result["findings"][0]["category"], "status-changed")
        self.assertEqual(result["status"], "failed")

    def test_unreachable_endpoint_is_a_blocker(self):
        after = {"name": "/a", "verb": "GET", "status": None,
                 "error": "Connection refused"}
        result = endpoint_diff.diff_captures([response("/a", body={})], [after])
        self.assertEqual(result["findings"][0]["category"], "endpoint-unreachable")

    def test_value_change_is_major_and_wants_a_reader(self):
        before = [response("/a", body={"count": 3})]
        after = [response("/a", body={"count": 0})]
        result = endpoint_diff.diff_captures(before, after)
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(result["findings"][0]["severity"], "major")
        self.assertTrue(result["needs_agent"])

    def test_list_length_change_is_reported_once(self):
        """Not as N missing fields -- the shape is the same, the contents are not."""
        before = [response("/a", body={"items": [{"t": "a"}, {"t": "b"}]})]
        after = [response("/a", body={"items": [{"t": "a"}]})]
        result = endpoint_diff.diff_captures(before, after)
        categories = [f["category"] for f in result["findings"]]
        self.assertEqual(categories, ["value-changed"])
        self.assertIn("2 item(s) -> 1", result["findings"][0]["evidence"])

    def test_field_ordering_is_not_a_difference(self):
        before = [response("/a", body={"a": 1, "b": 2})]
        after = [response("/a", body={"b": 2, "a": 1})]
        self.assertEqual(endpoint_diff.diff_captures(before, after)["status"], "passed")

    def test_json_to_text_is_a_blocker(self):
        before = [response("/a", body={"t": 1})]
        after = [response("/a", text="Internal Server Error",
                          content_type="text/plain")]
        categories = [
            f["category"] for f in endpoint_diff.diff_captures(before, after)["findings"]
        ]
        self.assertIn("body-kind-changed", categories)

    def test_endpoint_missing_from_the_after_capture_fails(self):
        """A probe that silently vanished must not read as a pass."""
        result = endpoint_diff.diff_captures([response("/a", body={})], [])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["not_captured_after"], ["/a"])

    def test_content_type_change_is_major(self):
        result = endpoint_diff.diff_captures(
            [response("/a", body={"t": 1}, content_type="application/json")],
            [response("/a", body={"t": 1}, content_type="text/plain")],
        )
        self.assertEqual(result["findings"][0]["category"], "content-type-changed")
        self.assertEqual(result["status"], "needs_review")


class TestFetchJar(unittest.TestCase):
    """
    The jar this prints has to be exactly the pinned bytes, or the caller has
    to see a loud failure -- never a silently wrong or missing jar.
    """

    def _release_file(self, tmp: Path, download_url: str, sha256: str,
                       version: str = "9.9.9") -> Path:
        release = tmp / "toolkit-release.json"
        release.write_text(json.dumps({
            "version": version, "download_url": download_url, "sha256": sha256,
        }), encoding="utf-8")
        return release

    def test_cache_hit_skips_download(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cache_dir = tmp / "cache"
            cache_dir.mkdir()
            content = b"fake jar bytes"
            digest = hashlib.sha256(content).hexdigest()
            (cache_dir / "dev-toolkit-9.9.9.jar").write_bytes(content)
            # A download_url that would fail if fetch_jar ever tried to use it --
            # proves the cache-hit path never calls download().
            release = self._release_file(tmp, "http://127.0.0.1:1/unreachable", digest)
            jar_path = fetch_jar.fetch(release, cache_dir)
            self.assertEqual(jar_path, cache_dir / "dev-toolkit-9.9.9.jar")

    def test_download_and_verify_success(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cache_dir = tmp / "cache"
            source = tmp / "source.jar"
            content = b"a real-enough jar for this test"
            source.write_bytes(content)
            digest = hashlib.sha256(content).hexdigest()
            release = self._release_file(tmp, source.resolve().as_uri(), digest)
            jar_path = fetch_jar.fetch(release, cache_dir)
            self.assertEqual(jar_path.read_bytes(), content)

    def test_checksum_mismatch_fails_loudly_and_does_not_leave_the_bad_jar(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cache_dir = tmp / "cache"
            source = tmp / "source.jar"
            source.write_bytes(b"whatever bytes")
            wrong_sha = "0" * 64
            release = self._release_file(tmp, source.resolve().as_uri(), wrong_sha)
            with self.assertRaises(SystemExit):
                fetch_jar.fetch(release, cache_dir)
            self.assertFalse((cache_dir / "dev-toolkit-9.9.9.jar").exists())

    def test_stale_cached_jar_is_redownloaded(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cache_dir = tmp / "cache"
            cache_dir.mkdir()
            (cache_dir / "dev-toolkit-9.9.9.jar").write_bytes(b"old wrong content")
            source = tmp / "source.jar"
            new_content = b"the correct new content"
            source.write_bytes(new_content)
            digest = hashlib.sha256(new_content).hexdigest()
            release = self._release_file(tmp, source.resolve().as_uri(), digest)
            jar_path = fetch_jar.fetch(release, cache_dir)
            self.assertEqual(jar_path.read_bytes(), new_content)

    def test_missing_release_file_fails_loudly(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with self.assertRaises(SystemExit):
                fetch_jar.fetch(tmp / "nonexistent.json", tmp / "cache")

    def test_release_file_missing_fields_fails_loudly(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            release = tmp / "toolkit-release.json"
            release.write_text(json.dumps({"version": "1.0.0"}), encoding="utf-8")
            with self.assertRaises(SystemExit):
                fetch_jar.fetch(release, tmp / "cache")


class TestReport(unittest.TestCase):
    """The report is a pure function of migration-status.json -- no network, no git required."""

    def _status(self, **overrides: Any) -> dict[str, Any]:
        base = {
            "current_step": "verify",
            "layers": {
                "model": {"status": "done", "files_migrated": 3, "files_failed": [],
                          "batches_completed": 1, "remaining_files": 0},
                "service": {"status": "failed", "files_migrated": 5,
                           "files_failed": ["X.java"], "batches_completed": 2,
                           "remaining_files": 3, "last_error_count": 4,
                           "failure_reason": "attempts exhausted"},
            },
            "failed_layers": ["service"],
            "qa_findings": [
                {"id": "F-001", "layer": "controller", "file": "GET /v1/content",
                 "tier": "T5", "severity": "blocker", "category": "field-missing",
                 "evidence": "fields absent: a, b", "suggested_fix": "check model",
                 "status": "open"},
                {"id": "F-002", "layer": "model", "file": "User.java", "tier": "T2",
                 "severity": "minor", "category": "drop", "evidence": "1 statement dropped",
                 "suggested_fix": "n/a", "status": "accepted"},
            ],
            "endpoint_verification": {"status": "completed",
                                      "checked_at": "2026-08-14T00:00:00Z",
                                      "probes_compared": 5, "not_captured_after": []},
            "commits": {"model": [{"batch": 1, "sha": "deadbeefdeadbeefdead"}]},
        }
        base.update(overrides)
        return base

    def test_report_includes_layers_and_marks_failed(self):
        out = report.render_report(self._status(), None, "2026-08-14T00:00:00Z")
        self.assertIn("model", out)
        self.assertIn("service", out)
        self.assertIn("failed-row", out)  # service is in failed_layers

    def test_report_includes_findings_sorted_by_severity(self):
        out = report.render_report(self._status(), None, "2026-08-14T00:00:00Z")
        self.assertIn("F-001", out)
        self.assertIn("F-002", out)
        self.assertLess(out.index("F-001"), out.index("F-002"))  # blocker before minor

    def test_report_handles_no_findings(self):
        out = report.render_report(self._status(qa_findings=[]), None, "2026-08-14T00:00:00Z")
        self.assertIn("No QA findings", out)

    def test_report_handles_missing_endpoint_verification(self):
        out = report.render_report(
            self._status(endpoint_verification=None), None, "2026-08-14T00:00:00Z"
        )
        self.assertIn("has not run yet", out)

    def test_report_escapes_html_in_evidence(self):
        status = self._status()
        status["qa_findings"][0]["evidence"] = "<script>alert(1)</script>"
        out = report.render_report(status, None, "2026-08-14T00:00:00Z")
        self.assertNotIn("<script>alert(1)</script>", out)
        self.assertIn("&lt;script&gt;", out)

    def test_main_writes_file_and_prints_path(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            status_file = tmp / "migration-status.json"
            status_file.write_text(json.dumps(self._status()), encoding="utf-8")
            out_path = tmp / ".migration" / "report.html"
            proc = subprocess.run(
                [sys.executable, str(Path(__file__).resolve().parent / "report.py"),
                 "--status-file", str(status_file), "--out", str(out_path)],
                capture_output=True, text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue(out_path.is_file())

    def test_main_fails_loudly_on_missing_status_file(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            proc = subprocess.run(
                [sys.executable, str(Path(__file__).resolve().parent / "report.py"),
                 "--status-file", str(tmp / "nope.json"), "--out", str(tmp / "out.html")],
                capture_output=True, text=True,
            )
            self.assertNotEqual(proc.returncode, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
