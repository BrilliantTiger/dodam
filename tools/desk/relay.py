"""Stands between the terminal and the desk's sessions, and does the desk's waiting.

A session of the desk is Claude Code without its terminal interface: it takes a message as
data on its input and reports what it does as data on its output. The relay is the program in
the terminal. It does these things and nothing else.

- It runs the watcher, one at a time, with the state path the desk was started with and no
  other switch. A watcher that ends any other way than cleanly is not started again, and the
  relay itself tells the coordinator.
- Every arrival the watcher reports is first written into a kept list in the state folder: at
  most one place for an item and an account, a few for one account, a fixed number in all. An arrival is known
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
- A session handles the arrivals of every writer on its item that is not held back, and it has
  one payer: the newest of those writers. The session and its cost count against the payer's
  share of a day. When the relay has to end a session, the item gets no further session for a
  day, and neither does the payer when the session was shown its text. Arrivals wait in the
  kept list, each account's in places of its own.
- Once an hour at most it sends the coordinator one digest of what the sessions reported.
"""

from __future__ import annotations

import fcntl
import json
import queue
import re
import secrets
import subprocess
import sys
import threading
import time
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
           "an account whose session had to be ended", "what a day may cost for one account",
           "an item on which two sessions in a row had to be ended")  # fmt: skip
SESSIONS, UNITS, PLACES = "sessions in a day", "what a day may cost", "arrivals kept for later; more were dropped"
WAITING = "arrivals waiting for an account's share of sessions"
TYPED_LINES = 200
TYPED_CHARS = 1_000  # A typed message is at most this many characters, so at most four times as many bytes.
EVENTS_PER_ARRIVAL = 10  # What one place keeps of its account's arrivals on its item, and what one session is told.
SHOWN_FILE = tools.SHOWN_FILE
# How a session can end without a result of Claude Code's own.
NOT_STARTED, WITHOUT_A_RESULT = "it could not be started", "it ended without a result"
PLACING = re.compile(r"^[0-9a-f]{32}$")


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
    if whole_number(event.get("seq")):
        kept["seq"] = event["seq"]  # The relay's own count of arrivals: which one is the newest.
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


def by_place(events: list[dict]) -> list[list[dict]]:
    """The arrivals of each account on each item together, each group in the order of its first arrival."""
    groups: dict[tuple[int, int], list[dict]] = {}
    for event in events:
        groups.setdefault((event["number"], event["actor_id"]), []).append(event)
    return list(groups.values())


