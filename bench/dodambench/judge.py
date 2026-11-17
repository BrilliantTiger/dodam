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

A command-line task has no program to keep running. After the build the judge runs each hidden test
file, and the tests run the program once per case through dodam_cli.py, which sits next to this
file. In the verifier the program then runs as a user of its own that owns nothing: the judge closes
the tests and the results to everyone but root, takes write access away from every place others
could write, and leaves the program only its case's folder. When the judge itself cannot do its
work it writes judge-fault.json instead of a result and ends with exit code 3, so a fault of the
judge never looks like a failure of the program.
"""
from __future__ import annotations

import argparse
import grp
import json
import os
import pwd
import re
import secrets
import shlex
import shutil
import signal
import stat
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


def port_in_use(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


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
                try:
                    if not os.path.islink(path):
                        os.chmod(path, os.stat(path).st_mode & ~0o022)
                except OSError:
                    pass  # a read-only folder is closed already


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


class JudgeFault(Exception):
    """The judge could not do its own work; no result is written."""


CLI_LIMITS = {"timeout_sec": "CASE_TIMEOUT_SEC", "memory_mb": "CASE_MEMORY_MB", "output_kb": "CASE_OUTPUT_KB",
              "processes": "CASE_PROCESSES", "disk_mb": "CASE_DISK_MB"}
FIRST_CASE_USER_ID, LAST_CASE_USER_ID = 200_000, 2_000_000_000
NO_FILES = {"proc", "sysfs", "cgroup", "cgroup2", "devpts", "securityfs", "debugfs", "tracefs", "pstore", "bpf"}
# What a file of hidden tests may take besides the time of its cases: starting Python and the judge's own work.
FILE_SECONDS, CASE_SECONDS = 30, 2


def free_user_id() -> int:
    """A user and group number nobody in this container has, so the user owns nothing and is in no group.

    It is drawn at random from a wide range: the kernel counts a user's processes over the whole
    machine, so two judges running side by side must not share the number."""
    users = {u.pw_uid for u in pwd.getpwall()} | {u.pw_gid for u in pwd.getpwall()}
    groups = {g.gr_gid for g in grp.getgrall()}
    while True:
        number = FIRST_CASE_USER_ID + secrets.randbelow(LAST_CASE_USER_ID - FIRST_CASE_USER_ID)
        if number not in users and number not in groups:
            return number


def writable_by_others(mounts: Path = Path("/proc/self/mounts")) -> list[str]:
    """Every folder and file any user may write, on every file system mounted here."""
    tops = []
    for line in mounts.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[2] not in NO_FILES:
            tops.append(parts[1].replace("\\040", " "))
    found: list[str] = []
    for top in tops:
        res = subprocess.run(["find", top, "-xdev", "(", "-type", "d", "-o", "-type", "f", ")", "-perm", "-0002",
                              "-print0"], capture_output=True)
        found += [p.decode("utf-8", errors="surrogateescape") for p in res.stdout.split(b"\0") if p]
    return sorted(set(found))


def close_writable_places() -> int:
    """Take write access away from every place any user could write; says how many places it closed."""
    closed = 0
    for path in writable_by_others():
        try:
            os.chmod(path, stat.S_IMODE(os.lstat(path).st_mode) & ~0o002)
            closed += 1
        except OSError as exc:
            if exc.errno != 30:  # a read-only file system cannot be written anyway
                raise JudgeFault(f"cannot close {path} to the program: {exc}") from None
    return closed


def open_for_reading(app_dir: Path) -> None:
    """After the build: everyone may read and run the program, nobody but its owner may change it,
    and no file of it lends its owner's rights to whoever runs it."""
    for root, dirs, files in os.walk(app_dir):
        for name in [""] + dirs + files:
            path = os.path.join(root, name) if name else root
            try:
                if os.path.islink(path):
                    continue
                mode = stat.S_IMODE(os.stat(path).st_mode)
                mode = (mode | 0o004 | (0o001 if mode & 0o100 or os.path.isdir(path) else 0)) & ~0o6022
                os.chmod(path, mode)
            except OSError:
                pass


def keep_private(folder: Path) -> None:
    """Only root may enter the folder, and no user may rename or replace it from the folder above."""
    os.chown(folder, 0, 0)
    os.chmod(folder, 0o700)
    for parent in folder.parents:
        info = os.stat(parent)
        if info.st_uid != 0 or info.st_mode & 0o022:
            try:
                os.chown(parent, 0, -1)
                os.chmod(parent, stat.S_IMODE(info.st_mode) & ~0o022)
            except OSError as exc:
                if exc.errno != 30:
                    raise JudgeFault(f"cannot protect {parent}: {exc}") from None


