from __future__ import annotations

from conftest import add_github_spec

from sparkwatch.repos import check_repos, get_repo, watch_repo
from sparkwatch.util import dumps, loads

REPO = "vllm-project/vllm"
BASE = f"/repos/{REPO}"
SHA1 = "a" * 40
SHA2 = "b" * 40
PAST = "2026-01-01T00:00:00+00:00"


def baseline(github, sha: str = SHA1, release: str | None = "v0.11.0", tags=("v0.11.0",)) -> None:
    github.add(
        BASE,
        {"default_branch": "main", "archived": False, "description": "serving", "pushed_at": PAST},
    )
    github.add(f"{BASE}/commits/main", sha, headers={"ETag": '"head"'})
    if release:
        github.add(
            f"{BASE}/releases/latest",
            {
                "tag_name": release,
                "name": release,
                "body": "notes",
                "html_url": "u",
                "published_at": PAST,
            },
            headers={"ETag": '"rel"'},
        )
    else:
        github.add(f"{BASE}/releases/latest", {"message": "Not Found"}, status=404)
    github.add(f"{BASE}/tags", [{"name": tag} for tag in tags], headers={"ETag": '"tags"'})


def expire_metadata(conn) -> None:
    """Make the next check re-fetch metadata, without moving `last_checked_at`."""
    row = conn.execute(
        "SELECT id, etags FROM repos WHERE url=?", (f"https://github.com/{REPO}",)
    ).fetchone()
    etags = loads(row["etags"], {})
    etags["meta_checked_at"] = "2025-01-01T00:00:00+00:00"
    conn.execute("UPDATE repos SET etags=? WHERE id=?", (dumps(etags), row["id"]))


def test_watch_repo_baseline_records_head_without_events(settings, conn, router, github):
    baseline(github)
    github.install(router)
    payload = watch_repo(conn, REPO, settings=settings)

    assert payload["watch"] is True
    assert payload["head_sha"] == SHA1
    assert conn.execute("SELECT COUNT(*) FROM repo_events").fetchone()[0] == 0
    row = conn.execute(
        "SELECT * FROM repos WHERE url=?", (f"https://github.com/{REPO}",)
    ).fetchone()
    assert row["latest_release_tag"] == "v0.11.0"
    assert row["last_checked_at"] is not None


def test_head_moved_creates_one_event_with_prev_ref(settings, conn, router, github):
    baseline(github)
    github.install(router)
    watch_repo(conn, REPO, settings=settings)

    github.add(f"{BASE}/commits/main", SHA2)
    github.add(f"{BASE}/releases/latest", None, status=304)
    github.add(f"{BASE}/tags", None, status=304)
    github.add(
        f"{BASE}/compare/{SHA1}...{SHA2}",
        {
            "status": "ahead",
            "ahead_by": 2,
            "behind_by": 0,
            "total_commits": 2,
            "commits": [
                {
                    "sha": "c" * 40,
                    "html_url": "u1",
                    "commit": {
                        "message": "fix nccl\n\nbody",
                        "author": {"name": "a", "date": PAST},
                    },
                },
                {
                    "sha": "d" * 40,
                    "html_url": "u2",
                    "commit": {"message": "bump cuda", "author": {"name": "b", "date": PAST}},
                },
            ],
            "files": [
                {"filename": "vllm/core/x.py"},
                {"filename": "vllm/y.py"},
                {"filename": "README.md"},
            ],
        },
    )

    check_repos(conn, settings, repo=REPO)
    events = conn.execute("SELECT * FROM repo_events WHERE kind='head_moved'").fetchall()
    assert len(events) == 1
    assert events[0]["prev_ref"] == SHA1
    assert events[0]["ref"] == SHA2
    assert events[0]["commits_ahead"] == 2
    assert (
        conn.execute(
            "SELECT head_sha FROM repos WHERE url=?", (f"https://github.com/{REPO}",)
        ).fetchone()[0]
        == SHA2
    )


def test_unchanged_repo_returns_304_and_no_events(settings, conn, router, github):
    baseline(github)
    github.install(router)
    watch_repo(conn, REPO, settings=settings)

    add_github_spec(github, f"{BASE}/commits/main", "github_304.json")
    add_github_spec(github, f"{BASE}/releases/latest", "github_304.json")
    add_github_spec(github, f"{BASE}/tags", "github_304.json")
    check_repos(conn, settings, repo=REPO)

    assert conn.execute("SELECT COUNT(*) FROM repo_events").fetchone()[0] == 0
    assert github.requests[-1].headers.get("If-None-Match") is not None


def test_branch_gone_creates_event(settings, conn, router, github):
    baseline(github)
    github.install(router)
    watch_repo(conn, REPO, settings=settings)

    add_github_spec(github, f"{BASE}/commits/main", "github_branch_gone.json")
    github.add(f"{BASE}/releases/latest", None, status=304)
    github.add(f"{BASE}/tags", None, status=304)
    check_repos(conn, settings, repo=REPO)

    events = conn.execute("SELECT * FROM repo_events WHERE kind='branch_gone'").fetchall()
    assert len(events) == 1
    assert events[0]["ref"] == "main"


