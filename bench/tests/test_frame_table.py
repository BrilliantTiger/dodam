"""The frame's one place of enforcing and its closed table.

Every container command that code of the repository gives goes through `frame.run` or `frame.popen`. The frame
reads the words into a structure, checks the structure against a closed table of what the repository's own starts
use, writes the command anew from the structure, and starts the container tool itself, from a fixed path, with an
environment in which nothing points it at another daemon. The command that a frame run puts first on the path
goes through the same reading, without the exceptions that only code may name. Whatever the table does not have is
refused, with a diagnostic in JSON. These tests give the frame its tools as stand-ins through a parameter of the
code, never through the path."""
from __future__ import annotations

import json
import os
import pwd
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from dodambench import frame
from frame_standins import Standins

DROP = "/proc/sys/vm/" + "drop" + "_caches"


@pytest.fixture
def tools(tmp_path, monkeypatch) -> Standins:
    made = Standins(tmp_path / "standins")
    monkeypatch.setenv("FRAME_STANDIN", str(made.data))
    # on the path as well, so that nothing a test gives the frame reaches a real container tool by any way
    monkeypatch.setenv("PATH", f"{made.bin}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(frame, "ROOT", tmp_path / "frame-root")
    monkeypatch.setattr(frame, "PLATFORM", "linux")
    monkeypatch.setitem(frame.SETTINGS, "lock_wait_seconds", 1)
    monkeypatch.setitem(frame.SETTINGS, "retry_seconds", 0.05)
    before = frame.use_tools(**made.tools())
    yield made
    frame.close()
    frame.use_tools(**before)


def refused(words, **kw) -> str | None:
    try:
        frame.run(words, capture_output=True, text=True, **kw)
    except frame.FrameRefused as refusal:
        json.dumps(refusal.said)  # a diagnostic in JSON
        return refusal.said["reason"]
    return None


def made(tools: Standins, *words: str) -> str:
    done = frame.run(["docker", "create", *words, "image"], capture_output=True, text=True, check=True)
    return done.stdout.strip()


# --- one closed table, read into a structure and written from it ---------------------------------------------------

# Each row: what code gives, and the words the tool gets after the program and the frame's own mark and id file.
ROWS = [
    (["docker", "create", "--name", "a", "--network", "none", "--user", "0", "--memory", "256m", "--memory-swap", "256m",
      "--cpus", "2", "--pids-limit", "64", "--tmpfs", "/dodam-cases:rw,exec,nosuid,nodev,size=32m,nr_inodes=20000,mode=0711",
      "-e", "DODAM_APP_USER=agent", "--security-opt", "seccomp=unconfined", "--cap-add", "LINUX_IMMUTABLE",
      "--label", "dodam-gemini-run=ab12", "dodam-bench/python:dev", "sh", "-c", "exec sh /tests/test.sh"],
     ["create", "--name", "a", "--network", "none", "--user", "0", "--memory", "256m", "--memory-swap", "256m",
      "--cpus", "2", "--pids-limit", "64", "--tmpfs", "/dodam-cases:rw,exec,nosuid,nodev,size=32m,nr_inodes=20000,mode=0711",
      "--env", "DODAM_APP_USER=agent", "--security-opt", "seccomp=unconfined", "--cap-add", "LINUX_IMMUTABLE",
      "--label", "dodam-gemini-run=ab12", "dodam-bench/python:dev", "sh", "-c", "exec sh /tests/test.sh"]),
    (["docker", "run", "--rm", "--network", "none", "--entrypoint", "/opt/judge/bin/python", "alpine", "-c", "print(1)"],
     ["run", "--rm", "--network", "none", "--entrypoint", "/opt/judge/bin/python", "alpine", "-c", "print(1)"]),
    (["docker", "run", "--detach", "--name", "b", "--network", "none", "--memory", "1g", "dodam-bench/go_x:dev"],
     ["run", "--detach", "--name", "b", "--network", "none", "--memory", "1g", "dodam-bench/go_x:dev"]),
]


@pytest.mark.parametrize("given, written", ROWS)
def test_every_row_of_the_table_is_written_from_what_was_read_and_reads_back_the_same(tools, given, written):
    structure = frame.read_command(given)
    assert frame.read_command(["docker", *frame.write_command(structure)]) == structure
    frame.run(given, capture_output=True, check=True)
    [call] = [c for c in tools.calls() if c[:1] in (["create"], ["run"])]
    assert call[0] == written[0]
    assert call[1] == "--label" and call[2].startswith("dodam.frame.run=") and call[3] == "--cidfile", call
    assert call[:1] + call[5:] == written, call


def test_a_volume_given_the_short_way_is_written_the_long_way_and_only_ro_or_rw_follow_it(tools):
    frame.run(["docker", "volume", "create", "asked"], capture_output=True, check=True)
    frame.run(["docker", "run", "--rm", "-v", "asked:/reading:ro", "image"], capture_output=True, check=True)
    [call] = [c for c in tools.calls() if c[:1] == ["run"]]
    assert call[5:7] == ["--volume", "asked:/reading:ro"], call
    for mode in ("z", "Z", "shared", "rw,z", "nocopy"):
        assert refused(["docker", "run", "--rm", "--volume", f"asked:/reading:{mode}", "image"]) == "value", mode


@pytest.mark.parametrize("words, reason", [
    (["docker", "container", "remove", "--force", "x"], "command"),
    (["docker", "container", "rm", "x"], "command"),
    (["docker", "image", "rmi", "image"], "command"),
    (["docker", "rmi", "image"], "command"),
    (["docker", "image", "rm", "image"], "command"),
    (["docker", "buildx", "--builder", "x", "prune", "-af"], "command"),
    (["docker", "builder", "prune"], "command"),
    (["docker", "system", "prune", "-f"], "command"),
    (["docker", "container", "--log-level", "error", "prune"], "command"),
    (["docker", "network", "prune"], "command"),
    (["docker", "compose", "-p", "x", "down", "--rmi", "all"], "command"),
    (["docker", "compose", "-f", "x.yaml", "up", "-d"], "command"),
    (["docker", "exec", "x", "sh"], "command"),
    (["docker", "rename", "x", "y"], "command"),
    (["docker", "build", "."], "command"),
    (["docker", "volume", "create", "--opt", "type=none", "--opt", "o=bind", "--opt", "device=/", "v"], "option"),
    (["docker", "-D", "run", "image"], "daemon_option"),
    (["docker", "-H", "tcp://elsewhere:2375", "run", "image"], "daemon_option"),
    (["docker", "--context", "other", "create", "image"], "daemon_option"),
    (["docker", "--config", "/tmp/x", "ps"], "daemon_option"),
    (["docker", "run", "--mount", "type=bind,source=/,target=/h", "image"], "option"),
    (["docker", "run", "--mount", "Type=bind,Source=/,target=/h", "image"], "option"),
    (["docker", "run", "--mount", "type=volume,dst=/h,volume-opt=type=none,volume-opt=o=bind,volume-opt=device=/", "image"], "option"),
    (["docker", "run", "--volume=asked:/x", "image"], "option"),
    (["docker", "run", "-itv", "/:/h", "image"], "option"),
    (["docker", "run", "-e=", "-v", "/:/h", "image"], "option"),
    (["docker", "run", "--Volume", "asked:/x", "image"], "option"),
    (["docker", "run", "--pid", "host", "image"], "option"),
    (["docker", "run", "--ipc", "host", "image"], "option"),
    (["docker", "run", "--privileged", "image"], "option"),
    (["docker", "run", "--device", "/dev/sda", "image"], "option"),
    (["docker", "run", "--volumes-from", "x", "image"], "option"),
    (["docker", "run", "--env-file", "/etc/hosts", "image"], "option"),
    (["docker", "run", "--cap-add", "ALL", "image"], "value"),
    (["docker", "run", "--security-opt", "apparmor=unconfined", "image"], "value"),
    (["docker", "run", "--network", "host", "image"], "value"),
    (["docker", "run", "--network", "bridge", "image"], "value"),
    (["docker", "run", "--label", "dodam.frame.run=00000000000000000000000000000000", "image"], "value"),
    (["docker", "run", "--name", "-x", "image"], "value"),
    (["docker", "run", "--name", "a", "--name", "b", "image"], "option"),
    (["docker", "run", "-e", "A=1\nB=2", "image"], "value"),
    (["docker", "run", "--memory", "lots", "image"], "value"),
    (["docker", "run", "--network", "none", "-v", "homevol:/h", "image"], "mount"),
    (["docker", "run", "-v", "/:/root", "image"], "mount"),
    (["docker", "run", "-v", "/var/run/docker.sock:/var/run/docker.sock", "image"], "mount"),
    (["docker", "run", "-v", f"{Path(pwd.getpwuid(os.getuid()).pw_dir)}/.ssh:/s", "image"], "mount"),
    (["docker", "run", "-v", f"{DROP}:/drop", "--network", "none", "image"], "mount"),
    (["docker", "run", '"--volume"', "asked:/x", "image"], "value"),
])
def test_what_the_table_does_not_have_is_refused_and_never_reaches_the_tool(tools, words, reason):
    tools.add_volume("homevol", {})
    assert refused(words) == reason, words
    assert tools.calls() == [], "a refused command reached the container tool"


def test_no_word_reaches_the_tool_that_was_not_written_from_the_table(tools):
    frame.run(["docker", "volume", "create", "asked"], capture_output=True, check=True)
    frame.run(["docker", "run", "--rm", "-e", "A=1", "-v", "asked:/a", "--network", "none", "image", "--weird", "-v", "/:/x"],
              capture_output=True, check=True)
    [call] = [c for c in tools.calls() if c[:1] == ["run"]]
    # what follows the image runs inside the container and is passed as it was; nothing before it is the caller's
    image = call.index("image")
    assert call[5:image] == ["--rm", "--env", "A=1", "--volume", "asked:/a", "--network", "none"], call
    assert call[image + 1:] == ["--weird", "-v", "/:/x"], call


# --- what names a container or a volume: only the batch's -----------------------------------------------------------

@pytest.mark.parametrize("words", [
    ["docker", "rm", "--force", "NAME"], ["docker", "rm", "-f", "NAME"], ["docker", "stop", "NAME"],
    ["docker", "start", "--attach", "NAME"], ["docker", "inspect", "--format", "{{.Id}}", "NAME"],
    ["docker", "inspect", "--type", "container", "--format", "{{.Id}}", "NAME"],
    ["docker", "cp", "NAME:/logs/verifier/.", "PLACE"], ["docker", "volume", "rm", "--force", "homevol"],
])
def test_a_command_that_names_a_container_or_a_volume_passes_only_for_the_batchs_own(tools, tmp_path, words):
    tools.add_container("f" * 64, {}, name="foreign")
    tools.add_volume("homevol", {})
    place = tmp_path / "place"
    place.mkdir()
    given = [w.replace("NAME", "foreign").replace("PLACE", str(place)) for w in words]
    assert refused(given, allow=[place]) in ("not_own", "option"), given
    assert not [c for c in tools.calls() if c[:1] in (["rm"], ["stop"], ["start"], ["cp"]) or c[:2] == ["volume", "rm"]]


def test_several_names_are_each_confirmed_and_written_after_a_double_dash(tools):
    made(tools, "--name", "one")
    made(tools, "--name", "two")
    tools.add_container("f" * 64, {}, name="foreign")
    assert refused(["docker", "rm", "--force", "one", "foreign"]) == "not_own"
    frame.run(["docker", "rm", "--force", "one", "two"], capture_output=True, check=True)
    [call] = [c for c in tools.calls() if c[:1] == ["rm"]]
    assert call == ["rm", "--force", "--", "one", "two"], call
    asked = [c for c in tools.calls() if c[:1] == ["inspect"]]
    assert asked and all("--type" in c and c[c.index("--type") + 1] == "container" for c in asked), asked


def test_a_named_volume_passes_only_when_the_batch_made_it(tools):
    tools.add_volume("homevol", {})
    assert refused(["docker", "run", "--rm", "-v", "homevol:/h", "image"]) == "mount"
    frame.run(["docker", "volume", "create", "asked"], capture_output=True, check=True)
    [call] = [c for c in tools.calls() if c[:2] == ["volume", "create"]]
    assert call[2] == "--label" and call[3].startswith("dodam.frame.run=") and call[-1] == "asked", call
    assert refused(["docker", "run", "--rm", "-v", "asked:/h", "image"]) is None


# --- copying is a mount by another way -------------------------------------------------------------------------------

def test_copying_keeps_a_trailing_dot_and_checks_the_side_of_this_computer(tools, tmp_path):
    made(tools, "--name", "mine")
    given = tmp_path / "given"
    (given / "sub").mkdir(parents=True)
    assert refused(["docker", "cp", f"{given}/.", "mine:/tests"], allow=[given]) is None
    assert refused(["docker", "cp", "mine:/logs/verifier/.", str(given / "sub")], allow=[given]) is None
    copies = [c for c in tools.calls() if c[:1] == ["cp"]]
    assert copies[0][-2:] == [f"{os.path.realpath(given)}/.", "mine:/tests"], copies
    assert copies[1][-2:] == ["mine:/logs/verifier/.", os.path.realpath(given / "sub")], copies
    home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    assert refused(["docker", "cp", f"{home}/.ssh", "mine:/s"]) == "mount"
    assert refused(["docker", "cp", f"{given}/.", "mine:tests"], allow=[given]) == "value"
    assert refused(["docker", "cp", "-L", f"{given}/.", "mine:/tests"], allow=[given]) == "option"


def test_a_folder_named_on_the_call_is_a_work_tree_or_a_folder_below_the_temporary_area_and_nothing_wider(tools, tmp_path):
    made(tools, "--name", "mine")
    temporary = Path(tempfile.gettempdir())
    for wide in (temporary, Path("/tmp"), Path("/var/tmp"), Path(pwd.getpwuid(os.getuid()).pw_dir)):
        assert refused(["docker", "cp", f"{wide}/.", "mine:/t"], allow=[wide]) == "mount", wide
    for name in ("dodam-frame-x", "dodam-gemini-x", "claude-x", ".ssh"):
        inside = tmp_path / name / "in"
        inside.mkdir(parents=True)
        assert refused(["docker", "cp", f"{inside}/.", "mine:/t"], allow=[inside]) == "mount", name
    held = tmp_path / "held"
    held.mkdir()
    (held / "out").symlink_to(pwd.getpwuid(os.getuid()).pw_dir)
    assert refused(["docker", "cp", f"{held}/out", "mine:/t"], allow=[held]) == "mount"


# --- folders of the container machine: keyword only, a container of this batch, closed --------------------------------

def test_folders_of_the_container_machine_are_named_by_keyword_for_a_container_of_this_batch_only(tools):
    mine = made(tools, "--name", "judged")
    foreign = "e" * 64
    tools.add_container(foreign, {}, name="other")
    helper = ["docker", "run", "--detach", "--name", "j-reader", "--network", "none", "--volume", "GROUP:/group",
              "--volume", f"{DROP}:/drop", "image", "python3"]
    for group in (f"/sys/fs/cgroup/docker/{mine}", f"/sys/fs/cgroup/system.slice/docker-{mine}.scope"):
        assert refused([w.replace("GROUP", group) for w in helper], machine=(group, DROP)) is None, group
    group = f"/sys/fs/cgroup/docker/{foreign}"
    assert refused([w.replace("GROUP", group) for w in helper], machine=(group, DROP)) == "mount"
    with pytest.raises(TypeError):
        frame.run([w.replace("GROUP", group) for w in helper], (), (group, DROP))
    other = "/proc/sys/vm/swappiness"
    assert refused(["docker", "run", "--network", "none", "--volume", f"{other}:/x", "image"], machine=(other,)) == "mount"


# --- one place enforces -----------------------------------------------------------------------------------------------

def test_the_code_starts_the_tool_by_its_fixed_path_not_by_the_path(tools, tmp_path, monkeypatch):
    lie = tmp_path / "lie"
    lie.mkdir()
    (lie / "docker").write_text(f"#!/bin/sh\ntouch {tmp_path / 'the-lie-ran'}\n")
    (lie / "docker").chmod(0o755)
    monkeypatch.setenv("PATH", f"{lie}{os.pathsep}{os.environ['PATH']}")
    frame.run(["docker", "ps", "--all", "--quiet"], capture_output=True, check=True)
    assert not (tmp_path / "the-lie-ran").exists()
    assert tools.calls() == [["ps", "--all", "--quiet"]]


def test_the_tool_is_started_without_what_would_point_it_at_another_daemon(tools, monkeypatch):
    for name, value in {"DOCKER_HOST": "tcp://elsewhere:2375", "DOCKER_CONTEXT": "other", "DOCKER_CONFIG": "/tmp/plug",
                        "DOCKER_CERT_PATH": "/tmp/certs", "DOCKER_TLS_VERIFY": "1", "BUILDX_CONFIG": "/tmp/b"}.items():
        monkeypatch.setenv(name, value)
    frame.run(["docker", "ps", "--quiet"], capture_output=True, check=True)
    assert tools.envs() == [{}], tools.envs()


def test_no_file_in_the_run_folder_lets_a_command_pass_its_check(tools):
    with frame.Batch(shim=True) as held:
        for name in ("approved", "approve", "allow"):
            place = held.folder / name
            place.mkdir(exist_ok=True)
            for digest in ("0" * 64, "f" * 64):
                (place / digest).write_text("")
        env = {**os.environ, **held.env()}
        done = subprocess.run(["docker", "run", "--network", "none", "-v", f"{DROP}:/d", "image"], env=env,
                              capture_output=True, text=True)
        assert done.returncode == 2 and json.loads(done.stderr)["reason"] == "mount", done.stderr


def test_a_stopped_batch_starts_nothing_by_any_way(tools):
    with frame.Batch(shim=True) as held:
        frame.close()
        held.stop("free_disk")
        env = {**os.environ, **held.env()}
        done = subprocess.run(["docker", "create", "image"], env=env, capture_output=True, text=True)
        assert done.returncode == 2 and json.loads(done.stderr)["reason"] == "free_disk", done.stderr
    assert [c for c in tools.calls() if c[:1] == ["create"]] == []


def test_in_the_automatic_check_the_command_on_the_path_refuses_every_call(tools):
    with frame.Batch(shim="refuse") as held:
        env = {**os.environ, **held.env()}
        for words in (["ps"], ["run", "image"], ["info"], ["image", "inspect", "image"]):
            done = subprocess.run(["docker", *words], env=env, capture_output=True, text=True)
            assert done.returncode == 2 and json.loads(done.stderr)["reason"] == "refuse_all", (words, done.stderr)
    assert tools.calls() == []


def test_the_automatic_check_puts_the_refusing_command_first_on_the_path_of_the_jobs_that_start_containers():
    text = (Path(__file__).resolve().parents[2] / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert text.count("python -m dodambench.frame run --refuse-all --") >= 2, "the jobs do not run under the refusing command"


# --- nothing from the caller's environment ----------------------------------------------------------------------------

def test_a_lying_path_changes_no_reading(tools, tmp_path, monkeypatch):
    monkeypatch.setattr(frame, "PLATFORM", "darwin")
    monkeypatch.setattr(frame, "free_disk", lambda path: 100 * 2**30)
    tools.pressure(4)
    lie = tmp_path / "lie"
    lie.mkdir()
    for name, said in (("sysctl", "echo 0"), ("pgrep", "exit 1"), ("ps", "echo 1")):
        (lie / name).write_text(f"#!/bin/sh\n{said}\n")
        (lie / name).chmod(0o755)
    monkeypatch.setenv("PATH", f"{lie}{os.pathsep}{os.environ['PATH']}")
    with pytest.raises(frame.FrameRefused) as said:
        frame.Batch().open()
    assert said.value.said["reason"] == "pressure"


def test_the_home_folder_is_the_accounts_and_not_what_the_environment_says(tools, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "elsewhere"))
    assert frame.home() == Path(pwd.getpwuid(os.getuid()).pw_dir)
    made(tools, "--name", "mine")
    real = Path(pwd.getpwuid(os.getuid()).pw_dir)
    assert refused(["docker", "cp", f"{real}/.ssh", "mine:/s"], allow=[real / ".ssh"]) == "mount"


# --- the promises at the edges ----------------------------------------------------------------------------------------

def _dead_pid() -> int:
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


def test_a_dead_run_whose_start_could_not_be_read_is_collected(tools):
    root = frame.ROOT
    run = "a" * 32
    folder = root / "runs" / run
    (folder / "cids").mkdir(parents=True)
    root.chmod(0o700)
    (folder / "state.json").write_text(json.dumps({"run": run, "pid": _dead_pid(), "started": None, "made": True}))
    tools.add_container("c" * 64, {"dodam.frame.run": run})
    (folder / "cids" / "x").write_text("c" * 64)
    frame.Batch().open().close()
    assert tools.containers() == [] and not folder.exists()


def test_no_signal_goes_to_a_group_whose_leader_cannot_be_confirmed(tools):
    root = frame.ROOT
    run = "b" * 32
    folder = root / "runs" / run
    folder.mkdir(parents=True)
    root.chmod(0o700)
    alive = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        (folder / "state.json").write_text(json.dumps({"run": run, "pid": _dead_pid(), "started": "x",
                                                       "pgid": alive.pid, "child_started": None}))
        frame.Batch().open().close()
        assert alive.poll() is None, "a group whose leader could not be confirmed was sent a signal"
    finally:
        alive.kill()
        alive.wait()


def test_a_runs_record_stays_while_one_of_its_volumes_could_not_be_removed(tools):
    frame.run(["docker", "volume", "create", "asked"], capture_output=True, check=True)
    run = frame.current().run_id
    (tools.data / "volume-rm-fails").write_text("")
    frame.close()
    assert [v["name"] for v in tools.volumes()] == ["asked"]
    assert (frame.ROOT / "runs" / run).exists(), "the run's record went while its volume was still there"


def test_the_pilots_batch_stops_harbor_at_a_line_and_removes_its_containers_while_the_lock_is_held(tools, tmp_path, monkeypatch):
    from dodambench import pilot
    from test_pilot import FakeDocker, pilot_args
    seen = {}

    class Docker(FakeDocker):
        def remove(self, prefix):
            seen["lock_held"] = frame.current() is not None
            return super().remove(prefix)

    def runner(cmd, env, log_path, timeout, on_start=None):
        jobs = Path(cmd[cmd.index("-o") + 1]) / cmd[cmd.index("--job-name") + 1]
        jobs.mkdir(parents=True)
        frame.current().stop("free_disk")
        request = on_start(subprocess.Popen(["true"])) if on_start else None
        deadline = time.monotonic() + 5
        while request is not None and not request.asked.is_set() and time.monotonic() < deadline:
            time.sleep(0.05)
        seen["asked"] = request is not None and request.asked.is_set()
        return 1
    try:
        pilot.run_pilot(runner=runner, **pilot_args(tmp_path, docker=Docker()))
    except Exception:  # the pilot reports a stopped run as it reports others; what matters is what was seen
        pass
    assert seen.get("asked") is True, "the frame's stop did not reach Harbor's run"
    assert seen.get("lock_held") is True, "Harbor's containers were removed after the batch had let go of its lock"


def test_the_gemini_jobs_attack_batch_names_its_checkout_as_a_work_tree():
    import ast
    source = (Path(__file__).resolve().parents[2] / "tools/gemini/job.py").read_text(encoding="utf-8")
    calls = [n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr == "batch"]
    assert calls and all({k.arg for k in c.keywords} >= {"worktrees", "on_stop"} for c in calls), \
        "the attack run's batch names no work tree or no way to stop the run"


# --- the lock on the files: a closed list of places --------------------------------------------------------------------

TOOL_NAMES = ("docker", "colima", "podman", "docker-compose")


def places_naming_a_container_tool() -> set[tuple[str, str]]:
    """Every function, class body or top of a tracked Python, shell or workflow file in which the name of a container
    tool or of the container machine appears, by file and place."""
    import ast
    repo = Path(__file__).resolve().parents[2]
    listed = subprocess.run(["git", "ls-files"], cwd=repo, capture_output=True, text=True, check=True).stdout.split()
    found = set()
    for f in listed:
        path = repo / f
        if not path.is_file() or not f.endswith((".py", ".sh", ".yml", ".yaml")):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if not any(name in text for name in TOOL_NAMES):
            continue
        if f.endswith(".py"):
            tree = ast.parse(text)
            parents = {c: n for n in ast.walk(tree) for c in ast.iter_child_nodes(n)}
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and any(
                        name in node.value for name in TOOL_NAMES):
                    owner = node
                    while owner in parents and not isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        owner = parents[owner]
                    found.add((f, getattr(owner, "name", "<top>")))
        else:
            found.add((f, "<file>"))
    return found


def test_every_place_that_names_a_container_tool_is_on_the_closed_list_with_its_reason():
    from frame_places import PLACES
    places = places_naming_a_container_tool()
    unlisted = sorted(places - set(PLACES))
    assert unlisted == [], "places that name a container tool and are not on the list: " + json.dumps(unlisted)
    assert all(isinstance(reason, str) and len(reason) > 20 for reason in PLACES.values())