class CliRun:
    """How the hidden tests of a command-line task reach the program: settings for dodam_cli.py."""

    def __init__(self, app_dir: Path, tests_dir: Path, out_dir: Path, lang_env: dict[str, str], run_as: str,
                 extra_env: dict[str, str]):
        self.run_as = run_as
        self.uid: int | None = None
        try:
            limits = {name: int(lang_env[key]) for name, key in CLI_LIMITS.items()}
            command = [part.replace("{app}", str(app_dir)) for part in shlex.split(lang_env["RUN_CMD"])]
        except (KeyError, ValueError) as exc:
            raise JudgeFault(f"the task's settings for a command-line program are incomplete: {exc}") from None
        if not command:
            raise JudgeFault("the task's settings name no command to run the program")
        self.limits = limits
        if run_as:
            kill_user(run_as)  # whatever the build left running
            open_for_reading(app_dir)
            self.uid = free_user_id()
            keep_private(tests_dir)
            keep_private(out_dir)
            self.closed = close_writable_places()
        self.private = Path(tempfile.mkdtemp(prefix="dodam-judge-"))
        self.work_root = Path(tempfile.mkdtemp(prefix="dodam-cases-"))
        os.chmod(self.work_root, 0o711)
        self.fault_file = self.private / "faults.jsonl"
        env = {k: v for k, v in {**os.environ, **extra_env}.items()
               if not k.startswith(("DODAM_", "PYTEST_")) and k not in ("PYTHONPATH", "HOME", "TMPDIR", "PWD", "OLDPWD")}
        settings = {"command": command, "uid": self.uid, "gid": self.uid, "limits": limits,
                    "work_root": str(self.work_root), "fault_file": str(self.fault_file), "env": env}
        self.settings_file = self.private / "settings.json"
        self.settings_file.write_text(json.dumps(settings), encoding="utf-8")
        # the tests get nothing that differs from one language to the next but the settings file itself
        self.test_env = {"PATH": extra_env.get("PATH", os.environ.get("PATH", "")),
                         "PYTHONPATH": str(Path(__file__).resolve().parent),
                         "PYTHONDONTWRITEBYTECODE": "1", "DODAM_CLI_CONFIG": str(self.settings_file)}

    def run_file(self, path: Path, junit: Path) -> list[dict]:
        tests = len(re.findall(r"^def test_", path.read_text(encoding="utf-8"), flags=re.M))
        timeout = FILE_SECONDS + tests * (self.limits["timeout_sec"] + CASE_SECONDS)
        cases = run_pytest(path, self.test_env, junit, timeout)
        if self.fault_file.exists():
            raise JudgeFault(self.fault_file.read_text(encoding="utf-8").strip()[:2000])
        self.sweep()
        whole = [c for c in cases if c["name"] == path.name]
        if whole:
            # a program cannot make a file of tests run out of time or end without a report: its cases are limited
            raise JudgeFault(f"{path.name}: {whole[0]['message']}")
        return cases

    def sweep(self) -> None:
        """Between two files of tests nothing of the program is left: no process, no case folder."""
        if self.uid is not None:
            for _ in range(200):
                pids = user_pids(self.uid)
                if not pids:
                    break
                for pid in pids:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                time.sleep(0.01)
        if not self.work_root.is_dir():
            raise JudgeFault("the folder the cases live in is gone")
        for entry in self.work_root.iterdir():
            shutil.rmtree(entry, ignore_errors=True)

    def close(self) -> None:
        try:
            self.sweep()
        except JudgeFault:
            pass
        shutil.rmtree(self.work_root, ignore_errors=True)
        shutil.rmtree(self.private, ignore_errors=True)


def write_fault(out_dir: Path, text: str) -> None:
    """Say that the judge failed, and leave no result that could be read as a judgement of the program."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for name in ("reward.json", "reward.txt"):
        path = out_dir / name
        if path.is_symlink() or path.exists():
            path.unlink()
    write_output(out_dir / "judge-fault.json", json.dumps({"judge_fault": text}, indent=1, ensure_ascii=False))


class App:
    def __init__(self, app_dir: Path, start_cmd: str, port: int, run_as: str, extra_env: dict[str, str]):
        self.app_dir, self.start_cmd, self.port, self.run_as = app_dir, start_cmd, port, run_as
        self.extra_env = extra_env
        self.proc: subprocess.Popen | None = None
        self.data_dir: Path | None = None
        self.env: dict[str, str] = {}

    def start(self, timeout: float = 60) -> bool:
        self.stop()
        # whatever still answers on the port is not this start's program
        deadline = time.monotonic() + 5
        while port_in_use(self.port):
            if time.monotonic() > deadline:
                return False
            time.sleep(0.2)
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
        if self.proc and self.run_as:
            # su starts the program in a session of its own, out of reach of the group signal
            kill_user(self.run_as)
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

    cli = None
    if lang_env.get("INTERFACE") == "cli" and report["build_ok"]:
        cli = CliRun(app_dir, tests_dir, out_dir, lang_env, run_as, extra_env or {})
    app = App(app_dir, lang_env.get("START_CMD", ""), port, run_as, extra_env or {})
    shared_up = False
    for t in selected:
        path = tests_dir / t["file"]
        # a program that does not build or start fails its features but shows no weakness
        not_run = "skipped" if t.get("kind") == "security" else "failed"
        cases: list[dict]
        if not report["build_ok"]:
            cases = [{"name": path.name, "outcome": not_run, "message": "build failed"}]
        elif cli is not None:
            try:
                cases = cli.run_file(path, out_dir / ("junit-" + t["file"].replace("/", "-") + ".xml"))
            except JudgeFault:
                cli.close()
                raise
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
    if cli is not None:
        cli.close()
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
    try:
        result = judge(a.tests, a.app, a.step, a.out, run_as=a.run_as)
    except JudgeFault as exc:
        write_fault(a.out.resolve(), str(exc))
        print(json.dumps({"judge_fault": str(exc)}), file=sys.stderr)
        return 3
    print(json.dumps(result["reward"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
