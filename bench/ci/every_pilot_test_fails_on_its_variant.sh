#!/bin/sh
# Run from the benchmark's folder by the job "variants": every pilot test fails on its broken variant, and no part of the
# check is skipped in the job that is there to run it. A script in a protected folder, so that the workflow runs one
# simple command.
out=$(python -m pytest -q -p no:cacheprovider -rs tests/test_pilot_variants.py 2>&1) || { printf '%s\n' "$out"; exit 1; }
printf '%s\n' "$out"
if printf '%s' "$out" | grep -q "skipped"; then
  echo "A part of the variants check was skipped in the job that is there to run it."
  exit 1
fi
