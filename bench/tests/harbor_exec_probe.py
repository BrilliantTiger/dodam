"""Builds the docker command Harbor runs for one command in the task container, without starting Docker.

Run it with the Python of the Harbor install under test, never with the test suite's own:

    <harbor venv>/bin/python -I harbor_exec_probe.py

It reads a made-up secret from DODAM_PROBE_SECRET, asks a real Harbor DockerEnvironment to run a command with that
secret (and two other variables) in its environment, stops where Harbor would start the docker CLI, and prints as
JSON the argument list and the environment that process would have been given.
"""
import asyncio
import json
import os
import tempfile
from importlib.metadata import version
from pathlib import Path

from harbor.environments.docker import docker as docker_module
from harbor.environments.docker.docker import DockerEnvironment
from harbor.models.task.config import EnvironmentConfig
from harbor.models.trial.paths import TrialPaths


class Captured(Exception):
    pass


def main() -> None:
    secret = os.environ.pop("DODAM_PROBE_SECRET")
    calls = []

    async def fake_exec(*argv, **kwargs):
        calls.append({"argv": [str(a) for a in argv], "env": dict(kwargs.get("env") or {})})
        raise Captured

    docker_module.asyncio.create_subprocess_exec = fake_exec
    requested = {"DODAM_PROBE_SECRET_VALUE": secret, "DODAM_PROBE_EMPTY": "", "PATH": "/container/bin"}
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "environment").mkdir()
        (root / "environment" / "Dockerfile").write_text("FROM scratch\n")
        environment = DockerEnvironment(environment_dir=root / "environment", environment_name="probe",
                                        session_id="probe-1", trial_paths=TrialPaths(root / "trial"),
                                        task_env_config=EnvironmentConfig())
        try:
            asyncio.run(environment.exec("true", env=requested))
        except Captured:
            pass
    call = calls[0]
    print(json.dumps({"harbor_version": version("harbor"), "argv": call["argv"],
                      "env": {name: call["env"].get(name) for name in requested}}))


if __name__ == "__main__":
    main()
