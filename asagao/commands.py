"""スラッシュコマンド。細かい設定は Web ダッシュボードで行う。"""
from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands

from . import config
from .models import DAYS, fmt_min
from .runner import QuizModal

if TYPE_CHECKING:
    from .bot import AlarmBot


def setup_commands(bot: "AlarmBot") -> None:
    tree = bot.tree
    group = app_commands.Group(name="alarm", description="目覚ましアラームの操作", guild_only=True)

    @group.command(name="dashboard", description="設定画面のURLを表示します")
    async def dashboard(interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            f"設定画面はこちらです。Discordアカウントでログインしてください。\n{config.PUBLIC_URL}",
            ephemeral=True,
        )

    @group.command(name="list", description="自分のアラーム一覧を表示します")
    async def list_alarms(interaction: discord.Interaction) -> None:
        uid = interaction.user.id
        alarms = await bot.db.list_alarms(uid)
        if not alarms:
            await interaction.response.send_message(
                f"まだアラームがありません。設定画面から追加してください。\n{config.PUBLIC_URL}",
                ephemeral=True,
            )
            return
        lines = []
        user = await bot.db.get_user(uid)
        if user and user["paused"]:
            lines.append("⏸ いまは全アラームを一時停止しています（`/alarm resume` で再開）。")
        nxt = await bot.scheduler.next_for_user(uid)
        if nxt:
            lines.append(f"次に鳴るのは **{nxt.start:%m/%d}（{DAYS[nxt.start.weekday()]}）{nxt.start:%H:%M}** です。")
        lines.append("")
        for a in alarms:
            mark = "✅" if a["enabled"] else "⏸"
            where = f"<#{a['channel_id']}>" if a["channel_id"] else "（VC未設定）"
            label = f" {a['label']}" if a["label"] else ""
            lines.append(f"{mark} {DAYS[a['weekday']]} {fmt_min(a['start_min'])}〜{fmt_min(a['end_min'])} {where}{label}")
        text = "\n".join(lines)
        if len(text) > 1900:
            text = text[:1900] + "\n…（続きは設定画面で確認してください）"
        await interaction.response.send_message(text, ephemeral=True)

    @group.command(name="skip", description="次に鳴る予定を1回だけスキップします")
    async def skip(interaction: discord.Interaction) -> None:
        occ = await bot.scheduler.next_for_user(interaction.user.id)
        if occ is None:
            await interaction.response.send_message("スキップできる予定がありません。", ephemeral=True)
            return
        await bot.db.add_override(occ.alarm["id"], occ.date, "skip")
        bot.scheduler.poke()
        await interaction.response.send_message(
            f"{occ.start:%m/%d}（{DAYS[occ.start.weekday()]}）{occ.start:%H:%M} のアラームをスキップしました。",
            ephemeral=True,
        )

    @group.command(name="pause", description="すべてのアラームを一時停止します（旅行中などに）")
    async def pause(interaction: discord.Interaction) -> None:
        await bot.db.set_paused(interaction.user.id, True)
        bot.scheduler.poke()
        await interaction.response.send_message(
            "すべてのアラームを一時停止しました。`/alarm resume` で再開できます。", ephemeral=True)

    @group.command(name="resume", description="一時停止したアラームを再開します")
    async def resume(interaction: discord.Interaction) -> None:
        await bot.db.set_paused(interaction.user.id, False)
        bot.scheduler.poke()
        await interaction.response.send_message("アラームを再開しました。", ephemeral=True)

    @group.command(name="test", description="今いるVCで、自分の音声を1回鳴らしてみます")
    async def test(interaction: discord.Interaction) -> None:
        member = interaction.user
        voice = getattr(member, "voice", None)
        if voice is None or not isinstance(voice.channel, discord.VoiceChannel):
            await interaction.response.send_message("VCに入ってから実行してください。", ephemeral=True)
            return
        _, message = await bot.runs.start_test(member.id, voice.channel)
        await interaction.response.send_message(message, ephemeral=True)

    tree.add_command(group)

    @tree.command(name="stop", description="鳴っているアラームを止めます")
    @app_commands.guild_only()
    async def stop(interaction: discord.Interaction) -> None:
        runs = bot.runs.for_user(interaction.user.id)
        if not runs:
            await interaction.response.send_message("いま鳴っているアラームはありません。", ephemeral=True)
            return
        if any(r.alarm["stop_mode"] == "quiz" for r in runs):
            await interaction.response.send_modal(QuizModal(runs))
            return
        for run in runs:
            run.stop("command")
        await interaction.response.send_message("止めました。おはようございます！", ephemeral=True)
