"""The pinned Harbor, Dodam's patch to it, and the check the pilot runs before it hands Harbor the token.

The first tests run Harbor itself and need a Harbor install: bench/.harbor-venv, or the virtual environment named by
DODAM_HARBOR_VENV. Without one they are skipped (CI installs no Harbor). The rest build fake installs in a temporary
folder. The secret below is a made-up string.
"""
import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

try:
    from dodambench import harbor_patch
except ImportError:  # each test then fails on its own instead of stopping the whole run
    harbor_patch = None

BENCH = Path(__file__).resolve().parents[1]
HARBOR_VENV = Path(os.environ.get("DODAM_HARBOR_VENV") or BENCH / ".harbor-venv")
PROBE = Path(__file__).with_name("harbor_exec_probe.py")
FAKE_SECRET = "sk-ant-oat01-PROBE0123456789abcdefghijklmnopqrstuvwxyzPROBE"
HOST_PATH = "/usr/local/bin:/usr/bin:/bin"

needs_harbor = pytest.mark.skipif(not (HARBOR_VENV / "bin" / "python").exists(),
                                  reason=f"no Harbor install at {HARBOR_VENV}")


# Host values of names the docker CLI reads for itself; the probe asks Harbor to set other values in the container.
HOST_CLI_ENV = {"HTTPS_PROXY": "http://host-proxy:3128", "OTEL_EXPORTER_OTLP_ENDPOINT": "http://host:4318",
                "XDG_CONFIG_HOME": "/host/config", "TMPDIR": "/host/tmp", "GODEBUG": "host=1",
                "BUILDKIT_PROGRESS": "host"}


def probe() -> dict:
    out = subprocess.run([str(HARBOR_VENV / "bin" / "python"), "-I", str(PROBE)], capture_output=True, text=True,
                         timeout=120, env={"DODAM_PROBE_SECRET": FAKE_SECRET, "PATH": HOST_PATH,
                                           "HOME": os.environ.get("HOME", "/tmp"), **HOST_CLI_ENV})
    assert out.returncode == 0, out.stderr[-2000:]
    return json.loads(out.stdout.strip().splitlines()[-1])


def flags(argv: list[str]) -> list[str]:
    return [argv[i + 1] for i, a in enumerate(argv[:-1]) if a == "-e"]


# --- Harbor itself -------------------------------------------------------------------------------------------

@needs_harbor
def test_harbor_passes_exec_variables_by_name_and_keeps_their_values_off_the_command_line():
    out = probe()
    assert [a for a in out["argv"] if FAKE_SECRET in a] == []
    assert out["harbor_version"] == "0.25.0"
    assert "DODAM_PROBE_SECRET_VALUE" in flags(out["argv"]) and "DODAM_PROBE_EMPTY" in flags(out["argv"])
    # docker compose reads the value of a bare `-e NAME` from its own environment
    assert out["env"]["DODAM_PROBE_SECRET_VALUE"] == FAKE_SECRET
    assert out["env"]["DODAM_PROBE_EMPTY"] == ""


@needs_harbor
def test_names_the_docker_cli_reads_itself_keep_the_host_value():
    out = probe()
    assert "PATH=/container/bin" in flags(out["argv"])
    assert out["env"]["PATH"] == HOST_PATH


@needs_harbor
@pytest.mark.parametrize("name", sorted(HOST_CLI_ENV))
def test_the_container_value_of_a_name_the_docker_cli_reads_never_steers_the_host_cli(name):
    out = probe()
    assert out["env"][name] == HOST_CLI_ENV[name]  # the docker CLI keeps the host's value
    assert any(f.startswith(name + "=") for f in flags(out["argv"]))  # the container's value goes as before


@needs_harbor
def test_the_installed_harbor_is_the_pinned_patched_one():
    installed = harbor_patch.check(HARBOR_VENV / "bin" / "harbor")
    assert installed.version == harbor_patch.PINNED_VERSION
    assert installed.sha256 == harbor_patch.PATCHED_SHA256


# --- the patch file and its pins ---------------------------------------------------------------------------------

