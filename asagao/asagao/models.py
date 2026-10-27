"""アラームの項目定義と入力チェック。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from . import config

DAYS = ("月", "火", "水", "木", "金", "土", "日")

ALARM_COLUMNS = (
    "user_id", "guild_id", "channel_id", "weekday", "start_min", "end_min",
    "group_id", "label", "enabled", "volume", "sound_mode", "play_count",
    "play_interval", "snooze_count", "snooze_interval", "snooze_leave",
    "stop_mode", "join_offset", "require_user_in_vc", "wait_for_user",
    "leave_on_user_leave", "leave_delay",
)
# 「この曜日にコピー」で複製する項目
COPY_FIELDS = tuple(c for c in ALARM_COLUMNS if c not in ("user_id", "weekday", "group_id"))

BOOL_FIELDS = {"enabled", "snooze_leave", "require_user_in_vc", "leave_on_user_leave"}
INT_RANGES = {
    "weekday": (0, 6),
    "start_min": (0, 1439),
    "end_min": (1, 1440),
    "play_count": (0, 20),       # 0 = 止めるまで
    "play_interval": (0, 120),   # 秒
    "snooze_count": (0, 10),
    "snooze_interval": (1, 60),  # 分
    "join_offset": (0, 60),      # 秒
    "wait_for_user": (0, 30),    # 分
    "leave_delay": (0, 60),      # 秒
}
CHOICES = {"sound_mode": ("random", "fixed"), "stop_mode": ("button", "quiz")}
LABELS = {
    "weekday": "曜日", "start_min": "鳴り始め", "end_min": "自動退出",
    "play_count": "再生回数", "play_interval": "再生間隔", "snooze_count": "スヌーズ回数",
    "snooze_interval": "スヌーズ間隔", "join_offset": "入室のタイミング",
    "wait_for_user": "待つ時間", "leave_delay": "抜けるまでの時間", "volume": "音量",
    "label": "名前", "sound_mode": "音声の選び方", "stop_mode": "止め方",
}

DEFAULTS: dict[str, Any] = {
    "label": "",
    "enabled": 1,
    "volume": 60,
    "sound_mode": "random",
    "play_count": 3,
    "play_interval": 5,
    "snooze_count": 0,
    "snooze_interval": 5,
    "snooze_leave": 1,
    "stop_mode": "button",
    "join_offset": 15,
    "require_user_in_vc": 1,
    "wait_for_user": 0,
    "leave_on_user_leave": 1,
    "leave_delay": 3,
}
EDITABLE = set(INT_RANGES) | BOOL_FIELDS | set(CHOICES) | {"volume", "label"}


class ValidationError(ValueError):
    pass


def clean_patch(data: dict[str, Any]) -> dict[str, Any]:
    """ブラウザから来た値を型・範囲チェックして DB 用の dict にする。"""
    out: dict[str, Any] = {}
    for key, value in data.items():
        if key not in EDITABLE:
            continue
        name = LABELS.get(key, key)
        if key in BOOL_FIELDS:
            if not isinstance(value, bool):
                raise ValidationError(f"{key} の値が正しくありません。")
            out[key] = int(value)
        elif key in INT_RANGES:
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValidationError(f"{name}は数字で入力してください。")
            lo, hi = INT_RANGES[key]
            if not lo <= value <= hi:
                raise ValidationError(f"{name}は {lo}〜{hi} の範囲で入力してください。")
            out[key] = value
        elif key in CHOICES:
            if value not in CHOICES[key]:
                raise ValidationError(f"{name}の値が正しくありません。")
            out[key] = value
        elif key == "volume":
            if value not in config.VOLUME_LEVELS:
                raise ValidationError("音量は 20 / 40 / 60 / 80 / 100 から選んでください。")
            out[key] = value
        elif key == "label":
            if not isinstance(value, str):
                raise ValidationError("名前の値が正しくありません。")
            value = value.strip()
            if len(value) > 40:
                raise ValidationError("名前は40文字以内にしてください。")
            out[key] = value
    return out


def check_times(alarm: dict[str, Any]) -> None:
    if alarm["end_min"] <= alarm["start_min"]:
        raise ValidationError("自動退出の時刻は、鳴り始めより後にしてください。")


def fmt_min(minutes: int) -> str:
    """分 → "HH:MM"。1440 は "24:00" と表示する。"""
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


@dataclass
class Occurrence:
    """ある日付に1回鳴る予定。"""
    alarm: dict[str, Any]
    date: date
    start: datetime
    end: datetime
    prep_at: datetime
    test: bool = False
    hashes: list[str] | None = field(default=None)

    @property
    def key(self) -> tuple[int, str]:
        return (self.alarm["id"], self.date.isoformat())
