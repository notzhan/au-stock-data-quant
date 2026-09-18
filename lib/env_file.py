"""Load supported local environment settings without executing shell code."""

import os
from pathlib import Path


ENV_KEYS = {
    'LONGPORT_APP_KEY', 'LONGPORT_APP_SECRET', 'LONGPORT_ACCESS_TOKEN',
    'WECHAT_WEBHOOK_QUANT', 'DINGTALK_WEBHOOK_QUANT',
    'TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID',
    'SMTP_HOST', 'SMTP_PORT', 'SMTP_USER', 'SMTP_PASSWORD',
    'ALERT_EMAIL_FROM', 'ALERT_EMAIL_TO', 'MONITOR_CHANNEL',
    'MONITOR_SYMBOLS', 'MONITOR_STRATEGY', 'MONITOR_SIDES', 'BARK_URL',
}


def load_env_file(path):
    """Read supported KEY=value settings, preserving existing environment values."""
    path = Path(path)
    if not path.exists():
        return
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        key = key.strip()
        if key not in ENV_KEYS:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)
