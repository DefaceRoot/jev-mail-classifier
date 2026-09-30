from __future__ import annotations

import os

from jev_mail.config import JevSettings

from .base import EmailRejected, InputTooLong, JevClient, ProviderError
from .openrouter import OpenRouterJevClient
from .typesafe_direct import TypeSafeDirectClient
from .vercel_gateway import VercelGatewayJevClient

# (provider name, env var, client class), in auto-detect priority order.
_BACKENDS = (
    ("typesafe", "TYPESAFE_API_KEY", TypeSafeDirectClient),
    ("openrouter", "OPENROUTER_API_KEY", OpenRouterJevClient),
    ("vercel", "AI_GATEWAY_API_KEY", VercelGatewayJevClient),
)

__all__ = ["EmailRejected", "InputTooLong", "JevClient", "ProviderError", "get_jev_client"]


def _build(client_cls, api_key: str, settings: JevSettings) -> JevClient:
    # Only the OpenRouter adapter takes a model id; the other two pin their own.
    if client_cls is OpenRouterJevClient and settings.model:
        return client_cls(api_key, model=settings.model)
    return client_cls(api_key)


def get_jev_client(settings: JevSettings, env: dict | None = None) -> JevClient:
    env = env if env is not None else os.environ

    if settings.provider == "auto":
        for _, env_var, client_cls in _BACKENDS:
            api_key = env.get(env_var)
            if api_key:
                return _build(client_cls, api_key, settings)
        raise ProviderError(
            "no Jev API key found -- set one of TYPESAFE_API_KEY, OPENROUTER_API_KEY, "
            "or AI_GATEWAY_API_KEY"
        )

    for name, env_var, client_cls in _BACKENDS:
        if settings.provider == name:
            api_key = env.get(env_var)
            if not api_key:
                raise ProviderError(f"jev.provider is {name!r} but {env_var} isn't set")
            return _build(client_cls, api_key, settings)

    raise ProviderError(f"unknown jev.provider: {settings.provider!r}")
