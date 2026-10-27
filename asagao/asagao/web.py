"""Web ダッシュボード（bot と同じプロセスで動かす aiohttp サーバー）。

- Discord OAuth2 でログイン（スコープは identify と guilds だけ）
- セッションは SQLite に保存し、Cookie にはランダムなIDだけを入れる
- すべての操作で「本人のアラーム・音声か」をサーバー側で確認する
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
import time
import uuid
from collections import defaultdict, deque
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

import discord
from aiohttp import web

from . import config, models
from .audio import UploadError
from .models import ValidationError

if TYPE_CHECKING:
    from .bot import AlarmBot

log = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"
DISCORD_API = "https://discord.com/api/v10"
BOT = web.AppKey("bot", object)
AUDIO_TYPES = {
    ".ogg": "audio/ogg", ".opus": "audio/ogg", ".mp3": "audio/mpeg", ".wav": "audio/wav",
    ".m4a": "audio/mp4", ".aac": "audio/aac", ".flac": "audio/flac", ".webm": "audio/webm",
}


# --- 共通 --------------------------------------------------------------------
def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def jerr(status: int, message: str) -> web.Response:
    return web.json_response({"error": message}, status=status)


def redirect(location: str) -> web.Response:
    return web.Response(status=302, headers={"Location": location})


def _bot(request: web.Request) -> "AlarmBot":
    return request.app[BOT]


def _uid(request: web.Request) -> int:
    return request["session"]["user_id"]


def _same_origin(request: web.Request) -> bool:
    origin = request.headers.get("Origin")
    if origin:
        return origin.rstrip("/") == config.PUBLIC_URL
    referer = request.headers.get("Referer", "")
    return referer.startswith(config.PUBLIC_URL + "/")


async def _json(request: web.Request) -> dict[str, Any]:
    try:
        data = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ValidationError("リクエストの形式が正しくありません。")
    if not isinstance(data, dict):
        raise ValidationError("リクエストの形式が正しくありません。")
    return data


def _to_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@web.middleware
async def session_middleware(request: web.Request, handler):
    request["session"] = None
    token = request.cookies.get("sid")
    if token:
        request["session"] = await _bot(request).db.get_session(_hash(token))
    if request.path.startswith("/api/"):
        if request["session"] is None:
            return jerr(401, "ログインが必要です。")
        if request.method not in ("GET", "HEAD") and not _same_origin(request):
            return jerr(403, "このリクエストは受け付けられません。")
    try:
        return await handler(request)
    except ValidationError as e:
        return jerr(400, str(e))
    except UploadError as e:
        return jerr(400, str(e))


async def _security_headers(request: web.Request, response: web.StreamResponse) -> None:
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")


# --- 出力の整形（Discord の ID は JS で桁落ちするので文字列にする） ------------
def alarm_out(a: dict, sound_ids: list[int]) -> dict:
    out = {k: a[k] for k in models.ALARM_COLUMNS if k not in ("user_id",)}
    out["id"] = a["id"]
    out["guild_id"] = str(a["guild_id"]) if a["guild_id"] else None
    out["channel_id"] = str(a["channel_id"]) if a["channel_id"] else None
    for key in models.BOOL_FIELDS:
        out[key] = bool(a[key])
    out["sound_ids"] = sound_ids
    return out


def sound_out(s: dict) -> dict:
    return {"id": s["id"], "name": s["name"], "duration": round(s["duration"] or 0, 1), "status": s["status"]}


async def _alarms_out(bot: "AlarmBot", alarms: list[dict]) -> list[dict]:
    sids = await bot.db.sound_ids_for_alarms([a["id"] for a in alarms])
    return [alarm_out(a, sids[a["id"]]) for a in alarms]


# --- 権限まわり ----------------------------------------------------------------
async def visible_guilds(bot: "AlarmBot", user_id: int, guild_ids: list[str]) -> list[dict]:
    """bot と本人が両方いるサーバーと、本人が入れる VC の一覧。"""
    result = []
    for gid in guild_ids:
        guild = bot.get_guild(int(gid))
        if guild is None:
            continue
        member = await bot.get_member_cached(guild, user_id)
        if member is None:
            continue
        channels = []
        for ch in sorted(guild.voice_channels, key=lambda c: (c.position, c.id)):
            perms = ch.permissions_for(member)
            if perms.view_channel and perms.connect:
                channels.append({"id": str(ch.id), "name": ch.name})
        if channels:
            result.append({
                "id": str(guild.id), "name": guild.name,
                "icon": guild.icon.with_size(64).url if guild.icon else None,
                "channels": channels,
            })
    result.sort(key=lambda g: g["name"])
    return result


async def check_channel(bot: "AlarmBot", user_id: int, channel_id: Any) -> tuple[int, int]:
    """本人がそのVCを見られて、接続できるかを確認する。"""
    cid = _to_int(channel_id)
    channel = bot.get_channel(cid) if cid else None
    if not isinstance(channel, discord.VoiceChannel):
        raise ValidationError("VCが見つかりません。")
    member = await bot.get_member_cached(channel.guild, user_id)
    if member is None:
        raise ValidationError("このサーバーのメンバーではありません。")
    perms = channel.permissions_for(member)
    if not (perms.view_channel and perms.connect):
        raise ValidationError("このVCに接続する権限がありません。")
    return channel.guild.id, channel.id


async def _own_alarm(request: web.Request) -> dict:
    alarm_id = _to_int(request.match_info["id"])
    alarm = await _bot(request).db.get_alarm(alarm_id) if alarm_id else None
    if alarm is None or alarm["user_id"] != _uid(request):
        raise web.HTTPNotFound(text=json.dumps({"error": "アラームが見つかりません。"}),
                               content_type="application/json")
    return alarm


async def _own_sound(request: web.Request) -> dict:
    sound_id = _to_int(request.match_info["id"])
    sound = await _bot(request).db.get_sound(sound_id) if sound_id else None
    if sound is None or sound["user_id"] != _uid(request):
        raise web.HTTPNotFound(text=json.dumps({"error": "音声が見つかりません。"}),
                               content_type="application/json")
    return sound


async def _check_sound_ids(bot: "AlarmBot", user_id: int, value: Any) -> list[int]:
    if not isinstance(value, list) or not all(isinstance(v, int) for v in value):
        raise ValidationError("音声の指定が正しくありません。")
    owned = {s["id"] for s in await bot.db.list_sounds(user_id)}
    ids = list(dict.fromkeys(value))
    if any(i not in owned for i in ids):
        raise ValidationError("自分の音声だけを選べます。")
    return ids


# --- ページ・ログイン ----------------------------------------------------------
async def index(request: web.Request) -> web.StreamResponse:
    return web.FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


async def healthz(request: web.Request) -> web.Response:
    return web.Response(text="ok")


async def login(request: web.Request) -> web.Response:
    state = secrets.token_urlsafe(16)
    params = {
        "client_id": config.CLIENT_ID,
        "redirect_uri": config.REDIRECT_URI,
        "response_type": "code",
        "scope": "identify guilds",
        "state": state,
        "prompt": "none",
    }
    resp = redirect(f"https://discord.com/oauth2/authorize?{urlencode(params)}")
    resp.set_cookie("oauth_state", state, max_age=600, httponly=True,
                    secure=config.COOKIE_SECURE, samesite="Lax", path="/")
    return resp


async def callback(request: web.Request) -> web.Response:
    bot = _bot(request)
    q = request.query
    if q.get("error"):
        return redirect("/?login_error=denied")
    state = q.get("state")
    if not state or state != request.cookies.get("oauth_state") or not q.get("code"):
        return redirect("/?login_error=state")

    http = bot.http_session
    try:
        async with http.post(f"{DISCORD_API}/oauth2/token", data={
            "client_id": config.CLIENT_ID,
            "client_secret": config.CLIENT_SECRET,
            "grant_type": "authorization_code",
            "code": q["code"],
            "redirect_uri": config.REDIRECT_URI,
        }) as r:
            if r.status != 200:
                log.warning("トークン交換に失敗しました: %s %s", r.status, await r.text())
                return redirect("/?login_error=token")
            token = await r.json()
        headers = {"Authorization": f"Bearer {token['access_token']}"}
        async with http.get(f"{DISCORD_API}/users/@me", headers=headers) as r:
            user = await r.json()
        async with http.get(f"{DISCORD_API}/users/@me/guilds", headers=headers) as r:
            guilds = await r.json() if r.status == 200 else []
        # アクセストークンはもう使わないので破棄する（保存しない）
        async with http.post(f"{DISCORD_API}/oauth2/token/revoke", data={
            "client_id": config.CLIENT_ID, "client_secret": config.CLIENT_SECRET,
            "token": token["access_token"], "token_type_hint": "access_token",
        }):
            pass
    except (asyncio.TimeoutError, KeyError, ValueError) as e:
        log.warning("OAuth2 の処理に失敗しました: %r", e)
        return redirect("/?login_error=token")

    uid = int(user["id"])
    await bot.db.upsert_user(uid, user.get("global_name") or user.get("username"), user.get("avatar"))
    sid = secrets.token_urlsafe(32)
    guild_ids = json.dumps([g["id"] for g in guilds if isinstance(g, dict) and "id" in g])
    await bot.db.create_session(_hash(sid), uid, guild_ids, int(time.time()) + config.SESSION_DAYS * 86400)

    resp = redirect("/")
    resp.set_cookie("sid", sid, max_age=config.SESSION_DAYS * 86400, httponly=True,
                    secure=config.COOKIE_SECURE, samesite="Lax", path="/")
    resp.del_cookie("oauth_state", path="/")
    return resp


async def logout(request: web.Request) -> web.Response:
    if not _same_origin(request):
        return jerr(403, "このリクエストは受け付けられません。")
    token = request.cookies.get("sid")
    if token:
        await _bot(request).db.delete_session(_hash(token))
    resp = web.json_response({"ok": True})
    resp.del_cookie("sid", path="/")
    return resp


# --- API: 初期データ ------------------------------------------------------------
async def bootstrap(request: web.Request) -> web.Response:
    bot = _bot(request)
    uid = _uid(request)
    session = request["session"]
    user = await bot.db.get_user(uid) or {}
    guilds = await visible_guilds(bot, uid, json.loads(session["guild_ids"]))
    alarms = await _alarms_out(bot, await bot.db.list_alarms(uid))
    sounds = [sound_out(s) for s in await bot.db.list_sounds(uid)]
    others = await bot.db.others_alarms(uid, [int(g["id"]) for g in guilds])
    avatar = user.get("avatar")
    return web.json_response({
        "me": {
            "id": str(uid),
            "name": user.get("username") or "you",
            "avatar_url": f"https://cdn.discordapp.com/avatars/{uid}/{avatar}.png?size=64" if avatar else None,
            "paused": bool(user.get("paused")),
        },
        "guilds": guilds,
        "alarms": alarms,
        "sounds": sounds,
        "others": [{
            "weekday": o["weekday"], "start_min": o["start_min"], "end_min": o["end_min"],
            "join_offset": o["join_offset"], "guild_id": str(o["guild_id"]),
            "channel_id": str(o["channel_id"]),
        } for o in others],
        "defaults": models.DEFAULTS,
        "volume_levels": list(config.VOLUME_LEVELS),
        "limits": {
            "alarms": config.MAX_ALARMS_PER_USER, "sounds": config.MAX_SOUNDS_PER_USER,
            "upload_mb": config.MAX_UPLOAD_BYTES // (1024 * 1024),
            "sound_seconds": config.MAX_SOUND_SECONDS,
        },
    })


# --- API: アラーム --------------------------------------------------------------
async def create_alarm(request: web.Request) -> web.Response:
    bot = _bot(request)
    uid = _uid(request)
    data = await _json(request)
    if await bot.db.count_alarms(uid) >= config.MAX_ALARMS_PER_USER:
        raise ValidationError(f"アラームは{config.MAX_ALARMS_PER_USER}個までです。")
    for key in ("weekday", "start_min", "end_min"):
        if key not in data:
            raise ValidationError("曜日と時刻を指定してください。")
    fields = dict(models.DEFAULTS)
    fields.update(models.clean_patch(data))
    models.check_times(fields)

    guild_id = channel_id = None
    wanted = data.get("channel_id") or await bot.db.last_channel(uid)
    if wanted:
        try:
            guild_id, channel_id = await check_channel(bot, uid, wanted)
        except ValidationError:
            if data.get("channel_id"):
                raise
    fields.update(user_id=uid, guild_id=guild_id, channel_id=channel_id, group_id=None)
    alarm_id = await bot.db.create_alarm(fields)
    # 最初は自分の音声をすべて選んだ状態にする（なければビープ音）
    await bot.db.set_alarm_sounds(alarm_id, [s["id"] for s in await bot.db.list_sounds(uid)])
    bot.scheduler.poke()
    alarm = await bot.db.get_alarm(alarm_id)
    return web.json_response({"alarm": (await _alarms_out(bot, [alarm]))[0]})


async def update_alarm(request: web.Request) -> web.Response:
    bot = _bot(request)
    uid = _uid(request)
    alarm = await _own_alarm(request)
    data = await _json(request)
    patch = models.clean_patch(data)
    if "channel_id" in data:
        if data["channel_id"]:
            gid, cid = await check_channel(bot, uid, data["channel_id"])
            patch.update(guild_id=gid, channel_id=cid)
        else:
            patch.update(guild_id=None, channel_id=None)
    sound_ids = await _check_sound_ids(bot, uid, data["sound_ids"]) if "sound_ids" in data else None

    targets = [alarm]
    if data.get("apply_group") is True and alarm["group_id"]:
        targets = await bot.db.group_alarms(uid, alarm["group_id"])
    planned = []
    for target in targets:
        p = dict(patch)
        if target["id"] != alarm["id"]:
            p.pop("weekday", None)  # 曜日だけはそれぞれのまま
        models.check_times({**target, **p})
        planned.append((target["id"], p))
    for target_id, p in planned:
        await bot.db.update_alarm(target_id, p)
        if sound_ids is not None:
            await bot.db.set_alarm_sounds(target_id, sound_ids)
    bot.scheduler.poke()
    updated = [await bot.db.get_alarm(t) for t, _ in planned]
    return web.json_response({"alarms": await _alarms_out(bot, updated)})


async def delete_alarm(request: web.Request) -> web.Response:
    bot = _bot(request)
    alarm = await _own_alarm(request)
    deleted = [alarm["id"]]
    if request.query.get("group") == "1" and alarm["group_id"]:
        group = await bot.db.group_alarms(_uid(request), alarm["group_id"])
        deleted = [a["id"] for a in group]
    for alarm_id in deleted:
        await bot.db.delete_alarm(alarm_id)
    bot.scheduler.poke()
    return web.json_response({"deleted": deleted})


async def copy_alarm(request: web.Request) -> web.Response:
    bot = _bot(request)
    uid = _uid(request)
    alarm = await _own_alarm(request)
    data = await _json(request)
    days = data.get("weekdays")
    if not isinstance(days, list) or not all(isinstance(d, int) and 0 <= d <= 6 for d in days):
        raise ValidationError("コピー先の曜日を選んでください。")

    group_id = alarm["group_id"] or uuid.uuid4().hex[:12]
    if not alarm["group_id"]:
        await bot.db.update_alarm(alarm["id"], {"group_id": group_id})
    taken = {a["weekday"] for a in await bot.db.group_alarms(uid, group_id)} | {alarm["weekday"]}
    new_days = [d for d in dict.fromkeys(days) if d not in taken]
    if await bot.db.count_alarms(uid) + len(new_days) > config.MAX_ALARMS_PER_USER:
        raise ValidationError(f"アラームは{config.MAX_ALARMS_PER_USER}個までです。")
    sound_ids = await bot.db.alarm_sound_ids(alarm["id"])
    for day in new_days:
        fields = {k: alarm[k] for k in models.COPY_FIELDS}
        fields.update(user_id=uid, weekday=day, group_id=group_id)
        new_id = await bot.db.create_alarm(fields)
        await bot.db.set_alarm_sounds(new_id, sound_ids)
    bot.scheduler.poke()
    group = await bot.db.group_alarms(uid, group_id)
    return web.json_response({"alarms": await _alarms_out(bot, group)})


async def test_alarm(request: web.Request) -> web.Response:
    bot = _bot(request)
    uid = _uid(request)
    alarm = await _own_alarm(request)
    channel = bot.get_channel(alarm["channel_id"]) if alarm["channel_id"] else None
    if not isinstance(channel, discord.VoiceChannel):
        raise ValidationError("先にVCを選んで保存してください。")
    ok, message = await bot.runs.start_test(uid, channel, alarm=alarm)
    if ok and uid not in channel.voice_states:
        message += " いまはそのVCに入っていないので、音は聞こえません。"
    return web.json_response({"ok": ok, "message": message})


# --- API: 音声 ------------------------------------------------------------------
_upload_log: dict[int, deque] = defaultdict(deque)


async def list_sounds(request: web.Request) -> web.Response:
    sounds = await _bot(request).db.list_sounds(_uid(request))
    return web.json_response({"sounds": [sound_out(s) for s in sounds]})


async def upload_sound(request: web.Request) -> web.Response:
    bot = _bot(request)
    uid = _uid(request)
    if await bot.db.count_sounds(uid) >= config.MAX_SOUNDS_PER_USER:
        raise ValidationError(f"音声は{config.MAX_SOUNDS_PER_USER}個までです。使わないものを削除してください。")
    history = _upload_log[uid]
    cutoff = time.monotonic() - 3600
    while history and history[0] < cutoff:
        history.popleft()
    if len(history) >= config.UPLOADS_PER_HOUR:
        raise ValidationError("アップロードが多すぎます。しばらくしてからもう一度試してください。")

    reader = await request.multipart()
    name = ""
    saved = None
    try:
        while (part := await reader.next()) is not None:
            if part.name == "name":
                name = (await part.text()).strip()[:40]
            elif part.name == "file" and saved is None:
                saved = await bot.audio.save_upload(part)
    except BaseException:
        if saved:
            saved[2].unlink(missing_ok=True)
        raise
    if saved is None:
        raise ValidationError("ファイルを選んでください。")
    digest, ext, tmp, filename = saved
    history.append(time.monotonic())
    await bot.audio.ingest(digest, ext, tmp)
    sound_id = await bot.db.create_sound(uid, name or Path(filename).stem[:40] or "音声", digest)
    return web.json_response({"sound": sound_out(await bot.db.get_sound(sound_id))})


async def rename_sound(request: web.Request) -> web.Response:
    bot = _bot(request)
    sound = await _own_sound(request)
    data = await _json(request)
    name = data.get("name")
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 40:
        raise ValidationError("名前は1〜40文字にしてください。")
    await bot.db.rename_sound(sound["id"], name.strip())
    return web.json_response({"sound": sound_out(await bot.db.get_sound(sound["id"]))})


async def delete_sound(request: web.Request) -> web.Response:
    bot = _bot(request)
    sound = await _own_sound(request)
    await bot.db.delete_sound(sound["id"])  # 実ファイルは毎日の掃除で消える
    return web.json_response({"deleted": sound["id"]})


async def sound_file(request: web.Request) -> web.StreamResponse:
    bot = _bot(request)
    sound = await _own_sound(request)
    path = None
    if request.query.get("original") != "1":
        level = _to_int(request.query.get("v")) or 60
        if level not in config.VOLUME_LEVELS:
            level = 60
        candidate = bot.audio.converted_path(sound["hash"], level)
        path = candidate if candidate.exists() else None
    path = path or bot.audio.original_path(sound["hash"])
    if path is None:
        return jerr(404, "ファイルが見つかりません。")
    ctype = AUDIO_TYPES.get(path.suffix.lower(), "application/octet-stream")
    return web.FileResponse(path, headers={"Cache-Control": "private, max-age=600", "Content-Type": ctype})


# --- 起動 -----------------------------------------------------------------------
def create_app(bot: "AlarmBot") -> web.Application:
    app = web.Application(middlewares=[session_middleware],
                          client_max_size=config.MAX_UPLOAD_BYTES + 1024 * 1024)
    app[BOT] = bot
    app.on_response_prepare.append(_security_headers)
    r = app.router
    r.add_get("/", index)
    r.add_get("/healthz", healthz)
    r.add_get("/login", login)
    r.add_get("/callback", callback)
    r.add_post("/logout", logout)
    r.add_get("/api/bootstrap", bootstrap)
    r.add_post("/api/alarms", create_alarm)
    r.add_patch("/api/alarms/{id}", update_alarm)
    r.add_delete("/api/alarms/{id}", delete_alarm)
    r.add_post("/api/alarms/{id}/copy", copy_alarm)
    r.add_post("/api/alarms/{id}/test", test_alarm)
    r.add_get("/api/sounds", list_sounds)
    r.add_post("/api/sounds", upload_sound)
    r.add_patch("/api/sounds/{id}", rename_sound)
    r.add_delete("/api/sounds/{id}", delete_sound)
    r.add_get("/api/sounds/{id}/file", sound_file)
    r.add_static("/static/", STATIC)
    return app


async def start_web(bot: "AlarmBot") -> web.AppRunner:
    runner = web.AppRunner(create_app(bot), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", config.PORT)
    await site.start()
    log.info("ダッシュボード: %s（ポート %d）", config.PUBLIC_URL, config.PORT)
    return runner
