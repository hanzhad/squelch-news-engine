"""Label definitions and one-time bootstrap.

Labels are the state machine, so they need to exist with sensible colours
before the pipeline runs. GitHub will happily invent a label on first use, but
it picks a random colour and no description, which makes the issue list much
harder to read at a glance.
"""

from __future__ import annotations

from typing import Any, NamedTuple
from urllib.parse import quote

from ..core.config import Config
from ..core.log import get_logger
from ..core.models import Period, Status
from .client import GitHubClient
from .digests import digest_label
from .issues import SENT_PREFIX
from .ledger import LEDGER_LABEL

log = get_logger(__name__)


class LabelSpec(NamedTuple):
    name: str
    color: str
    description: str


STATUS_LABELS = [
    LabelSpec(Status.RAW.value, "d4c5f9", "Scraped, waiting to be classified"),
    LabelSpec(Status.RELEVANT.value, "fef2c0", "Worth publishing, waiting for its write-up"),
    LabelSpec(Status.READY.value, "0e8a16", "Written up, waiting on one or more channels"),
    LabelSpec(Status.PUBLISHED.value, "1d76db", "Delivered to every enabled channel"),
    LabelSpec(Status.REJECTED.value, "b60205", "Classified as noise and closed"),
]


def label_specs(config: Config) -> list[LabelSpec]:
    specs = list(STATUS_LABELS)
    specs.append(LabelSpec(LEDGER_LABEL, "c5def5", "Bookkeeping: the scraper's dedup ledger"))
    # The roundups. Their own axis, never `status:` — that is what keeps a
    # digest issue invisible to every article query, including the window the
    # next digest reads.
    specs += [
        LabelSpec(digest_label(period), "5319e7", f"A {period.label} waiting to be posted")
        for period in Period
    ]
    # Every channel, not only the enabled ones: turning a channel off must not
    # make the labels it already wrote unreadable.
    specs += [
        LabelSpec(f"{SENT_PREFIX}{channel.id}", "bfd4f2", f"Delivered to {channel.id}")
        for channel in config.channels
    ]
    specs += [
        LabelSpec(f"source:{source.id}", "ededed", f"Scraped from {source.id}")
        for source in config.sources
    ]
    specs += [
        LabelSpec(f"topic:{topic}", "fbca04", f"Topic: {topic}") for topic in config.topics
    ]
    return specs


def ensure_labels(client: GitHubClient, specs: list[LabelSpec]) -> None:
    existing: dict[str, dict[str, Any]] = {
        label["name"]: label for label in client.paginate(f"/repos/{client.repo}/labels")
    }

    for spec in specs:
        current = existing.get(spec.name)
        if current is None:
            client.request(
                "POST",
                f"/repos/{client.repo}/labels",
                json={
                    "name": spec.name,
                    "color": spec.color,
                    "description": spec.description,
                },
            )
            log.info("created label %s", spec.name)
        elif current.get("color") != spec.color or current.get("description") != spec.description:
            client.request(
                "PATCH",
                f"/repos/{client.repo}/labels/{quote(spec.name, safe='')}",
                json={"color": spec.color, "description": spec.description},
            )
            log.info("updated label %s", spec.name)
