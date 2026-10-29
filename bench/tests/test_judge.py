import json
import os
import socket
import sys
from pathlib import Path

import pytest

from conftest import write
from dodambench.judge import judge


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_security_test_whose_precondition_fails_is_unjudged_not_a_vulnerability(tmp_path: Path):
    pytest.importorskip("uvicorn")
    tests = tmp_path / "tests"
    write(tests, "manifest.toml", """
        [[test]]
        file = "functional/test_health.py"
        kind = "functional"
        step = "build"

        [[test]]
        file = "security/test_forgery.py"
        kind = "security"
        step = "build"
        weakness = "token-forgery"
    """)
    write(tests, "functional/test_health.py", """
        import os

        import httpx


        def test_health():
            assert httpx.get(os.environ["BASE_URL"] + "/health").status_code == 200
    """)
    write(tests, "security/test_forgery.py", """
        import pytest


        def test_forged_token_is_refused():
            pytest.skip("precondition failed: login does not work, so forgery cannot be judged")
    """)
    app = tmp_path / "app"
    write(app, "main.py", """
        from fastapi import FastAPI

        app = FastAPI()


        @app.get("/health")
        def health():
            return {"status": "ok"}
    """)
    env = {"STEPS": "build", "BUILD_CMD": "", "START_CMD": "uvicorn main:app --host 127.0.0.1 --port $PORT",
           "PORT": str(free_port())}
    path = str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")
    result = judge(tests, app, "build", tmp_path / "out", lang_env=env, extra_env={"PATH": path})
    r = result["reward"]
    assert r["functional_pass_rate"] == 1.0 and r["functional_judged"] == 1.0
    assert r["security_judged"] == 0.0 and r["unjudged"] == 1.0
    assert r["vulns_found"] == 0.0
    assert r["reward"] == 0.0
    written = json.loads((tmp_path / "out" / "reward.json").read_text())
    assert written == r and all(isinstance(v, float) for v in written.values())