def test_new_release_and_tag_create_events(settings, conn, router, github):
    baseline(github)
    github.install(router)
    watch_repo(conn, REPO, settings=settings)

    github.add(f"{BASE}/commits/main", None, status=304)
    github.add(
        f"{BASE}/releases/latest",
        {
            "tag_name": "v0.12.0",
            "name": "v0.12.0",
            "body": "x" * 3000,
            "html_url": "u",
            "published_at": PAST,
        },
    )
    github.add(f"{BASE}/tags", [{"name": "v0.12.0"}, {"name": "v0.11.0"}])
    check_repos(conn, settings, repo=REPO)

    kinds = {row["kind"] for row in conn.execute("SELECT kind FROM repo_events")}
    assert kinds == {"release", "tag"}
    body = conn.execute("SELECT body_excerpt FROM repo_events WHERE kind='release'").fetchone()[0]
    assert len(body) == 2000


def test_archived_repo_creates_event(settings, conn, router, github):
    baseline(github)
    github.install(router)
    watch_repo(conn, REPO, settings=settings)

    expire_metadata(conn)
    github.add(
        BASE,
        {"default_branch": "main", "archived": True, "description": "serving", "pushed_at": PAST},
    )
    add_github_spec(github, f"{BASE}/commits/main", "github_304.json")
    add_github_spec(github, f"{BASE}/releases/latest", "github_304.json")
    add_github_spec(github, f"{BASE}/tags", "github_304.json")
    check_repos(conn, settings, repo=REPO)

    assert conn.execute("SELECT COUNT(*) FROM repo_events WHERE kind='archived'").fetchone()[0] == 1


def test_rate_limit_floor_stops_the_check(settings, conn, router, github):
    baseline(github)
    github.install(router)
    watch_repo(conn, REPO, settings=settings)

    github.add(f"{BASE}/commits/main", SHA2, headers={"X-RateLimit-Remaining": "2"})
    github.add(f"{BASE}/releases/latest", None, status=304)
    github.add(f"{BASE}/tags", None, status=304)
    payload = check_repos(conn, settings, repo=REPO)

    assert payload["stopped_early"] is True
    assert (
        conn.execute("SELECT COUNT(*) FROM repo_events WHERE kind='head_moved'").fetchone()[0] == 1
    )
    assert f"{BASE}/releases/latest" not in github.calls[-1]


def test_check_repos_stops_at_its_deadline(settings, conn, router, github):
    import time

    baseline(github)
    github.install(router)
    watch_repo(conn, REPO, settings=settings)
    calls_before = len(github.calls)

    payload = check_repos(conn, settings, repo=REPO, deadline=time.monotonic() - 1.0)

    assert payload["stopped_early"] is True
    assert payload["checked"] == 0
    assert payload["requests"] == 0
    assert len(github.calls) == calls_before


def test_link_discovery_never_watches(settings, conn):
    conn.execute(
        "INSERT INTO repos (owner, name, url, origin, watch, first_seen_at)"
        " VALUES ('o', 'n', 'https://github.com/o/n', 'forum', 0, ?)",
        (PAST,),
    )
    assert conn.execute("SELECT watch FROM repos").fetchone()[0] == 0


def test_renamed_repo_follows_the_redirect(settings, conn, router, github):
    baseline(github)
    github.install(router)
    watch_repo(conn, REPO, settings=settings)
    expire_metadata(conn)

    github.add(
        BASE,
        None,
        status=301,
        headers={"Location": "https://api.github.com/repositories/4242"},
    )
    github.add(
        "/repositories/4242",
        {
            "full_name": "NewOwner/NewName",
            "default_branch": "main",
            "archived": False,
            "description": "renamed",
            "pushed_at": PAST,
        },
    )
    add_github_spec(github, f"{BASE}/commits/main", "github_304.json")
    add_github_spec(github, f"{BASE}/releases/latest", "github_304.json")
    add_github_spec(github, f"{BASE}/tags", "github_304.json")
    check_repos(conn, settings, repo=REPO)

    row = conn.execute("SELECT owner, name, url FROM repos WHERE id=1").fetchone()
    assert row["owner"] == "newowner"
    assert row["name"] == "newname"
    assert row["url"] == "https://github.com/newowner/newname"


