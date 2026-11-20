"""The pinned Harbor and the one change Dodam makes to it.

Harbor 0.25.0 starts every command in a task container with `docker compose exec -e NAME=VALUE`, which puts the
agent's environment, the subscription token among it, on the command line of host processes. The patch in
bench/harbor/ makes Harbor pass the credential names in CREDENTIALS_BY_NAME, and only those, by name, and hand their
values to the docker CLI through that process's environment; every other variable goes exactly as before. This
module applies the patch to an install and checks one; the pilot runs the check before it reads the token.

What the check is for: it keeps the pilot from using a wrong, unpatched or drifted Harbor by accident. It is not a
defence against a hostile process of the same user, which could read the token file itself. Within that purpose it
pins what runs as Harbor's own code with the token in its environment: every file of the harbor package (the
patched one by its patched hash, all the others by one digest of the release), the `harbor` entry script (by its
structure, since installers write it from their own templates, with nothing after the interpreter on its #! line),
the files that run whenever that Python starts (.pth files, sitecustomize and usercustomize in any form), and a
virtual environment that does not also load the system's site-packages. Harbor's dependencies run with the token in
their environment but build no command lines. Their files are pinned by hash when installed, from
bench/harbor/requirements.txt, and the check pins their versions: every installed package that the file lists must
have the version the file gives, so an install made from an earlier file, with a dependency since moved off a known
vulnerability, is refused. The installed packages are found and read as Python's own metadata reader finds and reads
them, and a package record whose name or version cannot be read is refused. Out of scope, for the same reason as
above: the dependencies' files after installing, the Python interpreter, compiled files in __pycache__, and anything
else that needs write access to the install.

Set up the install once, from bench/ (to move from an earlier version of the patch, install again first):

    python3.13 -m venv .harbor-venv
    .harbor-venv/bin/python -m pip install --require-hashes --only-binary=:all: -r harbor/requirements.txt
    python -m dodambench.harbor_patch apply
    python -m dodambench.harbor_patch check
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import re
import sys
from dataclasses import dataclass
from importlib.metadata import distributions
from pathlib import Path

BENCH = Path(__file__).resolve().parents[1]
DEFAULT_VENV = BENCH / ".harbor-venv"
PINNED_VERSION = "0.25.0"
PATCH_FILE = BENCH / "harbor" / "docker-exec-env-by-name.patch"
REQUIREMENTS = BENCH / "harbor" / "requirements.txt"
PIN = re.compile(r"(?m)^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;\\]+)")
TARGET = Path("harbor/environments/docker/docker.py")
# sha256 of TARGET as released in harbor 0.25.0 (the wheel's RECORD agrees), and after the patch.
ORIGINAL_SHA256 = "4f74e08941b3534864643405dd6ff43543597248539d7f5de7208ec5c2f5b511"
PATCHED_SHA256 = "4427a5dd197a6ab5f7a401038df5f6bbc421e97235265fed32fc49d01df20f61"
# The names the patched Harbor passes by name only (the list in the patch); every other name goes as NAME=VALUE.
CREDENTIALS_BY_NAME = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
# sha256 over "<path> <sha256>" lines, sorted, of every other file of the harbor package as released in 0.25.0
# (paths relative to site-packages, __pycache__ left out): 514 files.
PACKAGE_REST_SHA256 = "8d83a9486ed6829982fa6a427642bb5d31d7b6eb358bc20f33452186d8ea401f"
# What the `harbor` entry script may contain after its #! line. Installers write it from their own templates, which
# change between versions (uv, pip 25 and pip 26 all differ), so it is checked by structure, not by bytes: it may
# import sys and re, import `app` from harbor.cli.main, tidy sys.argv[0] (assigning to nothing else, under no
# condition but the two installers use) and call sys.exit(app()). Nothing else.
ENTRY_NAMES = {"__name__", "sys", "re", "app"}
ENTRY_ATTRIBUTES = {"argv", "exit", "endswith", "removesuffix", "sub"}
ENTRY_NODES = (ast.Module, ast.Import, ast.ImportFrom, ast.alias, ast.If, ast.Compare, ast.Eq, ast.Name, ast.Load,
               ast.Store, ast.Constant, ast.Attribute, ast.Subscript, ast.Slice, ast.UnaryOp, ast.USub, ast.Call,
               ast.Assign, ast.Expr)
# The only files allowed to run code whenever Harbor's Python starts: uv's virtual environment hook.
STARTUP_FILES = {"_virtualenv.pth": "69ac3d8f27e679c81b94ab30b3b56e9cd138219b1ba94a1fa3606d5a76a1433d",
                 "_virtualenv.py": "cfb3db86aaa53bb62b5ff764970bec2d71c9228590a0ebec57f6ec926cc0bf1a"}
STARTUP_MODULES = ("sitecustomize", "usercustomize")  # as a .py file, a package or an extension module
_ARGV0_STORE = ast.dump(ast.parse("sys.argv[0] = x").body[0].targets[0])
_ARGV0_LOAD = ast.dump(ast.parse("sys.argv[0]", mode="eval").body)
_MAIN_TEST = ast.dump(ast.parse("__name__ == '__main__'", mode="eval").body)
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
    if Path(words[0].decode("utf-8", "replace")).name in ("sh", "bash"):
        raise HarborSetupError(f"{harbor} starts its Python through {words[0].decode('utf-8', 'replace')}, which "
                               "installers do when the interpreter's path is long or has a space in it; the check "
                               "needs the interpreter on the #! line, so make the virtual environment at a shorter "
                               "path without spaces and install Harbor into it again")
    if len(words) > 1:
        raise HarborSetupError(f"{harbor} gives its interpreter more than a path on its #! line")
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


def package_rest_digest(site: Path) -> str:
    """The digest PACKAGE_REST_SHA256 pins, of the installed harbor package."""
    lines = []
    for path in (Path(site) / "harbor").rglob("*"):
        rel = path.relative_to(site).as_posix()
        if path.is_file() and "__pycache__" not in path.parts and rel != TARGET.as_posix():
            lines.append(f"{rel} {hashlib.sha256(path.read_bytes()).hexdigest()}\n")
    return hashlib.sha256("".join(sorted(lines)).encode()).hexdigest()


def entry_script_problem(text: str) -> str | None:
    """Why the entry script's body (after the #! line) is not one an installer writes for Harbor, or None."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return "it is not Python"
    for node in ast.walk(tree):
        if not isinstance(node, ENTRY_NODES):
            return f"it contains {type(node).__name__}"
        if isinstance(node, ast.Name) and node.id not in ENTRY_NAMES:
            return f"it uses {node.id}"
        if isinstance(node, ast.Attribute) and node.attr not in ENTRY_ATTRIBUTES:
            return f"it uses .{node.attr}"
        if isinstance(node, ast.Import) and any(a.name not in ("sys", "re") or a.asname for a in node.names):
            return "it imports something other than sys and re"
        if isinstance(node, ast.ImportFrom) and (node.module != "harbor.cli.main" or node.level
                                                 or [(a.name, a.asname) for a in node.names] != [("app", None)]):
            return "it imports something other than harbor.cli.main's app"
        if isinstance(node, ast.Assign) and [ast.dump(t) for t in node.targets] != [_ARGV0_STORE]:
            return "it assigns to something other than sys.argv[0]"
        if isinstance(node, ast.If) and not (ast.dump(node.test) == _MAIN_TEST or _is_suffix_test(node.test)):
            return "it runs something under a condition of its own"
    calls = [ast.dump(n) for n in ast.walk(tree) if isinstance(n, ast.Call)]
    if ast.dump(ast.parse("sys.exit(app())").body[0].value) not in calls:
        return "it does not run harbor.cli.main's app"
    return None


