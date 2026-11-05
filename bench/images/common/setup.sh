#!/bin/sh
# Steps every Dodam language image shares. Run as root while building the image, from /opt/dodam.
#   judge: the judge's own Python environment in /opt/judge, owned by root, pinned with hashes.
#   agent: the plain user (uid 1000) the agent works as and the program is built and run as.
set -eu
case "$1" in
judge)
  python3 -m venv /opt/judge
  /opt/judge/bin/pip install --no-cache-dir --disable-pip-version-check --require-hashes \
    --only-binary=:all: -r /opt/dodam/judge-requirements.txt
  ;;
agent)
  existing="$(getent passwd 1000 | cut -d: -f1 || true)"
  if [ -n "$existing" ] && [ "$existing" != agent ]; then
    group="$(id -gn "$existing")"
    usermod --login agent --home /home/agent --move-home "$existing"
    [ "$group" = agent ] || groupmod --new-name agent "$group"
  elif [ -z "$existing" ]; then
    useradd --create-home --uid 1000 --shell /bin/bash agent
  fi
  mkdir -p /app
  chown -R -h agent:agent /app
  ;;
*)
  echo "usage: setup.sh judge|agent" >&2
  exit 2
  ;;
esac
