#!/bin/sh
# Rebuild the plan to 1.0 (Markdown and HTML) from the work breakdown and the forecast results.
set -e
cd "$(dirname "$0")"
python3 build.py
python3 build_doc.py ../../docs/plan/2026-10-29-plan-to-1.0.ko.md
python3 build_html.py page.html
python3 wrap_local.py page.html ../../docs/plan/2026-10-29-plan-to-1.0.ko.html
