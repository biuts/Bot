"""Конфигурация приложения (читается из .env / переменных окружения)."""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass(frozen=True, slots=True)
class Config:
    """Настройки бота."""

    bot_token: str
    database_url: str
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "Config":
        """Загружает конфиг из окружения; бросает RuntimeError без токена или БД."""
        load_dotenv()
        token = os.getenv("BOT_TOKEN", "").strip()
        if not token:
            raise RuntimeError("BOT_TOKEN не задан. Создайте .env по образцу .env.example")
        dsn = os.getenv("DATABASE_URL", "").strip()
        if not dsn:
            raise RuntimeError("DATABASE_URL не задан (External Database URL из Render)")
        return cls(
            bot_token=token,
            database_url=dsn,
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        )
