"""Stands between the terminal and the desk's sessions, and does the desk's waiting.

A session of the desk is Claude Code without its terminal interface: it takes a message as
data on its input and reports what it does as data on its output. The relay is the program in
the terminal. It does these things and nothing else.

- It runs the watcher, one at a time, with the state path the desk was started with and no
  other switch. A watcher that ends any other way than cleanly is not started again, and the
  relay itself tells the coordinator.
- Every arrival the watcher reports is first written into a kept list in the state folder: at
  most one entry for an item, a few for one account, a fixed number in all. An arrival is known
  by the item's number and the account's numeric id; an event without that id is not handled.
- It starts one session at a time, each for one kept arrival. The day's bounds are checked at
  the moment a session starts, and the session is counted then, with everything it may cost
  reserved; the reservation is settled when it ends. A session that goes past a bound is ended.
  What a day may cost is therefore at most the day's bound.
- For every item it starts a fresh session, tells it the kind of each arrival and the number
  of that one item, lets it finish, and ends it. What one outsider wrote is therefore never in
  the conversation in which another outsider's item is handled, and no conversation grows.
- It passes what is typed into the terminal to a fresh session as a message, as plain text.
  Nothing typed is ever treated as a command: no line runs a program, whatever it starts with.
  It shows, in the terminal, what each session says and which tools it calls, with every
  character taken out that is not on the list of what the desk writes with.
- A session is charged to the account whose arrival set it off, the newest one it handles: at
  most a share of the day's sessions for one account. When the relay has to end a session, that
  item and that account get no further session for a day. Their arrivals wait in the kept list.
- Once an hour at most it sends the coordinator one digest of what the sessions reported.
"""

from __future__ import annotations

import fcntl
import json
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
import state as state_files  # noqa: E402
import tools  # noqa: E402

WATCH_STATE_FILE = "watch-state.json"
KEPT_FILE = "held-events.json"
MAX_NUMBER = 10_000_000
LOGIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}(?:\[bot\])?$")
EVENT_ID = re.compile(r"^[A-Za-z0-9_.:/#-]{1,120}$")
EVENT_KIND = re.compile(r"^[a-z_]{1,40}$")
EVENT_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
READING_TOOLS = guard.READING
USAGE = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")
WEIGHTS = ("input", "cache_written", "cache_read", "output")
# What Claude Code says when the service does not accept the sign-in. Read only from its own report of an error.
SIGN_IN_REFUSED = re.compile(r"(?i)authenticat|invalid bearer|\b401\b|oauth|not logged in|/login")
DAMAGED = "the desk's record is damaged"
# Bounds that hold one item or one account back and leave the rest of the kept arrivals free to start.
FOR_ONE = ("sessions for one item in a day", "an item whose session had to be ended", "sessions for one account in a day",
           "an account whose session had to be ended")  # fmt: skip
WAITING = "arrivals waiting for an account's share of sessions"
TYPED_LINES = 200
TYPED_CHARS = 1_000  # A typed message is at most this many characters, so at most four times as many bytes.
EVENTS_PER_ARRIVAL = 10
ACCOUNTS_PER_ARRIVAL = 20


def whole_number(value, most: int = 2**53, least: int = 1) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and least <= value <= most


def address_only(event) -> dict | None:
    """Only an address, a kind and the account's numeric id pass, each in its expected shape. No outside text does."""
    if not isinstance(event, dict) or not whole_number(event.get("number"), MAX_NUMBER) or not whole_number(event.get("actor_id"), least=0):
        # The number 0 stands for "GitHub names no account": one account of its own, with the limits of any account.
        return None
    kept: dict = {"number": event["number"], "actor_id": event["actor_id"]}
    for key, shape in (("id", EVENT_ID), ("kind", EVENT_KIND), ("actor", LOGIN), ("time", EVENT_TIME)):
        value = event.get(key)
        if isinstance(value, str) and shape.fullmatch(value):
            kept[key] = value
    return kept if "kind" in kept else None


def watcher_command(copy: Path, state: Path) -> list[str]:
    """The one way the watcher is ever started."""
    return [sys.executable, "-I", str(copy / "tools" / "desk" / "watch.py"), "--wait", "--state", str(state / WATCH_STATE_FILE)]


