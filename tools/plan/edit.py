"""Load wbs_data.py, apply edits from a small script, write it back in one canonical layout."""
import importlib, sys, pprint
from pathlib import Path
HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import wbs_data as W

ORDER = ["id", "group", "name", "milestone", "ongoing", "start", "end", "after", "out", "done", "who",
         "human_h", "ai_days", "cost_usd", "note"]

def item(i):
    return next(x for x in W.ITEMS if x["id"] == i)

def update(i, **kw):
    item(i).update(kw)

def remove(i):
    W.ITEMS[:] = [x for x in W.ITEMS if x["id"] != i]
    for x in W.ITEMS:
        if i in x.get("after", []):
            x["after"] = [p for p in x["after"] if p != i]

def add(before=None, **kw):
    if before:
        k = next(n for n, x in enumerate(W.ITEMS) if x["id"] == before)
        W.ITEMS.insert(k, kw)
    else:
        W.ITEMS.append(kw)

def save():
    head = Path(HERE / "wbs_data.py").read_text(encoding="utf-8").split("ITEMS = [")[0]
    lines = ["ITEMS = ["]
    for x in W.ITEMS:
        parts = []
        for k in ORDER + [k for k in x if k not in ORDER]:
            if k in x and x[k] not in (None, [], 0, False, ""):
                parts.append(f"{k}={x[k]!r}")
        lines.append("    dict(" + ", ".join(parts) + "),")
    lines.append("]")
    (HERE / "wbs_data.py").write_text(head + "\n".join(lines) + "\n", encoding="utf-8")
