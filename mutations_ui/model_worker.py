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
    from .genetics import MODEL_ID, ADAPTER_ID
    device = "cuda" if torch.cuda.is_available() else "mps" if torch.backends.mps.is_available() else "cpu"
    # Float16 inference on the current MPS stack can produce NaNs for this
    # checkpoint (which Diffusers serializes as black pixels). Float32 remains
    # accelerated on Apple Silicon and is the reliable demo default.
    dtype = torch.float16 if device == "cuda" else torch.float32
    cache = manifest["cache_dir"]
    emit("stage", message="Downloading and loading the model; first launch caches about 3 GB", device=device)
    index = hf_hub_download(MODEL_ID, "model_index.json", cache_dir=cache, token=False)
    revision = Path(index).parent.name
    adapter = hf_hub_download(ADAPTER_ID, "pytorch_lora_weights.safetensors", cache_dir=cache, token=False)
    adapter_revision = Path(adapter).parent.name
    pipe = StableDiffusionPipeline.from_pretrained(MODEL_ID, revision=revision, variant="fp16", torch_dtype=dtype,
                                                   use_safetensors=True, cache_dir=cache, token=False)
    pipe.scheduler = LCMScheduler.from_config(pipe.scheduler.config)
    pipe.load_lora_weights(ADAPTER_ID, revision=adapter_revision, cache_dir=cache, token=False)
    pipe.fuse_lora()
    # SD 1.5's legacy checker produces black placeholders for benign painterly
    # prompts on this local demo (especially on MPS). Keep the generated pixels
    # and record the setting rather than silently presenting nine identical
    # black images as if they were successful variations.
    pipe.safety_checker = None
    pipe.to(device)
    if device != "cuda":
        pipe.enable_attention_slicing()
    pipe.set_progress_bar_config(disable=True)
    for item in manifest["candidates"]:
        genome, slot = item["genome"], item["slot"]
        emit("stage", message=f"Generating image {slot + 1} of 9", device=device)
        start = time.monotonic()

        def step_callback(pipeline, step, timestep, callback_kwargs):
            emit("step", slot=slot, step=step + 1, steps=genome["steps"])
            return callback_kwargs

        output = pipe(genome["prompt"], width=genome["width"], height=genome["height"],
                      num_inference_steps=genome["steps"], guidance_scale=genome["guidance"],
                      generator=torch.Generator("cpu").manual_seed(genome["seed"]),
                      callback_on_step_end=step_callback)
        target = Path(item["output"])
        target.parent.mkdir(parents=True, exist_ok=True)
        output.images[0].save(target)
        emit("image", slot=slot, path=str(target), execution={"device": device, "dtype": str(dtype),
             "model_revision": revision, "adapter_revision": adapter_revision,
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
