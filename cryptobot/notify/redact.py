"""Secret redaction for the notification layer.

Every string that leaves the notifier -- HTTP error details, response bodies,
audit records, log fields -- is passed through :func:`redact` first.  The layer
knows which environment variables hold secrets and scrubs their *values*, plus a
few structural patterns (a Telegram bot URL, an ``Authorization: Bearer``
header), so a token can never reach ``logs/`` or ``logs/notifications.jsonl``.

Secrets are only ever read from the environment; they are never written to
``config.yaml`` (that file is checked by ``config.assert_no_secrets_in_config``).
"""

from __future__ import annotations

import os
import re
from typing import Iterable, Mapping, Optional, Tuple

#: Replacement marker written instead of a secret value.
REDACTED = "[REDACTED]"

#: Environment variables whose *values* must never be printed or persisted.
SECRET_ENV_VARS: Tuple[str, ...] = (
    "CRYPTOBOT_NTFY_TOKEN",
    "CRYPTOBOT_NTFY_TOPIC",
    "CRYPTOBOT_TELEGRAM_BOT_TOKEN",
    "CRYPTOBOT_TELEGRAM_CHAT_ID",
    "CRYPTOBOT_WEBHOOK_URL",
)

#: Values shorter than this are left alone: masking a 3-char value would damage
#: ordinary text far more than it protects anything.
_MIN_SECRET_LEN = 4

_PATTERNS = (
    # Telegram bot URL: /bot<digits>:<token>  -> keep the prefix, drop the rest.
    (re.compile(r"(?i)(/bot)\d{5,}:[A-Za-z0-9_\-]+"), lambda m: m.group(1) + REDACTED),
    # Bearer <value>
    (re.compile(r"(?i)\b(bearer)(\s+)[A-Za-z0-9._\-]{6,}"), lambda m: m.group(1) + m.group(2) + REDACTED),
    # anything that looks like "<something-token|key|secret>: value"
    (re.compile(r"(?i)([a-z0-9\-_]*(?:token|key|secret)[a-z0-9\-_]*)\s*[:=]\s*\S+"),
     lambda m: m.group(1) + "=" + REDACTED),
)


def known_secrets(environ: Optional[Mapping[str, str]] = None,
                  extra: Iterable[str] = ()) -> Tuple[str, ...]:
    """Return the secret values currently configured (longest first)."""
    env = os.environ if environ is None else environ
    values = {str(env.get(name, "")).strip() for name in SECRET_ENV_VARS}
    values.update(str(value).strip() for value in extra)
    ordered = sorted((v for v in values if len(v) >= _MIN_SECRET_LEN), key=len, reverse=True)
    return tuple(ordered)


def redact(value: object, *, secrets: Optional[Iterable[str]] = None,
           environ: Optional[Mapping[str, str]] = None) -> str:
    """Return ``value`` as text with every known secret replaced by ``[REDACTED]``.

    Accepts anything (exceptions included) so callers can do
    ``redact(exc)`` inside an ``except`` block.  Never raises.
    """
    if value is None:
        return ""
    try:
        text = value if isinstance(value, str) else str(value)
    except Exception:  # pragma: no cover - defensive: __str__ must not break us
        return REDACTED
    candidates = tuple(secrets) if secrets is not None else known_secrets(environ)
    for secret in candidates:
        if secret and len(secret) >= _MIN_SECRET_LEN and secret in text:
            text = text.replace(secret, REDACTED)
    for pattern, repl in _PATTERNS:
        try:
            text = pattern.sub(repl, text)
        except re.error:  # pragma: no cover - patterns are static
            continue
    return text


def redact_mapping(mapping: Optional[Mapping[str, object]], *,
                   secrets: Optional[Iterable[str]] = None) -> dict:
    """Redact every key and value of a header/payload mapping."""
    if not mapping:
        return {}
    candidates = tuple(secrets) if secrets is not None else known_secrets()
    return {
        redact(key, secrets=candidates): redact(value, secrets=candidates)
        for key, value in mapping.items()
    }


__all__ = ["REDACTED", "SECRET_ENV_VARS", "known_secrets", "redact", "redact_mapping"]
