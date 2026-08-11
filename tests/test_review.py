"""The skills rubric's reading of a repository, and how it reaches the thread.

Three things are being pinned down here. Which articles are owed a review is a
question about routing, not a list of labels in Python. The review is written
in the same pass as the summary, so an article never goes out with half of what
the channel is going to post. And the reply is a second message *into the post's
own thread* — a card in the channel and an analysis under it, not one on top of
the other.
"""

from __future__ import annotations

from typing import Any

import pytest
from test_routing import FEED, SKILLS, FakeClient, FakeWebhook, make_settings

from squelch.core.config import Channel, Config
from squelch.core.models import SkillNote, SkillsReview, Status, Summary
from squelch.core.settings import Settings
from squelch.github.issues import IssueStore, render_body
from squelch.llm import summarize as summarize_module
from squelch.llm.summarize import run_summarize
from squelch.publishers import discord
from squelch.publishers.discord import (
    REVIEW_DISCLAIMER,
    REVIEW_TITLE,
    publish_ready,
)

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

REVIEW = {
    "verdict": "mixed",
    "promise": "The README lists twelve skills; six exist and three are one paragraph each.",
    "usefulness": "Worth it if you already write short drama; nothing here for anyone else.",
    "skills": [
        {
            "name": "novel-characters",
            "does": "Turns a novel into per-character bibles",
            "verdict": "real",
        },
        {
            "name": "model-sheets",
            "does": "Tells the agent to call an image API",
            "verdict": "thin",
        },
        {
            "name": "outline",
            "does": "Describes a format but never says what fills it",
            "verdict": "unclear",
        },
    ],
}


def rubric_config(**overrides: Any) -> Config:
    skills: dict[str, Any] = {
        "id": "discord-skills",
        "only": ["topic:claude-skills"],
        "forum": True,
        "review": True,
    }
    skills.update(overrides)
    return Config(
        focus="f",
        channels=[
            Channel(id="discord", skip=["topic:claude-skills"]),
            Channel(**skills),
        ],
    )


def make_issue(
    number: int,
    *,
    labels: list[str],
    review: dict[str, Any] | None = None,
    facts: dict[str, int] | None = None,
) -> dict[str, Any]:
    meta: dict[str, Any] = {"uid": f"uid{number}", "score": 5}
    if review is not None:
        meta["review"] = review
    if facts is not None:
        meta["facts"] = facts
    return {
        "number": number,
        "title": f"acme/skills-{number}",
        "labels": [{"name": name} for name in labels],
        "state": "open",
        "body": render_body(meta, "Text.", "A summary."),
    }


def skills_issue(
    number: int = 2,
    review: dict[str, Any] | None = REVIEW,
    facts: dict[str, int] | None = None,
) -> dict[str, Any]:
    return make_issue(
        number,
        labels=[Status.READY.value, "topic:claude-skills"],
        review=review,
        facts=facts,
    )


@pytest.fixture(autouse=True)
def fake_webhook(monkeypatch: pytest.MonkeyPatch) -> type[FakeWebhook]:
    FakeWebhook.sent = []
    FakeWebhook.threads = []
    monkeypatch.setattr(discord, "_Webhook", FakeWebhook)
    return FakeWebhook


def to_skills() -> list[dict[str, Any]]:
    return [payload for url, payload in FakeWebhook.sent if url == SKILLS]


# -- who is owed a review ----------------------------------------------------


def test_an_article_is_owed_a_review_because_a_channel_publishes_them() -> None:
    config = rubric_config()

    assert config.wants_review({"topic:claude-skills"})
    assert not config.wants_review({"topic:models"})


def test_switching_the_channel_off_stops_the_extra_call_with_it() -> None:
    # The LLM call is the expensive half of this feature; a disabled channel
    # that still paid for its analysis would be the worst of both.
    assert not rubric_config(enabled=False).wants_review({"topic:claude-skills"})


def test_a_text_channel_cannot_ask_for_reviews() -> None:
    # A reply needs a thread to land in, and the stage would run and be paid
    # for regardless of whether anywhere could show the result.
    with pytest.raises(ValueError, match="review"):
        Channel(id="discord-skills", review=True)


# -- writing it --------------------------------------------------------------


class FakeStore:
    """Enough of IssueStore for stage two, remembering what it was handed."""

    def __init__(self, issues: list[Any]) -> None:
        self._issues = issues
        self.applied: list[tuple[int, Summary, SkillsReview | None]] = []

    def list_by_status(self, status: Status, limit: int | None = None) -> list[Any]:
        return self._issues

    def apply_summary(
        self, issue: Any, summary: Summary, review: SkillsReview | None = None
    ) -> None:
        self.applied.append((issue.number, summary, review))


