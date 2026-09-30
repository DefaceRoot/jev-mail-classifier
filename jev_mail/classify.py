from __future__ import annotations

from jev_mail.config import AppConfig, Decision
from jev_mail.providers.base import JevClient


def classify(client: JevClient, config: AppConfig, state: str) -> dict[str, float]:
    """One Jev call, one yes/no question per configured category."""
    return client.decide(state, {c.name: c.description for c in config.categories})


def decide(config: AppConfig, probabilities: dict[str, float]) -> Decision:
    """Every category that clears its threshold contributes a label. The first
    category in config (= priority) order that has a disposition and clears its
    disposition_threshold picks the single destination, so there is at most one
    move per email. `keep` is a disposition that means "stay put" and, being
    first, shadows lower-priority archive/quarantine."""
    matched = [c for c in config.categories if probabilities[c.name] >= config.category_threshold(c)]
    labels = tuple(config.label_folder(c.label) for c in matched) or (config.label_folder(config.unmatched_label),)
    disposition = next(
        (c.disposition for c in matched if c.disposition and probabilities[c.name] >= config.disposition_threshold(c)),
        None,
    )
    destination = config.mailbox.folders.get(disposition) if disposition in ("archive", "quarantine") else None
    return Decision(labels=labels, destination=destination, probabilities=dict(probabilities))
