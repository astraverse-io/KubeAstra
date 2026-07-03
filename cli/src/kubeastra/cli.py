"""KubeAstra CLI entry point.

Six commands total in this shim release:
  - ``kubeastra --version``          version info
  - ``kubeastra ask "..."``          natural-language investigation, streams
  - ``kubeastra investigate --pod``  scoped investigation shortcut
  - ``kubeastra connect``            pick a kubeconfig context
  - ``kubeastra config set/get``     backend URL + api token persistence
  - ``kubeastra doctor``             health-check the CLI + backend

Every command shares a friendly-error contract: connectivity failures
suggest the docker-compose spinup; auth failures suggest the config
command; backend errors show status + detail.
"""

from __future__ import annotations

import sys
import uuid
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from . import __version__
from .client import ApiError, AuthError, BackendError, Client
from .config import ALLOWED_KEYS, Config, config_path
from .render import render_stream


app = typer.Typer(
    name="kubeastra",
    help=(
        "AI-powered Kubernetes troubleshooting from the terminal. "
        "Uses your running KubeAstra backend (docker-compose or Helm)."
    ),
    add_completion=False,
    no_args_is_help=True,
)

config_app = typer.Typer(name="config", help="Manage CLI configuration.", no_args_is_help=True)
app.add_typer(config_app)

console = Console()
err_console = Console(stderr=True)


# ── Version + shared options ────────────────────────────────────────────────


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"kubeastra {__version__}")
        raise typer.Exit()


@app.callback()
def _main(
    version: Optional[bool] = typer.Option(
        None,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show the CLI version and exit.",
    ),
) -> None:
    """AI-powered Kubernetes troubleshooting from the terminal."""


# ── Helpers ─────────────────────────────────────────────────────────────────


def _client(
    backend_url: Optional[str] = None,
    api_token: Optional[str] = None,
) -> tuple[Client, Config]:
    """Build a Client using CLI flags -> config -> defaults, in that order."""
    cfg = Config.load()
    resolved_url = backend_url or cfg.backend_url
    resolved_token = api_token or cfg.api_token
    return Client(backend_url=resolved_url, api_token=resolved_token), cfg


def _ensure_session_id(cfg: Config) -> str:
    """Return the persisted session ID, generating and saving one on first use."""
    if not cfg.session_id:
        cfg.session_id = f"cli-{uuid.uuid4().hex[:16]}"
        cfg.save()
    return cfg.session_id


def _handle_and_exit(exc: Exception) -> None:
    """Render a friendly error message and exit with a non-zero code."""
    if isinstance(exc, AuthError):
        err_console.print(f"[bold red]auth error:[/bold red] {exc}")
        raise typer.Exit(code=2)
    if isinstance(exc, ApiError):
        err_console.print(f"[bold red]connection error:[/bold red] {exc}")
        err_console.print(
            "[dim]Is the backend running? Try `cd ui && docker compose up -d`, "
            "or set the URL with `kubeastra config set backend-url <url>`.[/dim]"
        )
        raise typer.Exit(code=3)
    if isinstance(exc, BackendError):
        err_console.print(f"[bold red]backend error {exc.status}:[/bold red] {exc.detail}")
        raise typer.Exit(code=4)
    err_console.print(f"[bold red]unexpected error:[/bold red] {exc}")
    raise typer.Exit(code=1)


# ── Commands ───────────────────────────────────────────────────────────────


@app.command()
def ask(
    question: str = typer.Argument(..., help="The question to investigate."),
    backend_url: Optional[str] = typer.Option(None, "--backend-url", help="Override the backend URL."),
    api_token: Optional[str] = typer.Option(None, "--api-token", help="Override the API token."),
    model: Optional[str] = typer.Option(None, "--model", help="Request a specific LLM model."),
) -> None:
    """Ask a natural-language question and stream the investigation.

    Example:
        kubeastra ask "why is checkout-service crashlooping in production?"
    """
    client, cfg = _client(backend_url, api_token)
    session_id = _ensure_session_id(cfg)
    try:
        events = client.stream_chat(
            message=question,
            session_id=session_id,
            model=model,
        )
        result = render_stream(events, console=console)
    except (ApiError, AuthError, BackendError) as exc:
        _handle_and_exit(exc)
    if result and result.get("error"):
        raise typer.Exit(code=5)


