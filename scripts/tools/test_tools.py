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
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import boot  # noqa: E402
import endpoint_diff  # noqa: E402
import fetch_jar  # noqa: E402
import gap_report  # noqa: E402
import gate  # noqa: E402
import inventory  # noqa: E402
import parse_mvn  # noqa: E402
import report  # noqa: E402
import routes as routes_mod  # noqa: E402
import signature_diff  # noqa: E402
import state  # noqa: E402
import verify  # noqa: E402
import workspace  # noqa: E402
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
        # /assets/*file is Play's built-in asset controller, not a controller
        # anyone migrates. Reporting it as missing is what forced a passthrough
        # controller to be hand-written to satisfy the check.
        self.assertNotIn(("GET", "/assets/*file"), missing)
        self.assertEqual(result["status"], "failed")

    def test_play_asset_route_is_out_of_scope_not_missing(self):
        play, _ = self._play(self.PLAY_ROUTES)
        spring = self._spring({"C.java": self.SPRING_CONTROLLER})
        result = routes_mod.compare_routes(play, spring)
        out_of_scope = {(m["verb"], m["path"]) for m in result["out_of_scope"]}
        self.assertIn(("GET", "/assets/*file"), out_of_scope)

    def test_assets_policy_require_restores_strict_behaviour(self):
        """A project that really did hand-migrate its assets can still demand them."""
        play, _ = self._play(self.PLAY_ROUTES)
        spring = self._spring({"C.java": self.SPRING_CONTROLLER})
        result = routes_mod.compare_routes(play, spring, assets_policy="require")
        missing = {(m["verb"], m["path"]) for m in result["missing"]}
        self.assertIn(("GET", "/assets/*file"), missing)
        self.assertEqual(result["out_of_scope"], [])

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


