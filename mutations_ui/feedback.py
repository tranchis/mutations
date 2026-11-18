"""Explicit observations for preference learning; no inferred negative labels."""

AXES = ("relationship", "departure", "specificity")
DECISIONS = ("keep", "reject", "undecided")


class ValidationError(ValueError):
    pass


def text(value, name, limit=4000):
    if not isinstance(value, str) or len(value) > limit:
        raise ValidationError(f"{name} must be text of at most {limit} characters.")
    return value.strip()


def validate_review(value, candidate_ids):
    if not isinstance(value, dict):
        raise ValidationError("Feedback must be an object.")
    rows = value.get("candidates")
    if not isinstance(rows, list) or len(rows) != 9:
        raise ValidationError("Feedback must contain all nine candidates.")
    seen, clean = set(), []
    for row in rows:
        if not isinstance(row, dict):
            raise ValidationError("Each candidate observation must be an object.")
        cid, decision = row.get("candidate_id"), row.get("decision")
        if not isinstance(cid, str) or cid not in candidate_ids or cid in seen:
            raise ValidationError("Candidate IDs must match this generation exactly.")
        if decision not in DECISIONS:
            raise ValidationError("Choose keep, reject or undecided.")
        seen.add(cid)
        scores = row.get("scores", {})
        if not isinstance(scores, dict) or set(scores) - set(AXES):
            raise ValidationError("Unknown rubric dimension.")
        for score in scores.values():
            if score is not None and (type(score) is not int or not 0 <= score <= 4):
                raise ValidationError("Rubric scores must be 0–4, or null when unrated.")
        clean.append({"candidate_id": cid, "decision": decision,
                      "scores": {axis: scores.get(axis) for axis in AXES},
                      "note": text(row.get("note", ""), "Candidate note")})
    if not any(row["decision"] != "undecided" for row in clean):
        raise ValidationError("Make at least one keep or reject decision before submitting.")
    favourite = value.get("favourite_id")
    if favourite is not None and not any(
        row["candidate_id"] == favourite and row["decision"] == "keep" for row in clean
    ):
        raise ValidationError("The favourite must be a candidate marked keep.")
    order = value.get("display_order", list(candidate_ids))
    if (not isinstance(order, list) or len(order) != 9
            or any(not isinstance(cid, str) for cid in order)
            or set(order) != set(candidate_ids)):
        raise ValidationError("Display order must contain each candidate once.")
    return {"candidates": clean, "favourite_id": favourite, "display_order": order,
            "reviewer": text(value.get("reviewer", ""), "Reviewer", 100),
            "note": text(value.get("note", ""), "Generation note")}


def preference_pairs(review):
    """Keep beats explicit reject. Favourite beats other keep. Undecided is unlabeled."""
    kept = [r["candidate_id"] for r in review["candidates"] if r["decision"] == "keep"]
    rejected = [r["candidate_id"] for r in review["candidates"] if r["decision"] == "reject"]
    pairs = [{"preferred": winner, "other": loser, "basis": "keep_over_reject"}
             for winner in kept for loser in rejected]
    favourite = review.get("favourite_id")
    if favourite:
        pairs.extend({"preferred": favourite, "other": other, "basis": "favourite_over_keep"}
                     for other in kept if other != favourite)
    return pairs

