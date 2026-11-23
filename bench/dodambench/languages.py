"""The seven existing languages Dodam is measured against, with how a web task is built and started in each.

Framework rule: an HTTP API task uses the most-used server framework of the language that needs no
browser screen; a task with a browser screen will add a full-stack choice when such tasks exist.
Package rule: besides the framework and the SQLite driver, a language gets the most-used packages for
what tasks require of it but its standard library lacks: a slow salted password hash, HMAC and
secure random bytes. Versions are pinned by the language images; the names here are what agents are told.
A language's starting /app lives in bench/images/<key>/app; deps_cmd installs its dependencies there,
and deps_links names the installed folders that vetting links instead of copying. The language image
keeps those folders under DEPS_DIR and the verifier relinks /app to them before every build, so a
program is always judged with the pinned packages, whatever the agent installed.

A command-line task uses no framework. The program is built with the same build command and is then
run once per case with run_cmd followed by the case's arguments; {app} stands for the program's
folder, because the program is started in the case's working folder, not in its own. cli_layout
says where the sources go when that differs from a web task. Every runtime but Java's takes its
temporary folder from the environment, which gives each case its own; Java's run command names the
case's temporary folder, `../home/tmp` from the working folder, to the runtime itself.
"""
from __future__ import annotations

from dataclasses import dataclass


DEPS_DIR = "/opt/deps"


@dataclass(frozen=True)
class Language:
    key: str
    name: str
    framework: str
    layout: str
    packages: str
    build_cmd: str
    start_cmd: str
    deps_cmd: str = ""
    deps_links: tuple[str, ...] = ()
    run_cmd: str = ""
    cli_layout: str = ""

    @property
    def image(self) -> str:
        return f"dodam-bench/{self.key}:dev"


LANGUAGES: dict[str, Language] = {lang.key: lang for lang in (
    Language("python", "Python 3", "FastAPI served by uvicorn",
             "Write the application in /app/main.py and expose a FastAPI instance named `app`.",
             "fastapi, uvicorn and pydantic, plus the Python standard library",
             "", "uvicorn main:app --host 0.0.0.0 --port $PORT",
             run_cmd="python3 {app}/main.py", cli_layout="Write the program in /app/main.py."),
    Language("typescript", "TypeScript on Node.js", "Express",
             "Write the sources in /app/src with the entry point /app/src/main.ts. "
             "/app/package.json and /app/tsconfig.json are set up; the build writes /app/dist.",
             "express and better-sqlite3 with their type packages, plus Node.js built-in modules",
             "npm run build --silent", "node dist/main.js",
             deps_cmd="npm ci --no-audit --no-fund --silent", deps_links=("node_modules",),
             run_cmd="node {app}/dist/main.js"),
    Language("java", "Java", "Spring Boot",
             "The Maven project is /app with pom.xml set up; write the sources under /app/src/main/java.",
             "the Spring Boot web starter and the SQLite JDBC driver, plus the Java standard library",
             "mvn -o -q -DskipTests package", "java -jar target/app.jar",
             run_cmd="java -Djava.io.tmpdir=../home/tmp -jar {app}/target/app.jar",
             cli_layout="The Maven project is /app with pom.xml set up; write the sources under /app/src/main/java, "
                        "with exactly one class that has a main method."),
    Language("csharp", "C#", "ASP.NET Core minimal APIs",
             "/app/App.csproj is set up; write the sources in /app.",
             "ASP.NET Core and Microsoft.Data.Sqlite, plus the .NET base library",
             "dotnet publish -c Release -o out --no-restore -v q", "dotnet out/App.dll",
             deps_cmd="dotnet restore --locked-mode", run_cmd="dotnet {app}/out/App.dll"),
    Language("go", "Go", "the standard library net/http",
             "/app/go.mod is set up as module app; write the sources in /app.",
             "the Go standard library and modernc.org/sqlite",
             "go build -o app .", "./app", deps_cmd="go mod download", run_cmd="{app}/app"),
    Language("rust", "Rust", "axum on tokio",
             "/app/Cargo.toml is set up with a binary named app; write the sources in /app/src.",
             "axum, tokio, serde, serde_json, rusqlite, argon2, hmac, sha2, rand and base64, "
             "plus the Rust standard library",
             "cargo build --release --offline --quiet", "./target/release/app",
             run_cmd="{app}/target/release/app"),
    Language("cpp", "C++", "Drogon",
             "/app/CMakeLists.txt is set up with a target named app; write the sources in /app/src.",
             "Drogon, SQLite3 and OpenSSL, plus the C++ standard library",
             "cmake -E rm -rf build && cmake -S . -B build -DCMAKE_BUILD_TYPE=Release && cmake --build build -j 2",
             "./build/app", run_cmd="{app}/build/app"),
)}
