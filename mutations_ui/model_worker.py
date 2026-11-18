"""Direct Stable Diffusion inference. Stdout is a JSON event stream for the WebUI."""

import json
import sys
import time
from pathlib import Path


def emit(event, **values):
    print(json.dumps({"event": event, **values}), flush=True)


def main():
    manifest = json.loads(Path(sys.argv[1]).read_text())
    import torch
    import diffusers
    from diffusers import StableDiffusionPipeline, LCMScheduler
    from huggingface_hub import hf_hub_download
    from .genetics import MODEL_REGISTRY
    device = "cuda" if torch.cuda.is_available() else "mps" if torch.backends.mps.is_available() else "cpu"
    # Float16 inference on the current MPS stack can produce NaNs for this
    # checkpoint (which Diffusers serializes as black pixels). Float32 remains
    # accelerated on Apple Silicon and is the reliable demo default.
    dtype = torch.float16 if device == "cuda" else torch.float32
    cache = manifest["cache_dir"]
    loaded = {}

    def load_model(model_id):
        if model_id in loaded:
            return loaded[model_id]
        config = MODEL_REGISTRY.get(model_id)
        if config is None:
            raise ValueError(f"Unknown model: {model_id}")
        model_repo, adapter_repo = config["model"], config.get("adapter")
        emit("stage", message=f"Downloading and loading {config['label']} (first use caches its checkpoint)", device=device)
        index = hf_hub_download(model_repo, "model_index.json", cache_dir=cache, token=False)
        revision = Path(index).parent.name
        adapter_revision = None
        if adapter_repo:
            adapter = hf_hub_download(adapter_repo, "pytorch_lora_weights.safetensors", cache_dir=cache, token=False)
            adapter_revision = Path(adapter).parent.name
        try:
            pipe = StableDiffusionPipeline.from_pretrained(model_repo, revision=revision, variant="fp16", torch_dtype=dtype,
                                                           use_safetensors=True, cache_dir=cache, token=False)
        except (OSError, ValueError):
            pipe = StableDiffusionPipeline.from_pretrained(model_repo, revision=revision, torch_dtype=dtype,
                                                           use_safetensors=True, cache_dir=cache, token=False)
        if adapter_repo:
            pipe.scheduler = LCMScheduler.from_config(pipe.scheduler.config)
            pipe.load_lora_weights(adapter_repo, revision=adapter_revision, cache_dir=cache, token=False)
            pipe.fuse_lora()
        pipe.safety_checker = None
        pipe.to(device)
        if device != "cuda":
            pipe.enable_attention_slicing()
        pipe.set_progress_bar_config(disable=True)
        loaded[model_id] = (pipe, revision, adapter_revision)
        return loaded[model_id]

    for item in manifest["candidates"]:
        genome, slot = item["genome"], item["slot"]
        model_id = genome.get("model_id", "sd15-lcm")
        pipe, revision, adapter_revision = load_model(model_id)
        # Image dimensions are integer genes, but diffusion pipelines require
        # multiples of eight. Normalize legacy/hand-edited genomes before the
        # call so retrying an older job cannot fail on 513×512-style values.
        width = max(64, round(genome["width"] / 8) * 8)
        height = max(64, round(genome["height"] / 8) * 8)
        emit("stage", message=f"Generating image {slot + 1} of 9", device=device)
        start = time.monotonic()

        def step_callback(pipeline, step, timestep, callback_kwargs):
            emit("step", slot=slot, step=step + 1, steps=genome["steps"])
            return callback_kwargs

        output = pipe(genome["prompt"], width=width, height=height,
                      num_inference_steps=genome["steps"], guidance_scale=genome["guidance"],
                      generator=torch.Generator("cpu").manual_seed(genome["seed"]),
                      callback_on_step_end=step_callback)
        target = Path(item["output"])
        target.parent.mkdir(parents=True, exist_ok=True)
        output.images[0].save(target)
        emit("image", slot=slot, path=str(target), execution={"device": device, "dtype": str(dtype),
             "model_id": model_id, "model_revision": revision, "adapter_revision": adapter_revision,
             "torch_version": torch.__version__, "diffusers_version": diffusers.__version__,
             "elapsed_seconds": round(time.monotonic() - start, 3),
             "safety_checker": "disabled_for_local_demo"})
    emit("complete")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        emit("error", message=f"{type(exc).__name__}: {exc}")
        raise
