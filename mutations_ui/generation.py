"""Persistent nine-image generation jobs and a serialized, direct diffusion worker."""

import json
import hashlib
import subprocess
import threading
import uuid
from pathlib import Path

from .demo import catalog
from .feedback import ValidationError
from .genetics import initial_population, reproduce, available_models, MODEL_REGISTRY, EVOLUTION_CONFIG, EVOLUTION_VERSION, validate_genome
from .runtime import PROJECT, ensure_runtime, environment
from .storage import ConflictError, now

LIVE_MODE = "local-diffusion"


class GenerationService:
    def __init__(self, store, auto_start=True):
        self.store, self.auto_start = store, auto_start
        self.lock = threading.RLock()
        self.thread = self.process = self.active_id = None
        self.closed = threading.Event()

    def _insert(self, db, population, preset, session_id=None, round_number=1, parent=None):
        preset_info = next((p for p in catalog()["presets"] if p["id"] == preset), None)
        if preset_info is None:
            raise ValidationError("Unknown research question.")
        gid = uuid.uuid4().hex
        session_id = session_id or uuid.uuid4().hex[:12]
        candidates = [{**item, "id": f"{session_id}:r{round_number}:c{i+1:02}",
                       "label": f"{MODEL_REGISTRY[item['genome'].get('model_id', 'sd15-lcm')]['label']} · seed {item['genome']['seed']}",
                       "media": None, "phenotype": {"status": "queued", "assets": [], "execution": None},
                       "evaluation": None,
                       "provenance": {"kind": "new_model_inference", "source_prompts": "local prompt catalog"}}
                      for i, item in enumerate(population)]
        payload = {"title": "First generated variations" if round_number == 1 else f"New descendants · round {round_number}",
                   "experiment": "My Word · local image generation", "prompt": preset_info["question"],
                   "is_demo": False, "candidates": candidates,
                   "algorithm": {"version": EVOLUTION_VERSION, "config": EVOLUTION_CONFIG},
                   "validation": {"duplicate_assets": [], "duplicate_genes": [], "warnings": []},
                   "demo": {"mode": LIVE_MODE, "round": round_number, "preset": preset,
                            "session_id": session_id, "parent_generation_id": parent["generation_id"] if parent else None,
                            "parent_review_id": parent["review_id"] if parent else None},
                   "generation": {"status": "queued", "message": "Preparing to generate nine new images",
                                  "completed": 0, "total": 9, "error": None, "updated_at": now()}}
        db.execute("INSERT INTO generations(id, created_at, payload) VALUES (?, ?, ?)", (gid, now(), json.dumps(payload)))
        return gid

    def bootstrap(self):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT value FROM app_state WHERE key='live-generation-v2'").fetchone()
            if row:
                gid = row["value"]
            else:
                gid = self._insert(db, initial_population(), "overview")
                db.execute("INSERT INTO app_state VALUES ('live-generation-v2', ?)", (gid,))
        self.wake()
        return gid

    def start_session(self, preset="overview", model_ids=None):
        try:
            population = initial_population(preset, model_ids)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        with self.store.connect() as db:
            gid = self._insert(db, population, preset)
        self.wake()
        return self.store.get_generation(gid)

    def next_generation(self, gid, expected_revision):
        if type(expected_revision) is not int or expected_revision < 1:
            raise ValidationError("Save your feedback before generating descendants.")
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM generations WHERE id=?", (gid,)).fetchone()
            if row is None:
                raise KeyError("Generation not found.")
            review_row = db.execute("SELECT * FROM reviews WHERE generation_id=? ORDER BY revision DESC LIMIT 1", (gid,)).fetchone()
            if review_row is None:
                raise ValidationError("Save your feedback before generating descendants.")
            if review_row["revision"] != expected_revision:
                raise ConflictError("The submitted feedback changed. Reload before continuing.")
            generation = self.store._generation(row, review_row)
            if generation.get("demo", {}).get("mode") != LIVE_MODE:
                raise ValidationError("This generation is not a live image-generation session.")
            if generation["generation"]["status"] != "ready":
                raise ValidationError("Wait until all nine images have been generated.")
            existing = db.execute("SELECT child_id FROM demo_continuations WHERE generation_id=? AND review_revision=?", (gid, expected_revision)).fetchone()
            if existing:
                child_id = existing["child_id"]
            else:
                try:
                    population = reproduce(generation, generation["review"])
                except ValueError as exc:
                    raise ValidationError(str(exc)) from exc
                demo = generation["demo"]
                child_id = self._insert(db, population, demo["preset"], demo["session_id"], demo["round"] + 1,
                                        {"generation_id": gid, "review_id": generation["review"]["id"]})
                db.execute("INSERT INTO demo_continuations VALUES (?, ?, ?)", (gid, expected_revision, child_id))
        self.wake()
        return self.store.get_generation(child_id)

    def update(self, gid, apply):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM generations WHERE id=?", (gid,)).fetchone()
            payload = json.loads(row["payload"])
            apply(payload)
            payload["generation"]["updated_at"] = now()
            db.execute("UPDATE generations SET payload=? WHERE id=?", (json.dumps(payload), gid))

    def retry(self, gid):
        generation = self.store.get_generation(gid)
        if generation.get("demo", {}).get("mode") != LIVE_MODE:
            raise ValidationError("Only generated populations have generation jobs.")
        if generation["generation"]["status"] not in ("error", "paused", "queued"):
            raise ValidationError("This generation is already running or complete.")
        with self.lock:
            self.update(gid, lambda p: p["generation"].update(status="queued", error=None, message="Resuming unfinished images"))
        self.wake()
        return self.store.get_generation(gid)

    def pause(self, gid):
        with self.lock:
            generation = self.store.get_generation(gid)
            if generation.get("generation", {}).get("status") not in ("queued", "preparing", "generating"):
                raise ValidationError("This generation is not running.")
            self.update(gid, lambda p: p["generation"].update(status="paused", message="Generation paused; completed images are saved"))
            if self.active_id == gid and self.process and self.process.poll() is None:
                self.process.terminate()
        return self.store.get_generation(gid)

    def wake(self):
        if not self.auto_start or self.closed.is_set():
            return
        with self.lock:
            if self.thread is None:
                self.thread = threading.Thread(target=self._run, daemon=True, name="image-generation")
                self.thread.start()

    def _run(self):
        while not self.closed.is_set():
            with self.lock:
                pending = [g for g in reversed(self.store.generations())
                           if g.get("demo", {}).get("mode") == LIVE_MODE
                           and g["generation"]["status"] in ("queued", "preparing", "generating")]
                if not pending:
                    self.thread = None
                    return
                self.active_id = pending[0]["id"]
            try:
                self._render(pending[0])
            except Exception as exc:
                if not self.closed.is_set() and self.store.get_generation(self.active_id)["generation"]["status"] != "paused":
                    self.update(self.active_id, lambda p: p["generation"].update(
                        status="error", error=str(exc)[:1200], message="Generation stopped. Retry to resume unfinished images."))
            finally:
                with self.lock:
                    self.process = None
                    self.active_id = None
        with self.lock:
            self.thread = None

    def _render(self, generation):
        gid = generation["id"]
        def progress(message):
            self.update(gid, lambda p: p["generation"].update(message=message))
        with self.lock:
            current = self.store.get_generation(gid)
            if current["generation"]["status"] == "paused" or self.closed.is_set():
                return
            self.update(gid, lambda p: p["generation"].update(status="preparing", error=None, message="Preparing the local image generator"))
        self.store.root.joinpath("jobs").mkdir(exist_ok=True)
        target = self.store.root / "generated" / gid
        manifest = {"cache_dir": str(PROJECT / ".mutations" / "models"), "candidates": [
            {"slot": i, "genome": c["genome"], "output": str(target / f"{i+1:02}.png")}
            for i, c in enumerate(generation["candidates"]) if c["media"] is None]}
        for candidate in generation["candidates"]:
            if candidate["media"] is None:
                validate_genome(candidate["genome"])
        manifest_path = self.store.root / "jobs" / f"{gid}.json"
        manifest_path.write_text(json.dumps(manifest))
        if not manifest["candidates"]:
            self.update(gid, lambda p: p["generation"].update(status="ready", completed=9, message="Nine new images are ready"))
            return
        error_message = None
        with (self.store.root / "generation.log").open("a") as log:
            executable = ensure_runtime(log, progress)
            with self.lock:
                state = self.store.get_generation(gid)["generation"]
                if self.closed.is_set() or state["status"] in ("paused", "queued"):
                    return
                self.update(gid, lambda p: p["generation"].update(status="generating"))
                self.process = subprocess.Popen([str(executable), "-u", "-m", "mutations_ui.model_worker", str(manifest_path)],
                    cwd=PROJECT, env=environment(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=log, text=True)
                process = self.process
            try:
                for line in process.stdout:
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        log.write(line)
                        continue
                    if not isinstance(event, dict):
                        continue
                    if event.get("event") == "stage":
                        self.update(gid, lambda p: p["generation"].update(message=event["message"], device=event.get("device")))
                    elif event.get("event") == "step":
                        self.update(gid, lambda p: p["generation"].update(current_slot=event["slot"], step=event["step"], steps=event["steps"]))
                    elif event.get("event") == "image":
                        data = Path(event["path"]).read_bytes()
                        if not data:
                            raise RuntimeError("The image worker produced an empty output.")
                        try:
                            from PIL import Image
                            with Image.open(Path(event["path"])) as image:
                                image.verify()
                        except Exception as exc:
                            raise RuntimeError(f"Invalid image output: {exc}") from exc
                        media = self.store.add_asset(f"generated-{event['slot']+1:02}.png", data)
                        event["execution"]["output_sha256"] = hashlib.sha256(data).hexdigest()
                        def record(payload):
                            payload["candidates"][event["slot"]].update(media=media, execution=event["execution"])
                            payload["candidates"][event["slot"]]["phenotype"] = {
                                "status": "complete", "assets": [media], "execution": event["execution"]}
                            hashes = [c["media"]["sha256"] for c in payload["candidates"] if c.get("media")]
                            payload["validation"]["duplicate_assets"] = sorted({h for h in hashes if hashes.count(h) > 1})
                            genes = [json.dumps(c["genome"].get("gene"), sort_keys=True) for c in payload["candidates"]]
                            payload["validation"]["duplicate_genes"] = sorted({g for g in genes if genes.count(g) > 1})
                            payload["generation"]["completed"] = sum(c["media"] is not None for c in payload["candidates"])
                        self.update(gid, record)
                    elif event.get("event") == "error":
                        error_message = event["message"]
            finally:
                if process.poll() is None:
                    process.terminate()
                code = process.wait()
        state = self.store.get_generation(gid)["generation"]
        if self.closed.is_set() or state["status"] in ("paused", "queued"):
            return
        if code or state["completed"] != 9:
            raise RuntimeError(error_message or "The image worker stopped. See generation.log in the workspace for details.")
        def complete(payload):
            duplicate_assets = payload.get("validation", {}).get("duplicate_assets", [])
            warnings = payload.setdefault("validation", {}).setdefault("warnings", [])
            if duplicate_assets and "duplicate image outputs" not in warnings:
                warnings.append("duplicate image outputs")
            payload["generation"].update(status="ready", message="Nine newly generated images are ready", error=None)
        self.update(gid, complete)

    def close(self):
        self.closed.set()
        with self.lock:
            if self.process and self.process.poll() is None:
                self.process.terminate()
            thread = self.thread
        if thread:
            thread.join(timeout=5)
