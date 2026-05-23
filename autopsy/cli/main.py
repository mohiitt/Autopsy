"""autopsy CLI commands.

Commands:
  autopsy run <script.py>     start server + run user agent + open browser
  autopsy serve               start server only
  autopsy sessions            list saved sessions
  autopsy diagnose <id>       diagnose a saved session
  autopsy replay <id>         simulated replay of a saved session
  autopsy clean               delete old sessions
  autopsy deploy              export sessions for sharing (v1 fallback)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import runpy
import sys
import threading
import time
import webbrowser
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

console = Console()


def _get_port() -> int:
    try:
        return int(os.environ.get("AUTOPSY_PORT", "7823"))
    except ValueError:
        return 7823


def _get_host() -> str:
    return os.environ.get("AUTOPSY_HOST", "127.0.0.1")


def _session_dir() -> Path:
    from autopsy.core.tracer import _default_session_dir
    return _default_session_dir()


@click.group()
@click.version_option(package_name="autopsy")
def cli() -> None:
    """autopsy - your agent died. here's why."""


@cli.command("run")
@click.argument("script", type=click.Path(exists=True, dir_okay=False))
@click.option("--port", default=None, type=int, help="Server port (default: 7823)")
@click.option("--host", default=None, help="Server host (default: 127.0.0.1)")
@click.option("--no-browser", is_flag=True, help="Don't auto-open browser")
@click.option("--debug", is_flag=True, help="Enable debug logging")
def cmd_run(script: str, port: int, host: str, no_browser: bool, debug: bool) -> None:
    """Run an agent script with autopsy tracing + dashboard."""
    if debug:
        logging.basicConfig(level=logging.DEBUG)
    port = port or _get_port()
    host = host or _get_host()
    _start_server_and_run(script, port=port, host=host, open_browser=not no_browser)


@cli.command("serve")
@click.option("--port", default=None, type=int)
@click.option("--host", default=None)
@click.option("--no-browser", is_flag=True)
def cmd_serve(port: int, host: str, no_browser: bool) -> None:
    """Start the dashboard server without running an agent."""
    port = port or _get_port()
    host = host or _get_host()
    _start_server(port=port, host=host, open_browser=not no_browser)


def _start_server(port: int, host: str, open_browser: bool) -> None:
    """Start uvicorn server in the foreground."""
    import uvicorn
    from autopsy.server.app import app
    if open_browser and not os.environ.get("AUTOPSY_NO_BROWSER"):
        threading.Timer(0.8, lambda: _open_browser(host, port)).start()
    console.print(
        f"[bold]autopsy[/bold] listening at "
        f"[link]http://{host}:{port}[/link]")
    config = uvicorn.Config(app, host=host, port=port, log_level="warning",
                            access_log=False)
    server = uvicorn.Server(config)
    server.run()


def _open_browser(host: str, port: int) -> None:
    try:
        url = f"http://{host}:{port}"
        webbrowser.open(url)
    except Exception:
        pass


def _start_server_and_run(script: str, *, port: int, host: str, open_browser: bool) -> None:
    """Run uvicorn in a background thread, then run the user script in the main thread."""
    import uvicorn
    from autopsy.server.app import app

    config = uvicorn.Config(app, host=host, port=port, log_level="warning",
                            access_log=False)
    server = uvicorn.Server(config)

    server_thread = threading.Thread(
        target=lambda: server.run(), daemon=True, name="autopsy-server")
    server_thread.start()

    # Wait briefly for server to become ready.
    for _ in range(40):
        time.sleep(0.05)
        if getattr(server, "started", False):
            break

    console.print(
        f"[bold]autopsy[/bold] listening at "
        f"[link]http://{host}:{port}[/link]\n"
        f"Running [cyan]{script}[/cyan]...\n")

    if open_browser and not os.environ.get("AUTOPSY_NO_BROWSER"):
        threading.Timer(0.5, lambda: _open_browser(host, port)).start()

    # Run the user script.
    abs_script = str(Path(script).resolve())
    sys.path.insert(0, str(Path(abs_script).parent))
    exit_code = 0
    try:
        runpy.run_path(abs_script, run_name="__main__")
    except SystemExit as e:
        exit_code = e.code or 0
    except KeyboardInterrupt:
        console.print("[yellow]script interrupted[/yellow]")
    except Exception:
        console.print_exception()
        exit_code = 1

    # Print session summary.
    try:
        from autopsy.core.tracer import list_sessions
        sessions = list_sessions(_session_dir())
        if sessions:
            console.print(
                f"\n[green]✓[/green] {len(sessions)} session(s) recorded. "
                "Dashboard stays open - press Ctrl+C to exit.\n")
    except Exception:
        pass

    # Keep the server alive until user kills it.
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        console.print("[dim]bye[/dim]")
    sys.exit(exit_code)


