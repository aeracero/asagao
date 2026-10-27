"""次に鳴るアラームまで眠り、準備時刻になったら起こすスケジューラ。

- DB を定期的に全件チェックするのではなく、「次の準備時刻」まで asyncio で眠る
- 設定が変わったら poke() で起こして計算し直す
- 再起動などで逃した予定は、CATCHUP_WINDOW 以内なら鳴らし直す
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING

from . import config
from .models import Occurrence

if TYPE_CHECKING:
    from .bot import AlarmBot

log = logging.getLogger(__name__)


def now() -> datetime:
    return datetime.now(config.TZ)


def at(day: date, minutes: int) -> datetime:
    return datetime.combine(day, time.min, tzinfo=config.TZ) + timedelta(minutes=minutes)


class Scheduler:
    def __init__(self, bot: "AlarmBot"):
        self.bot = bot
        self._changed = asyncio.Event()

    def poke(self) -> None:
        self._changed.set()

    async def upcoming(self, current: datetime, user_id: int | None = None,
                       future_only: bool = False) -> list[Occurrence]:
        """アラームごとに「次の1回」を求めて、準備時刻の早い順に返す。"""
        db = self.bot.db
        alarms = await db.enabled_alarms(user_id)
        if not alarms:
            return []
        today = current.date()
        days = [today + timedelta(days=i) for i in range(-1, 8)]
        claimed = await db.runs_since(days[0])
        skips = await db.skips_between(days[0], days[-1])

        result: list[Occurrence] = []
        for alarm in alarms:
            for day in days:
                if day.weekday() != alarm["weekday"]:
                    continue
                start = at(day, alarm["start_min"])
                end = at(day, alarm["end_min"])
                if end <= current:
                    continue
                if start < current:
                    if future_only or start < current - timedelta(seconds=config.CATCHUP_WINDOW):
                        continue
                    if alarm["updated_at"] > start.timestamp():
                        continue  # 鳴る時刻を過ぎてから作った・変えたものは今回は鳴らさない
                key = (alarm["id"], day.isoformat())
                if key in claimed or key in skips:
                    continue
                lead = max(config.PREP_LEAD, alarm["join_offset"] + 10)
                result.append(Occurrence(alarm=alarm, date=day, start=start, end=end,
                                         prep_at=start - timedelta(seconds=lead)))
                break
        result.sort(key=lambda o: o.prep_at)
        return result

    async def next_for_user(self, user_id: int) -> Occurrence | None:
        occs = await self.upcoming(now(), user_id=user_id, future_only=True)
        occs.sort(key=lambda o: o.start)
        return occs[0] if occs else None

    async def run(self) -> None:
        await self.bot.wait_until_ready()
        log.info("スケジューラを開始しました")
        while True:
            self._changed.clear()
            try:
                current = now()
                occs = await self.upcoming(current)
                for occ in occs:
                    if occ.prep_at > current:
                        break
                    if await self.bot.db.claim_run(occ.alarm["id"], occ.date):
                        log.info("準備開始: alarm=%s %s", occ.alarm["id"], occ.start.isoformat())
                        self.bot.runs.start(occ)
                waiting = [o for o in occs if o.prep_at > current]
                timeout = (waiting[0].prep_at - now()).total_seconds() if waiting else 3600
            except Exception:
                log.exception("スケジューラでエラーが発生しました")
                timeout = 30
            # 次の準備時刻まで眠る（最長1時間ごとに見直し）
            try:
                await asyncio.wait_for(self._changed.wait(), timeout=min(max(timeout, 0.5), 3600))
            except asyncio.TimeoutError:
                pass

    async def maintenance_loop(self) -> None:
        """1日1回、朝を避けた時間に掃除とバックアップを行う。"""
        await self.bot.wait_until_ready()
        while True:
            current = now()
            target = datetime.combine(current.date(), time(config.MAINTENANCE_HOUR), tzinfo=config.TZ)
            if target <= current:
                target += timedelta(days=1)
            await asyncio.sleep((target - current).total_seconds())
            try:
                await self.maintenance()
            except Exception:
                log.exception("メンテナンスに失敗しました")

    async def maintenance(self) -> None:
        db = self.bot.db
        await db.purge_sessions()
        await db.purge_history(now().date() - timedelta(days=14))
        removed = await self.bot.audio.collect_garbage()
        backup_dir = config.DATA_DIR / "backup"
        backup_dir.mkdir(exist_ok=True)
        await db.backup(backup_dir / f"alarm-{now():%Y%m%d}.db")
        for old in sorted(backup_dir.glob("alarm-*.db"))[:-7]:
            old.unlink(missing_ok=True)
        log.info("メンテナンス完了（未使用の音声 %d 件を削除）", removed)