def told(places: list[dict]) -> list[dict]:
    """What a session is told of the places it handles: the newest arrivals, at least one of each account, a bounded number."""
    each = max(1, EVENTS_PER_ARRIVAL // max(1, len(places)))
    events = [event for place in places for event in place["events"][-each:]]
    return sorted(events, key=lambda event: event.get("seq", 0))[-EVENTS_PER_ARRIVAL:]


def news_message(events: list[dict]) -> str:
    number = events[0]["number"]
    return (
        f"From the desk's relay: something new arrived on GitHub on item {number}. Each line below is one arrival, with its "
        "kind. Read the item with read_item and handle it as your standing instructions say. Handle no other item.\n"
        + "\n".join(json.dumps({key: value for key, value in event.items() if key != "seq"}, ensure_ascii=False) for event in events)
    )


def newest_first(places: list[dict]) -> list[int]:
    """The accounts of these places, the one that wrote last first."""
    return [place["account"] for place in sorted(places, key=lambda place: place["events"][-1].get("seq", 0), reverse=True)]


def units(counts: list[int], weights: dict) -> float:
    """What a session cost in weighted units: each class of tokens times its weight."""
    return sum(count * weights[name] for count, name in zip(counts, WEIGHTS, strict=True))


def reservation(spending: dict) -> int:
    """What is set aside when a session starts: its own bound, and one more turn, which is reported only after it happened."""
    return spending["units_per_session"] + spending["units_per_turn"]


# The points between two of the relay's own steps at the start and at the end of a session, in their order. Between the
# first and the last point of each the relay holds its one lock: nothing else of the relay happens there.
POINTS_AT_START = ("start: before the first step", "start: what is kept on a closed item is counted", "start: what is kept on a closed item is out of the list",
                   "start: the kept list is read", "start: the session is counted and its places are in hand", "start: the step is over")  # fmt: skip
POINTS_AT_END = ("end: before the first step", "end: the handled places are out of the list", "end: the cost is written",
                 "end: what is kept on a closed item is counted", "end: the places of a closed item are out of the list",
                 "end: nothing is in hand and no session runs", "end: the step is over")  # fmt: skip
# The points of a placing at which the desk can be stopped: after the kept list, with what went written beside it, and
# after what went was counted in the record.
POINTS_AT_PLACING = ("place: the list is written", "place: what went is counted")


def marked(env: dict[str, str], number: int | None = None, accounts=(), held_back=()) -> dict[str, str]:
    """The environment of one session, with the mark that says what kind it is: without the mark its tools do nothing.

    A session for an item is given the item, the accounts whose arrivals it handles, the newest first, and the accounts
    that are held back now, whose text its tools leave out. A session for what was typed to the desk is given none of these.
    """
    env = dict(env)
    if number is None:
        env["DESK_SESSION"] = "typed"
    else:
        env |= {"DESK_SESSION": "item", "DESK_ITEM": str(number), "DESK_ACCOUNTS": json.dumps(list(accounts)), "DESK_HELD_BACK": json.dumps(list(held_back))}
    return env


class Kept:
    """The arrivals that wait for a session, in a file of the state folder.

    One place for an item and an account, holding that account's own arrivals on that item and nobody else's;
    at most a few places for one account, and a fixed number in all. What another account writes therefore
    touches no place but its own, with two exceptions. When every place is taken and a writer that is not held
    back needs one, a place of an account that is held back goes first, even its last; only when no held-back
    account holds a place it can give does an account that holds nothing take the oldest place that can go of the
    account that can give the most, and only while that account can give more than one. An account can give the
    places it holds that are not in hand. And when an item is closed, every arrival kept
    on it goes with it. A place whose arrivals the running session is handling is in hand: it counts as a place,
    it does not go, and it is not among the places its account can give.
    """

    def __init__(self, path: Path, most: int, most_per_account: int):
        self.path, self.most, self.most_per_account = path, most, most_per_account
        self.guard = threading.Lock()

    def entries(self) -> list[dict]:
        return self._read()[0]

    def pending(self) -> list[dict]:
        """What went at placings that is not counted for certain yet, each placing with its own name."""
        return self._read()[1]

    def _read(self) -> tuple[list[dict], list[dict]]:
        """The places, and beside them in the same file what went at a placing whose counting is not known to be done."""
        text = state_files.read(self.path, "The list of kept arrivals")
        if text is None:
            return ([], [])
        try:
            entries = json.loads(text)
        except ValueError:
            return ([], [])
        good, pending = [], []
        for entry in entries if isinstance(entries, list) else []:
            if isinstance(entry, dict) and "placing" in entry:
                placing = went_at(entry)
                if placing is not None:
                    pending.append(placing)
                continue
            events = [e for e in map(address_only, entry.get("events", [])) if e] if isinstance(entry, dict) else []
            # A place holds the arrivals of its own account on one item, and nothing else.
            if events and whole_number(entry.get("account"), least=0) and len({e["number"] for e in events}) == 1 and all(
                    e["actor_id"] == entry["account"] for e in events):  # fmt: skip
                good.append({"number": events[0]["number"], "account": entry["account"], "events": events[-EVENTS_PER_ARRIVAL:]})
        return (good, pending)

    def _write(self, entries: list[dict], pending: list[dict]) -> None:
        state_files.write(self.path, "The list of kept arrivals", json.dumps(entries + pending))

    def place(self, entries: list[dict], events: list[dict], held_back=(), in_hand=(), gone: list | None = None) -> int | None:
        """Give one account's arrivals on one item their place. Returns the account whose arrival gave way, or None.

        What gave way is added to gone: the account, the item, how many arrivals, and why. The reasons: the writer's own
        places are all taken ("own"), every place is taken and the writer is held back ("held back") or nothing can
        give way ("full"), or a kept place went to the writer ("gave way").
        """
        number, account = events[0]["number"], events[0]["actor_id"]
        gone = [] if gone is None else gone
        if any("seq" not in event for event in events):
            seq = max((e.get("seq", 0) for entry in entries for e in entry["events"]), default=0)
            events = [event | {"seq": seq + n + 1} for n, event in enumerate(events)]

        def went(whose: int, item: int, arrivals: int, why: str, closed_to_all: bool = False) -> int:
            gone.append({"account": whose, "number": item, "arrivals": arrivals, "why": why, "writer_held_back": account in held_back,
                         "closed_to_all": closed_to_all})  # fmt: skip
            return whose

        for entry in entries:
            if entry["number"] == number and entry["account"] == account:
                # Writing again removes only the account's own older arrivals on this item.
                entry["events"] = (entry["events"] + events)[-EVENTS_PER_ARRIVAL:]
                return None
        held: dict[int, int] = {}
        for entry in entries:
            held[entry["account"]] = held.get(entry["account"], 0) + 1
        if held.get(account, 0) >= self.most_per_account:
            return went(account, number, len(events), "own")
        dropped = None
        if len(entries) >= self.most:
            if account in held_back:
                return went(account, number, len(events), "held back")
            # A place in hand does not go, and is not counted among the places its account can give.
            can_go = [e for e in entries if (e["number"], e["account"]) not in in_hand]
            gives: dict[int, int] = {}
            for entry in can_go:
                gives[entry["account"]] = gives.get(entry["account"], 0) + 1
            # The places of accounts that are held back never stand in the way of one that is not: one of them goes
            # first. Otherwise the account that can give the most places gives one, while it can give more than one, to
            # an account that holds none. Among equals it is the one whose oldest arrival that can go is the oldest.
            waiting = [a for a in gives if a in held_back]
            most = max(waiting or gives, key=lambda a: (gives[a], -min(i for i, e in enumerate(can_go) if e["account"] == a)), default=None)
            if most is None or not waiting and (account in held or gives[most] <= 1):
                # The list is closed to everyone, an account that holds nothing too, when no account can give a place.
                return went(account, number, len(events), "full", most is None or not waiting and gives[most] <= 1)
            leaves = next(e for e in can_go if e["account"] == most)
            entries.remove(leaves)
            dropped = went(most, leaves["number"], len(leaves["events"]), "gave way")
        entries.append({"number": number, "account": account, "events": events[-EVENTS_PER_ARRIVAL:]})
        return dropped

    def put(self, events: list[dict], held_back=(), in_hand=(), gone: list | None = None) -> int | None:
        return (self.put_all(events, held_back, in_hand, gone) or [None])[0]

    def put_all(self, events: list[dict], held_back=(), in_hand=(), gone: list | None = None) -> list[int]:
        """Keep arrivals, given in the order in which they arrived. Returns the accounts whose arrivals were dropped.

        Every arrival is numbered in that order before it is placed, so that which writer is the newest is the same
        whether the arrivals came one by one or together, in any order of their accounts. What went (in gone) is written
        into the same file as the places, under a name of its own, so that a stop before it is counted leaves it there
        to be counted at the next placing or start.
        """
        gone = [] if gone is None else gone
        already = len(gone)
        with self.guard:
            entries, pending = self._read()
            seq = max((e.get("seq", 0) for entry in entries for e in entry["events"]), default=0)
            numbered = [event | {"seq": seq + n + 1} for n, event in enumerate(events)]
            dropped = [account for account in (self.place(entries, group, held_back, in_hand, gone) for group in by_place(numbered)) if account is not None]
            if gone:
                # Every place was taken and no account could give one: the accounts that hold the most, for the digest.
                holders: dict[int, int] = {}
                if [went for went in gone[already:] if went["why"] == "full" and went.get("closed_to_all")]:
                    for entry in entries:
                        holders[entry["account"]] = holders.get(entry["account"], 0) + 1
                # A limit was reached by what went, unless only what a held-back account wrote went, or an arrival on a closed item.
                notice = bool([went for went in gone if went["why"] != "closed" and not (went["why"] in ("own", "held back") and went.get("writer_held_back"))])
                pending = pending + [{"placing": secrets.token_hex(16), "gone": [dict(went) for went in gone], "notice": notice,
                                      "holders": sorted(holders, key=lambda a: (-holders[a], a))[:3]}]  # fmt: skip
            self._write(entries, pending)
            return dropped

    def settled(self, names: list[str]) -> None:
        """What went at these placings is counted: their names leave the file."""
        with self.guard:
            entries, pending = self._read()
            self._write(entries, [placing for placing in pending if placing["placing"] not in names])

    def done(self, handled: list[dict]) -> None:
        """A session dealt with these places. What their accounts wrote meanwhile stays kept, in the same places."""
        with self.guard:
            entries, pending = self._read()
            for entry in entries:
                for place in handled:
                    if (entry["number"], entry["account"]) == (place["number"], place["account"]):
                        entry["events"] = [event for event in entry["events"] if event not in place["events"]]
            self._write([entry for entry in entries if entry["events"]], pending)

    def forget(self, number: int) -> list[tuple[int, int]]:
        """Take away every place on one item. Returns the accounts whose places they were, each with how many arrivals it held."""
        with self.guard:
            entries, pending = self._read()
            self._write([entry for entry in entries if entry["number"] != number], pending)
            return [(entry["account"], len(entry["events"])) for entry in entries if entry["number"] == number]


def went_at(entry: dict) -> dict | None:
    """What went at one placing, as it was written beside the places, or None when it is not in that shape."""
    gone = entry.get("gone")
    if not isinstance(entry.get("placing"), str) or not PLACING.fullmatch(entry["placing"]) or not isinstance(gone, list):
        return None
    good = [went for went in gone if isinstance(went, dict) and whole_number(went.get("account"), least=0)
            and whole_number(went.get("number"), MAX_NUMBER) and whole_number(went.get("arrivals")) and isinstance(went.get("why"), str)]  # fmt: skip
    holders = [account for account in entry.get("holders", []) if whole_number(account, least=0)] if isinstance(entry.get("holders"), list) else []
    return {"placing": entry["placing"], "gone": good, "notice": entry.get("notice") is True, "holders": holders[:3]}


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
        # The one lock for what the watcher's arrivals and the relay's sessions share: the kept list, the record of
        # sessions, which session is running and which places are in hand. Placing an arrival, choosing what starts,
        # starting a session and ending one each take it. It is held for the steps around a session, never for its length.
        self.turn = threading.Lock()
        self.handled = 0
        self.seen = 0
        self.failures = 0
        self.starts_nothing: str | None = None
        self.waiting_written: dict | None = None
        self.log = None
        # The session that is running, by its number in the record, and the places whose arrivals it is handling.
        self.running: int | None = None
        self.in_hand: list[tuple[int, int]] = []

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

    def at(self, point: str) -> None:
        """A point between two of the relay's own steps at a session's start or end. Nothing happens here."""

    # -- what a day may cost ------------------------------------------------

    def day_bound(self, sessions: list[dict]) -> str | None:
        """The bound of the day that stops every session from starting now, or None."""
        spending = self.spending
        if len(sessions) >= spending["sessions_per_day"]:
            return "sessions in a day"
        if sum(tools.charge_of(s) for s in sessions) + reservation(spending) > spending["units_per_day"]:
            return "what a day may cost"
        return None

    def item_bound(self, record: dict, number: int) -> str | None:
        """What holds one item back now, whoever wrote on it."""
        if str(number) in record["closed_items"]:
            return "an item on which two sessions in a row had to be ended"
        # A session that the service did not let in was nobody's doing: it counts against no item and no account.
        on_item = [s for s in record["sessions"] if s["number"] == number and s.get("let_in", True)]
        if any(s.get("ended") for s in on_item):
            return "an item whose session had to be ended"
        if len(on_item) >= self.spending["sessions_per_item_per_day"]:
            return "sessions for one item in a day"
        return None

    def account_bound(self, sessions: list[dict], account: int) -> str | None:
        """What holds one account back now: its own doing only, counted from the sessions it paid for that have ended.

        This is the one place that says who is held back: the choice of what starts, the placing of arrivals and what
        the tools are told all come from here. The session that is running is no part of it, neither as one of the
        account's sessions nor with what is set aside for it; a session whose cost was never written, because the
        desk was stopped while it ran, counts as one that ended and was charged everything set aside.
        """
        spending = self.spending
        sessions = [s for s in sessions if s.get("id") != self.running or s.get("charged") is not None]
        if any(account in s.get("waits", []) for s in sessions):
            return "an account whose session had to be ended"
        paid = [s for s in sessions if s.get("account") == account and s.get("let_in", True)]
        if len(paid) >= spending["sessions_per_account_per_day"]:
            return "sessions for one account in a day"
        if sum(tools.charge_of(s) for s in paid) >= spending["units_per_account_per_day"]:
            return "what a day may cost for one account"
        return None

    def held_back(self, sessions: list[dict] | None = None) -> list[int]:
        """The accounts that get no session now. Only an account that paid for a session of the last day can be one."""
        try:
            if sessions is None:
                with self.desk.counts() as record:
                    sessions = record["sessions"]
        except tools.Refused:
            return []
        paying = {s.get("account") for s in sessions} | {a for s in sessions for a in s.get("waits", [])}
        return sorted(a for a in paying if whole_number(a, least=0) and self.account_bound(sessions, a))

    def choose(self, kept: list[dict], record: dict) -> tuple[list[dict], str | None, dict[int, int], list[str]]:
        """The places the next session handles, all on one item; or the bound that stops everything.

        The oldest place whose account and item are not held back says which item starts. The session handles the
        places on that item of every account that is not held back, a bounded number of them. Places of accounts that
        are held back are passed over and change nothing of this: they are only counted, for the digest.
        Returns the places, the bound, how many places wait for each held-back account, and the bounds of items met on the way.
        """
        sessions = record["sessions"]
        reached = self.day_bound(sessions)
        if reached:
            return ([], reached, {}, [])
        held = {account: self.account_bound(sessions, account) for account in {place["account"] for place in kept}}
        waiting: dict[int, int] = {}
        met: list[str] = []
        chosen: list[dict] = []
        for place in kept:
            if held[place["account"]]:
                waiting[place["account"]] = waiting.get(place["account"], 0) + 1
                reached = reached or held[place["account"]]
            elif not chosen:
                bound = self.item_bound(record, place["number"])
                if bound:
                    met.append(bound)
                    reached = bound
                else:
                    chosen = [other for other in kept if other["number"] == place["number"] and not held[other["account"]]][:EVENTS_PER_ARRIVAL]
        return (chosen, None if chosen else reached, waiting, met)

    def admit(self, number: int | None = None, kept: list[dict] | None = None) -> tuple[dict | None, str | None, list[dict], dict[int, int]]:
        """Count a session at the moment it starts, with everything it may cost set aside.

        Without kept arrivals it is a session for what was typed. Returns the session's entry in the record (which
        names the payer and the accounts it handles), or the bound that stops it; the places it handles; and how
        many places wait because of their account's own bounds.
        """
        spending = self.spending
        try:
            with self.desk.counts() as record:
                sessions = record["sessions"]
                for session in sessions:
                    if session.get("charged", 0) is None:
                        # No session is running now. One whose cost was never written, because the desk was stopped
                        # while it ran, ended then: it is charged everything that was set aside for it.
                        session["charged"] = session["reserved"]
                if kept is None:
                    places, reached, waiting, met = [], self.day_bound(sessions), {}, []
                else:
                    places, reached, waiting, met = self.choose(kept, record)
                    number = places[0]["number"] if places else None
                for bound in met:
                    if bound == "sessions for one item in a day":
                        self.desk.limit_reached(record, bound)
                if reached is None and (places or kept is None):
                    accounts = newest_first(places)
                    entry = {"t": record["clock"], "id": max([s.get("id", 0) for s in sessions], default=0) + 1, "number": number,
                             "account": accounts[0] if accounts else None, "accounts": accounts, "ended": False, "waits": [],
                             "reserved": reservation(spending), "charged": None, "counts": None}  # fmt: skip
                    held_back = self.held_back(sessions)
                    # From here on this session is the one that is running, and its places are in hand.
                    self.running, self.in_hand = entry["id"], [(place["number"], place["account"]) for place in places]
                    sessions.append(entry)
                    return (dict(entry) | {"held_back": held_back}, None, places, waiting)
                if reached in (SESSIONS, UNITS):
                    # A bound of the day stops everything. The coordinator is told once for each bound, and the next digest names who used the most.
                    self.desk.limit_reached(record, reached)
                    self.desk.day_closed(record, reached, tools.most_used(sessions, by_units=reached == UNITS))
                return (None, reached, [], waiting)
        except tools.Refused:
            return (None, DAMAGED, [], {})

    def settle(self, entry: dict, counts: list[int] | None, charged: float | None, ended: bool = False, waits=(), read: bool = False) -> None:
        """Write what the session cost. Without a count from the service it is charged everything that was set aside.

        ended says that the relay had to end the session at a bound: its item gets no further session for a day, and
        neither do the accounts in waits: the payer, when the session was shown its text. read says that the session was
        shown text of its item: only then does a session that had to be ended count toward closing the item.
        """
        try:
            with self.desk.counts() as record:
                for session in record["sessions"]:
                    if session.get("id") == entry["id"] and session["charged"] is None:
                        session["charged"] = entry["reserved"] if not charged or charged <= 0 else charged
                        # The four counts as the service gave them, so that real days can replace the estimate of a day's cost.
                        session["counts"] = counts
                        session["ended"] = bool(ended)
                        session["waits"] = list(waits) if ended else []
                        session["let_in"] = bool(entry.get("let_in", True))
                        if entry["number"] is not None and session["let_in"]:
                            # Sessions that had to be ended on one item and were shown its text, one after the other. A session that
                            # was not ended begins the count anew; one that was ended before it was shown anything says nothing about the item.
                            row, name = record["ended_in_a_row"], str(entry["number"])
                            if not ended:
                                row.pop(name, None)
                            elif read:
                                row[name] = row.get(name, 0) + 1
                                if row[name] >= self.spending["ended_in_a_row_per_item"]:
                                    # Closed from now on, for a fixed number of days. When it opens again its count stands at one.
                                    record["closed_items"][name] = record["clock"]
                                    row[name] = self.spending["ended_in_a_row_per_item"] - 1
        except tools.Refused:
            pass

    # -- arrivals -----------------------------------------------------------

    def take(self, output: bytes) -> int:
        """Write what the watcher printed into the kept list. Returns how many arrivals it held.

        The placing is one step under the relay's lock: the places are written, and what went is written beside them in
        the same file. Then, outside that lock, what went is counted for its accounts and the notice is tried, and only
        then does it leave the file. A stop between the two leaves it there, and the next placing or start counts it,
        once: the record keeps the names of the placings it counted.
        """
        events, malformed = read_events(output)
        if malformed:
            self.show(f"relay: {malformed} arrival(s) without an account's numeric id or in the wrong shape were not handled")
            self.desk.count("arrivals that could not be read", by=malformed)
        with self.turn:
            closed = self.closed_items()
            # What goes is counted arrival by arrival, for the account that wrote it and with its item.
            gone: list[dict] = []
            for event in events:
                if event["number"] in closed:
                    # No session is started for such an item now, so nothing waits for one: counted for its account like any dropped arrival.
                    gone.append({"account": event["actor_id"], "number": event["number"], "arrivals": 1, "why": "closed"})
            self.kept.put_all([event for event in events if event["number"] not in closed], self.held_back(), list(self.in_hand), gone)
            self.at_placing(POINTS_AT_PLACING[0])
        if gone:
            self.show(f"relay: {sum(went['arrivals'] for went in gone)} arrival(s) were dropped: the list of kept arrivals has no room for them, or their item is closed")
        self.settle_placings()
        return len(events)

    def at_placing(self, point: str) -> None:
        """A point of a placing at which the desk can be stopped. Nothing happens here."""

    def settle_placings(self) -> None:
        """Count what went at placings, once each, for the accounts that wrote it, and try the notice; then forget it.

        An arrival of an account that is held back, dropped for want of a place, is counted for that account and is
        nothing more: no limit of the day was reached by it. Neither is one on a closed item.
        """
        pending = self.kept.pending()
        if not pending:
            return
        try:
            with self.desk.counts() as record:
                for placing in pending:
                    if placing["placing"] in record["placings"]:
                        continue  # Counted before a stop left it in the file.
                    for went in placing["gone"]:
                        tools.Desk._count(record, tools.DROPPED, went["account"], went["arrivals"], went["number"])
                    record["placings"] = (record["placings"] + [placing["placing"]])[-tools.PLACINGS_REMEMBERED :]
                    if placing["notice"]:
                        self.desk.limit_reached(record, "arrivals kept for later; more were dropped")
                        if placing["holders"]:
                            # Every place was taken and no account could give one, so an account that holds nothing would
                            # get none either: only then is the list closed for everyone.
                            self.desk.day_closed(record, PLACES, placing["holders"])
        except tools.Refused:
            return
        self.at_placing(POINTS_AT_PLACING[1])
        self.kept.settled([placing["placing"] for placing in pending])

    def closed_items(self) -> set[int]:
        """The items for which no session is started now."""
        return set(self.closed_since())

    def closed_since(self) -> dict[int, float]:
        """Those items, each with the moment it was closed."""
        try:
            with self.desk.counts() as record:
                return {int(number): at for number, at in record["closed_items"].items()}
        except tools.Refused:
            return {}

    def closed_line(self, number: int, since: float, gone: list[tuple[int, int]]) -> str:
        """The digest's line for an item that closed: the day it opens again, how many arrivals went, and whose."""
        opens = time.strftime("%Y-%m-%d UTC", time.gmtime(since + self.spending["item_closed_days"] * tools.DAY))
        line = (f"Closed until {opens}: two sessions in a row that were shown it had to be ended. "
                f"{sum(arrivals for _, arrivals in gone)} arrival(s) kept on it went")  # fmt: skip
        if gone:
            # The accounts by number, up to eight of them and as many as the line holds, each number whole.
            room = self.desk.lock["coordinator"]["digest_line_chars"] - len(line) - len(", of accounts .")
            line += ", of accounts " + tools.named([account for account, _ in gone], room, 8)
        return line + "."

    def let_go(self, closed: dict[int, float], point: str) -> None:
        """Every arrival kept on these closed items goes, whoever wrote it.

        First each is counted for the account that wrote it and the item's line is written, in one look at the record
        and once for each closing; then the places go from the kept list. A stop between the two leaves them counted
        and kept, and the next start only takes them out. Called at the end of the session that closed an item, and at
        every start, which finds what a stop left kept on a closed item or a closing whose line was not written.
        """
        kept, items = self.kept.entries(), []
        for number, since in sorted(closed.items()):
            gone = [(place["account"], len(place["events"])) for place in kept if place["number"] == number]
            items.append((number, since, gone, self.closed_line(number, since, gone)))
        if items:
            self.desk.closed_items(items)
        self.at(point)
        for number in sorted({place["number"] for place in kept if place["number"] in closed}):
            self.kept.forget(number)

    def start_one(self) -> bool:
        """Start one session: for what was typed, or for the oldest kept arrival that the bounds leave room for."""
        # What went at a placing that a stop left uncounted is counted first.
        self.settle_placings()
        if self.starts_nothing:
            return False
        try:
            text = self.typed_lines.get_nowait()
        except queue.Empty:
            text = None
        if text is not None:
            self.show("typed: " + tools.cut(text, 200))
            entry, reached, _, _ = self.begin(typed=True)
            if entry is None:
                self.show(f"relay: not started ({reached})")
                self.damaged(reached)
                return True
            self.handle("typed", text, None, [], entry, [])
            return True
        entry, reached, places, _ = self.begin()
        if entry is None:
            self.damaged(reached)
            return False
        number = entry["number"]
        # The arrival stays in the kept list, in hand, until the session has dealt with it.
        self.handle(f"item {number}", news_message(told(places)), number, entry["accounts"], entry, places)
        return True

    def begin(self, typed: bool = False) -> tuple[dict | None, str | None, list[dict], dict[int, int]]:
        """The one step at a session's start: what is still kept on a closed item goes, the kept list is read, the
        session is chosen and counted, its places are in hand."""
        self.at(POINTS_AT_START[0])
        with self.turn:
            # What a stop left: arrivals kept on an item that closed, or a closing whose line was not written. Done as
            # the end of the session that closed the item would have done it, and counted once.
            self.let_go(self.closed_since(), POINTS_AT_START[1])
            self.at(POINTS_AT_START[2])
            kept = None if typed else self.kept.entries()
            self.at(POINTS_AT_START[3])
            entry, reached, places, waiting = self.admit(kept=kept)
            if not typed and waiting != self.waiting_written:
                # Arrivals that wait for an account's share are counted for the next digest.
                self.desk.count_as(WAITING, waiting)
                self.waiting_written = waiting
            self.at(POINTS_AT_START[4])
        self.at(POINTS_AT_START[5])
        return entry, reached, places, waiting

    def end(self, entry: dict, places: list[dict], dealt_with: bool, counts: list[int] | None, charged: float | None, **how) -> None:
        """The one step at a session's end, for everything else the relay does.

        The places the session dealt with go out of the kept list, then its cost is written, then what is kept on its
        item is counted and goes if the item closed, and then nothing is in hand and no session is running. On disk
        the handled places go before the cost: a stop between the two leaves a session without a cost, which counts as
        one that ended and was charged everything set aside, and never an arrival that was handled and is handled again.
        """
        self.at(POINTS_AT_END[0])
        with self.turn:
            try:
                if dealt_with:
                    self.kept.done(places)
                self.at(POINTS_AT_END[1])
                self.settle(entry, counts, charged, **how)
                self.at(POINTS_AT_END[2])
                number = entry["number"]
                closed = self.closed_since() if dealt_with and number is not None else {}
                # Every arrival kept on the item goes with it, whoever wrote it: each is counted for its account, and the digest names them.
                self.let_go({number: closed[number]} if number in closed else {}, POINTS_AT_END[3])
                self.at(POINTS_AT_END[4])
            finally:
                self.running, self.in_hand = None, []
            self.at(POINTS_AT_END[5])
        self.at(POINTS_AT_END[6])

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
            return seen | {"ended_by": NOT_STARTED, "not_started": True}
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
                            self.show("  refused: " + tools.cut(json.dumps(part.get("content"), ensure_ascii=False), 300))
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
            seen["error"] = result.get("is_error") is True
            if seen["error"] and not sum(seen["counts"] or [0]) and SIGN_IN_REFUSED.search(str(result.get("result", "")) + " ".join(said)):
                seen["refused_sign_in"] = True
        elif seen["ended_by"] is None:
            seen["ended_by"] = WITHOUT_A_RESULT
            self.show(f"relay: session {self.handled} ended early ({seen['ended_by']})")
        return seen

    def handle(self, about: str, text: str, number: int | None, accounts: list[int], entry: dict, places: list[dict]) -> bool:
        """One fresh session for one message. Returns False when the arrival has to be kept for another try."""
        # The tools of a session for an item refuse every other item; a session for what was typed says so.
        env = marked(self.plan["env"], number, accounts, entry.get("held_back", []))
        if number is not None:
            self.forget_shown()
        self.handled += 1
        if self.log is not None:
            self.log.write(json.dumps({"type": "relay", "session": self.handled, "about": about}) + "\n")
            self.log.flush()
        self.show(f"relay: session {self.handled} ({about})")
        try:
            seen = self.run_session(text, env)
        except BaseException:
            with self.turn:
                self.running, self.in_hand = None, []
            raise
        spending = self.spending
        if seen.get("not_started"):
            # Nothing ran and nothing was spent: the desk itself could not start a session. That is nobody's doing, so
            # it counts against no account and no item; the arrival stays kept, and nothing more is started.
            entry["let_in"] = False
            self.end(entry, places, False, None, 1)
            self.starts_nothing = "a session could not be started"
            self.show("relay: no more sessions are started until the desk is started again (" + self.starts_nothing + ")")
            self.desk_down("A session could not be started. The arrival is kept. No session is started until the desk is started again.")
            self.stop_watching()
            return False
        if seen["refused_sign_in"]:
            # Nothing was spent: the service did not let the session in.
            entry["let_in"] = False
            self.end(entry, places, False, seen["counts"], 1)
            return self.failed("the service refused the desk's sign-in", "The service refused the desk's sign-in to Claude.")
        counts = seen["counts"]
        cost = units(counts, spending["weights"]) if counts else 0
        if seen.get("over_the_base"):
            self.end(entry, places, False, counts, max(cost, entry["reserved"]))
            self.starts_nothing = "a session took in more than the bounds count on"
            self.show("relay: no more sessions are started until the desk is started again (" + self.starts_nothing + ")")
            self.desk_down("The first response of a session took in more than Claude Code's own instructions, the standing instructions and the first "
                           "message may be. The bounds on cost do not hold; no session is started until the desk is started again. The arrival is kept.")
            self.stop_watching()
            return False
        if self.stopping.is_set() and not seen.get("at_a_bound"):
            # The desk is being stopped in the middle of a session: the arrival stays kept, and nobody waits for it.
            self.end(entry, places, False, counts, max(cost, entry["reserved"]))
            return False
        responded = bool(counts) and sum(counts) > 0
        if not responded and (seen["ended_by"] == WITHOUT_A_RESULT or seen.get("error")):
            # Claude Code ended before the service counted any response, without a result or with its own report of an
            # error (the service overloaded, a limit of use reached): the desk itself is not working, as when the service
            # refuses its sign-in. What it may have spent is not known, so the day is charged everything that was set
            # aside, and no account and no item: it counts against nobody's share. The arrival stays kept.
            entry["let_in"] = False
            self.end(entry, places, False, None, entry["reserved"])
            return self.failed("a session ended without any response", "A session of the desk ended without any response.")
        self.failures = 0
        if seen["ended_by"] == WITHOUT_A_RESULT:
            # It responded and ended without a result: charged everything that was set aside, as a session without a count
            # from the service is, to its payer. What it handled stays kept, as after a stop, so that a fresh session
            # handles it again; nobody waits.
            self.desk.note("session_ended", f"A session ended early ({WITHOUT_A_RESULT}) after it had responded. It was about: {about}. What it handled is kept.", number)
            self.end(entry, places, False, counts, max(cost, entry["reserved"]))
            if cost > entry["reserved"]:
                self.desk_down("One session cost more than what is set aside for a session. The bound on one turn does not hold; look at the desk's record.")
            return False
        if seen["ended_by"] is None and cost > spending["units_per_session"]:
            # The count at the end says more than the responses did: past the bound of one session all the same.
            seen["ended_by"], seen["at_a_bound"] = "it cost more than one session may", True
        if seen["ended_by"] is not None:
            # A session the relay had to end is charged everything that was set aside for it. Its payer waits a day
            # when the session was shown the payer's text, and nobody else does.
            ended = bool(seen.get("at_a_bound"))
            shown = self.shown_accounts() if ended and number is not None else []
            self.desk.note("session_ended", f"The relay ended a session early ({seen['ended_by']}). It was about: {about}.", number)
            self.end(entry, places, True, counts, max(cost, entry["reserved"]), ended=ended, waits=[a for a in accounts[:1] if a in shown], read=bool(shown))
        else:
            self.end(entry, places, True, counts, cost)
        if cost > entry["reserved"]:
            self.desk_down("One session cost more than what is set aside for a session. The bound on one turn does not hold; look at the desk's record.")
        return True

    def failed(self, shown: str, line: str) -> bool:
        """A session that the service refused or that gave no response: after three in a row nothing more is started."""
        self.failures += 1
        self.show("relay: " + shown)
        if self.failures == 1:
            self.desk_down(line + " The arrival is kept. After three sessions in a row that the service refused or that gave no response, "
                           "the desk starts no more sessions until it is started again.")  # fmt: skip
        if self.failures >= self.spending["sign_in_failures"]:
            self.starts_nothing = "three sessions in a row were refused by the service or gave no response"
            self.show("relay: no more sessions are started until the desk is started again")
            self.stop_watching()
        return False

    def forget_shown(self) -> None:
        try:
            if state_files.file(self.state / "session" / SHOWN_FILE, "The list of whose text a session was shown"):
                (self.state / "session" / SHOWN_FILE).unlink()
        except (OSError, state_files.StateProblem):
            pass

    def shown_accounts(self) -> list[int]:
        """The accounts whose text the reading tool handed to the session that just ran. Written before the tool answered."""
        try:
            text = state_files.read(self.state / "session" / SHOWN_FILE, "The list of whose text a session was shown")
            return tools.accounts_of(text)
        except state_files.StateProblem:
            return []

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
        state_files.real_folder(self.state, "The state folder")
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
                looked = time.monotonic()
                try:
                    while not self.stopping.is_set():
                        if time.monotonic() - looked >= self.retry_seconds:
                            # The digest's time is looked at before every start, so that a queue of sessions does not hold it up.
                            looked = time.monotonic()
                            if self.desk.digest():
                                self.waiting_written = None
                        seen = self.seen
                        if self.start_one():
                            continue
                        if seen != self.seen:
                            continue  # Something arrived while this look was under way: look again before anything else.
                        self.nothing_to_start.set()
                        if self.exit_when_idle and self.typing_ended.is_set() and self.typed_lines.empty() and (
                            self.handled > 0 or seen > 0 or self.watching_ended.is_set() or self.starts_nothing
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
