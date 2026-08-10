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
from ..core.models import Status
from .client import GitHubClient
from .ledger import LEDGER_LABEL

log = get_logger(__name__)


class LabelSpec(NamedTuple):
    name: str
    color: str
    description: str


STATUS_LABELS = [
    LabelSpec(Status.RAW.value, "d4c5f9", "Scraped, waiting for the LLM filter"),
    LabelSpec(Status.READY.value, "0e8a16", "Passed the filter, waiting to be published"),
    LabelSpec(Status.PUBLISHED.value, "1d76db", "Delivered to Discord and the web archive"),
    LabelSpec(Status.REJECTED.value, "b60205", "Filtered out as noise"),
]


def label_specs(config: Config) -> list[LabelSpec]:
    specs = list(STATUS_LABELS)
    specs.append(LabelSpec(LEDGER_LABEL, "c5def5", "Bookkeeping: the scraper's dedup ledger"))
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
