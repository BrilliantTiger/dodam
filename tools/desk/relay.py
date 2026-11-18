"""Stands between the terminal and the desk's session, and does the desk's waiting.

The desk's session is Claude Code without its terminal interface: it takes messages as data on
its input and reports what it does as data on its output. The relay is the program in the
terminal. It does three things and nothing else.

- It shows, in the terminal, what the session says and which tools it calls, so that a person
  can watch.
- It passes every line typed into the terminal to the session as a message, as plain text.
  Nothing typed is ever treated as a command: no line runs a program, whatever it starts with.
- It runs the watcher, one at a time, with the state path the desk was started with and no
  other switch. When the watcher reports something new, the relay tells the session, passing
  only a kind and an item's number, and starts the watcher again once the session has dealt
  with it. While nothing arrives the session rests and uses nothing.

The session itself has no part in the waiting, so there is nothing for it to start twice, to
start with a switch of its own, or to forget.
"""

from __future__ import annotations

import fcntl
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import facts as facts_file  # noqa: E402

WATCH_STATE_FILE = "watch-state.json"
MAX_NUMBER = 10_000_000
LOGIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}(?:\[bot\])?$")
EVENT_ID = re.compile(r"^[A-Za-z0-9_.:/#-]{1,120}$")
EVENT_KIND = re.compile(r"^[a-z_]{1,40}$")
EVENT_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

NOT_SIGNED_IN = (
    "From the desk's relay: the watcher says that GitHub is not signed in. Watching has stopped. "
    "Tell the coordinator, with the kind trouble, and do nothing else."
)
WATCHER_FAILED = (
    "From the desk's relay: the watcher stopped without news. It is not started again. Watching has stopped. "
    "Tell the coordinator, with the kind trouble, and do nothing else."
)


def address_only(event) -> dict | None:
    """Only an address and a kind pass, each in its expected shape. No outside text does."""
    if not isinstance(event, dict):
        return None
    number = event.get("number")
    if isinstance(number, bool) or not isinstance(number, int) or not 1 <= number <= MAX_NUMBER:
        return None
    kept: dict = {"number": number}
    for key, shape in (("id", EVENT_ID), ("kind", EVENT_KIND), ("actor", LOGIN), ("time", EVENT_TIME)):
        value = event.get(key)
        if isinstance(value, str) and shape.fullmatch(value):
            kept[key] = value
    return kept if "kind" in kept else None


def watcher_command(copy: Path, state: Path) -> list[str]:
    """The one way the watcher is ever started."""
    return [sys.executable, "-I", str(copy / "tools" / "desk" / "watch.py"), "--wait", "--state", str(state / WATCH_STATE_FILE)]


def read_events(code: int, output: bytes) -> list[dict] | None:
    """The events of a watcher that ended well, or None when its output cannot be trusted."""
    if code != 0:
        return None
    try:
        events = json.loads(output.decode("utf-8", "replace"))["events"]
    except (ValueError, KeyError, TypeError):
        return None
    if not isinstance(events, list):
        return None
    return [kept for kept in map(address_only, events) if kept]


def news_message(events: list[dict]) -> str:
    return (
        "From the desk's relay: something new arrived on GitHub. Each line below is one arrival, with its kind and the "
        "number of the item. Read each item with read_item and handle it as your standing instructions say.\n"
        + "\n".join(json.dumps(event, ensure_ascii=False) for event in events)
    )


