"""Точка входа: запуск polling с реконнектами и корректным завершением."""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ErrorEvent
from aiohttp import ClientError

from config import Config
from db import Database
from handlers import group, private
from matcher import WordMatcher

logger = logging.getLogger("modbot")


async def main() -> None:
    """Инициализация и запуск бота."""
    cfg = Config.from_env()
    logging.basicConfig(
        level=cfg.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    db = Database(cfg.db_path)
    await db.connect()
    bot = Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage(), db=db, matcher=WordMatcher())
    dp.include_routers(private.router, group.router)

    @dp.error()
    async def on_error(event: ErrorEvent) -> bool:
        logger.error("Необработанная ошибка в хендлере", exc_info=event.exception)
        return True

    backoff = 1
    try:
        while True:
            try:
                # SIGINT/SIGTERM обрабатываются aiogram -> polling завершается штатно
                await dp.start_polling(
                    bot, allowed_updates=dp.resolve_used_update_types(), polling_timeout=30
                )
                break
            except TelegramUnauthorizedError:
                logger.critical("Неверный BOT_TOKEN")
                break
            except (TelegramNetworkError, ClientError, asyncio.TimeoutError, OSError) as exc:
                logger.warning("Нет связи с Telegram (%s). Повтор через %ss", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
    finally:
        logger.info("Останавливаюсь...")
        await bot.session.close()
        await db.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
