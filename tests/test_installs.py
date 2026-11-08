from __future__ import annotations

import pytest
from conftest import synthetic

from sparkwatch.repos import (
    clear_installed,
    list_installs,
    repo_status,
    set_installed,
    watch_repo,
)

REPO = "vllm-project/vllm"
BASE = f"/repos/{REPO}"
HEAD = "1" * 40
OLD = "2" * 40
PAST = "2026-01-01T00:00:00+00:00"
BEFORE = "2025-06-01T00:00:00+00:00"

COMPARE = {
    "status": "ahead",
    "ahead_by": 3,
    "behind_by": 0,
    "total_commits": 3,
    "commits": [
        {
            "sha": f"{index}" * 40,
            "html_url": f"u{index}",
            "commit": {
                "message": f"commit {index}",
                "author": {"name": f"dev{index}", "date": PAST},
            },
        }
        for index in range(1, 4)
    ],
    "files": [{"filename": "vllm/a.py"}, {"filename": "vllm/b.py"}, {"filename": "docs/c.md"}],
}


def prepare(settings, conn, router, github):
    github.add(
        BASE, {"default_branch": "main", "archived": False, "description": "d", "pushed_at": PAST}
    )
    github.add(f"{BASE}/commits/main", HEAD, headers={"ETag": '"h"'})
    github.add(
        f"{BASE}/commits/{OLD}",
        {"sha": OLD, "commit": {"message": "old commit", "author": {"date": PAST}}},
    )
    github.add(f"{BASE}/releases/latest", {"message": "Not Found"}, status=404)
    github.add(f"{BASE}/tags", [], headers={"ETag": '"t"'})
    github.install(router)
    watch_repo(conn, REPO, settings=settings)


def test_set_installed_resolves_short_sha(settings, conn, router, github):
    prepare(settings, conn, router, github)
    github.add(
        f"{BASE}/commits/abc1234",
        {"sha": OLD, "commit": {"message": "old commit", "author": {"date": PAST}}},
    )
    github.add(f"{BASE}/compare/{OLD}...{HEAD}", COMPARE)

    payload = set_installed(conn, REPO, "abc1234", installed_at=PAST, settings=settings)
    assert payload["sha"] == OLD
    assert payload["subject"] == "old commit"
    assert payload["status"]["status"] == "behind"
    assert payload["status"]["behind_by"] == 3
    assert payload["status"]["ahead_by"] == 0


def test_set_installed_resolves_a_full_sha_and_returns_its_subject(settings, conn, router, github):
    prepare(settings, conn, router, github)
    github.add(f"{BASE}/compare/{OLD}...{HEAD}", COMPARE)

    payload = set_installed(conn, REPO, OLD, installed_at=PAST, settings=settings)

    assert payload["sha"] == OLD
    assert payload["subject"] == "old commit"
    assert payload["commit_date"] == PAST
    assert f"{BASE}/commits/{OLD}" in github.calls
    assert "hint" not in payload


def test_set_installed_rejects_an_unknown_full_sha(settings, conn, router, github):
    prepare(settings, conn, router, github)
    missing = "f" * 40

    with pytest.raises(ValueError, match="not found"):
        set_installed(conn, REPO, missing, installed_at=PAST, settings=settings)

    assert conn.execute("SELECT COUNT(*) FROM repo_installs").fetchone()[0] == 0


def test_set_installed_hints_when_the_repo_is_not_watched(settings, conn, router, github):
    repo = "o/n"
    github.add(
        f"/repos/{repo}/commits/{OLD}",
        {"sha": OLD, "commit": {"message": "x", "author": {"date": PAST}}},
    )
    github.add(f"/repos/{repo}/commits/HEAD", HEAD)
    github.install(router)

    payload = set_installed(conn, repo, OLD, installed_at=PAST, settings=settings)

    assert "watch_repo" in payload["hint"]
    assert repo_status(conn, settings)["repos"] == []


