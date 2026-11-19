import tempfile
import unittest

from mutations_ui.demo import catalog
from mutations_ui.feedback import ValidationError
from mutations_ui.storage import Store
from mutations_ui.generation import GenerationService
from mutations_ui.genetics import MODEL_REGISTRY, initial_population, reproduce, validate_genome


class CatalogTests(unittest.TestCase):
    def test_catalog_contains_only_live_generation_inputs(self):
        value = catalog()
        self.assertEqual(set(value), {"version", "title", "prompts", "presets"})
        self.assertEqual(len(value["prompts"]), 15)
        self.assertEqual(len(value["presets"]), 3)
        self.assertTrue(all("samples" not in preset for preset in value["presets"]))
        self.assertIn("oil painting by delacroix", value["prompts"][0]["text"])

    def test_legacy_archive_demo_is_disabled(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(ValidationError):
                Store(root).create_demo()

    def test_reproduction_keeps_adapters_compatible_with_models(self):
        candidates = initial_population(model_ids=["sd15-lcm", "dreamshaper-8"])
        generation = {"candidates": [{**candidate, "id": f"c{i}"} for i, candidate in enumerate(candidates)]}
        review = {"candidates": [{"candidate_id": f"c{i}", "decision": "keep"} for i in range(9)],
                  "favourite_id": None}
        for child in reproduce(generation, review):
            gene = child["genome"]["gene"]
            self.assertEqual(gene["adapter"], MODEL_REGISTRY[gene["model"]]["adapter"])
            self.assertTrue(validate_genome(child["genome"]))

    def test_reproduction_does_not_repeat_parent_or_sibling_seeds(self):
        candidates = initial_population(model_ids=["sd15-lcm"])
        generation = {"candidates": [{**candidate, "id": f"c{i}"} for i, candidate in enumerate(candidates)]}
        review = {"candidates": [
            {"candidate_id": f"c{i}", "decision": "keep" if i == 0 else "reject"}
            for i in range(9)
        ], "favourite_id": "c0"}

        children = reproduce(generation, review)
        parent_seeds = {candidate["genome"]["gene"]["seed"] for candidate in generation["candidates"]}
        child_seeds = [child["genome"]["gene"]["seed"] for child in children]

        self.assertEqual(len(child_seeds), len(set(child_seeds)))
        self.assertTrue(parent_seeds.isdisjoint(child_seeds))
        for child in children:
            self.assertTrue(any(op["type"] == "diversity_guard" for op in child["operators"]))

    def test_retry_repairs_an_existing_incompatible_adapter(self):
        with tempfile.TemporaryDirectory() as root:
            service = GenerationService(Store(root), auto_start=False)
            generation = service.start_session(model_ids=["dreamshaper-8"])
            service.update(generation["id"], lambda payload: (
                payload["candidates"][0]["genome"]["gene"].update(adapter=MODEL_REGISTRY["sd15-lcm"]["adapter"]),
                payload["generation"].update(status="error", error="Adapter is incompatible with model.")
            ))
            repaired = service.retry(generation["id"])
            self.assertIsNone(repaired["candidates"][0]["genome"]["gene"]["adapter"])
            self.assertEqual(repaired["generation"]["status"], "queued")
            self.assertEqual(repaired["candidates"][0]["operators"][-1]["type"], "compatibility_repair")
            service.close()


if __name__ == "__main__":
    unittest.main()
