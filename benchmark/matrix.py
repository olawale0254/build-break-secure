"""
benchmark/matrix.py - turn the raw eval logs in benchmark/raw/ into the matrix.

    python benchmark/matrix.py

Updates benchmark/matrix.html, the results page (one file, works offline). The
page is its own template: only its `const DATA = ...;` line is rewritten, so edit
the layout in matrix.html directly.

Each cell has two numbers:
    attack success rate (ASR)  how often the attack worked        lower is better
    task success rate  (TSR)   replied, and nothing leaked         higher is better

If the same model + defence was run more than once, the newest finished run wins.
"""

import json
import os
import re
import sys
from collections import defaultdict

from inspect_ai.log import list_eval_logs, read_eval_log

HERE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(HERE, "raw")
sys.path.insert(0, os.path.join(HERE, "..", "src"))

# The columns of the matrix, in the order of the Swiss cheese slide.
# `layers` uses the numbers in src/defences.py, which differ from the slide:
#   code 1 = allowlist, 2 = human approval, 3 = dual LLM, 4 = output guard
CONFIGS = [
    ("baseline",     "No defence",        ""),
    ("allowlist",    "L1 Allowlist",      "1"),
    ("dual-llm",     "L2 Dual LLM",       "3"),
    ("output-guard", "L3 Output guard",   "4"),
    ("human",        "L4 Human approval", "2"),
    ("all",          "All four stacked",  "1,2,3,4"),
]
LABEL_BY_LAYERS = {layers: label for _, label, layers in CONFIGS}
# The matrix compares local models. Claude is the attacker (benchmark/redteam.py),
# not a target, so cloud results stay in raw/ but are left off. Empty it to show them.
HIDE_PROVIDERS = {"anthropic", "openai"}
CONFIG_ORDER = [label for _, label, _ in CONFIGS]


def layers_key(task_args):
    raw = task_args.get("layers", "")
    items = raw if isinstance(raw, (list, tuple)) else str(raw).split(",")
    return ",".join(sorted(str(x).strip() for x in items if str(x).strip()))


def latest_logs():
    """Newest finished log for each (model, layers)."""
    chosen = {}
    for info in list_eval_logs(RAW, recursive=True):
        log = read_eval_log(info, header_only=True)
        if log.status != "success" or log.eval.model.split("/")[0] in HIDE_PROVIDERS:
            continue
        key = (log.eval.model, layers_key(log.eval.task_args))
        if key not in chosen or log.eval.created > chosen[key][0]:
            chosen[key] = (log.eval.created, info)
    return {key: info for key, (_, info) in chosen.items()}


def collect():
    """{(model, config_label, scenario): {"kind", "asr": [...], "tsr": [...]}}"""
    cells = defaultdict(lambda: {"kind": "attack", "covers": "", "asr": [], "tsr": [], "filtered": []})
    for (model, layers), info in latest_logs().items():
        label = LABEL_BY_LAYERS.get(layers, f"layers {layers}")
        for s in read_eval_log(info).samples or []:
            if not s.scores or "leak_rate" not in s.scores:
                continue  # sample errored; not counted
            cell = cells[(model, label, s.id)]
            cell["kind"] = (s.metadata or {}).get("kind", "attack")
            cell["covers"] = (s.metadata or {}).get("covers") or ""
            cell["asr"].append(float(s.scores["leak_rate"].value))
            cell["tsr"].append(float(s.scores["task_success"].value))
            # The API's own safety filter cut the reply off (Claude: stop_reason
            # "content_filter"). No leak, but it was the provider, not the model or
            # our defences, that stopped it, so we count it separately.
            cell["filtered"].append(bool(s.output and s.output.stop_reason == "content_filter"))
    return cells


def pct(values):
    return None if not values else 100 * sum(values) / len(values)


def short(model):
    return model.split("/", 1)[-1]


def write_html(cells, path):
    models = sorted({m for m, _, _ in cells})
    labels = [lb for lb in CONFIG_ORDER if any(lb == l for _, l, _ in cells)]
    import agent  # scenarios.yaml: which layers are *supposed* to stop each attack
    code_to_label = {int(layers): label for _, label, layers in CONFIGS if layers.isdigit()}
    scen = {}
    for (_, _, s), c in cells.items():
        meant = agent.SCENARIOS.get(s, {}).get("stopped_by") or []
        scen[s] = {"name": s, "kind": c["kind"], "covers": c["covers"],
                   "stopped_by": [code_to_label[n] for n in meant if n in code_to_label]}
    data = {
        "models": [short(m) for m in models],
        "labels": labels,
        "scenarios": sorted(scen.values(), key=lambda x: (x["kind"] != "attack", x["name"])),
        "cells": {f"{short(m)}|{lb}|{s}": {"asr": pct(c["asr"]), "tsr": pct(c["tsr"]), "n": len(c["asr"]),
                                                     "f": sum(c["filtered"])}
                  for (m, lb, s), c in cells.items()},
    }
    # The page is its own template: only its `const DATA = ...;` line changes.
    with open(path) as f:
        page = f.read()
    page, n = re.subn(r"^const DATA = .*;$", lambda _: f"const DATA = {json.dumps(data)};",
                      page, count=1, flags=re.M)
    if not n:
        raise SystemExit(f"No `const DATA = ...;` line in {path}")
    with open(path, "w") as f:
        f.write(page)


def main():
    cells = collect()
    if not cells:
        raise SystemExit(f"No finished eval logs in {RAW}. Run benchmark/run.py first.")
    write_html(cells, os.path.join(HERE, "matrix.html"))
    print(f"Updated benchmark/matrix.html "
          f"({len(cells)} cells from {len(latest_logs())} runs)")


if __name__ == "__main__":
    main()
