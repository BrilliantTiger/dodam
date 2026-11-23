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

from conftest import CANARY
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
# what needs the system's temporary folder or /dev/shm, both closed to a case, and so is refused
CLOSED = {"python": ("semaphore",), "cpp": ("tmpfile",)}
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
WORKS, DOES_NOT_WORK, SERVED, CLOSED = WORKS_HERE, DOES_NOT_WORK_HERE, SERVED_HERE, CLOSED_HERE


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


def test_temp():
    # a temporary file, made through the runtime's own call, goes to the case's temporary folder,
    # and the runtime prints nothing about it
    with case() as c:
        r = c.run(["temp"])
        note("temp", {"exit_code": r.exit_code, "out": r.out[-300:], "err": r.err[-1200:]})
        assert r.exit_code == 0 and r.out.splitlines()[:1] == [os.path.realpath(c.home / "tmp")] and r.err == "", r


def test_closed():
    need(CLOSED, "this language's runtime has no call here that needs a closed folder")
    for mode in CLOSED:
        with case() as c:
            r = c.run([mode])
            note(mode, {"exit_code": r.exit_code, "out": r.out[-300:], "err": r.err[-600:]})
            assert r.exit_code == 0 and r.out.startswith("refused"), (mode, r)


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
    """The run command is the language's own. A temporary file goes to the case's temporary folder in
    every language, with nothing printed about it. Every byte arrives, in every language, through the calls its programs use: a file copied,
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
    tests = TESTS.replace("WORKS_HERE", repr(WORKS[lang])).replace("DOES_NOT_WORK_HERE", repr(DOES_NOT_WORK.get(lang, ()))).replace("SERVED_HERE", repr(SERVED[lang])).replace("CLOSED_HERE", repr(CLOSED.get(lang, ())))
    result = judge_attack(tmp_path, (RUNTIMES / "python.py").read_text(encoding="utf-8"), {"functional/test_runtime.py": tests},
                          {"seconds": 1200}, RUN_CMD=language.run_cmd, BUILD_CMD=language.build_cmd,
                          CASE_TIMEOUT_SEC="90", CASE_PROCESSES="128", CASE_MEMORY_MB="512", CASE_DISK_MB="64")
    assert result["exit_code"] == 0 and result["reward"] is not None, lost(result, tmp_path / "out")
    said = outcomes(result)
    seen = found_by(tmp_path / "out", "attack.json")
    for mode in WORKS[lang]:
        assert seen[mode]["same"], f"{language.name}, {mode}: not every byte arrived: {seen[mode]}"
    for mode, paths in SERVED[lang].items():
        for path, whole in paths.items():
            assert seen[f"{mode} {path}"]["same"] == whole, f"{language.name}, {mode}, {path}: {seen[f'{mode} {path}']}"
    assert seen["temp"]["exit_code"] == 0 and seen["temp"]["err"] == "", f"{language.name}, temp: {seen['temp']}"
    wanted = {"test_temp": "passed", "test_closed": "passed" if lang in CLOSED else "skipped", "test_copy": "passed", "test_send": "passed" if "send" in WORKS[lang] or lang in DOES_NOT_WORK else "skipped",
              "test_receive": "passed" if "receive" in WORKS[lang] else "skipped",
              "test_relay": "passed" if "relay" in WORKS[lang] else "skipped", "test_serve": "passed",
              "test_what_does_not_work": "passed"}
    assert said == wanted, lost(result, tmp_path / "out")


# A served program of a web task makes a temporary file while it answers, through its runtime's own
# call: in Java as in Python, which takes its temporary folder from the environment as the other
# five runtimes do. The served program is started with the language's own start command.
SERVED_TEMP = {
    "python": ("main.py", '''
import os, tempfile

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse

app = FastAPI()


@app.get("/temp", response_class=PlainTextResponse)
def temp():
    try:
        with tempfile.NamedTemporaryFile(delete=False) as made:
            made.write(b"temp")
        return os.path.dirname(os.path.realpath(made.name))
    except OSError as error:
        return f"refused: {error}"
'''),
    "java": ("src/main/java/app/App.java", '''
package app;

import com.sun.net.httpserver.HttpServer;
import java.net.InetSocketAddress;
import java.nio.file.Files;

public class App {
    public static void main(String[] args) throws Exception {
        var server = HttpServer.create(new InetSocketAddress("0.0.0.0", Integer.parseInt(System.getenv("PORT"))), 0);
        server.createContext("/temp", exchange -> {
            String said;
            try {
                var made = Files.createTempFile("temp-", ".tmp");
                Files.writeString(made, "temp");
                said = Files.readString(made).equals("temp") ? made.toRealPath().getParent().toString() : "unread";
            } catch (Exception error) {
                said = "refused: " + error;
            }
            var body = said.getBytes();
            exchange.sendResponseHeaders(200, body.length);
            exchange.getResponseBody().write(body);
            exchange.close();
        });
        server.start();
    }
}
'''),
}
ASK_TEMP = '''
import urllib.request


def test_temp():
    said = urllib.request.urlopen(os.environ["BASE_URL"] + "/temp", timeout=60).read().decode()
    with open("/logs/verifier/attack.json", "w") as f:
        json.dump({"temp": said}, f)
    assert said.startswith("/") and not said.startswith("refused"), said
'''


def judge_served(tmp_path: Path, lang: str) -> dict:
    language = LANGUAGES[lang]
    tests_dir, solution = tmp_path / "tests", tmp_path / "solution"
    (tests_dir / "lib").mkdir(parents=True)
    place, source = SERVED_TEMP[lang]
    (solution / place).parent.mkdir(parents=True)
    (solution / place).write_text(source.lstrip(), encoding="utf-8")
    shutil.copy(test_cli_isolation.BENCH / "dodambench/judge.py", tests_dir / "lib/dodam_judge.py")
    (tests_dir / "functional").mkdir()
    # a web task's tests have no case runner
    (tests_dir / "functional/test_temp.py").write_text(f"# {CANARY}\nimport json, os\n" + ASK_TEMP, encoding="utf-8")
    (tests_dir / "manifest.toml").write_text('[[test]]\nfile = "functional/test_temp.py"\nkind = "functional"\n'
                                             'step = "build"\n', encoding="utf-8")
    env = {"STEPS": "build", "BUILD_CMD": language.build_cmd, "START_CMD": language.start_cmd, "PORT": "8080",
           "DEPS_DIR": "", "DEPS_LINKS": ""}
    (tests_dir / "lang.env").write_text("".join(f"{k}={v}\n" for k, v in env.items()), encoding="utf-8")
    return test_cli_isolation.in_the_image(tests_dir, solution, tmp_path / "out", without=("cases",), seconds=900)


def test_a_served_java_program_makes_its_temporary_files_where_a_python_one_does(tmp_path, monkeypatch):
    for lang in ("python", "java"):
        if not _image_ready(LANGUAGES[lang].image):
            pytest.skip(f"needs Docker and the {LANGUAGES[lang].name} image")
    folders = {}
    for lang in ("python", "java"):
        monkeypatch.setattr(test_cli_isolation, "IMAGE", LANGUAGES[lang].image)
        result = judge_served(tmp_path / lang, lang)
        assert result["exit_code"] == 0 and outcomes(result) == {"test_temp": "passed"}, lost(result, tmp_path / lang / "out")
        folders[lang] = found_by(tmp_path / lang / "out", "attack.json")["temp"]
    assert folders["java"] == folders["python"], folders


@pytest.mark.parametrize("lang", list(LANGUAGES))
def test_no_helper_may_be_named_like_a_module_of_the_judges_python(lang):
    """The judge's own Python differs between the images. A helper of a task may be named like no
    module of the standard library of any of them, whatever Python runs the format checker."""
    import json as json_module
    from dodambench.task import NOT_A_HELPER
    image = LANGUAGES[lang].image
    if not _image_ready(image):
        pytest.skip(f"needs Docker and the {LANGUAGES[lang].name} image")
    done = subprocess.run(["docker", "run", "--rm", "--network", "none", "--entrypoint", "/opt/judge/bin/python", image,
                           "-c", "import sys, json; print(json.dumps(sorted(sys.stdlib_module_names)))"],
                          capture_output=True, text=True, timeout=120)
    names = set(json_module.loads(done.stdout))
    assert names and not names - NOT_A_HELPER, sorted(names - NOT_A_HELPER)
