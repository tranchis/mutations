"""Ready-to-run archive replay using the actual stills and prompts in main.pdf.

Selection changes archive sampling. It never represents an archived still as a newly
generated image or invents the missing original generation parameters.
"""

import hashlib
import json
import random
import uuid
from collections import Counter
from functools import lru_cache
from pathlib import Path

from .feedback import ValidationError
from .storage import ConflictError, now

BUNDLE = Path(__file__).with_name("demo")


@lru_cache(maxsize=1)
def catalog():
    return json.loads((BUNDLE / "catalog.json").read_text())


def _preset(preset_id):
    for preset in catalog()["presets"]:
        if preset["id"] == preset_id:
            return preset
    raise ValidationError("Choose one of the bundled research questions.")


def _candidates(store, db, samples, session_id, round_number, selection_parents):
    prompts = {p["id"]: p["text"] for p in catalog()["prompts"]}
    candidates = []
    for slot, sample in enumerate(samples, 1):
        asset_id = sample["sha256"][:32]
        filename = asset_id + ".jpg"
        path = store.assets / filename
        if not path.exists():
            path.write_bytes((BUNDLE / sample["file"]).read_bytes())
        db.execute("INSERT OR IGNORE INTO assets VALUES (?, ?, ?, ?, ?)",
                   (asset_id, filename, "image/jpeg", Path(sample["file"]).name, sample["sha256"]))
        candidates.append({
            "id": f"{session_id}:r{round_number}:c{slot:02}",
            "label": f"{sample['model']} · {sample['prompt_id']}" + (" · LoRA" if sample["identity_lora"] else ""),
            "archive_id": sample["id"],
            "media": {"asset_id": asset_id, "url": "/media/" + filename,
                      "mime": "image/jpeg", "name": Path(sample["file"]).name,
                      "sha256": sample["sha256"]},
            "genome": {"model": sample["model"], "prompt_id": sample["prompt_id"],
                       "prompt": prompts[sample["prompt_id"]], "seed": sample["seed"],
                       "identity_lora": sample["identity_lora"],
                       "other_generation_parameters": None},
            "parents": [],  # Original render ancestry is not supplied by the report.
            "selection_parents": selection_parents,
            "provenance": {**sample["source"], "medium": sample["medium"],
                           "frame_time_seconds": sample["frame_time_seconds"],
                           "description": "Original archived output, extracted directly from the research report.",
                           "selection_parents_meaning": "Selections that led to this demo batch; not the ancestry of the original render."},
        })
    return candidates


def _insert(store, db, preset, samples, session_id=None, round_number=1, parent=None):
    session_id = session_id or uuid.uuid4().hex[:12]
    gid = uuid.uuid4().hex
    parent_ids = parent["selected_ids"] if parent else []
    payload = {
        "title": preset["title"] if round_number == 1 else f"Following your choices · round {round_number}",
        "experiment": "Mutations of My Word · research demo",
        "prompt": preset["question"], "is_demo": True,
        "demo": {"version": 1, "mode": "archive-replay", "session_id": session_id,
                 "round": round_number, "preset": preset["id"],
                 "source_document": "main.pdf", "archive_size": len(catalog()["samples"]),
                 "parent_generation_id": parent["generation_id"] if parent else None,
                 "parent_review_id": parent["review_id"] if parent else None,
                 "selected_candidate_ids": parent_ids,
                 "description": "Selection-guided replay of archived stills. No new model inference."},
        "candidates": _candidates(store, db, samples, session_id, round_number, parent_ids),
    }
    db.execute("INSERT INTO generations(id, created_at, payload) VALUES (?, ?, ?)",
               (gid, now(), json.dumps(payload)))
    return gid


def bootstrap(store):
    """Install the first batch exactly once, including when upgrading an old workspace."""
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        marker = db.execute("SELECT value FROM app_state WHERE key='research-demo-v1'").fetchone()
        if marker:
            return marker["value"]
        preset = _preset("overview")
        by_id = {s["id"]: s for s in catalog()["samples"]}
        gid = _insert(store, db, preset, [by_id[sid] for sid in preset["samples"]])
        db.execute("INSERT INTO app_state VALUES ('research-demo-v1', ?)", (gid,))
    return gid


