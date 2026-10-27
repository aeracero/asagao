"""VC への接続をサーバー単位で管理する。

- bot は1サーバーで同時に1つの VC にしか入れない
- 同じ VC のアラームは相乗りし、使っているアラームが0になったら退出する
- 別の VC のアラームは、前のアラームが終わるまで待つ
- 再生は1つずつ順番に（同じ VC で同時刻のアラームも交互に鳴る）
"""
from __future__ import annotations

import asyncio
import logging

import discord

log = logging.getLogger(__name__)


class VoiceGone(Exception):
    """再生しようとしたら VC から切断されていた。"""


class _GuildVoice:
    __slots__ = ("cond", "connect_lock", "play_lock", "holders", "channel_id")

    def __init__(self) -> None:
        self.cond = asyncio.Condition()
        self.connect_lock = asyncio.Lock()
        self.play_lock = asyncio.Lock()
        self.holders: set[int] = set()
        self.channel_id: int | None = None


class VoiceManager:
    def __init__(self) -> None:
        self._guilds: dict[int, _GuildVoice] = {}

    def _g(self, guild_id: int) -> _GuildVoice:
        gv = self._guilds.get(guild_id)
        if gv is None:
            gv = self._guilds[guild_id] = _GuildVoice()
        return gv

    async def acquire(self, guild: discord.Guild, channel: discord.VoiceChannel,
                      run_id: int, timeout: float) -> None:
        gv = self._g(guild.id)
        async with gv.cond:
            await asyncio.wait_for(
                gv.cond.wait_for(lambda: not gv.holders or gv.channel_id == channel.id),
                timeout=max(timeout, 0.1),
            )
            gv.holders.add(run_id)
            gv.channel_id = channel.id
        try:
            async with gv.connect_lock:
                vc = guild.voice_client
                if vc is not None and vc.is_connected():
                    if vc.channel is None or vc.channel.id != channel.id:
                        await vc.move_to(channel)
                else:
                    if vc is not None:
                        await vc.disconnect(force=True)
                    # 受信音声は使わないのでスピーカーミュートで入る
                    await channel.connect(timeout=20.0, reconnect=True, self_deaf=True)
        except BaseException:
            await self.release(guild, run_id, delay=0)
            raise

    async def release(self, guild: discord.Guild, run_id: int, delay: float = 0) -> None:
        gv = self._g(guild.id)
        async with gv.cond:
            gv.holders.discard(run_id)
            empty = not gv.holders
            if empty:
                gv.cond.notify_all()
        if not empty:
            return
        if delay > 0:
            await asyncio.sleep(delay)
        async with gv.connect_lock:
            if gv.holders:
                return  # 待っている間に別のアラームが使い始めた
            vc = guild.voice_client
            if vc is not None:
                try:
                    await vc.disconnect(force=False)
                except Exception:
                    log.exception("VC からの退出に失敗しました")
            gv.channel_id = None

    async def play(self, guild: discord.Guild, source: discord.AudioSource,
                   interrupt: asyncio.Event, max_seconds: float) -> None:
        """1回分を再生する。interrupt か max_seconds で途中終了する。"""
        gv = self._g(guild.id)
        async with gv.play_lock:
            if interrupt.is_set() or max_seconds <= 0:
                source.cleanup()
                return
            vc = guild.voice_client
            if vc is None or not vc.is_connected():
                source.cleanup()
                raise VoiceGone()

            loop = asyncio.get_running_loop()
            done: asyncio.Future = loop.create_future()

            def _finish(err: Exception | None) -> None:
                if not done.done():
                    done.set_result(err)

            vc.play(source, after=lambda err: loop.call_soon_threadsafe(_finish, err))
            waiter = asyncio.ensure_future(interrupt.wait())
            try:
                await asyncio.wait({done, waiter}, timeout=max_seconds,
                                   return_when=asyncio.FIRST_COMPLETED)
            except asyncio.CancelledError:
                vc.stop()
                raise
            finally:
                waiter.cancel()
            if not done.done():
                vc.stop()
                try:
                    await asyncio.wait_for(asyncio.shield(done), timeout=5)
                except asyncio.TimeoutError:
                    pass
            if done.done() and done.result():
                log.warning("再生中にエラーが発生しました: %s", done.result())
