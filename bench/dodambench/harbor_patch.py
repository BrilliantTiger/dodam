"""The pinned Harbor and the one change Dodam makes to it.

Harbor 0.25.0 starts every command in a task container with `docker compose exec -e NAME=VALUE`, which puts the
agent's environment, the subscription token among it, on the command line of host processes. The patch in
bench/harbor/ makes Harbor pass each variable by name only and hand the value to the docker CLI through that
process's environment. This module applies the patch to an install and checks one; the pilot runs the check before
it reads the token.

Set up the install once, from bench/:

    uv venv --python 3.13 .harbor-venv
    uv pip install --python .harbor-venv/bin/python harbor==0.25.0
    python -m dodambench.harbor_patch apply
    python -m dodambench.harbor_patch check
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sys
from dataclasses import dataclass
from pathlib import Path

BENCH = Path(__file__).resolve().parents[1]
DEFAULT_VENV = BENCH / ".harbor-venv"
PINNED_VERSION = "0.25.0"
PATCH_FILE = BENCH / "harbor" / "docker-exec-env-by-name.patch"
TARGET = Path("harbor/environments/docker/docker.py")
# sha256 of TARGET as released in harbor 0.25.0 (the wheel's RECORD agrees), and after the patch.
ORIGINAL_SHA256 = "4f74e08941b3534864643405dd6ff43543597248539d7f5de7208ec5c2f5b511"
PATCHED_SHA256 = "095542a4ef023e18963d3c096b5c2cc1b14ce2991847079019089210333f3e87"
HUNK = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class HarborSetupError(Exception):
    pass


@dataclass
class Installed:
    venv: Path
    site_packages: Path
    version: str | None
    target: Path
    sha256: str | None


def venv_of(harbor: Path) -> Path:
    """The virtual environment whose Python runs the `harbor` entry script (from its #! line)."""
    try:
        with open(harbor, "rb") as f:
            first = f.readline()
    except OSError as err:
        raise HarborSetupError(f"cannot read {harbor}: {err.strerror}") from None
    words = first[2:].split() if first.startswith(b"#!") else []
    if not words:
        raise HarborSetupError(f"{harbor} is not a Python entry script")
    venv = Path(words[0].decode("utf-8", "replace")).parent.parent
    if not (venv / "pyvenv.cfg").is_file():
        raise HarborSetupError(f"{harbor} does not run from a virtual environment")
    return venv


def inspect(venv: Path) -> Installed:
    sites = [p for p in sorted(Path(venv).glob("lib/python3*/site-packages")) if (p / "harbor").is_dir()]
    if len(sites) != 1:
        raise HarborSetupError(f"expected one site-packages with harbor in {venv}, found {len(sites)}")
    site = sites[0]
    infos = sorted(site.glob("harbor-*.dist-info"))
    if len(infos) != 1:
        raise HarborSetupError(f"expected one installed harbor in {site}, found {len(infos)}")
    version = None
    try:
        for line in (infos[0] / "METADATA").read_text(encoding="utf-8").splitlines():
            if line.startswith("Version:"):
                version = line.split(":", 1)[1].strip()
                break
    except OSError:
        pass
    target = site / TARGET
    digest = hashlib.sha256(target.read_bytes()).hexdigest() if target.is_file() else None
    return Installed(Path(venv), site, version, target, digest)


def check(harbor: Path, version: str = PINNED_VERSION, original_sha256: str = ORIGINAL_SHA256,
          patched_sha256: str = PATCHED_SHA256) -> Installed:
    """The install behind `harbor`, when it is the pinned release with the patch; an error otherwise."""
    installed = inspect(venv_of(Path(harbor).expanduser()))
    if installed.version != version:
        raise HarborSetupError(f"Harbor {installed.version} is installed in {installed.venv}; "
                               f"the pilot runs only with Harbor {version}")
    if installed.sha256 == original_sha256:
        raise HarborSetupError(f"Harbor in {installed.venv} is not patched, so it would put the agent's environment "
                               f"on host command lines; run `python -m dodambench.harbor_patch apply --venv "
                               f"{installed.venv}`")
    if installed.sha256 != patched_sha256:
        raise HarborSetupError(f"{installed.target} is neither the released file nor the patched one; "
                               f"reinstall harbor=={version} and apply the patch")
    return installed


def apply_unified(text: str, patch: str) -> str:
    """Apply a one-file unified diff whose context and removed lines must match exactly where the hunks say."""
    src = text.splitlines(keepends=True)
    out: list[str] = []
    pos = 0
    lines = patch.splitlines(keepends=True)
    i = 0
    while i < len(lines):
        m = HUNK.match(lines[i])
        i += 1
        if not m:
            continue
        old_start, old_len = int(m.group(1)), int(m.group(2) or "1")
        start = old_start - 1 if old_len else old_start
        if start < pos or start > len(src):
            raise HarborSetupError("the patch does not fit the file")
        out += src[pos:start]
        pos = start
        while i < len(lines) and lines[i][:1] in (" ", "-", "+", "\\"):
            kind, body = lines[i][:1], lines[i][1:]
            i += 1
            if kind == "\\":
                raise HarborSetupError("the patch touches a file end without a newline")
            if kind in (" ", "-"):
                if pos >= len(src) or src[pos] != body:
                    raise HarborSetupError(f"the patch does not fit the file at line {pos + 1}")
                pos += 1
                if kind == " ":
                    out.append(body)
            else:
                out.append(body)
    return "".join(out + src[pos:])


def apply(venv: Path = DEFAULT_VENV, patch_file: Path = PATCH_FILE, version: str = PINNED_VERSION,
          original_sha256: str = ORIGINAL_SHA256, patched_sha256: str = PATCHED_SHA256) -> str:
    """Patch the released file of the pinned Harbor in `venv`. Anything else is refused and left alone."""
    installed = inspect(Path(venv))
    if installed.version != version:
        raise HarborSetupError(f"Harbor {installed.version} is installed in {installed.venv}; the patch is for {version}")
    if installed.sha256 == patched_sha256:
        return "already patched"
    if installed.sha256 != original_sha256:
        raise HarborSetupError(f"{installed.target} is not the file released in Harbor {version}; "
                               f"reinstall harbor=={version} first")
    before = installed.target.read_bytes()
    after = apply_unified(before.decode("utf-8"), Path(patch_file).read_text(encoding="utf-8")).encode("utf-8")
    if hashlib.sha256(after).hexdigest() != patched_sha256:
        raise HarborSetupError("the patched file is not the pinned result; nothing was changed")
    installed.target.write_bytes(after)
    if hashlib.sha256(installed.target.read_bytes()).hexdigest() != patched_sha256:
        installed.target.write_bytes(before)
        raise HarborSetupError("the patched file did not read back as written; the released file was restored")
    return "applied"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="command", required=True)
    a = sub.add_parser("apply", help="patch the pinned Harbor in a virtual environment")
    a.add_argument("--venv", type=Path, default=DEFAULT_VENV)
    c = sub.add_parser("check", help="check that a harbor command is the pinned, patched release")
    c.add_argument("--harbor", type=Path, default=DEFAULT_VENV / "bin" / "harbor")
    args = p.parse_args(argv)
    try:
        if args.command == "apply":
            print(f"{apply(args.venv)}: {inspect(args.venv).target}")
        else:
            installed = check(args.harbor)
            print(f"ok: Harbor {installed.version} with the patch, in {installed.venv}")
    except HarborSetupError as err:
        print(f"harbor_patch: {err}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
