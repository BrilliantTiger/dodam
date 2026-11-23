#!/bin/sh
# Run from the benchmark's folder by the job "bench": the leak test must fail on the released Harbor, and fail because
# the made-up secret is on its command line. A script in a protected folder, so that the workflow runs one simple command.
set +e
out=$(python -m pytest -q -p no:cacheprovider "tests/test_harbor_patch.py::test_a_listed_credential_stays_off_the_command_line_and_reaches_the_docker_cli_environment" 2>&1)
code=$?
set -e
if [ "$code" -ne 1 ] || ! printf '%s' "$out" | grep -q "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-PROBE"; then
  printf '%s\n' "$out"
  echo "The leak test did not find the made-up secret on the command line of the released Harbor."
  exit 1
fi
echo "As expected, the leak test fails on the released Harbor: the made-up secret is on the command line."
