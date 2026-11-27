"""Checks every commit of a pull request or of a push, and the pull request's own text.

Not built yet: it reads its arguments and finds nothing. The tests beside it say what it must do.
"""

import argparse
import sys


def main(argv=None):
    parser = argparse.ArgumentParser()
    for name in ("--repo", "--from", "--head", "--settings", "--context", "--event", "--verified-shas"):
        parser.add_argument(name)
    parser.add_argument("--own", action="store_true")
    parser.add_argument("--require-commits", action="store_true")
    parser.parse_args(argv)
    print("0 commits checked")
    return 0


if __name__ == "__main__":
    sys.exit(main())
