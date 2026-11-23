"""Run from the benchmark's folder by the job "variants": every pilot test fails on its broken variant, and no part of the
check is skipped in the job that is there to run it. A script in a protected folder, so that the workflow runs one simple
command and starts no shell."""

import subprocess
import sys

done = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-rs", "tests/test_pilot_variants.py"],
                      capture_output=True, text=True, check=False)  # fmt: skip
said = done.stdout + done.stderr
print(said, end="")
if done.returncode != 0:
    sys.exit(1)
if "skipped" in said:
    print("A part of the variants check was skipped in the job that is there to run it.")
    sys.exit(1)