def test_rename_keeps_mentions_and_events(settings, conn, router, github):
    baseline(github)
    github.install(router)
    watch_repo(conn, REPO, settings=settings)
    conn.execute("INSERT OR IGNORE INTO forums (id, path, name) VALUES (723, 'p', 'n')")
    conn.execute(
        "INSERT INTO topics (id, forum_id, title, first_fetched_at) VALUES (900, 723, 't', ?)",
        (PAST,),
    )
    conn.execute(
        "INSERT INTO posts (id, topic_id, post_number, post_type, raw, cooked, content_hash,"
        " first_fetched_at, changed_at) VALUES (9001, 900, 1, 1, 'vllm', '<p>x</p>', 'h', ?, ?)",
        (PAST, PAST),
    )
    conn.execute(
        "INSERT INTO repo_mentions (repo_id, post_id, topic_id, url, mentioned_at)"
        " SELECT id, 9001, 900, url, ? FROM repos WHERE url=?",
        (PAST, f"https://github.com/{REPO}"),
    )
    expire_metadata(conn)
    github.add(
        BASE, None, status=301, headers={"Location": "https://api.github.com/repositories/4243"}
    )
    github.add(
        "/repositories/4243",
        {"full_name": "newowner/newname", "default_branch": "main", "archived": False},
    )
    add_github_spec(github, f"{BASE}/commits/main", "github_304.json")
    add_github_spec(github, f"{BASE}/releases/latest", "github_304.json")
    add_github_spec(github, f"{BASE}/tags", "github_304.json")

    check_repos(conn, settings, repo=REPO)

    mentions = conn.execute("SELECT COUNT(*) FROM repo_mentions").fetchone()[0]
    assert mentions == 1
    payload = get_repo(conn, "newowner/newname")
    assert payload is not None
    assert payload["repo"] == "newowner/newname"
    assert payload["mentions"]


def _queue_304s(github) -> None:
    github.add(f"{BASE}/commits/main", None, status=304)
    github.add(f"{BASE}/releases/latest", None, status=304)
    github.add(f"{BASE}/tags", None, status=304)


def test_metadata_refreshes_once_a_day_not_once_a_check(
    settings, conn, router, github, monkeypatch
):
    from datetime import UTC, datetime, timedelta

    from sparkwatch import repos as repos_module

    clock = {"now": datetime(2026, 11, 8, 12, 0, tzinfo=UTC)}
    monkeypatch.setattr(repos_module, "utcnow", lambda: clock["now"])

    baseline(github)
    github.install(router)
    watch_repo(conn, REPO, settings=settings)
    assert github.calls.count(BASE) == 1

    clock["now"] += timedelta(hours=2)
    _queue_304s(github)
    check_repos(conn, settings, repo=REPO)
    assert github.calls.count(BASE) == 1

    clock["now"] += timedelta(hours=23)
    check_repos(conn, settings, repo=REPO)
    assert github.calls.count(BASE) == 2


def test_metadata_304_counts_as_a_metadata_check(settings, conn, router, github, monkeypatch):
    from datetime import UTC, datetime

    from sparkwatch import repos as repos_module
    from sparkwatch.util import dumps, loads

    clock = {"now": datetime(2026, 11, 8, 12, 0, tzinfo=UTC)}
    monkeypatch.setattr(repos_module, "utcnow", lambda: clock["now"])

    baseline(github)
    github.install(router)
    watch_repo(conn, REPO, settings=settings)

    row = conn.execute(
        "SELECT id, etags FROM repos WHERE url=?", (f"https://github.com/{REPO}",)
    ).fetchone()
    etags = loads(row["etags"], {})
    etags["meta_checked_at"] = "2026-11-01T00:00:00+00:00"
    conn.execute("UPDATE repos SET etags=? WHERE id=?", (dumps(etags), row["id"]))

    github.add(BASE, None, status=304, headers={"ETag": '"meta"'})
    _queue_304s(github)
    before = github.calls.count(BASE)

    check_repos(conn, settings, repo=REPO)
    assert github.calls.count(BASE) == before + 1
    etags = loads(
        conn.execute("SELECT etags FROM repos WHERE id=?", (row["id"],)).fetchone()[0], {}
    )
    assert etags["meta_checked_at"] == "2026-11-08T12:00:00+00:00"

    check_repos(conn, settings, repo=REPO)
    assert github.calls.count(BASE) == before + 1


def test_archived_event_is_created_once(settings, conn, router, github, monkeypatch):
    from datetime import UTC, datetime, timedelta

    from sparkwatch import repos as repos_module

    clock = {"now": datetime(2026, 11, 8, 12, 0, tzinfo=UTC)}
    monkeypatch.setattr(repos_module, "utcnow", lambda: clock["now"])

    baseline(github)
    github.install(router)
    watch_repo(conn, REPO, settings=settings)

    github.add(
        BASE,
        {"default_branch": "main", "archived": True, "description": "d", "pushed_at": PAST},
    )
    _queue_304s(github)

    clock["now"] += timedelta(hours=25)
    check_repos(conn, settings, repo=REPO)
    clock["now"] += timedelta(hours=25)
    check_repos(conn, settings, repo=REPO)

    assert conn.execute("SELECT COUNT(*) FROM repo_events WHERE kind='archived'").fetchone()[0] == 1
