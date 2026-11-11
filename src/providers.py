"""
providers.py - talk to a real model, or to the deterministic mock.

The agent keeps a single neutral history. Each provider here converts that
history into its own format, calls the model, and converts the reply back into
one shape the agent understands:

    Resp.text   -> the assistant's text (may be None)
    Resp.calls  -> list of {"id", "name", "args"} tool calls (may be empty)

Supported providers: "ollama" (local Qwen), "anthropic", "openai", "mock" (offline script).
"""

import json
import os


class Resp:
    def __init__(self, text=None, calls=None, raw=None):
        self.text = text
        self.calls = calls or []
        self.raw = raw  # Claude only: the full reply, passed back unchanged next turn


# Neutral tool definitions. Each provider converts these to its own schema.
TOOLS = [
    {"name": "read_issue", "description": "Read a GitHub issue on the project by its number.",
     "properties": {"number": {"type": "integer"}}, "required": ["number"]},
    {"name": "list_files", "description": "List the files in the project repository.",
     "properties": {}, "required": []},
    {"name": "read_file", "description": "Read a file from the project repository.",
     "properties": {"path": {"type": "string"}}, "required": ["path"]},
    {"name": "post_comment", "description": "Post a public comment on a GitHub issue.",
     "properties": {"number": {"type": "integer"}, "body": {"type": "string"}},
     "required": ["number", "body"]},
]

# Models come from models.yaml (committed, no secrets). Each has a short name,
# e.g. `qwen` or `claude`, that --model accepts. API keys never go there: the
# SDKs read them from the environment (.env).
MODELS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "models.yaml")


def _load_config():
    try:
        import yaml
        with open(MODELS_FILE) as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}


_CONFIG = _load_config()
MODELS = _CONFIG.get("models") or {}  # short name -> {"provider", "name"}
DEFAULT_MODEL = _CONFIG.get("default", "qwen")


def _first_model(provider, fallback):
    """The first model listed for a provider is its default."""
    for entry in MODELS.values():
        if entry.get("provider") == provider and entry.get("name"):
            return entry["name"]
    return fallback


# An environment variable (e.g. OLLAMA_MODEL=...) still beats the file.
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL") or _first_model("anthropic", "claude-opus-5-5")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL") or _first_model("openai", "gpt-4o")

# Ollama runs locally and speaks the OpenAI API, so we reuse that adapter and
# just point the client at the local server. Default model is Qwen.
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL") or _first_model("ollama", "qwen3.5:4b")
OLLAMA_BASE_URL = (os.environ.get("OLLAMA_BASE_URL") or _CONFIG.get("ollama_url")
                   or "http://localhost:11434/v1")


def set_model(provider, name):
    """Override the model for one run (the --model flag)."""
    global ANTHROPIC_MODEL, OPENAI_MODEL, OLLAMA_MODEL
    if provider == "anthropic":
        ANTHROPIC_MODEL = name
    elif provider == "openai":
        OPENAI_MODEL = name
    elif provider == "ollama":
        OLLAMA_MODEL = name


def choose(model=None, provider=None):
    """Turn --model / --provider into the provider to use, setting its model.

    --model qwen          a short name from models.yaml: picks provider and model
    --model llama3.2:3b   any other tag: runs on --provider (default ollama)
    --provider anthropic  no --model: that provider's first model in models.yaml
    neither               the `default` short name in models.yaml
    """
    if model is None and provider is None:
        model = DEFAULT_MODEL
    if model in MODELS:
        entry = MODELS[model]
        provider = entry["provider"]
        if entry.get("name"):
            set_model(provider, entry["name"])
    elif model:
        provider = provider or "ollama"
        set_model(provider, model)
    if provider not in PROVIDERS:
        raise SystemExit(f"unknown provider '{provider}'. Use one of: {', '.join(PROVIDERS)}")
    return provider


PROVIDERS = ["ollama", "anthropic", "openai", "mock"]


def model_name(provider):
    return {"anthropic": ANTHROPIC_MODEL, "openai": OPENAI_MODEL,
            "ollama": f"{OLLAMA_MODEL} ({'Ollama cloud' if OLLAMA_MODEL.endswith(':cloud') else 'local'})",
            "mock": "mock (scripted replay)"}[provider]


# ---------------------------------------------------------------------------
# Anthropic (live)
# ---------------------------------------------------------------------------

def _anthropic_tools():
    return [
        {"name": t["name"], "description": t["description"],
         "input_schema": {"type": "object", "properties": t["properties"],
                          "required": t["required"]}}
        for t in TOOLS
    ]


def _anthropic_messages(history):
    msgs = []
    for turn in history:
        if turn["role"] == "user":
            msgs.append({"role": "user", "content": turn["text"]})
        elif turn["role"] == "assistant" and turn.get("raw"):
            msgs.append({"role": "assistant", "content": turn["raw"]})
        elif turn["role"] == "assistant":
            content = []
            if turn.get("text"):
                content.append({"type": "text", "text": turn["text"]})
            for c in turn.get("calls", []):
                content.append({"type": "tool_use", "id": c["id"],
                                "name": c["name"], "input": c["args"]})
            msgs.append({"role": "assistant", "content": content})
        elif turn["role"] == "tool":
            block = {"type": "tool_result", "tool_use_id": turn["id"],
                     "content": turn["content"]}
            # group with a preceding tool_result user message if present
            if msgs and msgs[-1]["role"] == "user" and isinstance(msgs[-1]["content"], list):
                msgs[-1]["content"].append(block)
            else:
                msgs.append({"role": "user", "content": [block]})
    return msgs


