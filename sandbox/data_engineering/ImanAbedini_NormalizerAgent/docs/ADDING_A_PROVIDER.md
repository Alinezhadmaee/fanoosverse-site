# Adding a New LLM Provider

The normalizer is provider-agnostic. Adding a model or vendor does not require
changes to the pipeline, prompts, or schema.

---

## 1. OpenAI-compatible providers (no code)

If the provider implements the OpenAI Chat Completions API (vLLM, Ollama,
Together AI, Groq, Mistral, DeepSeek, or an Iranian cloud endpoint), add a
provider entry to `config/llm.yaml`:

```yaml
providers:
  - name: my_new_provider
    type: openai_compat
    enabled: true
    priority: 2
    base_url: https://api.example.com/v1
    model: deepseek-chat
    api_key_env: MY_NEW_PROVIDER_API_KEY
    timeout_s: 30
    requests_per_minute: 60
    cost_per_mtok: {input: 0.5, output: 1.5}
    params:
      temperature: 0.0
```

Then put the key in `.env` or the process environment:

```bash
MY_NEW_PROVIDER_API_KEY=your-secret-api-key
```

`ProviderConfig` uses `extra="forbid"`. The supported keys are:

| Key | Meaning |
|---|---|
| `name` | Unique provider name used in logs and audit entries |
| `type` | Registered adapter type: `anthropic`, `openai_compat`, or `mock` |
| `model` | Model identifier sent to the provider |
| `api_key_env` | Name of the environment variable that holds the key |
| `base_url` | Base URL for OpenAI-compatible providers |
| `priority` | Lower number means higher priority |
| `max_retries` | Retries per provider for transient failures |
| `timeout_s` | Request timeout in seconds |
| `params` | Extra parameters passed to the provider unchanged |
| `features.json_mode` | Whether the provider supports JSON response mode |
| `features.prompt_caching` | Whether the provider supports prompt caching |
| `cost_per_mtok` | Input/output cost per million tokens, for usage estimates |
| `requests_per_minute` | Optional client-side rate limit |

`priority` and `max_retries` control fallback behavior; there is no `weight`
key.

---

## 2. Custom provider adapter

If the provider uses a different protocol, add an adapter.

### Step 1: Implement the adapter

Create `src/normalizer_agent/llm/providers/my_provider.py`:

```python
from normalizer_agent.llm.base import (
    LLMProvider,
    LLMRequest,
    LLMResponse,
    ProviderPermanentError,
    ProviderTransientError,
)
from normalizer_agent.llm.registry import register, resolve_api_key
from normalizer_agent.settings import ProviderConfig


@register("my_custom_type")
def my_provider_factory(config: ProviderConfig) -> LLMProvider:
    return MyCustomProvider(config)


class MyCustomProvider(LLMProvider):
    def __init__(self, config: ProviderConfig) -> None:
        self.name = config.name
        self.model = config.model
        self.api_key = resolve_api_key(config)
        self.timeout = config.timeout_s

    async def complete(self, request: LLMRequest) -> LLMResponse:
        # Call the provider with httpx.
        # Map 401/403/404/400 to ProviderPermanentError.
        # Map 429/5xx/timeouts to ProviderTransientError.
        ...
        return LLMResponse(
            text=generated_text,
            input_tokens=prompt_tokens,
            output_tokens=completion_tokens,
            latency_ms=elapsed_ms,
            provider=self.name,
            model=self.model,
            cost_usd=cost,
        )

    async def ping(self) -> float:
        # Send a tiny request and return latency in milliseconds.
        ...

    async def aclose(self) -> None:
        return None
```

### Step 2: Register the module

Import the adapter in `src/normalizer_agent/llm/registry.py` so the
`@register` decorator runs at import time:

```python
from normalizer_agent.llm.providers import my_provider as _my_provider  # noqa: F401
```

### Step 3: Configure and test

```yaml
providers:
  - name: my_custom
    type: my_custom_type
    model: my-model
    api_key_env: MY_CUSTOM_API_KEY
    timeout_s: 30
```

```bash
normalizer validate-config -c config
normalizer llm-check -c config
```

`validate-config` checks the YAML shape and provider type. `llm-check` sends
a real request to every enabled provider and reports latency or the error.
