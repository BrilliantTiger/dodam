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
    yield made
    frame.close()


@pytest.fixture
def this_computer(standins, monkeypatch) -> dict:
    """The frame as it runs on this computer, whose readings the stand-ins give; free disk is set per test."""
    monkeypatch.setattr(frame, "PLATFORM", "darwin")
    disk = {"free": 100 * GIB}
    monkeypatch.setattr(frame, "free_disk", lambda path: disk["free"])
    return disk


def started(batch, *words: str) -> str:
    """Start a container through the stand-in, as a caller does: the frame's words, then the caller's run."""
    done = subprocess.run(batch.container(["docker", "create", "--name", "x", *words, "image", "true"]),
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
            batch.container(["docker", "create", "image"])
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
import sys, time
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from dodambench import frame
frame.ROOT = Path(sys.argv[2])
frame.PLATFORM = "linux"
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
    first = frame.container(["docker", "create", "image"])
    second = frame.container(["docker", "run", "--rm", "image"])
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
        cmd = frame.container(["docker", "create", "image"])
        Path({str(said)!r}).write_text(json.dumps(cmd))
    ''')
    held = holder(standins, frame.ROOT, sys.executable, "-c", child)
    out, err = held.communicate(timeout=60)
    assert held.returncode == 0, err
    cmd = json.loads(said.read_text())
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
            frame.container(["docker", "create", "image"])
        assert refused.value.said["reason"] == "lock_held"
    finally:
        os.killpg(held.pid, signal.SIGKILL)
        held.wait()
        end_programs(int(Path(f"{ready}.pid").read_text()))


# --- the mark, and collecting exactly one's own --------------------------------------------------------------------

@pytest.mark.parametrize("cmd, at", [(["docker", "create", "--name", "a", "image"], 2),
                                     (["docker", "run", "--rm", "image", "true"], 2),
                                     (["docker", "container", "run", "image"], 3),
                                     (["docker", "container", "create", "image"], 3)])
def test_every_container_is_started_with_the_batchs_mark_right_after_the_command(standins, cmd, at):
    batch = frame.Batch().open()
    try:
        given = batch.container(cmd)
        assert given[:at] == cmd[:at]
        assert given[at] == "--label" and given[at + 1] == f"dodam.frame.run={batch.run_id}"
        assert given[at + 2] == "--cidfile" and not Path(given[at + 3]).exists()
        assert given[at + 4:] == cmd[at:]
    finally:
        batch.close()


def test_the_end_collects_exactly_the_containers_it_started_by_their_id(standins):
    standins.add_container("f" * 64, {}, running=True)
    standins.add_container("e" * 64, {"dodam.frame.run": "someone-else"}, running=True)
    batch = frame.Batch().open()
    mine = [started(batch), started(batch)]
    batch.close()
    assert sorted(c["id"] for c in standins.containers()) == ["e" * 64, "f" * 64]
    removing = [call for call in standins.calls() if call[:1] == ["rm"]]
    assert removing and all(call[:2] == ["rm", "--force"] for call in removing)
    assert sorted(i for call in removing for i in call[2:]) == sorted(mine)
    assert not [call for call in standins.calls() if any("prune" in word or word == "rmi" for word in call)]


def test_a_container_whose_id_never_reached_its_file_is_found_by_the_exact_mark_of_the_run(standins):
    batch = frame.Batch().open()
    batch.container(["docker", "create", "image"])  # made, and killed before docker wrote its id
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
        subprocess.run(batch.volume("asked"), check=True, capture_output=True)
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

GROUP = "docker/" + "0123456789abcdef" * 4


def refused_reason(batch, cmd, allow=()) -> str | None:
    try:
        batch.container(cmd, allow=allow)
    except frame.FrameRefused as refused:
        return refused.said["reason"]
    return None


def test_the_sensitive_folders_of_this_computer_are_never_mounted(standins, tmp_path):
    home = Path.home()
    link = tmp_path / "link-to-home"
    link.symlink_to(home)
    short = Path(tempfile.mkdtemp(prefix="dodam-frame-sock-", dir="/tmp"))
    sock = short / "s.sock"
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(sock))
    try:
        refused_mounts(home, link, sock, tmp_path)
    finally:
        server.close()
        sock.unlink()
        short.rmdir()


def refused_mounts(home: Path, link: Path, sock: Path, tmp_path: Path) -> None:
    with frame.Batch() as batch:
        for cmd in (["docker", "run", "-v", f"{home}:/h", "image"],
                    ["docker", "run", "--volume", f"{home}/.dodam:/d", "image"],
                    ["docker", "run", f"--volume={home}/.dodam-desk:/d", "image"],
                    ["docker", "run", "--mount", f"type=bind,source={home}/.ssh,target=/s", "image"],
                    ["docker", "run", "-itv", f"{home}:/h", "image"],
                    ["docker", "run", "-v", f"{link}:/h", "image"],
                    ["docker", "run", "-v", "/:/root", "image"],
                    ["docker", "run", "-v", "/var/run/docker.sock:/var/run/docker.sock", "image"],
                    ["docker", "run", "-v", f"{sock}:/s", "image"],
                    ["docker", "run", "-v", f"{tmp_path}:/t", "image"]):
            assert refused_reason(batch, cmd) == "mount", cmd
        for cmd in (["docker", "run", "--env-file", f"{home}/.dodam/env", "image"],
                    ["docker", "run", "--label-file", "/etc/hosts", "image"],
                    ["docker", "run", "--device", "/dev/sda", "image"],
                    ["docker", "run", "--volumes-from", "other", "image"],
                    ["docker", "run", "--made-up-option", "x", "image"]):
            assert refused_reason(batch, cmd) == "option", cmd
        for cmd in (["docker", "-H", "tcp://elsewhere:2375", "run", "image"],
                    ["docker", "--host=unix:///x.sock", "run", "image"],
                    ["docker", "--context", "other", "create", "image"]):
            assert refused_reason(batch, cmd) == "daemon_option", cmd


def test_what_may_be_mounted_is_the_frames_own_folder_a_named_work_tree_and_named_volumes(standins, tmp_path):
    tree = tmp_path / "tree"
    (tree / ".git").mkdir(parents=True)
    with frame.Batch() as batch:
        assert refused_reason(batch, ["docker", "run", "-v", f"{batch.tmp}:/t", "image"]) is None
        assert refused_reason(batch, ["docker", "run", "-v", f"{tree}:/w:ro", "image"], allow=[tree]) is None
        assert refused_reason(batch, ["docker", "run", "-v", "asked:/reading", "image"]) is None
        assert refused_reason(batch, ["docker", "run", "--tmpfs", "/dodam-cases:rw,size=32m", "image"]) is None
        # a work tree is a folder with its history in it, and never the home folder or one above it
        assert refused_reason(batch, ["docker", "run", "-v", f"{tmp_path}:/t", "image"], allow=[tmp_path]) == "mount"
        assert refused_reason(batch, ["docker", "run", "-v", f"{Path.home()}:/t", "image"], allow=[Path.home()]) == "mount"


def test_folders_of_the_container_machine_are_given_only_as_the_caller_names_them_and_only_to_a_closed_container(
        standins):
    """The judge's tests give their helper two folders of the container machine. The frame names neither: the
    caller names each exactly, and only a container without network and without a folder of this computer gets
    them."""
    group = f"/sys/fs/cgroup/{GROUP}"
    scoped = "/sys/fs/cgroup/system.slice/docker-" + "ab" * 32 + ".scope"
    memory = "/proc/sys/vm/" + "drop" + "_caches"
    helper = ["docker", "run", "--detach", "--name", "j-reader", "--network", "none", "--user", "0",
              "--memory", "256m", "--pids-limit", "64", "--volume", f"{group}:/group",
              "--volume", f"{memory}:/drop", "--volume", "j-asked:/reading", "image", "python3"]
    with frame.Batch() as batch:
        def said(cmd, machine):
            try:
                batch.container(cmd, machine=machine)
            except frame.FrameRefused as refused:
                return refused.said["reason"]
            return None
        assert said(helper, (group, memory)) is None
        assert said([w.replace(group, scoped) for w in helper], (scoped, memory)) is None
        assert said(helper, ()) == "mount"
        assert said(helper, (group,)) == "mount"
        with_network = [w for w in helper if w not in ("--network", "none")]
        assert said(with_network, (group, memory)) == "mount"
        with_bind = helper[:2] + ["-v", f"{batch.tmp}:/t"] + helper[2:]
        assert said(with_bind, (group, memory)) == "mount"
        for path in ("/sys/fs/cgroup", "/sys/fs/cgroup/docker/../..", f"{group}/../x", "/proc/sys/vm",
                     "/sys/fs/cgroup/docker/1234", "/proc/self/root", "/proc/sys/vm/../../self"):
            assert said(["docker", "run", "--network", "none", "-v", f"{path}:/g", "image"], (path,)) == "mount", path


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


def test_the_container_command_of_a_frame_run_marks_starts_and_refuses_pruning_and_others_containers(standins):
    standins.add_container("d" * 64, {})
    with frame.Batch(shim=True) as batch:
        made = shim(standins, batch, "run", "--detach", "image")
        assert made.returncode == 0, made.stderr
        ident = made.stdout.strip()
        assert [c["labels"] for c in standins.containers() if c["id"] == ident] == [{"dodam.frame.run": batch.run_id}]
        for words in (["system", "prune", "-f"], ["container", "prune"], ["image", "prune", "-a"], ["volume", "prune"],
                      ["rmi", "image"], ["image", "rm", "image"], ["rm", "--force", "d" * 64], ["stop", "d" * 64],
                      ["run", "-v", f"{Path.home()}:/h", "image"]):
            refused = shim(standins, batch, *words)
            assert refused.returncode == 2, words
            assert json.loads(refused.stderr)["refused"], words
        assert shim(standins, batch, "rm", "--force", ident).returncode == 0
    assert [c["id"] for c in standins.containers()] == ["d" * 64]
    assert not [call for call in standins.calls() if "prune" in call or "rmi" in call]


def test_a_command_framed_by_the_code_inside_a_frame_run_passes_its_container_command_once(standins):
    """Code inside a frame run frames its command, and the frame run's container command then sees it: the mark
    and the file for the id are given once, and its mounts are still checked."""
    with frame.Batch(shim=True) as batch:
        joined = frame.Joined(batch.folder, {})
        made = shim(standins, batch, *joined.container(["docker", "run", "--detach", "image"])[1:])
        assert made.returncode == 0, made.stderr
        [call] = [c for c in standins.calls() if c[:1] == ["run"]]
        assert call.count("--label") == 1 and call.count("--cidfile") == 1, call
        framed = joined.container(["docker", "run", "image"])
        framed[framed.index("image"):framed.index("image")] = ["-v", f"{Path.home()}:/h"]
        assert shim(standins, batch, *framed[1:]).returncode == 2


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
    created = []
    real = subprocess.run

    def run(cmd, *args, **kwargs):
        if cmd[:2] == ["docker", "create"]:
            created.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[:2] == ["docker", "info"]:
            return subprocess.CompletedProcess(cmd, 0, "4294967296\n", "")
        if cmd[0] == "docker":
            return subprocess.CompletedProcess(cmd, 3 if cmd[1] == "start" else 0, "", "")
        return real(cmd, *args, **kwargs)
    monkeypatch.setattr(vetting.subprocess, "run", run)
    vetting.vet_in_image(load_task(cli_task_dir), "python", tmp_path / "out", "dodam-bench/python:dev")
    assert created
    for cmd in created:
        assert cmd[2] == "--label" and cmd[3].startswith("dodam.frame.run="), cmd


# --- the lock: every file that starts a container goes through the frame or is exempt ------------------------------

STARTS = {"run", "create"}
# A container is made by `docker run` and `docker create`; `docker start` starts one that was made, and is not
# counted. Harbor and agy start containers of their own: the files that run them hold a batch of the frame.
OTHER_PROGRAMS = {
    "bench/dodambench/pilot.py": "Harbor starts the trial's containers with docker compose, which takes no mark "
                                 "from the command line; the pilot runs Harbor inside a batch of the frame (the "
                                 "lock, the readings, the temporary folders) and removes the containers of its own "
                                 "trials by their compose projects, as before",
    "tools/gemini/job.py": "agy starts the attack run's containers; the job runs agy inside a batch of the frame "
                           "whose container command is first on agy's path",
}
EXEMPT = {
    ("bench/tests/test_pilot.py", "test_a_group_already_confirmed_gone_is_not_stopped_again"):
        "the Harbor it runs is a stand-in that starts no container",
    ("bench/tests/test_frame.py", "*"):
        "the frame's own tests: every command they name goes through the frame to the stand-in container command",
}
# Calls of a function that builds a container command, made only to read the command it gives.
INSPECTING = {
    ("bench/tests/test_vet.py", "test_image_judge_runs_the_verifier_judge_without_network"):
        "reads the vetting tool's command word by word and starts nothing",
    ("bench/tests/test_cli_convert.py", "test_a_web_task_converts_as_before_and_is_vetted_in_a_capped_container"):
        "reads the caps of the vetting tool's command and starts nothing",
    ("bench/tests/test_cli_convert.py", "test_a_command_line_task_carries_its_caps_onto_the_path_the_measurement_uses"):
        "reads the caps of the vetting tool's command and starts nothing",
}


def _calls_frame_container(node: ast.AST, parents: dict) -> bool:
    while node in parents:
        node = parents[node]
        if isinstance(node, ast.Call):
            func = node.func
            if (isinstance(func, ast.Attribute) and func.attr == "container") or (
                    isinstance(func, ast.Name) and func.id == "container"):
                return True
        if isinstance(node, ast.Compare):
            return True  # a command compared word for word starts nothing
    return False


def _in_compare(node: ast.AST, parents: dict) -> bool:
    while node in parents:
        node = parents[node]
        if isinstance(node, ast.Compare):
            return True
    return False


def _function_of(node: ast.AST, parents: dict) -> str:
    """The function a place is in, or the name it is given at the top of its file."""
    while node in parents:
        node = parents[node]
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node.name
        if isinstance(node, ast.Assign) and isinstance(parents.get(node), ast.Module):
            return ast.unparse(node.targets[0])
    return ""


def _word(node: ast.AST) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def container_starts(root: Path, files: list[str]) -> dict:
    """Each place in the files that makes a container: where it is, and whether it goes through the frame.
    A command built in a function goes through the frame where every call of that function, in any of the
    files, is inside a call of the frame (or compares the command word for word)."""
    trees = {f: ast.parse((root / f).read_text(encoding="utf-8")) for f in files}
    found, built = [], {}
    for f, tree in trees.items():
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        for node in ast.walk(tree):
            words = None
            if isinstance(node, (ast.List, ast.Tuple)) and len(node.elts) >= 2:
                e = node.elts
                first = ast.unparse(e[0])
                if _word(e[0]) == "docker" and (_word(e[1]) in STARTS or (
                        _word(e[1]) == "container" and len(e) > 2 and _word(e[2]) in STARTS)):
                    words = "docker"
                elif "harbor" in first.lower() and _word(e[1]) == "run":
                    words = "harbor"
            elif isinstance(node, ast.Constant) and isinstance(node.value, str) and not isinstance(
                    parents.get(node), ast.Expr) and re.search(
                    r"\bdocker\s+(container\s+)?(run|create)\b", node.value):
                words = "shell"
            if words is None:
                continue
            place = {"file": f, "line": node.lineno, "function": _function_of(node, parents), "kind": words,
                     "compared": _in_compare(node, parents), "framed": _calls_frame_container(node, parents)}
            found.append(place)
            if not place["framed"] and place["function"]:
                built.setdefault(place["function"], []).append(place)
    for f, tree in trees.items():
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        calls: dict[str, list[bool]] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
                if name in built:
                    calls.setdefault(name, []).append(_calls_frame_container(node, parents) or (
                        f, _function_of(node, parents)) in INSPECTING)
        for name, framed in calls.items():
            for place in built[name]:
                place.setdefault("callers", []).extend(framed)
    for place in found:
        callers = place.pop("callers", [])
        if not place["framed"] and callers and all(callers):
            place["framed"] = True
    return {"places": found}


def repository_files() -> list[str]:
    listed = subprocess.run(["git", "ls-files", "*.py"], cwd=REPO, capture_output=True, text=True, check=True)
    return [f for f in listed.stdout.split() if (REPO / f).is_file()]


def test_every_file_that_starts_a_container_goes_through_the_frame_or_is_exempt_with_its_reason():
    places = container_starts(REPO, repository_files())["places"]
    loose = [p for p in places if not p["framed"] and not p["compared"]
             and (p["file"], p["function"]) not in EXEMPT and (p["file"], "*") not in EXEMPT
             and not (p["kind"] == "harbor" and p["file"] in OTHER_PROGRAMS)]
    assert loose == [], "these start a container outside the frame: " + json.dumps(loose, indent=1)
    for f, reason in OTHER_PROGRAMS.items():
        tree = ast.parse((REPO / f).read_text(encoding="utf-8"))
        holds = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Attribute) and node.func.attr == "batch"
                 and isinstance(node.func.value, ast.Name) and node.func.value.id == "frame"]
        assert holds, f"{f} runs another program that starts containers outside a batch of the frame ({reason})"


def test_the_files_that_start_containers_and_those_that_only_name_the_command_are_these():
    """Counted on main at f786d05, thirteen files started containers or named the container command. Of those,
    three name the command only to compare it word for word (the converter's, the judge's helper's and the
    vetting tool's), and two name it only in a stand-in or a path, and are not found here at all (the checks on
    Harbor's patch and on the Gemini job). The frame's own tests and the pilot's stand-in Harbor are added."""
    places = container_starts(REPO, repository_files())["places"]
    starting = sorted({p["file"] for p in places if not p["compared"]} | set(OTHER_PROGRAMS))
    naming = sorted({p["file"] for p in places if p["compared"]} - set(starting))
    assert starting == ["bench/dodambench/pilot.py", "bench/dodambench/vet.py", "bench/experiments/cli_separation.py",
                        "bench/experiments/memory_overshoot.py", "bench/experiments/removal_time.py",
                        "bench/tests/test_cli_isolation.py", "bench/tests/test_cli_runtimes.py",
                        "bench/tests/test_frame.py", "bench/tests/test_pilot.py", "tools/gemini/job.py"]
    assert naming == ["bench/tests/test_cli_convert.py", "bench/tests/test_cli_judge.py", "bench/tests/test_vet.py"]


def test_the_lock_finds_a_container_started_outside_the_frame(tmp_path):
    (tmp_path / "a.py").write_text(textwrap.dedent('''
        import subprocess
        def made(name):
            return ["docker", "create", "--name", name, "image"]
        def judged():
            subprocess.run(made("x"))
            subprocess.run(["docker", "container", "run", "image"])
            subprocess.run("docker run image", shell=True)
            subprocess.run(frame.container(["docker", "run", "image"]))
    '''))
    places = container_starts(tmp_path, ["a.py"])["places"]
    assert sorted((p["line"], p["framed"]) for p in places) == [(4, False), (7, False), (8, False), (9, True)]


def test_a_command_built_in_a_function_goes_through_the_frame_only_where_every_call_of_it_does(tmp_path):
    (tmp_path / "a.py").write_text(textwrap.dedent('''
        def made(name):
            return ["docker", "create", "--name", name, "image"]
        def judged():
            subprocess.run(frame.container(made("x")))
    '''))
    assert [p["framed"] for p in container_starts(tmp_path, ["a.py"])["places"]] == [True]
    (tmp_path / "b.py").write_text("subprocess.run(made('y'))\n")
    assert [p["framed"] for p in container_starts(tmp_path, ["a.py", "b.py"])["places"]] == [False]


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
                callers.append((f, _function_of(node, parents)))
    outside = sorted({c for c in callers if c[0] not in ("bench/tests/test_frame.py", "bench/dodambench/frame.py")})
    assert outside == [("bench/tests/test_cli_isolation.py", "in_the_image")], outside
    source = (BENCH / "dodambench/frame.py").read_text(encoding="utf-8")
    assert "--machine" not in source
    assert frame.shim.__code__.co_varnames[:3] == ("folder", "real", "args")
