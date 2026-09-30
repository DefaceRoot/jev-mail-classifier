import httpx
import pytest

from jev_mail.config import JevSettings
from jev_mail.providers import ProviderError, get_jev_client
from jev_mail.providers.base import describe_http_error
from jev_mail.providers.openrouter import OpenRouterJevClient
from jev_mail.providers.typesafe_direct import TypeSafeDirectClient
from jev_mail.providers.vercel_gateway import VercelGatewayJevClient


def _fake_response(json_body: dict, status_code: int = 200) -> httpx.Response:
    return httpx.Response(status_code, json=json_body, request=httpx.Request("POST", "https://example.com"))


CATEGORIES = {"invoice": "Invoice or billing", "urgent": "Time-sensitive"}


@pytest.mark.parametrize(
    "client_cls,answer_key",
    [(OpenRouterJevClient, "noul"), (TypeSafeDirectClient, "noul"), (VercelGatewayJevClient, "boolean")],
)
def test_decide_normalizes_probabilities(monkeypatch, client_cls, answer_key):
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        return _fake_response(
            {
                "answers": {
                    "invoice": {"type": answer_key, answer_key: 0.9},
                    "urgent": {"type": answer_key, answer_key: 0.2},
                }
            }
        )

    monkeypatch.setattr(httpx, "post", fake_post)

    client = client_cls(api_key="test-key")
    result = client.decide("some email body", CATEGORIES)

    assert result == {"invoice": 0.9, "urgent": 0.2}
    assert captured["json"]["state"] == "some email body"
    assert set(captured["json"]["questions"]) == set(CATEGORIES)


def test_decide_raises_provider_error_on_http_failure(monkeypatch):
    def fake_post(*args, **kwargs):
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(httpx, "post", fake_post)
    client = OpenRouterJevClient(api_key="test-key")

    with pytest.raises(ProviderError):
        client.decide("body", CATEGORIES)


def test_describe_http_error_status_error_is_short():
    response = _fake_response({}, status_code=403)
    exc = httpx.HTTPStatusError("403 Forbidden for url ...", request=response.request, response=response)
    assert describe_http_error(exc) == "403 Forbidden"


def test_describe_http_error_non_status_error_passes_through():
    exc = httpx.ConnectError("Connection refused")
    assert describe_http_error(exc) == "Connection refused"


def test_decide_error_message_is_short_not_the_full_httpx_dump(monkeypatch):
    """Regression: httpx's own str() on an HTTPStatusError includes the full
    request URL plus an MDN boilerplate line -- unreadable dumped into a
    single status line in the TUI or CLI stderr."""
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _fake_response({}, status_code=403))
    client = OpenRouterJevClient(api_key="test-key")

    with pytest.raises(ProviderError) as exc_info:
        client.decide("body", CATEGORIES)

    message = str(exc_info.value)
    assert "403" in message
    assert "developer.mozilla.org" not in message
    assert len(message) < 100


def test_decide_raises_provider_error_on_missing_answer(monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _fake_response({"answers": {}}))
    client = OpenRouterJevClient(api_key="test-key")

    with pytest.raises(ProviderError):
        client.decide("body", CATEGORIES)


def test_get_jev_client_auto_detect_priority(monkeypatch):
    env = {"OPENROUTER_API_KEY": "or-key", "AI_GATEWAY_API_KEY": "vck-key"}
    client = get_jev_client(JevSettings(provider="auto"), env=env)
    assert isinstance(client, OpenRouterJevClient)

    env = {"TYPESAFE_API_KEY": "ts-key", "OPENROUTER_API_KEY": "or-key"}
    client = get_jev_client(JevSettings(provider="auto"), env=env)
    assert isinstance(client, TypeSafeDirectClient)

    env = {"AI_GATEWAY_API_KEY": "vck-key"}
    client = get_jev_client(JevSettings(provider="auto"), env=env)
    assert isinstance(client, VercelGatewayJevClient)


def test_get_jev_client_auto_detect_no_keys_raises():
    with pytest.raises(ProviderError):
        get_jev_client(JevSettings(provider="auto"), env={})


def test_get_jev_client_explicit_provider_missing_key_raises():
    with pytest.raises(ProviderError):
        get_jev_client(JevSettings(provider="vercel"), env={})


def test_get_jev_client_explicit_provider_unknown_raises():
    with pytest.raises(ProviderError):
        get_jev_client(JevSettings(provider="not-a-real-provider"), env={"TYPESAFE_API_KEY": "x"})


def test_get_jev_client_passes_configured_model_to_openrouter():
    client = get_jev_client(
        JevSettings(provider="openrouter", model="typesafe/jev-9.9"), env={"OPENROUTER_API_KEY": "or-key"}
    )
    assert client._model == "typesafe/jev-9.9"
