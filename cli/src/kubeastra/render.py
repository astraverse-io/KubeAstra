"""Terminal rendering for streaming ReAct investigations.

The layout mirrors the hero terminal on https://kubeastra.io so the CLI
looks like what the marketing site promises. Colored step badges appear
in real time as each tool call fires, then a final root-cause panel
matches the site's warning/ok/muted palette.
"""

from __future__ import annotations

from typing import Iterator, Optional

from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

from .client import StreamEvent

# Tool → (short label, foreground color) mapping. The four categories mirror
# what the site's terminal mock shows (kubectl / metrics / logs / events /
# analyze) plus a few real tools that fire in the ReAct loop.
_TOOL_BADGES: dict[str, tuple[str, str]] = {
    "get_pods":               ("kubectl", "blue"),
    "get_pod_logs":           ("logs",    "cyan"),
    "get_events":             ("events",  "magenta"),
    "describe_pod":           ("kubectl", "blue"),
    "get_deployment":         ("kubectl", "blue"),
    "get_service":            ("kubectl", "blue"),
    "list_namespace_resources": ("kubectl", "blue"),
    "find_workload":          ("kubectl", "blue"),
    "investigate_pod":        ("analyze", "green"),
    "investigate_workload":   ("analyze", "green"),
    "investigate_node":       ("analyze", "green"),
    "analyze_namespace":      ("analyze", "green"),
    "prom_query":             ("metrics", "yellow"),
    "kb_search":              ("runbook", "cyan"),
    "get_recent_alerts":      ("alerts",  "red"),
}


def _tool_badge(tool: Optional[str]) -> Text:
    if not tool:
        return Text("thinking", style="bold dim")
    label, color = _TOOL_BADGES.get(tool, (tool, "white"))
    return Text(f" {label} ", style=f"bold black on {color}")


def render_stream(events: Iterator[StreamEvent], console: Optional[Console] = None) -> Optional[dict]:
    """Consume an SSE stream and render it live to the terminal.

    Returns the final ``result`` dict from the ``done`` event (or ``None``
    if the stream ended with an error). Errors are printed to the console
    rather than raised so callers can decide the exit code.
    """
    console = console or Console()
    lines: list[Text] = []
    answer_buffer: list[str] = []
    in_answer = False
    result: Optional[dict] = None

    def _render() -> Group:
        renderables: list = list(lines)
        if in_answer:
            renderables.append(Text(""))
            renderables.append(Text("".join(answer_buffer), style="default"))
        return Group(*renderables)

    with Live(_render(), console=console, refresh_per_second=12, transient=False) as live:
        for event in events:
            etype = event.type

            if etype == "start":
                lines.append(Text("$ kubeastra investigate", style="bold"))
                lines.append(Text(""))
            elif etype == "iteration_planned":
                if event.thought:
                    # Truncate thought preview to one line so the live view stays clean.
                    thought = event.thought.strip().replace("\n", " ")
                    if len(thought) > 100:
                        thought = thought[:97] + "…"
                    lines.append(Text(f"  thinking · {thought}", style="dim italic"))
            elif etype == "step_complete":
                badge = _tool_badge(event.action)
                preview = (event.preview or "").strip().replace("\n", " ")
                if len(preview) > 80:
                    preview = preview[:77] + "…"
                duration = f" ({event.duration_ms}ms)" if event.duration_ms is not None else ""
                line = Text.assemble(badge, Text(f" {preview}{duration}", style="default"))
                lines.append(line)
            elif etype == "answer_start":
                lines.append(Text(""))
                lines.append(Text("─ answer ─", style="bold dim"))
                in_answer = True
            elif etype == "token":
                if event.text:
                    answer_buffer.append(event.text)
            elif etype == "answer_end":
                # Freeze the streaming answer into the log so the final Panel
                # doesn't render underneath a still-updating token buffer.
                if answer_buffer:
                    lines.append(Text("".join(answer_buffer), style="default"))
                    answer_buffer.clear()
                in_answer = False
            elif etype == "done":
                result = event.result
            elif etype == "error":
                lines.append(Text(f"✗ {event.message or 'stream error'}", style="bold red"))
            # Other event types (thought_stream chunks, etc.) are absorbed
            # into iteration_planned above; nothing to render for them.

            live.update(_render())

    # Print a final root-cause panel if the result carries structured data.
    if result:
        panel = _root_cause_panel(result)
        if panel is not None:
            console.print()
            console.print(panel)

    return result


def _root_cause_panel(result: dict) -> Optional[Panel]:
    """Build the final root-cause panel from a ChatResponse.

    Matches the site's terminal mock: `Root cause` header, evidence,
    `Recommended:` line. Kept forgiving — if fields aren't shaped as
    expected, we fall back to plain text.
    """
    reply = (result.get("reply") or "").strip()
    tool_used = result.get("tool_used") or ""
    error = result.get("error")

    if error:
        body = Text(f"Error: {error}", style="bold red")
        return Panel(body, title="[bold red]Failed[/bold red]", border_style="red")

    if not reply:
        return None

    header = Text("Root cause", style="bold green")
    body = Group(header, Text(""), Markdown(reply))

    cost = result.get("cost_summary") or {}
    total_cost = cost.get("total_cost_usd")
    tokens = cost.get("total_tokens_out")
    footer_bits = []
    if tool_used:
        footer_bits.append(f"tool: {tool_used}")
    if total_cost is not None:
        footer_bits.append(f"cost: ${total_cost:.4f}")
    if tokens is not None:
        footer_bits.append(f"tokens: {tokens}")
    footer = " · ".join(footer_bits) if footer_bits else None

    return Panel(
        body,
        title="[bold cyan]KubeAstra[/bold cyan]",
        subtitle=f"[dim]{footer}[/dim]" if footer else None,
        border_style="cyan",
        padding=(1, 2),
    )
