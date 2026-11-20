"""Runs what the pilot's Harbor runs of filelock and litellm, in the Harbor install under test, and prints the results.

Run it with the Python of the Harbor install under test, never with the test suite's own, from an empty folder:

    LITELLM_LOCAL_MODEL_COST_MAP=True <harbor venv>/bin/python -I harbor_uses_probe.py

Harbor's Docker environment takes filelock's AsyncFileLock around building an image (its egress control sidecar
among them, which every run of the pilot asks for); huggingface_hub, which Harbor installs, takes filelock's locks
through WeakFileLock; Harbor's Claude Code agent prices each step with litellm.cost_per_token when Claude Code's own
cost line is missing. The probe uses each the way Harbor does and prints as JSON what came of it, with the versions.
"""
import asyncio
import json
import tempfile
from importlib.metadata import version
from pathlib import Path

import filelock
from harbor.environments.docker import utils as docker_utils
from huggingface_hub.utils import WeakFileLock

out = {"filelock": version("filelock"), "litellm": version("litellm"),
       "harbor_locks_with_this_filelock": docker_utils.filelock is filelock}
folder = Path(tempfile.mkdtemp())


async def build_lock() -> dict:
    """Two builds of one image: the second waits while the first holds the lock and gets it after."""
    seen = {}
    first = filelock.AsyncFileLock(folder / ".harbor-docker-build.lock")
    second = filelock.AsyncFileLock(folder / ".harbor-docker-build.lock", timeout=0.2)
    async with first:
        try:
            async with second:
                seen["second_while_held"] = "acquired"
        except filelock.Timeout:
            seen["second_while_held"] = "timeout"
    async with second:
        seen["second_after_release"] = "acquired"
    return seen


out["build_lock"] = asyncio.run(build_lock())
with WeakFileLock(folder / "hub.lock", timeout=1):
    out["hub_lock"] = "acquired"

import litellm  # noqa: E402  (slow to import; only after the locks)

prompt, completion = litellm.cost_per_token(model="claude-sonnet-4-5", prompt_tokens=1000, completion_tokens=100,
                                            cache_creation_input_tokens=10, cache_read_input_tokens=20,
                                            service_tier=None)
out["cost_usd"] = [prompt, completion]
print(json.dumps(out))
