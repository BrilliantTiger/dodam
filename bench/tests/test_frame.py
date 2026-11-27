"""The one frame through which every container on this computer is started.

The rules for containers were written in briefs only: free space before a run, one batch at a time, the
space given back afterwards, and nothing removed but one's own. On 18 November the disk filled that way, on
25 November a bulk run ended the login session, and on 26 November a session that cleaned up its own container
removed six old ones by a filter. These tests hold the frame to each rule, with stand-ins for the container
command, for the container machine's command and for the readings of this computer, so that they run without a
container. The last of them is the lock itself: a file of the repository that starts a container and neither
goes through the frame nor stands among the exemptions with its reason fails it."""
from __future__ import annotations

import ast
import json
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from pathlib import Path

import pytest

from dodambench import frame
from frame_standins import Standins

BENCH = Path(__file__).resolve().parents[1]
REPO = BENCH.parent
GIB = 2**30


@pytest.fixture
def standins(tmp_path, monkeypatch) -> Standins:
    made = Standins(tmp_path / "standins")
    for name, value in made.env().items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(frame, "ROOT", tmp_path / "frame-root")
    monkeypatch.setattr(frame, "PLATFORM", "linux")
    monkeypatch.setitem(frame.SETTINGS, "lock_wait_seconds", 1)
    monkeypatch.setitem(frame.SETTINGS, "retry_seconds", 0.05)
    # the frame starts its tools from fixed places: the stand-ins are given to it, not found on the path
    before = frame.use_tools(**made.tools())
    yield made
    frame.close()
    frame.use_tools(**before)


@pytest.fixture
def this_computer(standins, monkeypatch) -> dict:
    """The frame as it runs on this computer, whose readings the stand-ins give; free disk is set per test."""
    monkeypatch.setattr(frame, "PLATFORM", "darwin")
    disk = {"free": 100 * GIB}
    monkeypatch.setattr(frame, "free_disk", lambda path: disk["free"])
    return disk


def started(batch, *words: str) -> str:
    """Start a container through the stand-in, as a caller does: the frame's words, then the caller's run."""
    done = batch.run(["docker", "create", "--name", "x", *words, "image", "true"],
                          capture_output=True, text=True, check=True)
    return done.stdout.strip()


# --- the lines ---------------------------------------------------------------------------------------------------

def test_the_lines_sit_in_one_place_as_named_settings():
    names = ("free_disk_gib", "disk_drop_gib", "pressure_level", "swap_used_gb", "fseventsd_gb", "every_seconds",
             "lock_wait_seconds", "trim_seconds")
    assert {name: frame.SETTINGS.get(name, "missing") for name in names} == {
        "free_disk_gib": 35, "disk_drop_gib": 10, "pressure_level": 2, "swap_used_gb": None, "fseventsd_gb": 3,
        "every_seconds": 120, "lock_wait_seconds": 60, "trim_seconds": 30}
    # the owner is still to decide the memory line (beside the pressure level, perhaps swap in use) and the way
    # fseventsd is measured; the two lines of the disk are the project's own
    assert set(frame.PENDING) == {"pressure_level", "swap_used_gb", "fseventsd_gb"}


# --- free space and the readings before a batch starts -----------------------------------------------------------

def test_a_batch_is_refused_under_the_free_disk_line(this_computer):
    this_computer["free"] = 34 * GIB
    with pytest.raises(frame.FrameRefused) as refused:
        frame.Batch().open()
    assert refused.value.said["reason"] == "free_disk"
    this_computer["free"] = 36 * GIB
    frame.Batch().open().close()


@pytest.mark.parametrize("level, refused", [(1, False), (2, True), (4, True)])
def test_a_batch_is_refused_at_memory_pressure_level_two_or_more(this_computer, standins, level, refused):
    standins.pressure(level)
    if refused:
        with pytest.raises(frame.FrameRefused) as said:
            frame.Batch().open()
        assert said.value.said["reason"] == "pressure"
    else:
        frame.Batch().open().close()


@pytest.mark.parametrize("memory, refused", [({"301": "2900M"}, False), ({"301": "3100M+"}, True),
                                             ({"301": "120M", "302": "4G"}, True), ({}, False)])
def test_a_batch_is_refused_when_fseventsd_holds_more_than_three_gigabytes(this_computer, standins, memory, refused):
    """By top, with its unit; several processes of that name are each read, and none is a reading of none."""
    standins.fseventsd(memory)
    if refused:
        with pytest.raises(frame.FrameRefused) as said:
            frame.Batch().open()
        assert said.value.said["reason"] == "fseventsd"
    else:
        frame.Batch().open().close()


