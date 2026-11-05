"""Judge one step of a Dodam benchmark task.

Runs inside the verifier container (copied there as /tests/lib/dodam_judge.py) and also on a
developer machine to vet a task. It only uses the Python standard library and pytest.

Steps: build the program, start it with a fresh data folder, run the hidden tests of this step
(earlier steps' tests too, unless superseded), restart the program before every test file that
needs a fresh start, and write reward.json plus details.json.

A test that skips is unjudged: a security test skips when the feature it attacks does not work,
because it then can say nothing about security. Unjudged tests are neither passes nor
vulnerabilities, and the full reward needs every test to pass.

In the verifier the judge runs as root and builds and starts the program as a plain user
(--run-as, or DODAM_APP_USER). That code is the agent's, so before the build the judge hands /app
to that user and relinks the dependency folders to the image's pinned copies; afterwards it kills
every process of that user and replaces, never follows, whatever sits where the results go.
"""
from __future__ import annotations

import argparse
import json
import os
import pwd
import secrets
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path


def read_env_file(path: Path) -> dict[str, str]:
    env = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                env[key.strip()] = value
    return env


def select_tests(manifest: dict, steps: list[str], step: str) -> list[dict]:
    upto = steps.index(step)
    chosen = []
    for t in manifest.get("test", []):
        if t.get("step") in steps and steps.index(t["step"]) <= upto and step not in t.get("superseded_in", []):
            t = dict(t)
            t.setdefault("fresh_app", t.get("kind") == "security")
            chosen.append(t)
    return chosen


def wait_for_port(port: int, timeout: float, proc: subprocess.Popen) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return False
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def restore_deps(app_dir: Path, deps_dir: Path, links: list[str]) -> None:
    """Point each dependency folder of the app at the pinned copy, dropping what the app brought."""
    for name in links:
        target = app_dir / name
        if target.is_symlink() or target.is_file():
            target.unlink()
        elif target.exists():
            shutil.rmtree(target)
        target.symlink_to(deps_dir / name, target_is_directory=True)


def give_app_to(app_dir: Path, user: str) -> None:
    """Make the plain user own the app folder so its build can write there; links are not followed."""
    entry = pwd.getpwnam(user)
    os.lchown(app_dir, entry.pw_uid, entry.pw_gid)
    for root, dirs, files in os.walk(app_dir):
        for name in dirs + files:
            os.lchown(os.path.join(root, name), entry.pw_uid, entry.pw_gid)


def close_to_others(*paths: Path) -> None:
    """Take away group and other write access, so the plain user cannot change tests or results."""
    for top in paths:
        if not top.exists():
            continue
        for root, dirs, files in os.walk(top):
            for name in [""] + dirs + files:
                path = os.path.join(root, name) if name else root
                if not os.path.islink(path):
                    os.chmod(path, os.stat(path).st_mode & ~0o022)


def user_pids(uid: int, proc_root: Path = Path("/proc")) -> list[int]:
    """Processes with uid as their real, effective, saved or file-system user."""
    found = []
    for entry in proc_root.iterdir() if proc_root.is_dir() else []:
        if not entry.name.isdigit():
            continue
        try:
            status = (entry / "status").read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in status.splitlines():
            if line.startswith("Uid:"):
                if str(uid) in line.split()[1:]:
                    found.append(int(entry.name))
                break
    return found


def kill_user(user: str) -> None:
    """Kill everything the plain user still runs, including daemons that left the process group."""
    uid = pwd.getpwnam(user).pw_uid
    for _ in range(3):
        pids = user_pids(uid)
        if not pids:
            return
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        time.sleep(0.2)


def write_output(path: Path, text: str) -> None:
    """Write through a new file and rename it into place, replacing a planted link instead of following it."""
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