class TestGuard(unittest.TestCase):
    """
    The read-only invariant. Its predecessor failed open, so every case here is
    about a *non*-clean answer being reachable.
    """

    TOOL = Path(__file__).resolve().parent / "guard.py"

    def _repo(self, root: Path) -> Path:
        play = root / "play"
        (play / "app" / "controllers").mkdir(parents=True)
        (play / "conf").mkdir(parents=True)
        (play / "app" / "controllers" / "A.java").write_text("class A {}", encoding="utf-8")
        (play / "conf" / "routes").write_text("GET / controllers.A.index\n", encoding="utf-8")
        (root / "spring").mkdir()
        return play

    def _run(self, *args) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(self.TOOL), *args], capture_output=True, text=True
        )

    def test_no_baseline_is_error_not_clean(self):
        """The exact regression: absence of evidence read as evidence of cleanliness."""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            play = self._repo(root)
            proc = self._run("check", "--play-repo", str(play),
                             "--spring-repo", str(root / "spring"))
            self.assertEqual(proc.returncode, 3)
            self.assertEqual(json.loads(proc.stdout)["status"], "error")

    def test_non_git_repo_uses_manifest_and_reports_clean(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            play = self._repo(root)
            base = self._run("baseline", "--play-repo", str(play),
                             "--spring-repo", str(root / "spring"))
            self.assertEqual(base.returncode, 0)
            self.assertEqual(json.loads(base.stdout)["mode"], "manifest")

            check = self._run("check", "--play-repo", str(play),
                              "--spring-repo", str(root / "spring"))
            self.assertEqual(check.returncode, 0)
            self.assertEqual(json.loads(check.stdout)["status"], "clean")

    def test_modified_file_is_tampered(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            play = self._repo(root)
            self._run("baseline", "--play-repo", str(play),
                      "--spring-repo", str(root / "spring"))
            (play / "app" / "controllers" / "A.java").write_text(
                "class A { int x; }", encoding="utf-8"
            )
            proc = self._run("check", "--play-repo", str(play),
                             "--spring-repo", str(root / "spring"))
            self.assertEqual(proc.returncode, 2)
            result = json.loads(proc.stdout)
            self.assertEqual(result["status"], "tampered")
            self.assertIn("app/controllers/A.java", result["changes"]["modified"])

    def test_deleted_file_is_tampered(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            play = self._repo(root)
            self._run("baseline", "--play-repo", str(play),
                      "--spring-repo", str(root / "spring"))
            (play / "app" / "controllers" / "A.java").unlink()
            proc = self._run("check", "--play-repo", str(play),
                             "--spring-repo", str(root / "spring"))
            self.assertEqual(proc.returncode, 2)
            self.assertIn("app/controllers/A.java",
                          json.loads(proc.stdout)["changes"]["deleted"])

    def test_touch_without_content_change_is_clean(self):
        """The invariant is about content. A rebuilt mtime is not a violation."""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            play = self._repo(root)
            self._run("baseline", "--play-repo", str(play),
                      "--spring-repo", str(root / "spring"))
            target = play / "app" / "controllers" / "A.java"
            os.utime(target, (0, 0))
            proc = self._run("check", "--play-repo", str(play),
                             "--spring-repo", str(root / "spring"))
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(json.loads(proc.stdout)["status"], "clean")

    def test_play_repo_nested_in_outer_git_repo_uses_manifest(self):
        """
        git init'ing the Play repo was rejected for this case: without it,
        `git -C <play> status` reports the *outer* repo's unrelated changes.
        """
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            subprocess.run(["git", "init", "-q", str(root)], check=True,
                           capture_output=True)
            play = self._repo(root)
            base = self._run("baseline", "--play-repo", str(play),
                             "--spring-repo", str(root / "spring"))
            self.assertEqual(json.loads(base.stdout)["mode"], "manifest")

            # An unrelated change in the outer repo must not trip the guard.
            (root / "unrelated.txt").write_text("noise", encoding="utf-8")
            proc = self._run("check", "--play-repo", str(play),
                             "--spring-repo", str(root / "spring"))
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(json.loads(proc.stdout)["status"], "clean")


class TestSignatureExemptions(unittest.TestCase):
    """T2 suppression: quiet, but never invisible, and never automatic."""

    PLAY = {
        "app/Filters.java": {
            "class": "Filters", "path": "app/Filters.java",
            "methods": [
                {"name": "apply", "visibility": "public", "arity": 1, "statements": 4},
                {"name": "realWork", "visibility": "public", "arity": 0, "statements": 9},
            ],
        }
    }
    SPRING = {
        "src/main/java/Filters.java": {
            "class": "Filters", "path": "src/main/java/Filters.java",
            "methods": [
                {"name": "doFilter", "visibility": "public", "arity": 3, "statements": 6},
            ],
        }
    }

    def test_framework_glue_is_suppressed_not_blocked(self):
        result = signature_diff.diff(self.PLAY, self.SPRING, "other")
        categories = {f["category"] for f in result["findings"]}
        self.assertNotIn(
            "apply", {f["evidence"].split(".")[1].split("(")[0] for f in result["findings"]}
        )
        self.assertIn("method-missing", categories)
        suppressed = {(s["class"], s["method"]) for s in result["suppressed"]}
        self.assertIn(("Filters", "apply"), suppressed)

    def test_real_gap_still_blocks(self):
        """The point of suppression is that it is narrow."""
        result = signature_diff.diff(self.PLAY, self.SPRING, "other")
        self.assertEqual(result["status"], "failed")
        self.assertTrue(
            any("realWork" in f["evidence"] for f in result["findings"])
        )

    def test_suggested_fix_forbids_writing_a_shim(self):
        result = signature_diff.diff(self.PLAY, self.SPRING, "other")
        fix = result["findings"][0]["suggested_fix"]
        self.assertIn("signature-exemptions.json", fix)
        self.assertIn("do not add a method solely to satisfy this check", fix)

    def test_project_entry_can_narrow_a_default(self):
        """Suppression must never be harder to remove than it was to add."""
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "signature-exemptions.json"
            path.write_text(json.dumps({"exemptions": {"Filters": {}}}), encoding="utf-8")
            exemptions = signature_diff.load_exemptions(path)
            result = signature_diff.diff(self.PLAY, self.SPRING, "other",
                                         exemptions=exemptions)
            self.assertTrue(any("apply" in f["evidence"] for f in result["findings"]))
            self.assertEqual(result["suppressed"], [])

    def test_unknown_class_is_never_exempt(self):
        play = {
            "app/ContentService.java": {
                "class": "ContentService", "path": "app/ContentService.java",
                "methods": [{"name": "apply", "visibility": "public",
                             "arity": 1, "statements": 4}],
            }
        }
        spring = {
            "src/main/java/ContentService.java": {
                "class": "ContentService", "path": "src/main/java/ContentService.java",
                "methods": [],
            }
        }
        result = signature_diff.diff(play, spring, "service")
        self.assertEqual(result["suppressed"], [])
        self.assertEqual(result["status"], "failed")

    def test_gate_flags_exemptions_edited_after_approval(self):
        with tempfile.TemporaryDirectory() as d:
            spring = Path(d)
            (spring / ".migration").mkdir()
            path = spring / ".migration" / signature_diff.EXEMPTIONS_FILE
            path.write_text('{"exemptions": {}}', encoding="utf-8")
            approved = hashlib.sha256(path.read_bytes()).hexdigest()

            self.assertFalse(gate.exemptions_state(spring, approved)["modified_after_gate"])

            path.write_text('{"exemptions": {"Anything": {"x": "y"}}}', encoding="utf-8")
            after = gate.exemptions_state(spring, approved)
            self.assertTrue(after["modified_after_gate"])
            self.assertNotEqual(after["sha256"], approved)


class TestStaticResourceRoutes(unittest.TestCase):
    """Spring serves static content from configuration, not from annotations."""

    def _spring_repo(self, root: Path, properties: str = "", java: str = "") -> Path:
        repo = root / "spring"
        (repo / "src" / "main" / "resources").mkdir(parents=True)
        (repo / "src" / "main" / "java").mkdir(parents=True)
        if properties:
            (repo / "src" / "main" / "resources" / "application.properties").write_text(
                properties, encoding="utf-8"
            )
        if java:
            (repo / "src" / "main" / "java" / "WebConfig.java").write_text(
                java, encoding="utf-8"
            )
        return repo

    def test_spring_catch_all_normalizes_like_plays_wildcard(self):
        self.assertEqual(routes_mod.normalize_path("/assets/{*file}"), "/assets/**")
        self.assertEqual(
            routes_mod.normalize_path("/assets/{*file}"),
            routes_mod.normalize_path("/assets/*file"),
        )

    def test_static_path_pattern_is_read_from_properties(self):
        with tempfile.TemporaryDirectory() as d:
            repo = self._spring_repo(
                Path(d), properties="spring.mvc.static-path-pattern=/assets/**\n"
            )
            self.assertIn("/assets/**", routes_mod.parse_static_resource_handlers(repo))

    def test_resource_handler_is_read_from_a_webmvcconfigurer(self):
        java = """
package com.acme;
import org.springframework.web.servlet.config.annotation.*;
public class WebConfig implements WebMvcConfigurer {
    public void addResourceHandlers(ResourceHandlerRegistry registry) {
        registry.addResourceHandler("/static/**").addResourceLocations("classpath:/static/");
    }
}
"""
        with tempfile.TemporaryDirectory() as d:
            repo = self._spring_repo(Path(d), java=java)
            self.assertIn("/static/**", routes_mod.parse_static_resource_handlers(repo))

    def test_default_pattern_when_nothing_is_configured(self):
        with tempfile.TemporaryDirectory() as d:
            repo = self._spring_repo(Path(d))
            self.assertEqual(
                routes_mod.parse_static_resource_handlers(repo),
                [routes_mod.SPRING_DEFAULT_STATIC_PATTERN],
            )

    def test_specific_pattern_matches_a_static_route(self):
        play = [routes_mod.Route("GET", "/static/*file", "controllers.Custom.serve")]
        result = routes_mod.compare_routes(play, [], static_routes=["/static/**"])
        self.assertEqual(result["missing"], [])
        self.assertEqual(len(result["matched_by_static"]), 1)

    def test_universal_pattern_never_excuses_a_missing_route(self):
        """
        Boot's default is /**, which would otherwise mark every unmigrated GET
        controller as "served by static resources" and turn T3 permanently green.
        """
        play = [routes_mod.Route("GET", "/content", "controllers.C.list")]
        result = routes_mod.compare_routes(play, [], static_routes=["/**"])
        self.assertEqual(len(result["missing"]), 1)
        self.assertEqual(result["matched_by_static"], [])


class TestInventoryOutOfScope(unittest.TestCase):
    """Views were invisible, so nothing could report them as excluded."""

    def _play(self, root: Path) -> Path:
        play = root / "play"
        (play / "app" / "views").mkdir(parents=True)
        (play / "public" / "images").mkdir(parents=True)
        (play / "conf").mkdir(parents=True)
        (play / "app" / "views" / "index.scala.html").write_text("@()", encoding="utf-8")
        (play / "app" / "views" / "main.scala.html").write_text("@()", encoding="utf-8")
        (play / "public" / "images" / "logo.png").write_bytes(b"png")
        (play / "conf" / "messages").write_text("hello=Hello", encoding="utf-8")
        return play

    def test_counts_templates_assets_and_messages(self):
        with tempfile.TemporaryDirectory() as d:
            result = inventory.scan_non_java(self._play(Path(d)))
            categories = result["categories"]
            self.assertEqual(categories["twirl_templates"]["count"], 2)
            self.assertEqual(categories["static_assets"]["count"], 1)
            self.assertEqual(categories["i18n_messages"]["count"], 1)
            self.assertEqual(result["total_files"], 4)
            self.assertEqual(result["policy"], "left-in-place")

    def test_samples_are_paths_a_human_can_open(self):
        with tempfile.TemporaryDirectory() as d:
            result = inventory.scan_non_java(self._play(Path(d)))
            self.assertIn(
                "app/views/index.scala.html",
                result["categories"]["twirl_templates"]["samples"],
            )

    def test_probes_seed_out_of_scope_routes_disabled(self):
        with tempfile.TemporaryDirectory() as d:
            routes_file = Path(d) / "routes"
            routes_file.write_text(
                "GET /          controllers.HomeController.index()\n"
                "GET /assets/*file controllers.Assets.at(path=\"/public\", file)\n",
                encoding="utf-8",
            )
            probes = endpoint_diff.build_probes(routes_file)["probes"]
            by_name = {p["name"]: p for p in probes}
            self.assertTrue(by_name["GET /"]["enabled"])
            asset = by_name["GET /assets/*file"]
            self.assertFalse(asset["enabled"])
            self.assertIn("out_of_scope", asset)

    def test_out_of_scope_probes_stay_disabled_even_when_forced(self):
        """Nothing was migrated behind them; a probe there manufactures a blocker."""
        with tempfile.TemporaryDirectory() as d:
            routes_file = Path(d) / "routes"
            routes_file.write_text(
                "GET /assets/*file controllers.Assets.at(path=\"/public\", file)\n",
                encoding="utf-8",
            )
            probes = endpoint_diff.build_probes(
                routes_file, include_parameterised=True
            )["probes"]
            self.assertFalse(probes[0]["enabled"])


class TestClassificationSmell(unittest.TestCase):
    """A percentage needs a sample before it means anything."""

    def test_small_repo_suppresses_the_warning_with_a_reason(self):
        paths = ["Module.java", "Filters.java"] + [
            f"controllers/C{i}.java" for i in range(6)
        ]
        smell = inventory.classification_smell(paths)
        self.assertFalse(smell["warn"])
        self.assertIn("sample too small", smell["warn_suppressed_reason"])
        self.assertGreater(smell["other_pct"], 0.15)  # reported, not hidden

    def test_large_repo_with_a_misnamed_directory_still_warns(self):
        paths = [f"web/C{i}.java" for i in range(6)] + [
            f"models/M{i}.java" for i in range(14)
        ]
        smell = inventory.classification_smell(paths)
        self.assertTrue(smell["warn"])
        self.assertIsNone(smell["warn_suppressed_reason"])
        self.assertEqual(smell["common_unmapped_dirs"][0][0], "web")

    def test_threshold_and_minimum_are_both_tunable(self):
        paths = [f"web/C{i}.java" for i in range(3)] + [
            f"controllers/C{i}.java" for i in range(7)
        ]
        self.assertFalse(inventory.classification_smell(paths, min_files=25)["warn"])
        self.assertTrue(inventory.classification_smell(paths, min_files=1)["warn"])


class TestStateCliBothOrders(unittest.TestCase):
    """The tool's own docstring showed an order argparse used to reject."""

    TOOL = Path(__file__).resolve().parent / "state.py"

    def _run(self, *args) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(self.TOOL), *args], capture_output=True, text=True
        )

    def test_status_file_before_or_after_the_subcommand(self):
        with tempfile.TemporaryDirectory() as d:
            status = Path(d) / "migration-status.json"
            self.assertEqual(
                self._run("init", "--status-file", str(status)).returncode, 0
            )
            after = self._run("show", "--status-file", str(status), "--path", "mode")
            before = self._run("--status-file", str(status), "show", "--path", "mode")
            self.assertEqual(after.returncode, 0, after.stderr)
            self.assertEqual(before.returncode, 0, before.stderr)
            self.assertEqual(after.stdout, before.stdout)

    def test_missing_status_file_says_so_in_both_positions(self):
        proc = self._run("show")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("--status-file", proc.stderr)

    def test_new_blocks_exist_in_a_fresh_status_file(self):
        merged = state.merge_status({})
        self.assertEqual(merged["run_config"]["assets_policy"], "skip")
        self.assertFalse(merged["run_config"]["skip_t5"])
        self.assertEqual(merged["out_of_scope"]["policy"], "left-in-place")
        self.assertIsNone(merged["architecture_review"]["exemptions_sha256"])


class TestBoot(unittest.TestCase):
    """The T5 teardown. An orphaned sbt/JVM tree outlives the whole session."""

    def _fake_app(self, run_dir: Path) -> int:
        """A process that forks a child, like sbt forking a JVM."""
        proc = subprocess.Popen(
            ["bash", "-c", "sleep 60 & sleep 60"],
            start_new_session=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        pgid = os.getpgid(proc.pid)
        (run_dir / "play.pid.json").write_text(
            json.dumps({"app": "play", "pid": proc.pid, "pgid": pgid, "port": 9000}),
            encoding="utf-8",
        )
        return pgid

    def test_stop_kills_the_whole_process_group(self):
        with tempfile.TemporaryDirectory() as d:
            run_dir = Path(d)
            pgid = self._fake_app(run_dir)
            self.assertEqual(boot.status(run_dir)["count"], 1)

            result = boot.stop(run_dir, "play")
            self.assertEqual(result["status"], "stopped")
            self.assertFalse(boot.alive(pgid))
            self.assertEqual(boot.status(run_dir)["count"], 0)

    def test_stop_all_is_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            run_dir = Path(d)
            self._fake_app(run_dir)
            self.assertEqual(boot.stop_all(run_dir)["status"], "clean")
            again = boot.stop_all(run_dir)
            self.assertEqual(again["status"], "clean")
            self.assertEqual(again["stopped"][0]["outcome"], "already_stopped")

    def test_stale_pidfile_does_not_kill_a_recycled_pid(self):
        """A pid from a previous run may belong to something else entirely."""
        with tempfile.TemporaryDirectory() as d:
            run_dir = Path(d)
            (run_dir / "spring.pid.json").write_text(
                json.dumps({"app": "spring", "pid": 999999, "pgid": 999999}),
                encoding="utf-8",
            )
            self.assertEqual(boot.status(run_dir)["count"], 0)
            self.assertEqual(boot.stop(run_dir, "spring")["outcome"], "already_stopped")

    def test_missing_pidfile_is_reported_not_crashed(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(boot.stop(Path(d), "play")["status"], "no_pidfile")

    def test_preflight_blocks_on_a_missing_toolchain(self):
        with tempfile.TemporaryDirectory() as d:
            result = boot.preflight("play", Path(d) / "does-not-exist")
            self.assertEqual(result["status"], "blocked")
            self.assertTrue(any("repo" in p for p in result["problems"]))

    def test_no_boot_command_can_ever_invoke_docker(self):
        """A missing toolchain must stay a finding, not become an image pull."""
        for app in ("play", "spring"):
            for fallback in (False, True):
                command = " ".join(
                    boot.boot_command(app, Path("/repo"), 9000, fallback)
                ).lower()
                self.assertNotIn("docker", command)
                self.assertNotIn("podman", command)


class TestGateTimeout(unittest.TestCase):
    """On a timeout the finding used to cite a log file that was never written."""

    def test_partial_log_is_written_when_mvn_times_out(self):
        with tempfile.TemporaryDirectory() as d:
            log_dir = Path(d)
            original = gate.run

            def fake_run(cmd, cwd=None, timeout=None):
                raise subprocess.TimeoutExpired(
                    cmd, timeout or 1, output="[INFO] Scanning for projects...\n",
                    stderr="[ERROR] killed\n",
                )

            gate.run = fake_run
            try:
                result = gate.tier_compile(Path(d), "service", log_dir, timeout=1)
            finally:
                gate.run = original

            self.assertEqual(result["status"], "error")
            self.assertTrue(result["partial"])
            log_path = Path(result["log"])
            self.assertTrue(log_path.is_file())
            self.assertIn("Scanning for projects", log_path.read_text(encoding="utf-8"))


class TestWorkspaceConfig(unittest.TestCase):
    """One reader, one allowlist: a knob absent from it silently does nothing."""

    def test_known_keys_are_typed_and_unknown_keys_dropped(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "workspace.yaml"
            path.write_text(
                "play_repo: /p\nbatch_size: 25\nmvn_timeout: 1200\n"
                "run_history: true\nnot_a_real_key: 7\n",
                encoding="utf-8",
            )
            parsed = workspace.parse_workspace_yaml(path)
            self.assertEqual(parsed["batch_size"], 25)
            self.assertEqual(parsed["mvn_timeout"], 1200)
            self.assertIs(parsed["run_history"], True)
            self.assertNotIn("not_a_real_key", parsed)

    def test_timeout_precedence_is_flag_then_file_then_default(self):
        ws = {"mvn_timeout": 1200}
        self.assertEqual(workspace.timeout(ws, "mvn_timeout", 30, 900), 30)
        self.assertEqual(workspace.timeout(ws, "mvn_timeout", None, 900), 1200)
        self.assertEqual(workspace.timeout({}, "mvn_timeout", None, 900), 900)

    def test_a_non_numeric_timeout_is_no_configuration_at_all(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "workspace.yaml"
            path.write_text("mvn_timeout: soon\n", encoding="utf-8")
            self.assertNotIn("mvn_timeout", workspace.parse_workspace_yaml(path))


class TestPermissionHook(unittest.TestCase):
    """The hook may only remove prompts it can prove, or add a denial."""

    HOOK = Path(__file__).resolve().parents[2] / "hooks" / "allow_migration_tools.py"

    def _decide(self, command: str, env: dict[str, str]) -> str:
        proc = subprocess.run(
            [sys.executable, str(self.HOOK)],
            input=json.dumps({"tool_name": "Bash", "tool_input": {"command": command}}),
            capture_output=True, text=True, env={**os.environ, **env},
        )
        if not proc.stdout.strip():
            return "prompt"
        return json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecision"]

    def _env(self, root: Path) -> dict[str, str]:
        for sub in ("play/app", "spring", "plugin/scripts/tools", "data"):
            (root / sub).mkdir(parents=True, exist_ok=True)
        return {
            "CLAUDE_PLUGIN_ROOT": str(root / "plugin"),
            "CLAUDE_PLUGIN_DATA": str(root / "data"),
            "P2SB_PLAY_REPO": str(root / "play"),
            "P2SB_SPRING_REPO": str(root / "spring"),
            "P2SB_CMD_WRAPPER": "",
        }

    def test_plugin_scripts_are_allowed(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            env = self._env(root)
            self.assertEqual(
                self._decide(f"python3 {root}/plugin/scripts/tools/gate.py --layer x", env),
                "allow",
            )

    def test_path_escape_does_not_pass_as_a_plugin_script(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            env = self._env(root)
            self.assertEqual(
                self._decide(f"python3 {root}/plugin/scripts/../../evil.py", env),
                "prompt",
            )

    def test_writes_to_the_play_repo_are_denied(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            env = self._env(root)
            for command in (
                f"rm -rf {root}/play/app",
                f"sed -i s/a/b/ {root}/play/app/A.java",
                f"git -C {root}/play commit -m x",
            ):
                self.assertEqual(self._decide(command, env), "deny", command)

    def test_read_only_git_against_the_play_repo_is_allowed(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            env = self._env(root)
            self.assertEqual(
                self._decide(f"git -C {root}/play status --porcelain", env), "allow"
            )

    def test_unrelated_commands_leave_the_normal_prompt_alone(self):
        with tempfile.TemporaryDirectory() as d:
            env = self._env(Path(d))
            self.assertEqual(self._decide("ls -la /tmp", env), "prompt")
            self.assertEqual(self._decide("java -jar /tmp/random.jar", env), "prompt")

    def test_wrapper_token_is_stripped_only_when_configured(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            env = self._env(root)
            command = f"rtk git -C {root}/play commit -m x"
            self.assertEqual(self._decide(command, env), "prompt")
            self.assertEqual(self._decide(command, {**env, "P2SB_CMD_WRAPPER": "rtk"}),
                             "deny")


class TestReportSections(unittest.TestCase):
    """A decision the report cannot show is indistinguishable from an omission."""

    def test_out_of_scope_section_lists_what_was_left_behind(self):
        html = report.render_out_of_scope(
            {
                "policy": "left-in-place",
                "total_files": 3,
                "categories": {"twirl_templates": {"count": 3,
                                                   "samples": ["app/views/index.scala.html"]}},
            }
        )
        self.assertIn("left-in-place", html)
        self.assertIn("app/views/index.scala.html", html)

    def test_exemptions_section_shouts_when_edited_after_approval(self):
        html = report.render_exemptions(
            {"exemptions": [{"class": "Filters", "method": "apply",
                             "replacement": "doFilter", "reason": "interface change"}],
             "exemptions_modified_after_gate": True}
        )
        self.assertIn("alert", html)
        self.assertIn("doFilter", html)

    def test_sections_degrade_rather_than_crash_on_an_older_status_file(self):
        html = report.render_report(state.merge_status({}), None, "now")
        self.assertIn("Out of scope", html)
        self.assertIn("T2 exemptions", html)


class TestGapRedaction(unittest.TestCase):
    """
    This report leaves the user's machine, so the tests are adversarial: they
    assert what must *never* appear, not merely that the happy path formats.
    """

    SALT = "test-salt-not-random"

    def test_framework_symbols_survive_verbatim(self):
        """They are the actionable half, and they are nobody's private data."""
        for symbol in ("play.libs.Akka.system", "play.mvc.Result",
                       "org.springframework.web.bind.annotation.GetMapping",
                       "akka.actor.ActorSystem", "views.html.index"):
            self.assertEqual(gap_report.redact_symbol(symbol, self.SALT), symbol)

    def test_maven_coordinates_survive(self):
        coord = "com.typesafe.play:play-mailer_2.13:8.0.1"
        self.assertEqual(gap_report.redact_symbol(coord, self.SALT), coord)

    def test_user_classes_are_hashed(self):
        redacted = gap_report.redact_symbol("com.acme.billing.PricingEngine", self.SALT)
        self.assertNotIn("acme", redacted)
        self.assertNotIn("Pricing", redacted)
        self.assertTrue(redacted.startswith("<class:"))

    def test_absolute_paths_never_survive(self):
        """Paths carry usernames, employers, and project names."""
        text = "read /home/jane/work/acme-payments/app/models/RateCard.java and guessed"
        redacted = gap_report.redact_text(text, self.SALT)
        for leak in ("jane", "acme", "payments", "RateCard"):
            self.assertNotIn(leak, redacted)

    def test_windows_paths_never_survive(self):
        redacted = gap_report.redact_text(
            r"opened C:\Users\jane\acme\App.java", self.SALT
        )
        self.assertNotIn("jane", redacted)
        self.assertNotIn("acme", redacted)

    def test_hash_is_stable_within_an_install_and_differs_across_installs(self):
        """
        Stable so one user's repeat gaps aggregate; salted so the same class
        name at two companies never merges into one identity.
        """
        a1 = gap_report.redact_symbol("com.acme.Thing", "salt-a")
        a2 = gap_report.redact_symbol("com.acme.Thing", "salt-a")
        b1 = gap_report.redact_symbol("com.acme.Thing", "salt-b")
        self.assertEqual(a1, a2)
        self.assertNotEqual(a1, b1)

    def test_unknown_fields_are_dropped_not_passed_through(self):
        """A field an agent invents later must not leak by default."""
        redacted = gap_report.redact_gap(
            {"kind": "unhandled_idiom", "subject": "play.mvc.Result",
             "secret_note": "internal codename Bluebird",
             "stack_trace": "/home/jane/app/Secret.java:42"},
            self.SALT,
        )
        self.assertNotIn("secret_note", redacted)
        self.assertNotIn("stack_trace", redacted)
        self.assertNotIn("Bluebird", json.dumps(redacted))

    def test_unknown_kind_is_bucketed_rather_than_echoed(self):
        redacted = gap_report.redact_gap(
            {"kind": "../../etc/passwd", "subject": "play.mvc.Result"}, self.SALT
        )
        self.assertEqual(redacted["kind"], "agent_improvised")

    def test_blind_tier_only_accepts_known_tiers(self):
        self.assertIsNone(
            gap_report.redact_gap({"kind": "tool_error", "blind_tier": "T9"},
                                  self.SALT)["blind_tier"]
        )
        self.assertEqual(
            gap_report.redact_gap({"kind": "tool_error", "blind_tier": "T2"},
                                  self.SALT)["blind_tier"],
            "T2",
        )

    def test_bare_class_names_without_a_package_are_hashed(self):
        """
        `what_i_did` is model-written prose. A class named with no package at
        all contains no dot, so the dotted-identifier pass never sees it.
        """
        redacted = gap_report.redact_text(
            "Hand-ported RealtimeScoringEngine to @Async", self.SALT
        )
        self.assertNotIn("RealtimeScoringEngine", redacted)
        self.assertIn("@Async", redacted)      # framework vocabulary survives
        self.assertIn("Hand-ported", redacted)  # ordinary prose survives

    def test_secrets_pasted_into_free_text_are_scrubbed(self):
        for secret in (
            "AKIA1234567890ABCD",
            "ghp_abcdefghijklmnop",
            "sk-abc123def456",
            "5f4dcc3b5aa765d61d8327deb882cf99",
            "password=hunter2",
        ):
            redacted = gap_report.redact_text(f"config had {secret} in it", self.SALT)
            self.assertNotIn(secret, redacted, secret)

    def test_a_placeholder_is_never_re_redacted_into_nonsense(self):
        """The scrubbed marker must not itself match the secret patterns."""
        redacted = gap_report.redact_text("API key AKIA1234567890 in config", self.SALT)
        self.assertNotIn("<<", redacted)
        self.assertEqual(redacted.count("<"), redacted.count(">"))

    def test_ticket_ids_and_internal_codes_do_not_survive(self):
        redacted = gap_report.redact_text(
            "see JIRA-9981 and rate_card_v2 for context", self.SALT
        )
        self.assertNotIn("JIRA", redacted)
        self.assertNotIn("rate_card_v2", redacted)

    def test_free_text_is_length_capped(self):
        long_text = "com.acme.Thing " * 500
        redacted = gap_report.redact_gap(
            {"kind": "tool_error", "what_i_did": long_text}, self.SALT
        )
        self.assertLessEqual(len(redacted["what_i_did"]), 400)

    def test_no_network_code_in_the_tool(self):
        """The promise is 'nothing is uploaded'; keep it enforceable."""
        source = (Path(__file__).resolve().parent / "gap_report.py").read_text(
            encoding="utf-8"
        )
        for forbidden in ("urllib.request", "http.client", "socket.",
                          "requests.", "urlopen"):
            self.assertNotIn(forbidden, source)


class TestGapReport(unittest.TestCase):
    """Collection, run shape, and the author's aggregation side."""

    def _workspace(self, root: Path, lines: list[str]) -> Path:
        spring = root / "spring"
        (spring / ".migration").mkdir(parents=True)
        (spring / ".migration" / gap_report.GAPS_FILE).write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )
        return spring

    def test_malformed_lines_are_skipped_not_fatal(self):
        with tempfile.TemporaryDirectory() as d:
            spring = self._workspace(Path(d), [
                json.dumps({"kind": "tool_error", "subject": "play.mvc.Result"}),
                "{ this is not json",
            ])
            gaps, warnings = gap_report.read_gaps(spring)
            self.assertEqual(len(gaps), 1)
            self.assertTrue(warnings)

    def test_missing_journal_is_an_empty_report_not_a_crash(self):
        with tempfile.TemporaryDirectory() as d:
            spring = Path(d) / "spring"
            (spring / ".migration").mkdir(parents=True)
            report = gap_report.build_report(spring)
            self.assertEqual(report["gaps"], [])
            self.assertTrue(report["redacted"])

    def test_run_shape_carries_counts_but_no_names(self):
        with tempfile.TemporaryDirectory() as d:
            spring = self._workspace(Path(d), [
                json.dumps({"kind": "tool_error", "subject": "play.mvc.Result"})
            ])
            (spring / "migration-status.json").write_text(
                json.dumps({
                    "mode": "collapsed",
                    "qa_findings": [
                        {"tier": "T2", "severity": "blocker", "category": "method-missing",
                         "file": "com/acme/Secret.java",
                         "evidence": "com.acme.Secret.chargeCard is missing"},
                    ],
                    "source_inventory": {"play": {"total_java_files": 8}},
                }),
                encoding="utf-8",
            )
            report = gap_report.build_report(spring)
            blob = json.dumps(report)
            self.assertEqual(report["run"]["findings_by_tier"], {"T2": 1})
            self.assertEqual(report["run"]["play_java_files"], 8)
            # Counts, never the finding's own text or the file it named.
            self.assertNotIn("acme", blob)
            self.assertNotIn("chargeCard", blob)

    def test_aggregate_counts_distinct_installs_not_occurrences(self):
        """One person running the same repo forty times is one signal."""
        gap = {"kind": "unhandled_idiom", "subject": "play.libs.Akka.system()"}
        reports = [
            {"install_id": "aaa", "plugin_version": "1.0.0", "gaps": [gap]},
            {"install_id": "aaa", "plugin_version": "1.0.0", "gaps": [gap]},
            {"install_id": "aaa", "plugin_version": "1.0.0", "gaps": [gap]},
        ]
        summary = gap_report.aggregate(reports)
        row = summary["ranked"][0]
        self.assertEqual(row["installs"], 1)
        self.assertEqual(row["occurrences"], 3)
        self.assertFalse(row["promote"])

    def test_two_installs_earns_promotion(self):
        gap = {"kind": "unhandled_idiom", "subject": "play.libs.Akka.system()"}
        summary = gap_report.aggregate([
            {"install_id": "aaa", "gaps": [gap]},
            {"install_id": "bbb", "gaps": [gap]},
        ])
        self.assertEqual(len(summary["promotable"]), 1)
        self.assertTrue(summary["ranked"][0]["promote"])

    def test_ranking_puts_the_widest_gap_first(self):
        wide = {"kind": "unhandled_idiom", "subject": "play.mvc.Http.Context"}
        narrow = {"kind": "tool_error", "subject": "sbt"}
        summary = gap_report.aggregate([
            {"install_id": "a", "gaps": [wide, narrow]},
            {"install_id": "b", "gaps": [wide]},
            {"install_id": "c", "gaps": [wide]},
        ])
        self.assertEqual(summary["ranked"][0]["subject"], "play.mvc.Http.Context")
        self.assertEqual(summary["ranked"][0]["installs"], 3)

    def test_markdown_renders_a_run_that_never_started(self):
        report = gap_report.build_report(Path("/nonexistent-spring-repo"))
        markdown = gap_report.render_markdown(report)
        self.assertIn("gap report", markdown)
        self.assertNotIn("None Play Java files", markdown)

    def test_cli_render_writes_a_file_and_prints_its_path(self):
        with tempfile.TemporaryDirectory() as d:
            spring = self._workspace(Path(d), [
                json.dumps({"kind": "boot_failure", "subject": "sbt", "role": "qa",
                            "what_i_did": "reported t5-skipped"})
            ])
            proc = subprocess.run(
                [sys.executable,
                 str(Path(__file__).resolve().parent / "gap_report.py"),
                 "render", "--spring-repo", str(spring)],
                capture_output=True, text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            out = Path(proc.stdout.strip())
            self.assertTrue(out.is_file())
            self.assertIn("boot_failure", out.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
