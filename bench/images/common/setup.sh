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
  # A base image's own uid-1000 user (node, ubuntu) can sit in groups such as sudo or adm, so it is
  # removed and agent is made afresh, in no group but its own.
  existing="$(getent passwd 1000 | cut -d: -f1 || true)"
  if [ -n "$existing" ] && [ "$existing" != agent ]; then
    userdel --remove "$existing" 2>/dev/null || true
    if getent passwd "$existing" >/dev/null; then
      echo "could not remove the user $existing" >&2
      exit 1
    fi
  fi
  if ! getent passwd agent >/dev/null; then
    stale_group="$(getent group 1000 | cut -d: -f1 || true)"
    [ -z "$stale_group" ] || groupdel "$stale_group"
    useradd --create-home --uid 1000 --user-group --shell /bin/bash agent
  fi
  usermod --groups "" agent
  mkdir -p /app
  chown -R -h agent:agent /app
  ;;
*)
  echo "usage: setup.sh judge|agent" >&2
  exit 2
  ;;
esac
