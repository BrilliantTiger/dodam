"""Stand-ins for the container command, for the container machine's command and for the readings of this
computer, so that the frame's tests run without a container. Each keeps what it was asked in a folder named by
FRAME_STANDIN, and what it answers is set by the files a test leaves there."""
from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

DOCKER = r'''
import json, os, secrets, sys
DATA = os.environ["FRAME_STANDIN"]
args = sys.argv[1:]
with open(os.path.join(DATA, "calls.jsonl"), "a", encoding="utf-8") as handle:
    handle.write(json.dumps(args) + "\n")
with open(os.path.join(DATA, "envs.jsonl"), "a", encoding="utf-8") as handle:
    handle.write(json.dumps({k: v for k, v in os.environ.items() if k.startswith(("DOCKER", "BUILDX"))}) + "\n")
path = os.path.join(DATA, "containers.json")
held = json.load(open(path, encoding="utf-8")) if os.path.exists(path) else []
vpath = os.path.join(DATA, "volumes.json")
volumes = json.load(open(vpath, encoding="utf-8")) if os.path.exists(vpath) else []


def save():
    json.dump(held, open(path, "w", encoding="utf-8"))
    json.dump(volumes, open(vpath, "w", encoding="utf-8"))


def options(rest):
    labels, cidfile, i = {}, None, 0
    global named
    named = None
    while i < len(rest) and rest[i].startswith("-"):
        word = rest[i]
        if word in ("--label", "-l"):
            key, _, value = rest[i + 1].partition("=")
            labels[key] = value
            i += 2
        elif word == "--cidfile":
            cidfile = rest[i + 1]
            i += 2
        elif word == "--name":
            named = rest[i + 1]
            i += 2
        elif word in ("--rm", "--detach", "-d"):
            i += 1
        else:
            i += 2
    return labels, cidfile


if os.path.exists(os.path.join(DATA, "down")):
    sys.stderr.write("Cannot connect to the Docker daemon\n")
    sys.exit(1)
if args[:1] == ["container"]:
    args = args[1:]
if args[:1] in (["create"], ["run"]):
    labels, cidfile = options(args[1:])
    ident = secrets.token_hex(32)
    if cidfile:
        if os.path.exists(cidfile):
            sys.stderr.write("cidfile exists\n")
            sys.exit(125)
        open(cidfile, "w").write(ident)
    held.append({"id": ident, "name": named, "labels": labels, "running": args[0] == "run" and "--rm" not in args})
    save()
    print(ident)
elif args[:1] == ["start"]:
    print(args[-1])
elif args[:2] == ["rm", "--force"]:
    named = {a for a in args[2:] if a != "--"}
    held = [c for c in held if c["id"] not in named and c.get("name") not in named]
    save()
elif args[:1] in (["stop"], ["cp"]):
    pass
elif args[:1] == ["ps"]:
    want = None
    if "--filter" in args:
        want = args[args.index("--filter") + 1].removeprefix("label=").partition("=")
    every = "-a" in args or "--all" in args or "-aq" in args
    for c in held:
        if want and c["labels"].get(want[0]) != want[2]:
            continue
        if not every and not c["running"]:
            continue
        print(c["id"])
elif args[:1] == ["inspect"]:
    for name in args[1:]:
        if name.startswith("-") or name.startswith("{{") or name in ("container", "--"):
            continue
        found = [c for c in held if c["id"] == name or c.get("name") == name]
        if not found:
            sys.exit(1)
        print(found[0]["labels"].get("dodam.frame.run", ""))
elif args[:2] == ["volume", "create"]:
    labels, _ = options(args[2:])
    volumes.append({"name": args[-1], "labels": labels})
    save()
    print(args[-1])
elif args[:2] == ["volume", "inspect"]:
    found = [v for v in volumes if v["name"] == args[-1]]
    if not found:
        sys.exit(1)
    print(found[0]["labels"].get("dodam.frame.run", ""))
elif args[:2] == ["volume", "rm"]:
    if os.path.exists(os.path.join(DATA, "volume-rm-fails")):
        sys.exit(1)
    named = {a for a in args[2:] if not a.startswith("-")}
    volumes = [v for v in volumes if v["name"] not in named]
    save()
else:
    print("other")
'''