@app.command()
def investigate(
    pod: Optional[str] = typer.Option(None, "--pod", help="Investigate a specific pod."),
    ns: Optional[str] = typer.Option(None, "--ns", "--namespace", help="Namespace of the pod."),
    deployment: Optional[str] = typer.Option(None, "--deployment", help="Investigate a deployment."),
    node: Optional[str] = typer.Option(None, "--node", help="Investigate a node."),
    backend_url: Optional[str] = typer.Option(None, "--backend-url", help="Override the backend URL."),
    api_token: Optional[str] = typer.Option(None, "--api-token", help="Override the API token."),
) -> None:
    """Scoped investigation shortcut.

    Wraps ``ask`` with a canned prompt derived from the target.

    Examples:
        kubeastra investigate --pod api-gateway --ns production
        kubeastra investigate --deployment redis --ns default
        kubeastra investigate --node k8s-worker-01
    """
    if pod:
        target = f"pod {pod}" + (f" in namespace {ns}" if ns else "")
    elif deployment:
        target = f"deployment {deployment}" + (f" in namespace {ns}" if ns else "")
    elif node:
        target = f"node {node}"
    else:
        err_console.print(
            "[bold red]error:[/bold red] pass --pod, --deployment, or --node"
        )
        raise typer.Exit(code=1)

    question = f"Investigate {target} and explain any issues you find."
    ask(question=question, backend_url=backend_url, api_token=api_token, model=None)


@app.command()
def connect(
    context: Optional[str] = typer.Option(None, "--context", help="kubeconfig context name to use."),
    kubeconfig: Optional[str] = typer.Option(None, "--kubeconfig", help="Path to a kubeconfig file."),
    list_only: bool = typer.Option(False, "--list", help="List available contexts and exit."),
    backend_url: Optional[str] = typer.Option(None, "--backend-url", help="Override the backend URL."),
    api_token: Optional[str] = typer.Option(None, "--api-token", help="Override the API token."),
) -> None:
    """Pick a kubeconfig context for this CLI session.

    Without arguments, lists detected contexts. With ``--context``, connects.
    """
    client, cfg = _client(backend_url, api_token)
    session_id = _ensure_session_id(cfg)
    try:
        detect = client.autodetect_cluster()
    except (ApiError, AuthError, BackendError) as exc:
        _handle_and_exit(exc)
        return

    if detect.get("in_cluster"):
        console.print("[green]✓[/green] Backend is running in-cluster.")
        return

    contexts = detect.get("contexts") or []
    kubeconfig_path = kubeconfig or detect.get("kubeconfig_path")
    current = detect.get("current_context")

    if list_only or not context:
        if not contexts:
            console.print(
                f"[yellow]no contexts detected[/yellow] "
                f"({detect.get('message') or 'no kubeconfig found'})"
            )
            raise typer.Exit(code=0)
        table = Table(title="Available contexts", show_header=True, header_style="bold")
        table.add_column("Context")
        table.add_column("Cluster")
        table.add_column("Current", justify="center")
        for ctx in contexts:
            name = ctx.get("name", "?")
            cluster = ctx.get("cluster", "?")
            marker = "•" if name == current else ""
            table.add_row(name, cluster, marker)
        console.print(table)
        if not context:
            console.print(
                "[dim]Connect with:[/dim] kubeastra connect --context <name>"
            )
        raise typer.Exit(code=0)

    try:
        result = client.connect_context(
            session_id=session_id,
            context_name=context,
            mode="autodetect",
            kubeconfig_path=kubeconfig_path,
        )
    except (ApiError, AuthError, BackendError) as exc:
        _handle_and_exit(exc)
        return

    if result.get("connected"):
        console.print(
            f"[green]✓ connected[/green] "
            f"context=[bold]{result.get('context_name')}[/bold] "
            f"cluster=[bold]{result.get('cluster_name')}[/bold] "
            f"ns=[bold]{result.get('namespace')}[/bold]"
        )
    else:
        err_console.print(
            f"[red]✗ connect failed:[/red] {result.get('error') or 'unknown'}"
        )
        raise typer.Exit(code=1)


