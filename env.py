"""Minimal .env loader (no dependencies). Secrets stay in memory and are never printed or logged."""
import os
from pathlib import Path

ENV_FILE = Path(__file__).parent / '.env'


def load(path=ENV_FILE):
    """Read KEY=VALUE lines into a dict. Real environment variables win over the file."""
    values = {}
    try:
        for line in Path(path).read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, _, value = line.partition('=')
                values[key.strip()] = value.strip().strip('"').strip("'")
    except OSError:
        pass
    values.update({k: v for k, v in os.environ.items() if k in values or k.startswith(('GEMINI_', 'TELEGRAM_', 'ADVISOR_'))})
    return values


def redact(text, secrets):
    """Remove secret values from any text before it can reach a log or an alert."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, '***')
    return text
