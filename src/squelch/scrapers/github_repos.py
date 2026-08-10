"""GitHub repositories as a news source.

Skill collections for coding agents are announced nowhere and hyped
everywhere; the only reliable signal is the repository itself appearing and
gathering stars. So the source is a GitHub search — repositories matching the
query, created within the feed's news window, most-starred first — and the
article text is what the repository says about itself: the README, a few
SKILL.md files (which are natural-language descriptions by design) and hard
numbers the LLM cannot invent. Nothing from the repository is ever executed;
this pipeline reads, it does not audit.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import httpx

from ..core.config import Config, Source
from ..core.log import get_logger
from ..core.models import RawArticle
from ..core.settings import get_settings

log = get_logger(__name__)

API_ROOT = "https://api.github.com"
# How many SKILL.md files are quoted in the article body, and how much of
# each. Enough for the LLM to see what the skills actually claim to do; the
# repository itself is one click away for everything else.
MAX_SKILL_FILES = 3
SKILL_EXCERPT_CHARS = 1500
SCRIPT_SUFFIXES = (".sh", ".bash", ".ps1")


def _headers(accept: str = "application/vnd.github+json") -> dict[str, str]:
    # The pipeline's token when there is one: unauthenticated callers get 60
    # requests an hour, which two scrape runs with a handful of repos exhaust.
    # A dry run without a token still works, just against the small budget.
    headers = {"Accept": accept, "X-GitHub-Api-Version": "2022-11-28"}
    token = get_settings().github_token
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _query(source: Source) -> str:
    """The ``q=`` of the configured search page — that is what the API runs."""
    return dict(parse_qsl(urlsplit(source.url).query)).get("q", "").strip()


def _get(client: httpx.Client, url: str, **kwargs: Any) -> httpx.Response | None:
    """One request that degrades to None: a missing README or an unreadable
    tree must cost detail, never the article — and never the batch."""
    try:
        response = client.get(url, **kwargs)
        response.raise_for_status()
        return response
    except httpx.HTTPError as exc:
        log.debug("github: %s -> %s", url, exc)
        return None


def _parse_date(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _tree_paths(client: httpx.Client, full_name: str, branch: str) -> list[str]:
    response = _get(
        client,
        f"{API_ROOT}/repos/{full_name}/git/trees/{branch}",
        params={"recursive": "1"},
        headers=_headers(),
    )
    if response is None:
        return []
    payload = response.json()
    if not isinstance(payload, dict):
        return []
    return [
        str(node.get("path", ""))
        for node in payload.get("tree", [])
        if isinstance(node, dict) and node.get("type") == "blob"
    ]


def _readme(client: httpx.Client, full_name: str) -> str:
    response = _get(
        client,
        f"{API_ROOT}/repos/{full_name}/readme",
        headers=_headers("application/vnd.github.raw+json"),
    )
    return response.text if response is not None else ""


def _skill_excerpts(
    client: httpx.Client, full_name: str, skill_paths: list[str]
) -> list[tuple[str, str]]:
    excerpts: list[tuple[str, str]] = []
    for path in skill_paths[:MAX_SKILL_FILES]:
        response = _get(
            client,
            f"{API_ROOT}/repos/{full_name}/contents/{path}",
            headers=_headers("application/vnd.github.raw+json"),
        )
        if response is not None and response.text.strip():
            excerpts.append((path, response.text.strip()[:SKILL_EXCERPT_CHARS]))
    return excerpts


def _facts_line(repo: dict[str, Any], created: datetime | None) -> str:
    parts = [f"Stars: {repo.get('stargazers_count', 0)}", f"Forks: {repo.get('forks_count', 0)}"]
    if created:
        parts.append(f"Created: {created:%Y-%m-%d}")
    pushed = _parse_date(repo.get("pushed_at"))
    if pushed:
        parts.append(f"Last push: {pushed:%Y-%m-%d}")
    if repo.get("language"):
        parts.append(f"Language: {repo['language']}")
    return " · ".join(parts)


def _body(client: httpx.Client, repo: dict[str, Any], created: datetime | None) -> str:
    """What the repository says about itself, hard facts first.

    The numbers and the file counts lead on purpose: they are the part the
    LLM cannot invent, and the part a hype repository cannot fake. Everything
    after them is the repository's own words, clearly unverified.
    """
    full_name = str(repo.get("full_name", ""))
    branch = str(repo.get("default_branch") or "main")

    paths = _tree_paths(client, full_name, branch)
    skill_paths = [p for p in paths if p.lower().split("/")[-1] == "skill.md"]
    scripts = sum(1 for p in paths if p.lower().endswith(SCRIPT_SUFFIXES))
    hooks = any("hooks" in p.lower().split("/") for p in paths)

    lines = [str(repo.get("description") or "").strip(), "", _facts_line(repo, created)]
    topics = [str(t) for t in repo.get("topics") or []]
    if topics:
        lines.append(f"Topics: {', '.join(topics)}")
    if paths:
        contents = (
            f"Contents: {len(paths)} files, {len(skill_paths)} SKILL.md, {scripts} shell scripts"
        )
        if hooks:
            contents += ", includes hooks"
        lines.append(contents)

    readme = _readme(client, full_name)
    if readme:
        lines += ["", "## README", "", readme]
    for path, text in _skill_excerpts(client, full_name, skill_paths):
        lines += ["", f"## {path}", "", text]
    return "\n".join(lines).strip()


def scrape(source: Source, config: Config, client: httpx.Client) -> list[RawArticle]:
    query = _query(source)
    if not query:
        log.error("source %s: url carries no q= search query", source.id)
        return []

    # Bounded to the feed's news window, most-starred first: what surfaces is
    # "the new repositories people actually flocked to", which is the closest
    # a scraper gets to the hype happening off-platform. A repository older
    # than the window that explodes later is deliberately out of scope.
    cutoff = (datetime.now(UTC) - timedelta(days=config.max_age_days)).date().isoformat()
    response = _get(
        client,
        f"{API_ROOT}/search/repositories",
        params={
            "q": f"{query} created:>={cutoff}",
            "sort": "stars",
            "order": "desc",
            "per_page": source.max_items,
        },
        headers=_headers(),
    )
    if response is None:
        log.error("source %s: search failed", source.id)
        return []
    items = response.json().get("items") or []

    articles: list[RawArticle] = []
    for repo in items[: source.max_items]:
        if not isinstance(repo, dict) or not repo.get("html_url") or not repo.get("full_name"):
            continue
        created = _parse_date(repo.get("created_at"))
        description = str(repo.get("description") or "").strip()
        title = repo["full_name"] + (f": {description}" if description else "")
        try:
            articles.append(
                RawArticle(
                    title=title,
                    url=str(repo["html_url"]),
                    source=source.id,
                    published_at=created,
                    body=_body(client, repo, created)[: config.max_body_chars],
                    # The owner's avatar is the closest thing a repository has
                    # to a cover picture.
                    image=str((repo.get("owner") or {}).get("avatar_url") or ""),
                )
            )
        except ValueError as exc:
            log.warning("skipping malformed repo from %s: %s", source.id, exc)

    log.info("source %s yielded %d repositories", source.id, len(articles))
    return articles