def test_repo_status_reports_commits_releases_and_mentions(settings, conn, router, github):
    prepare(settings, conn, router, github)
    github.add(f"{BASE}/compare/{OLD}...{HEAD}", COMPARE)
    set_installed(conn, REPO, OLD, installed_at=PAST, settings=settings)

    conn.execute(
        "INSERT INTO repo_events (repo_id, kind, ref, title, occurred_at, detected_at)"
        " SELECT id, 'release', 'v0.12.0', 'v0.12.0', ?, ? FROM repos WHERE url=?",
        (PAST, PAST, f"https://github.com/{REPO}"),
    )
    conn.execute("INSERT OR IGNORE INTO forums (id, path, name) VALUES (723, 'p', 'n')")
    conn.execute(
        "INSERT INTO topics (id, forum_id, title, first_fetched_at) VALUES (900, 723, 'vllm thread', ?)",
        (PAST,),
    )
    conn.execute(
        "INSERT INTO posts (id, topic_id, post_number, post_type, raw, cooked, content_hash,"
        " first_fetched_at, changed_at) VALUES (9001, 900, 1, 1, 'vllm fixed it', '<p>x</p>', 'h', ?, ?)",
        (PAST, PAST),
    )
    conn.execute(
        "INSERT INTO repo_mentions (repo_id, post_id, topic_id, url, context_snippet, mentioned_at)"
        " SELECT id, 9001, 900, 'https://github.com/vllm-project/vllm', 'vllm fixed it', ?"
        " FROM repos WHERE url=?",
        (PAST, f"https://github.com/{REPO}"),
    )

    status = repo_status(conn, settings, repo=REPO)
    entry = status["repos"][0]
    assert entry["status"] == "behind"
    assert entry["behind_by"] == 3
    assert entry["ahead_by"] == 0
    assert entry["commits_total"] == 3
    assert entry["commits"][0]["subject"] == "commit 3"
    assert entry["changed_dirs"][0] == {"path": "vllm", "files": 2}
    assert entry["releases"][0]["ref"] == "v0.12.0"
    assert entry["forum_mentions"][0]["post_id"] == 9001


def test_repo_status_and_digest_agree_on_direction(settings, conn, router, github):
    from sparkwatch.digest import build_digest

    prepare(settings, conn, router, github)
    github.add(f"{BASE}/compare/{OLD}...{HEAD}", COMPARE)
    set_installed(conn, REPO, OLD, installed_at=PAST, settings=settings)
    repo_status(conn, settings, repo=REPO)

    entry = repo_status(conn, settings, repo=REPO)["repos"][0]
    digest = build_digest(conn, settings, since=BEFORE, advance=False)
    installed = digest["sections"]["watched_repos"][0]["installed"]
    assert entry["status"] == "behind"
    assert entry["behind_by"] == installed["commits_behind"] == 3


def test_second_repo_status_is_served_from_cache(settings, conn, router, github):
    prepare(settings, conn, router, github)
    github.add(f"{BASE}/compare/{OLD}...{HEAD}", COMPARE)
    set_installed(conn, REPO, OLD, installed_at=PAST, settings=settings)
    compare_calls = [call for call in github.calls if "/compare/" in call]
    assert len(compare_calls) == 1

    repo_status(conn, settings, repo=REPO)
    repo_status(conn, settings, repo=REPO)
    assert len([call for call in github.calls if "/compare/" in call]) == 1
    assert conn.execute("SELECT COUNT(*) FROM compare_cache").fetchone()[0] == 1


def test_since_date_resolves_to_base_commit(settings, conn, router, github):
    prepare(settings, conn, router, github)
    github.add(f"{BASE}/commits", [{"sha": OLD}])
    github.add(f"{BASE}/compare/{OLD}...{HEAD}", COMPARE)

    status = repo_status(conn, settings, repo=REPO, since=BEFORE)
    assert status["repos"][0]["base_sha"] == OLD
    assert any(call == f"{BASE}/commits" for call in github.calls)


