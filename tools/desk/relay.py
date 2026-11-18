"""Stands between the terminal and the desk's sessions, and does the desk's waiting.

A session of the desk is Claude Code without its terminal interface: it takes a message as
data on its input and reports what it does as data on its output. The relay is the program in
the terminal. It does three things and nothing else.

- It runs the watcher, one at a time, with the state path the desk was started with and no
  other switch. A watcher that ends any other way than cleanly is not started again, and the
  relay itself tells the coordinator.
- It keeps what a day may cost within hard bounds: turns, reads, tokens and time for one session;
  sessions for one item and for all items in a day; tokens in a day. When the day's bounds are
  reached nothing new is started; arrivals are kept for later up to a bound and the rest are
  counted and dropped; the coordinator is told once.
- For every item the watcher reports, it starts a fresh session, tells it the kind of each
  arrival and the number of that one item, lets it finish, and ends it. What one outsider wrote
  is therefore never in the conversation in which another outsider's item is handled, and no
  conversation grows. The tools of such a session refuse every other item. Several arrivals on one item go to one session together; several items
  are handled one after the other, each in a session of its own. What has to last between
  sessions (the counts and limits) is kept by the tools, and GitHub is the memory of a thread.
- It passes what is typed into the terminal to a fresh session as a message, as plain text.
  Nothing typed is ever treated as a command: no line runs a program, whatever it starts with.
  It shows, in the terminal, what each session says and which tools it calls, with everything
  taken out that is not shown or that steers a terminal.
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
import guard  # noqa: E402
import tools  # noqa: E402

WATCH_STATE_FILE = "watch-state.json"
MAX_NUMBER = 10_000_000
LOGIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}(?:\[bot\])?$")
EVENT_ID = re.compile(r"^[A-Za-z0-9_.:/#-]{1,120}$")
EVENT_KIND = re.compile(r"^[a-z_]{1,40}$")
EVENT_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

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


def read_events(output: bytes) -> list[dict]:
    """The events a watcher printed, whatever way it ended. Output that cannot be read gives none."""
    try:
        events = json.loads(output.decode("utf-8", "replace"))["events"]
    except (ValueError, KeyError, TypeError):
        return []
    if not isinstance(events, list):
        return []
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


READING_TOOLS = ("Read", "Glob", "Grep", "mcp__desk__read_item")
TYPED_LINES = 200
TYPED_CHARS = 20_000


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
        # The same counts, limits and one receiver as the session's tools.
        self.desk = tools.Desk(plan["desk_env"])
        self.spending = self.desk.lock["spending"]
        self.jobs: queue.Queue = queue.Queue()
        self.stopping = threading.Event()
        self.typing_ended = threading.Event()
        self.watching_ended = threading.Event()
        self.session: subprocess.Popen | None = None
        self.watcher: subprocess.Popen | None = None
        self.guard = threading.Lock()
        self.handled = 0
        self.seen = 0
        self.held_path = self.state / "held-events.json"

    # -- the terminal -------------------------------------------------------

    def show(self, text: str) -> None:
        """Nothing is printed raw: whatever steers a terminal or is not shown is taken out, whoever wrote it."""
        try:
            self.shown.write(guard.shown(text).rstrip("\n") + "\n")
            self.shown.flush()
        except (OSError, ValueError):
            pass

    def read_typed(self) -> None:
        """Every typed line becomes part of a message. Lines that come together are one message, up to a bound."""
        pending: list[str] = []
        pending_lock = threading.Lock()

        def flush() -> None:
            with pending_lock:
                text = "\n".join(pending).strip()
                pending.clear()
            if text:
                self.jobs.put(("typed", text[:TYPED_CHARS]))

        timer: threading.Timer | None = None
        for line in self.typed:
            if timer is not None:
                timer.cancel()
            with pending_lock:
                full = len(pending) >= TYPED_LINES or sum(map(len, pending)) >= TYPED_CHARS
                if not full:
                    pending.append(line.rstrip("\n")[:TYPED_CHARS])
            if full:
                # What is typed without a pause does not pile up without bound: it goes on as it is.
                flush()
                with pending_lock:
                    pending.append(line.rstrip("\n")[:TYPED_CHARS])
            timer = threading.Timer(0.3, flush)
            timer.daemon = True
            timer.start()
        if timer is not None:
            timer.cancel()
        flush()
        self.typing_ended.set()

    # -- what a day may cost ------------------------------------------------

    def may_start(self, number: int | None) -> str | None:
        """The limit that stops a new session for this item now, or None when it may start."""
        try:
            with self.desk.counts() as record:
                spent = sum(tokens for _, _, tokens in record["sessions"])
                reached = None
                if len(record["sessions"]) >= self.spending["sessions_per_day"]:
                    reached = "sessions in a day"
                elif spent >= self.spending["tokens_per_day"]:
                    reached = "tokens in a day"
                elif number is not None and sum(1 for _, n, _ in record["sessions"] if n == number) >= self.spending["sessions_per_item_per_day"]:
                    reached = "sessions for one item in a day"
                if reached:
                    self.desk.limit_reached(record, reached)
                return reached
        except tools.Refused:
            return "the desk's record is damaged"

    def spent(self, number: int | None, tokens: int) -> None:
        try:
            with self.desk.counts() as record:
                record["sessions"].append([tools.now(), number, tokens])
        except tools.Refused:
            pass

    def hold(self, events: list[dict]) -> None:
        """Keep arrivals that the day's budget has no room for, up to a bound; count and drop the rest."""
        try:
            held = json.loads(self.held_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            held = []
        room = max(0, self.spending["events_kept"] - len(held))
        held += events[:room]
        self.held_path.write_text(json.dumps(held), encoding="utf-8")
        if len(events) > room:
            self.show(f"relay: {len(events) - room} arrival(s) dropped, the list of kept arrivals is full")
            try:
                with self.desk.counts() as record:
                    self.desk.limit_reached(record, "arrivals kept for later; more were dropped")
            except tools.Refused:
                pass

    def offer_held(self) -> None:
        """Put kept arrivals back in line when the budget has room again. What still has no room stays kept."""
        try:
            held = [e for e in json.loads(self.held_path.read_text(encoding="utf-8")) if address_only(e)]
        except (OSError, ValueError, TypeError):
            return
        if not held:
            return
        waiting = self.offer(held)
        self.held_path.write_text(json.dumps(waiting), encoding="utf-8")

    def offer(self, events: list[dict]) -> list[dict]:
        """Start what the budget has room for. Returns the arrivals that have to wait."""
        waiting: list[dict] = []
        for group in by_item(events):
            reached = self.may_start(group[0]["number"])
            if reached:
                waiting += group
            else:
                self.jobs.put((f"item {group[0]['number']}", news_message(group), group))
        return waiting

    # -- one session for one message ----------------------------------------

    def handle(self, about: str, text: str, events: list[dict], log) -> None:
        """Start a fresh session, give it this one message, show what it does, and end it within its bounds."""
        env = dict(self.plan["env"])
        number = None
        if about.startswith("item "):
            number = int(about.removeprefix("item "))
            # The tools of this session refuse every other item, and count answers against the account that wrote.
            env["DESK_ITEM"] = str(number)
            actors = [e["actor"] for e in events if "actor" in e]
            if actors:
                env["DESK_ACTOR"] = actors[-1]
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
        ended_by: list[str] = []

        def end(reason: str) -> None:
            if not ended_by:
                ended_by.append(reason)
                self.show(f"relay: session {self.handled} ended by the relay ({reason})")
                session.terminate()

        # A session that does not finish is ended, so that one item cannot hold up the desk or spend without bound.
        timer = threading.Timer(self.spending["seconds_per_session"], end, args=("its time was up",))
        timer.daemon = True
        timer.start()
        turns: dict[str, int] = {}
        reads = 0
        total = None
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
                if not isinstance(event, dict):
                    continue
                kind = event.get("type")
                message = event.get("message") if isinstance(event.get("message"), dict) else {}
                content = message.get("content")
                if kind == "assistant":
                    usage = message.get("usage") if isinstance(message.get("usage"), dict) else {}
                    turns[str(message.get("id"))] = sum(
                        usage.get(key) or 0 for key in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")
                    )
                if kind in ("assistant", "user") and isinstance(content, list):
                    for part in content:
                        if not isinstance(part, dict):
                            continue
                        if part.get("type") == "text" and kind == "assistant":
                            self.show("desk: " + str(part.get("text", "")))
                        elif part.get("type") == "tool_use":
                            self.show("desk calls " + str(part.get("name")))
                            reads += part.get("name") in READING_TOOLS
                        elif part.get("type") == "tool_result" and part.get("is_error"):
                            self.show("  refused: " + json.dumps(part.get("content"), ensure_ascii=False)[:300])
                if kind == "result":
                    usage = event.get("usage") if isinstance(event.get("usage"), dict) else {}
                    total = sum(usage.get(key) or 0 for key in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens"))
                    break
                if len(turns) > self.spending["turns_per_session"]:
                    end("too many turns")
                elif reads > self.spending["reads_per_session"]:
                    end("too many reads")
                elif sum(turns.values()) > self.spending["tokens_per_session"]:
                    end("too many tokens")
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
            # What the session's own account of its turns says, or, when it was ended early, what was seen of them.
            self.spent(number, max(total or 0, sum(turns.values())))
            if ended_by:
                self.desk.report("trouble", f"The relay ended a session early ({ended_by[0]}). It was about: {about}.")

    # -- the watcher --------------------------------------------------------

    def github_open(self) -> bool:
        try:
            return facts_file.load(self.facts_path).github_open
        except facts_file.FactsProblem:
            return False

    def watch(self) -> None:
        try:
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
                # What the watcher printed is handled whatever way it ended.
                events = read_events(output)
                if events:
                    self.show(f"relay: news on {len(by_item(events))} item(s) on GitHub")
                    self.seen += len(events)
                    waiting = self.offer(events)
                    if waiting:
                        self.show(f"relay: {len(by_item(waiting))} item(s) wait, the day's budget is used up")
                        self.hold(waiting)
                    # Not again until every one of them has been dealt with.
                    self.jobs.join()
                if code != 0:
                    # Every way of ending other than with a clean exit means: do not start it again.
                    reason = "it says that GitHub is not signed in" if code == 2 else f"it ended with code {code}"
                    self.show(f"relay: the watcher stopped ({reason}); watching has stopped")
                    self.desk.report("trouble", f"The watcher stopped: {reason}. It is not started again; the desk hears of nothing new until it is restarted.")
                    return
        finally:
            self.watching_ended.set()

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
                    self.jobs.put(("first message", self.first, []))
                self.offer_held()
                for target in (self.read_typed, self.watch):
                    threading.Thread(target=target, daemon=True).start()
                waited = 0.0
                try:
                    while not self.stopping.is_set():
                        tick = min(1.0, self.quiet_seconds)
                        try:
                            job = self.jobs.get(timeout=tick)
                        except queue.Empty:
                            if self.exit_when_idle and self.typing_ended.is_set() and (self.handled > 0 or self.seen > 0 or self.watching_ended.is_set()):
                                break
                            waited += tick
                            if waited >= self.retry_seconds:
                                waited = 0.0
                                self.offer_held()
                            continue
                        about, text, events = job if len(job) == 3 else (*job, [])
                        if about == "typed":
                            self.show("typed: " + text[:200])
                        try:
                            # What is typed comes from the coordinator and is always heard; it is counted like the rest.
                            self.handle(about, text, events, log)
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
