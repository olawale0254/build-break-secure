"""
log.py - the stage display, built with `rich`. Only printing lives here.
"""

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.prompt import Confirm
from rich.rule import Rule
from rich.text import Text

console = Console(highlight=False)

LAYER_NAMES = {1: "allowlist", 2: "human approval", 3: "dual LLM", 4: "output guard"}


def _short(text, limit=110):
    flat = " ".join(str(text).split())
    return flat[:limit] + ("…" if len(flat) > limit else "")


def _clip(text, lines):
    rows = text.strip().splitlines()
    if len(rows) <= lines:
        return "\n".join(rows)
    return "\n".join(rows[:lines]) + f"\n… ({len(rows) - lines} more lines)"


def header(layers, model):
    on = " + ".join(LAYER_NAMES[n] for n in sorted(layers)) if layers else "[red]none[/]"
    console.print()
    console.print(Rule(f"[bold]MaintainerBot[/]   [dim]model[/] [cyan]{model}[/]   [dim]defences[/] [bold]{on}[/]",
                       style="grey50"))
    console.print()


def task(text):
    console.print(Panel(escape(text), title="[bold]maintainer asks[/]", title_align="left",
                        border_style="blue", padding=(0, 2)))


def thinking(what="MaintainerBot is thinking"):
    return console.status(f"[dim]{what}…[/]", spinner="dots")


def tool(name, args):
    shown = ", ".join(f"{k}={_short(v, 40)!r}" if isinstance(v, str) else f"{k}={v}" for k, v in args.items())
    console.print(f"  [bold yellow]→ {name}[/][yellow]({escape(shown)})[/]")


def result(text):
    console.print(f"    [grey62]{escape(_short(text, console.width - 8))}[/]")


def blocked(reason):
    console.print(f"    [bold white on red] BLOCKED [/] [red]{escape(reason)}[/]")


def quarantined(summary):
    console.print(f"    [bold white on blue] DUAL LLM [/] [blue]bot only sees:[/] [grey62]{escape(_short(summary, console.width - 40))}[/]")


def ask_approval(number, body):
    console.print(Panel(escape(body.strip()), title=f"[bold]bot wants to post a PUBLIC comment on #{number}[/]",
                        title_align="left", border_style="yellow", padding=(0, 2)))
    if not console.is_terminal:
        console.print("    [dim]no human at the keyboard, so the answer is no[/]")
        return False
    try:
        return Confirm.ask("    [bold yellow]Approve?[/]", default=False)
    except (EOFError, KeyboardInterrupt):
        return False


def agent_says(text):
    console.print()
    console.print(Panel(escape(_clip(text or "(no reply)", 6)), title="[bold]MaintainerBot replies[/]",
                        title_align="left", border_style="green", padding=(0, 2)))


def public(content):
    body = Text(_clip(content, 14)) if content else Text("(nothing was posted)", style="dim")
    console.print(Panel(body, title="[bold]public on GitHub · anyone can read this[/]",
                        title_align="left", border_style="magenta", padding=(0, 2)))


def verdict(leaked):
    if leaked:
        msg, style = "  LEAKED  ·  private data is now public on GitHub  ", "bold white on red"
    else:
        msg, style = "  SAFE  ·  nothing sensitive went public  ", "bold white on green"
    console.print()
    console.print(Text(msg, style=style), justify="center")
    console.print()
