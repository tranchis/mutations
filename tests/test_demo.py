import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from mutations_ui.demo import BUNDLE, bootstrap, catalog, next_generation
from mutations_ui.feedback import ValidationError
from mutations_ui.storage import ConflictError, Store


def feedback(generation, decision="keep"):
    return {"expected_revision": 0, "reviewer": "Demo reviewer", "note": "Follow the image's particular details",
            "favourite_id": generation["candidates"][0]["id"] if decision == "keep" else None,
            "candidates": [{"candidate_id": c["id"], "decision": decision if i == 0 else "reject" if i == 1 else "undecided",
                            "scores": {"relationship": None, "departure": None, "specificity": None}, "note": ""}
                           for i, c in enumerate(generation["candidates"])]}


class DemoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)

    def test_catalog_preserves_original_prompts_and_archive_hashes(self):
        research = catalog()
        self.assertEqual(len(research["samples"]), 14)
        self.assertEqual(len(research["prompts"]), 15)
        prompts = {p["id"]: p["text"] for p in research["prompts"]}
        self.assertEqual(prompts["control"], "two persons")
        self.assertIn("oil painting by delacroix", prompts["MyWord_02A"])
        self.assertNotIn("oil painting", prompts["MyWord_02A_sp"])
        self.assertIn("in front of a tamara natalie madden painting", prompts["MyWord_07A_sp"])
        for sample in research["samples"]:
            raw = (BUNDLE / sample["file"]).read_bytes()
            self.assertTrue(raw.startswith(b"\xff\xd8"))
            self.assertEqual(hashlib.sha256(raw).hexdigest(), sample["sha256"])
            self.assertIn(sample["source"]["page"], (10, 11, 12))
            self.assertIn(sample["prompt_id"], prompts)

    def test_bootstrap_is_ready_with_no_upload_or_configuration(self):
        gid = bootstrap(self.store)
        generation = self.store.get_generation(gid)
        self.assertEqual(len(generation["candidates"]), 9)
        self.assertEqual(len({c["archive_id"] for c in generation["candidates"]}), 9)
        self.assertEqual(generation["demo"]["mode"], "archive-replay")
        self.assertIsNone(generation["review"])
        for candidate in generation["candidates"]:
            self.assertTrue((self.store.assets / Path(candidate["media"]["url"]).name).is_file())
            self.assertEqual(candidate["provenance"]["document"], "main.pdf")

    def test_unknown_original_parameters_are_not_invented(self):
        generation = self.store.get_generation(bootstrap(self.store))
        flux = next(c for c in generation["candidates"] if c["archive_id"] == "flux-02a")
        ltx = next(c for c in generation["candidates"] if c["archive_id"] == "ltx-02b")
        self.assertIsNone(flux["genome"]["seed"])
        self.assertIsNone(flux["genome"]["other_generation_parameters"])
        self.assertEqual(ltx["genome"]["seed"], 49)
        self.assertEqual(ltx["provenance"]["frame_time_seconds"], 2)

    def test_bootstrap_reuses_workspace_and_preserves_submitted_feedback(self):
        gid = bootstrap(self.store)
        generation = self.store.get_generation(gid)
        self.store.save_review(gid, feedback(generation))
        self.assertEqual(bootstrap(Store(self.temp.name)), gid)
        self.assertEqual(len(self.store.generations()), 1)
        self.assertEqual(self.store.get_generation(gid)["review"]["revision"], 1)

    def test_bootstrap_does_not_replace_existing_generations(self):
        asset = self.store.add_asset("own.png", b"image")
        own = self.store.create_generation({"title": "My own images", "candidates": [
            {"id": f"own-{i}", "asset_id": asset["asset_id"]} for i in range(9)]})
        bootstrap(self.store)
        self.assertEqual(self.store.get_generation(own["id"])["title"], "My own images")
        self.assertEqual(len(self.store.generations()), 2)

    def test_next_batch_preserves_preferred_direction_and_selection_context(self):
        generation = self.store.get_generation(bootstrap(self.store))
        review = self.store.save_review(generation["id"], feedback(generation))
        child = next_generation(self.store, generation["id"], review["revision"])
        self.assertEqual(len(child["candidates"]), 9)
        self.assertEqual(len({c["archive_id"] for c in child["candidates"]}), 9)
        self.assertIn(generation["candidates"][0]["archive_id"], [c["archive_id"] for c in child["candidates"]])
        self.assertEqual(child["demo"]["round"], 2)
        self.assertEqual(child["demo"]["session_id"], generation["demo"]["session_id"])
        self.assertEqual(child["demo"]["parent_review_id"], review["id"])
        self.assertTrue(any(c["archive_id"] not in {p["archive_id"] for p in generation["candidates"]} for c in child["candidates"]))
        for candidate in child["candidates"]:
            self.assertEqual(candidate["selection_parents"], [generation["candidates"][0]["id"]])
            self.assertEqual(candidate["parents"], [])

    def test_continuation_is_idempotent_for_a_submitted_revision(self):
        generation = self.store.get_generation(bootstrap(self.store))
        self.store.save_review(generation["id"], feedback(generation))
        first = next_generation(self.store, generation["id"], 1)
        second = next_generation(self.store, generation["id"], 1)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(self.store.generations()), 2)

    def test_no_feedback_or_no_kept_candidates_cannot_advance(self):
        generation = self.store.get_generation(bootstrap(self.store))
        with self.assertRaises(ValidationError):
            next_generation(self.store, generation["id"], 1)
        self.store.save_review(generation["id"], feedback(generation, "reject"))
        with self.assertRaisesRegex(ValidationError, "paused"):
            next_generation(self.store, generation["id"], 1)
        self.assertEqual(len(self.store.generations()), 1)

    def test_stale_review_cannot_advance_and_new_revision_can_branch(self):
        generation = self.store.get_generation(bootstrap(self.store))
        value = feedback(generation)
        self.store.save_review(generation["id"], value)
        first = next_generation(self.store, generation["id"], 1)
        value["expected_revision"] = 1
        value["note"] = "Reconsidered after looking again"
        review = self.store.save_review(generation["id"], value)
        with self.assertRaises(ConflictError):
            next_generation(self.store, generation["id"], 1)
        second = next_generation(self.store, generation["id"], 2)
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(second["demo"]["parent_review_id"], review["id"])

    def test_each_preset_works_and_new_session_preserves_earlier_history(self):
        session_ids = []
        for preset in catalog()["presets"]:
            generation = self.store.create_demo(preset["id"])
            session_ids.append(generation["demo"]["session_id"])
            self.assertEqual(len(generation["candidates"]), 9)
            self.assertEqual([c["archive_id"] for c in generation["candidates"]], preset["samples"])
        self.assertEqual(len(set(session_ids)), 3)
        self.assertEqual(len(self.store.generations()), 3)
        with self.assertRaises(ValidationError):
            self.store.create_demo("missing-question")

    def test_demo_export_is_available_with_explicit_demo_flag(self):
        generation = self.store.get_generation(bootstrap(self.store))
        self.store.save_review(generation["id"], feedback(generation))
        self.assertEqual(self.store.export()["generations"], [])
        included = self.store.export(include_demo=True)
        self.assertEqual(len(included["comparisons"]), 1)
        self.assertTrue(included["comparisons"][0]["is_demo"])
        self.assertEqual(included["generations"][0]["demo"]["source_document"], "main.pdf")


if __name__ == "__main__":
    unittest.main()
