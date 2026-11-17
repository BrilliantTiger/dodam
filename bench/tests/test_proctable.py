"""The host process table, watched for the token while Harbor runs.

The token below is a made-up string. Some tests start short-lived Python children of the test process.
"""
import json
import os
import subprocess
import sys
import threading
import time
import uuid

import pytest

try:
    from dodambench import proctable
except ImportError:  # each test then fails on its own instead of stopping the whole run
    proctable = None

FAKE_TOKEN = "sk-ant-oat01-FAKE0123456789abcdefghijklmnopqrstuvwxyzPROC"
CLEAN = b"    1 /sbin/launchd\n  202 /bin/zsh -l\n"
LEAKY = CLEAN + f"  303 docker compose exec -e CLAUDE_CODE_OAUTH_TOKEN={FAKE_TOKEN} main bash -c x\n".encode()


class Sampler:
    """Clean tables until call number `hit_from` (1-based), then a table holding the token."""

    def __init__(self, hit_from=None, error=None):
        self.calls, self.hit_from, self.error, self.lock = 0, hit_from, error, threading.Lock()

    def __call__(self) -> bytes:
        with self.lock:
            self.calls += 1
            if self.error:
                raise self.error
            return LEAKY if self.hit_from is not None and self.calls >= self.hit_from else CLEAN


class Stop:
    def __init__(self):
        self.calls = 0

    def __call__(self, proc=None):
        self.calls += 1
        return [303]


def wait_for(cond, seconds=10.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


def test_the_processes_whose_command_line_holds_the_token_are_found():
    table = CLEAN + f"  101 /usr/local/bin/docker compose exec -e T={FAKE_TOKEN} main\n".encode() \
        + f"103 docker-compose compose exec -e T={FAKE_TOKEN} main\n".encode()
    assert proctable.token_pids(table, FAKE_TOKEN) == [101, 103]
    assert proctable.token_pids(CLEAN, FAKE_TOKEN) == []


def test_the_table_is_read_with_the_systems_ps_not_one_found_on_path(tmp_path, monkeypatch):
    fake = tmp_path / "ps"
    fake.write_text("#!/bin/sh\necho '    1 a made-up table'\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    pids = [line.split()[0] for line in proctable.process_table().splitlines() if line.split()]
    assert str(os.getpid()).encode() in pids


def test_the_real_process_table_shows_the_command_lines_of_this_users_processes():
    marker = "dodam-proctable-marker-" + uuid.uuid4().hex
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", marker])
    try:
        assert wait_for(lambda: proctable.token_pids(proctable.process_table(), marker) == [child.pid])
    finally:
        child.kill()
        child.wait()


def test_a_watch_samples_before_during_and_after_the_run():
    sampler, stop = Sampler(), Stop()
    with proctable.ProcessTableWatch(FAKE_TOKEN, sampler, interval=0.01, stop=stop) as watch:
        assert watch.clean and sampler.calls >= 1  # one sample before Harbor starts
        assert wait_for(lambda: sampler.calls >= 4)
    record = watch.record()
    assert record["samples"] == sampler.calls >= 5
    assert record["clean"] and record["hit_samples"] == 0 and record["hit_pids"] == [] and record["error"] is None
    assert not record["stopped_harbor"] and stop.calls == 0
    assert record["interval_seconds"] == 0.01


def test_a_watch_given_no_harbor_process_never_claims_to_have_stopped_one():
    sampler, stop = Sampler(hit_from=3), Stop()
    with proctable.ProcessTableWatch(FAKE_TOKEN, sampler, interval=0.01, stop=stop) as watch:
        assert wait_for(lambda: sampler.calls >= 3)  # the third reading holds the token
    record = watch.record()
    assert not watch.clean and not record["clean"]
    assert record["hit_samples"] >= 1 and record["hit_pids"] == [303]
    assert record["stopped_harbor"] is False  # nothing was handed to it, so nothing was stopped
    assert stop.calls == 0
    assert FAKE_TOKEN not in json.dumps(record)


def test_the_token_seen_before_the_start_marks_the_watch_unclean_at_once():
    sampler, stop = Sampler(hit_from=1), Stop()
    with proctable.ProcessTableWatch(FAKE_TOKEN, sampler, interval=0.01, stop=stop) as watch:
        assert not watch.clean


def test_a_sampler_that_fails_fails_closed_and_its_error_never_shows_the_token():
    sampler, stop = Sampler(error=OSError(f"ps broke near {FAKE_TOKEN}")), Stop()
    with proctable.ProcessTableWatch(FAKE_TOKEN, sampler, interval=0.01, stop=stop) as watch:
        assert not watch.clean
    record = watch.record()
    assert not record["clean"] and "OSError" in record["error"] and FAKE_TOKEN not in json.dumps(record)


def test_samples_are_counted_inside_time_windows():
    times = iter([10.0, 20.0, 30.0, 40.0, 50.0])
    watch = proctable.ProcessTableWatch(FAKE_TOKEN, Sampler(), interval=60, stop=Stop(), clock=lambda: next(times))
    for _ in range(4):
        watch.sample()
    assert watch.count_within([(15.0, 35.0)]) == 2
    assert watch.count_within([(0.0, 12.0), (39.0, 41.0)]) == 2
    assert watch.count_within([]) == 0
