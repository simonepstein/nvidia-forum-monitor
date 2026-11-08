from __future__ import annotations

import json
from typing import Any

import typer
from pydantic import ValidationError
from rich.console import Console

from .config import Settings, describe_validation_error, get_settings
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


def _settings_or_exit() -> Settings:
    """Load settings, turning an unsafe configuration into one clear line and exit 1."""
    try:
        return get_settings()
    except ValidationError as exc:
        console.print(f"error: {describe_validation_error(exc)}", style="red")
        raise typer.Exit(code=1) from None


@app.command()
def sync(
    forum: list[int] | None = typer.Option(None, "--forum", help="category id, repeatable"),
    max_pages: int | None = typer.Option(None, "--max-pages", help="stop after N listing pages"),
    days: int | None = typer.Option(None, "--days", help="override lookback_days"),
    budget: float | None = typer.Option(None, "--budget", help="time budget in seconds"),
    repair_coverage: bool = typer.Option(
        False,
        "--repair-coverage",
        help="reset the named forum's listing_floor to the oldest stored topic (dry run)",
    ),
    apply_repair: bool = typer.Option(
        False, "--apply", help="with --repair-coverage, write the change instead of a dry run"
    ),
) -> None:
    """Mirror the forums into SQLite."""
    from .sync import repair_coverage as run_repair
    from .sync import sync as run_sync

    settings = _settings_or_exit()
    try:
        with write_lock("sync", settings.db_path):
            if repair_coverage:
                if not forum or len(forum) != 1:
                    _print({"error": "--repair-coverage needs exactly one --forum ID"})
                    raise typer.Exit(code=1)
                conn = open_db(settings)
                _print(run_repair(conn, forum[0], apply=apply_repair))
                return
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

    settings = _settings_or_exit()
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

    settings = _settings_or_exit()
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
    limit: int = typer.Option(6, "--limit"),
    no_advance: bool = typer.Option(False, "--no-advance"),
    as_json: bool = typer.Option(False, "--json", help="print the JSON the MCP tool returns"),
) -> None:
    """Show what changed since the cursor."""
    from .digest import build_digest, render_digest

    settings = _settings_or_exit()
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
    if as_json:
        _print(payload)
    else:
        console.print(render_digest(payload))


@app.command("global-digest")
def global_digest(
    since: str = typer.Option("cursor", "--since"),
    until: str | None = typer.Option(None, "--until"),
    cursor: str = typer.Option("global", "--cursor"),
    forum: int | None = typer.Option(None, "--forum", help="category id"),
    limit: int = typer.Option(10, "--limit"),
    relevant_only: bool = typer.Option(False, "--relevant-only"),
    detail: bool = typer.Option(
        False, "--detail", help="include each topic's 2 most-liked new posts"
    ),
    no_advance: bool = typer.Option(False, "--no-advance"),
    as_json: bool = typer.Option(False, "--json", help="print the JSON the MCP tool returns"),
) -> None:
    """Show the forum's hot topics, ranked across both categories."""
    from .digest import global_digest as build_global_digest
    from .digest import render_global_digest

    settings = _settings_or_exit()
    conn = open_db(settings)
    payload = build_global_digest(
        conn,
        settings,
        since=since,
        until=until,
        cursor=cursor,
        forum_id=forum,
        limit=limit,
        relevant_only=relevant_only,
        detail=detail,
        advance=not no_advance,
    )
    if as_json:
        _print(payload)
    else:
        console.print(render_global_digest(payload))


@app.command("serve-mcp")
def serve_mcp() -> None:
    """Serve the MCP interface over stdio."""
    from .mcp_server import main

    main()


if __name__ == "__main__":
    app()
