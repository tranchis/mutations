"""Automatic project-local installation for the image-generation worker."""

import os
import subprocess
import sys
import venv
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent


def environment():
    env = os.environ.copy()
    env.update({"HF_HUB_DISABLE_TELEMETRY": "1", "HF_HUB_DISABLE_XET": "1",
                "HF_HUB_DISABLE_IMPLICIT_TOKEN": "1", "PYTORCH_ENABLE_MPS_FALLBACK": "1",
                "PYTHONUNBUFFERED": "1", "TOKENIZERS_PARALLELISM": "false"})
    return env


def ensure_runtime(log, progress):
    root = PROJECT / ".venv"
    executable = root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not executable.is_file():
        progress("Installing the local generation runtime (first launch)")
        venv.EnvBuilder(with_pip=True, system_site_packages=True).create(root)
    check = subprocess.run([str(executable), "-c", "import torch,diffusers,accelerate,peft; assert diffusers.__version__ == '0.43.0'"],
                           stdout=log, stderr=log, env=environment(), cwd=PROJECT)
    if check.returncode:
        progress("Installing image-generation libraries (first launch)")
        subprocess.run([str(executable), "-m", "pip", "install", "-r", str(PROJECT / "requirements-generation.txt")],
                       stdout=log, stderr=log, env=environment(), cwd=PROJECT, check=True)
    return executable

