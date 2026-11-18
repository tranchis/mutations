"""Flat image genomes and explicit genetic operators."""
import copy, random, secrets
from .demo import catalog

MODEL_ID = "stable-diffusion-v1-5/stable-diffusion-v1-5"
ADAPTER_ID = "latent-consistency/lcm-lora-sdv1-5"
MODEL_REGISTRY = {
 "sd15-lcm":{"label":"Stable Diffusion 1.5 + LCM","model":MODEL_ID,"adapter":ADAPTER_ID,"steps":4,"guidance":1.0},
 "dreamshaper-8":{"label":"DreamShaper 8","model":"Lykon/dreamshaper-8","adapter":None,"steps":20,"guidance":7.0},
 "realistic-vision":{"label":"Realistic Vision 5.1","model":"stablediffusionapi/realistic-vision-v51","adapter":None,"steps":20,"guidance":7.0}}
GENES={"treatment":("original","without_painterly_instruction","visible brushwork, rough painted texture"),"lighting":("as prompted","soft diffuse light","dramatic side lighting","flat even light"),"composition":("as prompted","off-centre composition","substantial empty space around the subject")}
IMAGE_GENE_FIELDS=("model","adapter","adapter_scale","prompt","negative_prompt","reference_1","reference_2","identity_reference","control_reference","operation","seed","steps","guidance")
EVOLUTION_CONFIG={"crossover_rate":0.5,"mutation_rate":0.1,"favourite_parent_weight":3,"other_parent_weight":1}

def compile_prompt(genome):
 prompts={p["id"]:p["text"] for p in catalog()["prompts"]}; pid=genome["source_prompt_id"]; treatment=genome["genes"]["treatment"]
 prompt=prompts[pid if treatment=="original" else pid+"_sp"]
 if treatment not in ("original","without_painterly_instruction"): prompt += ", "+treatment
 for key in ("lighting","composition"):
  if genome["genes"][key] != "as prompted": prompt += ", "+genome["genes"][key]
 return prompt

def sync_genome(genome):
 gene=genome["gene"]; model=MODEL_REGISTRY[gene["model"]]
 genome.update(model=model["model"],model_id=gene["model"],prompt=gene["prompt"],seed=gene["seed"],steps=gene["steps"],guidance=gene["guidance"],width=512,height=512,adapters=([{"model":gene["adapter"],"scale":gene["adapter_scale"]}] if gene["adapter"] else []))
 return genome

def available_models(): return [{"id":k,**v} for k,v in MODEL_REGISTRY.items()]

def initial_population(preset="overview",model_ids=None):
 if preset not in ("overview","style","prompts"): raise ValueError("Unknown research question.")
 model_ids=model_ids or list(MODEL_REGISTRY)
 if any(x not in MODEL_REGISTRY for x in model_ids): raise ValueError("Unknown image model.")
 scenes=["MyWord_02A"]*9
 if preset=="prompts": scenes=["MyWord_02A","MyWord_02B","MyWord_02C","MyWord_05A","MyWord_05B","MyWord_07A","MyWord_07B","MyWord_02A","MyWord_02B"]
 out=[]; used=set()
 for slot,pid in enumerate(scenes):
  seed=secrets.randbits(32)
  while seed in used: seed=secrets.randbits(32)
  used.add(seed); model_id=model_ids[slot%len(model_ids)]; model=MODEL_REGISTRY[model_id]
  prompt_state={"source_prompt_id":pid,"genes":{"treatment":GENES["treatment"][slot//3],"lighting":"as prompted","composition":"as prompted"}}
  gene={"model":model_id,"adapter":model["adapter"],"adapter_scale":1.0,"prompt":compile_prompt(prompt_state),"negative_prompt":"","reference_1":None,"reference_2":None,"identity_reference":None,"control_reference":None,"operation":"generate","seed":seed,"steps":model["steps"],"guidance":model["guidance"]}
  genome={"gene":gene,"source_prompt_id":pid,"genes":prompt_state["genes"],"scheduler":"LCMScheduler"}; sync_genome(genome)
  out.append({"genome":genome,"parents":[],"operators":[{"type":"initial_sample"}]})
 return out

def _mutate(field,value,rng):
 if field=="seed":
  flips=[i for i in range(32) if rng.random()<EVOLUTION_CONFIG["mutation_rate"]]
  if not flips: flips=[rng.randrange(32)]
  for i in flips: value ^= 1<<i
  return value,{"flipped_bits":flips}
 if field=="model": return rng.choice([x for x in MODEL_REGISTRY if x!=value]),{}
 if field=="adapter": return MODEL_REGISTRY[value]["adapter"] if value in MODEL_REGISTRY else None,{}
 if field=="adapter_scale": return round(max(0,min(1,value+rng.uniform(-.2,.2))),3),{}
 if field == "steps": return max(1,value+rng.choice((-1,1))),{}
 if field=="guidance": return round(max(0,value+rng.uniform(-1,1)),3),{}
 return value,{}

def reproduce(generation,review):
 decisions={r["candidate_id"]:r["decision"] for r in review["candidates"]}
 kept={cid for cid,decision in decisions.items() if decision=="keep"}
 if not kept: raise ValueError("No parents selected. The search is paused.")
 # Keep is positive evidence, undecided is neutral evidence, reject is an
 # explicit exclusion. Neutral candidates may contribute at baseline weight;
 # rejected candidates can never become parents.
 parents=[c for c in generation["candidates"] if decisions.get(c["id"]) in ("keep","undecided")]
 rng=random.Random(secrets.randbits(64)); weights=[(EVOLUTION_CONFIG["favourite_parent_weight"] if p["id"]==review.get("favourite_id") else EVOLUTION_CONFIG["other_parent_weight"]) if decisions[p["id"]]=="keep" else 1 for p in parents]; children=[]
 for _ in range(9):
  a,b=rng.choices(parents,weights=weights,k=2); gene=copy.deepcopy(a["genome"]["gene"]); inherited={}; ops=[]
  for field in IMAGE_GENE_FIELDS:
   if rng.random()<EVOLUTION_CONFIG["crossover_rate"]: gene[field]=b["genome"]["gene"][field]; inherited[field]=b["id"]; ops.append({"type":"crossover","field":field,"parents":[a["id"],b["id"]]})
   else: inherited[field]=a["id"]
   if rng.random()<EVOLUTION_CONFIG["mutation_rate"]:
    before=gene[field]; gene[field],detail=_mutate(field,gene[field],rng); ops.append({"type":"mutation","field":field,"before":before,"after":gene[field],**detail})
  ops.insert(0,{"type":"inherit","fields":inherited}); model=MODEL_REGISTRY[gene["model"]]
  if gene["adapter"] is None and model["adapter"]: gene["adapter"]=model["adapter"]
  genome={"gene":gene,"source_prompt_id":a["genome"]["source_prompt_id"],"genes":copy.deepcopy(a["genome"].get("genes",{})),"scheduler":"LCMScheduler"}; sync_genome(genome)
  children.append({"genome":genome,"parents":list(dict.fromkeys([a["id"],*inherited.values()])),"operators":ops})
 return children