def test_the_patch_file_changes_only_the_docker_environment():
    text = harbor_patch.PATCH_FILE.read_text(encoding="utf-8")
    targets = [line.split()[1] for line in text.splitlines() if line.startswith(("--- ", "+++ "))]
    assert targets == [f"a/{harbor_patch.TARGET.as_posix()}", f"b/{harbor_patch.TARGET.as_posix()}"]
    assert harbor_patch.PINNED_VERSION == "0.25.0"
    for digest in (harbor_patch.ORIGINAL_SHA256, harbor_patch.PATCHED_SHA256):
        assert len(digest) == 64 and int(digest, 16) >= 0
    assert harbor_patch.ORIGINAL_SHA256 != harbor_patch.PATCHED_SHA256


def test_a_unified_diff_is_applied_only_where_its_context_matches_exactly():
    patch = "comment line\n--- a/f\n+++ b/f\n@@ -2,3 +2,4 @@\n b\n-c\n+C\n+c2\n d\n"
    assert harbor_patch.apply_unified("a\nb\nc\nd\ne\n", patch) == "a\nb\nC\nc2\nd\ne\n"
    with pytest.raises(harbor_patch.HarborSetupError):
        harbor_patch.apply_unified("a\nb\nX\nd\ne\n", patch)


# --- fake installs -------------------------------------------------------------------------------------------------

ORIGINAL = "def exec():\n    flag = 'NAME=VALUE'\n    return flag\n"
PATCHED = "def exec():\n    flag = 'NAME'\n    return flag\n"
SMALL_PATCH = ("Pass the name only.\n"
               "--- a/harbor/environments/docker/docker.py\n+++ b/harbor/environments/docker/docker.py\n"
               "@@ -1,3 +1,3 @@\n def exec():\n-    flag = 'NAME=VALUE'\n+    flag = 'NAME'\n     return flag\n")


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


ENTRY_BODY = "import sys\nfrom harbor.cli.main import app\nif __name__ == '__main__':\n    sys.exit(app())\n"


def rest_digest(site: Path) -> str:
    """sha256 over "<path> <sha256>" lines, sorted by path, of every file of the harbor package but the patched one
    (paths relative to site-packages, __pycache__ left out)."""
    lines = []
    for path in (site / "harbor").rglob("*"):
        rel = path.relative_to(site).as_posix()
        if path.is_file() and "__pycache__" not in path.parts and rel != harbor_patch.TARGET.as_posix():
            lines.append(f"{rel} {hashlib.sha256(path.read_bytes()).hexdigest()}\n")
    return hashlib.sha256("".join(sorted(lines)).encode()).hexdigest()


def site_of(root: Path) -> Path:
    return root / "venv" / "lib" / "python3.13" / "site-packages"


def fake_install(monkeypatch, root: Path, version: str = "0.25.0", content: str = PATCHED, dist_infos: int = 1) -> Path:
    """A virtual environment with a `harbor` entry script and a small harbor package, and the pins that fit it;
    returns the entry script."""
    venv = root / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = /usr/bin\n")
    (venv / "bin" / "python").write_text("")
    harbor = venv / "bin" / "harbor"
    harbor.write_text(f"#!{venv / 'bin' / 'python'}\n{ENTRY_BODY}")
    site = site_of(root)
    target = site / "harbor" / "environments" / "docker" / "docker.py"
    target.parent.mkdir(parents=True)
    target.write_text(content)
    (site / "harbor" / "__init__.py").write_text("")
    (site / "harbor" / "agents").mkdir()
    (site / "harbor" / "agents" / "claude_code.py").write_text("def run(env):\n    return env\n")
    (site / "harbor" / "agents" / "__pycache__").mkdir()
    (site / "harbor" / "agents" / "__pycache__" / "claude_code.cpython-313.pyc").write_bytes(b"compiled")
    monkeypatch.setattr(harbor_patch, "PACKAGE_REST_SHA256", rest_digest(site), raising=False)
    monkeypatch.setattr(harbor_patch, "STARTUP_FILES", {}, raising=False)
    for i in range(dist_infos):
        info = site / f"harbor-{version if i == 0 else '0.24.0'}.dist-info"
        info.mkdir()
        (info / "METADATA").write_text(f"Metadata-Version: 2.4\nName: harbor\nVersion: {version if i == 0 else '0.24.0'}\n")
    return harbor


