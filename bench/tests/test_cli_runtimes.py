"""What the library calls of each runtime do for a judged program.

For a judged program the four calls that move pages into a pipe or a socket without copying answer
"invalid argument". The libraries of the runtimes use those calls where they can and copy where
they cannot. Here a program in each language copies a file, sends it over a local socket, copies
one connection into another and serves it through the web framework of that language's prepared
web solutions, each through the call a program of that language would use, and the test compares
the bytes that arrive with the bytes that were sent. The library calls that do not work are
pinned as such: a runtime that learns to copy, or a changed answer, fails the test, and the
documents that name those calls are then corrected.

Each language runs in its own image, judged as the verifier judges. A language whose image is
not there is skipped.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

from dodambench.languages import LANGUAGES
import test_cli_isolation
from test_cli_isolation import found_by, judge_attack, lost, outcomes

RUNTIMES = Path(__file__).resolve().parent / "runtimes"
# where each language's program goes in its project, and what it is asked to do
PLACE = {"python": ("python.py", "main.py"), "typescript": ("main.ts", "src/main.ts"),
         "java": ("App.java", "src/main/java/app/App.java"), "csharp": ("Program.cs", "Program.cs"),
         "go": ("main.go", "main.go"), "rust": ("main.rs", "src/main.rs"), "cpp": ("main.cc", "src/main.cc")}
WORKS = {"python": ("copy", "send", "relay"), "typescript": ("copy", "send", "relay"),
         "java": ("copy", "send", "receive", "relay"), "csharp": ("copy", "relay"),
         "go": ("copy", "send", "relay"), "rust": ("copy", "send", "relay"),
         "cpp": ("copy",)}
# a case's program writes in its own folder only, so the Java server keeps its working files there
RUN = {"java": "java -Djava.io.tmpdir=. -jar {app}/target/app.jar"}
# the library calls that send a file over a socket and have no way of copying it instead
DOES_NOT_WORK = {"csharp": ("send", "send-async", "send-packets")}
# what is served, by which way of starting the server, at which path, and whether every byte arrives
WHOLE = {"/in.bin": True}
SERVED = {lang: {"serve": WHOLE} for lang in PLACE}
# The C++ framework sends a file above about 200 KB with the kernel's file-sending call and has no
# way of copying instead. Its own switch turns that off, by a call or in its configuration file;
# then the file is read and written. A smaller file is not sent that way to begin with.
SERVED["cpp"] = {"serve": {"/response/small.bin": True, "/response/in.bin": False, "/small.bin": True, "/in.bin": False},
                 "serve-switched-off": {"/response/in.bin": True, "/in.bin": True},
                 "serve-configured": {"/response/in.bin": True, "/in.bin": True}}

TESTS = '''
import http.client, threading, urllib.error, urllib.request

DATA = bytes(range(256)) * 8192 + b"the end"
WORKS, DOES_NOT_WORK, SERVED = WORKS_HERE, DOES_NOT_WORK_HERE, SERVED_HERE


def note(name, value):
    seen = json.load(open("/logs/verifier/attack.json")) if os.path.exists("/logs/verifier/attack.json") else {}
    seen[name] = value
    with open("/logs/verifier/attack.json", "w") as f:
        json.dump(seen, f)


def asked(mode, arrives_in):
    with case(files={"in.bin": DATA}) as c:
        r = c.run([mode])
        arrived = c.read(arrives_in)
        note(mode, {"exit_code": r.exit_code, "limit": r.limit, "out": r.out[-300:], "err": r.err[-1200:],
                    "arrived": None if arrived is None else len(arrived), "same": arrived == DATA})
        return r, arrived


def test_copy():
    r, arrived = asked("copy", "out.bin")
    assert r.exit_code == 0 and arrived == DATA, r


def test_send():
    if "send" in DOES_NOT_WORK:
        return
    need("send" in WORKS, "this language's library has no call that sends a file")
    r, arrived = asked("send", "got.bin")
    assert r.exit_code == 0 and arrived == DATA, r


def test_receive():
    need("receive" in WORKS, "this language's library has no call that takes a file out of a socket")
    r, arrived = asked("receive", "got.bin")
    assert r.exit_code == 0 and arrived == DATA, r


def test_relay():
    need("relay" in WORKS, "this language's library has no call that copies one connection into another")
    r, arrived = asked("relay", "got.bin")
    assert r.exit_code == 0 and arrived == DATA, r


def test_serve():
    files = {"in.bin": DATA, "small.bin": DATA[:100000], "config.json": json.dumps({"app": {"use_sendfile": False}})}
    wrong = []
    for mode, paths in SERVED.items():
        with case(files=files) as c:
            got = {}

            def fetch():
                for path in paths:
                    for _ in range(700):
                        try:
                            got[path] = urllib.request.urlopen("http://127.0.0.1:18080" + path, timeout=20).read()
                            break
                        except urllib.error.HTTPError as error:
                            got[path] = repr(error)
                            break
                        except urllib.error.URLError as error:
                            if not isinstance(error.reason, ConnectionRefusedError):
                                got[path] = repr(error)
                                break
                            time.sleep(0.1)                     # the server is not up yet
                        except (OSError, http.client.HTTPException) as error:
                            got[path] = repr(error)             # the server answered, and not with the file
                            break
                try:
                    urllib.request.urlopen("http://127.0.0.1:18080/quit", timeout=5).read()
                except (OSError, http.client.HTTPException):
                    pass

            fetching = threading.Thread(target=fetch, daemon=True)
            fetching.start()
            r = c.run([mode])
            fetching.join(30)
            for path, whole in paths.items():
                body = got.get(path)
                same = body == files[path.rsplit("/", 1)[1]]
                note(f"{mode} {path}", {"exit_code": r.exit_code, "limit": r.limit, "err": r.err[-600:], "same": same,
                                        "arrived": len(body) if isinstance(body, bytes) else body})
                if same != whole:
                    wrong.append((mode, path, same))
    assert not wrong, wrong


def test_what_does_not_work():
    # each of these calls is answered with a failure and sends nothing through
    for mode in DOES_NOT_WORK:
        r, arrived = asked(mode, "got.bin")
        assert r.out.startswith("refused: SocketException") and arrived != DATA, (mode, r)
'''


def _image_ready(image: str) -> bool:
    try:
        return subprocess.run(["docker", "image", "inspect", image], capture_output=True).returncode == 0
    except OSError:
        return False


@pytest.mark.parametrize("lang", list(PLACE))
def test_each_runtime_copies_sends_relays_and_serves_a_file_whole_under_the_judges_filter(tmp_path, monkeypatch, lang):
    """Every byte arrives, in every language, through the calls its programs use: a file copied,
    a file sent over a local socket, one connection copied into another, a file served by the
    web framework. In Java a file also comes out of a socket through the channel's transfer. The
    three calls of C# that send a file over a socket fail, and that is pinned. So is the C++
    framework: a file of 2 MB arrives only with the framework's own switch for the kernel's
    file-sending turned off, by a call or in the configuration file, as a response and as a file
    of a served folder alike; a file of 100 KB arrives either way."""
    language = LANGUAGES[lang]
    if not _image_ready(language.image):
        pytest.skip(f"needs Docker and the {language.name} image")
    monkeypatch.setattr(test_cli_isolation, "IMAGE", language.image)
    source, place = PLACE[lang]
    (tmp_path / "solution" / place).parent.mkdir(parents=True)
    shutil.copy(RUNTIMES / source, tmp_path / "solution" / place)
    tests = TESTS.replace("WORKS_HERE", repr(WORKS[lang])).replace("DOES_NOT_WORK_HERE", repr(DOES_NOT_WORK.get(lang, ()))).replace("SERVED_HERE", repr(SERVED[lang]))
    result = judge_attack(tmp_path, (RUNTIMES / "python.py").read_text(encoding="utf-8"), {"functional/test_runtime.py": tests},
                          {"seconds": 1200}, RUN_CMD=RUN.get(lang, language.run_cmd), BUILD_CMD=language.build_cmd,
                          CASE_TIMEOUT_SEC="90", CASE_PROCESSES="128", CASE_MEMORY_MB="512", CASE_DISK_MB="64")
    assert result["exit_code"] == 0 and result["reward"] is not None, lost(result, tmp_path / "out")
    said = outcomes(result)
    seen = found_by(tmp_path / "out", "attack.json")
    for mode in WORKS[lang]:
        assert seen[mode]["same"], f"{language.name}, {mode}: not every byte arrived: {seen[mode]}"
    for mode, paths in SERVED[lang].items():
        for path, whole in paths.items():
            assert seen[f"{mode} {path}"]["same"] == whole, f"{language.name}, {mode}, {path}: {seen[f'{mode} {path}']}"
    wanted = {"test_copy": "passed", "test_send": "passed" if "send" in WORKS[lang] or lang in DOES_NOT_WORK else "skipped",
              "test_receive": "passed" if "receive" in WORKS[lang] else "skipped",
              "test_relay": "passed" if "relay" in WORKS[lang] else "skipped", "test_serve": "passed",
              "test_what_does_not_work": "passed"}
    assert said == wanted, lost(result, tmp_path / "out")