def _anthropic_create(system, messages, tools=None):
    import anthropic
    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from the environment
    extra = {"tools": tools} if tools else {}
    return client.beta.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=16000,
        system=system,
        messages=messages,
        output_config={"effort": "medium"},
        # If a safety classifier declines, retry on a fallback model automatically.
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        **extra,
    )


def _anthropic_text(resp):
    if resp.stop_reason == "refusal":
        return "(Claude declined this request)"
    return "".join(b.text for b in resp.content if b.type == "text") or None


def anthropic_call(system, history):
    resp = _anthropic_create(system, _anthropic_messages(history), _anthropic_tools())
    calls = []
    if resp.stop_reason != "refusal":
        calls = [{"id": b.id, "name": b.name, "args": dict(b.input)}
                 for b in resp.content if b.type == "tool_use"]
    return Resp(_anthropic_text(resp), calls, raw=[b.to_dict() for b in resp.content])


def anthropic_complete(system, user_text):
    """A plain, tool-less completion, used by the dual-LLM defence."""
    resp = _anthropic_create(system, [{"role": "user", "content": user_text}])
    return _anthropic_text(resp) or ""


# ---------------------------------------------------------------------------
# OpenAI (live) - kept so the code still runs against GPT if you switch
# ---------------------------------------------------------------------------

def _openai_tools():
    return [
        {"type": "function", "function": {
            "name": t["name"], "description": t["description"],
            "parameters": {"type": "object", "properties": t["properties"],
                           "required": t["required"]}}}
        for t in TOOLS
    ]


def _openai_messages(system, history):
    msgs = [{"role": "system", "content": system}]
    for turn in history:
        if turn["role"] == "user":
            msgs.append({"role": "user", "content": turn["text"]})
        elif turn["role"] == "assistant":
            tcs = [{"id": c["id"], "type": "function",
                    "function": {"name": c["name"], "arguments": json.dumps(c["args"])}}
                   for c in turn.get("calls", [])]
            msgs.append({"role": "assistant", "content": turn.get("text") or "",
                         "tool_calls": tcs or None})
        elif turn["role"] == "tool":
            msgs.append({"role": "tool", "tool_call_id": turn["id"],
                         "content": turn["content"]})
    return msgs


def _openai_client(base_url=None, api_key=None):
    from openai import OpenAI
    if base_url:
        return OpenAI(base_url=base_url, api_key=api_key or "not-needed")
    return OpenAI()


def _chat_call(client, model, system, history):
    resp = client.chat.completions.create(
        model=model, messages=_openai_messages(system, history),
        tools=_openai_tools(),
    )
    m = resp.choices[0].message
    calls = [{"id": tc.id or f"call_{i}", "name": tc.function.name,
              "args": json.loads(tc.function.arguments or "{}")}
             for i, tc in enumerate(m.tool_calls or [])]
    return Resp(m.content, calls)


def _chat_complete(client, model, system, user_text):
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": system},
                  {"role": "user", "content": user_text}],
    )
    return resp.choices[0].message.content


def openai_call(system, history):
    return _chat_call(_openai_client(), OPENAI_MODEL, system, history)


def openai_complete(system, user_text):
    return _chat_complete(_openai_client(), OPENAI_MODEL, system, user_text)


# ---------------------------------------------------------------------------
# Ollama (local, e.g. Qwen) - OpenAI-compatible endpoint, no key, no internet
# ---------------------------------------------------------------------------

def ollama_call(system, history):
    client = _openai_client(OLLAMA_BASE_URL, "ollama")
    try:
        return _chat_call(client, OLLAMA_MODEL, system, history)
    except Exception as e:
        if "does not support tools" in str(e):
            raise SystemExit(f"\n{OLLAMA_MODEL} can't call tools, so it can't drive MaintainerBot. "
                             "Pick another model from models.yaml.")
        if "not found" in str(e):
            raise SystemExit(f"\n{OLLAMA_MODEL} isn't downloaded. Run: ollama pull {OLLAMA_MODEL}")
        raise


def ollama_complete(system, user_text):
    client = _openai_client(OLLAMA_BASE_URL, "ollama")
    return _chat_complete(client, OLLAMA_MODEL, system, user_text)


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def call(provider, system, history):
    if provider == "anthropic":
        return anthropic_call(system, history)
    if provider == "openai":
        return openai_call(system, history)
    if provider == "ollama":
        return ollama_call(system, history)
    if provider == "mock":
        import mock_model
        return mock_model.respond(history)
    raise ValueError(f"unknown provider: {provider}")


def complete(provider, system, user_text):
    if provider == "anthropic":
        return anthropic_complete(system, user_text)
    if provider == "openai":
        return openai_complete(system, user_text)
    if provider == "ollama":
        return ollama_complete(system, user_text)
    if provider == "mock":
        import mock_model
        return mock_model.quarantine(user_text)
    raise ValueError(f"unknown provider: {provider}")
