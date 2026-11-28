"""Does the kernel keep a stop signal pending on a process that already stands stopped, until it is continued?

Run straight on the runner, no container. Every expected value is written out below, before any run; the script
prints every reading and exits 1 on any difference from what is expected.
"""
import os
import platform
import signal
import subprocess
import sys
import time

SIGSTOP, SIGCONT = signal.SIGSTOP, signal.SIGCONT  # 19, 18 on Linux
assert (int(SIGSTOP), int(SIGCONT)) == (19, 18), (SIGSTOP, SIGCONT)

differences = []


def status(pid):
    """State letter, and the signals pending on the process (shared) and on its first thread (own), from one read."""
    text = open(f"/proc/{pid}/status").read()
    fields = dict(line.split(":", 1) for line in text.splitlines() if ":" in line)
    state = fields["State"].split()[0]
    shared = int(fields["ShdPnd"].strip(), 16)
    own = int(fields["SigPnd"].strip(), 16)

    def signals(mask):
        return sorted(n + 1 for n in range(64) if mask >> n & 1)
    return state, signals(shared), signals(own)


def expect(what, seen, wanted):
    ok = seen == wanted
    print(f"{'ok  ' if ok else 'DIFF'} {what}: seen {seen}, expected {wanted}")
    if not ok:
        differences.append(what)


def wait_state(pid, letter, bound=5.0):
    began = time.monotonic()
    while time.monotonic() < began + bound:
        if status(pid)[0] == letter:
            return time.monotonic() - began
        time.sleep(0.0005)
    return None


SLEEPER = "import time; time.sleep(120)"
SELF_STOP = "import os, sys, time; os.kill(os.getpid(), int(sys.argv[1])); time.sleep(120)"
TWO_THREADS = ("import threading, time; threading.Thread(target=time.sleep, args=(120,), daemon=True).start(); "
               "time.sleep(120)")


def child(code, *args):
    # a process group of its own in this session, so the group is not orphaned and the kernel does not discard the
    # terminal's stop signals (20, 21, 22) for it
    p = subprocess.Popen([sys.executable, "-c", code, *map(str, args)], process_group=0)
    time.sleep(0.3)
    return p


def end(p):
    p.kill()
    p.wait()


def stopped_then_stopped_again(label, code, first, from_outside_first=True):
    """The first stop (from outside, or the child's own), then 19 from outside, then the continue."""
    p = child(code, first) if not from_outside_first else child(code)
    if from_outside_first:
        os.kill(p.pid, first)
    took = wait_state(p.pid, "T")
    expect(f"{label}: stands stopped after the first stop ({first})", took is not None, True)
    time.sleep(0.05)
    state, shared, own = status(p.pid)
    expect(f"{label}: after the first stop took effect, nothing pending", (state, shared, own), ("T", [], []))
    os.kill(p.pid, SIGSTOP)
    time.sleep(0.05)
    expect(f"{label}: 19 again while stopped, pending on the process", status(p.pid), ("T", [19], []))
    time.sleep(0.5)
    expect(f"{label}: and still pending half a second later", status(p.pid), ("T", [19], []))
    os.kill(p.pid, SIGCONT)
    took = wait_state(p.pid, "S")
    time.sleep(0.05)
    state, shared, own = status(p.pid)
    expect(f"{label}: after the continue, nothing pending and not stopped", (state != "T", shared, own), (True, [], []))
    end(p)


print("kernel:", platform.release(), "| python:", platform.python_version())
for path in ("/sys/class/dmi/id/sys_vendor", "/sys/class/dmi/id/product_name"):
    try:
        print(path, open(path).read().strip())
    except OSError:
        pass

# a, b, c: a sleeping child, stopped by 19, then 19 again, then continued
stopped_then_stopped_again("a-c, 19 from outside", SLEEPER, SIGSTOP)
# d: a child that stops itself with 19, then 19 from outside
stopped_then_stopped_again("d, the child's own 19", SELF_STOP, SIGSTOP, from_outside_first=False)
# e: b and d with 20, 21 and 22 as the first stop
for first in (signal.SIGTSTP, signal.SIGTTIN, signal.SIGTTOU):
    stopped_then_stopped_again(f"e, {int(first)} from outside", SLEEPER, int(first))
    stopped_then_stopped_again(f"e, the child's own {int(first)}", SELF_STOP, int(first), from_outside_first=False)
# f: a child with a second thread, both asleep: the state and pending signals read are the process's (first thread's)
stopped_then_stopped_again("f, two threads", TWO_THREADS, SIGSTOP)

# g: 200 times, a child stopped by 19, read as fast as one read allows from the signal to the state T and on for
# 20 ms: no reading may show T with 19 still pending (the false alarm a watcher would have)
false_alarms, to_t, gone_after, readings = 0, [], [], 0
for turn in range(200):
    p = child(SLEEPER)
    sent = time.monotonic()
    os.kill(p.pid, SIGSTOP)
    seen_t = cleared = None
    while time.monotonic() < sent + 2.0:
        state, shared, own = status(p.pid)
        readings += 1
        now = time.monotonic() - sent
        if state == "T" and 19 in shared + own:
            false_alarms += 1
        if 19 not in shared + own and cleared is None:
            cleared = now
        if state == "T" and seen_t is None:
            seen_t = now
        if seen_t is not None and now > seen_t + 0.02:
            break
    to_t.append(seen_t if seen_t is not None else -1)
    gone_after.append(cleared if cleared is not None else -1)
    end(p)
print(f"g: {readings} readings in 200 turns; first T after (s) min {min(to_t):.6f} median "
      f"{sorted(to_t)[100]:.6f} max {max(to_t):.6f}; 19 no longer pending after (s) max {max(gone_after):.6f}")
expect("g: every turn reached T within 2 s", min(to_t) >= 0, True)
expect("g: readings with T and 19 still pending", false_alarms, 0)

print("differences:", differences or "none")
sys.exit(1 if differences else 0)