def _is_suffix_test(test: ast.expr) -> bool:
    """`sys.argv[0].endswith("<text>")`, the condition installers write around tidying sys.argv[0]."""
    return (isinstance(test, ast.Call) and isinstance(test.func, ast.Attribute) and test.func.attr == "endswith"
            and ast.dump(test.func.value) == _ARGV0_LOAD and not test.keywords and len(test.args) == 1
            and isinstance(test.args[0], ast.Constant) and isinstance(test.args[0].value, str))


def startup_problems(site: Path) -> list[str]:
    """Files in site-packages that would run code in Harbor's Python and are not the known ones."""
    site = Path(site)
    names = {p.name for p in site.glob("*.pth")} | {n for n in STARTUP_FILES if (site / n).exists()}
    names |= {p.name for p in site.iterdir() if p.name.split(".")[0] in STARTUP_MODULES}
    problems = []
    for name in sorted(names):
        want = STARTUP_FILES.get(name)
        if name == "distutils-precedence.pth":
            problems.append(f"{site / name} comes from setuptools, which Harbor's pinned install does not include; "
                            "make a fresh virtual environment and install Harbor into it from "
                            "bench/harbor/requirements.txt")
        elif want is None:
            problems.append(f"{site / name} would run code whenever Harbor's Python starts")
        elif hashlib.sha256((site / name).read_bytes()).hexdigest() != want:
            problems.append(f"{site / name} is not the known virtual environment hook")
    return problems


