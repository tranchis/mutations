"""SQLite generations and append-only review revisions, with local media storage."""

import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .feedback import ValidationError, preference_pairs, text, validate_review

MEDIA = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
         ".webp": "image/webp", ".gif": "image/gif", ".avif": "image/avif",
         ".mp4": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime"}


def now():
    return datetime.now(timezone.utc).isoformat()


class ConflictError(ValidationError):
    pass


class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.assets = self.root / "media"
        self.assets.mkdir(parents=True, exist_ok=True)
        self.database = self.root / "feedback.sqlite3"
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS assets (
                    id TEXT PRIMARY KEY, filename TEXT NOT NULL,
                    mime TEXT NOT NULL, original_name TEXT NOT NULL, sha256 TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS generations (
                    ordinal INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
                    created_at TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reviews (
                    id TEXT PRIMARY KEY, generation_id TEXT NOT NULL,
                    revision INTEGER NOT NULL, created_at TEXT NOT NULL, payload TEXT NOT NULL,
                    UNIQUE(generation_id, revision),
                    FOREIGN KEY(generation_id) REFERENCES generations(id)
                );
                CREATE TABLE IF NOT EXISTS app_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS demo_continuations (
                    generation_id TEXT NOT NULL, review_revision INTEGER NOT NULL,
                    child_id TEXT NOT NULL UNIQUE,
                    PRIMARY KEY(generation_id, review_revision),
                    FOREIGN KEY(generation_id) REFERENCES generations(id),
                    FOREIGN KEY(child_id) REFERENCES generations(id)
                );
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.database, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def add_asset(self, name, data):
        suffix = Path(name).suffix.lower()
        if suffix not in MEDIA:
            raise ValidationError("Use PNG, JPEG, WebP, GIF, AVIF, MP4, WebM or MOV files.")
        if not data:
            raise ValidationError("The media file is empty.")
        aid = uuid.uuid4().hex
        filename = aid + suffix
        (self.assets / filename).write_bytes(data)
        with self.connect() as db:
            db.execute("INSERT INTO assets VALUES (?, ?, ?, ?, ?)",
                       (aid, filename, MEDIA[suffix], Path(name).name,
                        hashlib.sha256(data).hexdigest()))
        return self.asset(aid)

    def asset(self, aid):
        with self.connect() as db:
            row = db.execute("SELECT * FROM assets WHERE id=?", (aid,)).fetchone()
        if row is None:
            raise ValidationError("Media asset was not found. Upload it first.")
        return {"asset_id": row["id"], "url": "/media/" + row["filename"],
                "mime": row["mime"], "name": row["original_name"], "sha256": row["sha256"]}

    def create_generation(self, value):
        if not isinstance(value, dict):
            raise ValidationError("A generation must be an object.")
        candidates = value.get("candidates")
        if not isinstance(candidates, list) or len(candidates) != 9:
            raise ValidationError("Each generation must contain exactly nine candidates.")
        cleaned, seen = [], set()
        for index, item in enumerate(candidates):
            if not isinstance(item, dict):
                raise ValidationError("Each candidate must be an object.")
            cid = text(item.get("id", f"candidate-{index + 1:02}"), "Candidate ID", 120)
            if not cid or cid in seen:
                raise ValidationError("Candidate IDs must be non-empty and unique within a generation.")
            seen.add(cid)
            aid = item.get("asset_id")
            if not isinstance(aid, str):
                raise ValidationError("Each candidate needs an uploaded asset_id.")
            genome = item.get("genome", {})
            parents = item.get("parents", [])
            if not isinstance(genome, dict):
                raise ValidationError("A genome must be a JSON object.")
            if not isinstance(parents, list) or any(not isinstance(p, str) for p in parents):
                raise ValidationError("Parents must be a list of IDs.")
            cleaned.append({"id": cid, "media": self.asset(aid), "genome": genome,
                            "parents": parents, "label": text(item.get("label", cid), "Label", 200)})
        title = text(value.get("title", "Untitled generation"), "Title", 200)
        if not title:
            raise ValidationError("Give this generation a title.")
        payload = {"title": title,
                   "prompt": text(value.get("prompt", ""), "Source prompt", 12000),
                   "experiment": text(value.get("experiment", "My Word"), "Experiment", 200),
                   "is_demo": False, "candidates": cleaned}
        return self._insert(payload)

    def _insert(self, payload):
        gid = uuid.uuid4().hex
        with self.connect() as db:
            db.execute("INSERT INTO generations(id, created_at, payload) VALUES (?, ?, ?)",
                       (gid, now(), json.dumps(payload)))
        return self.get_generation(gid)

    def create_demo(self, preset="overview"):
        raise ValidationError("Legacy archive demos are not part of the live generation workflow. Use GenerationService.start_session().")

    @staticmethod
    def _generation(row, review_row):
        return {**json.loads(row["payload"]), "id": row["id"], "ordinal": row["ordinal"],
                "created_at": row["created_at"], "review": Store._review(review_row)}

    @staticmethod
    def _review(row):
        return ({**json.loads(row["payload"]), "id": row["id"], "revision": row["revision"],
                 "created_at": row["created_at"]} if row else None)

    def get_generation(self, gid):
        with self.connect() as db:
            row = db.execute("SELECT * FROM generations WHERE id=?", (gid,)).fetchone()
            if row is None:
                raise KeyError("Generation not found.")
            review = db.execute("SELECT * FROM reviews WHERE generation_id=? ORDER BY revision DESC LIMIT 1",
                                (gid,)).fetchone()
        return self._generation(row, review)

    def generations(self):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM generations ORDER BY ordinal DESC").fetchall()
            reviews = {r["generation_id"]: r for r in db.execute(
                "SELECT r.* FROM reviews r JOIN (SELECT generation_id, MAX(revision) AS revision "
                "FROM reviews GROUP BY generation_id) latest USING (generation_id, revision)")}
        return [self._generation(row, reviews.get(row["id"])) for row in rows]

    def save_review(self, gid, value):
        generation = self.get_generation(gid)
        if generation.get("generation", {}).get("status", "ready") != "ready":
            raise ValidationError("Wait until all nine images have been generated before submitting feedback.")
        expected = value.get("expected_revision") if isinstance(value, dict) else None
        if type(expected) is not int or expected < 0:
            raise ValidationError("expected_revision must be a non-negative integer.")
        review = validate_review(value, [c["id"] for c in generation["candidates"]])
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT COALESCE(MAX(revision), 0) FROM reviews WHERE generation_id=?",
                                 (gid,)).fetchone()[0]
            if current != expected:
                raise ConflictError("This generation changed in another tab. Reload it before submitting again; your draft is preserved.")
            db.execute("INSERT INTO reviews VALUES (?, ?, ?, ?, ?)",
                       (uuid.uuid4().hex, gid, current + 1, now(), json.dumps(review)))
            # Keep the latest evaluation beside each phenotype for consumers
            # that inspect individuals without joining the review table.
            payload = json.loads(db.execute("SELECT payload FROM generations WHERE id=?", (gid,)).fetchone()[0])
            by_id = {row["candidate_id"]: row for row in review["candidates"]}
            for candidate in payload["candidates"]:
                candidate["evaluation"] = by_id.get(candidate["id"])
            payload["evaluation_revision"] = current + 1
            db.execute("UPDATE generations SET payload=? WHERE id=?", (json.dumps(payload), gid))
        return self.get_generation(gid)["review"]

    def export(self, include_demo=False):
        generations = [g for g in reversed(self.generations()) if include_demo or not g["is_demo"]]
        comparisons = []
        for generation in generations:
            review = generation["review"]
            with self.connect() as db:
                generation["review_history"] = [self._review(row) for row in db.execute(
                    "SELECT * FROM reviews WHERE generation_id=? ORDER BY revision", (generation["id"],))]
            if review:
                comparisons.extend({**pair, "generation_id": generation["id"],
                                    "review_id": review["id"], "review_revision": review["revision"],
                                    "reviewer": review["reviewer"], "is_demo": generation["is_demo"]}
                                   for pair in preference_pairs(review))
        return {"schema_version": 1, "exported_at": now(),
                "rubric": {"version": 1, "scale": [0, 4],
                           "axes": ["relationship", "departure", "specificity"]},
                "semantics": {"undecided": "unlabeled", "unrated": None,
                              "comparisons": "Latest submitted review per generation only; pairs share review context and are not independent votes.",
                              "fitness": "Human observations for fitting an evaluator; no learned fitness score yet."},
                "generations": generations, "comparisons": comparisons}