def read_events(output: bytes) -> tuple[list[dict], int]:
    """The events a watcher printed, whatever way it ended, and how many of them could not be handled."""
    try:
        events = json.loads(output.decode("utf-8", "replace"))["events"]
    except (ValueError, KeyError, TypeError):
        return ([], 0)
    if not isinstance(events, list):
        return ([], 0)
    good = [kept for kept in map(address_only, events) if kept]
    return (good, len(events) - len(good))


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


def accounts_of(known: list[int], events: list[dict]) -> list[int]:
    """The accounts of one kept arrival, in the order they first wrote, up to a bound."""
    accounts = list(dict.fromkeys([*known, *[event["actor_id"] for event in events]]))
    return accounts[:ACCOUNTS_PER_ARRIVAL]


def units(counts: list[int], weights: dict) -> float:
    """What a session cost in weighted units: each class of tokens times its weight."""
    return sum(count * weights[name] for count, name in zip(counts, WEIGHTS, strict=True))


def reservation(spending: dict) -> int:
    """What is set aside when a session starts: its own bound, and one more turn, which is reported only after it happened."""
    return spending["units_per_session"] + spending["units_per_turn"]


class Kept:
    """The arrivals that wait for a session, in a file of the state folder.

    At most one entry for an item (a later arrival joins it: the session reads the whole item anyway),
    at most a few entries for one account, and a fixed number in all. When the list is full, an arrival
    of an account with nothing kept takes the place of the oldest entry of the account that has the most.
    """

    def __init__(self, path: Path, most: int, most_per_account: int):
        self.path, self.most, self.most_per_account = path, most, most_per_account
        self.guard = threading.Lock()

    def entries(self) -> list[dict]:
        text = state_files.read(self.path, "The list of kept arrivals")
        if text is None:
            return []
        try:
            entries = json.loads(text)
        except ValueError:
            return []
        good = []
        for entry in entries if isinstance(entries, list) else []:
            events = [e for e in map(address_only, entry.get("events", [])) if e] if isinstance(entry, dict) else []
            if events and whole_number(entry.get("account"), least=0) and len({e["number"] for e in events}) == 1:
                listed = entry.get("accounts") if isinstance(entry.get("accounts"), list) else []
                good.append({"number": events[0]["number"], "account": entry["account"],
                             "accounts": accounts_of([entry["account"], *[a for a in listed if whole_number(a, least=0)]], events),
                             "events": events[-EVENTS_PER_ARRIVAL:]})  # fmt: skip
        return good

    def _write(self, entries: list[dict]) -> None:
        state_files.write(self.path, "The list of kept arrivals", json.dumps(entries))

    def place(self, entries: list[dict], events: list[dict]) -> int | None:
        """Give one item's arrivals their place among the kept ones. Returns the account whose arrival gave way, or None."""
        number, account = events[0]["number"], events[0]["actor_id"]
        for entry in entries:
            if entry["number"] == number:
                # Only the newest arrivals are told to the session, but every account that wrote stays known, the first ones first:
                # writing again and again does not push another account out.
                entry["accounts"] = accounts_of(entry["accounts"], events)
                entry["events"] = (entry["events"] + events)[-EVENTS_PER_ARRIVAL:]
                return None
        held: dict[int, int] = {}
        for entry in entries:
            held[entry["account"]] = held.get(entry["account"], 0) + 1
        if held.get(account, 0) >= self.most_per_account:
            return account
        dropped = None
        if len(entries) >= self.most:
            # The account with the most places; among equals, the one whose oldest arrival is the oldest.
            most = max(held, key=lambda a: (held[a], -min(i for i, e in enumerate(entries) if e["account"] == a)))
            if account in held or held[most] <= 1:
                return account
            entries.remove(next(e for e in entries if e["account"] == most))
            dropped = most
        entries.append({"number": number, "account": account, "accounts": accounts_of([account], events), "events": events[-EVENTS_PER_ARRIVAL:]})
        return dropped

    def put(self, events: list[dict]) -> int | None:
        return (self.put_all([events]) or [None])[0]

    def put_all(self, groups: list[list[dict]]) -> list[int]:
        """Keep the arrivals of several items. Returns the accounts whose arrivals were dropped, one entry for each."""
        with self.guard:
            entries = self.entries()
            dropped = [account for account in (self.place(entries, group) for group in groups) if account is not None]
            self._write(entries)
            return dropped

    def done(self, number: int, handled: list[dict]) -> list[int]:
        """A session dealt with these arrivals. What arrived on the item meanwhile stays kept.

        Returns the accounts whose arrival was dropped: what stays goes to an account that wrote meanwhile, and
        the cap for one account holds for that account as for any other.
        """
        dropped = []
        with self.guard:
            entries = self.entries()
            for entry in entries:
                if entry["number"] == number:
                    entry["events"] = [event for event in entry["events"] if event not in handled]
                    entry["accounts"] = accounts_of([], entry["events"])
                    if entry["events"]:
                        held = [other["account"] for other in entries if other is not entry and other["events"]]
                        free = [account for account in entry["accounts"] if held.count(account) < self.most_per_account]
                        if free:
                            entry["account"] = free[0]
                        else:
                            dropped.append(entry["accounts"][0])
                            entry["events"] = []
            self._write([entry for entry in entries if entry["events"]])
        return dropped


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
        # The same record, limits and one receiver as the session's tools.
        self.desk = tools.Desk(plan["desk_env"])
        self.spending = self.desk.lock["spending"]
        self.kept = Kept(self.state / KEPT_FILE, self.spending["arrivals_kept"], self.spending["arrivals_kept_per_account"])
        self.typed_lines: queue.Queue = queue.Queue()
        self.stopping = threading.Event()
        self.typing_ended = threading.Event()
        self.watching_ended = threading.Event()
        self.nothing_to_start = threading.Event()
        self.session: subprocess.Popen | None = None
        self.watcher: subprocess.Popen | None = None
        self.guard = threading.Lock()
        self.handled = 0
        self.seen = 0
        self.failures = 0
        self.starts_nothing: str | None = None
        self.waiting_written: dict | None = None
        self.log = None

    # -- the terminal -------------------------------------------------------

    def show(self, text: str) -> None:
        """Nothing is printed raw: every character that is not on the list of what the desk writes with is taken out."""
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
                self.typed_lines.put(text[:TYPED_CHARS])

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

    def bound_reached(self, sessions: list[dict], number: int | None, account: int | None) -> str | None:
        """The bound that stops a session for this item and this account from starting now, or None."""
        spending = self.spending
        if len(sessions) >= spending["sessions_per_day"]:
            return "sessions in a day"
        if sum(s["reserved"] if s["charged"] is None else s["charged"] for s in sessions) + reservation(spending) > spending["units_per_day"]:
            return "what a day may cost"
        if number is not None:
            on_item = [s for s in sessions if s["number"] == number]
            if len(on_item) >= spending["sessions_per_item_per_day"]:
                return "sessions for one item in a day"
            if any(s.get("ended") for s in on_item):
                return "an item whose session had to be ended"
        if account is not None:
            for_account = [s for s in sessions if s.get("account") == account]
            if any(s.get("ended") for s in for_account):
                return "an account whose session had to be ended"
            if len(for_account) >= spending["sessions_per_account_per_day"]:
                return "sessions for one account in a day"
        return None

    def admit(self, number: int | None, account: int | None = None) -> tuple[dict | None, str | None]:
        position, entry, reached, _ = self.admit_first([(number, [account])])
        return (entry, reached)

    def admit_first(self, wanted: list[tuple[int | None, list[int | None]]]) -> tuple[int | None, dict | None, str | None, dict[int, int]]:
        """Count a session at the moment it starts, with everything it may cost set aside: the first of these that the bounds allow.

        Each is an item and the accounts that wrote on it, the newest first. The session is charged to the first of
        them that is not held back, so that an account that is held back cannot hold up another account's arrival
        by writing after it. Returns the place of the one that starts and its entry in the record (which names the
        account), or the bound that stops them all; and how many of them wait because of an account's own bounds.
        """
        spending = self.spending
        waiting: dict[int, int] = {}
        reached = None
        try:
            with self.desk.counts() as record:
                sessions = record["sessions"]
                for position, (number, accounts) in enumerate(wanted):
                    for account in accounts:
                        reached = self.bound_reached(sessions, number, account)
                        if reached is None or "account" not in reached:
                            break
                    else:
                        account = accounts[0]
                    if reached is None:
                        entry = {"t": record["clock"], "id": max([s.get("id", 0) for s in sessions], default=0) + 1, "number": number,
                                 "account": account, "ended": False, "reserved": reservation(spending), "charged": None, "counts": None}  # fmt: skip
                        sessions.append(entry)
                        return (position, dict(entry), None, waiting)
                    if reached not in FOR_ONE:
                        # A bound of the day stops everything. The coordinator is told once for each bound.
                        self.desk.limit_reached(record, reached)
                        break
                    if reached == "sessions for one item in a day":
                        self.desk.limit_reached(record, reached)
                    elif account is not None and "account" in reached:
                        waiting[account] = waiting.get(account, 0) + 1
                return (None, None, reached, waiting)
        except tools.Refused:
            return (None, None, DAMAGED, waiting)

    def settle(self, entry: dict, counts: list[int] | None, charged: float | None, ended: bool = False) -> None:
        """Write what the session cost. Without a count from the service it is charged everything that was set aside.

        ended says that the relay had to end the session at a bound: its item and the account that set it off get no further session for a day.
        """
        try:
            with self.desk.counts() as record:
                for session in record["sessions"]:
                    if session.get("id") == entry["id"] and session["charged"] is None:
                        session["charged"] = entry["reserved"] if not charged or charged <= 0 else charged
                        # The four counts as the service gave them, so that real days can replace the estimate of a day's cost.
                        session["counts"] = counts
                        session["ended"] = bool(ended)
        except tools.Refused:
            pass

    # -- arrivals -----------------------------------------------------------

    def take(self, output: bytes) -> int:
        """Write what the watcher printed into the kept list. Returns how many arrivals it held."""
        events, malformed = read_events(output)
        if malformed:
            self.show(f"relay: {malformed} arrival(s) without an account's numeric id or in the wrong shape were not handled")
            self.desk.count("arrivals that could not be read", by=malformed)
        dropped: dict[int, int] = {}
        for account in self.kept.put_all(by_item(events)):
            dropped[account] = dropped.get(account, 0) + 1
        for account, count in dropped.items():
            self.desk.count("arrivals dropped", account=account, by=count)
        if dropped:
            self.show(f"relay: {sum(dropped.values())} arrival(s) were dropped, the list of kept arrivals has no room for them")
            try:
                with self.desk.counts() as record:
                    self.desk.limit_reached(record, "arrivals kept for later; more were dropped")
            except tools.Refused:
                pass
        return len(events)

    def start_one(self) -> bool:
        """Start one session: for what was typed, or for the oldest kept arrival that the bounds leave room for."""
        if self.starts_nothing:
            return False
        try:
            text = self.typed_lines.get_nowait()
        except queue.Empty:
            text = None
        if text is not None:
            self.show("typed: " + text[:200])
            entry, reached = self.admit(None)
            if entry is None:
                self.show(f"relay: not started ({reached})")
                self.damaged(reached)
                return True
            self.handle("typed", text, None, [], entry)
            return True
        kept = self.kept.entries()
        # A session is charged to the account whose arrival set it off: the newest one it handles.
        position, entry, reached, waiting = self.admit_first(
            [(arrival["number"], list(dict.fromkeys(event["actor_id"] for event in reversed(arrival["events"])))) for arrival in kept])  # fmt: skip
        if waiting != self.waiting_written:
            # Arrivals that wait for an account's share are counted for the next digest.
            self.desk.count_as(WAITING, waiting)
            self.waiting_written = waiting
        if entry is None:
            self.damaged(reached)
            return False
        arrival = kept[position]
        # The session handles the arrivals up to the newest one of the account it is charged to. What an account that
        # is held back wrote after that stays kept, for that account.
        newest = max(n for n, event in enumerate(arrival["events"]) if event["actor_id"] == entry["account"])
        handled = arrival["events"][: newest + 1]
        # The arrival stays in the kept list until a session has dealt with it.
        if self.handle(f"item {arrival['number']}", news_message(handled), arrival["number"], arrival["accounts"], entry):
            for account in self.kept.done(arrival["number"], handled):
                self.desk.count("arrivals dropped", account=account)
        return True

    # -- one session for one message ----------------------------------------

    def run_session(self, text: str, env: dict[str, str]) -> dict:
        """Run one session to its end within its bounds. Returns what was seen of it."""
        spending = self.spending
        seen = {"counts": None, "ended_by": None, "refused_sign_in": False, "turns": 0, "reads": 0, "at_a_bound": False, "over_the_base": False}
        try:
            with self.guard:
                if self.stopping.is_set():
                    return seen | {"ended_by": "the desk is stopping"}
                self.session = subprocess.Popen(
                    self.plan["argv"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    text=True, encoding="utf-8", errors="replace", env=env, cwd=self.plan["cwd"],
                )  # fmt: skip
        except OSError:
            self.show("relay: the session could not be started")
            return seen | {"ended_by": "it could not be started"}
        session = self.session

        def end(reason: str, at_a_bound: bool = True) -> None:
            if seen["ended_by"] is None:
                seen["ended_by"] = reason
                seen["at_a_bound"] = at_a_bound
                self.show(f"relay: session {self.handled} ended by the relay ({reason})")
                session.terminate()

        # A session that does not finish is ended, so that one item cannot hold up the desk or spend without bound.
        timer = threading.Timer(spending["seconds_per_session"], end, args=("its time was up",))
        timer.daemon = True
        timer.start()
        turns: dict[str, list[int]] = {}
        asked: dict[str, str] = {}
        said: list[str] = []
        result = None
        try:
            # Always a message with plain text in it. Nothing here is a command to Claude Code or to a shell.
            session.stdin.write(json.dumps({"type": "user", "message": {"role": "user", "content": text}}, ensure_ascii=False) + "\n")
            session.stdin.flush()
            for line in session.stdout:
                if self.log is not None:
                    self.log.write(line)
                    self.log.flush()
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
                    turns[str(message.get("id"))] = [int(usage.get(key) or 0) for key in USAGE]
                    # The arithmetic checks itself: the first response takes in Claude Code's own instructions and tools,
                    # the standing instructions and the first message, and nothing else. More than these may be is a
                    # fault of the desk, not of an outsider.
                    if len(turns) == 1 and sum(turns[str(message.get("id"))][:3]) > spending["base_tokens"] + spending["brief_bytes"] + spending["message_bytes"]:
                        seen["over_the_base"] = True
                        end("its first response took in more than the bounds count on", at_a_bound=False)
                if kind in ("assistant", "user") and isinstance(content, list):
                    for part in content:
                        if not isinstance(part, dict):
                            continue
                        if part.get("type") == "text" and kind == "assistant":
                            said.append(str(part.get("text", "")))
                            self.show("desk: " + said[-1])
                        elif part.get("type") == "tool_use":
                            self.show("desk calls " + str(part.get("name")))
                            asked[str(part.get("id"))] = part.get("name")
                        elif part.get("type") == "tool_result" and part.get("is_error"):
                            self.show("  refused: " + json.dumps(part.get("content"), ensure_ascii=False)[:300])
                        elif part.get("type") == "tool_result":
                            # A reading call that ran. One that the check refused read nothing and is not counted here.
                            seen["reads"] += asked.get(str(part.get("tool_use_id"))) in READING_TOOLS
                if kind == "result":
                    result = event
                    break
                # The second line behind Claude Code's own bound on turns and the check that counts reading calls.
                spent = units([sum(column) for column in zip(*turns.values(), strict=True)], spending["weights"]) if turns else 0
                if len(turns) > spending["turns_per_session"]:
                    end("too many turns")
                elif seen["reads"] > spending["reads_per_session"]:
                    end("too many reads")
                elif spent > spending["units_per_session"]:
                    end("it cost more than one session may")
        except (OSError, ValueError):
            pass
        finally:
            timer.cancel()
            try:
                session.stdin.close()
            except (OSError, ValueError):
                pass
            try:
                session.wait(timeout=spending["grace_seconds"])
            except subprocess.TimeoutExpired:
                session.kill()
                session.wait()
            with self.guard:
                self.session = None
        seen["turns"] = len(turns)
        if turns:
            seen["counts"] = [sum(column) for column in zip(*turns.values(), strict=True)]
        if result is not None:
            usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
            total = [int(usage.get(key) or 0) for key in USAGE]
            if sum(total) > sum(seen["counts"] or [0]):
                seen["counts"] = total
            # Claude Code's own report of an error, not anything the model wrote.
            if result.get("is_error") is True and not sum(seen["counts"] or [0]) and SIGN_IN_REFUSED.search(str(result.get("result", "")) + " ".join(said)):
                seen["refused_sign_in"] = True
        elif seen["ended_by"] is None:
            seen["ended_by"] = "it ended without a result"
            self.show(f"relay: session {self.handled} ended early ({seen['ended_by']})")
        return seen

    def handle(self, about: str, text: str, number: int | None, accounts: list[int], entry: dict) -> bool:
        """One fresh session for one message. Returns False when the arrival has to be kept for another try."""
        env = dict(self.plan["env"])
        if number is not None:
            # The tools of this session refuse every other item, and count answers against the accounts that wrote.
            env["DESK_ITEM"] = str(number)
            env["DESK_ACCOUNTS"] = json.dumps(sorted(accounts))
        self.handled += 1
        if self.log is not None:
            self.log.write(json.dumps({"type": "relay", "session": self.handled, "about": about}) + "\n")
            self.log.flush()
        self.show(f"relay: session {self.handled} ({about})")
        seen = self.run_session(text, env)
        spending = self.spending
        if seen["refused_sign_in"]:
            # Nothing was spent: the service did not let the session in.
            self.settle(entry, seen["counts"], 1)
            self.failures += 1
            self.show("relay: the service refused the desk's sign-in")
            if self.failures == 1:
                self.desk_down("The service refused the desk's sign-in to Claude. The arrival is kept. After three refusals in a row the desk starts no more sessions until it is started again.")
            if self.failures >= spending["sign_in_failures"]:
                self.starts_nothing = "the sign-in was refused three times in a row"
                self.show("relay: no more sessions are started until the desk is started again")
                self.stop_watching()
            return False
        self.failures = 0
        counts = seen["counts"]
        cost = units(counts, spending["weights"]) if counts else 0
        if seen.get("over_the_base"):
            self.settle(entry, counts, max(cost, entry["reserved"]))
            self.starts_nothing = "a session took in more than the bounds count on"
            self.show("relay: no more sessions are started until the desk is started again (" + self.starts_nothing + ")")
            self.desk_down("The first response of a session took in more than Claude Code's own instructions, the standing instructions and the first "
                           "message may be. The bounds on cost do not hold; no session is started until the desk is started again. The arrival is kept.")
            self.stop_watching()
            return False
        if self.stopping.is_set() and not seen.get("at_a_bound"):
            # The desk is being stopped in the middle of a session: the arrival stays kept, and nobody is held back for it.
            self.settle(entry, counts, max(cost, entry["reserved"]))
            return False
        if seen["ended_by"] is not None:
            # A session the relay had to end is charged everything that was set aside for it.
            self.settle(entry, counts, max(cost, entry["reserved"]), ended=bool(seen.get("at_a_bound")))
            self.desk.note("session_ended", f"The relay ended a session early ({seen['ended_by']}). It was about: {about}.", number)
        else:
            self.settle(entry, counts, cost)
        if cost > entry["reserved"]:
            self.desk_down("One session cost more than what is set aside for a session. The bound on one turn does not hold; look at the desk's record.")
        return True

    def damaged(self, reached: str | None) -> None:
        """A record that cannot be read cannot show that a bound is not reached: nothing more is started."""
        if reached == DAMAGED and not self.starts_nothing:
            self.starts_nothing = DAMAGED
            self.show("relay: no more sessions are started until the desk is started again (" + DAMAGED + ")")
            self.desk_down("The desk's record of what it sent and spent is damaged. No session is started until the desk is started again.")

    def desk_down(self, line: str) -> None:
        if not self.desk.desk_down(line):
            self.show("relay: the coordinator could not be told: " + line)

    # -- the watcher --------------------------------------------------------

    def github_open(self) -> bool:
        try:
            return facts_file.load(self.facts_path).github_open
        except facts_file.FactsProblem:
            return False

    def stop_watching(self) -> None:
        self.watching_ended.set()
        with self.guard:
            if self.watcher is not None and self.watcher.poll() is None:
                self.watcher.terminate()

    def watch(self) -> None:
        try:
            while not self.stopping.is_set() and not self.watching_ended.is_set():
                # The facts are read before every wait: closing the way in the file stops the watching.
                if not self.github_open():
                    self.stopping.wait(timeout=self.retry_seconds)
                    continue
                try:
                    # The watcher's own file is looked at like every file of the state folder, before every start.
                    state_files.file(self.state / WATCH_STATE_FILE, "The watcher's state file")
                except state_files.StateProblem as problem:
                    self.show(f"relay: the watcher was not started ({problem}); watching has stopped")
                    self.desk_down(f"The watcher was not started: {problem} The desk hears of nothing new until it is started again.")
                    return
                try:
                    with self.guard:
                        if self.stopping.is_set() or self.watching_ended.is_set():
                            return
                        self.watcher = subprocess.Popen(
                            watcher_command(self.copy, self.state), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, env=self.watcher_env, cwd=str(self.copy),
                        )  # fmt: skip
                    output, _ = self.watcher.communicate()
                    code = self.watcher.returncode
                except OSError:
                    code, output = 127, b""
                if self.stopping.is_set() or self.watching_ended.is_set():
                    return
                # What the watcher printed is kept whatever way it ended.
                self.nothing_to_start.clear()
                held = self.take(output)
                if held:
                    self.seen += held
                    self.show(f"relay: news on GitHub ({held} arrival(s))")
                if code != 0:
                    # Every way of ending other than with a clean exit means: do not start it again.
                    reason = "it says that GitHub is not signed in" if code == 2 else f"it ended with code {code}"
                    self.show(f"relay: the watcher stopped ({reason}); watching has stopped")
                    self.desk_down(f"The watcher stopped: {reason}. It is not started again; the desk hears of nothing new until it is started again.")
                    return
                # Not again until everything that can start now has been dealt with.
                while not self.nothing_to_start.wait(timeout=0.2):
                    if self.stopping.is_set():
                        return
        finally:
            self.watching_ended.set()

    # -- everything together ------------------------------------------------

    def run(self) -> int:
        try:
            return self._run()
        except state_files.StateProblem as problem:
            self.show(f"The desk was stopped. {problem}")
        except Exception as error:  # Whatever was not expected ends the desk in one sentence.
            self.show(f"The desk was stopped. Something it relies on failed ({type(error).__name__}).")
        self.stop()
        return 1

    def _run(self) -> int:
        state_files.folder(self.state, "The state folder")
        session = state_files.folder(self.state / "session", "The folder session in the state folder", make=True)
        state_files.file(self.state / WATCH_STATE_FILE, "The watcher's state file")
        with state_files.opened(self.state / "desk.lock", "The file desk.lock in the state folder") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                self.show("The desk was not started. Another desk is already running on this state folder.")
                return 1
            # The counts of reading calls of earlier sessions are of no use any more.
            for old in session.glob("reads-*.json*"):
                if state_files.file(old, "A count of reading calls in the state folder"):
                    old.unlink()
            self.kept.entries()
            log_path = session / "stream.jsonl"
            if state_files.file(log_path, "The record of the sessions' output"):
                state_files.file(log_path.with_suffix(".jsonl.1"), "The record of the sessions' output")
                log_path.replace(log_path.with_suffix(".jsonl.1"))
            with state_files.opened(log_path, "The record of the sessions' output") as log:
                self.log = log
                self.show("relay: the desk is running, locked. What is typed here is passed to a session as a message.")
                if self.first:
                    self.typed_lines.put(self.first)
                for target in (self.read_typed, self.watch):
                    threading.Thread(target=target, daemon=True).start()
                waited = self.retry_seconds
                try:
                    while not self.stopping.is_set():
                        if self.start_one():
                            continue
                        self.nothing_to_start.set()
                        if self.exit_when_idle and self.typing_ended.is_set() and self.typed_lines.empty() and (
                            self.handled > 0 or self.seen > 0 or self.watching_ended.is_set() or self.starts_nothing
                        ):
                            break
                        tick = min(1.0, self.quiet_seconds)
                        self.stopping.wait(timeout=tick)
                        waited += tick
                        if waited >= self.retry_seconds:
                            # At most once an hour, and only when there is something to say; the tools keep that time.
                            waited = 0.0
                            if self.desk.digest():
                                # The digest carried the count of what waits; it is written again for the next one.
                                self.waiting_written = None
                except KeyboardInterrupt:
                    pass
                finally:
                    self.log = None
                    self.stop()
        return 0

    def stop(self) -> None:
        self.stopping.set()
        with self.guard:
            for process in (self.watcher, self.session):
                if process is not None and process.poll() is None:
                    process.terminate()