COLIMA = r'''
import json, os, sys
DATA = os.environ["FRAME_STANDIN"]
with open(os.path.join(DATA, "colima.jsonl"), "a", encoding="utf-8") as handle:
    handle.write(json.dumps(sys.argv[1:]) + "\n")
if sys.argv[1:2] == ["status"]:
    sys.exit(0 if os.path.exists(os.path.join(DATA, "machine-running")) else 1)
'''

SYSCTL = r'''
import os, sys
DATA = os.environ["FRAME_STANDIN"]
place = os.path.join(DATA, "pressure")
once = os.path.join(DATA, "pressure-fail-once")
if os.path.exists(once):
    os.unlink(once)
    sys.exit(1)
if not os.path.exists(place):
    sys.exit(1)
print(open(place).read().strip())
'''

PGREP = r'''
import os, sys
DATA = os.environ["FRAME_STANDIN"]
place = os.path.join(DATA, "fseventsd-pids")
pids = open(place).read().split() if os.path.exists(place) else []
print("\n".join(pids))
sys.exit(0 if pids else 1)
'''

TOP = r'''
import os, sys
DATA = os.environ["FRAME_STANDIN"]
pid = sys.argv[sys.argv.index("-pid") + 1]
place = os.path.join(DATA, f"mem-{pid}")
if not os.path.exists(place):
    sys.exit(1)
print("Processes: 600 total\nMEM\n" + open(place).read().strip())
'''


def _program(path: Path, body: str) -> None:
    path.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


class Standins:
    """A folder with the stand-in programs on a PATH of their own, and the answers they give."""

    def __init__(self, root: Path):
        self.root = root
        self.data = root / "data"
        self.bin = root / "bin"
        self.data.mkdir(parents=True)
        self.bin.mkdir(parents=True)
        for name, body in (("docker", DOCKER), ("colima", COLIMA), ("sysctl", SYSCTL), ("pgrep", PGREP), ("top", TOP)):
            _program(self.bin / name, body)
        self.pressure(1)
        self.fseventsd({"301": "120M"})
        (self.data / "machine-running").write_text("")

    def env(self) -> dict[str, str]:
        return {"FRAME_STANDIN": str(self.data), "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}"}

    def pressure(self, level: int | None) -> None:
        place = self.data / "pressure"
        if level is None:
            place.unlink(missing_ok=True)
        else:
            place.write_text(f"{level}\n")

    def fseventsd(self, memory: dict[str, str]) -> None:
        for old in self.data.glob("mem-*"):
            old.unlink()
        (self.data / "fseventsd-pids").write_text("\n".join(memory))
        for pid, said in memory.items():
            (self.data / f"mem-{pid}").write_text(said)

    def envs(self) -> list[dict]:
        place = self.data / "envs.jsonl"
        return [json.loads(line) for line in place.read_text().splitlines()] if place.exists() else []

    def tools(self) -> dict[str, str]:
        """The stand-ins by the names the frame knows its tools by."""
        return {name: str(self.bin / name) for name in ("docker", "colima", "sysctl", "pgrep", "top")}

    def calls(self) -> list[list[str]]:
        place = self.data / "calls.jsonl"
        return [json.loads(line) for line in place.read_text().splitlines()] if place.exists() else []

    def machine_calls(self) -> list[list[str]]:
        place = self.data / "colima.jsonl"
        return [json.loads(line) for line in place.read_text().splitlines()] if place.exists() else []

    def containers(self) -> list[dict]:
        place = self.data / "containers.json"
        return json.loads(place.read_text()) if place.exists() else []

    def add_container(self, ident: str, labels: dict[str, str], running: bool = True, name: str | None = None) -> None:
        held = self.containers() + [{"id": ident, "name": name, "labels": labels, "running": running}]
        (self.data / "containers.json").write_text(json.dumps(held))

    def volumes(self) -> list[dict]:
        place = self.data / "volumes.json"
        return json.loads(place.read_text()) if place.exists() else []

    def add_volume(self, name: str, labels: dict[str, str]) -> None:
        held = self.volumes() + [{"name": name, "labels": labels}]
        (self.data / "volumes.json").write_text(json.dumps(held))
