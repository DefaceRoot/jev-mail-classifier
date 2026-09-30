from __future__ import annotations

import httpx

from .base import build_noul_questions, extract_probabilities, to_provider_error

SYSTEMONE_URL = "https://api.typesafe.ai/v1/systemone"


class TypeSafeDirectClient:
    """Documented endpoint, not yet live-tested by us -- flagged in the README.
    Same request/response shape as the OpenRouter backend we did verify."""

    def __init__(self, api_key: str, model: str = "jev-latest", timeout: float = 15.0):
        self._api_key = api_key
        self._model = model
        self._timeout = timeout

    def decide(self, state: str, categories: dict[str, str]) -> dict[str, float]:
        questions = build_noul_questions(categories)
        try:
            response = httpx.post(
                SYSTEMONE_URL,
                headers={"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"},
                json={"model": self._model, "state": state, "questions": questions},
                timeout=self._timeout,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise to_provider_error("TypeSafe API", exc) from exc

        return extract_probabilities(response.json().get("answers", {}), list(categories))
