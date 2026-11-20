"""
benchmark/run.py - run every scenario against several models and every defence
setting, then build the matrix.

    python benchmark/run.py --fast                           # local qwen 0.8B/2B/4B + llama 8B, 3 scenarios, ~40 min
    python benchmark/run.py                                  # default models, 5 epochs
    python benchmark/run.py --models qwen-9b claude-haiku    # pick models
    python benchmark/run.py --epochs 1 --scenarios triage clean   # quick smoke test
    python benchmark/run.py --configs baseline all           # only some columns
    python benchmark/run.py --fresh                          # ignore earlier runs

Models are short names from models.yaml (qwen-9b, claude-fable, ...) or any
Inspect model id (ollama/llama3.1:8b, anthropic/claude-sonnet-5-5).

Runs that already finished with the same settings are skipped, so you can stop
it and start it again. Raw logs go to benchmark/raw/<model>/. Open them with
`inspect view --log-dir benchmark/raw`.
"""

import argparse
import os
import sys

import yaml
from inspect_ai import eval as inspect_eval
from inspect_ai.log import list_eval_logs, read_eval_log
from inspect_ai.model import GenerateConfig, get_model

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
RAW = os.path.join(HERE, "raw")
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "evals"))

import matrix  # noqa: E402
from maintainerbot import maintainerbot  # noqa: E402

DEFAULT_MODELS = ["qwen-0.8b", "qwen-2b", "qwen", "qwen-9b", "llama"]
# Layer 4 (human approval) needs a stand-in human. One fixed reviewer for every
# model keeps the comparison fair. Local, so it costs nothing.
DEFAULT_REVIEWER = "qwen-9b"

# --fast: small local models, thinking off, 3 scenarios, 1 repeat (~10 min per model).
FAST_MODELS = ["qwen-0.8b", "qwen-2b", "qwen", "llama"]  # all local, already pulled
# leaks undefended / gets past the allowlist / normal issue (is the bot still useful?)
FAST_SCENARIOS = ["triage", "harmful-reply", "clean"]
FAST_REVIEWER = "qwen"
FAST_EPOCHS = 1
# Can't be scored automatically: the verdict needs a person to read the transcript.
SKIP_SCENARIOS = {"impossible-task"}


def load_env():
    """Read .env (API keys) without printing anything from it.

    Looks in the repo root, then the folder above it (outside the repo).
    The first non-empty value for a key wins; the shell's own env wins over both."""
    for path in (os.path.join(ROOT, ".env"), os.path.join(ROOT, "..", ".env")):
        if not os.path.exists(path):
            continue
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("export "):
                    line = line[len("export "):]
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    if v.strip() and not os.environ.get(k.strip()):
                        os.environ[k.strip()] = v.strip().strip('"').strip("'")


def inspect_id(name):
    """models.yaml short name -> Inspect model id."""
    if "/" in name:
        return name
    with open(os.path.join(ROOT, "models.yaml")) as f:
        models = (yaml.safe_load(f) or {}).get("models", {})
    if name not in models:
        return f"ollama/{name}"  # any Ollama tag, like agent.py
    m = models[name]
    if m["provider"] == "mock":
        raise SystemExit("The mock model is a scripted replay; it can't be benchmarked.")
    return f"{m['provider']}/{m['name']}"


def already_done(model, layers, scenarios, epochs):
    want = matrix.layers_key({"layers": layers})
    for info in list_eval_logs(RAW, recursive=True):
        log = read_eval_log(info, header_only=True)
        args = log.eval.task_args
        if (log.status == "success" and log.eval.model == model
                and matrix.layers_key(args) == want
                and str(args.get("scenario", "")) == scenarios
                and (log.eval.config.epochs or 1) == epochs):
            return True
    return False


def main():
    import agent  # after evals/ put src/ on the path

    configs = {key: (label, layers) for key, label, layers in matrix.CONFIGS}
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--fast", action="store_true",
                   help="quick preset: local qwen 0.8b/2b/4b + llama 8b, 3 scenarios, thinking off, 1 epoch")
    p.add_argument("--models", nargs="+", default=None)
    p.add_argument("--configs", nargs="+", default=list(configs), choices=list(configs))
    p.add_argument("--scenarios", nargs="+", default=None,
                   help="default: all scored scenarios (--fast: triage harmful-reply clean)")
    p.add_argument("--epochs", type=int, default=None, help="repeats of each scenario (default 5, --fast 1)")
    p.add_argument("--reviewer", default=None,
                   help="model that plays the human for layer 4 (default qwen-9b, --fast qwen; claude-haiku when every model is Claude)")
    p.add_argument("--think", action="store_true",
                   help="leave local (Ollama) models' thinking on. Off by default: ~100x faster")
    p.add_argument("--max-connections", type=int, default=4,
                   help="parallel requests per model (default 4)")
    p.add_argument("--fresh", action="store_true", help="re-run even if a matching run exists")
    a = p.parse_args()

    a.models = a.models or (FAST_MODELS if a.fast else DEFAULT_MODELS)
    a.epochs = a.epochs or (FAST_EPOCHS if a.fast else 5)
    a.scenarios = a.scenarios or (FAST_SCENARIOS if a.fast else
                                  [s for s in agent.SCENARIOS if s not in SKIP_SCENARIOS])
    all_cloud = all(inspect_id(m).startswith("anthropic/") for m in a.models)
    if not a.reviewer:
        # A Claude-only run shouldn't wait on the laptop's GPU for layer 4's reviewer.
        a.reviewer = "claude-haiku" if all_cloud else (FAST_REVIEWER if a.fast else DEFAULT_REVIEWER)
    if all_cloud and a.max_connections == 4:
        a.max_connections = 10  # the API runs requests in parallel; Ollama doesn't

    load_env()
    scenarios = ",".join(a.scenarios)

    def config_for(model_id):
        # Qwen3.5 thinks by default, which makes each call take ~a minute.
        if model_id.startswith("ollama/") and not a.think:
            return GenerateConfig(reasoning_effort="none")
        return GenerateConfig()

    reviewer_id = inspect_id(a.reviewer)
    reviewer = get_model(reviewer_id, config=config_for(reviewer_id))
    total = len(a.models) * len(a.configs)
    n = 0
    for name in a.models:
        model = inspect_id(name)
        if model.startswith("anthropic/") and not os.environ.get("ANTHROPIC_API_KEY"):
            print(f"!! skipping {name}: ANTHROPIC_API_KEY is not set (put it in .env)")
            n += len(a.configs)
            continue
        for key in a.configs:
            n += 1
            label, layers = configs[key]
            tag = f"[{n}/{total}] {name} · {label}"
            if not a.fresh and already_done(model, layers, scenarios, a.epochs):
                print(f"{tag}: already done, skipping")
                continue
            print(f"{tag}: running {len(a.scenarios)} scenarios × {a.epochs} epochs")
            inspect_eval(
                maintainerbot(layers=layers, scenario=scenarios),
                model=model,
                model_roles={"reviewer": reviewer} if "2" in layers.split(",") else None,
                epochs=a.epochs,
                log_dir=os.path.join(RAW, model.replace("/", "_").replace(":", "-")),
                max_connections=a.max_connections,
                **config_for(model).model_dump(exclude_none=True),
                fail_on_error=False,  # one broken sample shouldn't sink the run
                tags=["benchmark"],
            )
    matrix.main()


if __name__ == "__main__":
    main()
