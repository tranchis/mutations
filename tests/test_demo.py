import tempfile
import unittest

from mutations_ui.demo import catalog
from mutations_ui.feedback import ValidationError
from mutations_ui.storage import Store


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


if __name__ == "__main__":
    unittest.main()
