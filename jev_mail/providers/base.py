from __future__ import annotations

from typing import Protocol

import httpx


class ProviderError(Exception):
    """Raised when a Jev backend can't be reached or returns something unexpected.
    A plain ProviderError is never about one email: it means retry later."""


class InputTooLong(ProviderError):
    """The state exceeded the model's context window."""


class EmailRejected(ProviderError):
    """The backend refused this particular input; retrying it can't succeed."""


# Client errors that say something is wrong with our account or endpoint, not
# with the email, so they must stop the process instead of poisoning mail.
_ACCOUNT_LEVEL_STATUSES = {401, 402, 403, 404, 408, 429}


def describe_http_error(exc: httpx.HTTPError) -> str:
    """A short, human-readable summary -- httpx's own str() on an
    HTTPStatusError includes the full request URL plus an MDN boilerplate
    line ("For more information check: developer.mozilla.org/...") which is
    unreadable dumped into a status line or a single-line CLI error."""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"{exc.response.status_code} {exc.response.reason_phrase}"
    return str(exc)


def to_provider_error(service: str, exc: httpx.HTTPError) -> ProviderError:
    message = f"{service} request failed: {describe_http_error(exc)}"
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status == 400 and "max_tokens_exceeded" in exc.response.text:
            return InputTooLong(message)
        if 400 <= status < 500 and status not in _ACCOUNT_LEVEL_STATUSES:
            return EmailRejected(message)
    return ProviderError(message)


class JevClient(Protocol):
    def decide(self, state: str, categories: dict[str, str]) -> dict[str, float]:
        """Given `state` text and {category_name: description}, returns
        {category_name: probability_it_applies}, one independent yes/no
        judgment per category (multi-label)."""
        ...


def build_noul_questions(categories: dict[str, str], noul_type: str = "noul") -> dict:
    """{category: description} -> the `questions` block Jev expects: one
    yes/no (noul) question per category, phrased from its description."""
    return {
        name: {
            "type": noul_type,
            "instructions": f"Does this apply: {description}",
            "criteria": {"true": description, "false": "Does not apply"},
        }
        for name, description in categories.items()
    }


def extract_probabilities(answers: dict, categories: list[str]) -> dict[str, float]:
    """Normalizes a decisions-style response's `answers` block back to
    {category: probability}, tolerant of the noul/boolean naming difference
    between backends."""
    result: dict[str, float] = {}
    for name in categories:
        answer = answers.get(name)
        if not isinstance(answer, dict):
            raise ProviderError(f"no answer returned for category {name!r}")
        probability = answer.get("noul", answer.get("boolean"))
        if probability is None:
            raise ProviderError(f"couldn't find a probability in the answer for {name!r}: {answer!r}")
        result[name] = float(probability)
    return result
