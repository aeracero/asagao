"""アラーム1回分の流れ（準備 → 自動入室 → 鳴動 → スヌーズ → 自動退出）。

  T - 60秒   準備開始（音声の変換確認、VC と権限の確認）
  T - 入室オフセット   本人がいれば VC に入る
  T          鳴り始め（箱の上端）
  箱の下端    どんな状態でも必ず止めて退出する
"""
from __future__ import annotations

import asyncio
import itertools
import logging
import random
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import discord

from . import config, models
from .models import Occurrence
from .voice import VoiceGone

if TYPE_CHECKING:
    from .bot import AlarmBot

log = logging.getLogger(__name__)
_ids = itertools.count(1)

FINISH_TEXT = {
    "button": "✅ 止めました。おはようございます！",
    "command": "✅ 止めました。おはようございます！",
    "quiz": "✅ 正解です。おはようございます！",
    "user_left": "✅ VCから抜けたので止めました。",
    "deadline": "⌛ 鳴らす時間が終わったので止めました。",
    "done": "✅ 鳴らし終わりました。",
    "empty": "VCに誰もいなくなったので止めました。",
    "voice_lost": "VCから切断されたので止めました。",
    "shutdown": "botが再起動するため止めました。",
}


def now() -> datetime:
    return datetime.now(config.TZ)