@pytest.mark.parametrize("said, value", [("120M", 120 * 2**20), ("3100M+", 3100 * 2**20), ("512K-", 512 * 2**10),
                                         ("4G", 4 * GIB), ("99B", 99), ("", None), ("lots", None)])
def test_top_is_read_with_its_unit(said, value):
    assert frame.read_top_memory(said) == value


def test_a_reading_that_cannot_be_made_twice_stops_and_one_that_fails_once_does_not(this_computer, standins):
    standins.pressure(None)
    with pytest.raises(frame.FrameRefused) as said:
        frame.Batch().open()
    assert said.value.said["reason"] == "reading_failed"
    standins.pressure(1)
    (standins.data / "pressure-fail-once").write_text("")
    frame.Batch().open().close()
    assert not (standins.data / "pressure-fail-once").exists()


def test_elsewhere_than_this_computer_its_readings_are_not_made_and_the_rest_is_the_same(standins, monkeypatch):
    """Where the frame runs is told by the platform. Away from this computer the readings that belong to it
    are not made, so that they cannot refuse there; the mark and the collecting are the same."""
    monkeypatch.setattr(frame, "free_disk", lambda path: 1)
    standins.pressure(4)
    standins.fseventsd({"301": "9G"})
    batch = frame.Batch().open()
    ident = started(batch)
    batch.close()
    assert ident not in [c["id"] for c in standins.containers()]
    assert standins.machine_calls() == []


def test_an_environment_variable_does_not_say_where_the_frame_runs(standins, monkeypatch):
    for name in ("DODAM_FRAME_PLATFORM", "DODAM_PLATFORM", "CI", "GITHUB_ACTIONS"):
        monkeypatch.setenv(name, "darwin")
    monkeypatch.setattr(frame, "free_disk", lambda path: 1)
    frame.Batch().open().close()


# --- the measuring while a batch runs -----------------------------------------------------------------------------

@pytest.mark.parametrize("change, reason", [("disk_low", "free_disk"), ("disk_drop", "disk_drop"),
                                            ("pressure", "pressure"), ("fseventsd", "fseventsd")])
def test_a_batch_stops_itself_when_a_reading_crosses_its_line(this_computer, standins, monkeypatch, change, reason):
    """Every interval it reads again; past a line it removes its own containers and starts no more."""
    monkeypatch.setitem(frame.SETTINGS, "every_seconds", 0.2)
    this_computer["free"] = 60 * GIB
    stops = []
    batch = frame.Batch(on_stop=stops.append).open()
    ident = started(batch)
    if change == "disk_low":
        this_computer["free"] = 34 * GIB
    elif change == "disk_drop":
        this_computer["free"] = 49 * GIB
    elif change == "pressure":
        standins.pressure(2)
    else:
        standins.fseventsd({"301": "3200M"})
    deadline = time.monotonic() + 10
    while batch.stopped is None and time.monotonic() < deadline:
        time.sleep(0.05)
    try:
        assert batch.stopped == reason
        assert stops == [reason]
        assert ident not in [c["id"] for c in standins.containers()]
        with pytest.raises(frame.FrameStopped):
            batch.run(["docker", "create", "image"], capture_output=True)
    finally:
        batch.close()


def test_a_drop_of_exactly_ten_gibibytes_does_not_stop_a_batch(this_computer, standins, monkeypatch):
    monkeypatch.setitem(frame.SETTINGS, "every_seconds", 0.1)
    this_computer["free"] = 60 * GIB
    batch = frame.Batch(on_stop=lambda reason: None).open()
    this_computer["free"] = 50 * GIB
    time.sleep(0.6)
    assert batch.stopped is None
    batch.close()


# --- one batch at a time -------------------------------------------------------------------------------------------

HOLDER = r'''
import os, sys, time
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from dodambench import frame
frame.ROOT = Path(sys.argv[2])
frame.PLATFORM = "linux"
standins = Path(os.environ["FRAME_STANDIN"]).parent / "bin"
frame.use_tools(**{name: str(standins / name) for name in ("docker", "colima", "sysctl", "pgrep", "top")})
sys.exit(frame.main(["run", "--", *sys.argv[3:]]))
'''


