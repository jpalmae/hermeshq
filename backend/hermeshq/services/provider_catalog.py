from hermeshq.models.provider import ProviderDefinition


PROVIDER_RUNTIME_ALIASES: dict[str, str] = {
    "openai": "openai-codex",
}


def normalize_runtime_provider(provider: str | None) -> str | None:
    if not provider:
        return provider
    return PROVIDER_RUNTIME_ALIASES.get(provider, provider)


BUILTIN_PROVIDERS: list[dict] = [
    {
        "slug": "kimi-coding",
        "name": "Kimi Coding",
        "runtime_provider": "kimi-coding",
        "auth_type": "api_key",
        "base_url": "https://api.kimi.com/coding/v1",
        "default_model": "kimi-k2.5",
        "description": "Moonshot Kimi coding provider using the Kimi coding API endpoint.",
        "docs_url": "https://platform.moonshot.ai/",
        "secret_placeholder": "KIMI API key",
        "supports_secret_ref": True,
        "supports_custom_base_url": True,
        "enabled": True,
        "sort_order": 10,
    },
    {
        "slug": "zai",
        "name": "Z.AI Coding Plan",
        "runtime_provider": "zai",
        "auth_type": "api_key",
        "base_url": "https://api.z.ai/api/coding/paas/v4",
        "default_model": "glm-5-turbo",
        "description": "Z.AI coding plan endpoint for GLM coding models.",
        "docs_url": "https://docs.z.ai/devpack/faq",
        "secret_placeholder": "Z.AI API key",
        "supports_secret_ref": True,
        "supports_custom_base_url": True,
        "enabled": True,
        "sort_order": 20,
    },
    {
        "slug": "openrouter",
        "name": "OpenRouter API",
        "runtime_provider": "openrouter",
        "auth_type": "api_key",
        "base_url": "https://openrouter.ai/api/v1",
        "default_model": "anthropic/claude-sonnet-4",
        "description": "OpenRouter unified API for multi-provider model access.",
        "docs_url": "https://openrouter.ai/docs/quickstart",
        "secret_placeholder": "OpenRouter API key",
        "supports_secret_ref": True,
        "supports_custom_base_url": True,
        "enabled": True,
        "sort_order": 30,
    },
    {
        "slug": "openai-api",
        "name": "OpenAI API",
        "runtime_provider": "openai-codex",
        "auth_type": "api_key",
        "base_url": "https://api.openai.com/v1",
        "default_model": "gpt-4.1",
        "description": "Direct OpenAI API using API keys.",
        "docs_url": "https://platform.openai.com/docs/overview",
        "secret_placeholder": "OpenAI API key",
        "supports_secret_ref": True,
        "supports_custom_base_url": True,
        "enabled": True,
        "sort_order": 40,
    },
    {
        "slug": "openai-compatible",
        "name": "OpenAI-compatible API",
        "runtime_provider": "openai-codex",
        "auth_type": "api_key",
        "base_url": None,
        "default_model": None,
        "description": "Generic OpenAI-compatible endpoint for gateways, self-hosted runtimes, and compatible vendors.",
        "docs_url": None,
        "secret_placeholder": "OpenAI-compatible API key",
        "supports_secret_ref": True,
        "supports_custom_base_url": True,
        "enabled": True,
        "sort_order": 45,
    },
    {
        "slug": "gemini-api",
        "name": "Gemini API",
        "runtime_provider": "openai-codex",
        "auth_type": "api_key",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "default_model": "gemini-2.5-pro",
        "description": "Gemini via Google's OpenAI-compatible endpoint.",
        "docs_url": "https://ai.google.dev/gemini-api/docs/openai",
        "secret_placeholder": "Gemini API key",
        "supports_secret_ref": True,
        "supports_custom_base_url": True,
        "enabled": True,
        "sort_order": 50,
    },
    {
        "slug": "anthropic-api",
        "name": "Anthropic API",
        "runtime_provider": "anthropic",
        "auth_type": "api_key",
        "base_url": "https://api.anthropic.com",
        "default_model": "claude-sonnet-4-5",
        "description": "Direct Anthropic API.",
        "docs_url": "https://docs.anthropic.com/en/api/getting-started",
        "secret_placeholder": "Anthropic API key",
        "supports_secret_ref": True,
        "supports_custom_base_url": False,
        "enabled": True,
        "sort_order": 60,
    },
    {
        "slug": "aws-bedrock",
        "name": "AWS Bedrock",
        "runtime_provider": "bedrock",
        "auth_type": "aws_sdk",
        "base_url": "https://bedrock-runtime.us-east-1.amazonaws.com",
        "default_model": "anthropic.claude-sonnet-4-5-20250929-v1:0",
        "description": "Amazon Bedrock using the AWS SDK credential chain and regional Bedrock runtime endpoints.",
        "docs_url": "https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_Converse.html",
        "secret_placeholder": None,
        "supports_secret_ref": False,
        "supports_custom_base_url": True,
        "enabled": True,
        "sort_order": 70,
    },
]


def seed_provider_defaults(existing: ProviderDefinition | None, payload: dict) -> None:
    if not existing:
        return
    existing.name = existing.name or payload["name"]
    existing.runtime_provider = payload["runtime_provider"]
    existing.auth_type = payload["auth_type"]
    existing.base_url = existing.base_url or payload["base_url"]
    existing.default_model = existing.default_model or payload["default_model"]
    existing.description = existing.description or payload["description"]
    existing.docs_url = existing.docs_url or payload["docs_url"]
    existing.secret_placeholder = existing.secret_placeholder or payload["secret_placeholder"]
    existing.supports_secret_ref = payload["supports_secret_ref"]
    existing.supports_custom_base_url = payload["supports_custom_base_url"]
