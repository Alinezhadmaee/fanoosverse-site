"""Provider registry: ``type`` string -> adapter class.

Adding a provider = one new adapter module + one ``register`` call (see
docs/ADDING_A_PROVIDER.md). Nothing outside ``llm/`` ever imports a vendor
adapter directly.
"""

from __future__ import annotations

import os
from collections.abc import Callable

from normalizer_agent.llm.base import LLMError, LLMProvider
from normalizer_agent.settings import ProviderConfig

_REGISTRY: dict[str, Callable[[ProviderConfig], LLMProvider]] = {}


def register(
    provider_type: str,
) -> Callable[[Callable[[ProviderConfig], LLMProvider]], Callable[[ProviderConfig], LLMProvider]]:
    def decorator(
        factory: Callable[[ProviderConfig], LLMProvider],
    ) -> Callable[[ProviderConfig], LLMProvider]:
        if provider_type in _REGISTRY:
            raise LLMError(f"provider type '{provider_type}' already registered")
        _REGISTRY[provider_type] = factory
        return factory

    return decorator


def known_types() -> list[str]:
    return sorted(_REGISTRY)


def build_provider(config: ProviderConfig) -> LLMProvider:
    """Instantiate one provider from its config section.

    Missing API keys are tolerated at build time (the router skips the
    provider and the reason is reported); the mock type needs no key.
    """

    factory = _REGISTRY.get(config.type)
    if factory is None:
        raise LLMError(f"unknown provider type '{config.type}' (known: {', '.join(known_types())})")
    return factory(config)


def resolve_api_key(config: ProviderConfig) -> str | None:
    if not config.api_key_env:
        return None
    return os.environ.get(config.api_key_env)


# Import adapter modules for their registration side effects. Vendor names
# appear ONLY here and inside llm/providers/.
def _load_builtin_adapters() -> None:
    from normalizer_agent.llm.providers import anthropic as _anthropic  # noqa: F401
    from normalizer_agent.llm.providers import mock as _mock  # noqa: F401
    from normalizer_agent.llm.providers import openai_compat as _openai  # noqa: F401


_load_builtin_adapters()