def holder(standins: Standins, root: Path, *command: str) -> subprocess.Popen:
    env = {**os.environ, **standins.env()}
    return subprocess.Popen([sys.executable, "-c", HOLDER, str(BENCH), str(root), *command], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)


def wait_for(path: Path, seconds: float = 20) -> None:
    deadline = time.monotonic() + seconds
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert path.exists(), f"{path.name} never came"


def test_a_second_batch_waits_and_is_then_refused_with_the_holders_process(standins, tmp_path):
    ready = tmp_path / "ready"
    held = holder(standins, frame.ROOT, "sh", "-c", f"echo $$ > {ready}.pid; touch {ready}; sleep 30")
    try:
        wait_for(ready)
        began = time.monotonic()
        with pytest.raises(frame.FrameRefused) as refused:
            frame.Batch().open()
        assert time.monotonic() - began >= 1
        assert refused.value.said["reason"] == "lock_held"
        assert refused.value.said["holder"] == held.pid
    finally:
        os.killpg(held.pid, signal.SIGKILL)
        held.wait()
        end_programs(int(Path(f"{ready}.pid").read_text()))


def test_the_container_calls_of_one_process_are_one_batch_until_it_is_closed(standins):
    frame.run(["docker", "create", "image"], capture_output=True, check=True)
    frame.run(["docker", "run", "--rm", "image"], capture_output=True, check=True)
    first, second = [c for c in standins.calls() if c[:1] in (["create"], ["run"])]
    assert "--label" in first and "--label" in second, (first, second)
    label = first[first.index("--label") + 1]
    assert label.startswith("dodam.frame.run=") and second[second.index("--label") + 1] == label
    assert frame.current() is not None
    frame.close()
    assert frame.current() is None


def test_a_process_inside_a_frame_run_joins_that_batch_and_waits_for_no_lock(standins, tmp_path):
    said = tmp_path / "joined.json"
    child = textwrap.dedent(f'''
        import json, sys
        sys.path.insert(0, {str(BENCH)!r})
        from pathlib import Path
        from dodambench import frame
        frame.ROOT = Path({str(frame.ROOT)!r})
        frame.use_tools(docker={str(standins.bin / "docker")!r})
        frame.run(["docker", "create", "image"], capture_output=True, check=True)
        Path({str(said)!r}).write_text(json.dumps(frame.current().run_id))
    ''')
    held = holder(standins, frame.ROOT, sys.executable, "-c", child)
    out, err = held.communicate(timeout=60)
    assert held.returncode == 0, err
    [cmd] = [c for c in standins.calls() if c[:1] == ["create"]]
    assert "--label" in cmd, cmd
    assert cmd[cmd.index("--label") + 1].startswith("dodam.frame.run=")


def test_naming_another_run_in_the_environment_does_not_let_a_process_into_it(standins, tmp_path, monkeypatch):
    ready = tmp_path / "ready"
    held = holder(standins, frame.ROOT, "sh", "-c",
                  f"echo $DODAM_FRAME_RUN > {ready}.id; echo $$ > {ready}.pid; touch {ready}; sleep 30")
    try:
        wait_for(ready)
        monkeypatch.setenv("DODAM_FRAME_RUN", Path(f"{ready}.id").read_text().strip())
        with pytest.raises(frame.FrameRefused) as refused:
            frame.run(["docker", "create", "image"], capture_output=True)
        assert refused.value.said["reason"] == "lock_held"
    finally:
        os.killpg(held.pid, signal.SIGKILL)
        held.wait()
        end_programs(int(Path(f"{ready}.pid").read_text()))


# --- the mark, and collecting exactly one's own --------------------------------------------------------------------


def test_the_end_collects_exactly_the_containers_it_started_by_their_id(standins):
    standins.add_container("f" * 64, {}, running=True)
    standins.add_container("e" * 64, {"dodam.frame.run": "someone-else"}, running=True)
    batch = frame.Batch().open()
    mine = [started(batch), started(batch)]
    batch.close()
    assert sorted(c["id"] for c in standins.containers()) == ["e" * 64, "f" * 64]
    removing = [call for call in standins.calls() if call[:1] == ["rm"]]
    assert removing and all(call[:2] == ["rm", "--force"] for call in removing)
    assert sorted(i for call in removing for i in call[2:] if i != "--") == sorted(mine)
    assert not [call for call in standins.calls() if any("prune" in word or word == "rmi" for word in call)]