def test_large_compare_is_truncated(settings, conn, router, github):
    prepare(settings, conn, router, github)
    large = synthetic("github_compare_large.json")
    github.add(f"{BASE}/compare/{OLD}...{HEAD}", large)
    set_installed(conn, REPO, OLD, installed_at=PAST, settings=settings)

    entry = repo_status(conn, settings, repo=REPO)["repos"][0]
    assert entry["status"] == "behind"
    assert entry["behind_by"] == 412
    assert entry["ahead_by"] == 0
    assert entry["truncated"] is True


def test_repo_status_reports_ahead_when_the_install_has_extra_commits(
    settings, conn, router, github
):
    prepare(settings, conn, router, github)
    github.add(
        f"{BASE}/compare/{OLD}...{HEAD}",
        {"status": "behind", "ahead_by": 0, "behind_by": 2, "total_commits": 2, "commits": []},
    )
    set_installed(conn, REPO, OLD, installed_at=PAST, settings=settings)

    entry = repo_status(conn, settings, repo=REPO)["repos"][0]
    assert entry["status"] == "ahead"
    assert entry["ahead_by"] == 2
    assert entry["behind_by"] == 0


def test_repo_status_reports_diverged_both_ways(settings, conn, router, github):
    prepare(settings, conn, router, github)
    github.add(
        f"{BASE}/compare/{OLD}...{HEAD}",
        {"status": "diverged", "ahead_by": 4, "behind_by": 1, "total_commits": 5, "commits": []},
    )
    set_installed(conn, REPO, OLD, installed_at=PAST, settings=settings)

    entry = repo_status(conn, settings, repo=REPO)["repos"][0]
    assert entry["status"] == "diverged"
    assert entry["behind_by"] == 4
    assert entry["ahead_by"] == 1
    assert entry["commits_total"] == 5


def test_list_installs_and_clear(settings, conn, router, github):
    prepare(settings, conn, router, github)
    github.add(f"{BASE}/compare/{OLD}...{HEAD}", COMPARE)
    set_installed(conn, REPO, OLD, installed_at=PAST, label="spark-pair", settings=settings)

    installs = list_installs(conn, settings)
    assert installs[0]["label"] == "spark-pair"
    assert installs[0]["behind_by"] == 3
    assert installs[0]["ahead_by"] == 0

    removed = clear_installed(conn, REPO, label="spark-pair")
    assert removed["removed"] is True
    assert list_installs(conn, settings) == []


def test_list_installs_computes_the_compare_on_a_cache_miss(settings, conn, router, github):
    prepare(settings, conn, router, github)
    github.add(f"{BASE}/compare/{OLD}...{HEAD}", COMPARE)
    conn.execute(
        "INSERT INTO repo_installs (repo_id, label, sha, installed_at, updated_at)"
        " SELECT id, 'default', ?, ?, ? FROM repos WHERE url=?",
        (OLD, PAST, PAST, f"https://github.com/{REPO}"),
    )
    assert conn.execute("SELECT COUNT(*) FROM compare_cache").fetchone()[0] == 0

    installs = list_installs(conn, settings)

    assert installs[0]["behind_by"] == 3
    assert installs[0]["ahead_by"] == 0
    assert conn.execute("SELECT COUNT(*) FROM compare_cache").fetchone()[0] == 1


def test_list_installs_reports_unknown_when_the_compare_fails(settings, conn, router, github):
    prepare(settings, conn, router, github)
    github.add(f"{BASE}/compare/{OLD}...{HEAD}", {"message": "Not Found"}, status=404)
    conn.execute(
        "INSERT INTO repo_installs (repo_id, label, sha, installed_at, updated_at)"
        " SELECT id, 'default', ?, ?, ? FROM repos WHERE url=?",
        (OLD, PAST, PAST, f"https://github.com/{REPO}"),
    )

    installs = list_installs(conn, settings)

    assert installs[0]["behind_by"] == "unknown"
    assert installs[0]["ahead_by"] == "unknown"
