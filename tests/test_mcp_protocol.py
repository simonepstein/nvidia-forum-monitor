from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SNAPSHOT = Path(__file__).resolve().parent / "snapshots" / "tools.json"

EXPECTED_PROMPTS = {"catch_up", "serve_model", "should_i_update", "frontier"}
EXPECTED_RESOURCES = {
    "sparkwatch://watchlist",
    "sparkwatch://flags/open",
    "sparkwatch://notes/latest",
}

PAST = "2026-01-01T00:00:00+00:00"


def server_params(db_path: Path) -> StdioServerParameters:
    env = dict(os.environ)
    env["SPARKWATCH_GENERAL__DB_PATH"] = str(db_path)
    env["SPARKWATCH_FORUM__REQUESTS_PER_SECOND"] = "1000"
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "sparkwatch.mcp_server"],
        env=env,
    )


def seed_db(db_path: Path) -> None:
    from sparkwatch.config import get_settings, reset_settings
    from sparkwatch.db import open_db

    os.environ["SPARKWATCH_GENERAL__DB_PATH"] = str(db_path)
    reset_settings()
    conn = open_db(get_settings())
    conn.execute(
        "INSERT INTO topics (id, forum_id, title, created_at, first_fetched_at)"
        " VALUES (800, 723, 'protocol topic', ?, ?)",
        (PAST, PAST),
    )
    for number in range(1, 4):
        conn.execute(
            "INSERT INTO posts (id, topic_id, post_number, post_type, username, created_at, raw,"
            " cooked, content_hash, first_fetched_at, changed_at)"
            " VALUES (?, 800, ?, 1, 'alice', ?, ?, '<p>x</p>', ?, ?, ?)",
            (
                8000 + number,
                number,
                PAST,
                f"post number {number} with a long body " * 5,
                f"h{number}",
                PAST,
                PAST,
            ),
        )
    reset_settings()


async def with_session(db_path: Path, scenario):
    params = server_params(db_path)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await scenario(session)


def test_list_tools_matches_snapshot(settings, tmp_path):
    db_path = settings.db_path
    seed_db(db_path)

    async def scenario(session):
        result = await session.list_tools()
        return {tool.name: tool for tool in result.tools}

    tools = asyncio.run(with_session(db_path, scenario))
    names = sorted(tools)

    if os.environ.get("SPARKWATCH_WRITE_SNAPSHOT") == "1":
        SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOT.write_text(json.dumps(names, indent=2) + "\n")

    assert SNAPSHOT.is_file(), "missing tests/snapshots/tools.json"
    assert names == json.loads(SNAPSHOT.read_text())

    for required in (
        "sync_now",
        "digest",
        "get_posts",
        "record_recipe",
        "sql_query",
        "repo_status",
    ):
        assert required in tools

    get_posts = tools["get_posts"].inputSchema
    assert get_posts["required"] == ["topic_id"]
    for optional in ("last_n", "max_chars", "next", "since", "username"):
        assert optional in get_posts["properties"]

    record_recipe = tools["record_recipe"].inputSchema
    assert set(record_recipe["required"]) == {"model", "topology", "status"}
    assert "evidence" in record_recipe["properties"]
    assert "supersedes" in record_recipe["properties"]


def test_prompts_and_resources(settings):
    db_path = settings.db_path
    seed_db(db_path)

    async def scenario(session):
        prompts = await session.list_prompts()
        resources = await session.list_resources()
        return prompts, resources

    prompts, resources = asyncio.run(with_session(db_path, scenario))
    assert {prompt.name for prompt in prompts.prompts} == EXPECTED_PROMPTS
    assert {str(resource.uri) for resource in resources.resources} == EXPECTED_RESOURCES


def test_get_posts_truncation_round_trip(settings):
    db_path = settings.db_path
    seed_db(db_path)

    async def scenario(session):
        first = await session.call_tool("get_posts", {"topic_id": 800, "max_chars": 30})
        payload = json.loads(first.content[0].text)
        collected = payload["text"]
        token = payload.get("next")
        truncated = payload["truncated"]
        while token:
            page = await session.call_tool(
                "get_posts", {"topic_id": 800, "max_chars": 30, "next": token}
            )
            body = json.loads(page.content[0].text)
            collected += body["text"]
            token = body.get("next")
        full = await session.call_tool("get_posts", {"topic_id": 800, "max_chars": 100000})
        complete = json.loads(full.content[0].text)["text"]
        return truncated, collected, complete

    truncated, collected, complete = asyncio.run(with_session(db_path, scenario))
    assert truncated is True
    assert collected == complete


def test_errors_are_tool_errors_and_server_survives(settings):
    db_path = settings.db_path
    seed_db(db_path)

    async def scenario(session):
        missing = await session.call_tool("get_posts", {"topic_id": 424242})
        bad_sql = await session.call_tool("sql_query", {"sql": "DELETE FROM posts"})
        still_alive = await session.list_tools()
        return missing, bad_sql, still_alive

    missing, bad_sql, still_alive = asyncio.run(with_session(db_path, scenario))
    assert missing.isError is True
    assert "unknown topic" in missing.content[0].text
    assert bad_sql.isError is True
    assert "only SELECT or WITH" in bad_sql.content[0].text
    assert len(still_alive.tools) > 0


def test_resource_contents_are_json(settings):
    db_path = settings.db_path
    seed_db(db_path)

    async def scenario(session):
        return await session.read_resource("sparkwatch://watchlist")

    result = asyncio.run(with_session(db_path, scenario))
    payload = json.loads(result.contents[0].text)
    assert isinstance(payload, list)
    assert any(row["label"] == "GLM 5.3 Flash" for row in payload)


def test_prompt_renders_workflow(settings):
    db_path = settings.db_path
    seed_db(db_path)

    async def scenario(session):
        return await session.get_prompt("serve_model", {"model": "GLM 5.3 Flash"})

    result = asyncio.run(with_session(db_path, scenario))
    text = result.messages[0].content.text
    assert "model_summary" in text
    assert "GLM 5.3 Flash" in text


@pytest.mark.parametrize("name", sorted(EXPECTED_PROMPTS))
def test_all_prompts_render(settings, name):
    db_path = settings.db_path
    seed_db(db_path)

    async def scenario(session):
        arguments = {}
        if name == "serve_model":
            arguments = {"model": "GLM 5.3 Flash"}
        return await session.get_prompt(name, arguments)

    result = asyncio.run(with_session(db_path, scenario))
    assert result.messages
