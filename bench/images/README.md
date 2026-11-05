# Language images

One image per language holds everything a benchmark run needs inside its container: the language
tools, the pinned packages the language gets (see `dodambench/languages.py`), the starting `/app`,
the judge's own Python, and the plain user `agent`. Agents work in it, and the verifier judges in a
fresh container of the same image with no network.

Build from this folder, for example:

    docker build -f python/Dockerfile -t dodam-bench/python:dev .

Every image keeps these rules, which the judge relies on:

- The base image is pinned by digest, and packages are pinned to exact versions with hashes where
  the package manager supports them.
- `/opt/judge` is a Python virtual environment owned by root, installed from
  `judge/requirements.txt` by `common/setup.sh judge`. The verifier runs the judge with it.
- The user `agent` (uid 1000) owns `/app`. It is created by `common/setup.sh agent`, which takes
  over a uid-1000 user if the base image already has one. The agent works as this user, and the
  judge builds and starts the program as this user.
- The starting app is `<language>/app`, copied to `/app`.
- Dependency folders named in the language's `deps_links` live in `/opt/deps`, owned by root, and
  `/app/<name>` links to them. Before every build the verifier replaces whatever `/app` holds there
  with the link, so a program is always judged with the pinned packages.
- The build and start commands in `dodambench/languages.py` work without network.
- A program may use whatever the image installs, the listed packages' own dependencies included,
  and nothing else: judging has no network, and only `/app` comes from the agent's container.
  Agents are told the listed packages; reaching for one of their dependencies is allowed,
  because it adds nothing that every agent of that language could not also use.

To check an image with a task's prepared solutions, judged exactly as the verifier judges them:

    .venv/bin/python -m dodambench.vet tasks/web-signup --lang python --image
