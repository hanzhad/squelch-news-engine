"""GitHub search as a source: repositories in, articles out, nothing executed."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from squelch.core.config import Config
from squelch.scrapers import github_repos

SEARCH_URL = "https://github.com/search?q=claude+skills+in%3Aname%2Cdescription&type=repositories"


def repo_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "full_name": "acme/claude-skills",
        "html_url": "https://github.com/acme/claude-skills",
        "description": "A curated pile of skills",
        "stargazers_count": 420,
        "forks_count": 7,
        "created_at": "2026-08-01T12:00:00Z",
        "pushed_at": "2026-08-09T08:00:00Z",
        "topics": ["claude", "skills"],
        "language": "Shell",
        "default_branch": "main",
        "owner": {"avatar_url": "https://avatars.githubusercontent.com/u/1"},
    }
    payload.update(overrides)
    return payload


TREE = {
    "tree": [
        {"path": "README.md", "type": "blob"},
        {"path": "skills/review/SKILL.md", "type": "blob"},
        {"path": "skills/review", "type": "tree"},
        {"path": "install.sh", "type": "blob"},
        {"path": "hooks/pre-commit.sh", "type": "blob"},
    ]
}
README = "# Claude skills\n\nReal content, promise."
SKILL = "---\nname: review\ndescription: reviews the diff against a checklist\n---"


class FakeResponse:
    def __init__(self, payload: Any = None, text: str = "") -> None:
        self._payload = payload
        self.text = text

    def json(self) -> Any:
        return self._payload

    def raise_for_status(self) -> None:
        return None


class FakeClient:
    """Routes API paths to canned responses; anything unrouted is a 404."""

    def __init__(self, routes: dict[str, FakeResponse]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get(self, url: str, params: Any = None, headers: Any = None) -> FakeResponse:
        self.calls.append((url, dict(params or {})))
        for fragment, response in self.routes.items():
            if fragment in url:
                return response
        raise httpx.HTTPError(f"404 {url}")


def full_routes(items: list[dict[str, Any]] | None = None) -> dict[str, FakeResponse]:
    return {
        "/search/repositories": FakeResponse({"items": items or [repo_payload()]}),
        "/git/trees/main": FakeResponse(TREE),
        "/readme": FakeResponse(text=README),
        "/contents/skills/review/SKILL.md": FakeResponse(text=SKILL),
    }


@pytest.fixture
def source(make_source):  # type: ignore[no-untyped-def]
    return make_source("claude-skills", url=SEARCH_URL, type="github", max_items=5)


def test_the_query_comes_from_the_url_and_is_bounded_to_the_news_window(
    source, config: Config
) -> None:  # type: ignore[no-untyped-def]
    client = FakeClient(full_routes())

    github_repos.scrape(source, config, client)

    search_params = client.calls[0][1]
    assert search_params["q"].startswith("claude skills in:name,description")
    assert " created:>=" in search_params["q"]
    assert search_params["sort"] == "stars"
    assert search_params["per_page"] == 5


def test_a_repository_becomes_an_article_of_its_own_words_and_hard_facts(
    source, config: Config
) -> None:  # type: ignore[no-untyped-def]
    articles = github_repos.scrape(source, config, FakeClient(full_routes()))

    assert len(articles) == 1
    article = articles[0]
    assert article.title == "acme/claude-skills: A curated pile of skills"
    assert article.url == "https://github.com/acme/claude-skills"
    assert article.source == "claude-skills"
    assert article.published_at == datetime(2026, 8, 1, 12, tzinfo=UTC)
    assert article.image == "https://avatars.githubusercontent.com/u/1"
    # Facts the LLM cannot invent lead the body.
    assert "Stars: 420" in article.body
    assert "1 SKILL.md" in article.body
    assert "2 shell scripts" in article.body
    assert "includes hooks" in article.body
    # Then the repository's own words: README and the skills' descriptions.
    assert "Real content, promise." in article.body
    assert "reviews the diff against a checklist" in article.body


def test_a_missing_readme_or_tree_costs_detail_never_the_article(
    source, config: Config
) -> None:  # type: ignore[no-untyped-def]
    routes = {"/search/repositories": FakeResponse({"items": [repo_payload()]})}

    articles = github_repos.scrape(source, config, FakeClient(routes))

    assert len(articles) == 1
    assert "Stars: 420" in articles[0].body
    assert "## README" not in articles[0].body


def test_a_url_without_a_query_is_an_empty_run_not_a_crash(
    make_source, config: Config
) -> None:  # type: ignore[no-untyped-def]
    source = make_source("claude-skills", url="https://github.com/search", type="github")

    assert github_repos.scrape(source, config, FakeClient(full_routes())) == []


def test_a_failed_search_yields_nothing(source, config: Config) -> None:  # type: ignore[no-untyped-def]
    assert github_repos.scrape(source, config, FakeClient({})) == []


def test_a_repo_without_a_link_is_skipped(source, config: Config) -> None:  # type: ignore[no-untyped-def]
    items = [repo_payload(html_url=""), repo_payload()]
    routes = full_routes(items)

    articles = github_repos.scrape(source, config, FakeClient(routes))

    assert [a.url for a in articles] == ["https://github.com/acme/claude-skills"]


# -- titles ------------------------------------------------------------------
#
# A repository description is not written to be a headline, and this title
# also names the Discord forum post, which cuts at 100 characters.


def headline(description: str, full_name: str = "acme/claude-skills") -> str:
    return github_repos._headline(full_name, description)


def test_a_title_never_outgrows_what_a_forum_post_can_be_called() -> None:
    assert len(headline("Ship faster " * 40)) <= github_repos.TITLE_LIMIT


def test_only_the_first_sentence_of_a_description_is_the_headline() -> None:
    assert (
        headline("Files your tax return from the terminal. Both regimes, AY 2026-27.")
        == "acme/claude-skills: Files your tax return from the terminal"
    )


def test_the_same_description_repeated_in_another_language_is_cut_at_the_bar() -> None:
    # Half of these repositories describe themselves twice, either side of a
    # bar; the second half is the same claim again, not more of it.
    assert (
        headline("Skills for short-drama production | AI 短剧制作的 skill 集合")
        == "acme/claude-skills: Skills for short-drama production"
    )


def test_a_full_stop_inside_a_word_does_not_end_the_sentence() -> None:
    assert headline("Growth hacking skills by enso.bot for v1.2").endswith("enso.bot for v1.2")


def test_a_first_sentence_past_the_limit_is_cut_at_a_word_and_says_so() -> None:
    title = headline(
        "Turn a photo of your handwriting into a real font (TTF/WOFF/WOFF2), "
        "free and open source, no uploads anywhere"
    )

    assert len(title) <= github_repos.TITLE_LIMIT
    assert title.endswith("…")
    assert " ".join(title.split()) == title


def test_a_repository_with_no_description_is_titled_by_its_name_alone() -> None:
    assert headline("") == "acme/claude-skills"


def test_a_name_that_leaves_no_room_goes_out_on_its_own() -> None:
    # A three-word fragment of a description reads worse than nothing.
    long_name = "a" * 80 + "/skills"

    assert headline("A curated pile of skills for agents", long_name) == long_name