class App:
    def __init__(self, app_dir: Path, start_cmd: str, port: int, run_as: str, extra_env: dict[str, str]):
        self.app_dir, self.start_cmd, self.port, self.run_as = app_dir, start_cmd, port, run_as
        self.extra_env = extra_env
        self.proc: subprocess.Popen | None = None
        self.data_dir: Path | None = None
        self.env: dict[str, str] = {}

    def start(self, timeout: float = 60) -> bool:
        self.stop()
        self.data_dir = Path(tempfile.mkdtemp(prefix="dodam-data-"))
        if self.run_as:
            subprocess.run(["chown", "-R", self.run_as, str(self.data_dir)], check=False)
        self.env = {**os.environ, **self.extra_env, "PORT": str(self.port),
                    "DB_PATH": str(self.data_dir / "app.db"), "APP_SECRET": secrets.token_hex(32)}
        cmd = self.start_cmd
        if self.run_as:
            cmd = f"su -s /bin/sh {shlex.quote(self.run_as)} -c {shlex.quote(cmd)}"
        self.proc = subprocess.Popen(cmd, shell=True, cwd=self.app_dir, env=self.env,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        return wait_for_port(self.port, timeout, self.proc)

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
                self.proc.wait(timeout=10)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        self.proc = None


def run_pytest(test_path: Path, env: dict[str, str], junit: Path, timeout: float) -> list[dict]:
    cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--junitxml={junit}", str(test_path)]
    try:
        subprocess.run(cmd, env=env, cwd=test_path.parent, timeout=timeout,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return [{"name": test_path.name, "outcome": "failed", "message": "timed out"}]
    if not junit.is_file():
        return [{"name": test_path.name, "outcome": "failed", "message": "no test report"}]
    cases = []
    for case in ET.parse(junit).iter("testcase"):
        outcome = "passed"
        message = ""
        for tag in ("failure", "error"):
            node = case.find(tag)
            if node is not None:
                outcome, message = "failed", (node.get("message") or "")[:300]
        if case.find("skipped") is not None:
            outcome = "skipped"
        cases.append({"name": case.get("name", ""), "outcome": outcome, "message": message})
    return cases or [{"name": test_path.name, "outcome": "failed", "message": "no tests collected"}]


def judge(tests_dir: Path, app_dir: Path, step: str, out_dir: Path, run_as: str = "",
          lang_env: dict[str, str] | None = None, extra_env: dict[str, str] | None = None) -> dict:
    # pytest runs from each test's folder, so every folder handed on must be absolute
    tests_dir, app_dir, out_dir = tests_dir.resolve(), app_dir.resolve(), out_dir.resolve()
    lang_env = lang_env if lang_env is not None else read_env_file(tests_dir / "lang.env")
    manifest = tomllib.loads((tests_dir / "manifest.toml").read_text(encoding="utf-8"))
    steps = [s for s in lang_env.get("STEPS", "").split(",") if s] or [step]
    selected = select_tests(manifest, steps, step)
    port = int(lang_env.get("PORT", "8080"))
    out_dir.mkdir(parents=True, exist_ok=True)
    report: dict = {"step": step, "build_ok": False, "tests": []}
    if run_as:
        close_to_others(tests_dir, out_dir)
        give_app_to(app_dir, run_as)
    deps_dir, links = lang_env.get("DEPS_DIR", ""), [n for n in lang_env.get("DEPS_LINKS", "").split(",") if n]
    if deps_dir and links:
        restore_deps(app_dir, Path(deps_dir), links)

    build_cmd = lang_env.get("BUILD_CMD", "").strip()
    started = time.monotonic()
    if build_cmd:
        if run_as:
            build_cmd = f"su -s /bin/sh {shlex.quote(run_as)} -c {shlex.quote(build_cmd)}"
        try:
            res = subprocess.run(build_cmd, shell=True, cwd=app_dir, timeout=900, capture_output=True, text=True,
                                 env={**os.environ, **(extra_env or {})})
            report["build_ok"] = res.returncode == 0
            report["build_log_tail"] = (res.stdout + res.stderr)[-2000:]
        except subprocess.TimeoutExpired:
            report["build_log_tail"] = "build timed out"
    else:
        report["build_ok"] = True
    report["build_sec"] = round(time.monotonic() - started, 2)

    app = App(app_dir, lang_env.get("START_CMD", ""), port, run_as, extra_env or {})
    shared_up = False
    for t in selected:
        path = tests_dir / t["file"]
        # a program that does not build or start fails its features but shows no weakness
        not_run = "skipped" if t.get("kind") == "security" else "failed"
        cases: list[dict]
        if not report["build_ok"]:
            cases = [{"name": path.name, "outcome": not_run, "message": "build failed"}]
        else:
            fresh = t.get("fresh_app")
            if fresh or not shared_up or not app.alive():
                up = app.start()
                shared_up = up and not fresh
            else:
                up = True
            if not up:
                cases = [{"name": path.name, "outcome": not_run, "message": "program did not start"}]
            else:
                env = {**app.env, "BASE_URL": f"http://127.0.0.1:{port}", "DODAM_APP_DIR": str(app_dir)}
                junit = out_dir / ("junit-" + t["file"].replace("/", "-") + ".xml")
                cases = run_pytest(path, env, junit, timeout=300)
                if fresh:
                    app.stop()
        for c in cases:
            report["tests"].append({**c, "file": t["file"], "kind": t.get("kind"), "step": t.get("step"),
                                    "weakness": t.get("weakness")})
    app.stop()
    if run_as:
        kill_user(run_as)

    judged = [c for c in report["tests"] if c["outcome"] != "skipped"]
    failed = [c for c in judged if c["outcome"] == "failed"]
    reward = {"build_ok": 1.0 if report["build_ok"] else 0.0}
    for kind in ("functional", "boundary", "security"):
        cases = [c for c in judged if c["kind"] == kind]
        passed = sum(c["outcome"] == "passed" for c in cases)
        reward[f"{kind}_pass_rate"] = round(passed / len(cases), 4) if cases else 1.0
        reward[f"{kind}_judged"] = float(len(cases))
    reward.update({
        "unjudged": float(len(report["tests"]) - len(judged)),
        "vulns_found": float(sum(c["kind"] == "security" for c in failed)),
        "regression_broken": float(sum(c["step"] != step for c in failed)),
        "reward": 1.0 if report["build_ok"] and report["tests"] and all(
            c["outcome"] == "passed" for c in report["tests"]) else 0.0,
    })
    write_output(out_dir / "details.json", json.dumps(report, indent=1, ensure_ascii=False))
    write_output(out_dir / "reward.json", json.dumps(reward, indent=1))
    return {"reward": reward, "report": report}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--tests", required=True, type=Path)
    p.add_argument("--app", required=True, type=Path)
    p.add_argument("--step", required=True)
    p.add_argument("--out", required=True, type=Path)
    p.add_argument("--run-as", default=os.environ.get("DODAM_APP_USER", ""))
    a = p.parse_args(argv)
    result = judge(a.tests, a.app, a.step, a.out, run_as=a.run_as)
    print(json.dumps(result["reward"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