def normalized(name: str) -> str:
    """A package name as installers compare names: case, '-', '_' and '.' make no difference."""
    return re.sub(r"[-_.]+", "-", name).lower()


def pinned_versions(requirements: Path) -> dict[str, str]:
    """The version the pinned file gives each package it lists, by normalized name."""
    return {normalized(m.group(1)): m.group(2) for m in PIN.finditer(Path(requirements).read_text(encoding="utf-8"))}


def package_problems(site: Path, pins: dict[str, str]) -> list[str]:
    """Installed packages that the pinned file lists with another version, and package records whose name or version
    cannot be read. The records (.dist-info and .egg-info) are found and read by Python's own metadata reader, as the
    Python that runs Harbor finds and reads them."""
    problems = []
    for dist in distributions(path=[str(site)]):
        record = getattr(dist, "_path", site)  # the record's folder or file, only to name it
        try:
            metadata = dist.metadata
            names = [] if metadata is None else [v.strip() for v in metadata.get_all("Name") or []]
            versions = [] if metadata is None else [v.strip() for v in metadata.get_all("Version") or []]
        except (OSError, ValueError):  # ValueError covers bytes that are not UTF-8
            names = versions = []
        if len(names) != 1 or len(versions) != 1 or not names[0] or not versions[0]:
            problems.append(f"the package record {record} has no single readable name and version")
            continue
        want = pins.get(normalized(names[0]))
        if want is not None and versions[0] != want:
            problems.append(f"{names[0]} {versions[0]} is installed in {site}, "
                            f"where bench/harbor/requirements.txt pins {want}")
    return sorted(problems)


def check(harbor: Path, version: str = PINNED_VERSION, original_sha256: str = ORIGINAL_SHA256,
          patched_sha256: str = PATCHED_SHA256) -> Installed:
    """The install behind `harbor`, when it is the pinned release with the patch; an error otherwise."""
    harbor = Path(harbor).expanduser()
    venv = venv_of(harbor)
    config = (venv / "pyvenv.cfg").read_text(encoding="utf-8", errors="replace")
    if re.search(r"(?mi)^\s*include-system-site-packages\s*=\s*true\s*$", config):
        raise HarborSetupError(f"{venv} also loads the system's site-packages, which the check does not cover")
    installed = inspect(venv)
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
    if package_rest_digest(installed.site_packages) != PACKAGE_REST_SHA256:
        raise HarborSetupError(f"the harbor package in {installed.site_packages} differs from the release of "
                               f"Harbor {version} (a changed, added or missing file); reinstall harbor=={version}")
    problem = entry_script_problem(harbor.read_bytes().split(b"\n", 1)[-1].decode("utf-8", "replace"))
    if problem:
        raise HarborSetupError(f"{harbor} is not the entry script an installer writes for Harbor {version}: {problem}")
    problems = startup_problems(installed.site_packages)
    if problems:
        raise HarborSetupError("; ".join(problems))
    problems = package_problems(installed.site_packages, pinned_versions(REQUIREMENTS))
    if problems:
        raise HarborSetupError("; ".join(problems) + "; make a fresh virtual environment and install Harbor into it "
                               "from bench/harbor/requirements.txt")
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
    if package_rest_digest(installed.site_packages) != PACKAGE_REST_SHA256:
        raise HarborSetupError(f"the harbor package in {installed.site_packages} differs from the release of "
                               f"Harbor {version}; reinstall harbor=={version} first")
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
