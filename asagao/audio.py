"""音声ファイルの保存・変換・再生ソースの用意。

Volume 上の配置:
  sounds/original/{hash}{ext}          アップロードされた元ファイル
  sounds/converted/{hash}/v{level}.ogg 正規化済み Opus（音量5段階）
  sounds/default.ogg                   音声未登録時のビープ音
  tmp/                                 変換途中のファイル（同じ Volume 内なので移動が一瞬）
"""
from __future__ import annotations

import asyncio
import hashlib
import itertools
import json
import logging
import os
import shutil
import uuid
from datetime import datetime
from pathlib import Path

import discord

from . import config
from .db import Database

log = logging.getLogger(__name__)

LOUDNORM = "loudnorm=I=-16:TP=-1.5:LRA=11"


class UploadError(Exception):
    pass


class AudioStore:
    def __init__(self, db: Database):
        self.db = db
        root = config.DATA_DIR
        self.orig_dir = root / "sounds" / "original"
        self.conv_dir = root / "sounds" / "converted"
        self.tmp_dir = root / "tmp"
        self.default_path = root / "sounds" / "default.ogg"
        self._queue: asyncio.PriorityQueue[tuple[int, int, str]] = asyncio.PriorityQueue()
        self._seq = itertools.count()
        self._pending: set[str] = set()
        self._waiters: dict[str, list[asyncio.Future]] = {}
        self._worker: asyncio.Task | None = None

    # --- 起動時 -------------------------------------------------------------
    async def start(self) -> None:
        for d in (self.orig_dir, self.conv_dir):
            d.mkdir(parents=True, exist_ok=True)
        # 変換中に再起動した場合のゴミを掃除
        shutil.rmtree(self.tmp_dir, ignore_errors=True)
        self.tmp_dir.mkdir(parents=True, exist_ok=True)

        if not self.default_path.exists():
            await self._make_default_sound()

        # DB 上は変換済みなのにファイルがないもの → 変換し直す
        for row in await self.db.all_sound_files():
            if row["status"] == "failed":
                continue
            if not self.levels_exist(row["hash"]):
                if row["status"] == "ready":
                    await self.db.set_sound_file_status(row["hash"], "pending")
                self.enqueue(row["hash"], priority=10)
        self._worker = asyncio.create_task(self._run_worker(), name="audio-worker")

    async def _make_default_sound(self) -> None:
        # 0.5秒おきに2回「ピピッ」、1秒休み、を繰り返す4秒の音
        expr = "0.5*sin(2*PI*880*t)*lt(mod(t\\,0.5)\\,0.18)*lt(mod(t\\,2)\\,1)"
        tmp = self.tmp_dir / "default.ogg"
        code, err = await _run(
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-f", "lavfi", "-i", f"aevalsrc={expr}:s=48000:d=4",
            "-c:a", "libopus", "-b:a", "48k", "-ac", "2", str(tmp),
        )
        if code == 0:
            os.replace(tmp, self.default_path)
        else:
            log.error("デフォルト音声の生成に失敗しました: %s", err)

    # --- パス ---------------------------------------------------------------
    def converted_path(self, digest: str, level: int) -> Path:
        return self.conv_dir / digest / f"v{level}.ogg"

    def original_path(self, digest: str) -> Path | None:
        matches = list(self.orig_dir.glob(f"{digest}.*"))
        return matches[0] if matches else None

    def levels_exist(self, digest: str) -> bool:
        return all(self.converted_path(digest, lv).exists() for lv in config.VOLUME_LEVELS)

    # --- アップロード -------------------------------------------------------
    async def save_upload(self, part) -> tuple[str, str, Path, str]:
        """multipart のファイル部分をストリームで tmp に書き出す（メモリに全体を載せない）。"""
        filename = part.filename or "sound"
        ext = Path(filename).suffix.lower()
        if ext not in config.ALLOWED_EXTS:
            raise UploadError("この形式には対応していません。mp3 / wav / ogg / m4a / flac などを使ってください。")
        tmp = self.tmp_dir / f"up-{uuid.uuid4().hex}{ext}"
        hasher = hashlib.sha256()
        size = 0
        try:
            with open(tmp, "wb") as f:
                while chunk := await part.read_chunk(64 * 1024):
                    size += len(chunk)
                    if size > config.MAX_UPLOAD_BYTES:
                        raise UploadError("ファイルは5MBまでにしてください。")
                    hasher.update(chunk)
                    f.write(chunk)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        if size == 0:
            tmp.unlink(missing_ok=True)
            raise UploadError("ファイルが空です。")
        return hasher.hexdigest(), ext, tmp, filename

    async def ingest(self, digest: str, ext: str, tmp: Path) -> dict:
        """元ファイルを保存して変換キューに入れる。同じ中身なら使い回す。"""
        existing = await self.db.get_sound_file(digest)
        if existing and existing["status"] != "failed":
            tmp.unlink(missing_ok=True)
            return existing
        duration = await self.probe(tmp)
        if duration is None:
            tmp.unlink(missing_ok=True)
            raise UploadError("音声ファイルとして読み込めませんでした。")
        if duration > config.MAX_SOUND_SECONDS:
            tmp.unlink(missing_ok=True)
            raise UploadError("音声は3分以内にしてください。")
        old = self.original_path(digest)
        if old:
            old.unlink(missing_ok=True)
        os.replace(tmp, self.orig_dir / f"{digest}{ext}")
        await self.db.upsert_sound_file(digest, ext, duration, "pending")
        self.enqueue(digest, priority=10)
        return await self.db.get_sound_file(digest)

    async def probe(self, path: Path) -> float | None:
        code, out = await _run(
            "ffprobe", "-v", "error", "-select_streams", "a:0",
            "-show_entries", "stream=codec_type:format=duration", "-of", "json", str(path),
            capture="stdout",
        )
        if code != 0:
            return None
        try:
            data = json.loads(out or "{}")
        except json.JSONDecodeError:
            return None
        if not data.get("streams"):
            return None
        try:
            return float(data.get("format", {}).get("duration") or 0)
        except ValueError:
            return 0.0

    # --- 変換キュー ---------------------------------------------------------
    def enqueue(self, digest: str, priority: int = 10) -> None:
        """priority が小さいほど先に処理する。アラーム直前の分は 0 で割り込む。"""
        self._pending.add(digest)
        self._queue.put_nowait((priority, next(self._seq), digest))

    async def ensure_ready(self, hashes: list[str], until: datetime) -> None:
        """アラーム準備時: 未変換があれば最優先で変換し、until まで待つ。"""
        missing = [h for h in dict.fromkeys(hashes) if not self.levels_exist(h)]
        if not missing:
            return
        loop = asyncio.get_running_loop()
        futures = []
        for digest in missing:
            self.enqueue(digest, priority=0)
            fut = loop.create_future()
            self._waiters.setdefault(digest, []).append(fut)
            futures.append(fut)
        timeout = (until - datetime.now(config.TZ)).total_seconds()
        if timeout > 0:
            await asyncio.wait(futures, timeout=timeout)

    async def _run_worker(self) -> None:
        # 同時に1件ずつだけ変換する（CPU・RAM の山を作らない）
        while True:
            _, _, digest = await self._queue.get()
            if digest not in self._pending:
                continue  # 優先度を上げた時の重複エントリ
            try:
                ok = await self._convert(digest)
            except Exception:
                log.exception("変換中にエラーが発生しました: %s", digest)
                ok = False
            self._pending.discard(digest)
            await self.db.set_sound_file_status(digest, "ready" if ok else "failed")
            for fut in self._waiters.pop(digest, []):
                if not fut.done():
                    fut.set_result(ok)

    async def _convert(self, digest: str) -> bool:
        if self.levels_exist(digest):
            return True
        src = self.original_path(digest)
        if src is None:
            log.warning("元ファイルが見つかりません: %s", digest)
            return False

        work = self.tmp_dir / f"conv-{digest[:12]}-{uuid.uuid4().hex[:6]}"
        work.mkdir(parents=True)
        levels = config.VOLUME_LEVELS
        n = len(levels)
        # 1回のデコード・1回の正規化から、5段階の音量をまとめて書き出す
        graph = (
            f"[0:a]{LOUDNORM},aresample=48000,asplit={n}"
            + "".join(f"[a{i}]" for i in range(n))
            + ";"
            + ";".join(f"[a{i}]volume={lv / 100:.2f}[o{i}]" for i, lv in enumerate(levels))
        )
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-threads", "1", "-filter_complex_threads", "1",
            "-i", str(src), "-filter_complex", graph,
        ]
        for i, lv in enumerate(levels):
            cmd += ["-map", f"[o{i}]", "-c:a", "libopus", "-b:a", "64k", "-ac", "2",
                    str(work / f"v{lv}.ogg")]
        code, err = await _run(*cmd)
        if code != 0:
            log.warning("ffmpeg の変換に失敗しました (%s): %s", digest, err[-500:])
            shutil.rmtree(work, ignore_errors=True)
            return False

        dest = self.conv_dir / digest
        dest.mkdir(parents=True, exist_ok=True)
        for lv in levels:
            os.replace(work / f"v{lv}.ogg", dest / f"v{lv}.ogg")  # 完成品だけを置く
        shutil.rmtree(work, ignore_errors=True)
        log.info("変換しました: %s", digest[:12])
        return True

    # --- 再生 ---------------------------------------------------------------
    def make_source(self, digest: str | None, volume: int) -> discord.AudioSource:
        if digest:
            converted = self.converted_path(digest, volume)
            if converted.exists():
                # 変換済み Opus はそのまま送るだけ（再エンコードなし＝CPU ほぼゼロ）
                return discord.FFmpegOpusAudio(str(converted), codec="copy", options="-loglevel error")
            original = self.original_path(digest)
            if original is not None:
                # 変換が間に合わなかった時だけ、その場で変換しながら鳴らす
                return discord.FFmpegOpusAudio(
                    str(original), bitrate=64,
                    options=f"-af {LOUDNORM},volume={volume / 100:.2f}",
                )
        return discord.FFmpegOpusAudio(str(self.default_path), codec="copy", options="-loglevel error")

    # --- 掃除 ---------------------------------------------------------------
    async def collect_garbage(self) -> int:
        """どのユーザーからも参照されなくなった音声を消す。"""
        removed = 0
        for row in await self.db.unreferenced_sound_files():
            digest = row["hash"]
            if digest in self._pending:
                continue
            original = self.original_path(digest)
            if original:
                original.unlink(missing_ok=True)
            shutil.rmtree(self.conv_dir / digest, ignore_errors=True)
            await self.db.delete_sound_file(digest)
            removed += 1
        return removed


async def _run(*cmd: str, capture: str = "stderr") -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE if capture == "stdout" else asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE if capture == "stderr" else asyncio.subprocess.DEVNULL,
    )
    out, err = await proc.communicate()
    data = out if capture == "stdout" else err
    return proc.returncode, (data or b"").decode("utf-8", "replace")