def stub_gemini(monkeypatch: pytest.MonkeyPatch, review: SkillsReview | None) -> None:
    class FakeGemini:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def structured(self, prompt: str, schema: type, system: str = "") -> Any:
            if schema is SkillsReview:
                return review
            return Summary(summary="A summary.")

    monkeypatch.setattr(summarize_module, "GeminiClient", FakeGemini)


def relevant(number: int, *labels: str) -> Any:
    from squelch.github.issues import parse_issue

    return parse_issue(make_issue(number, labels=[Status.RELEVANT.value, *labels]))


def settings() -> Settings:
    return Settings(_env_file=None, gemini_api_key="k", llm_delay_seconds=0)  # type: ignore[call-arg]


def test_a_skills_article_is_written_up_and_reviewed_in_one_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    written = SkillsReview(
        verdict="hype",
        promise="Four files and a banner.",
        usefulness="Nobody.",
        skills=[SkillNote(name="a", does="nothing", verdict="thin")],
    )
    stub_gemini(monkeypatch, written)
    store = FakeStore([relevant(2, "topic:claude-skills")])

    assert run_summarize(settings(), rubric_config(), store) == 1  # type: ignore[arg-type]
    assert store.applied[0][2] == written


def test_an_ordinary_article_is_not_reviewed(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_gemini(monkeypatch, None)
    store = FakeStore([relevant(1)])

    assert run_summarize(settings(), rubric_config(), store) == 1  # type: ignore[arg-type]
    assert store.applied[0][2] is None


def test_an_article_whose_review_failed_waits_rather_than_going_out_bare(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The analysis is the whole content of this channel, so a post without one
    # is worse than a post a tick later. Stays relevant and comes back around.
    stub_gemini(monkeypatch, None)
    store = FakeStore([relevant(2, "topic:claude-skills")])

    assert run_summarize(settings(), rubric_config(), store) == 0  # type: ignore[arg-type]
    assert store.applied == []


def test_the_review_is_stored_on_the_issue_where_the_publisher_finds_it() -> None:
    from squelch.github.issues import parse_issue

    client = FakeClient([make_issue(2, labels=[Status.RELEVANT.value, "topic:claude-skills"])])
    store = IssueStore(client)
    issue = parse_issue(client.issues[2])

    store.apply_summary(
        issue,
        Summary(summary="A summary."),
        SkillsReview(verdict="substance", promise="p", usefulness="u", skills=[]),
    )

    assert parse_issue(client.issues[2]).review["verdict"] == "substance"


# -- posting it --------------------------------------------------------------


def test_the_review_arrives_as_a_reply_inside_the_article_s_own_thread() -> None:
    store = IssueStore(FakeClient([skills_issue()]))

    publish_ready(make_settings(), rubric_config(), store)

    posts = to_skills()
    assert len(posts) == 2
    # The post opens the thread; the review joins the one it just opened.
    assert posts[0]["thread_name"] == "acme/skills-2"
    assert "thread_name" not in posts[1]
    assert FakeWebhook.threads == ["", "thread-1"]
    assert posts[1]["embeds"][0]["title"] == REVIEW_TITLE


def test_the_reply_walks_the_skills_that_actually_exist() -> None:
    store = IssueStore(FakeClient([skills_issue()]))

    publish_ready(make_settings(), rubric_config(), store)

    text = to_skills()[1]["embeds"][0]["description"]
    assert "**novel-characters** — Turns a novel into per-character bibles" in text
    # The weak ones are marked; a real skill says nothing about itself, or the
    # list would read as scored homework rather than an inventory.
    assert "**model-sheets** — *thin* —" in text
    assert "**outline** — *unclear* —" in text
    assert "*thin*" not in text.split("**model-sheets**")[0]


def test_the_reply_leads_with_the_verdict_and_ends_with_who_it_helps() -> None:
    store = IssueStore(FakeClient([skills_issue()]))

    publish_ready(make_settings(), rubric_config(), store)

    embed = to_skills()[1]["embeds"][0]
    assert embed["description"].startswith("**Mixed** — The README lists twelve skills")
    assert embed["description"].rstrip().endswith("nothing here for anyone else.")


def test_the_verdict_is_read_next_to_the_numbers_it_is_weighed_against() -> None:
    # The whole argument of the rubric in one line: a lot of stars, nothing
    # behind them. It is also what tells a reader whether to clone and look.
    store = IssueStore(
        FakeClient([skills_issue(review={"verdict": "hype", "promise": "A banner."},
                                 facts={"stars": 822, "skills": 0, "forks": 15})])
    )

    publish_ready(make_settings(), rubric_config(), store)

    assert to_skills()[1]["embeds"][0]["description"].startswith("**Hype** · 822 ★ · 0 skills — ")


def test_a_single_skill_is_not_called_one_skills() -> None:
    store = IssueStore(FakeClient([skills_issue(facts={"stars": 1200, "skills": 1})]))

    publish_ready(make_settings(), rubric_config(), store)

    assert "1 200 ★ · 1 skill —" in to_skills()[1]["embeds"][0]["description"]


def test_an_article_scraped_before_the_numbers_existed_still_reads_fine() -> None:
    # Every issue already in the queue has no facts block, and the rubric must
    # not start emitting "0 ★" over them.
    store = IssueStore(FakeClient([skills_issue()]))

    publish_ready(make_settings(), rubric_config(), store)

    text = to_skills()[1]["embeds"][0]["description"]
    assert text.startswith("**Mixed** — ")
    assert "★" not in text


def test_a_tree_that_could_not_be_read_shows_stars_and_says_nothing_about_skills() -> None:
    store = IssueStore(FakeClient([skills_issue(facts={"stars": 500, "forks": 3})]))

    publish_ready(make_settings(), rubric_config(), store)

    assert "**Mixed** · 500 ★ — " in to_skills()[1]["embeds"][0]["description"]


def test_the_reply_never_claims_more_than_reading_the_files() -> None:
    # Nothing from a repository is ever executed, and a verdict that sounded
    # like it had been would be a lie in the one place it matters.
    store = IssueStore(FakeClient([skills_issue()]))

    publish_ready(make_settings(), rubric_config(), store)

    assert to_skills()[1]["embeds"][0]["footer"]["text"] == REVIEW_DISCLAIMER


def test_an_article_with_no_review_is_simply_posted_on_its_own() -> None:
    # An issue opened by hand carries no metadata block at all.
    store = IssueStore(FakeClient([skills_issue(review=None)]))

    assert publish_ready(make_settings(), rubric_config(), store) == 1
    assert len(to_skills()) == 1


def test_a_channel_that_does_not_publish_reviews_never_sends_one() -> None:
    # An issue can carry a review and still be routed to the feed — by a hand
    # edit, or by a label changing after the rubric wrote one. The flag on the
    # channel decides, not the presence of the analysis.
    store = IssueStore(FakeClient([make_issue(1, labels=[Status.READY.value], review=REVIEW)]))

    publish_ready(make_settings(), rubric_config(), store)

    assert [url for url, _ in FakeWebhook.sent] == [FEED]
    assert FakeWebhook.threads == [""]


def test_a_review_the_model_declined_to_write_costs_the_reply_not_the_post() -> None:
    store = IssueStore(FakeClient([skills_issue(review={"verdict": "", "skills": []})]))

    assert publish_ready(make_settings(), rubric_config(), store) == 1
    assert len(to_skills()) == 1


def test_the_delivery_record_remembers_both_messages_and_the_thread() -> None:
    client = FakeClient([skills_issue()])
    store = IssueStore(client)

    publish_ready(make_settings(), rubric_config(), store)

    from squelch.github.issues import parse_issue

    record = parse_issue(client.issues[2]).delivery("discord-skills")
    assert record["message_id"] == "msg-1"
    assert record["thread_id"] == "thread-1"
    assert record["review_message_id"] == "msg-2"


def test_a_post_that_lost_its_reply_gets_one_more_chance_before_it_leaves_the_queue() -> None:
    # The window this covers: the post went out, the reply did not, and the run
    # died before the label landed. Reposting the article would double it up;
    # dropping the analysis silently would empty the rubric of its point.
    issue = skills_issue()
    issue["body"] = render_body(
        {
            "uid": "uid2",
            "score": 5,
            "review": REVIEW,
            "delivery": {"discord-skills": {"at": "2026-08-01T00:00:00", "message_id": "msg-0"}},
        },
        "Text.",
        "A summary.",
    )
    client = FakeClient([issue])
    store = IssueStore(client)

    publish_ready(make_settings(), rubric_config(), store)

    posts = to_skills()
    assert len(posts) == 1
    assert posts[0]["embeds"][0]["title"] == REVIEW_TITLE
    # Posted into the thread the earlier run opened, not into a new one.
    assert FakeWebhook.threads == ["msg-0"]

    from squelch.github.issues import parse_issue

    record = parse_issue(client.issues[2]).delivery("discord-skills")
    assert record["message_id"] == "msg-0"
    assert record["review_message_id"] == "msg-1"
    assert record["at"] != "2026-08-01T00:00:00"


def test_a_post_that_already_has_its_reply_is_not_replied_to_twice() -> None:
    issue = skills_issue()
    issue["body"] = render_body(
        {
            "uid": "uid2",
            "review": REVIEW,
            "delivery": {
                "discord-skills": {"message_id": "msg-0", "review_message_id": "msg-x"}
            },
        },
        "Text.",
        "A summary.",
    )
    store = IssueStore(FakeClient([issue]))

    publish_ready(make_settings(), rubric_config(), store)

    assert FakeWebhook.sent == []
