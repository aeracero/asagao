"""python -m asagao で起動する。"""
from __future__ import annotations

import asyncio
import logging
import signal
import sys

from . import config


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("discord").setLevel(logging.WARNING)

    missing = config.missing_env()
    if missing:
        logging.error("環境変数が足りません: %s", ", ".join(missing))
        sys.exit(1)

    from .bot import AlarmBot

    async def runner() -> None:
        bot = AlarmBot()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:  # Railway は再デプロイ時に SIGTERM を送る
                loop.add_signal_handler(sig, lambda: asyncio.ensure_future(bot.close()))
            except NotImplementedError:
                pass
        async with bot:
            await bot.start(config.DISCORD_TOKEN)

    asyncio.run(runner())


if __name__ == "__main__":
    main()