@app.command()
def doctor(
    backend_url: Optional[str] = typer.Option(None, "--backend-url", help="Override the backend URL."),
    api_token: Optional[str] = typer.Option(None, "--api-token", help="Override the API token."),
) -> None:
    """Health-check the CLI installation and the backend it points at."""
    cfg = Config.load()
    resolved_url = backend_url or cfg.backend_url

    checks: list[tuple[str, str, str]] = []

    checks.append(("cli version", "ok", __version__))
    checks.append(("config path", "ok", str(config_path())))
    checks.append(("backend url", "ok", resolved_url))
    checks.append(
        ("api token", "ok", "set" if (api_token or cfg.api_token) else "unset (single-user mode assumed)")
    )
    checks.append(("session id", "ok", cfg.session_id or "(will be generated on first use)"))

    # Try to reach the backend.
    client = Client(backend_url=resolved_url, api_token=api_token or cfg.api_token)
    try:
        detect = client.autodetect_cluster()
        checks.append(("backend reachable", "ok", "yes"))
        if detect.get("in_cluster"):
            checks.append(("cluster access", "ok", "in-cluster ServiceAccount"))
        elif detect.get("contexts"):
            count = len(detect["contexts"])
            checks.append(("cluster access", "ok", f"{count} kubeconfig context(s) available"))
        else:
            checks.append(
                ("cluster access", "warn", detect.get("message") or "no kubeconfig detected")
            )
    except AuthError:
        checks.append(("backend reachable", "warn", "yes, but auth required (set api-token)"))
    except ApiError as exc:
        checks.append(("backend reachable", "fail", str(exc)))
    except BackendError as exc:
        checks.append(("backend reachable", "warn", f"HTTP {exc.status}: {exc.detail}"))

    table = Table(show_header=True, header_style="bold")
    table.add_column("Check")
    table.add_column("Status", justify="center")
    table.add_column("Detail")
    status_style = {"ok": "green", "warn": "yellow", "fail": "red"}
    for name, status, detail in checks:
        table.add_row(
            name,
            f"[{status_style[status]}]{status}[/{status_style[status]}]",
            detail,
        )
    console.print(table)

    if any(status == "fail" for _, status, _ in checks):
        raise typer.Exit(code=1)


# ── Config sub-app ─────────────────────────────────────────────────────────


def _normalize_key(key: str) -> str:
    return key.replace("-", "_").lower()


@config_app.command("set")
def config_set(
    key: str = typer.Argument(..., help=f"Config key (one of: {', '.join(sorted(ALLOWED_KEYS))})."),
    value: str = typer.Argument(..., help="Value to set."),
) -> None:
    """Persist a config value at ~/.config/kubeastra/config.toml."""
    normalized = _normalize_key(key)
    if normalized not in ALLOWED_KEYS:
        err_console.print(
            f"[red]unknown key:[/red] {key} "
            f"(allowed: {', '.join(sorted(ALLOWED_KEYS))})"
        )
        raise typer.Exit(code=1)
    cfg = Config.load()
    setattr(cfg, normalized, value)
    cfg.save()
    console.print(
        f"[green]✓[/green] set [bold]{normalized}[/bold] in {config_path()}"
    )


@config_app.command("get")
def config_get(
    key: Optional[str] = typer.Argument(None, help="Specific key to fetch (omit to show all)."),
) -> None:
    """Read a config value (or all values) from ~/.config/kubeastra/config.toml."""
    cfg = Config.load()
    if key:
        normalized = _normalize_key(key)
        if normalized not in ALLOWED_KEYS:
            err_console.print(
                f"[red]unknown key:[/red] {key} "
                f"(allowed: {', '.join(sorted(ALLOWED_KEYS))})"
            )
            raise typer.Exit(code=1)
        value = getattr(cfg, normalized)
        if value is None:
            console.print(f"[dim](unset)[/dim]")
        else:
            typer.echo(str(value))
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("Key")
    table.add_column("Value")
    for name in sorted(ALLOWED_KEYS):
        value = getattr(cfg, name)
        rendered = str(value) if value else "[dim](unset)[/dim]"
        # Mask api_token so it doesn't leak in shared screenshots.
        if name == "api_token" and value:
            rendered = value[:4] + "…" + value[-4:] if len(value) > 8 else "***"
        table.add_row(name, rendered)
    console.print(table)


@config_app.command("path")
def config_path_cmd() -> None:
    """Print the config file location."""
    typer.echo(str(config_path()))


if __name__ == "__main__":  # pragma: no cover
    app()