@cli.command("sessions")
def cmd_sessions() -> None:
    """List recorded sessions."""
    from autopsy.core.tracer import list_sessions
    sessions = list_sessions(_session_dir())
    if not sessions:
        console.print(
            "[yellow]No sessions yet. "
            "Run [cyan]autopsy run agent.py[/cyan] to record one.[/yellow]")
        return
    table = Table(title="autopsy sessions", show_lines=False)
    table.add_column("session_id", style="dim", overflow="fold")
    table.add_column("agent")
    table.add_column("status")
    table.add_column("nodes", justify="right")
    table.add_column("errors", justify="right")
    table.add_column("tokens", justify="right")
    table.add_column("dur (ms)", justify="right")
    table.add_column("created", style="dim")
    for s in sessions[:50]:
        from datetime import datetime
        created = datetime.fromtimestamp(s.get("created_at", 0))
        status = s.get("status", "?")
        color = "green" if status == "success" else (
            "red" if status == "error" else "yellow")
        table.add_row(
            s.get("session_id", "")[:18],
            s.get("agent_name", "")[:30],
            f"[{color}]{status}[/{color}]",
            str(s.get("node_count", 0)),
            str(s.get("error_count", 0)),
            str(s.get("total_tokens", 0)),
            str(int(s.get("total_duration_ms", 0))),
            created.strftime("%Y-%m-%d %H:%M:%S"),
        )
    console.print(table)


@cli.command("diagnose")
@click.argument("session_id")
@click.option("--node", "node_id", default=None, help="Specific node to diagnose")
@click.option("--model", default="auto",
              type=click.Choice(["auto", "gmi", "gemini"]))
def cmd_diagnose(session_id: str, node_id: str, model: str) -> None:
    """Diagnose a saved session with the AI debugger."""
    from autopsy.core.tracer import load_bundle
    from autopsy.diagnostics.gemini_agent import GeminiAgent, estimate_bundle_tokens
    from autopsy.diagnostics.gmi_agent import GMIAgent

    bundle = load_bundle(_session_dir(), session_id)
    if bundle is None:
        # Try partial match.
        from autopsy.core.tracer import list_sessions
        candidates = [s for s in list_sessions(_session_dir())
                      if s.get("session_id", "").startswith(session_id)]
        if len(candidates) == 1:
            bundle = load_bundle(_session_dir(), candidates[0]["session_id"])
        elif len(candidates) > 1:
            console.print(f"[red]ambiguous session prefix '{session_id}' — "
                          f"{len(candidates)} matches[/red]")
            return
    if bundle is None:
        console.print(f"[red]session {session_id!r} not found[/red]")
        return

    if model == "gemini":
        agent = GeminiAgent()
    elif model == "gmi":
        agent = GMIAgent()
    else:
        est = estimate_bundle_tokens(bundle)
        agent = GeminiAgent() if est > 32_000 else GMIAgent()
    console.print(f"[dim]Diagnosing with {type(agent).__name__}...[/dim]")
    result = asyncio.run(agent.diagnose(bundle, node_id))
    console.print()
    console.print(f"[bold]🔍 Root cause[/bold]\n{result.root_cause}\n")
    console.print(
        f"[dim]Node:[/dim] {result.affected_node_name} "
        f"([dim]{result.affected_node_id}[/dim])")
    console.print(f"[dim]Category:[/dim] {result.error_category}")
    console.print(f"[dim]Confidence:[/dim] {result.confidence:.0%}\n")
    console.print(f"[bold]💡 Fix[/bold]\n{result.fix_suggestion}\n")
    if result.fix_code_snippet:
        console.print("[bold]Code snippet[/bold]")
        console.print(result.fix_code_snippet)
        console.print()
    if result.latency_insight:
        console.print(
            f"[bold]⚡ Latency[/bold]\n{result.latency_insight}")
        if result.estimated_latency_savings_ms:
            console.print(
                f"[dim]Est. savings: "
                f"{int(result.estimated_latency_savings_ms)}ms[/dim]")


