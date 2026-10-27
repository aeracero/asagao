"""bot 本体。RAM を抑えるため Intents とキャッシュは最小限にしている。"""
from __future__ import annotations

import logging
import time

import aiohttp
import discord
from discord import app_commands

from . import config
from .audio import AudioStore
from .commands import setup_commands
from .db import Database
from .runner import RunManager
from .scheduler import Scheduler
from .voice import VoiceManager
from .web import start_web

log = logging.getLogger(__name__)

MEMBER_TTL = 300  # 秒。ダッシュボード用のメンバー情報を短時間だけ覚えておく


class AlarmBot(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.none()
        intents.guilds = True        # サーバー・VC・ロール（権限計算に必要）
        intents.voice_states = True  # 誰がどの VC にいるか
        member_cache = discord.MemberCacheFlags.none()
        member_cache.voice = True    # VC にいるメンバーだけキャッシュ
        super().__init__(
            intents=intents,
            member_cache_flags=member_cache,
            chunk_guilds_at_startup=False,
            max_messages=None,       # メッセージキャッシュなし
        )
        self.tree = app_commands.CommandTree(self)
        self.db = Database(config.DATA_DIR / "alarm.db")
        self.audio = AudioStore(self.db)
        self.voice = VoiceManager()
        self.runs = RunManager(self)
        self.scheduler = Scheduler(self)
        self.http_session: aiohttp.ClientSession | None = None
        self._web_runner = None
        self._members: dict[tuple[int, int], tuple[float, discord.Member | None]] = {}

    async def setup_hook(self) -> None:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        await self.db.init()
        await self.audio.start()
        setup_commands(self)
        if config.SYNC_COMMANDS:
            if config.DEV_GUILD_ID:
                guild = discord.Object(id=config.DEV_GUILD_ID)
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
            else:
                await self.tree.sync()
        self.http_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        self._web_runner = await start_web(self)
        self.loop.create_task(self.scheduler.run(), name="scheduler")
        self.loop.create_task(self.scheduler.maintenance_loop(), name="maintenance")

    async def on_ready(self) -> None:
        log.info("ログインしました: %s（%d サーバー）", self.user, len(self.guilds))

    async def on_voice_state_update(self, member: discord.Member,
                                    before: discord.VoiceState, after: discord.VoiceState) -> None:
        self.runs.on_voice_state_update(member, before, after)

    async def close(self) -> None:
        self.runs.stop_all("shutdown")
        if self._web_runner is not None:
            await self._web_runner.cleanup()
        if self.http_session is not None:
            await self.http_session.close()
        await super().close()
        await self.db.close()

    # --- 便利関数 -----------------------------------------------------------
    async def get_member_cached(self, guild: discord.Guild, user_id: int) -> discord.Member | None:
        """メンバー情報を API から取得して5分だけ覚える（全員分はキャッシュしない）。"""
        key = (guild.id, user_id)
        hit = self._members.get(key)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        member = guild.get_member(user_id)
        if member is None:
            try:
                member = await guild.fetch_member(user_id)
            except discord.HTTPException:
                member = None
        if len(self._members) > 500:
            self._members.clear()
        self._members[key] = (time.monotonic() + MEMBER_TTL, member)
        return member

    async def notify_user(self, user_id: int, text: str) -> None:
        try:
            user = self.get_user(user_id) or await self.fetch_user(user_id)
            await user.send(text)
        except discord.HTTPException:
            log.info("DM を送れませんでした: user=%s", user_id)