def test_a_container_whose_id_never_reached_its_file_is_found_by_the_exact_mark_of_the_run(standins):
    batch = frame.Batch().open()
    batch.run(["docker", "create", "image"], capture_output=True)  # made: and one killed before its id was written
    standins.add_container("a" * 64, {"dodam.frame.run": batch.run_id})
    standins.add_container("b" * 64, {"dodam.frame.run": batch.run_id + "0"})
    batch.close()
    assert [c["id"] for c in standins.containers()] == ["b" * 64]


def test_the_end_collects_also_when_the_work_inside_fails(standins):
    with pytest.raises(RuntimeError):
        with frame.Batch() as batch:
            ident = started(batch)
            raise RuntimeError("the work failed")
    assert ident not in [c["id"] for c in standins.containers()]


def test_a_named_volume_made_through_the_frame_is_removed_with_it_and_no_other(standins):
    standins.add_volume("kept", {})
    with frame.Batch() as batch:
        batch.run(["docker", "volume", "create", "asked"], check=True, capture_output=True)
    assert [v["name"] for v in standins.volumes()] == ["kept"]


def test_the_temporary_folder_of_a_batch_is_gone_at_its_end(standins):
    with frame.Batch() as batch:
        (batch.tmp / "left.txt").write_text("x")
        place = batch.folder
    assert not place.exists()


KILLED = '''
set -e
docker create --name a image true > /dev/null
sleep 300 &
echo $$ $! > {pids}
echo $TMPDIR > {tmp}
touch {ready}
wait
'''


def test_what_a_run_that_was_killed_left_is_ended_and_removed_by_the_next_batch(standins, tmp_path):
    """Its programs, its containers and its temporary folder; while the killed run held the lock, nothing of it."""
    ready, pids, tmp = tmp_path / "ready", tmp_path / "pids", tmp_path / "tmp"
    held = holder(standins, frame.ROOT, "sh", "-c", KILLED.format(ready=ready, pids=pids, tmp=tmp))
    wait_for(ready)
    shell, left = (int(n) for n in pids.read_text().split())
    try:
        frame_ends_what_the_killed_run_left(standins, held, left, Path(tmp.read_text().strip()))
    finally:
        end_programs(shell, left)


