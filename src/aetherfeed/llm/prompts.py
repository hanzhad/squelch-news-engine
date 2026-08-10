"""Prompt text, kept apart from the code that runs it.

What this feed publishes is tuned by rewriting sentences, not logic, so the
wording lives in one file that can be read end to end. Two rules shape
everything here: ``config.focus`` is quoted verbatim, because it is the
editorial policy and paraphrasing it silently changes the feed; and the JSON
shape is never described, because the schema already travels with the request
and repeating it in prose degrades the answer.
"""

from __future__ import annotations

from collections.abc import Sequence

from ..core.config import Config
from ..github.issues import IssueRecord

# Enough of each article for the model to see the theme, short enough that forty
# of them still make a small request.
DIGEST_SUMMARY_CHARS = 400

FILTER_SYSTEM = (
    "You are the editor of a technical news feed. You judge one article at a "
    "time against the feed's policy, using only what the article itself says. "
    "You are strict: a slot in the feed is expensive, an omission is cheap."
)

DIGEST_SYSTEM = (
    "You are writing the weekly roundup for a technical news feed. You work "
    "only from the articles you are given, you never invent a fact, a title or "
    "a link, and you write plainly for engineers who read the feed already."
)


def filter_prompt(config: Config, issue: IssueRecord) -> str:
    """The judgement call on a single article."""
    body = issue.text.strip()[: config.max_body_chars]
    topics = ", ".join(config.topics) or "(none)"

    return "\n".join(
        [
            "EDITORIAL POLICY",
            "",
            config.focus.strip(),
            "",
            "ALLOWED TOPICS",
            "",
            topics,
            "",
            "RULES",
            "",
            "- Judge only the article below. Do not fill gaps from your own knowledge.",
            "- Default to not relevant. Say relevant only when the article clearly matches",
            "  the policy and carries facts a practitioner could act on.",
            "- Thin, vague, promotional or unverifiable items are not relevant, however",
            "  interesting the headline sounds. So is anything you cannot check from the",
            "  text in front of you.",
            "- The text may be truncated; judge what is present. A headline with no body",
            "  and nothing concrete in it is not relevant.",
            "- Tag only from the allowed topics above, at most three, and only ones the",
            "  article is genuinely about. No tags when it is not relevant.",
            "- Score how much this changes a working engineer's day: 0-3 routine, 4-6 worth",
            "  knowing, 7-10 changes decisions.",
            "- Summarise in plain English with no marketing adjectives, and only when the",
            "  article is relevant.",
            "- Give the reason in one sentence, naming the part of the policy that decided",
            "  it.",
            "",
            "ARTICLE",
            "",
            f"Title: {issue.title}",
            f"Source: {issue.source or 'unknown'}",
            f"URL: {issue.url or 'unknown'}",
            "",
            body or "(no article text was captured)",
        ]
    )


def digest_prompt(config: Config, days: int, issues: Sequence[IssueRecord]) -> str:
    """One roundup over everything the feed published in the window."""
    entries: list[str] = []
    for issue in issues:
        gist = " ".join((issue.summary or issue.text).split())[:DIGEST_SUMMARY_CHARS]
        entries += [
            f"- Title: {issue.title}",
            f"  URL: {issue.url or issue.html_url}",
            f"  Summary: {gist or '(none)'}",
        ]

    return "\n".join(
        [
            "EDITORIAL POLICY",
            "",
            config.focus.strip(),
            "",
            "TASK",
            "",
            f"These {len(issues)} articles were published by the feed in the last "
            f"{days} days. Write the roundup.",
            "",
            "RULES",
            "",
            "- The headline is one sentence naming what this week was actually about.",
            "  No questions, no colons-and-a-slogan, no hype.",
            "- A trend must be visible in more than one article. If only one thing",
            "  happened, say so in fewer trends rather than padding the list.",
            "- Highlight the articles that matter most, at most eight of them.",
            "- Copy each title and URL exactly as given below. Never write a URL that",
            "  does not appear in the list.",
            "- Each takeaway is one sentence on why that article matters, not a restated",
            "  headline.",
            "",
            "ARTICLES",
            "",
            *entries,
        ]
    )