class AlarmRun:
    def __init__(self, bot: "AlarmBot", occ: Occurrence):
        self.bot = bot
        self.occ = occ
        self.alarm = occ.alarm
        self.id = next(_ids)
        self.user_id: int = occ.alarm["user_id"]
        self.guild_id: int = occ.alarm["guild_id"]
        self.channel_id: int = occ.alarm["channel_id"]
        self.state = "preparing"  # preparing / waiting / joining / ringing / snoozing / done
        self.stop_reason: str | None = None
        self.interrupt = asyncio.Event()
        self.user_joined = asyncio.Event()
        self.snooze_requested = False
        self.snoozes_left: int = occ.alarm["snooze_count"]
        self.hashes: list[str] = []
        self.message: discord.Message | None = None
        self.acquired = False
        self._prepared: discord.AudioSource | None = None
        self.task: asyncio.Task | None = None

    # --- 外から呼ばれる操作 -------------------------------------------------
    @property
    def stopped(self) -> bool:
        return self.stop_reason is not None

    def stop(self, reason: str) -> None:
        if self.stop_reason is None:
            self.stop_reason = reason
        self.interrupt.set()

    def request_snooze(self) -> bool:
        if self.state != "ringing" or self.snoozes_left <= 0:
            return False
        self.snooze_requested = True
        self.interrupt.set()
        return True

    # --- 本体 ---------------------------------------------------------------
    async def run(self) -> None:
        a = self.alarm
        guild = self.bot.get_guild(self.guild_id)
        try:
            channel = guild.get_channel(self.channel_id) if guild else None
            if not isinstance(channel, discord.VoiceChannel):
                await self.notify("アラームのVCが見つからなかったため、鳴らせませんでした。"
                                  "設定画面でVCを選び直してください。")
                return
            perms = channel.permissions_for(guild.me)
            if not (perms.connect and perms.speak):
                await self.notify(f"botに「{channel.name}」への接続・発言の権限がないため、鳴らせませんでした。")
                return

            # 準備: 音声の変換が終わっているか確認（未完了なら最優先で変換）
            self.hashes = self.occ.hashes if self.occ.hashes is not None \
                else await self.bot.db.hashes_for_alarm(a["id"])
            await self.bot.audio.ensure_ready(self.hashes, until=self.occ.start - timedelta(seconds=3))

            join_at = self.occ.start - timedelta(seconds=a["join_offset"])
            if not await self._sleep_until(join_at):
                return

            # 本人がVCにいるか
            if a["require_user_in_vc"] and not self._user_in(channel):
                if a["wait_for_user"] <= 0 or not await self._wait_for_user(channel):
                    if not self.stopped:
                        self.stop("absent")
                        await self.notify(
                            f"{self._when()} のアラームは、本人が「{channel.name}」にいなかったので鳴らしませんでした。"
                        )
                    return

            # 自動入室
            self.state = "joining"
            try:
                await self.bot.voice.acquire(guild, channel, self.id, timeout=self._left())
            except asyncio.TimeoutError:
                await self.notify(f"{self._when()} のアラームは、同じサーバーの別のVCでアラームが鳴っていたため鳴らせませんでした。")
                return
            except Exception:
                log.exception("VCへの接続に失敗しました")
                await self.notify(f"{self._when()} のアラームは、VCに接続できなかったため鳴らせませんでした。")
                return
            self.acquired = True

            # 鳴り始めに間に合うよう、再生ソース（FFmpeg）を先に起動しておく
            self._prepared = self._make_source()
            if not await self._sleep_until(self.occ.start):
                return
            if a["require_user_in_vc"] and not self._user_in(channel):
                self.stop("user_left")
                return

            self.state = "ringing"
            await self._post_message(channel)
            await self._ring(guild, channel)
        except asyncio.CancelledError:
            self.stop("shutdown")
            raise
        except VoiceGone:
            self.stop("voice_lost")
        except Exception:
            log.exception("アラームの処理中にエラーが発生しました")
        finally:
            await self._finish(guild)

    async def _ring(self, guild: discord.Guild, channel: discord.VoiceChannel) -> None:
        a = self.alarm
        while True:
            played = 0
            while a["play_count"] == 0 or played < a["play_count"]:
                if self.stopped:
                    return
                if self._left() <= 0:
                    self.stop("deadline")
                    return
                source = self._prepared or self._make_source()
                self._prepared = None
                await self.bot.voice.play(guild, source, self.interrupt, self._left())
                played += 1
                if self.stopped or self.snooze_requested:
                    break
                if a["play_count"] == 0 or played < a["play_count"]:
                    await self._pause(max(a["play_interval"], 0.3))
                    if self.stopped or self.snooze_requested:
                        break
            if self.stopped:
                return

            # 1セット終わり → スヌーズ
            if self.snoozes_left <= 0:
                self.stop("done")
                return
            self.snoozes_left -= 1
            self.snooze_requested = False
            self.interrupt.clear()
            resume_at = now() + timedelta(minutes=a["snooze_interval"])
            if resume_at >= self.occ.end:
                self.stop("deadline")
                return
            self.state = "snoozing"
            await self._edit_message(f"💤 スヌーズ中です。{resume_at:%H:%M} にもう一度鳴らします。", keep_view=True)
            if a["snooze_leave"] and self.acquired:
                await self.bot.voice.release(guild, self.id, delay=0)
                self.acquired = False
            if not await self._sleep_until(resume_at):
                return
            if not self.acquired:
                try:
                    await self.bot.voice.acquire(guild, channel, self.id, timeout=self._left())
                except Exception:
                    self.stop("voice_lost")
                    return
                self.acquired = True
            self.state = "ringing"
            await self._edit_message(self._ring_text(), keep_view=True)

    async def _finish(self, guild: discord.Guild | None) -> None:
        self.state = "done"
        if self._prepared is not None:
            self._prepared.cleanup()
            self._prepared = None
        if self.acquired and guild is not None:
            delay = 0 if self.stop_reason in ("empty", "voice_lost", "shutdown") else self.alarm["leave_delay"]
            try:
                await self.bot.voice.release(guild, self.id, delay=delay)
            except Exception:
                log.exception("VCの解放に失敗しました")
            self.acquired = False
        text = FINISH_TEXT.get(self.stop_reason or "")
        if self.message is not None and text:
            await self._edit_message(text, keep_view=False)
        self.bot.runs.remove(self)

    # --- 待機まわり ---------------------------------------------------------
    def _left(self) -> float:
        """箱の下端（自動退出）までの残り秒数。"""
        return (self.occ.end - now()).total_seconds()

    async def _sleep_until(self, when: datetime) -> bool:
        delay = min((when - now()).total_seconds(), self._left())
        if delay > 0:
            try:
                await asyncio.wait_for(self.interrupt.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
        if self._left() <= 0 and not self.stopped:
            self.stop("deadline")
        return not self.stopped

    async def _pause(self, seconds: float) -> None:
        timeout = min(seconds, self._left())
        if timeout <= 0:
            return
        try:
            await asyncio.wait_for(self.interrupt.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass

    async def _wait_for_user(self, channel: discord.VoiceChannel) -> bool:
        self.state = "waiting"
        deadline = min(self.occ.start + timedelta(minutes=self.alarm["wait_for_user"]), self.occ.end)
        timeout = (deadline - now()).total_seconds()
        if timeout <= 0:
            return False
        self.user_joined.clear()
        if self._user_in(channel):
            return True
        joined = asyncio.ensure_future(self.user_joined.wait())
        stopped = asyncio.ensure_future(self.interrupt.wait())
        try:
            await asyncio.wait({joined, stopped}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        finally:
            joined.cancel()
            stopped.cancel()
        return not self.stopped and self._user_in(channel)

    def _user_in(self, channel: discord.VoiceChannel) -> bool:
        return self.user_id in channel.voice_states

    # --- 音声 ---------------------------------------------------------------
    def _make_source(self) -> discord.AudioSource:
        digest = None
        if self.hashes:
            digest = random.choice(self.hashes) if self.alarm["sound_mode"] == "random" else self.hashes[0]
        return self.bot.audio.make_source(digest, self.alarm["volume"])

    # --- メッセージ ---------------------------------------------------------
    def _when(self) -> str:
        return f"{self.occ.start:%m/%d}（{models.DAYS[self.occ.start.weekday()]}）{self.occ.start:%H:%M}"

    def _ring_text(self) -> str:
        label = self.alarm.get("label") or ""
        head = f"⏰ <@{self.user_id}> おはようございます！"
        return f"{head}「{label}」" if label else head

    async def _post_message(self, channel: discord.VoiceChannel) -> None:
        view = RingView(self)
        allowed = discord.AllowedMentions(users=True, roles=False, everyone=False)
        try:
            self.message = await channel.send(self._ring_text(), view=view, allowed_mentions=allowed)
            return
        except discord.HTTPException:
            pass
        try:  # VCのチャットに書けない時は DM で
            user = await self.bot.fetch_user(self.user_id)
            self.message = await user.send(self._ring_text(), view=view)
        except discord.HTTPException:
            log.info("停止ボタンを送れませんでした（/stop で止められます）")

    async def _edit_message(self, text: str, keep_view: bool) -> None:
        if self.message is None:
            return
        try:
            await self.message.edit(content=text, view=RingView(self) if keep_view else None,
                                    allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            pass

    async def notify(self, text: str) -> None:
        if self.occ.test:
            return
        await self.bot.notify_user(self.user_id, text)


# --- 停止ボタン・計算問題 ----------------------------------------------------
class RingView(discord.ui.View):
    def __init__(self, run: AlarmRun):
        super().__init__(timeout=None)
        self.run = run
        if run.snoozes_left <= 0 or run.state == "snoozing":
            self.remove_item(self.snooze_button)

    async def _owner(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.run.user_id:
            await interaction.response.send_message("このアラームを操作できるのは本人だけです。", ephemeral=True)
            return False
        if self.run.state == "done":
            await interaction.response.send_message("このアラームはもう終わっています。", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="止める", emoji="⏹️", style=discord.ButtonStyle.danger)
    async def stop_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self._owner(interaction):
            return
        if self.run.alarm["stop_mode"] == "quiz":
            await interaction.response.send_modal(QuizModal([self.run]))
            return
        self.run.stop("button")
        await interaction.response.send_message("止めました。おはようございます！", ephemeral=True)

    @discord.ui.button(label="スヌーズ", emoji="💤", style=discord.ButtonStyle.secondary)
    async def snooze_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self._owner(interaction):
            return
        if self.run.request_snooze():
            await interaction.response.send_message(
                f"{self.run.alarm['snooze_interval']}分後にもう一度鳴らします。", ephemeral=True)
        else:
            await interaction.response.send_message("スヌーズはもう使えません。", ephemeral=True)


class QuizModal(discord.ui.Modal, title="計算問題に正解すると止まります"):
    def __init__(self, runs: list[AlarmRun]):
        super().__init__(timeout=180)
        self.runs = runs
        a, b, c = random.randint(12, 49), random.randint(3, 9), random.randint(10, 99)
        self.expected = a * b + c
        self.answer = discord.ui.TextInput(label=f"{a} × {b} + {c} = ?", max_length=6, placeholder="答えを数字で")
        self.add_item(self.answer)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        text = self.answer.value.strip().translate(str.maketrans("０１２３４５６７８９", "0123456789"))
        if text.isdigit() and int(text) == self.expected:
            for run in self.runs:
                run.stop("quiz")
            await interaction.response.send_message("正解です。止めました！", ephemeral=True)
        else:
            await interaction.response.send_message(
                "不正解です。もう一度「止める」を押して、新しい問題に答えてください。", ephemeral=True)


# --- 実行中のアラームの管理 --------------------------------------------------
class RunManager:
    def __init__(self, bot: "AlarmBot"):
        self.bot = bot
        self.runs: dict[int, AlarmRun] = {}

    def start(self, occ: Occurrence) -> AlarmRun:
        run = AlarmRun(self.bot, occ)
        self.runs[run.id] = run
        run.task = asyncio.create_task(run.run(), name=f"alarm-{occ.alarm['id']}-{run.id}")
        return run

    def remove(self, run: AlarmRun) -> None:
        self.runs.pop(run.id, None)

    def for_user(self, user_id: int, states=("ringing", "snoozing")) -> list[AlarmRun]:
        return [r for r in self.runs.values() if r.user_id == user_id and r.state in states]

    def stop_all(self, reason: str) -> None:
        for run in list(self.runs.values()):
            run.stop(reason)

    async def start_test(self, user_id: int, channel: discord.VoiceChannel,
                         alarm: dict | None = None) -> tuple[bool, str]:
        if any(r.user_id == user_id and r.occ.test for r in self.runs.values()):
            return False, "テストはすでに実行中です。"
        base = dict(models.DEFAULTS)
        if alarm:
            base.update(alarm)
            hashes = await self.bot.db.hashes_for_alarm(alarm["id"])
        else:
            hashes = await self.bot.db.hashes_for_user(user_id)
        base.update(
            id=0, user_id=user_id, guild_id=channel.guild.id, channel_id=channel.id,
            label="テスト", play_count=1, snooze_count=0, join_offset=0,
            require_user_in_vc=0, wait_for_user=0, leave_delay=1, stop_mode="button",
        )
        t = now()
        self.start(Occurrence(alarm=base, date=t.date(), start=t + timedelta(seconds=2),
                              end=t + timedelta(seconds=120), prep_at=t, test=True, hashes=hashes))
        return True, "テストで1回鳴らします。"

    def on_voice_state_update(self, member: discord.Member,
                              before: discord.VoiceState, after: discord.VoiceState) -> None:
        bot_id = self.bot.user.id if self.bot.user else None
        before_id = before.channel.id if before.channel else None
        after_id = after.channel.id if after.channel else None

        for run in list(self.runs.values()):
            if member.id == run.user_id:
                if after_id == run.channel_id:
                    run.user_joined.set()
                left = before_id == run.channel_id and after_id != run.channel_id
                if left and run.alarm["leave_on_user_leave"] and run.state in ("ringing", "snoozing"):
                    run.stop("user_left")  # VCから抜ける＝起きた
            if member.id == bot_id and before_id == run.channel_id and after_id is None \
                    and run.state == "ringing":
                run.stop("voice_lost")  # 外部から切断された

        # VCに bot 以外いなくなったら止める
        channel = before.channel
        if channel is not None and before_id != after_id:
            others = [uid for uid in channel.voice_states if uid != bot_id]
            if not others:
                for run in list(self.runs.values()):
                    if run.channel_id == channel.id and run.state == "ringing":
                        run.stop("empty")
