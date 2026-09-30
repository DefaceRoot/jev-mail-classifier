from __future__ import annotations

import httpx

from .base import build_noul_questions, extract_probabilities, to_provider_error

DECISIONS_URL = "https://openrouter.ai/api/alpha/decisions"


class OpenRouterJevClient:
    """Verified live against OpenRouter's Decisions endpoint."""

    def __init__(self, api_key: str, model: str = "typesafe/jev-1.13", timeout: float = 15.0):
        self._api_key = api_key
        self._model = model
        self._timeout = timeout

    def decide(self, state: str, categories: dict[str, str]) -> dict[str, float]:
        questions = build_noul_questions(categories)
        try:
            response = httpx.post(
                DECISIONS_URL,
                headers={"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"},
                json={"model": self._model, "state": state, "questions": questions},
                timeout=self._timeout,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise to_provider_error("OpenRouter", exc) from exc

        return extract_probabilities(response.json().get("answers", {}), list(categories))