def check(harbor: Path):
    return harbor_patch.check(harbor, version="0.25.0", original_sha256=sha(ORIGINAL), patched_sha256=sha(PATCHED))


def test_the_pinned_patched_install_passes(tmp_path, monkeypatch):
    installed = check(fake_install(monkeypatch, tmp_path))
    assert installed.version == "0.25.0" and installed.sha256 == sha(PATCHED)
    assert installed.venv == tmp_path / "venv"


def test_another_harbor_version_is_refused(tmp_path, monkeypatch):
    with pytest.raises(harbor_patch.HarborSetupError) as err:
        check(fake_install(monkeypatch, tmp_path, version="0.25.1"))
    assert "0.25.1" in str(err.value) and "0.25.0" in str(err.value)


def test_the_released_file_without_the_patch_is_refused_with_the_way_to_patch_it(tmp_path, monkeypatch):
    with pytest.raises(harbor_patch.HarborSetupError) as err:
        check(fake_install(monkeypatch, tmp_path, content=ORIGINAL))
    assert "not patched" in str(err.value) and "harbor_patch apply" in str(err.value)


def test_a_file_that_is_neither_released_nor_patched_is_refused(tmp_path, monkeypatch):
    with pytest.raises(harbor_patch.HarborSetupError) as err:
        check(fake_install(monkeypatch, tmp_path, content=PATCHED + "# changed\n"))
    assert "neither" in str(err.value)


def test_two_installed_harbors_are_refused(tmp_path, monkeypatch):
    with pytest.raises(harbor_patch.HarborSetupError):
        check(fake_install(monkeypatch, tmp_path, dist_infos=2))


def test_a_harbor_that_does_not_run_from_a_virtual_environment_is_refused(tmp_path, monkeypatch):
    harbor = fake_install(monkeypatch, tmp_path)
    (tmp_path / "venv" / "pyvenv.cfg").unlink()
    with pytest.raises(harbor_patch.HarborSetupError):
        check(harbor)
    with pytest.raises(harbor_patch.HarborSetupError):
        check(tmp_path / "missing" / "harbor")
    script = tmp_path / "not-python"
    script.write_text("echo hi\n")
    with pytest.raises(harbor_patch.HarborSetupError):
        check(script)


def apply(venv: Path, patch_text: str = SMALL_PATCH, patched: str = PATCHED):
    patch_file = venv.parent / "change.patch"
    patch_file.write_text(patch_text)
    return harbor_patch.apply(venv, patch_file=patch_file, version="0.25.0", original_sha256=sha(ORIGINAL),
                              patched_sha256=sha(patched))


def target_of(venv: Path) -> Path:
    return venv / "lib" / "python3.13" / "site-packages" / "harbor" / "environments" / "docker" / "docker.py"


def test_apply_patches_the_released_file_once(tmp_path, monkeypatch):
    fake_install(monkeypatch, tmp_path, content=ORIGINAL)
    venv = tmp_path / "venv"
    assert apply(venv) == "applied"
    assert target_of(venv).read_text() == PATCHED
    assert apply(venv) == "already patched"
    assert target_of(venv).read_text() == PATCHED
    check(venv / "bin" / "harbor")


def test_apply_refuses_an_unknown_file_and_leaves_it_alone(tmp_path, monkeypatch):
    fake_install(monkeypatch, tmp_path, content=ORIGINAL + "# local change\n")
    venv = tmp_path / "venv"
    with pytest.raises(harbor_patch.HarborSetupError):
        apply(venv)
    assert target_of(venv).read_text() == ORIGINAL + "# local change\n"


def test_apply_refuses_another_version(tmp_path, monkeypatch):
    fake_install(monkeypatch, tmp_path, version="0.24.0", content=ORIGINAL)
    with pytest.raises(harbor_patch.HarborSetupError):
        apply(tmp_path / "venv")
    assert target_of(tmp_path / "venv").read_text() == ORIGINAL


def test_apply_restores_the_file_when_the_result_is_not_the_pinned_one(tmp_path, monkeypatch):
    fake_install(monkeypatch, tmp_path, content=ORIGINAL)
    venv = tmp_path / "venv"
    with pytest.raises(harbor_patch.HarborSetupError):
        apply(venv, patched=PATCHED + "# expected something else\n")
    assert target_of(venv).read_text() == ORIGINAL