@cli.command("replay")
@click.argument("session_id")
@click.option("--from-node", "node_id", default=None)
@click.option("--fix", default="Applied developer fix")
def cmd_replay(session_id: str, node_id: str, fix: str) -> None:
    """Simulated replay of a saved session from the first error node."""
    from autopsy.core.replay import ReplayEngine
    from autopsy.core.tracer import load_bundle

    bundle = load_bundle(_session_dir(), session_id)
    if bundle is None:
        console.print(f"[red]session {session_id!r} not found[/red]")
        return
    if not node_id:
        for ev in bundle.events:
            if ev.get("event_type") == "node_error":
                node_id = ev.get("node_id")
                break
    if not node_id:
        console.print(
            "[yellow]No error node found — pass --from-node NODE_ID[/yellow]")
        return
    engine = ReplayEngine(bundle)
    result = engine.simulated_replay(node_id, fix)
    comp = result["comparison"]
    console.print(f"[bold]↻ Replay from {node_id[:8]}[/bold]")
    console.print(f"fix: {fix}\n")
    table = Table(show_header=True)
    table.add_column("metric")
    table.add_column("original")
    table.add_column("replay")
    table.add_column("delta")
    table.add_row("status", comp["original"]["status"],
                  f"[green]{comp['replay']['status']}[/green]", "—")
    table.add_row("errors", str(comp["original"]["errors"]),
                  str(comp["replay"]["errors"]),
                  f"-{comp['original']['errors']-comp['replay']['errors']}")
    table.add_row("duration_ms", f"{int(comp['original']['duration_ms'])}",
                  f"{int(comp['replay']['duration_ms'])}",
                  f"{int(comp['latency_delta_ms'])}")
    table.add_row("tokens", str(comp["original"]["tokens"]),
                  str(comp["replay"]["tokens"]), str(comp["token_delta"]))
    console.print(table)
    console.print(f"[dim]{result['side_effect_warning']}[/dim]")


@cli.command("clean")
@click.option("--all", "all_", is_flag=True, help="Delete all sessions")
def cmd_clean(all_: bool) -> None:
    """Delete saved sessions."""
    sd = _session_dir()
    if not sd.exists():
        console.print("[dim]nothing to clean[/dim]")
        return
    if not all_:
        console.print(
            "[yellow]Pass --all to confirm deleting every session[/yellow]")
        return
    count = 0
    for p in sd.glob("*.json"):
        try:
            p.unlink()
            count += 1
        except Exception:
            pass
    console.print(f"[green]deleted {count} session files[/green]")


@cli.command("deploy")
@click.option("--out", default="autopsy-export.json")
def cmd_deploy(out: str) -> None:
    """Export sessions as a shareable JSON bundle (v1 fallback)."""
    from autopsy.core.tracer import list_sessions
    sd = _session_dir()
    sessions = list_sessions(sd)
    bundles = []
    for s in sessions:
        p = sd / f"{s['session_id']}.json"
        if p.exists():
            try:
                bundles.append(json.loads(p.read_text()))
            except Exception:
                pass
    Path(out).write_text(json.dumps(
        {"version": "1", "sessions": bundles}, default=str))
    console.print(f"[green]exported {len(bundles)} sessions to {out}[/green]")
    console.print(
        "[dim]Send this file to a teammate; they can view it with "
        "[cyan]autopsy serve[/cyan] after dropping it into "
        "~/.autopsy/sessions/[/dim]")


if __name__ == "__main__":
    cli()
