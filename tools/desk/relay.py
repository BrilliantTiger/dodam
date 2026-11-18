"""Stands between the terminal and the desk's sessions, and does the desk's waiting.

A session of the desk is Claude Code without its terminal interface: it takes a message as
data on its input and reports what it does as data on its output. The relay is the program in
the terminal. It does three things and nothing else.

- It runs the watcher, one at a time, with the state path the desk was started with and no
  other switch. A watcher that ends any other way than with news is not started again.
- For every item the watcher reports, it starts a fresh session, tells it the kind of each
  arrival and the number of that one item, lets it finish, and ends it. What one outsider wrote
  is therefore never in the conversation in which another outsider's item is handled, and no
  conversation grows. The tools of such a session refuse every other item. Several arrivals on one item go to one session together; several items
  are handled one after the other, each in a session of its own. What has to last between
  sessions (the counts and limits) is kept by the tools, and GitHub is the memory of a thread.
- It passes what is typed into the terminal to a fresh session as a message, as plain text.
  Nothing typed is ever treated as a command: no line runs a program, whatever it starts with.
  It shows, in the terminal, what each session says and which tools it calls.
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


def by_item(events: list[dict]) -> list[list[dict]]:
    """The arrivals of each item together, items in the order of their first arrival."""
    groups: dict[int, list[dict]] = {}
    for event in events:
        groups.setdefault(event["number"], []).append(event)
    return list(groups.values())


def news_message(events: list[dict]) -> str:
    number = events[0]["number"]
    return (
        f"From the desk's relay: something new arrived on GitHub on item {number}. Each line below is one arrival, with its "
        "kind. Read the item with read_item and handle it as your standing instructions say. Handle no other item.\n"
        + "\n".join(json.dumps(event, ensure_ascii=False) for event in events)
    )


class Relay:
    def __init__(self, plan: dict, watcher_env: dict[str, str], *, typed=None, shown=None, say: str | None = None,
                 exit_when_idle: bool = False, retry_seconds: float = 60.0, quiet_seconds: float = 3.0,
                 session_seconds: float = 1800.0):  # fmt: skip
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
        self.session_seconds = session_seconds
        self.jobs: queue.Queue = queue.Queue()
        self.stopping = threading.Event()
        self.typing_ended = threading.Event()
        self.session: subprocess.Popen | None = None
        self.watcher: subprocess.Popen | None = None
        self.guard = threading.Lock()
        self.handled = 0

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
                self.jobs.put(("typed", text))

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
        self.typing_ended.set()

    # -- one session for one message ----------------------------------------

    def handle(self, about: str, text: str, log) -> None:
        """Start a fresh session, give it this one message, show what it does, and end it."""
        env = dict(self.plan["env"])
        if about.startswith("item "):
            # The tools of this session refuse every other item.
            env["DESK_ITEM"] = about.removeprefix("item ")
        self.handled += 1
        log.write(json.dumps({"type": "relay", "session": self.handled, "about": about}) + "\n")
        log.flush()
        self.show(f"relay: session {self.handled} ({about})")
        try:
            with self.guard:
                if self.stopping.is_set():
                    return
                self.session = subprocess.Popen(
                    self.plan["argv"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    text=True, encoding="utf-8", errors="replace", env=env, cwd=self.plan["cwd"],
                )  # fmt: skip
        except OSError:
            self.show("relay: the session could not be started")
            return
        session = self.session
        # A session that does not finish is ended, so that one item cannot hold up the desk.
        timer = threading.Timer(self.session_seconds, session.terminate)
        timer.daemon = True
        timer.start()
        try:
            # Always a message with plain text in it. Nothing here is a command to Claude Code or to a shell.
            session.stdin.write(json.dumps({"type": "user", "message": {"role": "user", "content": text}}, ensure_ascii=False) + "\n")
            session.stdin.flush()
            for line in session.stdout:
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
                        elif part.get("type") == "tool_result" and part.get("is_error"):
                            self.show("  refused: " + json.dumps(part.get("content"), ensure_ascii=False)[:300])
                elif kind == "result":
                    break
        except (OSError, ValueError):
            pass
        finally:
            timer.cancel()
            try:
                session.stdin.close()
            except (OSError, ValueError):
                pass
            try:
                session.wait(timeout=10)
            except subprocess.TimeoutExpired:
                session.kill()
                session.wait()
            with self.guard:
                self.session = None

    # -- the watcher --------------------------------------------------------

    def github_open(self) -> bool:
        try:
            return facts_file.load(self.facts_path).github_open
        except facts_file.FactsProblem:
            return False

    def watch(self) -> None:
        while not self.stopping.is_set():
            # The facts are read before every wait: closing the way in the file stops the watching.
            if not self.github_open():
                self.stopping.wait(timeout=self.retry_seconds)
                continue
            try:
                with self.guard:
                    if self.stopping.is_set():
                        return
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
                self.jobs.put(("the watcher", NOT_SIGNED_IN))
                return
            events = read_events(code, output)
            if events is None:
                # Every way of ending other than with news means: do not start it again.
                self.show(f"relay: the watcher ended with code {code}; watching has stopped")
                self.jobs.put(("the watcher", WATCHER_FAILED))
                return
            if events:
                groups = by_item(events)
                self.show(f"relay: news on {len(groups)} item(s) on GitHub")
                for group in groups:
                    self.jobs.put((f"item {group[0]['number']}", news_message(group)))
                # Not again until every one of them has been dealt with.
                self.jobs.join()

    # -- everything together ------------------------------------------------

    def run(self) -> int:
        self.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        (self.state / "session").mkdir(mode=0o700, exist_ok=True)
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
                self.show("relay: the desk is running, locked. What is typed here is passed to a session as a message.")
                if self.first:
                    self.jobs.put(("first message", self.first))
                for target in (self.read_typed, self.watch):
                    threading.Thread(target=target, daemon=True).start()
                try:
                    while not self.stopping.is_set():
                        try:
                            about, text = self.jobs.get(timeout=min(1.0, self.quiet_seconds))
                        except queue.Empty:
                            if self.exit_when_idle and self.typing_ended.is_set() and self.handled > 0:
                                break
                            continue
                        if about == "typed":
                            self.show("typed: " + text[:200])
                        try:
                            self.handle(about, text, log)
                        finally:
                            self.jobs.task_done()
                except KeyboardInterrupt:
                    pass
                finally:
                    self.stop()
        return 0

    def stop(self) -> None:
        self.stopping.set()
        with self.guard:
            for process in (self.watcher, self.session):
                if process is not None and process.poll() is None:
                    process.terminate()
