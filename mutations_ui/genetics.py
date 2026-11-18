"""Heritable generation recipes. Every child is rendered from a fresh latent seed."""

import copy
import random
import secrets

from .demo import catalog

MODEL_ID = "stable-diffusion-v1-5/stable-diffusion-v1-5"
ADAPTER_ID = "latent-consistency/lcm-lora-sdv1-5"
GENES = {
    "treatment": ("original", "without_painterly_instruction", "visible brushwork, rough painted texture"),
    "lighting": ("as prompted", "soft diffuse light", "dramatic side lighting", "flat even light"),
    "composition": ("as prompted", "off-centre composition", "substantial empty space around the subject"),
}


def compile_prompt(genome):
    prompts = {p["id"]: p["text"] for p in catalog()["prompts"]}
    pid = genome["source_prompt_id"]
    treatment = genome["genes"]["treatment"]
    prompt = prompts[pid if treatment == "original" else pid + "_sp"]
    if treatment not in ("original", "without_painterly_instruction"):
        prompt += ", " + treatment
    for gene in ("lighting", "composition"):
        if genome["genes"][gene] != "as prompted":
            prompt += ", " + genome["genes"][gene]
    return prompt


def initial_population(preset="overview"):
    if preset not in ("overview", "style", "prompts"):
        raise ValueError("Unknown research question.")
    scenes = ["MyWord_02A"] * 9
    if preset == "prompts":
        scenes = ["MyWord_02A", "MyWord_02B", "MyWord_02C", "MyWord_05A", "MyWord_05B", "MyWord_07A", "MyWord_07B", "MyWord_02A", "MyWord_02B"]
    population = []
    used_seeds = set()
    for slot, pid in enumerate(scenes):
        seed = secrets.randbits(32)
        while seed in used_seeds:
            seed = secrets.randbits(32)
        used_seeds.add(seed)
        genome = {"model": MODEL_ID, "adapters": [{"model": ADAPTER_ID, "scale": 1.0}],
                  "source_prompt_id": pid, "genes": {"treatment": GENES["treatment"][slot // 3],
                    "lighting": "as prompted", "composition": "as prompted"},
                  "seed": seed, "steps": 4, "guidance": 1.0, "width": 512, "height": 512,
                  "scheduler": "LCMScheduler"}
        genome["prompt"] = compile_prompt(genome)
        population.append({"genome": genome, "parents": [], "operators": [{"type": "initial_sample"}]})
    return population


def reproduce(generation, review):
    selected_ids = {r["candidate_id"] for r in review["candidates"] if r["decision"] == "keep"}
    parents = [c for c in generation["candidates"] if c["id"] in selected_ids]
    if not parents:
        raise ValueError("No parents selected. The search is paused.")
    rng = random.Random(secrets.randbits(64))
    weights = [3 if p["id"] == review.get("favourite_id") else 1 for p in parents]
    used_seeds = {c["genome"]["seed"] for c in generation["candidates"]}
    children = []
    for slot in range(9):
        a, b = rng.choices(parents, weights=weights, k=2)
        genome = copy.deepcopy(a["genome"])
        inherited = {key: a["id"] for key in GENES}
        for key in GENES:
            if slot and rng.random() < .5:
                genome["genes"][key] = b["genome"]["genes"][key]
                inherited[key] = b["id"]
        operators = [{"type": "inherit", "fields": inherited}]
        if slot:
            key = rng.choice(tuple(GENES))
            before = genome["genes"][key]
            genome["genes"][key] = rng.choice([v for v in GENES[key] if v != before])
            operators.append({"type": "mutation", "field": key, "before": before, "after": genome["genes"][key]})
        seed = rng.getrandbits(32)
        while seed in used_seeds:
            seed = rng.getrandbits(32)
        used_seeds.add(seed)
        operators.append({"type": "mutation", "field": "seed", "before": genome["seed"], "after": seed})
        genome["seed"] = seed
        genome["prompt"] = compile_prompt(genome)
        parent_ids = list(dict.fromkeys([a["id"], *inherited.values()]))
        children.append({"genome": genome, "parents": parent_ids, "operators": operators})
    return children

