from __future__ import annotations

import json
from typing import Any

import typer
from rich.console import Console

from .config import get_settings
from .db import open_db
from .lock import LockBusy, write_lock

app = typer.Typer(add_completion=False, help="sparkwatch — DGX Spark / GB10 forum monitor")
repos_app = typer.Typer(help="GitHub monitoring for watched repos")
app.add_typer(repos_app, name="repos")

console = Console()


def _print(payload: Any) -> None:
    console.print_json(json.dumps(payload, default=str))


def _exit_busy(exc: LockBusy) -> None:
    _print(exc.as_dict())
    raise typer.Exit(code=2)


@app.command()
def sync(
    forum: list[int] | None = typer.Option(None, "--forum", help="category id, repeatable"),
    max_pages: int | None = typer.Option(None, "--max-pages", help="stop after N listing pages"),
    days: int | None = typer.Option(None, "--days", help="override lookback_days"),
    budget: float | None = typer.Option(None, "--budget", help="time budget in seconds"),
) -> None:
    """Mirror the forums into SQLite."""
    from .sync import sync as run_sync

    settings = get_settings()
    try:
        with write_lock("sync", settings.db_path):
            result = run_sync(
                settings=settings,
                forum_ids=forum or None,
                max_pages=max_pages,
                lookback_days=days,
                time_budget_s=budget,
                trigger="cli",
            )
    except LockBusy as exc:
        _exit_busy(exc)
        return
    _print(result.as_dict())


@app.command()
def backfill(days: int = typer.Option(60, "--days", help="how far back to crawl")) -> None:
    """Widen the crawl window; never re-crawls from scratch."""
    from .sync import sync as run_sync

    settings = get_settings()
    try:
        with write_lock("sync", settings.db_path):
            result = run_sync(settings=settings, lookback_days=days, trigger="cli")
    except LockBusy as exc:
        _exit_busy(exc)
        return
    _print(result.as_dict())


@repos_app.command("check")
def repos_check(repo: str | None = typer.Option(None, "--repo", help="owner/name or URL")) -> None:
    """Check watched repos for a moved head, new releases and new tags."""
    from .repos import check_repos

    settings = get_settings()
    try:
        with write_lock("repos", settings.db_path):
            conn = open_db(settings)
            payload = check_repos(conn, settings, repo=repo)
    except LockBusy as exc:
        _exit_busy(exc)
        return
    _print(payload)


@app.command()
def digest(
    since: str = typer.Option("cursor", "--since"),
    cursor: str = typer.Option("default", "--cursor"),
    relevant_only: bool = typer.Option(False, "--relevant-only"),
    limit: int = typer.Option(30, "--limit"),
    no_advance: bool = typer.Option(False, "--no-advance"),
) -> None:
    """Show what changed since the cursor."""
    from .digest import build_digest, render_digest

    settings = get_settings()
    conn = open_db(settings)
    payload = build_digest(
        conn,
        settings,
        since=since,
        cursor=cursor,
        relevant_only=relevant_only,
        limit=limit,
        advance=not no_advance,
    )
    console.print(render_digest(payload))


@app.command("serve-mcp")
def serve_mcp() -> None:
    """Serve the MCP interface over stdio."""
    from .mcp_server import main

    main()


if __name__ == "__main__":
    app()
