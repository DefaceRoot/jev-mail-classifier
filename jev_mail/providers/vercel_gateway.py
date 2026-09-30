from __future__ import annotations

import httpx

from .base import build_noul_questions, extract_probabilities, to_provider_error

EVALUATION_URL = "https://ai-gateway.vercel.sh/v4/ai/evaluation-model"
MODEL_ID = "typesafe-ai/jev"

# Best-effort: pieced together from Vercel's changelog/model page and search
# results, not a live-tested integration (no vck_... key was available while
# building this). The AI SDK uses `boolean` where OpenRouter/TypeSafe use
# `noul` for the same yes/no primitive -- if this endpoint/header shape turns
# out wrong, this is the one file that needs fixing.


class VercelGatewayJevClient:
    def __init__(self, api_key: str, timeout: float = 15.0):
        self._api_key = api_key
        self._timeout = timeout

    def decide(self, state: str, categories: dict[str, str]) -> dict[str, float]:
        questions = build_noul_questions(categories, noul_type="boolean")
        try:
            response = httpx.post(
                EVALUATION_URL,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                    "ai-model-id": MODEL_ID,
                    "ai-evaluation-model-specification-version": "4",
                    "ai-gateway-protocol-version": "0.0.1",
                },
                json={"state": state, "questions": questions},
                timeout=self._timeout,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise to_provider_error("Vercel AI Gateway", exc) from exc

        return extract_probabilities(response.json().get("answers", {}), list(categories))
