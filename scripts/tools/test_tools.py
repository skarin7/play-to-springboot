#!/usr/bin/env python3
"""
Tests for the deterministic helpers. Stdlib unittest, no dependencies.

    python3 scripts/tools/test_tools.py

These cover the pieces every later stage trusts blindly: layer classification,
Maven error parsing, and state round-tripping. If any of these are wrong,
verification silently reports success on a broken migration.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import parse_mvn  # noqa: E402
import routes as routes_mod  # noqa: E402
import signature_diff  # noqa: E402
import state  # noqa: E402
import verify  # noqa: E402
from layers import (classify, classify_legacy, divergences,  # noqa: E402
                    jar_has_layer_fix)


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


class TestJarVersionCheck(unittest.TestCase):
    """
    The check must inspect the JAR, not the project layout.

    An earlier version asked "would a pre-fix JAR misclassify these paths?",
    which is a property of the layout - so it fired on every flat-layout project
    forever, including correctly configured ones.
    """

    def _jar(self, path: Path, entries: list[str]) -> Path:
        import zipfile
        with zipfile.ZipFile(path, "w") as z:
            for e in entries:
                z.writestr(e, "")
        return path

    def test_jar_with_marker_is_current(self):
        with tempfile.TemporaryDirectory() as d:
            jar = self._jar(
                Path(d) / "dev-toolkit-1.0.0.jar",
                ["com/phenom/devtoolkit/LayerDetector.class",
                 "com/phenom/devtoolkit/SignatureExtractor.class"],
            )
            self.assertIs(jar_has_layer_fix(jar), True)

    def test_jar_without_marker_is_stale(self):
        with tempfile.TemporaryDirectory() as d:
            jar = self._jar(
                Path(d) / "dev-toolkit-1.0.0.jar",
                ["com/phenom/devtoolkit/LayerDetector.class"],
            )
            self.assertIs(jar_has_layer_fix(jar), False)

    def test_missing_jar_is_unknown_not_stale(self):
        # Absent must not read as broken; the caller reports "not_found".
        self.assertIsNone(jar_has_layer_fix(Path("/nonexistent/dev-toolkit-1.0.0.jar")))

    def test_non_zip_is_unknown(self):
        with tempfile.TemporaryDirectory() as d:
            junk = Path(d) / "dev-toolkit-1.0.0.jar"
            junk.write_text("not a jar", encoding="utf-8")
            self.assertIsNone(jar_has_layer_fix(junk))


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
