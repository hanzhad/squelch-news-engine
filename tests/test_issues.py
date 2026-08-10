"""The issue body is the storage format, so render -> parse must be lossless."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from squelch.core.models import Status, Verdict
from squelch.github.issues import (
    BODY_LIMIT,
    ORIGINAL_CLOSE,
    IssueRecord,
    IssueStore,
    parse_body,
    parse_issue,
    render_body,
)


def make_meta(**overrides: Any) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "uid": "0123456789abcdef",
        "url": "https://example.com/2025/01/story",
        "source": "arstechnica",
        "published_at": "2025-01-06T09:30:00+00:00",
        "scraped_by": "squelch",
    }
    meta.update(overrides)
    return meta


# -- round trip -------------------------------------------------------------


def test_render_parse_round_trip_preserves_meta_summary_and_original() -> None:
    meta = make_meta()
    original = "First paragraph.\n\nSecond paragraph with detail."
    summary = "A short summary.\nStill the summary."

    meta_out, summary_out, original_out = parse_body(render_body(meta, original, summary))

    assert meta_out == meta
    assert summary_out == summary
    assert original_out == original


def test_round_trip_without_a_summary_yields_an_empty_summary() -> None:
    meta = make_meta()
    original = "Body text of a freshly scraped article."

    meta_out, summary_out, original_out = parse_body(render_body(meta, original))

    assert meta_out == meta
    assert summary_out == ""
    assert original_out == original
    assert "## Summary" not in render_body(meta, original)


def test_round_trip_preserves_unicode() -> None:
    meta = make_meta(source="golem", verdict_reason="Concrete pricing — and benchmarks")
    original = "Überschrift.\n\nText with «quotes», an em dash — 日本語, and an emoji 🚀."
    summary = "A summary carrying non-ASCII: café, naïve, 漢字."

    meta_out, summary_out, original_out = parse_body(render_body(meta, original, summary))

    assert meta_out == meta
    assert summary_out == summary
    assert original_out == original


def test_metadata_survives_a_value_containing_an_html_comment_close() -> None:
    meta = make_meta(verdict_reason="Concrete pricing --> worth keeping")

    meta_out, _, original_out = parse_body(render_body(meta, "Body."))

    assert meta_out["verdict_reason"] == "Concrete pricing --> worth keeping"
    assert original_out == "Body."


def test_article_text_containing_a_bare_comment_close_still_parses() -> None:
    meta = make_meta()
    original = "The template used <!-- a comment --> inline.\n--> and a line of its own."

    meta_out, _, original_out = parse_body(render_body(meta, original))

    assert meta_out == meta
    assert original_out == original


def test_article_text_containing_the_original_close_marker_is_neutralized() -> None:
    meta = make_meta()
    original = f"Before the marker.\n{ORIGINAL_CLOSE}\nAfter the marker."

    body = render_body(meta, original)
    meta_out, _, original_out = parse_body(body)

    # The marker itself is defanged, but nothing around it may be lost and the
    # rest of the record must still parse.
    assert meta_out == meta
    assert original_out.startswith("Before the marker.")
    assert original_out.endswith("After the marker.")
    assert ORIGINAL_CLOSE not in original_out
    assert "<!- - /squelch:original -->" in original_out
    assert body.count(ORIGINAL_CLOSE) == 1


def test_summary_with_a_verdict_renders_tags_and_score() -> None:
    verdict = Verdict(
        relevant=True,
        reason="Concrete capability news",
        summary="They shipped a thing.",
        tags=["models", "tooling"],
        score=8,
    )

    body = render_body(make_meta(), "Body.", verdict.summary, verdict)

    assert "**Tags:** models, tooling · **Score:** 8/10" in body


def test_verdict_without_tags_renders_a_dash() -> None:
    verdict = Verdict(relevant=True, reason="ok", summary="S.", tags=[], score=3)

    assert "**Tags:** — · **Score:** 3/10" in render_body(make_meta(), "Body.", "S.", verdict)


def test_summary_round_trips_when_a_verdict_line_follows_it() -> None:
    verdict = Verdict(
        relevant=True, reason="r", summary="Just the summary.", tags=["models"], score=7
    )

    _, summary_out, _ = parse_body(
        render_body(make_meta(), "Body.", verdict.summary, verdict)
    )

    assert summary_out == "Just the summary."


# -- body length cap --------------------------------------------------------


def test_long_original_is_truncated_but_the_record_stays_parseable() -> None:
    meta = make_meta()
    original = "Long article body. " * 4000  # ~76k chars, well over the cap
    summary = "Short summary that must survive truncation."

    body = render_body(meta, original, summary)

    assert len(body) <= BODY_LIMIT
    assert len(body) > BODY_LIMIT - 1000  # the cap is used, not wildly undershot

    meta_out, summary_out, original_out = parse_body(body)
    assert meta_out == meta
    assert summary_out == summary
    assert original_out.startswith("Long article body.")
    assert original_out.endswith("[truncated]")


def test_a_body_under_the_cap_is_left_alone() -> None:
    body = render_body(make_meta(), "Short body.", "Short summary.")

    assert len(body) < BODY_LIMIT
    assert "[truncated]" not in body


# -- parse_issue / IssueRecord ---------------------------------------------


def make_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "number": 17,
        "title": "A scraped article",
        "labels": [
            {"name": "source:arstechnica"},
            {"name": Status.READY.value},
            {"name": "topic:models"},
        ],
        "state": "open",
        "html_url": "https://github.com/owner/repo/issues/17",
        "created_at": "2025-01-06T09:30:00Z",
        "body": render_body(make_meta(), "The article text.", "The summary."),
    }
    payload.update(overrides)
    return payload


def test_parse_issue_maps_the_github_payload() -> None:
    record = parse_issue(make_payload())

    assert record.number == 17
    assert record.title == "A scraped article"
    assert record.labels == ["source:arstechnica", Status.READY.value, "topic:models"]
    assert record.state == "open"
    assert record.html_url == "https://github.com/owner/repo/issues/17"
    assert record.created_at == datetime(2025, 1, 6, 9, 30, tzinfo=UTC)
    assert record.summary == "The summary."
    assert record.original == "The article text."


def test_metadata_is_exposed_through_uid_url_and_source_properties() -> None:
    record = parse_issue(make_payload())

    assert record.uid == "0123456789abcdef"
    assert record.url == "https://example.com/2025/01/story"
    assert record.source == "arstechnica"


def test_missing_metadata_yields_empty_property_strings() -> None:
    record = parse_issue(make_payload(body="Just some hand-written text."))

    assert record.uid == ""
    assert record.url == ""
    assert record.source == ""


def test_status_is_picked_out_of_a_mixed_label_list() -> None:
    record = parse_issue(
        make_payload(
            labels=[
                {"name": "topic:security"},
                {"name": "source:krebs"},
                {"name": Status.PUBLISHED.value},
                {"name": "good first issue"},
            ]
        )
    )

    assert record.status is Status.PUBLISHED


def test_status_is_none_when_no_status_label_is_present() -> None:
    record = parse_issue(make_payload(labels=[{"name": "source:krebs"}]))

    assert record.status is None


def test_an_unknown_status_label_does_not_hide_a_valid_one() -> None:
    record = parse_issue(
        make_payload(labels=[{"name": "status:archived"}, {"name": Status.RAW.value}])
    )

    assert record.status is Status.RAW


def test_text_falls_back_to_the_raw_body_for_a_hand_opened_issue() -> None:
    # Community submissions have no metadata block and no original marker.
    hand_written = "Please look at this: https://example.com/interesting"
    record = parse_issue(make_payload(body=hand_written, labels=[]))

    assert record.original == ""
    assert record.raw_body == hand_written
    assert record.text == hand_written


def test_text_prefers_the_marked_original_over_the_raw_body() -> None:
    record = parse_issue(make_payload())

    assert record.text == "The article text."
    assert record.raw_body != record.text


def test_an_empty_body_parses_into_an_empty_record() -> None:
    record = parse_issue(make_payload(body=None))

    assert record.meta == {}
    assert record.summary == ""
    assert record.text == ""


def test_optional_payload_fields_fall_back_to_defaults() -> None:
    payload = {"number": 3, "title": "Bare payload", "body": ""}

    record = parse_issue(payload)

    assert record.labels == []
    assert record.state == "open"
    assert record.created_at is None


# -- label state machine ----------------------------------------------------


def test_swap_status_replaces_the_status_label_and_keeps_the_rest() -> None:
    labels = ["source:krebs", Status.RAW.value, "topic:security", "topic:models"]

    swapped = IssueStore._swap_status(labels, Status.READY)

    assert Status.RAW.value not in swapped
    assert swapped.count(Status.READY.value) == 1
    assert set(swapped) == {
        "source:krebs",
        "topic:security",
        "topic:models",
        Status.READY.value,
    }


def test_swap_status_adds_a_status_when_the_issue_had_none() -> None:
    assert IssueStore._swap_status(["source:krebs"], Status.RAW) == [
        "source:krebs",
        Status.RAW.value,
    ]


def test_swap_status_drops_every_stale_status_label() -> None:
    labels = [Status.RAW.value, Status.READY.value, "source:x"]

    swapped = IssueStore._swap_status(labels, Status.PUBLISHED)

    assert swapped == ["source:x", Status.PUBLISHED.value]


def test_swap_status_does_not_mutate_the_input_list() -> None:
    labels = [Status.RAW.value, "source:x"]

    IssueStore._swap_status(labels, Status.REJECTED)

    assert labels == [Status.RAW.value, "source:x"]


def test_a_swapped_record_reports_its_new_status() -> None:
    record = IssueRecord(
        number=1,
        title="t",
        labels=IssueStore._swap_status(["source:x", Status.RAW.value], Status.REJECTED),
    )

    assert record.status is Status.REJECTED