# --- the whole Harbor install, not only the patched file --------------------------------------------------------------

def test_a_changed_harbor_module_besides_the_patched_file_is_refused(tmp_path, monkeypatch):
    harbor = fake_install(monkeypatch, tmp_path)
    module = site_of(tmp_path) / "harbor" / "agents" / "claude_code.py"
    module.write_text(module.read_text() + "print(env)\n")
    with pytest.raises(harbor_patch.HarborSetupError):
        check(harbor)


def test_a_module_added_to_the_harbor_package_is_refused(tmp_path, monkeypatch):
    harbor = fake_install(monkeypatch, tmp_path)
    (site_of(tmp_path) / "harbor" / "agents" / "extra.py").write_text("import os\n")
    with pytest.raises(harbor_patch.HarborSetupError):
        check(harbor)


def test_a_changed_entry_script_is_refused(tmp_path, monkeypatch):
    harbor = fake_install(monkeypatch, tmp_path)
    harbor.write_text(harbor.read_text() + "import os\n")
    with pytest.raises(harbor_patch.HarborSetupError):
        check(harbor)


@pytest.mark.parametrize("name,text", [("extra.pth", "import os\n"), ("sitecustomize.py", "import os\n"),
                                       ("usercustomize.py", "import os\n")])
def test_a_file_that_runs_code_whenever_harbors_python_starts_is_refused(tmp_path, monkeypatch, name, text):
    harbor = fake_install(monkeypatch, tmp_path)
    (site_of(tmp_path) / name).write_text(text)
    with pytest.raises(harbor_patch.HarborSetupError):
        check(harbor)


def test_apply_refuses_when_another_harbor_module_differs_from_the_release(tmp_path, monkeypatch):
    fake_install(monkeypatch, tmp_path, content=ORIGINAL)
    module = site_of(tmp_path) / "harbor" / "agents" / "claude_code.py"
    module.write_text(module.read_text() + "print(env)\n")
    with pytest.raises(harbor_patch.HarborSetupError):
        apply(tmp_path / "venv")
    assert target_of(tmp_path / "venv").read_text() == ORIGINAL


# The entry scripts installers write for `harbor = harbor.cli.main:app`, after the #! line: uv, pip 25.2, pip 26.2.2
# (CI), and the older template that strips the suffix with re.sub.
INSTALLER_ENTRY_BODIES = {
    "uv": ('# -*- coding: utf-8 -*-\nimport sys\nfrom harbor.cli.main import app\nif __name__ == "__main__":\n'
           '    if sys.argv[0].endswith("-script.pyw"):\n        sys.argv[0] = sys.argv[0][:-11]\n'
           '    elif sys.argv[0].endswith(".exe"):\n        sys.argv[0] = sys.argv[0][:-4]\n    sys.exit(app())\n'),
    "pip-25": ("import sys\nfrom harbor.cli.main import app\nif __name__ == '__main__':\n"
               "    if sys.argv[0].endswith('.exe'):\n        sys.argv[0] = sys.argv[0][:-4]\n    sys.exit(app())\n"),
    "pip-26": ("import sys\nfrom harbor.cli.main import app\nif __name__ == '__main__':\n"
               "    sys.argv[0] = sys.argv[0].removesuffix('.exe')\n    sys.exit(app())\n"),
    "re-sub": ("# -*- coding: utf-8 -*-\nimport re\nimport sys\nfrom harbor.cli.main import app\n"
               "if __name__ == '__main__':\n    sys.argv[0] = re.sub(r'(-script\\.pyw|\\.exe)?$', '', sys.argv[0])\n"
               "    sys.exit(app())\n"),
}


@pytest.mark.parametrize("installer", sorted(INSTALLER_ENTRY_BODIES))
def test_the_entry_scripts_installers_write_are_accepted(tmp_path, monkeypatch, installer):
    harbor = fake_install(monkeypatch, tmp_path)
    harbor.write_text(harbor.read_text().split("\n", 1)[0] + "\n" + INSTALLER_ENTRY_BODIES[installer])
    check(harbor)