def end_programs(*pids: int) -> None:
    """What a test started and the frame should have ended: ended here if it is still there."""
    for pid in pids:
        for end in (os.killpg, os.kill):  # its group, where it leads one, and itself
            try:
                end(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass


def frame_ends_what_the_killed_run_left(standins, held, left: int, folder: Path) -> None:
    assert len(standins.containers()) == 1 and folder.is_dir()
    os.kill(held.pid, signal.SIGKILL)
    held.wait()
    os.kill(left, 0)  # the program outlived the run that started it
    with frame.Batch():
        pass
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            os.kill(left, 0)
            time.sleep(0.05)
        except ProcessLookupError:
            break
    with pytest.raises(ProcessLookupError):
        os.kill(left, 0)
    assert standins.containers() == []
    assert not folder.exists()


def test_a_frame_run_ends_the_programs_it_started_and_its_temporary_folder(standins, tmp_path):
    pids, tmp = tmp_path / "pids", tmp_path / "tmp"
    done = holder(standins, frame.ROOT, "sh", "-c", f"sleep 300 > /dev/null 2>&1 & echo $! > {pids}; echo $TMPDIR > {tmp}")
    done.communicate(timeout=60)
    assert done.returncode == 0
    left = int(pids.read_text())
    time.sleep(0.2)
    try:
        with pytest.raises(ProcessLookupError):
            os.kill(left, 0)
    finally:
        end_programs(left)
    assert not Path(tmp.read_text().strip()).exists()


def test_a_frame_run_gives_back_the_exit_code_of_its_command(standins):
    done = holder(standins, frame.ROOT, "sh", "-c", "exit 7")
    done.communicate(timeout=60)
    assert done.returncode == 7


# --- the mounts ----------------------------------------------------------------------------------------------------


# --- giving space back ---------------------------------------------------------------------------------------------

def trims(standins: Standins) -> list[list[str]]:
    return [call for call in standins.machine_calls() if call[:1] == ["ssh"]]


def test_space_is_given_back_once_when_the_batch_started_a_container_and_none_runs(this_computer, standins):
    with frame.Batch() as batch:
        started(batch)
    assert trims(standins) == [["ssh", "--", "sudo", "fstrim", "-a"]]
    assert not [c for c in standins.machine_calls() if c[:1] in (["start"], ["stop"], ["restart"], ["delete"])]


def test_space_is_not_given_back_while_any_container_runs(this_computer, standins):
    standins.add_container("c" * 64, {}, running=True)
    with frame.Batch() as batch:
        started(batch)
    assert trims(standins) == []
    assert [c["id"] for c in standins.containers()] == ["c" * 64]


def test_space_is_not_given_back_after_a_batch_that_started_nothing_or_when_the_machine_is_stopped(this_computer, standins):
    with frame.Batch():
        pass
    (standins.data / "machine-running").unlink()
    with frame.Batch() as batch:
        started(batch)
    assert trims(standins) == []


# --- the container command that a frame run puts first on the path -----------------------------------------------

def shim(standins: Standins, batch, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, **batch.env()}
    return subprocess.run(["docker", *args], env=env, capture_output=True, text=True)


def test_the_container_command_of_a_frame_run_works_with_an_environment_of_its_own(standins):
    """The Gemini job gives agy a fixed list of names; the command finds its batch without them."""
    with frame.Batch(shim=True) as batch:
        env = {"PATH": batch.env().get("PATH", ""), "FRAME_STANDIN": os.environ["FRAME_STANDIN"]}
        made = subprocess.run(["docker", "create", "image"], env=env, capture_output=True, text=True)
        assert made.returncode == 0, made.stderr
        assert [c["labels"] for c in standins.containers()] == [{"dodam.frame.run": batch.run_id}]


# --- the state folder ----------------------------------------------------------------------------------------------

def test_a_state_folder_that_is_a_link_or_open_to_others_is_refused(standins, tmp_path, monkeypatch):
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(real)
    monkeypatch.setattr(frame, "ROOT", link)
    with pytest.raises(frame.FrameRefused) as refused:
        frame.Batch().open()
    assert refused.value.said["reason"] == "root_unsafe"
    opened = tmp_path / "opened"
    opened.mkdir()
    opened.chmod(0o777)
    monkeypatch.setattr(frame, "ROOT", opened)
    with pytest.raises(frame.FrameRefused) as refused:
        frame.Batch().open()
    assert refused.value.said["reason"] == "root_unsafe"


def test_the_state_folder_does_not_follow_the_temporary_folder_of_a_session():
    """Each session may have a temporary folder of its own; the lock is one for all of them."""
    assert frame.default_root() == Path("/tmp").resolve() / f"dodam-frame-{os.getuid()}"


# --- the tools that start containers go through it -----------------------------------------------------------------

def test_the_vetting_tool_starts_its_containers_through_the_frame(cli_task_dir, tmp_path, monkeypatch, standins):
    import dodambench.vet as vetting
    from dodambench.task import load_task
    vetting.vet_in_image(load_task(cli_task_dir), "python", tmp_path / "out", "dodam-bench/python:dev")
    created = [c for c in standins.calls() if c[:1] == ["create"]]
    assert created
    for cmd in created:
        assert cmd[1] == "--label" and cmd[2].startswith("dodam.frame.run="), cmd


# --- the lock: every file that starts a container goes through the frame or is exempt ------------------------------


def repository_files() -> list[str]:
    listed = subprocess.run(["git", "ls-files", "*.py"], cwd=REPO, capture_output=True, text=True, check=True)
    return [f for f in listed.stdout.split() if (REPO / f).is_file()]


def test_only_the_judges_tests_name_folders_of_the_container_machine_and_only_for_their_helper():
    """Folders of the container machine are given only where a caller names them in code, and the one caller
    outside the frame's own tests is the helper of the judge's container tests. The frame's command and its
    container command take no such option."""
    callers = []
    for f in repository_files():
        tree = ast.parse((REPO / f).read_text(encoding="utf-8"))
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and any(k.arg == "machine" for k in node.keywords):
                owner = node
                while owner in parents and not isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    owner = parents[owner]
                callers.append((f, getattr(owner, "name", "")))
    outside = sorted({c for c in callers if c[0] not in ("bench/tests/test_frame.py", "bench/tests/test_frame_table.py",
                                                           "bench/dodambench/frame.py")})
    assert outside == [("bench/tests/test_cli_isolation.py", "in_the_image")], outside
    source = (BENCH / "dodambench/frame.py").read_text(encoding="utf-8")
    assert "--machine" not in source
    assert frame.shim.__code__.co_varnames[:3] == ("folder", "mode", "args")