def start_session(store, preset_id="overview"):
    preset = _preset(preset_id)
    by_id = {s["id"]: s for s in catalog()["samples"]}
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        gid = _insert(store, db, preset, [by_id[sid] for sid in preset["samples"]])
    return store.get_generation(gid)


def next_generation(store, gid, expected_revision):
    if type(expected_revision) is not int or expected_revision < 1:
        raise ValidationError("Submit this generation's feedback before continuing.")
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM generations WHERE id=?", (gid,)).fetchone()
        if row is None:
            raise KeyError("Generation not found.")
        review_row = db.execute("SELECT * FROM reviews WHERE generation_id=? ORDER BY revision DESC LIMIT 1",
                                (gid,)).fetchone()
        if review_row is None:
            raise ValidationError("Submit your choices before continuing.")
        if review_row["revision"] != expected_revision:
            raise ConflictError("The submitted review changed. Reload it before continuing.")
        generation = store._generation(row, review_row)
        if generation.get("demo", {}).get("mode") != "archive-replay":
            raise ValidationError("Automatic continuation is available in the bundled research demo.")
        existing = db.execute("SELECT child_id FROM demo_continuations WHERE generation_id=? AND review_revision=?",
                              (gid, expected_revision)).fetchone()
        if existing:
            child_id = existing["child_id"]
        else:
            review, demo = generation["review"], generation["demo"]
            selected_ids = [r["candidate_id"] for r in review["candidates"] if r["decision"] == "keep"]
            if not selected_ids:
                raise ValidationError("No parents selected. This search is paused; start a fresh session whenever you wish.")
            selected = [c for c in generation["candidates"] if c["id"] in selected_ids]
            rejected_ids = {r["candidate_id"] for r in review["candidates"] if r["decision"] == "reject"}
            rejected_archive = {c["archive_id"] for c in generation["candidates"] if c["id"] in rejected_ids}
            seen = Counter()
            for past in db.execute("SELECT payload FROM generations"):
                payload = json.loads(past["payload"])
                if payload.get("demo", {}).get("session_id") == demo["session_id"]:
                    seen.update(c["archive_id"] for c in payload["candidates"])
            samples = {s["id"]: s for s in catalog()["samples"]}
            rng = random.Random(hashlib.sha256((gid + review["id"]).encode()).hexdigest())
            # Keep one preferred direction, then mix related and previously unseen archive samples.
            elite = next((c for c in selected if c["id"] == review["favourite_id"]), selected[0])
            picked = [samples[elite["archive_id"]]]
            pool = [s for s in samples.values() if s["id"] != elite["archive_id"]]
            while len(picked) < 9:
                unseen = [s for s in pool if not seen[s["id"]]]
                eligible = unseen if unseen and len(picked) < 3 else pool
                weights = []
                for sample in eligible:
                    affinity = sum(
                        2 * (sample["model"] == c["genome"]["model"])
                        + 2 * (sample["prompt_id"].removesuffix("_sp") == c["genome"]["prompt_id"].removesuffix("_sp"))
                        for c in selected) / len(selected)
                    weight = (1 + affinity + (3 if not seen[sample["id"]] else 0)) / (1 + .25 * seen[sample["id"]])
                    weights.append(weight * (.15 if sample["id"] in rejected_archive else 1))
                chosen = rng.choices(eligible, weights=weights, k=1)[0]
                picked.append(chosen)
                pool.remove(chosen)
            rng.shuffle(picked)
            child_id = _insert(store, db, _preset(demo["preset"]), picked, demo["session_id"], demo["round"] + 1,
                               {"generation_id": gid, "review_id": review["id"], "selected_ids": selected_ids})
            db.execute("INSERT INTO demo_continuations VALUES (?, ?, ?)", (gid, expected_revision, child_id))
    return store.get_generation(child_id)
