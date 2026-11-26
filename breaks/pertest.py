"""A time limit of each test of its own, for the runner of the breaks: a test whose body still runs PER_TEST
seconds after it began fails with the words below, and the tests after it go on. The slowest test takes 69 s
with nothing broken."""
import signal

import pytest

PER_TEST = 150
WORDS = "a test that does not end: still running after"


class DoesNotEnd(Exception):
    pass


def _alarm(signum, frame):
    raise DoesNotEnd(f"{WORDS} {PER_TEST} s")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    old = signal.signal(signal.SIGALRM, _alarm)
    signal.alarm(PER_TEST)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)