class Relay:
    def __init__(self, plan: dict, watcher_env: dict[str, str], *, typed=None, shown=None, say: str | None = None,
                 exit_when_idle: bool = False, retry_seconds: float = 60.0, quiet_seconds: float = 3.0):  # fmt: skip
        self.plan = plan
        self.copy = Path(plan["cwd"])
        self.state = Path(plan["state"])
        self.facts_path = self.copy / "tools" / "desk" / "desk.toml"
        self.watcher_env = watcher_env
        self.typed = sys.stdin if typed is None else typed
        self.shown = sys.stdout if shown is None else shown
        self.first = say
        self.exit_when_idle = exit_when_idle
        self.retry_seconds = retry_seconds
        self.quiet_seconds = quiet_seconds
        self.live_wait = plan["live_wait"]
        self.outgoing: queue.Queue = queue.Queue()
        self.idle = threading.Event()
        self.stopping = threading.Event()
        self.waiting_calls: set[str] = set()
        self.session: subprocess.Popen | None = None
        self.watcher: subprocess.Popen | None = None
        self.guard = threading.Lock()
        self.last_sent = 0.0
        self.results = 0

    # -- the terminal -------------------------------------------------------

    def show(self, text: str) -> None:
        try:
            self.shown.write(text.rstrip("\n") + "\n")
            self.shown.flush()
        except (OSError, ValueError):
            pass

    def read_typed(self) -> None:
        """Every typed line becomes part of a message. Lines that come together are one message."""
        pending: list[str] = []

        def flush() -> None:
            text = "\n".join(pending).strip()
            pending.clear()
            if text:
                self.outgoing.put(("typed", text))

        timer: threading.Timer | None = None
        for line in self.typed:
            if timer is not None:
                timer.cancel()
            pending.append(line.rstrip("\n"))
            timer = threading.Timer(0.3, flush)
            timer.daemon = True
            timer.start()
        if timer is not None:
            timer.cancel()
        flush()
        self.outgoing.put(("end of typing", ""))

    # -- the session --------------------------------------------------------

    def write(self, message: dict) -> None:
        with self.guard:
            if self.session is None or self.session.poll() is not None:
                return
            try:
                self.session.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
                self.session.stdin.flush()
            except (OSError, ValueError):
                pass

    def send(self, text: str) -> None:
        if self.waiting_calls:
            # The session is inside a long wait for a live session. End that wait so that it hears this now.
            self.write({"type": "control_request", "request_id": f"relay-{time.monotonic_ns()}", "request": {"subtype": "interrupt"}})
            self.idle.wait(timeout=15)
        self.idle.clear()
        self.last_sent = time.monotonic()
        # Always a message with plain text in it. Nothing here is a command to Claude Code or to a shell.
        self.write({"type": "user", "message": {"role": "user", "content": text}})

    def read_session(self, log) -> None:
        for line in self.session.stdout:
            if log is not None:
                log.write(line)
                log.flush()
            try:
                event = json.loads(line)
            except ValueError:
                continue
            kind = event.get("type")
            content = event.get("message", {}).get("content") if isinstance(event.get("message"), dict) else None
            if kind in ("assistant", "user") and isinstance(content, list):
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    if part.get("type") == "text" and kind == "assistant":
                        self.show("desk: " + str(part.get("text", "")))
                    elif part.get("type") == "tool_use":
                        self.show("desk calls " + str(part.get("name")))
                        if part.get("name") == self.live_wait:
                            self.waiting_calls.add(part.get("id"))
                    elif part.get("type") == "tool_result":
                        self.waiting_calls.discard(part.get("tool_use_id"))
                        if part.get("is_error"):
                            self.show("  refused: " + json.dumps(part.get("content"), ensure_ascii=False)[:300])
            elif kind == "result":
                self.waiting_calls.clear()
                self.results += 1
                self.idle.set()
        self.stopping.set()
        self.idle.set()
        self.outgoing.put(("session ended", ""))

    # -- the watcher --------------------------------------------------------

    def github_open(self) -> bool:
        try:
            return facts_file.load(self.facts_path).github_open
        except facts_file.FactsProblem:
            return False

    def pause(self, seconds: float) -> None:
        self.stopping.wait(timeout=seconds)

    def watch(self) -> None:
        while not self.stopping.is_set():
            # The facts are read before every wait: closing the way in the file stops the watching.
            if not self.github_open():
                self.pause(self.retry_seconds)
                continue
            self.idle.wait()
            if self.stopping.is_set():
                return
            try:
                with self.guard:
                    self.watcher = subprocess.Popen(
                        watcher_command(self.copy, self.state), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL, env=self.watcher_env, cwd=str(self.copy),
                    )  # fmt: skip
                output, _ = self.watcher.communicate()
                code = self.watcher.returncode
            except OSError:
                code, output = 127, b""
            if self.stopping.is_set():
                return
            if code == 2:
                self.show("relay: the watcher says GitHub is not signed in; watching has stopped")
                self.outgoing.put(("relay", NOT_SIGNED_IN))
                return
            events = read_events(code, output)
            if events is None:
                # Every way of ending other than with news means: do not start it again.
                self.show(f"relay: the watcher ended with code {code}; watching has stopped")
                self.outgoing.put(("relay", WATCHER_FAILED))
                return
            if events:
                self.show(f"relay: {len(events)} new on GitHub")
                self.idle.clear()
                self.outgoing.put(("news", news_message(events)))
                # Not again until the session has taken this in and finished with it.
                while not self.stopping.is_set() and not self.idle.wait(timeout=1):
                    pass

    # -- everything together ------------------------------------------------

    def run(self) -> int:
        self.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        with open(self.state / "desk.lock", "w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                self.show("The desk was not started. Another desk is already running on this state folder.")
                return 1
            log_path = self.state / "session" / "stream.jsonl"
            if log_path.exists():
                os.replace(log_path, log_path.with_suffix(".jsonl.1"))
            descriptor = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as log:
                self.session = subprocess.Popen(
                    self.plan["argv"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    text=True, encoding="utf-8", errors="replace", env=self.plan["env"], cwd=self.plan["cwd"],
                )  # fmt: skip
                self.show("relay: the desk's session is running, locked. Lines typed here are passed to it as messages.")
                self.idle.set()
                threads = [threading.Thread(target=self.read_session, args=(log,), daemon=True)]
                if self.first:
                    self.send(self.first)
                threads.append(threading.Thread(target=self.read_typed, daemon=True))
                threads.append(threading.Thread(target=self.watch, daemon=True))
                for thread in threads:
                    thread.start()
                try:
                    self.loop()
                except KeyboardInterrupt:
                    pass
                finally:
                    self.stop()
                    threads[0].join(timeout=10)
            return self.session.returncode if self.session.returncode is not None else 0

    def loop(self) -> None:
        typing_ended = False
        while True:
            try:
                kind, text = self.outgoing.get(timeout=min(1.0, self.quiet_seconds))
            except queue.Empty:
                quiet = self.idle.is_set() and time.monotonic() - self.last_sent > self.quiet_seconds and self.outgoing.empty()
                if self.exit_when_idle and typing_ended and quiet and self.results > 0:
                    return
                continue
            if kind == "session ended":
                return
            if kind == "end of typing":
                typing_ended = True
                continue
            if kind == "typed":
                self.show("typed: " + text[:200])
            self.send(text)

    def stop(self) -> None:
        self.stopping.set()
        self.idle.set()
        with self.guard:
            for process in (self.watcher, self.session):
                if process is not None and process.poll() is None:
                    process.terminate()
        if self.session is not None:
            try:
                self.session.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.session.kill()
