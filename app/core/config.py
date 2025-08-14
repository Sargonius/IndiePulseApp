import os
from typing import Optional

def get_env(key: str, default: Optional[str] = None) -> Optional[str]:
    value = os.getenv(key, default)
    return value

APP_NAME = get_env("APP_NAME", "IndiePulse")
APP_HOST = get_env("APP_HOST", "127.0.0.1")
APP_PORT = int(get_env("APP_PORT", "8000"))
DATABASE_URL = get_env("DATABASE_URL", "sqlite:///./indiepulse.db")
LOG_LEVEL = get_env("LOG_LEVEL", "INFO")
