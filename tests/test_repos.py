from __future__ import annotations

from sparkwatch.repos import check_repos, watch_repo

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

    github.add(f"{BASE}/commits/main", None, status=304)
    github.add(f"{BASE}/releases/latest", None, status=304)
    github.add(f"{BASE}/tags", None, status=304)
    check_repos(conn, settings, repo=REPO)

    assert conn.execute("SELECT COUNT(*) FROM repo_events").fetchone()[0] == 0
    assert github.requests[-1].headers.get("If-None-Match") is not None


def test_branch_gone_creates_event(settings, conn, router, github):
    baseline(github)
    github.install(router)
    watch_repo(conn, REPO, settings=settings)

    github.add(f"{BASE}/commits/main", {"message": "Not Found"}, status=404)
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

    conn.execute(
        "UPDATE repos SET last_checked_at='2025-01-01T00:00:00+00:00' WHERE url=?",
        (f"https://github.com/{REPO}",),
    )
    github.add(
        BASE,
        {"default_branch": "main", "archived": True, "description": "serving", "pushed_at": PAST},
    )
    github.add(f"{BASE}/commits/main", None, status=304)
    github.add(f"{BASE}/releases/latest", None, status=304)
    github.add(f"{BASE}/tags", None, status=304)
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


def test_link_discovery_never_watches(settings, conn):
    conn.execute(
        "INSERT INTO repos (owner, name, url, origin, watch, first_seen_at)"
        " VALUES ('o', 'n', 'https://github.com/o/n', 'forum', 0, ?)",
        (PAST,),
    )
    assert conn.execute("SELECT watch FROM repos").fetchone()[0] == 0
