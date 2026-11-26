"""Single source of truth for environment-variable sanitization.

Child processes (task runners, gateways, PTY shells) must never inherit
infrastructure credentials or provider API keys from the backend process —
agent credentials are injected explicitly per agent from the secret vault.
"""

import os

# Infrastructure / platform secrets
_INFRA_SENSITIVE_PREFIXES = (
    "AWS_",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_CREDENTIALS",
    "KUBECONFIG",
    "DOCKER_",
    "GITHUB_TOKEN",
    "GITLAB_TOKEN",
    "HEROKU_API_KEY",
    "STRIPE_",
    "TWILIO_",
    "SENDGRID_",
    "DATABASE_URL",
    "POSTGRES_",
    "PGPASS",
    "PGUSER",
    "PGHOST",
    "PGSERVICE",
    "SQLALCHEMY_",
    "REDIS_URL",
    "RABBITMQ_",
    "KAFKA_",
    "LDAP_",
    "VAULT_TOKEN",
    "VAULT_ADDR",
    "HERMESHQ_",
    "JWT_SECRET",
    "FERNET_KEY",
    "ADMIN_PASSWORD",
    "OIDC_CLIENT_SECRET",
)

# LLM provider credentials — agents receive theirs explicitly via config/env,
# never by inheriting the backend's (which would bill the platform operator
# and make per-agent usage tracking impossible).
_PROVIDER_SENSITIVE_KEYS = (
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "OPENROUTER_API_KEY",
    "KIMI_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "GLM_API_KEY",
    "ZAI_API_KEY",
    "Z_AI_API_KEY",
    "NOUS_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "RESEND_API_KEY",
)

# Provider-prefixed patterns (AUXILIARY_*_API_KEY etc.)
_PROVIDER_SENSITIVE_PREFIXES = (
    "AUXILIARY_",
    "AZURE_OPENAI_",
    "BEDROCK_",
)

SENSITIVE_ENV_KEYS = _INFRA_SENSITIVE_PREFIXES + _PROVIDER_SENSITIVE_KEYS + _PROVIDER_SENSITIVE_PREFIXES


def is_sensitive_env_key(key: str) -> bool:
    upper = key.upper()
    return any(upper.startswith(prefix) for prefix in SENSITIVE_ENV_KEYS)


def build_safe_env(extra_allow: tuple[str, ...] = ()) -> dict[str, str]:
    """Build a sanitized copy of os.environ with sensitive keys removed.

    ``extra_allow`` lists exact key names that are kept even if they match a
    sensitive prefix (used sparingly, e.g. documented non-secret config).
    """
    allow = {k.upper() for k in extra_allow}
    return {key: value for key, value in os.environ.items() if key.upper() in allow or not is_sensitive_env_key(key)}
