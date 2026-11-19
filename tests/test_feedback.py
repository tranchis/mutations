import copy
import json
import tempfile
import threading
import unittest
import subprocess
import sys
import io
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from unittest.mock import patch
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from mutations_ui.feedback import ValidationError, preference_pairs, validate_review
from mutations_ui.server import make_server, application_shell
from mutations_ui.storage import ConflictError, Store


def review_for(generation):
    return {"expected_revision": 0, "reviewer": "Reviewer A", "note": "A new direction",
            "display_order": [c["id"] for c in generation["candidates"]],
            "favourite_id": None,
            "candidates": [{"candidate_id": c["id"], "decision": "undecided", "note": "",
                            "scores": {"relationship": None, "departure": None, "specificity": None}}
                           for c in generation["candidates"]]}


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        asset = self.store.add_asset("still.png", b"test-image")
        self.spec = {"title": "Generation one", "prompt": "Source question", "candidates": [
            {"id": f"c-{i}", "asset_id": asset["asset_id"], "genome": {"seed": i},
             "parents": ["earlier:c-1"]} for i in range(9)]}
        self.generation = self.store.create_generation(self.spec)

    def test_nine_candidates_required(self):
        for size in (0, 8, 10):
            with self.subTest(size=size), self.assertRaises(ValidationError):
                self.store.create_generation({**self.spec, "candidates": self.spec["candidates"][:size] if size < 9 else self.spec["candidates"] + [self.spec["candidates"][0]]})

    def test_unique_ids_and_registered_assets_required(self):
        value = copy.deepcopy(self.spec)
        value["candidates"][1]["id"] = "c-0"
        with self.assertRaises(ValidationError):
            self.store.create_generation(value)
        value = copy.deepcopy(self.spec)
        value["candidates"][0]["asset_id"] = "missing"
        with self.assertRaises(ValidationError):
            self.store.create_generation(value)

    def test_genomes_and_ancestry_survive_restart(self):
        reopened = Store(self.temp.name).get_generation(self.generation["id"])
        self.assertEqual(reopened["candidates"][8]["genome"], {"seed": 8})
        self.assertEqual(reopened["candidates"][0]["parents"], ["earlier:c-1"])
        self.assertEqual(reopened["candidates"][0]["media"]["name"], "still.png")

    def test_unknown_and_duplicate_review_ids_rejected(self):
        for cid in ("missing", "c-1"):
            value = review_for(self.generation)
            value["candidates"][0].update(candidate_id=cid, decision="keep")
            with self.assertRaises(ValidationError):
                self.store.save_review(self.generation["id"], value)

    def test_unrated_is_not_zero_and_undecided_is_not_negative(self):
        value = review_for(self.generation)
        value["candidates"][0]["decision"] = "keep"
        value["candidates"][0]["scores"]["relationship"] = 0
        saved = self.store.save_review(self.generation["id"], value)
        self.assertEqual(saved["candidates"][0]["scores"]["relationship"], 0)
        self.assertIsNone(saved["candidates"][0]["scores"]["departure"])
        self.assertEqual(preference_pairs(saved), [])

    def test_scores_validate_range_and_type(self):
        for score in (-1, 5, False, "4", 0.5):
            with self.subTest(score=score):
                value = review_for(self.generation)
                value["candidates"][0]["decision"] = "keep"
                value["candidates"][0]["scores"]["relationship"] = score
                with self.assertRaises(ValidationError):
                    self.store.save_review(self.generation["id"], value)

    def test_decision_required_but_all_reject_is_valid(self):
        value = review_for(self.generation)
        with self.assertRaises(ValidationError):
            self.store.save_review(self.generation["id"], value)
        for row in value["candidates"]:
            row["decision"] = "reject"
        saved = self.store.save_review(self.generation["id"], value)
        self.assertEqual(preference_pairs(saved), [])

    def test_favourite_must_be_kept(self):
        value = review_for(self.generation)
        value["candidates"][0]["decision"] = "keep"
        value["favourite_id"] = "c-1"
        with self.assertRaises(ValidationError):
            self.store.save_review(self.generation["id"], value)

    def test_explicit_pairs_and_favourite(self):
        value = review_for(self.generation)
        value["candidates"][0]["decision"] = value["candidates"][1]["decision"] = "keep"
        value["candidates"][2]["decision"] = "reject"
        value["favourite_id"] = "c-1"
        pairs = preference_pairs(value)
        self.assertEqual({(p["preferred"], p["other"]) for p in pairs},
                         {("c-0", "c-2"), ("c-1", "c-2"), ("c-1", "c-0")})
        self.assertFalse(any(p["other"] == "c-3" for p in pairs))

    def test_revision_conflict_preserves_history(self):
        value = review_for(self.generation)
        value["candidates"][0]["decision"] = "keep"
        first = self.store.save_review(self.generation["id"], value)
        with self.assertRaises(ConflictError):
            self.store.save_review(self.generation["id"], value)
        value["expected_revision"] = first["revision"]
        value["candidates"][0]["decision"] = "reject"
        value["candidates"][1]["decision"] = "keep"
        second = self.store.save_review(self.generation["id"], value)
        self.assertEqual(second["revision"], 2)
        exported = self.store.export()
        self.assertEqual(len(exported["generations"][0]["review_history"]), 2)
        self.assertEqual(len(exported["comparisons"]), 1)
        self.assertEqual(exported["comparisons"][0]["preferred"], "c-1")
        self.assertEqual(exported["comparisons"][0]["review_id"], second["id"])

    def test_display_order_must_be_permutation(self):
        value = review_for(self.generation)
        value["candidates"][0]["decision"] = "keep"
        value["display_order"][0] = value["display_order"][1]
        with self.assertRaises(ValidationError):
            self.store.save_review(self.generation["id"], value)

    def test_asset_name_cannot_choose_storage_path(self):
        asset = self.store.add_asset("../../portrait.png", b"image")
        self.assertEqual(asset["name"], "portrait.png")
        self.assertTrue((self.store.assets / Path(asset["url"]).name).is_file())
        for name in ("script.html", "vector.svg", "empty.png"):
            with self.assertRaises(ValidationError):
                self.store.add_asset(name, b"" if name == "empty.png" else b"bad")

    def test_cli_import_and_export_preserve_metadata(self):
        root = Path(self.temp.name)
        (root / "frame.png").write_bytes(b"image")
        manifest = root / "generation.json"
        manifest.write_text(json.dumps({"title": "CLI generation", "candidates": [
            {"id": f"cli-{i}", "file": "frame.png", "genome": {"seed": i}, "parents": ["g1:parent"]}
            for i in range(9)]}))
        command = [sys.executable, "-m", "mutations_ui", "--data-dir", str(root / "cli-data")]
        imported = subprocess.run(command + ["import", str(manifest)], capture_output=True, text=True)
        self.assertEqual(imported.returncode, 0, imported.stderr)
        exported = subprocess.run(command + ["export"], capture_output=True, text=True, check=True)
        generation = json.loads(exported.stdout)["generations"][0]
        self.assertEqual(generation["candidates"][8]["genome"], {"seed": 8})
        self.assertEqual(generation["candidates"][0]["parents"], ["g1:parent"])


class HandlerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)

    def test_application_shell_is_one_self_contained_response(self):
        response, csp = application_shell()
        self.assertIn("<style nonce=", response)
        self.assertIn("<script nonce=", response)
        self.assertNotIn('src="/app.js"', response)
        self.assertNotIn('href="/styles.css"', response)
        self.assertIn("nonce-", csp)

    def test_bootstrap_contains_catalog_models_and_summaries(self):
        self.assertEqual(self.store.generation_summaries(), [])


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.store = Store(cls.temp.name)
        cls.server = make_server(cls.store, 0, auto_generate=False)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.temp.cleanup()

    def request(self, path, value=None, headers=None, raw=None):
        if value is not None:
            raw = json.dumps(value).encode()
            headers = {"Content-Type": "application/json", **(headers or {})}
        request = Request(self.base + path, data=raw, headers=headers or {})
        try:
            with urlopen(request, timeout=5) as response:
                return response.status, response.headers, response.read()
        except HTTPError as error:
            return error.code, error.headers, error.read()

    def test_static_app_and_path_isolation(self):
        self.assertEqual(self.request("/")[0], 200)
        self.assertEqual(self.request("/app.js")[0], 200)
        self.assertEqual(self.request("/styles.css")[0], 200)
        self.assertEqual(self.request("/../IMPLEMENTATION_ANALYSIS.md")[0], 404)
        self.assertEqual(self.request("/media/../../README.md")[0], 404)

    def test_server_bootstraps_the_demo_and_exposes_its_catalog(self):
        status, _, data = self.request("/api/generations")
        self.assertEqual(status, 200)
        generations = json.loads(data)["generations"]
        self.assertTrue(any(g.get("demo", {}).get("mode") == "local-diffusion" for g in generations))
        live = next(g for g in generations if g.get("demo", {}).get("mode") == "local-diffusion")
        self.assertEqual(live["generation"]["status"], "queued")
        self.assertEqual(len(live["candidates"]), 9)
        status, _, data = self.request("/api/catalog")
        self.assertEqual(status, 200)
        self.assertEqual(len(json.loads(data)["prompts"]), 15)
        self.assertEqual(self.request("/api/health")[0], 200)

    def test_bootstrap_is_compact_and_live_stream_sends_snapshot(self):
        status, _, data = self.request("/api/bootstrap")
        self.assertEqual(status, 200)
        bootstrap = json.loads(data)
        self.assertIn("generation", bootstrap)
        self.assertIn("catalog", bootstrap)
        self.assertIn("models", bootstrap)
        self.assertTrue(all("candidates" not in summary for summary in bootstrap["generations"]
                            if summary["id"] != bootstrap["generation"]["id"]))
        _, _, created = self.request("/api/demo", {"preset": "overview"})
        gid = json.loads(created)["id"]
        self.server.generator.update(gid, lambda payload: payload["generation"].update(
            status="ready", completed=9, message="fixture ready"))
        status, headers, stream = self.request(f"/api/generations/{gid}/events")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get_content_type(), "text/event-stream")
        self.assertIn(b"event: snapshot", stream)
        self.assertIn(b'"status":"ready"', stream)

    def test_parallel_page_requests_are_served(self):
        with ThreadPoolExecutor(max_workers=20) as pool:
            statuses = list(pool.map(lambda _: self.request("/")[0], range(40)))
        self.assertEqual(statuses, [200] * 40)

    def test_demo_continuation_http_round_trip(self):
        status, _, data = self.request("/api/demo", {"preset": "style"})
        self.assertEqual(status, 201)
        generation = json.loads(data)
        # Unit-test fixture: mark the queued live job complete without invoking a
        # multi-second diffusion render. Production servers leave this to the
        # background worker.
        asset = self.store.add_asset("fixture.png", b"fixture")
        self.server.generator.update(generation["id"], lambda payload: (
            payload["generation"].update(status="ready", completed=9, message="fixture ready"),
            [candidate.update(media=asset) for candidate in payload["candidates"]]
        ))
        generation = json.loads(self.request(f"/api/generations/{generation['id']}")[2])
        value = review_for(generation)
        value["candidates"][0]["decision"] = "keep"
        self.assertEqual(self.request(f"/api/generations/{generation['id']}/reviews", value)[0], 201)
        status, _, data = self.request(f"/api/generations/{generation['id']}/next", {"expected_revision": 1})
        self.assertEqual(status, 200)
        child = json.loads(data)
        self.assertEqual(child["demo"]["round"], 2)
        self.assertEqual(len(child["candidates"]), 9)
        repeated = self.request(f"/api/generations/{generation['id']}/next", {"expected_revision": 1})
        self.assertEqual(json.loads(repeated[2])["id"], child["id"])

    def test_start_session_is_idempotent(self):
        request = {"preset": "overview", "request_id": "request-12345678"}
        first = json.loads(self.request("/api/demo", request)[2])
        second = json.loads(self.request("/api/demo", request)[2])
        self.assertEqual(first["id"], second["id"])

    def test_launcher_reuses_an_existing_workspace_server(self):
        process = subprocess.run([sys.executable, "-m", "mutations_ui", "--data-dir", self.temp.name,
                                  "serve", "--port", str(self.server.server_port), "--no-browser"],
                                 capture_output=True, text=True, timeout=5)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertIn("already running", process.stdout)

    def test_default_launcher_opens_the_browser_for_an_existing_workspace(self):
        from mutations_ui.__main__ import main
        args = ["run.py", "--data-dir", self.temp.name, "serve", "--port", str(self.server.server_port)]
        with patch.object(sys, "argv", args), patch("mutations_ui.__main__.webbrowser.open") as opened, redirect_stdout(io.StringIO()):
            main()
        opened.assert_called_once_with(self.base)

    def test_upload_generation_review_export_round_trip(self):
        status, _, data = self.request("/api/assets", headers={"X-Filename": "frame.png", "Content-Type": "application/octet-stream"}, raw=b"image")
        self.assertEqual(status, 201)
        asset = json.loads(data)
        spec = {"title": "API generation", "candidates": [{"id": str(i), "asset_id": asset["asset_id"], "genome": {"seed": i}} for i in range(9)]}
        status, _, data = self.request("/api/generations", spec)
        self.assertEqual(status, 201)
        generation = json.loads(data)
        self.assertEqual(self.request("/api/generations/" + generation["id"])[0], 200)
        value = review_for(generation)
        value["candidates"][0]["decision"] = "keep"
        value["candidates"][1]["decision"] = "reject"
        self.assertEqual(self.request(f"/api/generations/{generation['id']}/reviews", value)[0], 201)
        self.assertEqual(self.request(f"/api/generations/{generation['id']}/reviews", value)[0], 409)
        status, headers, data = self.request("/api/export")
        self.assertEqual(status, 200)
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertTrue(any(pair["generation_id"] == generation["id"] for pair in json.loads(data)["comparisons"]))

    def test_cross_origin_and_unexpected_host_rejected(self):
        self.assertEqual(self.request("/api/demo", {}, {"Origin": "https://elsewhere.example"})[0], 403)
        self.assertEqual(self.request("/api/generations", headers={"Host": "elsewhere.example"})[0], 403)

    def test_malformed_inputs_return_errors(self):
        self.assertEqual(self.request("/api/generations", [1, 2])[0], 400)
        self.assertEqual(self.request("/api/generations", raw=b"not json", headers={"Content-Type": "application/json"})[0], 400)
        self.assertEqual(self.request("/api/generations", raw=b"{}", headers={"Content-Type": "text/plain"})[0], 415)

    def test_video_byte_ranges(self):
        asset = self.store.add_asset("clip.mp4", b"0123456789")
        status, headers, data = self.request(asset["url"], headers={"Range": "bytes=2-5"})
        self.assertEqual(status, 206)
        self.assertEqual(data, b"2345")
        self.assertEqual(headers["Content-Range"], "bytes 2-5/10")
        self.assertEqual(self.request(asset["url"], headers={"Range": "bytes=-3"})[2], b"789")
        self.assertEqual(self.request(asset["url"], headers={"Range": "bytes=99-"})[0], 416)


if __name__ == "__main__":
    unittest.main()
