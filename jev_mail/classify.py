from __future__ import annotations

from jev_mail.config import AppConfig, Decision
from jev_mail.providers.base import JevClient


def classify(client: JevClient, config: AppConfig, state: str) -> dict[str, float]:
    """One Jev call, one yes/no question per configured category."""
    return client.decide(state, {c.name: c.description for c in config.categories})


def decide(config: AppConfig, probabilities: dict[str, float]) -> Decision:
    matched = [c for c in config.categories if probabilities[c.name] >= config.category_threshold(c)]
    labels = tuple(config.label_folder(c.label) for c in matched) or (config.label_folder(config.unmatched_label),)
    disposition = next(
        (c.disposition for c in matched if c.disposition and probabilities[c.name] >= config.disposition_threshold(c)),
        None,
    )
    destination = config.mailbox.folders.get(disposition) if disposition in ("archive", "quarantine") else None
    return Decision(labels=labels, destination=destination, probabilities=dict(probabilities))
