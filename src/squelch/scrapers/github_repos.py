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

import re
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import httpx
import yaml

from ..core.config import Config, Source
from ..core.log import get_logger
from ..core.models import RawArticle
from ..core.settings import get_settings
from ..core.text import trim

log = get_logger(__name__)

API_ROOT = "https://api.github.com"
# How many SKILL.md files are read at all. Every one of them costs a request,
# and the point of the ceiling is the API budget rather than the reading: a
# collection with more skills than this is described by its first twenty just
# as well as by all of them, and the count on the Contents line still tells the
# truth about how many there are.
MAX_SKILL_FILES = 20
# How many of those are then quoted at length. The rest contribute their
# frontmatter — the name and the one-line description a skill is required to
# declare — which is what makes an inventory of the whole collection affordable.
MAX_SKILL_EXCERPTS = 3
SKILL_EXCERPT_CHARS = 1500
# One inventory line per skill, so twenty of them stay a list rather than
# becoming the article.
SKILL_SUMMARY_CHARS = 140
SCRIPT_SUFFIXES = (".sh", ".bash", ".ps1")
FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*(?:\n|$)", re.DOTALL)
# A repository description is written to fit under a repository name, not to
# be a headline, and this is the one source whose titles we compose ourselves.
# So they are cut here, once, to the shortest limit they will meet downstream —
# the name of a Discord forum post. Nothing is lost: the full description is
# the first line of the article body.
TITLE_LIMIT = 100
# Under this there is no room for a description that still says anything, and
# a three-word fragment reads worse than the repository name on its own.
MIN_DESCRIPTION_CHARS = 24
# Where a description stops being a headline: the end of its first sentence,
# or the bar these repositories use to restate themselves in another language.
# A full stop only counts when whitespace follows, so "enso.bot" and "v1.2"
# survive intact.
FIRST_CLAUSE = re.compile(r"^(.*?)(?:[.!?](?=\s|$)|[。！？]|\s*[|｜])")


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


def _skill_files(
    client: httpx.Client, full_name: str, skill_paths: list[str]
) -> list[tuple[str, str]]:
    """Read the SKILL.md files themselves — once, for both the list and the quotes."""
    files: list[tuple[str, str]] = []
    for path in skill_paths[:MAX_SKILL_FILES]:
        response = _get(
            client,
            f"{API_ROOT}/repos/{full_name}/contents/{path}",
            headers=_headers("application/vnd.github.raw+json"),
        )
        if response is not None and response.text.strip():
            files.append((path, response.text.strip()))
    if len(skill_paths) > MAX_SKILL_FILES:
        log.info(
            "%s: read %d of %d SKILL.md files", full_name, MAX_SKILL_FILES, len(skill_paths)
        )
    return files


def _frontmatter(text: str) -> dict[str, Any]:
    """The YAML header a skill declares itself with, or nothing.

    A skill's name and one-line description live there by convention, which is
    the only part of a collection that is cheap to read in full. Anything
    unparseable is simply not frontmatter: the file still contributes its path.
    """
    match = FRONTMATTER_RE.match(text)
    if not match:
        return {}
    try:
        parsed = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _skill_line(path: str, text: str) -> str:
    """One inventory line: what this skill calls itself and what it claims to do."""
    header = _frontmatter(text)
    name = str(header.get("name") or "").strip()
    if not name:
        # skills/<name>/SKILL.md is the layout everything uses, so the directory
        # is the name whenever the file does not declare one.
        parts = path.split("/")
        name = parts[-2] if len(parts) > 1 else path

    described = " ".join(str(header.get("description") or "").split())
    if not described:
        # No declared description. The first prose line says more than the path
        # does, and a skill that declares nothing is itself worth seeing.
        match = FRONTMATTER_RE.match(text)
        body = text[match.end() :] if match else text
        described = next(
            (line.strip("# ").strip() for line in body.splitlines() if line.strip()),
            "no description declared",
        )
    return f"- {name} ({path}): {trim(described, SKILL_SUMMARY_CHARS)}"


def _headline(full_name: str, description: str) -> str:
    """``owner/repo`` plus as much of the description as reads like a title."""
    clause = " ".join(description.split())
    first = FIRST_CLAUSE.match(clause)
    if first:
        clause = first.group(1).strip()
    room = TITLE_LIMIT - len(full_name) - len(": ")
    if not clause or room < MIN_DESCRIPTION_CHARS:
        return full_name
    return f"{full_name}: {trim(clause, room)}"


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


def _body(
    client: httpx.Client, repo: dict[str, Any], created: datetime | None
) -> tuple[str, dict[str, int]]:
    """What the repository says about itself, hard facts first.

    Returns the article text and, separately, the numbers behind it. They are
    the same figures, but the ones handed back here never pass through a model
    on their way to a reader — a star count published under a verdict is what
    that verdict is weighed against, and it has to be counted rather than
    recalled.

    The numbers and the file counts lead on purpose: they are the part the
    LLM cannot invent, and the part a hype repository cannot fake. Then the
    inventory of skills that actually exist — ahead of the README, because that
    is the order the question is asked in ("what is in here" before "what does
    it claim"), and because everything downstream reads a prefix of this text:
    the classifier sees the first 1500 characters, the summariser the first
    ``max_body_chars``. What survives both cuts should be the contents rather
    than the banner at the top of a README.
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

    skill_files = _skill_files(client, full_name, skill_paths)
    if skill_files:
        lines += ["", "## Skills present", ""]
        lines += [_skill_line(path, text) for path, text in skill_files]
        if len(skill_paths) > len(skill_files):
            lines.append(f"- (and {len(skill_paths) - len(skill_files)} more, not read)")

    readme = _readme(client, full_name)
    if readme:
        lines += ["", "## README", "", readme]
    for path, text in skill_files[:MAX_SKILL_EXCERPTS]:
        lines += ["", f"## {path}", "", text[:SKILL_EXCERPT_CHARS]]

    facts = {
        "stars": int(repo.get("stargazers_count") or 0),
        "forks": int(repo.get("forks_count") or 0),
    }
    if paths:
        # Only when the tree was actually readable: a zero here would otherwise
        # read as "this repository ships no skills", which is a finding, not a
        # failed request.
        facts["files"] = len(paths)
        facts["skills"] = len(skill_paths)
    return "\n".join(lines).strip(), facts


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
        body, facts = _body(client, repo, created)
        try:
            articles.append(
                RawArticle(
                    title=_headline(
                        str(repo["full_name"]), str(repo.get("description") or "")
                    ),
                    url=str(repo["html_url"]),
                    source=source.id,
                    published_at=created,
                    body=body[: config.max_body_chars],
                    # The owner's avatar is the closest thing a repository has
                    # to a cover picture.
                    image=str((repo.get("owner") or {}).get("avatar_url") or ""),
                    facts=facts,
                )
            )
        except ValueError as exc:
            log.warning("skipping malformed repo from %s: %s", source.id, exc)

    log.info("source %s yielded %d repositories", source.id, len(articles))
    return articles
