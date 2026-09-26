#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
═══════════════════════════════════════════════════════════════════════════
   ⏤͟͟͞͞ 🇮🇳 Dᴋ Sʜᴀʀᴍᴀ  —  PREMIUM REFERRAL & REWARD BOT  (v3)
───────────────────────────────────────────────────────────────────────────
   • Force Join (Public + Private join-request auto detect + auto approve)
   • Unlimited Force-Join Channels (add / edit name / change link / remove)
   • First Bonus Claim  →  then 1 Reward per N Referrals
   • Agent Number Pool (unique / shared / cyclic distribution)
   • WhatsApp "Contact Now" button on every Agent Number
   • Gift Code System  (/claim CODE)  — create / edit / limit / expiry
   • Premium (custom) emoji safe in every editable text & button
   • Global Maintenance Mode (blocks every button / command instantly)
   • Broadcast v2: live counters • Pause / Resume / Stop • rate-limit safe
   • DB Backup + DB Restore (.db upload) • Broadcast with URL button
   • Referral Tracking (unique link:  t.me/bot?start=USERID)
   • Full Admin Panel : Stats • Rewards • Channels • Gift Codes • Settings •
     Users Manage • Broadcast • Leaderboard • Waitlist • CSV • Sub-Admins
───────────────────────────────────────────────────────────────────────────
   Developer : ⏤͟͟͞͞ 🇮🇳 Dᴋ Sʜᴀʀᴍᴀ
═══════════════════════════════════════════════════════════════════════════
"""

import os
import re
import io
import shutil
import csv
import html
import json
import time
import random
import string
import asyncio
import logging
import sqlite3
import threading
from datetime import datetime, timezone

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MessageEntity,
)
from telegram.constants import ParseMode, ChatMemberStatus
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError, TimedOut
from telegram.ext import (
    Application,
    ApplicationBuilder,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    ChatJoinRequestHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

# ════════════════════════════════════════════════════════════════════════
#  CONFIG  (Railway → Variables)
# ════════════════════════════════════════════════════════════════════════

BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
OWNER_ID = int((os.getenv("OWNER_ID") or "0").strip() or 0)
EXTRA_ADMINS = {
    int(x) for x in (os.getenv("ADMIN_IDS") or "").replace(" ", "").split(",") if x.isdigit()
}
DEV_NAME = os.getenv("DEV_NAME", "⏤͟͟͞͞ 🇮🇳 Dᴋ Sʜᴀʀᴍᴀ")
DEV_USERNAME = (os.getenv("DEV_USERNAME") or "DkSharma").strip().lstrip("@")
DB_PATH = os.getenv("DB_PATH", "bot_data.db")

# Broadcast tuning (safe defaults — Telegram allows ~30 msg/s bulk)
BC_RATE = float(os.getenv("BC_RATE") or "20")        # messages per second (global cap)
BC_WORKERS = int(os.getenv("BC_WORKERS") or "6")      # parallel senders
BC_STATUS_EVERY = float(os.getenv("BC_STATUS_EVERY") or "2")   # seconds between live updates

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("DKBOT")

BOT_USERNAME = ""  # runtime me fill hoga

# ════════════════════════════════════════════════════════════════════════
#  DATABASE
# ════════════════════════════════════════════════════════════════════════

_lock = threading.RLock()
_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
_conn.row_factory = sqlite3.Row
_conn.execute("PRAGMA journal_mode=WAL")
_conn.execute("PRAGMA synchronous=NORMAL")


def q(sql, args=()):
    with _lock:
        cur = _conn.execute(sql, args)
        _conn.commit()
        return cur


def one(sql, args=()):
    with _lock:
        cur = _conn.execute(sql, args)
        return cur.fetchone()


def rows(sql, args=()):
    with _lock:
        cur = _conn.execute(sql, args)
        return cur.fetchall()


def scalar(sql, args=(), default=0):
    r = one(sql, args)
    if not r:
        return default
    v = r[0]
    return default if v is None else v


DEFAULT_SETTINGS = {
    "refs_per_reward": "1",
    "bonus_enabled": "1",
    "force_join": "1",
    "auto_approve": "1",
    "maintenance": "0",
    "reward_mode": "unique",          # unique | shared | cyclic
    "leave_penalty": "1",             # channel chhoda to dobara verify
    "notify_referrer": "1",
    "log_channel": "",
    "start_photo": "",
    "gc_prefix": "EARNINGZONE",       # gift code prefix
    "premium_btn_icons": "1",         # buttons par premium emoji icon (Bot API 9.4+)
    "user_button_config": "{}",       # legacy key; migrated automatically
    "button_config": "{}",            # all buttons: replacement emoji/text/style
    "button_registry": "{}",          # discovered static and dynamic button definitions
    "welcome_text": (
        "🌟 <b>Google Map Rating Agent Bot</b> 🗺️\n\n"
        "📲 Yahan milega <b>Google Map Agent</b> ka WhatsApp number.\n\n"
        "🔑 Pehla number <b>FREE</b> hai!\n"
        "👥 Har <b>{per} referral</b> = 1 naya <b>Agent Number</b>\n"
        "📞 Number pe WhatsApp se task lo!\n\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "👥 Referrals: <b>{refs}</b>  •  Numbers: <b>{claims}</b>\n"
        "🎯 Next: <b>{next}</b>\n"
        "━━━━━━━━━━━━━━━━━━━\n\n"
        "Shuru karo 👇 | <i>By {dev}</i>"
    ),
    "reward_text": (
        "🎉 <b>Cᴏɴɢʀᴀᴛᴜʟᴀᴛɪᴏɴs {name}!</b> 🎉"
    ),
    "reward_buttons": "[]",
    "gate_text": (
        "📢 <b>Join Required</b>\n\n"
        "Please join the {channels} below to continue using the bot.\n"
        "✅ Join the {channels} and then tap <b>Continue</b>."
    ),
    "outofstock_text": (
        "😔 <b>Aɢᴇɴᴛ Nᴜᴍʙᴇʀs ᴏᴜᴛ ᴏғ sᴛᴏᴄᴋ!</b>\n\n"
        "Filhaal saare agent numbers claim ho chuke hain. Admin naye numbers add karega "
        "to bot aapko <b>automatically notify</b> kar dega. Aap waiting list me "
        "safe hain ✅"
    ),
    "maintenance_text": (
        "🛠 <b>Bot Maintenance Mode me hai</b>\n\n"
        "Hum bot ko upgrade kar rahe hain. Saare buttons aur features "
        "thodi der ke liye band hain.\n\n"
        "⏳ Kripya kuch der baad dobara try kijiye."
    ),
}


def _add_col(table: str, col: str, decl: str) -> None:
    """Safe ALTER TABLE ADD COLUMN (purane DB ke liye migration)."""
    try:
        q(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    except sqlite3.OperationalError:
        pass


def db_init():
    q("""CREATE TABLE IF NOT EXISTS users(
            user_id     INTEGER PRIMARY KEY,
            username    TEXT,
            first_name  TEXT,
            ref_by      INTEGER,
            ref_done    INTEGER DEFAULT 0,
            refs        INTEGER DEFAULT 0,
            claims      INTEGER DEFAULT 0,
            verified    INTEGER DEFAULT 0,
            blocked     INTEGER DEFAULT 0,
            joined_at   INTEGER,
            last_seen   INTEGER
        )""")
    q("""CREATE TABLE IF NOT EXISTS rewards(
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            code      TEXT,
            used_by   INTEGER,
            used_at   INTEGER,
            added_at  INTEGER
        )""")
    q("""CREATE TABLE IF NOT EXISTS claims(
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER,
            reward_id  INTEGER,
            code       TEXT,
            kind       TEXT,
            claimed_at INTEGER
        )""")
    q("""CREATE TABLE IF NOT EXISTS channels(
            chat_id     INTEGER PRIMARY KEY,
            title       TEXT,
            invite_link TEXT,
            is_private  INTEGER DEFAULT 1,
            added_at    INTEGER
        )""")
    q("""CREATE TABLE IF NOT EXISTS requests(
            user_id  INTEGER,
            chat_id  INTEGER,
            status   TEXT,
            ts       INTEGER,
            PRIMARY KEY(user_id, chat_id)
        )""")
    q("""CREATE TABLE IF NOT EXISTS admins(
            user_id  INTEGER PRIMARY KEY,
            added_by INTEGER,
            added_at INTEGER
        )""")
    q("""CREATE TABLE IF NOT EXISTS waitlist(
            user_id INTEGER PRIMARY KEY,
            ts      INTEGER
        )""")
    q("""CREATE TABLE IF NOT EXISTS settings(
            key TEXT PRIMARY KEY,
            val TEXT
        )""")
    # ── Gift codes ──
    q("""CREATE TABLE IF NOT EXISTS giftcodes(
            code         TEXT PRIMARY KEY,
            agent_number TEXT,
            max_uses     INTEGER DEFAULT 1,
            used         INTEGER DEFAULT 0,
            expires_at   INTEGER DEFAULT 0,
            active       INTEGER DEFAULT 1,
            created_at   INTEGER,
            created_by   INTEGER
        )""")
    q("""CREATE TABLE IF NOT EXISTS giftclaims(
            code    TEXT,
            user_id INTEGER,
            number  TEXT,
            ts      INTEGER,
            PRIMARY KEY(code, user_id)
        )""")
    # ── migrations ──
    _add_col("channels", "btn_text", "TEXT")          # custom join-button label
    _add_col("channels", "link_manual", "INTEGER DEFAULT 0")  # admin ne link khud set kiya
    _add_col("users", "gate_msg", "INTEGER")          # last gate message id (auto-unlock)

    for k, v in DEFAULT_SETTINGS.items():
        q("INSERT OR IGNORE INTO settings(key,val) VALUES(?,?)", (k, v))
    # migration: agar DB me purane default texts pade hain to naye texts laga do
    legacy = {
        "welcome_text": "exclusive reward codes",
        "reward_text": "Aapka reward unlock ho gaya hai",
        "outofstock_text": "Rewards ᴏᴜᴛ ᴏғ sᴛᴏᴄᴋ",
        "gate_text": "Vᴇʀɪғɪᴇᴅ",
        "gate_text ": "Bot use karne ke liye niche diya gaya channel",
    }
    for k, marker in legacy.items():
        k = k.strip()
        cur = one("SELECT val FROM settings WHERE key=?", (k,))
        if cur and cur["val"] and marker in cur["val"]:
            ss(k, DEFAULT_SETTINGS[k])


def gs(key, default=""):
    r = one("SELECT val FROM settings WHERE key=?", (key,))
    if r is None or r["val"] is None:
        return DEFAULT_SETTINGS.get(key, default)
    return r["val"]


def gi(key, default=0):
    try:
        return int(gs(key, str(default)))
    except (TypeError, ValueError):
        return default


def ss(key, val):
    q("INSERT INTO settings(key,val) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET val=excluded.val",
      (key, str(val)))


def db_restore_from(src_path: str) -> None:
    """Uploaded .db file se current database replace kare (safe swap + .bak)."""
    global _conn
    with open(src_path, "rb") as f:
        head = f.read(16)
    if not head.startswith(b"SQLite format 3"):
        raise ValueError("Ye valid SQLite database file nahi hai.")
    with _lock:
        _conn.commit()
        _conn.close()
        try:
            if os.path.exists(DB_PATH):
                shutil.copyfile(DB_PATH, DB_PATH + ".bak")
            for suffix in ("-wal", "-shm"):
                p = DB_PATH + suffix
                if os.path.exists(p):
                    os.remove(p)
            os.replace(src_path, DB_PATH)
        finally:
            _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
            _conn.row_factory = sqlite3.Row
            _conn.execute("PRAGMA journal_mode=WAL")
            _conn.execute("PRAGMA synchronous=NORMAL")
    db_init()
    _join_cache.clear()


# ════════════════════════════════════════════════════════════════════════
#  SMALL UTILS
# ════════════════════════════════════════════════════════════════════════

def now() -> int:
    return int(time.time())


def esc(x) -> str:
    return html.escape(str(x if x is not None else ""))


def fnum(n) -> str:
    """1250 → 1,250"""
    try:
        return f"{int(n):,}"
    except (TypeError, ValueError):
        return str(n)


def normalize_phone(raw: str) -> str:
    """Any format → 10-digit Indian number (no country code)."""
    digits = re.sub(r"\D", "", str(raw or ""))
    if digits.startswith("91") and len(digits) == 12:
        digits = digits[2:]           # 91XXXXXXXXXX → XXXXXXXXXX
    elif digits.startswith("0") and len(digits) == 11:
        digits = digits[1:]           # 0XXXXXXXXXX → XXXXXXXXXX
    return digits


def is_phone(raw: str) -> bool:
    return len(normalize_phone(raw)) == 10


def wa_link(number: str) -> str:
    """WhatsApp deeplink for an Indian number."""
    return f"https://wa.me/91{normalize_phone(number)}"


def fmt_ts(ts) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(int(ts), timezone.utc).strftime("%d %b %Y, %H:%M UTC")


def admin_ids() -> set:
    s = {OWNER_ID} | EXTRA_ADMINS
    for r in rows("SELECT user_id FROM admins"):
        s.add(int(r["user_id"]))
    s.discard(0)
    return s


def is_admin(uid: int) -> bool:
    return int(uid) in admin_ids()


def is_owner(uid: int) -> bool:
    return int(uid) == OWNER_ID


def get_user(uid: int):
    return one("SELECT * FROM users WHERE user_id=?", (uid,))


def touch_user(u) -> None:
    q("UPDATE users SET username=?, first_name=?, last_seen=? WHERE user_id=?",
      (u.username, u.first_name, now(), u.id))


def register_user(u, ref_by=None) -> bool:
    """Naya user insert kare. True = pehli baar aaya."""
    if get_user(u.id):
        touch_user(u)
        return False
    if ref_by is not None:
        if int(ref_by) == int(u.id) or not get_user(int(ref_by)):
            ref_by = None
    q("""INSERT INTO users(user_id,username,first_name,ref_by,ref_done,refs,claims,
         verified,blocked,joined_at,last_seen) VALUES(?,?,?,?,0,0,0,0,0,?,?)""",
      (u.id, u.username, u.first_name, ref_by, now(), now()))
    return True


async def send_log(context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    ch = gs("log_channel").strip()
    if not ch:
        return
    try:
        await context.bot.send_message(int(ch), text, parse_mode=ParseMode.HTML,
                                       disable_web_page_preview=True)
    except Exception as e:  # noqa: BLE001
        log.warning("log channel fail: %s", e)


def ref_link(uid: int) -> str:
    return f"https://t.me/{BOT_USERNAME}?start={uid}"


# ════════════════════════════════════════════════════════════════════════
#  PREMIUM (CUSTOM) EMOJI ENGINE
# ════════════════════════════════════════════════════════════════════════
#  Telegram premium emoji = MessageEntity(type="custom_emoji").  Agar hum
#  sirf msg.text save karein to emoji ka sirf fallback bachta hai aur premium
#  look chala jata hai.  Isliye admin ke har editable text ko HTML me convert
#  karke save karte hain — premium emoji <tg-emoji emoji-id="…">🙂</tg-emoji>
#  ban ke safe rehta hai, aur bot ParseMode.HTML se wapas premium bhejta hai.
#
#  NOTE (Telegram rule): bot premium emoji tabhi render karega jab bot ke
#  owner ke paas Telegram Premium ho (Bot API 9.4+) ya bot ke paas Fragment
#  wala username ho.  Warna Telegram fallback emoji dikhata hai — text kabhi
#  tootta nahi.

TG_EMOJI_RE = re.compile(r'<tg-emoji\s+emoji-id="(\d+)">(.*?)</tg-emoji>', re.S)


def html_from_message(msg) -> str:
    """Admin ke bheje message ko HTML string me convert kare.

    • Sirf premium emoji entities → raw text rakho (admin ke type kiye HTML
      tags <b> etc. bhi chalte hain) aur emoji ko <tg-emoji> me wrap karo.
    • Telegram formatting (bold/italic/link…) bhi hai → PTB ka text_html
      (poori formatting + premium emoji preserved).
    """
    if msg is None:
        return ""
    text = msg.text if msg.text is not None else (msg.caption or "")
    ents = list(msg.entities or msg.caption_entities or [])
    if not text:
        return ""
    if not ents:
        return text
    others = [e for e in ents if e.type != MessageEntity.CUSTOM_EMOJI]
    if others:
        try:
            return msg.text_html if msg.text is not None else msg.caption_html
        except Exception:  # noqa: BLE001
            pass
    # Sirf custom-emoji entities: UTF-16 offsets ke hisaab se wrap karo
    u16 = text.encode("utf-16-le")
    out, pos = [], 0
    for e in sorted(ents, key=lambda x: x.offset):
        if e.type != MessageEntity.CUSTOM_EMOJI or not e.custom_emoji_id:
            continue
        s, en = e.offset * 2, (e.offset + e.length) * 2
        if s < pos:
            continue
        out.append(u16[pos:s].decode("utf-16-le", "ignore"))
        inner = u16[s:en].decode("utf-16-le", "ignore")
        out.append(f'<tg-emoji emoji-id="{e.custom_emoji_id}">{inner}</tg-emoji>')
        pos = en
    out.append(u16[pos:].decode("utf-16-le", "ignore"))
    return "".join(out)


def strip_tg_emoji(s: str) -> str:
    """<tg-emoji …>🙂</tg-emoji> → 🙂  (button text / plain places ke liye)."""
    return TG_EMOJI_RE.sub(r"\2", s or "")


def plain_text(s: str) -> str:
    """HTML → plain (buttons / alerts ke liye)."""
    s = strip_tg_emoji(s or "")
    s = re.sub(r"<[^>]+>", "", s)
    return html.unescape(s)


def split_button_label(label: str):
    """Button label se premium emoji nikal kar (text, icon_id) de.
    Telegram Bot API 9.4+: button pe premium emoji icon_custom_emoji_id se
    dikhta hai; text me fallback emoji rehta hai."""
    icon = None
    m = TG_EMOJI_RE.search(label or "")
    if m:
        icon = m.group(1)
    return plain_text(label).strip(), icon


BUTTON_STYLES = ("default", "primary", "success", "danger")

# Every bot-owned button is registered automatically. This includes static user/admin
# buttons and runtime-generated buttons. Config survives label changes because identity
# is based on callback_data (or an explicit key for URL buttons).
STATIC_BUTTON_DEFAULTS = {
    "claim": "🎫 Cʟᴀɪᴍ Aɢᴇɴᴛ Nᴜᴍʙᴇʀ",
    "refer": "👥 Rᴇғᴇʀ & Eᴀʀɴ",
    "top": "🏆 Lᴇᴀᴅᴇʀʙᴏᴀʀᴅ",
    "menu": "🏠 Mᴇɴᴜ",
    "verify": "✅ Continue",
    "url:share": "📤 Sʜᴀʀᴇ Lɪɴᴋ",
    "url:contact": "📲 Cᴏɴᴛᴀᴄᴛ Nᴏᴡ",
}


def button_registry():
    try:
        data = json.loads(gs("button_registry") or "{}")
        return data if isinstance(data, dict) else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


def save_button_registry(data: dict) -> None:
    ss("button_registry", json.dumps(data, ensure_ascii=False, separators=(",", ":")))


def button_config():
    """Read the global registry, migrating the previous user-only config once."""
    try:
        data = json.loads(gs("button_config") or "{}")
        if not isinstance(data, dict):
            data = {}
    except (TypeError, ValueError, json.JSONDecodeError):
        data = {}
    if not data:
        try:
            old = json.loads(gs("user_button_config") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            old = {}
        old_map = {"leaderboard": "top", "share": "url:share", "contact": "url:contact",
                   "continue": "verify"}
        if isinstance(old, dict):
            for old_key, value in old.items():
                data[old_map.get(old_key, old_key)] = value
            if data:
                save_button_config(data)
    return data


def save_button_config(data: dict) -> None:
    ss("button_config", json.dumps(data, ensure_ascii=False, separators=(",", ":")))


def strip_leading_emoji(text: str) -> str:
    """Remove every old leading emoji/decorative icon before inserting a replacement."""
    text = plain_text(text or "").strip()
    # Keep letters/numbers; discard leading emoji, symbols, variation selectors and spaces.
    text = re.sub(r"^[^\w<]+", "", text, flags=re.UNICODE)
    return text.strip() or plain_text(text).strip()


def register_button(key: str, label: str, action: str = "") -> None:
    reg = button_registry()
    clean = plain_text(label).strip() or "•"
    old = reg.get(key)
    value = {"label": clean[:64], "action": action[:64]}
    if old != value:
        reg[key] = value
        save_button_registry(reg)


def button_key(*, cb: str = None, url: str = None, key: str = None) -> str:
    if key:
        return key
    if cb:
        return cb
    # Dynamic URL buttons are keyed by their label/caller-provided key, never by secret URL.
    return "url:dynamic"


def button_parts(key: str, default_label: str):
    cfg = button_config().get(key, {})
    custom_label = str(cfg.get("label") or "").strip()
    base = custom_label or default_label
    emoji_html = str(cfg.get("emoji") or "").strip()
    remove_emoji = bool(cfg.get("remove_emoji"))
    if emoji_html or remove_emoji:
        base = strip_leading_emoji(base)
    text = plain_text(base).strip() or "•"
    icon = None
    fallback = ""
    m = TG_EMOJI_RE.search(emoji_html)
    if m:
        icon = m.group(1)
        fallback = plain_text(m.group(2)).strip()
    elif emoji_html:
        # Normal emoji replacement: still replace the old icon instead of appending to it.
        fallback = plain_text(emoji_html).strip()
    if fallback and not icon:
        text = f"{fallback} {text}".strip()
    style = str(cfg.get("style") or "default").lower()
    return text[:64], icon, style


def btn(label: str, *, url: str = None, cb: str = None, style: str = None,
        key: str = None) -> InlineKeyboardButton:
    """Global button factory: replacement emoji, custom style and automatic registry."""
    key = button_key(cb=cb, url=url, key=key)
    register_button(key, label, cb or ("url" if url else ""))
    text, icon, saved_style = button_parts(key, label)
    chosen_style = saved_style if saved_style in BUTTON_STYLES else (style or "default")
    kwargs = {"url": url} if url else {"callback_data": cb}
    extra = {}
    if icon and gi("premium_btn_icons", 1) == 1:
        extra["icon_custom_emoji_id"] = icon
    if chosen_style in BUTTON_STYLES[1:]:
        extra["style"] = chosen_style
    if not extra:
        return InlineKeyboardButton(text, **kwargs)
    try:
        return InlineKeyboardButton(text, **extra, **kwargs)
    except TypeError:
        return InlineKeyboardButton(text, api_kwargs=extra, **kwargs)


def ubtn(key: str, *, url: str = None, cb: str = None, label: str = None) -> InlineKeyboardButton:
    aliases = {"leaderboard": "top", "continue": "verify",
               "share": "url:share", "contact": "url:contact"}
    real_key = aliases.get(key, key)
    default = label or STATIC_BUTTON_DEFAULTS.get(real_key, "•")
    callback = cb if cb is not None else (None if real_key.startswith("url:") else real_key)
    return btn(default, url=url, cb=callback, key=real_key)


def ikb(label: str, callback_data: str = None, url: str = None, *, key: str = None,
        style: str = None) -> InlineKeyboardButton:
    """Drop-in factory used by all static and dynamic bot keyboards."""
    return btn(label, cb=callback_data, url=url, key=key, style=style)

def parse_button_lines(text_html: str):
    """'Button Text - https://link' lines → [{text,url,icon}] , bad_lines"""
    btns, bad = [], []
    for line in (text_html or "").splitlines():
        if "-" not in line:
            if line.strip():
                bad.append(plain_text(line.strip()))
            continue
        # URL me '-' ho sakta hai, isliye last " - " / pehla " http" split
        m = re.search(r"\s*-\s*((?:https?|tg)://\S+)\s*$", line)
        if not m:
            bad.append(plain_text(line.strip()))
            continue
        url = m.group(1)
        label = line[:m.start()].strip()
        if not label:
            bad.append(plain_text(line.strip()))
            continue
        t, icon = split_button_label(label)
        btns.append({"text": t[:40], "url": url, "icon": icon})
    return btns, bad


def url_btn_from_cfg(b: dict) -> InlineKeyboardButton:
    label = b.get("text") or "•"
    if b.get("icon"):
        label = f'<tg-emoji emoji-id="{b["icon"]}">{label}</tg-emoji>'
    return btn(label, url=b.get("url"))


# ════════════════════════════════════════════════════════════════════════
#  FORCE JOIN ENGINE
# ════════════════════════════════════════════════════════════════════════

OK_STATUS = {
    ChatMemberStatus.MEMBER,
    ChatMemberStatus.ADMINISTRATOR,
    ChatMemberStatus.OWNER,
}
_join_cache: dict = {}     # uid -> (ts, [pending channels])
CACHE_TTL = 45           # normal cache
FRESH_TTL = 3            # force_fresh par bhi double-tap absorb karne ke liye


def all_channels():
    return rows("SELECT * FROM channels ORDER BY added_at")


def get_channel(chat_id):
    return one("SELECT * FROM channels WHERE chat_id=?", (int(chat_id),))


def has_request(uid: int, chat_id: int) -> bool:
    r = one("SELECT status FROM requests WHERE user_id=? AND chat_id=?", (uid, chat_id))
    return bool(r and r["status"] in ("pending", "approved"))


def normalize_channel_link(raw: str):
    """Admin ka diya link normalize kare. Return None agar invalid."""
    s = (raw or "").strip()
    if not s:
        return None
    if s.startswith("@"):
        s = s[1:]
        return f"https://t.me/{s}" if re.fullmatch(r"[A-Za-z0-9_]{4,32}", s) else None
    if s.startswith(("t.me/", "telegram.me/")):
        s = "https://" + s
    if s.startswith("http://"):
        s = "https://" + s[7:]
    if re.fullmatch(r"https://(t\.me|telegram\.me|telegram\.dog)/(\+[\w-]+|joinchat/[\w-]+|[A-Za-z0-9_]{4,32})/?", s):
        return s.rstrip("/")
    if s.startswith("tg://"):
        return s
    return None


async def ensure_link(bot, ch):
    if ch["invite_link"]:
        return ch["invite_link"]
    link = None
    try:
        lk = await bot.create_chat_invite_link(
            ch["chat_id"], creates_join_request=True, name="Bot Gate"
        )
        link = lk.invite_link
    except Exception:  # noqa: BLE001
        try:
            link = await bot.export_chat_invite_link(ch["chat_id"])
        except Exception:  # noqa: BLE001
            link = None
    if link:
        q("UPDATE channels SET invite_link=?, link_manual=0 WHERE chat_id=?", (link, ch["chat_id"]))
    return link


async def pending_channels(bot, uid: int, force_fresh: bool = False):
    """User ne jo channel join nahi kiye unki list.
    Public channel → membership check.  Private channel → membership YA
    join-request (pending/approved) dono me pass."""
    if gi("force_join", 1) != 1:
        return []
    if is_admin(uid):
        return []
    chans = all_channels()
    if not chans:
        return []
    hit = _join_cache.get(uid)
    if hit and now() - hit[0] < (FRESH_TTL if force_fresh else CACHE_TTL):
        return hit[1]

    missing = []
    for ch in chans:
        ok = False
        if has_request(uid, ch["chat_id"]):
            ok = True          # private channel me request daal chuka hai / approved
        if not ok:
            try:
                m = await bot.get_chat_member(ch["chat_id"], uid)
                if m.status in OK_STATUS:
                    ok = True
                elif m.status == ChatMemberStatus.RESTRICTED and getattr(m, "is_member", False):
                    ok = True
            except (BadRequest, Forbidden):
                ok = False
            except TelegramError:
                ok = True          # API issue → user ko block na karein
        if not ok:
            missing.append(ch)
    _join_cache[uid] = (now(), missing)
    return missing


async def gate_keyboard(bot, missing):
    """Clean gate: Join buttons (max 2 per row) + full-width Continue.
    Koi dev/powered-by nahi."""
    single = len(missing) == 1
    buttons = []
    for ch in missing:
        link = await ensure_link(bot, ch)
        if not link:
            continue
        label = (ch["btn_text"] or "").strip()
        if not label:
            if single:
                label = "📢 Join Channel"
            else:
                title = (ch["title"] or "Channel").strip()
                label = f"📢 Join • {title[:14]}"      # 2-column row me fit rahe
        buttons.append(btn(label[:40], url=link))
    kb = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]   # 2 per row
    kb.append([ubtn("continue")])
    return InlineKeyboardMarkup(kb)


async def require_join(update: Update, context: ContextTypes.DEFAULT_TYPE, edit=None) -> bool:
    """Har user-action se pehle mandatory-join check (hamesha FRESH, cache-bypass).
    True = user blocked hai aur gate screen dikha di gayi.
    Admins ko pending_channels() khud exempt karta hai."""
    tg = update.effective_user
    missing = await pending_channels(context.bot, tg.id, force_fresh=True)
    if not missing:
        return False
    cq = update.callback_query
    if cq:
        try:
            await cq.answer("🔒 Please join the required channel(s) first.", show_alert=True)
        except Exception:  # noqa: BLE001
            pass
    await show_gate(update, context, missing, edit=bool(cq) if edit is None else edit)
    return True


# ════════════════════════════════════════════════════════════════════════
#  REWARD ENGINE
# ════════════════════════════════════════════════════════════════════════

def rewards_left() -> int:
    mode = gs("reward_mode")
    if mode in ("shared", "cyclic"):
        total = scalar("SELECT COUNT(*) FROM rewards")
        return 9999 if total else 0   # pool hai to kabhi khatam nahi hota
    return int(scalar("SELECT COUNT(*) FROM rewards WHERE used_by IS NULL"))


def stock_label() -> str:
    left = rewards_left()
    if gs("reward_mode") in ("shared", "cyclic") and left:
        return "∞"
    return str(left)


def claim_state(u):
    """(can_claim, needed_refs, kind)"""
    per = max(1, gi("refs_per_reward", 1))
    bonus = gi("bonus_enabled", 1) == 1
    claims = int(u["claims"] or 0)
    refs = int(u["refs"] or 0)
    if claims == 0 and bonus:
        return True, 0, "bonus"
    used = (claims - (1 if bonus else 0)) * per
    used = max(0, used)
    have = refs - used
    if have >= per:
        return True, 0, "referral"
    return False, per - have, "referral"


def take_reward(uid: int, kind: str):
    mode = gs("reward_mode")
    with _lock:
        if mode == "cyclic":
            pool = rows("SELECT * FROM rewards ORDER BY id")
            if not pool:
                return None
            u = get_user(uid)
            idx = int(u["claims"] or 0) % len(pool)
            r = pool[idx]
            prev = str(r["used_by"] or "")
            new_val = f"{prev},{uid}".strip(",")
            q("UPDATE rewards SET used_by=?, used_at=? WHERE id=?", (new_val, now(), r["id"]))
        elif mode == "shared":
            pool = rows("SELECT * FROM rewards ORDER BY id")
            if not pool:
                return None
            u = get_user(uid)
            idx = int(u["claims"] or 0) % len(pool)
            r = pool[idx]
        else:  # unique (default)
            r = one("SELECT * FROM rewards WHERE used_by IS NULL ORDER BY id LIMIT 1")
            if not r:
                return None
            q("UPDATE rewards SET used_by=?, used_at=? WHERE id=?", (uid, now(), r["id"]))
        q("INSERT INTO claims(user_id,reward_id,code,kind,claimed_at) VALUES(?,?,?,?,?)",
          (uid, r["id"], r["code"], kind, now()))
        q("UPDATE users SET claims=claims+1 WHERE user_id=?", (uid,))
    return r


def reward_buttons_kb(code=None, refer=True):
    """Contact Now (WhatsApp) + admin custom buttons + Refer & Earn."""
    kb = []
    if code and is_phone(code):
        kb.append([ubtn("contact", url=wa_link(code))])
    try:
        data = json.loads(gs("reward_buttons") or "[]")
    except Exception:  # noqa: BLE001
        data = []
    row = []
    for b in data:
        if not b.get("text") or not b.get("url"):
            continue
        row.append(url_btn_from_cfg(b))
        if len(row) == 2:
            kb.append(row)
            row = []
    if row:
        kb.append(row)
    if refer:
        kb.append([ubtn("refer")])
    return InlineKeyboardMarkup(kb)


async def delete_current_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Delete the callback's current post. Deletion failure never creates duplicate fallback edits."""
    cq = update.callback_query
    if not cq or not cq.message:
        return
    try:
        await context.bot.delete_message(cq.message.chat_id, cq.message.message_id)
    except (BadRequest, Forbidden):
        # If Telegram no longer allows deletion, remove the keyboard so the stale post is inert.
        try:
            await cq.edit_message_reply_markup(reply_markup=None)
        except Exception:  # noqa: BLE001
            pass


async def fresh_html(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, kb=None,
                     photo: str = None, delete_current: bool = True):
    """Navigation primitive: delete current callback post, then send exactly one fresh post."""
    if delete_current and update.callback_query:
        await delete_current_post(update, context)
    chat_id = update.effective_chat.id if update.effective_chat else update.effective_user.id
    if photo:
        try:
            return await context.bot.send_photo(chat_id, photo, caption=text, reply_markup=kb,
                                                parse_mode=ParseMode.HTML)
        except Exception as e:  # noqa: BLE001
            log.warning("fresh photo send failed; using text: %s", e)
    return await context.bot.send_message(chat_id, text, reply_markup=kb,
                                          parse_mode=ParseMode.HTML,
                                          disable_web_page_preview=True)


# ════════════════════════════════════════════════════════════════════════
#  USER SIDE UI
# ════════════════════════════════════════════════════════════════════════

def main_menu_kb(u=None):
    return InlineKeyboardMarkup([
        [ubtn("claim")],
        [ubtn("refer"), ubtn("leaderboard")],
    ])


def render(text: str, u_row, tg_user=None) -> str:
    per = max(1, gi("refs_per_reward", 1))
    name = (tg_user.first_name if tg_user else None) or (u_row["first_name"] if u_row else "User")
    nxt = "—"
    if u_row:
        can, need, _ = claim_state(u_row)
        nxt = "READY ✅" if can else f"{need} referral aur"
    return (text or "")\
        .replace("{name}", esc(name))\
        .replace("{per}", str(per))\
        .replace("{refs}", str(int(u_row["refs"] or 0) if u_row else 0))\
        .replace("{claims}", str(int(u_row["claims"] or 0) if u_row else 0))\
        .replace("{stock}", stock_label())\
        .replace("{next}", nxt)\
        .replace("{dev}", DEV_NAME)


def menu_payload(uid: int, tg_user=None):
    """(text, keyboard) — welcome/menu ka content, kahin se bhi bhejne ke liye."""
    u = get_user(uid)
    return render(gs("welcome_text"), u, tg_user), main_menu_kb(u)


async def show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, edit=False):
    tg = update.effective_user
    u = get_user(tg.id)
    if not u:
        register_user(tg)
        u = get_user(tg.id)
    txt = render(gs("welcome_text"), u, tg)
    return await fresh_html(update, context, txt, main_menu_kb(u),
                            photo=gs("start_photo").strip() or None,
                            delete_current=bool(update.callback_query))


async def show_gate(update: Update, context: ContextTypes.DEFAULT_TYPE, missing, edit=False):
    """Clean, minimal join screen: sirf Join button(s) + Continue."""
    tg = update.effective_user
    u = get_user(tg.id)
    txt = render(gs("gate_text"), u, tg).replace(
        "{channels}", "channel" if len(missing) == 1 else "channels")
    kb = await gate_keyboard(context.bot, missing)
    m = await fresh_html(update, context, txt, kb,
                         delete_current=bool(update.callback_query))
    # gate message id yaad rakho
    try:
        q("UPDATE users SET gate_msg=? WHERE user_id=?", (int(m.message_id), tg.id))
    except Exception:  # noqa: BLE001
        pass


async def unlock_after_join(context: ContextTypes.DEFAULT_TYPE, uid: int, tg_user=None):
    """Sab channels clear → verify + referral credit + gate message ko menu me badlo."""
    u = get_user(uid)
    if not u:
        return
    if int(u["verified"] or 0) == 0:
        q("UPDATE users SET verified=1 WHERE user_id=?", (uid,))
        await credit_referral(context, uid)
    txt, kb = menu_payload(uid, tg_user)
    gate_msg = u["gate_msg"] if "gate_msg" in u.keys() else None
    if gate_msg:
        try:
            await context.bot.edit_message_text(
                chat_id=uid, message_id=int(gate_msg), text=txt, reply_markup=kb,
                parse_mode=ParseMode.HTML, disable_web_page_preview=True)
            q("UPDATE users SET gate_msg=NULL WHERE user_id=?", (uid,))
            return
        except Exception:  # noqa: BLE001
            pass
    try:
        await context.bot.send_message(
            uid,
            "✅ <b>Verification Complete!</b>\n\nAapka access unlock ho gaya 🎉\n"
            "Abhi apna <b>Agent Number</b> claim kijiye 👇",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [ubtn("claim")],
                [ubtn("menu")],
            ]))
    except Exception:  # noqa: BLE001
        pass


async def credit_referral(context: ContextTypes.DEFAULT_TYPE, uid: int):
    """Verified hone par referrer ko count de."""
    u = get_user(uid)
    if not u or int(u["ref_done"] or 0) == 1 or not u["ref_by"]:
        return
    rb = int(u["ref_by"])
    if rb == uid:
        return
    q("UPDATE users SET ref_done=1 WHERE user_id=?", (uid,))
    q("UPDATE users SET refs=refs+1 WHERE user_id=?", (rb,))
    ref = get_user(rb)
    if ref and gi("notify_referrer", 1) == 1:
        can, need, _ = claim_state(ref)
        tail = ("🎉 Aapka <b>Agent Number ready</b> hai — Claim kijiye!"
                if can else f"Aur <b>{need}</b> referral me next Agent Number unlock hoga.")
        try:
            await context.bot.send_message(
                rb,
                f"🎊 <b>Nᴇᴡ Rᴇғᴇʀʀᴀʟ!</b>\n\n"
                f"👤 <b>{esc(u['first_name'])}</b> ne aapke link se join kiya.\n"
                f"👥 Total Referrals : <b>{int(ref['refs'] or 0)}</b>\n\n{tail}",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(
                    [[ubtn("claim")]]),
            )
        except Exception:  # noqa: BLE001
            pass
    await send_log(context,
                   f"➕ <b>Referral</b>\n👤 {esc(u['first_name'])} (<code>{uid}</code>)\n"
                   f"🔗 By: <code>{rb}</code>")


# ════════════════════════════════════════════════════════════════════════
#  GLOBAL MAINTENANCE GATE  (group -1 → har handler se pehle chalta hai)
# ════════════════════════════════════════════════════════════════════════
#  Maintenance ON hote hi har non-admin update yahin ruk jata hai — purane
#  khule hue messages ke buttons bhi, kyunki har button press ek naya
#  callback update hai jo sabse pehle isi gate se guzarta hai.

def maintenance_on() -> bool:
    return gi("maintenance", 0) == 1


_maint_notice_ts: dict = {}      # uid -> last maintenance notice (anti-spam)
MAINT_NOTICE_COOLDOWN = 60       # seconds


def is_user_interaction(update: Update) -> bool:
    """True SIRF tab jab ek real (non-bot) user ne khud bot se interact kiya ho:
    inline button press, ya private chat me apna message/command.

    Ye sab FALSE hai → gate kabhi trigger nahi hota:
      • service messages (pinned message, joined, title change…) — jaise broadcast
        'Send + Pin' ke baad Telegram jo 'pinned a message' notice banata hai
      • bot ke apne messages / bot accounts
      • channel posts, membership updates, join requests
      • background tasks (broadcast workers) — inme koi update hota hi nahi
    """
    tg = update.effective_user
    if not tg or getattr(tg, "is_bot", False):
        return False
    if update.callback_query:
        return True
    msg = update.message
    if not msg:
        return False
    chat = update.effective_chat
    if not chat or chat.type != "private":
        return False
    if msg.from_user is None or msg.from_user.is_bot:
        return False
    if (msg.pinned_message or msg.new_chat_members or msg.left_chat_member
            or msg.new_chat_title or msg.new_chat_photo or msg.delete_chat_photo
            or msg.group_chat_created or msg.message_auto_delete_timer_changed):
        return False
    return bool(msg.text or msg.caption or msg.effective_attachment)


async def maintenance_gate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Group -1 middleware. Maintenance ON → sirf NORMAL USERS ke direct
    interactions block hote hain. Admin ka har action (panel, commands,
    broadcast start/pause/resume/stop) aur har background/service update
    bina chhue aage nikal jata hai — maintenance text yahin se, aur sirf
    yahin se, bheja jata hai."""
    if not maintenance_on():
        return
    if not is_user_interaction(update):
        return
    tg = update.effective_user
    if is_admin(tg.id):
        return
    txt = gs("maintenance_text")
    cq = update.callback_query
    if cq:
        try:
            await cq.answer("🛠 Bot maintenance me hai. Thodi der baad try kijiye.", show_alert=True)
        except Exception:  # noqa: BLE001
            pass
        # Khula hua message → maintenance notice me badlo, saare buttons hata do
        try:
            await cq.edit_message_text(txt, parse_mode=ParseMode.HTML, reply_markup=None)
        except Exception:  # noqa: BLE001
            try:
                await cq.edit_message_caption(caption=txt, parse_mode=ParseMode.HTML,
                                              reply_markup=None)
            except Exception:  # noqa: BLE001
                try:
                    await cq.edit_message_reply_markup(reply_markup=None)
                except Exception:  # noqa: BLE001
                    pass
        raise ApplicationHandlerStop
    # Plain message/command from a normal user → ek notice, per user max 1 / minute
    last = _maint_notice_ts.get(tg.id, 0)
    if now() - last >= MAINT_NOTICE_COOLDOWN:
        _maint_notice_ts[tg.id] = now()
        try:
            await update.message.reply_html(txt)
        except Exception:  # noqa: BLE001
            pass
    raise ApplicationHandlerStop


# ════════════════════════════════════════════════════════════════════════
#  COMMANDS  (USER)
# ════════════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg = update.effective_user
    if update.effective_chat and update.effective_chat.type != "private":
        return
    ref = None
    if context.args:
        a = str(context.args[0]).strip()
        if a.isdigit():
            ref = int(a)
    fresh = register_user(tg, ref)
    u = get_user(tg.id)

    if int(u["blocked"] or 0) == 1:
        return await context.bot.send_message(
            tg.id, "🚫 <b>Aap blocked hain.</b>\nAdmin se contact kijiye.",
            parse_mode=ParseMode.HTML)

    if fresh:
        await send_log(context,
                       f"🆕 <b>New User</b>\n👤 {esc(tg.first_name)} "
                       f"(@{esc(tg.username) or '—'})\n🆔 <code>{tg.id}</code>\n"
                       f"🔗 Ref: <code>{ref or '—'}</code>")

    missing = await pending_channels(context.bot, tg.id, force_fresh=True)
    if missing:
        return await show_gate(update, context, missing)

    if int(u["verified"] or 0) == 0:
        q("UPDATE users SET verified=1 WHERE user_id=?", (tg.id,))
        await credit_referral(context, tg.id)
    await show_menu(update, context)


async def do_claim(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg = update.effective_user
    cq = update.callback_query
    u = get_user(tg.id)
    if not u:
        register_user(tg)
        u = get_user(tg.id)

    if int(u["blocked"] or 0) == 1:
        if cq:
            return await cq.answer("🚫 Aap blocked hain.", show_alert=True)
        return

    missing = await pending_channels(context.bot, tg.id, force_fresh=True)
    if missing:
        if cq:
            await cq.answer("🔒 Pehle channel join kijiye!", show_alert=True)
        return await show_gate(update, context, missing, edit=bool(cq))

    can, need, kind = claim_state(u)
    if not can:
        per = max(1, gi("refs_per_reward", 1))
        popup = (f"🔒 Agent Number locked!\n\nAur {need} referral chahiye "
                 f"({per} referral = 1 Agent Number).\n\nRefer & Earn button dabaiye 👇")
        if cq:
            await cq.answer(popup, show_alert=True)
        return await show_refer(update, context, edit=bool(cq))

    r = take_reward(tg.id, kind)
    if not r:
        q("INSERT OR IGNORE INTO waitlist(user_id,ts) VALUES(?,?)", (tg.id, now()))
        txt = render(gs("outofstock_text"), u, tg)
        kb = InlineKeyboardMarkup([[ubtn("menu")]])
        if cq:
            await cq.answer("😔 Agent Numbers out of stock!", show_alert=True)
            return await fresh_html(update, context, txt, kb, delete_current=True)
        return await update.effective_message.reply_html(txt, reply_markup=kb)

    u2 = get_user(tg.id)
    can2, need2, _ = claim_state(u2)
    code = str(r["code"])
    body = render(gs("reward_text"), u2, tg)
    txt = (
        f"{body}\n\n"
        f"✅ <b>This is your Google Map Rating Agent Number:</b>\n\n"
        f"┌─────────────────────────┐\n"
        f"│  📱  <code>{esc(code)}</code>\n"
        f"└─────────────────────────┘\n\n"
        f"📋 Type : <b>{'Free Bonus' if kind == 'bonus' else 'Agent Number Reward'}</b>\n"
        f"👥 Referrals : <b>{int(u2['refs'] or 0)}</b>\n"
        f"🔢 Total Claimed : <b>{int(u2['claims'] or 0)}</b>\n\n"
        f"📲 Ye number WhatsApp pe available hai.\n"
        f"Contact karo aur apna <b>Google Map Rating task</b> lo!\n\n"
    )
    txt += ("✅ <b>Agla number bhi ready hai — dobara Claim dabao!</b>"
            if can2 else f"👥 Next number ke liye <b>{need2}</b> referral chahiye.")
    reward_kb = reward_buttons_kb(code)

    if cq:
        await cq.answer("🎉 Agent Number unlocked!")
        await fresh_html(update, context, txt, reward_kb, delete_current=True)
    else:
        await update.effective_message.reply_html(txt, reply_markup=reward_kb)

    await send_log(context,
                   f"📱 <b>Agent Number Claimed</b>\n👤 {esc(tg.first_name)} (<code>{tg.id}</code>)\n"
                   f"🔑 <code>{esc(code)}</code>\n🏷 {kind}\n📦 Left: {stock_label()}")

    left = rewards_left()
    if gs("reward_mode") not in ("shared", "cyclic") and left in (0, 1, 2):
        for a in admin_ids():
            try:
                await context.bot.send_message(
                    a, f"⚠️ <b>Agent Number Stock Alert</b>\nSirf <b>{left}</b> number bache hain.\n"
                       f"Admin Panel → Rewards Manager se add kijiye.",
                    parse_mode=ParseMode.HTML)
            except Exception:  # noqa: BLE001
                pass


async def show_refer(update: Update, context: ContextTypes.DEFAULT_TYPE, edit=False):
    tg = update.effective_user
    u = get_user(tg.id)
    if not u:
        register_user(tg)
        u = get_user(tg.id)
    per = max(1, gi("refs_per_reward", 1))
    link = ref_link(tg.id)
    invited = int(scalar("SELECT COUNT(*) FROM users WHERE ref_by=?", (tg.id,)))
    verified = int(scalar("SELECT COUNT(*) FROM users WHERE ref_by=? AND ref_done=1", (tg.id,)))
    can, need, _ = claim_state(u)
    txt = (
        "👥 <b>Rᴇғᴇʀ & Eᴀʀɴ</b>\n\n"
        f"Har <b>{per} referral</b> = <b>1 Agent Number</b> 📱\n\n"
        "🔗 <b>Yᴏᴜʀ Lɪɴᴋ</b>\n"
        f"<code>{esc(link)}</code>\n\n"
        "━━━━━━━━━━━━━━━\n"
        f"👤 Total Invited : <b>{invited}</b>\n"
        f"✅ Verified : <b>{verified}</b>\n"
        f"📱 Numbers Claimed : <b>{int(u['claims'] or 0)}</b>\n"
        "━━━━━━━━━━━━━━━\n\n"
        + ("🎉 Aapka Agent Number ready hai — Claim kijiye!"
           if can else f"⏳ Next Agent Number : <b>{need}</b> referral aur.")
        + "\n\n<i>Note: Referral tab count hoga jab aapka friend saare channels join kar le.</i>"
    )
    share = (
        "https://t.me/share/url?url=" + link +
        "&text=" + "📱%20Free%20Google%20Map%20Rating%20Agent%20Number%20le%20lo%20is%20bot%20se!"
    )
    kb = InlineKeyboardMarkup([
        [ubtn("share", url=share)],
        [ubtn("claim")],
        [ubtn("menu")],
    ])
    return await fresh_html(update, context, txt, kb,
                            delete_current=bool(update.callback_query))


async def show_top(update: Update, context: ContextTypes.DEFAULT_TYPE, edit=False):
    top = rows("SELECT user_id,first_name,refs,claims FROM users "
               "WHERE refs>0 ORDER BY refs DESC, claims DESC LIMIT 10")
    medals = ["🥇", "🥈", "🥉"] + ["🏅"] * 7
    lines = ["🏆 <b>Tᴏᴘ Rᴇғᴇʀʀᴇʀs</b>\n"]
    if not top:
        lines.append("<i>Abhi koi referral nahi hua. Pehle bano! 🚀</i>")
    for i, r in enumerate(top):
        lines.append(f"{medals[i]} <b>{esc(r['first_name'])}</b> — {int(r['refs'])} refs")
    me = get_user(update.effective_user.id)
    if me:
        rank = int(scalar("SELECT COUNT(*) FROM users WHERE refs > ?", (int(me["refs"] or 0),))) + 1
        lines.append(f"\n━━━━━━━━━━━━━━━\n👤 Your Rank : <b>#{rank}</b> ({int(me['refs'] or 0)} refs)")
    txt = "\n".join(lines)
    kb = InlineKeyboardMarkup([
        [ubtn("refer")],
        [ubtn("menu")],
    ])
    return await fresh_html(update, context, txt, kb,
                            delete_current=bool(update.callback_query))


async def redeem_gift(update: Update, context: ContextTypes.DEFAULT_TYPE, raw_code: str):
    tg = update.effective_user
    msg = update.effective_message
    u = get_user(tg.id)
    if not u:
        register_user(tg)
        u = get_user(tg.id)
    if int(u["blocked"] or 0) == 1:
        return await msg.reply_html("🚫 <b>Aap blocked hain.</b>")

    missing = await pending_channels(context.bot, tg.id, force_fresh=True)
    if missing:
        return await show_gate(update, context, missing)

    code = re.sub(r"[^A-Za-z0-9]", "", raw_code or "").upper()
    if not code:
        return await msg.reply_html("❌ Format: <code>/claim CODE</code>")

    # Atomic check + claim (race-safe)
    with _lock:
        g = gc_get(code)
        err = gc_validate(g, tg.id)
        if err:
            reward_number = None
        else:
            number = str(g["agent_number"]).strip()
            if number.upper() == "POOL":
                r = take_reward(tg.id, "giftcode")
                if not r:
                    err = ("😔 <b>Abhi Agent Numbers out of stock hain.</b>\n"
                           "Code safe hai — stock aate hi dobara try kijiye.")
                    reward_number = None
                else:
                    reward_number = str(r["code"])
            else:
                reward_number = number
                q("INSERT INTO claims(user_id,reward_id,code,kind,claimed_at) VALUES(?,?,?,?,?)",
                  (tg.id, None, reward_number, "giftcode", now()))
            if reward_number:
                q("INSERT OR IGNORE INTO giftclaims(code,user_id,number,ts) VALUES(?,?,?,?)",
                  (code, tg.id, reward_number, now()))
                q("UPDATE giftcodes SET used=used+1 WHERE code=?", (code,))
    if err:
        return await msg.reply_html(err)

    txt = (
        "🎉 <b>Gift Code Claimed Successfully!</b>\n\n"
        "🎁 Your Reward: <b>Agent Number</b>\n"
        f"📞 Number: <b>{esc(reward_number)}</b>\n\n"
        "👇 <b>Contact Now</b> karke apna task lijiye."
    )
    kb = reward_buttons_kb(reward_number, refer=False)
    await msg.reply_html(txt, reply_markup=kb, disable_web_page_preview=True)
    await send_log(context,
                   f"🎁 <b>Gift Code Claimed</b>\n👤 {esc(tg.first_name)} (<code>{tg.id}</code>)\n"
                   f"🏷 <code>{esc(code)}</code>\n📞 <code>{esc(reward_number)}</code>")


# ════════════════════════════════════════════════════════════════════════
#  ADMIN PANEL
# ════════════════════════════════════════════════════════════════════════

def admin_home_kb():
    return InlineKeyboardMarkup([
        [ikb("📊 Sᴛᴀᴛɪsᴛɪᴄs", callback_data="a_stats"),
         ikb("🎁 Rᴇᴡᴀʀᴅs", callback_data="a_rw")],
        [ikb("📢 Fᴏʀᴄᴇ Jᴏɪɴ", callback_data="a_ch"),
         ikb("🎟 Gɪғᴛ Cᴏᴅᴇs", callback_data="a_gc")],
        [ikb("👥 Usᴇʀs Mᴀɴᴀɢᴇ", callback_data="a_users"),
         ikb("📣 Bʀᴏᴀᴅᴄᴀsᴛ", callback_data="a_bc")],
        [ikb("⚙️ Sᴇᴛᴛɪɴɢs", callback_data="a_set"),
         ikb("🎫 Cʟᴀɪᴍ Lᴏɢs", callback_data="a_logs")],
        [ikb("🏆 Tᴏᴘ Rᴇғᴇʀʀᴇʀs", callback_data="a_top"),
         ikb("👮 Sᴜʙ-Aᴅᴍɪɴs", callback_data="a_admins")],
        [ikb("🛠 Tᴏᴏʟs & Bᴀᴄᴋᴜᴘ", callback_data="a_tools"),
         ikb("🏠 Usᴇʀ Mᴇɴᴜ", callback_data="menu")],
    ])


def back_kb(target="a_home"):
    return InlineKeyboardMarkup([[ikb("⬅️ Bᴀᴄᴋ", callback_data=target)]])


async def edit_or_send(update: Update, context: ContextTypes.DEFAULT_TYPE, txt: str, kb=None):
    """Admin navigation: delete current post and send one fresh screen."""
    return await fresh_html(update, context, txt, kb, delete_current=bool(update.callback_query))


async def admin_home(update: Update, context: ContextTypes.DEFAULT_TYPE, edit=True):
    context.user_data.pop("state", None)
    context.user_data.pop("bc_button", None)
    total = int(scalar("SELECT COUNT(*) FROM users"))
    txt = (
        "👑 <b>Aᴅᴍɪɴ Cᴏɴᴛʀᴏʟ Pᴀɴᴇʟ</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"👥 Users : <b>{total}</b>\n"
        f"📦 Numbers Left : <b>{stock_label()}</b>\n"
        f"🎛 Mode : <b>{gs('reward_mode').upper()}</b>\n"
        f"🔗 Refs / Reward : <b>{gi('refs_per_reward', 1)}</b>\n"
        f"📢 Force Join : <b>{'ON ✅' if gi('force_join', 1) else 'OFF ❌'}</b>\n"
        f"🎟 Gift Codes : <b>{int(scalar('SELECT COUNT(*) FROM giftcodes WHERE active=1'))} active</b>\n"
        f"🛠 Maintenance : <b>{'ON ⚠️' if maintenance_on() else 'OFF ✅'}</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"<i>Pᴀɴᴇʟ ʙʏ</i> <b>{DEV_NAME}</b>"
    )
    if edit and update.callback_query:
        return await edit_or_send(update, context, txt, admin_home_kb())
    await context.bot.send_message(update.effective_user.id, txt,
                                   reply_markup=admin_home_kb(),
                                   parse_mode=ParseMode.HTML)


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.effective_message.reply_html(
            "🚫 <b>Ye command sirf admin ke liye hai.</b>")
    await admin_home(update, context, edit=False)


async def a_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    day = now() - 86400
    week = now() - 604800
    txt = (
        "📊 <b>Bᴏᴛ Sᴛᴀᴛɪsᴛɪᴄs</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"👥 Total Users : <b>{int(scalar('SELECT COUNT(*) FROM users'))}</b>\n"
        f"✅ Verified : <b>{int(scalar('SELECT COUNT(*) FROM users WHERE verified=1'))}</b>\n"
        f"🚫 Blocked : <b>{int(scalar('SELECT COUNT(*) FROM users WHERE blocked=1'))}</b>\n"
        f"🆕 Today : <b>{int(scalar('SELECT COUNT(*) FROM users WHERE joined_at>?', (day,)))}</b>\n"
        f"📅 7 Days : <b>{int(scalar('SELECT COUNT(*) FROM users WHERE joined_at>?', (week,)))}</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🔗 Total Referrals : <b>{int(scalar('SELECT COALESCE(SUM(refs),0) FROM users'))}</b>\n"
        f"🎟 Total Claims : <b>{int(scalar('SELECT COUNT(*) FROM claims'))}</b>\n"
        f"🎁 Bonus Claims : <b>{int(scalar('SELECT COUNT(*) FROM claims WHERE kind=?', ('bonus',)))}</b>\n"
        f"🎫 Gift Code Claims : <b>{int(scalar('SELECT COUNT(*) FROM giftclaims'))}</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"📦 Rewards Total : <b>{int(scalar('SELECT COUNT(*) FROM rewards'))}</b>\n"
        f"✅ Used : <b>{int(scalar('SELECT COUNT(*) FROM rewards WHERE used_by IS NOT NULL'))}</b>\n"
        f"🆓 Left : <b>{rewards_left()}</b>\n"
        f"⏳ Waiting List : <b>{int(scalar('SELECT COUNT(*) FROM waitlist'))}</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"📢 Channels : <b>{len(all_channels())}</b>\n"
        f"🎟 Gift Codes : <b>{int(scalar('SELECT COUNT(*) FROM giftcodes'))}</b>\n"
        f"👮 Admins : <b>{len(admin_ids())}</b>"
    )
    kb = InlineKeyboardMarkup([
        [ikb("🔄 Rᴇғʀᴇsʜ", callback_data="a_stats")],
        [ikb("⬅️ Bᴀᴄᴋ", callback_data="a_home")],
    ])
    await edit_or_send(update, context, txt, kb)


# ─────────────────────────── REWARDS MANAGER ───────────────────────────

async def a_rewards(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("state", None)
    total = int(scalar("SELECT COUNT(*) FROM rewards"))
    used = int(scalar("SELECT COUNT(*) FROM rewards WHERE used_by IS NOT NULL"))
    mode = gs("reward_mode")
    mode_label = {
        "unique": "UNIQUE (1 number = 1 user)",
        "shared": "SHARED (repeat allowed)",
        "cyclic": "CYCLIC (pool rotate → wrap-around)",
    }.get(mode, mode.upper())
    txt = (
        "📱 <b>Aɢᴇɴᴛ Nᴜᴍʙᴇʀ Pᴏᴏʟ</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"📦 Total Numbers : <b>{total}</b>\n"
        f"✅ Used : <b>{used}</b>\n"
        f"🆓 Available : <b>{stock_label() if mode != 'unique' else total - used}</b>\n"
        f"🎛 Mode : <b>{mode_label}</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "<b>Aᴅᴅ Nᴜᴍʙᴇʀs :</b> ek message me bulk numbers daal sakte hain — har line par ek. "
        "Koi bhi format chalega (+91, 91, spaces), auto-normalize ho jayega.\n\n"
        "<b>Mᴏᴅᴇs :</b> UNIQUE = har user ko alag number, khatam to out-of-stock • "
        "SHARED = pool repeat • CYCLIC = 20 numbers / 100 users → 1..20, phir wapas 1 se."
    )
    kb = InlineKeyboardMarkup([
        [ikb("➕ Aᴅᴅ Aɢᴇɴᴛ Nᴜᴍʙᴇʀs", callback_data="a_rw_add")],
        [ikb("📋 Lɪsᴛ / Dᴇʟᴇᴛᴇ", callback_data="a_rw_list")],
        [ikb("🔘 Rᴇᴡᴀʀᴅ Bᴜᴛᴛᴏɴs", callback_data="a_rw_btn"),
         ikb("✍️ Rᴇᴡᴀʀᴅ Tᴇxᴛ", callback_data="a_set_rwtext")],
        [ikb(f"🎛 Mᴏᴅᴇ : {mode.upper()}", callback_data="a_rw_mode")],
        [ikb("🧹 Cʟᴇᴀʀ Usᴇᴅ", callback_data="a_rw_clrused"),
         ikb("🗑 Cʟᴇᴀʀ Aʟʟ", callback_data="a_rw_clrall")],
        [ikb("⬅️ Bᴀᴄᴋ", callback_data="a_home")],
    ])
    await edit_or_send(update, context, txt, kb)


async def a_rw_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rw = rows("SELECT * FROM rewards ORDER BY id LIMIT 30")
    lines = ["📋 <b>Aɢᴇɴᴛ Nᴜᴍʙᴇʀs</b> (max 30 shown)\n"]
    kb = []
    if not rw:
        lines.append("<i>Koi agent number add nahi hai.</i>")
    for r in rw:
        mark = "✅" if r["used_by"] else "🆓"
        used = str(r["used_by"] or "")
        if len(used) > 40:
            used = used[:40] + "…"
        lines.append(f"{mark} <code>{esc(r['code'])}</code>"
                     + (f" → <code>{esc(used)}</code>" if used else ""))
        kb.append([ikb(f"🗑 {r['code'][:28]}", callback_data=f"a_rw_del:{r['id']}")])
    kb.append([ikb("⬅️ Bᴀᴄᴋ", callback_data="a_rw")])
    await edit_or_send(update, context, "\n".join(lines), InlineKeyboardMarkup(kb))


async def notify_waitlist(context: ContextTypes.DEFAULT_TYPE):
    wl = rows("SELECT user_id FROM waitlist")
    if not wl:
        return 0
    sent = 0
    for r in wl:
        try:
            await context.bot.send_message(
                int(r["user_id"]),
                "🎉 <b>Nᴇᴡ Aɢᴇɴᴛ Nᴜᴍʙᴇʀs Aᴅᴅᴇᴅ!</b>\n\n"
                "Stock refill ho gaya hai — abhi apna Agent Number claim kijiye 👇",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(
                    [[ubtn("claim", label="🎫 Cʟᴀɪᴍ Nᴏᴡ")]]))
            sent += 1
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(0.05)
    q("DELETE FROM waitlist")
    return sent


# ─────────────────────────── CHANNELS MANAGER ───────────────────────────

async def a_channels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("state", None)
    context.user_data.pop("target_ch", None)
    ch = all_channels()
    lines = [
        "📢 <b>Fᴏʀᴄᴇ Jᴏɪɴ Cʜᴀɴɴᴇʟs</b>",
        "━━━━━━━━━━━━━━━━━━",
        f"📊 Total : <b>{len(ch)}</b>",
        f"🔐 Force Join : <b>{'ON ✅' if gi('force_join', 1) else 'OFF ❌'}</b>",
        f"⚡ Auto Approve Request : <b>{'ON ✅' if gi('auto_approve', 1) else 'OFF ❌'}</b>",
        "━━━━━━━━━━━━━━━━━━",
    ]
    kb = []
    if not ch:
        lines.append("<i>Koi channel add nahi hai.</i>")
    for c in ch:
        kind = "🔒 Private" if int(c["is_private"] or 0) else "🌐 Public"
        lines.append(f"• <b>{esc(c['title'])}</b> — {kind}\n  <code>{c['chat_id']}</code>")
        kb.append([ikb(f"⚙️ Mᴀɴᴀɢᴇ • {(c['title'] or 'Channel')[:26]}",
                                        callback_data=f"a_ch_v:{c['chat_id']}")])
    lines.append("\n<i>Kisi channel ka naam, link, button text badalne ke liye "
                 "uske ⚙️ Manage button par jaiye.</i>")
    kb.append([ikb("➕ Aᴅᴅ Cʜᴀɴɴᴇʟ", callback_data="a_ch_add")])
    kb.append([ikb(
        f"🔐 Fᴏʀᴄᴇ Jᴏɪɴ : {'ON' if gi('force_join', 1) else 'OFF'}", callback_data="a_t_force"),
        ikb(
        f"⚡ Aᴜᴛᴏ Aᴘᴘʀᴏᴠᴇ : {'ON' if gi('auto_approve', 1) else 'OFF'}",
        callback_data="a_t_approve")])
    kb.append([ikb("🔒 Gᴀᴛᴇ Tᴇxᴛ", callback_data="a_set_gate"),
               ikb("♻️ Rᴇғʀᴇsʜ Aᴜᴛᴏ Lɪɴᴋs", callback_data="a_ch_relink")])
    kb.append([ikb("⬅️ Bᴀᴄᴋ", callback_data="a_home")])
    await edit_or_send(update, context, "\n".join(lines), InlineKeyboardMarkup(kb))


def channel_card(c) -> str:
    link = c["invite_link"] or "—"
    src = "✍️ Manual (admin set)" if int(c["link_manual"] or 0) else "🤖 Auto (bot generated)"
    return (
        "📢 <b>Cʜᴀɴɴᴇʟ Mᴀɴᴀɢᴇ</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🏷 Name : <b>{esc(c['title'])}</b>\n"
        f"🆔 ID : <code>{c['chat_id']}</code>\n"
        f"🔐 Type : <b>{'Private (join request)' if int(c['is_private'] or 0) else 'Public'}</b>\n"
        f"🔗 Link : {esc(link)}\n"
        f"📎 Link Source : <b>{src}</b>\n"
        f"🔘 Button Text : <b>{esc(c['btn_text']) if c['btn_text'] else '📢 Join Channel (default)'}</b>\n"
        f"📅 Added : <b>{fmt_ts(c['added_at'])}</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "<i>Link change karne par purana link turant replace ho jata hai. "
        "Auto Link = bot khud naya join-request link banayega.</i>"
    )


def channel_card_kb(c):
    cid = c["chat_id"]
    return InlineKeyboardMarkup([
        [ikb("✏️ Eᴅɪᴛ Nᴀᴍᴇ", callback_data=f"a_ch_name:{cid}"),
         ikb("🔗 Cʜᴀɴɢᴇ Lɪɴᴋ", callback_data=f"a_ch_link:{cid}")],
        [ikb("🔘 Bᴜᴛᴛᴏɴ Tᴇxᴛ", callback_data=f"a_ch_btn:{cid}"),
         ikb(f"🔐 Tʏᴘᴇ : {'PRIVATE' if int(c['is_private'] or 0) else 'PUBLIC'}",
                              callback_data=f"a_ch_type:{cid}")],
        [ikb("♻️ Aᴜᴛᴏ Lɪɴᴋ (ʀᴇɢᴇɴ)", callback_data=f"a_ch_auto:{cid}"),
         ikb("🔄 Sʏɴᴄ ғʀᴏᴍ Tᴇʟᴇɢʀᴀᴍ", callback_data=f"a_ch_sync:{cid}")],
        [ikb("🗑 Rᴇᴍᴏᴠᴇ Cʜᴀɴɴᴇʟ", callback_data=f"a_ch_del:{cid}")],
        [ikb("⬅️ Bᴀᴄᴋ", callback_data="a_ch")],
    ])


async def a_channel_view(update: Update, context: ContextTypes.DEFAULT_TYPE, cid):
    context.user_data.pop("state", None)
    c = get_channel(cid)
    if not c:
        return await a_channels(update, context)
    await edit_or_send(update, context, channel_card(c), channel_card_kb(c))


async def add_channel_by_id(context: ContextTypes.DEFAULT_TYPE, chat_id):
    chat = await context.bot.get_chat(chat_id)
    cid = chat.id
    me = await context.bot.get_chat_member(cid, context.bot.id)
    if me.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
        raise RuntimeError("Bot us channel me admin nahi hai.")
    is_priv = 1 if not getattr(chat, "username", None) else 0
    link = None
    try:
        if is_priv:
            lk = await context.bot.create_chat_invite_link(
                cid, creates_join_request=True, name="Bot Gate")
            link = lk.invite_link
        else:
            link = f"https://t.me/{chat.username}"
    except Exception:  # noqa: BLE001
        link = None
    existing = get_channel(cid)
    if existing and int(existing["link_manual"] or 0) and existing["invite_link"]:
        link = existing["invite_link"]       # admin ka manual link overwrite mat karo
    q("""INSERT INTO channels(chat_id,title,invite_link,is_private,added_at)
         VALUES(?,?,?,?,?)
         ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title,
         invite_link=excluded.invite_link, is_private=excluded.is_private""",
      (cid, chat.title or "Channel", link, is_priv, now()))
    _join_cache.clear()
    return chat.title or "Channel", link, cid


# ─────────────────────────── SETTINGS ───────────────────────────

async def a_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("state", None)
    txt = (
        "⚙️ <b>Bᴏᴛ Sᴇᴛᴛɪɴɢs</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🔗 Referrals per Reward : <b>{gi('refs_per_reward', 1)}</b>\n"
        f"🎁 First Free Bonus : <b>{'ON ✅' if gi('bonus_enabled', 1) else 'OFF ❌'}</b>\n"
        f"🔐 Force Join : <b>{'ON ✅' if gi('force_join', 1) else 'OFF ❌'}</b>\n"
        f"⚡ Auto Approve : <b>{'ON ✅' if gi('auto_approve', 1) else 'OFF ❌'}</b>\n"
        f"🚪 Leave Penalty : <b>{'ON ✅' if gi('leave_penalty', 1) else 'OFF ❌'}</b>\n"
        f"🔔 Notify Referrer : <b>{'ON ✅' if gi('notify_referrer', 1) else 'OFF ❌'}</b>\n"
        f"🛠 Maintenance : <b>{'ON ⚠️ (sab users blocked)' if maintenance_on() else 'OFF ✅'}</b>\n"
        f"🗒 Log Channel : <b>{esc(gs('log_channel')) or '—'}</b>\n"
        f"🖼 Start Photo : <b>{'SET ✅' if gs('start_photo') else '—'}</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "<i>Text me {name} {per} {refs} {claims} {stock} {next} {dev} variables use kar sakte hain.\n"
        "💎 Premium emoji: text me seedha premium emoji type/paste karo — as-is save hoga.</i>"
    )
    kb = InlineKeyboardMarkup([
        [ikb("🔗 Rᴇғs ᴘᴇʀ Rᴇᴡᴀʀᴅ", callback_data="a_set_refs")],
        [ikb(f"🎁 Bᴏɴᴜs : {'ON' if gi('bonus_enabled', 1) else 'OFF'}",
                              callback_data="a_t_bonus"),
         ikb(f"🛠 Mᴀɪɴᴛ : {'ON' if maintenance_on() else 'OFF'}",
                              callback_data="a_t_maint")],
        [ikb(f"🚪 Lᴇᴀᴠᴇ Pᴇɴᴀʟᴛʏ : {'ON' if gi('leave_penalty', 1) else 'OFF'}",
                              callback_data="a_t_leave"),
         ikb(f"🔔 Nᴏᴛɪғʏ : {'ON' if gi('notify_referrer', 1) else 'OFF'}",
                              callback_data="a_t_notify")],
        [ikb("✍️ Wᴇʟᴄᴏᴍᴇ Tᴇxᴛ", callback_data="a_set_welcome"),
         ikb("✍️ Rᴇᴡᴀʀᴅ Tᴇxᴛ", callback_data="a_set_rwtext")],
        [ikb("🔒 Gᴀᴛᴇ Tᴇxᴛ", callback_data="a_set_gate"),
         ikb("😔 Oᴜᴛ-ᴏғ-Sᴛᴏᴄᴋ Tᴇxᴛ", callback_data="a_set_oos")],
        [ikb("🛠 Mᴀɪɴᴛᴇɴᴀɴᴄᴇ Tᴇxᴛ", callback_data="a_set_maint"),
         ikb("👁 Pʀᴇᴠɪᴇᴡ Tᴇxᴛs", callback_data="a_set_preview")],
        [ikb(f"💎 Pʀᴇᴍɪᴜᴍ Bᴜᴛᴛᴏɴ Iᴄᴏɴs : {'ON' if gi('premium_btn_icons', 1) else 'OFF'}",
                              callback_data="a_t_pbtn")],
        [ikb("🎨 Usᴇʀ Bᴜᴛᴛᴏɴ Cᴜsᴛᴏᴍɪᴢᴇʀ", callback_data="a_btncfg")],
        [ikb("🖼 Sᴛᴀʀᴛ Pʜᴏᴛᴏ", callback_data="a_set_photo"),
         ikb("🗒 Lᴏɢ Cʜᴀɴɴᴇʟ", callback_data="a_set_log")],
        [ikb("⬅️ Bᴀᴄᴋ", callback_data="a_home")],
    ])
    await edit_or_send(update, context, txt, kb)


def button_cfg_text(key: str) -> str:
    reg = button_registry().get(key, {})
    default = reg.get("label") or STATIC_BUTTON_DEFAULTS.get(key, key)
    cfg = button_config().get(key, {})
    label = str(cfg.get("label") or default)
    emoji = str(cfg.get("emoji") or "").strip()
    style = str(cfg.get("style") or "default").lower()
    status = ("Premium" if TG_EMOJI_RE.search(emoji) else "Normal") if emoji else ("Removed" if cfg.get("remove_emoji") else "Default")
    return (f"🔘 <b>{esc(key)}</b>\n"
            f"Text: <code>{esc(strip_leading_emoji(label) if emoji or cfg.get('remove_emoji') else plain_text(label))}</code>\n"
            f"Emoji: <b>{esc(status)}</b>\n"
            f"Style: <b>{esc(style.upper())}</b>")


def button_customizer_kb():
    reg = button_registry()
    for key, label in STATIC_BUTTON_DEFAULTS.items():
        reg.setdefault(key, {"label": label, "action": key})
    save_button_registry(reg)
    cfg = button_config()
    kb = []
    for key in sorted(reg):
        label = reg[key].get("label") or key
        style = str(cfg.get(key, {}).get("style") or "default").upper()
        kb.append([ikb(f"{plain_text(label)[:28]} • {style}", callback_data=f"a_btncfg_v:{key}")])
    kb.append([ikb("♻️ Rᴇsᴇᴛ Aʟʟ", callback_data="a_btncfg_reset")])
    kb.append([ikb("⬅️ Bᴀᴄᴋ", callback_data="a_set")])
    return InlineKeyboardMarkup(kb)


async def a_button_customizer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("state", None)
    txt = ("🎨 <b>Aʟʟ Bᴜᴛᴛᴏɴ Cᴜsᴛᴏᴍɪᴢᴇʀ</b>\n"
           "━━━━━━━━━━━━━━━━━━\n"
           "User, Admin and discovered dynamic buttons are listed here. Each button has "
           "independent text, replacement emoji and style settings.\n\nChoose a button:")
    await edit_or_send(update, context, txt, button_customizer_kb())


async def a_button_customizer_view(update: Update, context: ContextTypes.DEFAULT_TYPE, key: str):
    if key not in button_registry() and key not in STATIC_BUTTON_DEFAULTS:
        return await a_button_customizer(update, context)
    txt = button_cfg_text(key) + "\n\nPremium emoji replaces the old normal emoji; it is never appended."
    kb = InlineKeyboardMarkup([
        [ikb("😀 Eᴅɪᴛ / Cʜᴀɴɢᴇ Eᴍᴏᴊɪ", callback_data=f"a_btncfg_emoji:{key}")],
        [ikb("🗑 Rᴇᴍᴏᴠᴇ Eᴍᴏᴊɪ", callback_data=f"a_btncfg_removeemoji:{key}")],
        [ikb("✍️ Eᴅɪᴛ Tᴇxᴛ", callback_data=f"a_btncfg_label:{key}"),
         ikb("🎨 Cʏᴄʟᴇ Cᴏʟᴏʀ", callback_data=f"a_btncfg_style:{key}")],
        [ikb("♻️ Rᴇsᴇᴛ Tʜɪs Bᴜᴛᴛᴏɴ", callback_data=f"a_btncfg_resetone:{key}")],
        [ikb("⬅️ Bᴀᴄᴋ", callback_data="a_btncfg")],
    ])
    await edit_or_send(update, context, txt, kb)


async def a_set_preview(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Saare editable texts ka live preview (premium emoji ke saath) alag messages me."""
    tg = update.effective_user
    u = get_user(tg.id)
    await update.callback_query.answer("👁 Preview bhej raha hoon…")
    for label, key in (("Welcome", "welcome_text"), ("Reward", "reward_text"),
                       ("Gate", "gate_text"), ("Out-of-Stock", "outofstock_text"),
                       ("Maintenance", "maintenance_text")):
        try:
            await context.bot.send_message(
                tg.id, f"👁 <b>{label} Text Preview</b>\n━━━━━━━━━━━━━━━━━━\n" + render(gs(key), u, tg),
                parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        except Exception as e:  # noqa: BLE001
            await context.bot.send_message(tg.id, f"⚠️ {label} text render fail: {esc(e)}",
                                           parse_mode=ParseMode.HTML)
    await context.bot.send_message(tg.id, "⬆️ Ye texts users ko aise hi dikhenge.",
                                   reply_markup=back_kb("a_set"))


# ─────────────────────────── USERS MANAGER ───────────────────────────

def user_card(u) -> str:
    can, need, _ = claim_state(u)
    gifts = int(scalar("SELECT COUNT(*) FROM giftclaims WHERE user_id=?", (u["user_id"],)))
    return (
        "👤 <b>Usᴇʀ Iɴғᴏ</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🏷 Name : <b>{esc(u['first_name'])}</b>\n"
        f"📛 Username : @{esc(u['username']) if u['username'] else '—'}\n"
        f"🆔 ID : <code>{u['user_id']}</code>\n"
        f"📅 Joined : <b>{fmt_ts(u['joined_at'])}</b>\n"
        f"🕒 Last Seen : <b>{fmt_ts(u['last_seen'])}</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"👥 Referrals : <b>{int(u['refs'] or 0)}</b>\n"
        f"🎟 Claims : <b>{int(u['claims'] or 0)}</b>\n"
        f"🎁 Gift Codes Used : <b>{gifts}</b>\n"
        f"🔗 Invited By : <code>{u['ref_by'] or '—'}</code>\n"
        f"✅ Verified : <b>{'Yes' if int(u['verified'] or 0) else 'No'}</b>\n"
        f"🚫 Blocked : <b>{'YES ⛔' if int(u['blocked'] or 0) else 'No'}</b>\n"
        f"🎁 Next Reward : <b>{'READY' if can else str(need) + ' refs baaki'}</b>"
    )


def user_card_kb(u):
    uid = int(u["user_id"])
    blocked = int(u["blocked"] or 0) == 1
    return InlineKeyboardMarkup([
        [ikb("✅ Uɴʙʟᴏᴄᴋ" if blocked else "🚫 Bʟᴏᴄᴋ",
                              callback_data=f"a_u_{'unblock' if blocked else 'block'}:{uid}")],
        [ikb("🎁 Gɪғᴛ Rᴇᴡᴀʀᴅ", callback_data=f"a_u_gift:{uid}"),
         ikb("➕ Aᴅᴅ Rᴇғs", callback_data=f"a_u_addref:{uid}")],
        [ikb("♻️ Rᴇsᴇᴛ Cʟᴀɪᴍs", callback_data=f"a_u_reset:{uid}"),
         ikb("✉️ Sᴇɴᴅ Mᴇssᴀɢᴇ", callback_data=f"a_u_msg:{uid}")],
        [ikb("🗑 Dᴇʟᴇᴛᴇ Usᴇʀ", callback_data=f"a_u_del:{uid}")],
        [ikb("🔍 Aɴᴏᴛʜᴇʀ Usᴇʀ", callback_data="a_u_find"),
         ikb("⬅️ Bᴀᴄᴋ", callback_data="a_users")],
    ])


async def a_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("state", None)
    txt = (
        "👥 <b>Usᴇʀs Mᴀɴᴀɢᴇʀ</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"👤 Total : <b>{int(scalar('SELECT COUNT(*) FROM users'))}</b>\n"
        f"✅ Verified : <b>{int(scalar('SELECT COUNT(*) FROM users WHERE verified=1'))}</b>\n"
        f"🚫 Blocked : <b>{int(scalar('SELECT COUNT(*) FROM users WHERE blocked=1'))}</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Kisi user ko manage karne ke liye <b>🔍 Find User</b> dabaiye aur "
        "uska ID ya @username bhejiye."
    )
    kb = InlineKeyboardMarkup([
        [ikb("🔍 Fɪɴᴅ Usᴇʀ", callback_data="a_u_find")],
        [ikb("⛔ Bʟᴏᴄᴋᴇᴅ Lɪsᴛ", callback_data="a_u_blist"),
         ikb("🆕 Lᴀᴛᴇsᴛ Usᴇʀs", callback_data="a_u_latest")],
        [ikb("📤 Exᴘᴏʀᴛ CSV", callback_data="a_exp_users")],
        [ikb("⬅️ Bᴀᴄᴋ", callback_data="a_home")],
    ])
    await edit_or_send(update, context, txt, kb)


async def send_user_card(update: Update, context: ContextTypes.DEFAULT_TYPE, uid: int):
    u = get_user(uid)
    cq = update.callback_query
    if not u:
        if cq:
            return await cq.answer("❌ User database me nahi mila.", show_alert=True)
        return await update.effective_message.reply_html("❌ User nahi mila.")
    await edit_or_send(update, context, user_card(u), user_card_kb(u))


# ─────────────────────────── GIFT CODES (ADMIN) ───────────────────────────

def gc_card(g) -> str:
    exp = "Never" if not int(g["expires_at"] or 0) else fmt_ts(g["expires_at"])
    limit = "Unlimited" if not int(g["max_uses"] or 0) else str(g["max_uses"])
    num = g["agent_number"] or "— (set karo)"
    if str(num).upper() == "POOL":
        num = "POOL (Agent Number Pool se agla number)"
    return (
        "🎟 <b>Gɪғᴛ Cᴏᴅᴇ</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🏷 Code : <code>{esc(g['code'])}</code>\n"
        f"📞 Reward Number : <b>{esc(num)}</b>\n"
        f"🔢 Used / Limit : <b>{int(g['used'] or 0)} / {limit}</b>\n"
        f"⏰ Expiry : <b>{exp}</b>\n"
        f"📌 Status : <b>{gc_status(g)}</b>\n"
        f"📅 Created : <b>{fmt_ts(g['created_at'])}</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"👤 User command : <code>/claim {esc(g['code'])}</code>"
    )


def gc_card_kb(g):
    c = g["code"]
    on = int(g["active"] or 0) == 1
    return InlineKeyboardMarkup([
        [ikb("📞 Sᴇᴛ Nᴜᴍʙᴇʀ", callback_data=f"a_gc_num:{c}"),
         ikb("🔢 Usᴀɢᴇ Lɪᴍɪᴛ", callback_data=f"a_gc_lim:{c}")],
        [ikb("⏰ Exᴘɪʀʏ", callback_data=f"a_gc_exp:{c}"),
         ikb("⛔ Dɪsᴀʙʟᴇ" if on else "✅ Aᴄᴛɪᴠᴀᴛᴇ", callback_data=f"a_gc_tog:{c}")],
        [ikb("👥 Wʜᴏ Cʟᴀɪᴍᴇᴅ", callback_data=f"a_gc_who:{c}"),
         ikb("🔁 Rᴇsᴇᴛ Usᴀɢᴇ", callback_data=f"a_gc_reset:{c}")],
        [ikb("🗑 Dᴇʟᴇᴛᴇ Cᴏᴅᴇ", callback_data=f"a_gc_del:{c}")],
        [ikb("⬅️ Bᴀᴄᴋ", callback_data="a_gc")],
    ])


async def a_giftcodes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("state", None)
    context.user_data.pop("target_gc", None)
    gcs = rows("SELECT * FROM giftcodes ORDER BY created_at DESC LIMIT 25")
    total = int(scalar("SELECT COUNT(*) FROM giftcodes"))
    active = sum(1 for g in gcs if gc_status(g) == "✅ ACTIVE")
    lines = [
        "🎟 <b>Gɪғᴛ Cᴏᴅᴇ Mᴀɴᴀɢᴇʀ</b>",
        "━━━━━━━━━━━━━━━━━━",
        f"📦 Total Codes : <b>{total}</b>  •  ✅ Active : <b>{active}</b>",
        f"🎁 Total Redemptions : <b>{int(scalar('SELECT COUNT(*) FROM giftclaims'))}</b>",
        f"🔤 Prefix : <code>{esc(gs('gc_prefix'))}</code>",
        "━━━━━━━━━━━━━━━━━━",
    ]
    kb = []
    if not gcs:
        lines.append("<i>Abhi koi gift code nahi hai. ➕ Create dabaiye.</i>")
    for g in gcs:
        lim = "∞" if not int(g["max_uses"] or 0) else g["max_uses"]
        kb.append([ikb(
            f"{gc_status(g).split()[0]} {g['code']}  ({int(g['used'] or 0)}/{lim})",
            callback_data=f"a_gc_v:{g['code']}")])
    lines.append("\n<i>Users redeem karte hain: <code>/claim CODE</code> — koi button nahi hai.</i>")
    kb.append([ikb("➕ Cʀᴇᴀᴛᴇ Rᴀɴᴅᴏᴍ Cᴏᴅᴇ", callback_data="a_gc_new"),
               ikb("✍️ Cᴜsᴛᴏᴍ Cᴏᴅᴇ", callback_data="a_gc_custom")])
    kb.append([ikb("🔤 Pʀᴇғɪx", callback_data="a_gc_prefix"),
               ikb("📤 Exᴘᴏʀᴛ CSV", callback_data="a_gc_export")])
    kb.append([ikb("⬅️ Bᴀᴄᴋ", callback_data="a_home")])
    await edit_or_send(update, context, "\n".join(lines), InlineKeyboardMarkup(kb))


async def a_gc_view(update: Update, context: ContextTypes.DEFAULT_TYPE, code: str):
    context.user_data.pop("state", None)
    g = gc_get(code)
    if not g:
        return await a_giftcodes(update, context)
    await edit_or_send(update, context, gc_card(g), gc_card_kb(g))


def gc_create(code: str, admin_id: int):
    q("""INSERT OR IGNORE INTO giftcodes(code,agent_number,max_uses,used,expires_at,active,
         created_at,created_by) VALUES(?,?,?,?,?,?,?,?)""",
      (code, None, 1, 0, 0, 1, now(), admin_id))
    return gc_get(code)


def parse_expiry(text: str):
    """'never'/'0' → 0 ; '24h' → +24h ; '7d' → +7 days ; '30' → +30 hours ;
    'DD-MM-YYYY' / 'DD-MM-YYYY HH:MM' → us waqt (UTC). None = invalid."""
    t = (text or "").strip().lower()
    if t in ("never", "0", "none", "no"):
        return 0
    m = re.fullmatch(r"(\d+)\s*([hdm]?)", t)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        mult = {"": 3600, "h": 3600, "d": 86400, "m": 60}[unit]
        return now() + n * mult
    for fmt in ("%d-%m-%Y %H:%M", "%d-%m-%Y", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return int(datetime.strptime(t, fmt).replace(tzinfo=timezone.utc).timestamp())
        except ValueError:
            continue
    return None


# ─────────────────────────── BROADCAST v2 ───────────────────────────
#  • Parallel workers + global rate limiter (Telegram-safe)
#  • RetryAfter / Forbidden / BadRequest handled per user — broadcast kabhi rukta nahi
#  • Live status message: Sent / Remaining / Failed / Total / Progress
#  • Pause ⏸ / Resume ▶️ / Stop ⏹  — resume exactly wahin se, koi resend nahi

class _Pacer:
    """Global send-rate limiter (messages per second) — workers share it."""

    def __init__(self, rate: float):
        self.interval = 1.0 / max(1.0, rate)
        self._next = 0.0
        self._lk = asyncio.Lock()

    async def wait(self):
        async with self._lk:
            t = time.monotonic()
            if self._next < t:
                self._next = t
            delay = self._next - t
            self._next += self.interval
        if delay > 0:
            await asyncio.sleep(delay)


class BroadcastJob:
    def __init__(self, admin_id, from_chat, msg_id, status_msg_id, pin=False, reply_markup=None):
        self.admin_id = admin_id
        self.from_chat = from_chat
        self.msg_id = msg_id
        self.status_msg_id = status_msg_id
        self.pin = pin
        self.reply_markup = reply_markup
        self.targets = [int(r["user_id"]) for r in rows("SELECT user_id FROM users WHERE blocked=0")]
        self.total = len(self.targets)
        self.sent = 0
        self.failed = 0
        self.blocked = 0
        self.flood_waits = 0
        self.cursor = 0                     # agla user index (resume yahin se)
        self.running = asyncio.Event()      # set = chal raha hai, clear = paused
        self.running.set()
        self.cancelled = False
        self.finished = False
        self.started_at = time.monotonic()
        self.paused_total = 0.0
        self._pause_at = None
        self._lk = asyncio.Lock()

    # ── state ──
    @property
    def processed(self):
        return self.sent + self.failed

    @property
    def remaining(self):
        return max(0, self.total - self.processed)

    @property
    def paused(self):
        return not self.running.is_set() and not self.finished

    def pause(self):
        if self.finished or self.paused:
            return
        self.running.clear()
        self._pause_at = time.monotonic()

    def resume(self):
        if self.finished or not self.paused:
            return
        if self._pause_at:
            self.paused_total += time.monotonic() - self._pause_at
            self._pause_at = None
        self.running.set()

    def stop(self):
        self.cancelled = True
        self.running.set()      # paused workers ko jagao taki wo exit kar sakein

    def elapsed(self):
        e = time.monotonic() - self.started_at - self.paused_total
        if self._pause_at:
            e -= time.monotonic() - self._pause_at
        return max(0.0, e)

    # ── render ──
    def status_text(self):
        pct = (self.processed / self.total * 100) if self.total else 100.0
        if self.finished:
            head = "⏹ <b>Bʀᴏᴀᴅᴄᴀsᴛ Sᴛᴏᴘᴘᴇᴅ</b>" if self.cancelled else "✅ <b>Bʀᴏᴀᴅᴄᴀsᴛ Cᴏᴍᴘʟᴇᴛᴇ</b>"
        elif self.paused:
            head = "⏸ <b>Bʀᴏᴀᴅᴄᴀsᴛ Pᴀᴜsᴇᴅ</b>"
        else:
            head = "📣 <b>Bʀᴏᴀᴅᴄᴀsᴛɪɴɢ…</b>"
        bar_n = int(pct // 10)
        bar = "█" * bar_n + "░" * (10 - bar_n)
        el = int(self.elapsed())
        speed = (self.processed / el) if el > 0 else 0.0
        eta = int(self.remaining / speed) if speed > 0 and not self.finished else 0
        txt = (
            f"{head}\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"📤 Sent: <b>{fnum(self.sent)}</b>\n"
            f"⏳ Remaining: <b>{fnum(self.remaining)}</b>\n"
            f"❌ Failed: <b>{fnum(self.failed)}</b>\n"
            f"📊 Total: <b>{fnum(self.total)}</b>\n"
            f"⚡ Progress: <b>{pct:.1f}%</b>\n"
            f"<code>[{bar}]</code>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"🚫 Blocked bot: <b>{fnum(self.blocked)}</b>  •  🐢 Flood waits: <b>{self.flood_waits}</b>\n"
            f"⏱ Time: <b>{el}s</b>  •  🚀 Speed: <b>{speed:.1f}/s</b>"
        )
        if not self.finished and eta:
            txt += f"  •  ⌛ ETA: <b>{eta}s</b>"
        return txt

    def controls(self):
        if self.finished:
            return InlineKeyboardMarkup([
                [ikb("📣 Nᴇᴡ Bʀᴏᴀᴅᴄᴀsᴛ", callback_data="a_bc"),
                 ikb("⬅️ Bᴀᴄᴋ", callback_data="a_home")]])
        row = ([ikb("▶️ Rᴇsᴜᴍᴇ", callback_data="a_bc_resume")]
               if self.paused else
               [ikb("⏸ Pᴀᴜsᴇ", callback_data="a_bc_pause")])
        row.append(ikb("⏹ Sᴛᴏᴘ", callback_data="a_bc_stop"))
        return InlineKeyboardMarkup([row, [ikb("🔄 Rᴇғʀᴇsʜ", callback_data="a_bc_ref")]])


BC_JOBS: dict = {}          # admin_id -> BroadcastJob


def active_broadcast(admin_id=None):
    j = BC_JOBS.get(admin_id) if admin_id is not None else None
    if j and not j.finished:
        return j
    for j in BC_JOBS.values():
        if not j.finished:
            return j
    return None


async def _bc_send_one(bot, job: BroadcastJob, uid: int) -> bool:
    """Ek user ko bhejo. True = delivered. Flood-wait par wait karke retry."""
    for attempt in range(3):
        try:
            m = await bot.copy_message(chat_id=uid, from_chat_id=job.from_chat,
                                       message_id=job.msg_id, reply_markup=job.reply_markup)
            if job.pin:
                try:
                    await bot.pin_chat_message(uid, m.message_id, disable_notification=True)
                except Exception:  # noqa: BLE001
                    pass
            return True
        except RetryAfter as e:
            job.flood_waits += 1
            await asyncio.sleep(float(e.retry_after) + 0.5)
            continue
        except TimedOut:
            await asyncio.sleep(1.0)
            continue
        except Forbidden:
            job.blocked += 1
            q("UPDATE users SET blocked=1 WHERE user_id=?", (uid,))
            return False
        except BadRequest:
            return False           # chat not found / deactivated etc.
        except Exception as e:  # noqa: BLE001
            log.warning("bc send fail %s: %s", uid, e)
            return False
    return False


async def _bc_worker(bot, job: BroadcastJob, pacer: _Pacer):
    while True:
        await job.running.wait()            # paused → yahin rukega
        if job.cancelled:
            return
        async with job._lk:                 # agla target atomically lo
            if job.cursor >= job.total:
                return
            uid = job.targets[job.cursor]
            job.cursor += 1
        await pacer.wait()
        ok = await _bc_send_one(bot, job, uid)
        if ok:
            job.sent += 1
        else:
            job.failed += 1


async def _bc_status_loop(bot, job: BroadcastJob):
    last = None
    while not job.finished:
        await asyncio.sleep(BC_STATUS_EVERY)
        txt = job.status_text()
        if txt == last:
            continue
        last = txt
        try:
            await bot.edit_message_text(chat_id=job.admin_id, message_id=job.status_msg_id,
                                        text=txt, parse_mode=ParseMode.HTML,
                                        reply_markup=job.controls())
        except RetryAfter as e:
            await asyncio.sleep(float(e.retry_after) + 0.5)
        except Exception:  # noqa: BLE001
            pass


async def refresh_bc_status(bot, job: BroadcastJob):
    try:
        await bot.edit_message_text(chat_id=job.admin_id, message_id=job.status_msg_id,
                                    text=job.status_text(), parse_mode=ParseMode.HTML,
                                    reply_markup=job.controls())
    except Exception:  # noqa: BLE001
        pass


async def run_broadcast(context: ContextTypes.DEFAULT_TYPE, job: BroadcastJob):
    bot = context.bot
    BC_JOBS[job.admin_id] = job
    pacer = _Pacer(BC_RATE)
    status_task = asyncio.create_task(_bc_status_loop(bot, job))
    workers = [asyncio.create_task(_bc_worker(bot, job, pacer))
               for _ in range(max(1, min(BC_WORKERS, max(1, job.total))))]
    try:
        await asyncio.gather(*workers, return_exceptions=True)
    finally:
        job.finished = True
        job.running.set()
        status_task.cancel()
        await refresh_bc_status(bot, job)
    await send_log(context,
                   f"📣 <b>Broadcast {'stopped' if job.cancelled else 'complete'}</b> — "
                   f"sent {job.sent}/{job.total}, failed {job.failed}, "
                   f"blocked {job.blocked}, {int(job.elapsed())}s")


async def a_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    job = active_broadcast(update.effective_user.id)
    if job:
        context.user_data.pop("state", None)
        return await edit_or_send(update, context,
                                  job.status_text() + "\n\n<i>Ek broadcast already chal raha hai — "
                                  "pehle use complete/stop kijiye.</i>", job.controls())
    context.user_data["state"] = "bc_wait"
    txt = (
        "📣 <b>Bʀᴏᴀᴅᴄᴀsᴛ</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Jo message sabko bhejna hai wo <b>abhi bhej dijiye</b> — text, photo, video, "
        "document, sticker, premium emoji, buttons wala forward — sab support hai.\n\n"
        "Bhejne ke baad confirm button aayega.\n\n"
        f"⚙️ Speed : <b>{BC_RATE:.0f} msg/s</b> • Workers : <b>{BC_WORKERS}</b>\n"
        "⏸ Pause / ▶️ Resume / ⏹ Stop — live status message par milenge.\n\n"
        "❌ Cancel ke liye /cancel"
    )
    await edit_or_send(update, context, txt, back_kb())


# ════════════════════════════════════════════════════════════════════════
#  TOOLS / EXPORT / SUB-ADMINS
# ════════════════════════════════════════════════════════════════════════

async def a_tools(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("state", None)
    txt = (
        "🛠 <b>Tᴏᴏʟs & Bᴀᴄᴋᴜᴘ</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "• Users CSV export\n• Agent numbers export\n• Full database backup\n"
        "• <b>DB Restore</b> : backup wali <code>.db</code> file yahan bhej dijiye\n"
        "• Join-cache clear (force re-check)\n• Waitlist ko manually notify"
    )
    kb = InlineKeyboardMarkup([
        [ikb("📤 Usᴇʀs CSV", callback_data="a_exp_users"),
         ikb("📤 Nᴜᴍʙᴇʀs TXT", callback_data="a_exp_codes")],
        [ikb("💾 DB Bᴀᴄᴋᴜᴘ", callback_data="a_backup")],
        [ikb("♻️ Cʟᴇᴀʀ Jᴏɪɴ Cᴀᴄʜᴇ", callback_data="a_clr_cache"),
         ikb("🔔 Nᴏᴛɪғʏ Wᴀɪᴛʟɪsᴛ", callback_data="a_wl_notify")],
        [ikb("⬅️ Bᴀᴄᴋ", callback_data="a_home")],
    ])
    await edit_or_send(update, context, txt, kb)


async def a_admins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("state", None)
    lines = ["👮 <b>Aᴅᴍɪɴs</b>", "━━━━━━━━━━━━━━━━━━",
             f"👑 Owner : <code>{OWNER_ID}</code>"]
    kb = []
    for x in sorted(EXTRA_ADMINS):
        lines.append(f"⭐ ENV Admin : <code>{x}</code>")
    for r in rows("SELECT * FROM admins ORDER BY added_at"):
        u = get_user(int(r["user_id"]))
        nm = esc(u["first_name"]) if u else "—"
        lines.append(f"🛡 {nm} : <code>{r['user_id']}</code>")
        kb.append([ikb(f"🗑 Rᴇᴍᴏᴠᴇ {r['user_id']}",
                                        callback_data=f"a_adm_del:{r['user_id']}")])
    kb.append([ikb("➕ Aᴅᴅ Sᴜʙ-Aᴅᴍɪɴ", callback_data="a_adm_add")])
    kb.append([ikb("⬅️ Bᴀᴄᴋ", callback_data="a_home")])
    await edit_or_send(update, context, "\n".join(lines), InlineKeyboardMarkup(kb))


async def a_claim_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cl = rows("""SELECT c.code, c.kind, c.claimed_at, u.first_name, u.user_id
                 FROM claims c LEFT JOIN users u ON u.user_id=c.user_id
                 ORDER BY c.id DESC LIMIT 15""")
    lines = ["🎟 <b>Lᴀsᴛ 15 Cʟᴀɪᴍs</b>\n"]
    if not cl:
        lines.append("<i>Abhi koi claim nahi hua.</i>")
    tags = {"bonus": "🎁", "referral": "👥", "gift": "👑", "giftcode": "🎟"}
    for c in cl:
        tag = tags.get(c["kind"], "•")
        lines.append(f"{tag} <b>{esc(c['first_name'])}</b> (<code>{c['user_id']}</code>)\n"
                     f"   <code>{esc(c['code'])}</code> • <i>{fmt_ts(c['claimed_at'])}</i>")
    kb = InlineKeyboardMarkup([
        [ikb("🔄 Rᴇғʀᴇsʜ", callback_data="a_logs")],
        [ikb("⬅️ Bᴀᴄᴋ", callback_data="a_home")],
    ])
    await edit_or_send(update, context, "\n".join(lines), kb)


async def a_top_refs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    top = rows("SELECT user_id,first_name,username,refs,claims FROM users "
               "ORDER BY refs DESC, claims DESC LIMIT 20")
    lines = ["🏆 <b>Tᴏᴘ 20 Rᴇғᴇʀʀᴇʀs</b>\n"]
    for i, r in enumerate(top, 1):
        lines.append(f"<b>{i}.</b> {esc(r['first_name'])} (<code>{r['user_id']}</code>) — "
                     f"👥 {int(r['refs'] or 0)} • 🎟 {int(r['claims'] or 0)}")
    await edit_or_send(update, context, "\n".join(lines), back_kb())


# ════════════════════════════════════════════════════════════════════════
#  ASK HELPER (admin text inputs)
# ════════════════════════════════════════════════════════════════════════

PREMIUM_NOTE = ("\n\n💎 <i>Premium emoji seedha type/paste karo — as-is save honge "
                "(HTML tags &lt;b&gt; &lt;i&gt; bhi chalte hain).</i>")

ASK_TEXT = {
    "rw_add": ("➕ <b>Aᴅᴅ Aɢᴇɴᴛ Nᴜᴍʙᴇʀs</b>\n\n"
               "Har line par ek number bhejiye.\n"
               "<b>Koi bhi format chalega:</b>\n\n"
               "<code>9876543210\n"
               "+919876543210\n"
               "919876543210\n"
               "+91 98765 43210</code>\n\n"
               "Bulk add ke liye ek baar me sab bhejiye 👆\n\n"
               "❌ /cancel"),
    "set_refs": ("🔗 <b>Rᴇғᴇʀʀᴀʟs ᴘᴇʀ Rᴇᴡᴀʀᴅ</b>\n\nEk number bhejiye (jaise <code>1</code> "
                 "ya <code>3</code>).\nMatlab: itne referral = 1 Agent Number.\n\n❌ /cancel"),
    "set_welcome": ("✍️ <b>Wᴇʟᴄᴏᴍᴇ Tᴇxᴛ</b>\n\nNaya text bhejiye.\nVariables: {name} {per} {refs} "
                    "{claims} {stock} {next} {dev}" + PREMIUM_NOTE + "\n\n❌ /cancel"),
    "set_rwtext": ("✍️ <b>Rᴇᴡᴀʀᴅ Mᴇssᴀɢᴇ Tᴇxᴛ</b>\n\nReward ke sath jo message jayega wo "
                   "bhejiye.\nVariables: {name} {refs} {claims} {dev}" + PREMIUM_NOTE + "\n\n❌ /cancel"),
    "set_gate": ("🔒 <b>Jᴏɪɴ Sᴄʀᴇᴇɴ Tᴇxᴛ</b>\n\nChannel-join screen ka message bhejiye."
                 + PREMIUM_NOTE + "\n\n❌ /cancel"),
    "set_oos": ("😔 <b>Oᴜᴛ-ᴏғ-Sᴛᴏᴄᴋ Tᴇxᴛ</b>\n\nRewards khatam hone par jo message jayega."
                + PREMIUM_NOTE + "\n\n❌ /cancel"),
    "set_maint": ("🛠 <b>Mᴀɪɴᴛᴇɴᴀɴᴄᴇ Tᴇxᴛ</b>\n\nMaintenance ON hone par users ko jo message "
                  "dikhega wo bhejiye." + PREMIUM_NOTE + "\n\n❌ /cancel"),
    "set_photo": ("🖼 <b>Wᴇʟᴄᴏᴍᴇ Iᴍᴀɢᴇ Sᴇᴛ Kᴀʀᴏ</b>\n\n"
                  "📤 Ek photo bhejiye — ye welcome message ke saath dikhegi.\n"
                  "🔗 Ya image URL bhejiye.\n"
                  "🗑 Hatane ke liye <code>clear</code> type karo.\n\n"
                  "❌ /cancel"),
    "set_log": ("🗒 <b>Lᴏɢ Cʜᴀɴɴᴇʟ</b>\n\nChannel ID bhejiye (<code>-100...</code>) — bot "
                "wahan admin ho.\nHatane ke liye <code>clear</code>.\n\n❌ /cancel"),
    "rw_btn": ("🔘 <b>Rᴇᴡᴀʀᴅ Bᴜᴛᴛᴏɴs</b>\n\nHar line par: <code>Button Text - https://link</code>\n\n"
               "<b>Example :</b>\n<code>🌐 Open Site - https://google.com\n"
               "📢 Updates - https://t.me/YourChannel</code>\n\n"
               "💎 Button text me premium emoji bhi chalega.\n"
               "Hatane ke liye <code>clear</code> bhejiye.\n\n❌ /cancel"),
    "ch_add": ("➕ <b>Aᴅᴅ Fᴏʀᴄᴇ-Jᴏɪɴ Cʜᴀɴɴᴇʟ</b>\n\n1️⃣ Bot ko channel me <b>admin</b> banaiye "
               "(Invite Users via Link ✅)\n2️⃣ Us channel ka koi message yahan <b>forward</b> "
               "kijiye, ya channel ID bhejiye (<code>-100xxxxxxxxxx</code>) ya @username\n\n"
               "Private channel ke liye bot khud <b>join-request link</b> bana lega, aur "
               "request aate hi user verify ho jayega ⚡\n"
               "Add hone ke baad ⚙️ Manage se naam / link / button text badal sakte hain.\n\n❌ /cancel"),
    "ch_name": ("✏️ <b>Eᴅɪᴛ Cʜᴀɴɴᴇʟ Nᴀᴍᴇ</b>\n\nNaya display name bhejiye (join button aur "
                "list me yahi dikhega)." + PREMIUM_NOTE + "\n\n❌ /cancel"),
    "ch_link": ("🔗 <b>Cʜᴀɴɢᴇ Cʜᴀɴɴᴇʟ Lɪɴᴋ</b>\n\nNaya link bhejiye:\n"
                "• <code>https://t.me/+AbCdEf…</code> (private invite / join-request link)\n"
                "• <code>https://t.me/username</code> ya <code>@username</code> (public)\n\n"
                "<code>auto</code> bhejne par bot khud naya join-request link bana dega.\n\n❌ /cancel"),
    "ch_btn": ("🔘 <b>Jᴏɪɴ Bᴜᴛᴛᴏɴ Tᴇxᴛ</b>\n\nIs channel ke join button ka text bhejiye "
               "(jaise <code>📢 Join Updates Channel</code>).\n"
               "Default par wapas jane ke liye <code>clear</code>." + PREMIUM_NOTE + "\n\n❌ /cancel"),
    "u_find": ("🔍 <b>Fɪɴᴅ Usᴇʀ</b>\n\nUser ka ID ya @username bhejiye.\n\n❌ /cancel"),
    "u_msg": ("✉️ <b>Sᴇɴᴅ Mᴇssᴀɢᴇ</b>\n\nJo message us user ko bhejna hai wo bhejiye.\n\n❌ /cancel"),
    "u_addref": ("➕ <b>Aᴅᴅ Rᴇғᴇʀʀᴀʟs</b>\n\nKitne referral add karne hain? Number bhejiye "
                 "(minus bhi chalega, jaise <code>-2</code>).\n\n❌ /cancel"),
    "adm_add": ("➕ <b>Aᴅᴅ Sᴜʙ-Aᴅᴍɪɴ</b>\n\nUser ka Telegram ID bhejiye.\n\n❌ /cancel"),
    "gc_num": ("📞 <b>Gɪғᴛ Cᴏᴅᴇ Rᴇᴡᴀʀᴅ Nᴜᴍʙᴇʀ</b>\n\nIs code par jo Agent Number milega wo "
               "bhejiye (jaise <code>9876543210</code>).\n\n"
               "Ya <code>pool</code> bhejiye → har claim par Agent Number Pool se agla number "
               "jayega.\n\n❌ /cancel"),
    "gc_lim": ("🔢 <b>Usᴀɢᴇ Lɪᴍɪᴛ</b>\n\nKitne users ye code use kar sakte hain? Number bhejiye.\n"
               "<code>0</code> = unlimited.\n\n❌ /cancel"),
    "gc_exp": ("⏰ <b>Exᴘɪʀʏ</b>\n\nBhejiye:\n• <code>24h</code> / <code>7d</code> / <code>30</code> (hours)\n"
               "• <code>25-12-2026</code> ya <code>25-12-2026 18:30</code> (UTC)\n"
               "• <code>never</code> = kabhi expire nahi\n\n❌ /cancel"),
    "gc_custom": ("✍️ <b>Cᴜsᴛᴏᴍ Gɪғᴛ Cᴏᴅᴇ</b>\n\nApna code bhejiye (4–32 letters/digits, "
                  "jaise <code>DIWALI2026</code>). Auto uppercase hoga.\n\n❌ /cancel"),
    "gc_prefix": ("🔤 <b>Cᴏᴅᴇ Pʀᴇғɪx</b>\n\nRandom codes ka prefix bhejiye (jaise "
                  "<code>EARNINGZONE</code> → EARNINGZONE8363).\n\n❌ /cancel"),
}


async def ask(update: Update, context: ContextTypes.DEFAULT_TYPE, state: str, back="a_home"):
    context.user_data["state"] = state
    await edit_or_send(update, context, ASK_TEXT.get(state, "Value bhejiye:"), back_kb(back))


# ════════════════════════════════════════════════════════════════════════
#  CALLBACK ROUTER
# ════════════════════════════════════════════════════════════════════════

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    data = cq.data or ""
    uid = update.effective_user.id
    u = get_user(uid)
    if not u:
        register_user(update.effective_user)
        u = get_user(uid)

    if int(u["blocked"] or 0) == 1 and not is_admin(uid):
        return await cq.answer("🚫 Aap blocked hain.", show_alert=True)

    if data.startswith("a_") and not is_admin(uid):
        return await cq.answer("🚫 Sirf admin ke liye!", show_alert=True)

    arg = None
    if ":" in data:
        data, arg = data.split(":", 1)

    # Navigation callbacks render through fresh_html/edit_or_send. Action/control callbacks
    # keep their source message until their action decides the next screen.

    # ───── USER ─────
    if data == "menu":
        if await require_join(update, context):
            return
        await cq.answer()
        return await show_menu(update, context, edit=True)

    if data == "verify":
        missing = await pending_channels(context.bot, uid, force_fresh=True)
        if missing:
            names = ", ".join((c["title"] or "Channel") for c in missing)
            await cq.answer(
                f"❌ Not joined yet: {names}\n\nPlease join the channel"
                f"{'s' if len(missing) > 1 else ''} above, then tap Continue.", show_alert=True)
            return await show_gate(update, context, missing, edit=True)
        await cq.answer("✅ Verified!")
        if int(u["verified"] or 0) == 0:
            q("UPDATE users SET verified=1 WHERE user_id=?", (uid,))
            await credit_referral(context, uid)
        q("UPDATE users SET gate_msg=NULL WHERE user_id=?", (uid,))
        return await show_menu(update, context, edit=True)

    if data == "claim":
        return await do_claim(update, context)
    if data == "refer":
        if await require_join(update, context):
            return
        await cq.answer()
        return await show_refer(update, context, edit=True)
    if data == "top":
        if await require_join(update, context):
            return
        await cq.answer()
        return await show_top(update, context, edit=True)

    # ───── ADMIN ─────
    if data not in ("a_set_preview",):
        await cq.answer()

    if data == "a_home":
        return await admin_home(update, context)
    if data == "a_stats":
        return await a_stats(update, context)
    if data == "a_rw":
        return await a_rewards(update, context)
    if data == "a_rw_add":
        return await ask(update, context, "rw_add", "a_rw")
    if data == "a_rw_list":
        return await a_rw_list(update, context)
    if data == "a_rw_btn":
        return await ask(update, context, "rw_btn", "a_rw")
    if data == "a_rw_del":
        q("DELETE FROM rewards WHERE id=?", (int(arg),))
        return await a_rw_list(update, context)
    if data == "a_rw_clrused":
        if gs("reward_mode") == "unique":
            q("DELETE FROM rewards WHERE used_by IS NOT NULL")
        else:
            q("UPDATE rewards SET used_by=NULL, used_at=NULL")
        return await a_rewards(update, context)
    if data == "a_rw_clrall":
        q("DELETE FROM rewards")
        return await a_rewards(update, context)
    if data == "a_rw_mode":
        order = ["unique", "shared", "cyclic"]
        cur = gs("reward_mode")
        nxt = order[(order.index(cur) + 1) % len(order)] if cur in order else "unique"
        ss("reward_mode", nxt)
        return await a_rewards(update, context)

    # ── channels ──
    if data == "a_ch":
        return await a_channels(update, context)
    if data == "a_ch_add":
        return await ask(update, context, "ch_add", "a_ch")
    if data == "a_ch_v":
        return await a_channel_view(update, context, int(arg))
    if data in ("a_ch_name", "a_ch_link", "a_ch_btn"):
        context.user_data["target_ch"] = int(arg)
        return await ask(update, context, data[2:], f"a_ch_v:{arg}")
    if data == "a_ch_type":
        c = get_channel(arg)
        if c:
            q("UPDATE channels SET is_private=? WHERE chat_id=?",
              (0 if int(c["is_private"] or 0) else 1, int(arg)))
            _join_cache.clear()
        return await a_channel_view(update, context, int(arg))
    if data == "a_ch_auto":
        q("UPDATE channels SET invite_link=NULL, link_manual=0 WHERE chat_id=?", (int(arg),))
        link = await ensure_link(context.bot, get_channel(arg))
        _join_cache.clear()
        if not link:
            await cq.answer("⚠️ Link nahi bana — bot ko 'Invite Users via Link' permission dijiye.",
                            show_alert=True)
        return await a_channel_view(update, context, int(arg))
    if data == "a_ch_sync":
        try:
            title, link, _ = await add_channel_by_id(context, int(arg))
            await cq.answer(f"🔄 Synced: {title}", show_alert=False)
        except Exception as e:  # noqa: BLE001
            await cq.answer(f"❌ Sync fail: {str(e)[:150]}", show_alert=True)
        return await a_channel_view(update, context, int(arg))
    if data == "a_ch_del":
        q("DELETE FROM channels WHERE chat_id=?", (int(arg),))
        _join_cache.clear()
        return await a_channels(update, context)
    if data == "a_ch_relink":
        for c in all_channels():
            if int(c["link_manual"] or 0):
                continue          # admin ke manual links ko mat chhedo
            q("UPDATE channels SET invite_link=NULL WHERE chat_id=?", (c["chat_id"],))
            await ensure_link(context.bot, get_channel(c["chat_id"]))
        _join_cache.clear()
        return await a_channels(update, context)

    # ── settings ──
    if data == "a_set":
        return await a_settings(update, context)
    if data == "a_set_preview":
        return await a_set_preview(update, context)
    if data == "a_btncfg":
        return await a_button_customizer(update, context)
    if data == "a_btncfg_v":
        return await a_button_customizer_view(update, context, arg)
    if data in ("a_btncfg_emoji", "a_btncfg_label"):
        if arg not in button_registry() and arg not in STATIC_BUTTON_DEFAULTS:
            return await a_button_customizer(update, context)
        context.user_data["target_button"] = arg
        state = "btn_emoji" if data.endswith("emoji") else "btn_label"
        prompt = ("😀 <b>Bᴜᴛᴛᴏɴ Eᴍᴏᴊɪ</b>\n\nNormal ya Premium emoji bhejiye. "
                  "Default par lautne ke liye <code>clear</code>."
                  if state == "btn_emoji" else
                  "✍️ <b>Bᴜᴛᴛᴏɴ Tᴇxᴛ</b>\n\nNaya text bhejiye (emoji ke bina). "
                  "Default par lautne ke liye <code>clear</code>.")
        context.user_data["state"] = state
        return await edit_or_send(update, context, prompt, back_kb(f"a_btncfg_v:{arg}"))
    if data == "a_btncfg_removeemoji":
        cfg = button_config()
        item = cfg.setdefault(arg, {})
        item.pop("emoji", None)
        item["remove_emoji"] = True
        save_button_config(cfg)
        return await a_button_customizer_view(update, context, arg)
    if data == "a_btncfg_style":
        if arg in button_registry() or arg in STATIC_BUTTON_DEFAULTS:
            cfg = button_config()
            item = cfg.setdefault(arg, {})
            cur = str(item.get("style") or "default").lower()
            item["style"] = BUTTON_STYLES[(BUTTON_STYLES.index(cur) + 1) % len(BUTTON_STYLES)] if cur in BUTTON_STYLES else "default"
            save_button_config(cfg)
        return await a_button_customizer_view(update, context, arg)
    if data == "a_btncfg_resetone":
        cfg = button_config()
        cfg.pop(arg, None)
        save_button_config(cfg)
        return await a_button_customizer_view(update, context, arg)
    if data == "a_btncfg_reset":
        save_button_config({})
        return await a_button_customizer(update, context)
    if data in ("a_set_refs", "a_set_welcome", "a_set_rwtext", "a_set_gate",
                "a_set_oos", "a_set_maint", "a_set_photo", "a_set_log"):
        return await ask(update, context, data[2:], "a_set")

    if data == "a_t_force":
        ss("force_join", 0 if gi("force_join", 1) else 1)
        _join_cache.clear()
        return await a_channels(update, context)
    if data == "a_t_approve":
        ss("auto_approve", 0 if gi("auto_approve", 1) else 1)
        return await a_channels(update, context)
    if data == "a_t_bonus":
        ss("bonus_enabled", 0 if gi("bonus_enabled", 1) else 1)
        return await a_settings(update, context)
    if data == "a_t_maint":
        new = 0 if maintenance_on() else 1
        ss("maintenance", new)
        await cq.answer("🛠 Maintenance ON — ab har user ka har button/command block hai."
                        if new else "✅ Maintenance OFF — bot sabke liye wapas live hai.",
                        show_alert=True)
        return await a_settings(update, context)
    if data == "a_t_leave":
        ss("leave_penalty", 0 if gi("leave_penalty", 1) else 1)
        return await a_settings(update, context)
    if data == "a_t_notify":
        ss("notify_referrer", 0 if gi("notify_referrer", 1) else 1)
        return await a_settings(update, context)
    if data == "a_t_pbtn":
        ss("premium_btn_icons", 0 if gi("premium_btn_icons", 1) else 1)
        return await a_settings(update, context)

    # ── gift codes ──
    if data == "a_gc":
        return await a_giftcodes(update, context)
    if data == "a_gc_v":
        return await a_gc_view(update, context, arg)
    if data == "a_gc_new":
        g = gc_create(gc_generate(), uid)
        context.user_data["target_gc"] = g["code"]
        context.user_data["state"] = "gc_num"
        return await edit_or_send(
            update, context,
            f"✅ <b>Naya Gift Code bana:</b> <code>{esc(g['code'])}</code>\n\n"
            + ASK_TEXT["gc_num"], back_kb(f"a_gc_v:{g['code']}"))
    if data == "a_gc_custom":
        return await ask(update, context, "gc_custom", "a_gc")
    if data == "a_gc_prefix":
        return await ask(update, context, "gc_prefix", "a_gc")
    if data in ("a_gc_num", "a_gc_lim", "a_gc_exp"):
        context.user_data["target_gc"] = arg
        return await ask(update, context, data[2:], f"a_gc_v:{arg}")
    if data == "a_gc_tog":
        g = gc_get(arg)
        if g:
            if not g["agent_number"] and int(g["active"] or 0) == 0:
                await cq.answer("⚠️ Pehle reward number set kijiye.", show_alert=True)
            else:
                q("UPDATE giftcodes SET active=? WHERE code=?", (0 if int(g["active"] or 0) else 1, arg))
        return await a_gc_view(update, context, arg)
    if data == "a_gc_reset":
        q("UPDATE giftcodes SET used=0 WHERE code=?", (arg,))
        q("DELETE FROM giftclaims WHERE code=?", (arg,))
        await cq.answer("🔁 Usage reset — sab users dobara claim kar sakte hain.", show_alert=True)
        return await a_gc_view(update, context, arg)
    if data == "a_gc_del":
        q("DELETE FROM giftcodes WHERE code=?", (arg,))
        q("DELETE FROM giftclaims WHERE code=?", (arg,))
        return await a_giftcodes(update, context)
    if data == "a_gc_who":
        cl = rows("""SELECT gc.user_id, gc.number, gc.ts, u.first_name FROM giftclaims gc
                     LEFT JOIN users u ON u.user_id=gc.user_id WHERE gc.code=?
                     ORDER BY gc.ts DESC LIMIT 30""", (arg,))
        lines = [f"👥 <b>Cʟᴀɪᴍs • {esc(arg)}</b>\n"]
        if not cl:
            lines.append("<i>Abhi kisi ne claim nahi kiya.</i>")
        for c in cl:
            lines.append(f"• {esc(c['first_name'])} (<code>{c['user_id']}</code>) → "
                         f"<code>{esc(c['number'])}</code> • <i>{fmt_ts(c['ts'])}</i>")
        return await edit_or_send(update, context, "\n".join(lines), back_kb(f"a_gc_v:{arg}"))
    if data == "a_gc_export":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["code", "agent_number", "used", "max_uses", "expires_at", "active", "status", "created_at"])
        for g in rows("SELECT * FROM giftcodes ORDER BY created_at"):
            w.writerow([g["code"], g["agent_number"], g["used"], g["max_uses"],
                        fmt_ts(g["expires_at"]) if g["expires_at"] else "never",
                        g["active"], plain_text(gc_status(g)), fmt_ts(g["created_at"])])
        bio = io.BytesIO(buf.getvalue().encode("utf-8"))
        bio.name = "gift_codes.csv"
        await context.bot.send_document(uid, bio, filename="gift_codes.csv",
                                        caption="📤 Gift codes export")
        return

    # ── users ──
    if data == "a_users":
        return await a_users(update, context)
    if data == "a_u_find":
        return await ask(update, context, "u_find", "a_users")
    if data == "a_u_blist":
        bl = rows("SELECT user_id,first_name FROM users WHERE blocked=1 LIMIT 30")
        lines = ["⛔ <b>Bʟᴏᴄᴋᴇᴅ Usᴇʀs</b>\n"]
        kb = []
        if not bl:
            lines.append("<i>Koi blocked user nahi.</i>")
        for r in bl:
            lines.append(f"• {esc(r['first_name'])} — <code>{r['user_id']}</code>")
            kb.append([ikb(f"✅ Uɴʙʟᴏᴄᴋ {r['user_id']}",
                                            callback_data=f"a_u_unblock:{r['user_id']}")])
        kb.append([ikb("⬅️ Bᴀᴄᴋ", callback_data="a_users")])
        return await edit_or_send(update, context, "\n".join(lines), InlineKeyboardMarkup(kb))
    if data == "a_u_latest":
        lt = rows("SELECT user_id,first_name,refs,claims FROM users "
                  "ORDER BY joined_at DESC LIMIT 15")
        lines = ["🆕 <b>Lᴀᴛᴇsᴛ Usᴇʀs</b>\n"]
        for r in lt:
            lines.append(f"• {esc(r['first_name'])} — <code>{r['user_id']}</code> "
                         f"(👥 {int(r['refs'] or 0)} • 🎟 {int(r['claims'] or 0)})")
        return await edit_or_send(update, context, "\n".join(lines), back_kb("a_users"))
    if data == "a_u_block":
        q("UPDATE users SET blocked=1 WHERE user_id=?", (int(arg),))
        return await send_user_card(update, context, int(arg))
    if data == "a_u_unblock":
        q("UPDATE users SET blocked=0 WHERE user_id=?", (int(arg),))
        return await send_user_card(update, context, int(arg))
    if data == "a_u_reset":
        q("UPDATE users SET claims=0 WHERE user_id=?", (int(arg),))
        return await send_user_card(update, context, int(arg))
    if data == "a_u_del":
        q("DELETE FROM users WHERE user_id=?", (int(arg),))
        return await a_users(update, context)
    if data == "a_u_addref":
        context.user_data["target"] = int(arg)
        return await ask(update, context, "u_addref", "a_users")
    if data == "a_u_msg":
        context.user_data["target"] = int(arg)
        return await ask(update, context, "u_msg", "a_users")
    if data == "a_u_gift":
        tid = int(arg)
        r = take_reward(tid, "gift")
        if not r:
            return await cq.answer("❌ Stock khatam — pehle numbers add kijiye.", show_alert=True)
        code = str(r["code"])
        try:
            await context.bot.send_message(
                tid,
                f"🎁 <b>Aᴅᴍɪɴ Gɪғᴛ — Aɢᴇɴᴛ Nᴜᴍʙᴇʀ!</b>\n\n"
                f"┌─────────────────────────┐\n"
                f"│  📱  <code>{esc(code)}</code>\n"
                f"└─────────────────────────┘\n\n"
                f"📲 WhatsApp pe contact karo aur apna <b>Google Map Rating task</b> lo!",
                parse_mode=ParseMode.HTML, reply_markup=reward_buttons_kb(code))
        except Exception:  # noqa: BLE001
            pass
        await cq.answer("🎁 Gift bhej diya!")
        return await send_user_card(update, context, tid)

    # ── broadcast ──
    if data == "a_bc":
        return await a_broadcast(update, context)
    if data in ("a_bc_pause", "a_bc_resume", "a_bc_stop", "a_bc_ref"):
        job = active_broadcast(uid) or BC_JOBS.get(uid)
        if not job:
            return await cq.answer("ℹ️ Koi active broadcast nahi hai.", show_alert=True)
        if data == "a_bc_pause":
            job.pause()
            await cq.answer("⏸ Paused — koi naya message nahi jayega.")
        elif data == "a_bc_resume":
            job.resume()
            await cq.answer("▶️ Resumed — wahin se continue.")
        elif data == "a_bc_stop":
            job.stop()
            await cq.answer("⏹ Broadcast stop ho raha hai…", show_alert=True)
        return await refresh_bc_status(context.bot, job)
    if data == "a_bc_addbtn":
        if not context.user_data.get("bc"):
            return await cq.answer("❌ Pehle broadcast message bhejiye.", show_alert=True)
        context.user_data["state"] = "bc_btn"
        return await edit_or_send(
            update, context,
            "🔘 <b>Bʀᴏᴀᴅᴄᴀsᴛ Bᴜᴛᴛᴏɴ</b>\n\n"
            "Format: <code>Button Text - https://link</code>\n\n"
            "Example: <code>🌐 Visit Now - https://example.com</code>\n\n❌ /cancel",
            back_kb("a_home"))
    if data in ("a_bc_go", "a_bc_pin"):
        bc = context.user_data.get("bc")
        if not bc:
            return await cq.answer("❌ Message expire ho gaya, dobara try kijiye.",
                                   show_alert=True)
        if active_broadcast(uid):
            return await cq.answer("⚠️ Ek broadcast already chal raha hai.", show_alert=True)
        context.user_data.pop("state", None)
        context.user_data.pop("bc", None)
        bc_btn = context.user_data.pop("bc_button", None)
        markup = None
        if bc_btn:
            markup = InlineKeyboardMarkup([[url_btn_from_cfg(bc_btn)]])
        job = BroadcastJob(uid, bc[0], bc[1], cq.message.message_id,
                           pin=(data == "a_bc_pin"), reply_markup=markup)
        try:
            await cq.edit_message_text(job.status_text(), parse_mode=ParseMode.HTML,
                                       reply_markup=job.controls())
        except BadRequest:
            pass
        context.application.create_task(run_broadcast(context, job))
        return

    if data == "a_top":
        return await a_top_refs(update, context)
    if data == "a_logs":
        return await a_claim_logs(update, context)
    if data == "a_tools":
        return await a_tools(update, context)
    if data == "a_admins":
        return await a_admins(update, context)
    if data == "a_adm_add":
        return await ask(update, context, "adm_add", "a_admins")
    if data == "a_adm_del":
        q("DELETE FROM admins WHERE user_id=?", (int(arg),))
        return await a_admins(update, context)

    if data == "a_clr_cache":
        _join_cache.clear()
        return await cq.answer("♻️ Join cache clear ho gaya!", show_alert=True)
    if data == "a_wl_notify":
        n = await notify_waitlist(context)
        return await cq.answer(f"🔔 {n} users ko notify kiya.", show_alert=True)
    if data == "a_exp_users":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["user_id", "first_name", "username", "ref_by", "refs", "claims",
                    "verified", "blocked", "joined_at"])
        for r in rows("SELECT * FROM users ORDER BY joined_at"):
            w.writerow([r["user_id"], r["first_name"], r["username"], r["ref_by"],
                        r["refs"], r["claims"], r["verified"], r["blocked"],
                        fmt_ts(r["joined_at"])])
        bio = io.BytesIO(buf.getvalue().encode("utf-8"))
        bio.name = "users.csv"
        await context.bot.send_document(uid, bio, filename="users.csv",
                                        caption=f"📤 Users export • by {DEV_NAME}")
        return
    if data == "a_exp_codes":
        lines = []
        for r in rows("SELECT * FROM rewards ORDER BY id"):
            lines.append(f"{r['code']} | {'USED by ' + str(r['used_by']) if r['used_by'] else 'FREE'}")
        bio = io.BytesIO(("\n".join(lines) or "empty").encode("utf-8"))
        bio.name = "reward_codes.txt"
        await context.bot.send_document(uid, bio, filename="reward_codes.txt",
                                        caption=f"📤 Reward codes • by {DEV_NAME}")
        return
    if data == "a_backup":
        try:
            with _lock:
                _conn.commit()
                _conn.execute("PRAGMA wal_checkpoint(FULL)")
            with open(DB_PATH, "rb") as f:
                bio = io.BytesIO(f.read())
            fname = f"bot_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
            bio.name = fname
            await context.bot.send_document(
                uid, bio, filename=fname,
                caption="💾 <b>Database Backup</b>\n\nYe file safe rakhein. "
                        "Restore karne ke liye isi file ko bot me wapas bhej dijiye.",
                parse_mode=ParseMode.HTML)
        except Exception as e:  # noqa: BLE001
            await context.bot.send_message(uid, f"❌ Backup fail: {esc(e)}",
                                           parse_mode=ParseMode.HTML)
        return
    if data == "a_restore_yes":
        file_id = context.user_data.pop("restore_file_id", None)
        context.user_data.pop("state", None)
        if not file_id:
            return await cq.answer("❌ File nahi mili — dobara .db file bhejiye.", show_alert=True)
        tmp = DB_PATH + ".restore"
        try:
            f = await context.bot.get_file(file_id)
            await f.download_to_drive(tmp)
            db_restore_from(tmp)
        except Exception as e:  # noqa: BLE001
            if os.path.exists(tmp):
                os.remove(tmp)
            return await context.bot.send_message(uid, f"❌ Restore fail: {esc(e)}",
                                                  parse_mode=ParseMode.HTML)
        return await edit_or_send(
            update, context,
            "✅ <b>Database restore ho gaya!</b>\n\n"
            "Bot ab naye data ke saath kaam karega. "
            f"Purana DB <code>{esc(os.path.basename(DB_PATH))}.bak</code> me safe hai.",
            back_kb("a_tools"))


# ════════════════════════════════════════════════════════════════════════
#  STATE ROUTER (admin text / media inputs)
# ════════════════════════════════════════════════════════════════════════

async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    tg = update.effective_user
    if not msg or not tg or not is_user_interaction(update):
        return
    u = get_user(tg.id)
    if not u:
        register_user(tg)
        u = get_user(tg.id)
    else:
        touch_user(tg)

    state = context.user_data.get("state")

    # ── DB RESTORE (admin .db file upload, jab koi aur state active na ho) ──
    if (not state or state == "db_restore_confirm") and msg.document and is_admin(tg.id):
        fname = (msg.document.file_name or "").lower()
        if fname.endswith((".db", ".sqlite", ".sqlite3")):
            context.user_data["state"] = "db_restore_confirm"
            context.user_data["restore_file_id"] = msg.document.file_id
            return await msg.reply_html(
                "⚠️ <b>DB Rᴇsᴛᴏʀᴇ Cᴏɴғɪʀᴍ</b>\n\n"
                f"📄 File: <code>{esc(msg.document.file_name)}</code>\n\n"
                "Kya aap sure hain? <b>Current data replace ho jayega.</b>\n"
                "Ye action undo nahi hoga! (purane DB ki ek .bak copy server pe rahegi)",
                reply_markup=InlineKeyboardMarkup([
                    [ikb("✅ Yᴇs, Rᴇsᴛᴏʀᴇ", callback_data="a_restore_yes")],
                    [ikb("❌ Cᴀɴᴄᴇʟ", callback_data="a_home")],
                ]))

    if not state:
        if int(u["blocked"] or 0) == 1:
            return
        if await require_join(update, context):
            return
        return await show_menu(update, context)

    if not is_admin(tg.id):
        context.user_data.pop("state", None)
        return

    text = (msg.text or msg.caption or "").strip()          # plain (IDs, numbers, commands)
    rich = html_from_message(msg).strip()                    # HTML + premium emoji preserved

    # ── USER BUTTON CUSTOMIZER ──
    if state in ("btn_emoji", "btn_label"):
        key = context.user_data.get("target_button")
        if key not in button_registry() and key not in STATIC_BUTTON_DEFAULTS:
            context.user_data.pop("state", None)
            return await msg.reply_html("❌ Button nahi mila.", reply_markup=back_kb("a_btncfg"))
        cfg = button_config()
        item = cfg.setdefault(key, {})
        if state == "btn_emoji":
            if text.lower() == "clear":
                item.pop("emoji", None)
            else:
                m = TG_EMOJI_RE.search(rich)
                if m:
                    item["emoji"] = f'<tg-emoji emoji-id="{m.group(1)}">{m.group(2)}</tg-emoji>'
                    item.pop("remove_emoji", None)
                elif text:
                    item["emoji"] = text[:16]
                    item.pop("remove_emoji", None)
                else:
                    return await msg.reply_html("❌ Emoji bhejiye.")
        else:
            if text.lower() == "clear":
                item.pop("label", None)
            elif plain_text(rich).strip():
                item["label"] = plain_text(rich).strip()[:64]
            else:
                return await msg.reply_html("❌ Button text bhejiye.")
        if not item:
            cfg.pop(key, None)
        save_button_config(cfg)
        context.user_data.pop("state", None)
        context.user_data.pop("target_button", None)
        return await msg.reply_html(
            "✅ Button customization save ho gayi.\n\n" + button_cfg_text(key),
            reply_markup=back_kb(f"a_btncfg_v:{key}"))

    # ── BROADCAST ──
    if state == "bc_wait":
        context.user_data["bc"] = (msg.chat_id, msg.message_id)
        total = int(scalar("SELECT COUNT(*) FROM users WHERE blocked=0"))
        context.user_data.pop("bc_button", None)
        return await msg.reply_html(
            f"📣 <b>Cᴏɴғɪʀᴍ Bʀᴏᴀᴅᴄᴀsᴛ</b>\n\nYe message <b>{fnum(total)}</b> users ko jayega.\n"
            "Chaho to pehle ek URL button add kar sakte ho.",
            reply_markup=InlineKeyboardMarkup([
                [ikb("🔘 Aᴅᴅ Bᴜᴛᴛᴏɴ", callback_data="a_bc_addbtn")],
                [ikb("🚀 Sᴇɴᴅ Nᴏᴡ", callback_data="a_bc_go")],
                [ikb("📌 Sᴇɴᴅ + Pɪɴ", callback_data="a_bc_pin")],
                [ikb("❌ Cᴀɴᴄᴇʟ", callback_data="a_home")],
            ]))

    # ── BROADCAST BUTTON ──
    if state == "bc_btn":
        btns, _bad = parse_button_lines(rich)
        if not btns:
            return await msg.reply_html("❌ Valid URL chahiye. Format: <code>Text - https://link</code>")
        context.user_data["bc_button"] = btns[0]
        context.user_data.pop("state", None)
        return await msg.reply_html(
            f"✅ Button set: <b>{esc(btns[0]['text'])}</b> → {esc(btns[0]['url'])}\n\nAb broadcast karo.",
            reply_markup=InlineKeyboardMarkup([
                [ikb("🚀 Sᴇɴᴅ Nᴏᴡ", callback_data="a_bc_go")],
                [ikb("📌 Sᴇɴᴅ + Pɪɴ", callback_data="a_bc_pin")],
                [ikb("❌ Cᴀɴᴄᴇʟ", callback_data="a_home")],
            ]), disable_web_page_preview=True)

    # ── AGENT NUMBERS (bulk add + normalize) ──
    if state == "rw_add":
        raw_lines = [l.strip() for l in text.splitlines() if l.strip()]
        if not raw_lines:
            return await msg.reply_html("❌ Koi valid number nahi mila. Dobara bhejiye.")
        added = 0
        skipped = []
        for line in raw_lines:
            normalized = normalize_phone(line)
            if len(normalized) == 10:
                q("INSERT INTO rewards(code,added_at) VALUES(?,?)", (normalized, now()))
                added += 1
            elif len(line) >= 5:
                q("INSERT INTO rewards(code,added_at) VALUES(?,?)", (line, now()))
                added += 1
            else:
                skipped.append(line)
        context.user_data.pop("state", None)
        sent = await notify_waitlist(context) if added else 0
        reply = (
            f"✅ <b>{added} numbers/codes add ho gaye!</b>\n\n"
            f"📦 Available : <b>{stock_label()}</b>\n"
            f"🔔 Waitlist notified : <b>{sent}</b>\n"
        )
        if skipped:
            reply += f"\n⚠️ Skip hua: {esc(', '.join(skipped))}"
        return await msg.reply_html(reply, reply_markup=back_kb("a_rw"))

    # ── REWARD BUTTONS ──
    if state == "rw_btn":
        if text.lower() == "clear":
            ss("reward_buttons", "[]")
            context.user_data.pop("state", None)
            return await msg.reply_html("🧹 Reward buttons hata diye.", reply_markup=back_kb("a_rw"))
        btns, bad = parse_button_lines(rich)
        if not btns:
            return await msg.reply_html(
                "❌ Format galat hai.\nUse: <code>Button Text - https://link</code>")
        ss("reward_buttons", json.dumps(btns, ensure_ascii=False))
        context.user_data.pop("state", None)
        out = f"✅ <b>{len(btns)} button set ho gaye!</b>\n\n"
        for b in btns:
            out += f"• {esc(b['text'])}{' 💎' if b.get('icon') else ''} → {esc(b['url'])}\n"
        if bad:
            out += f"\n⚠️ Skip: {esc(', '.join(bad))}"
        return await msg.reply_html(out, reply_markup=InlineKeyboardMarkup(
            [[url_btn_from_cfg(b) for b in btns[:2]], [ikb("⬅️ Bᴀᴄᴋ", callback_data="a_rw")]]),
            disable_web_page_preview=True)

    # ── NUMBERS / TEXTS ──
    if state == "set_refs":
        if not text.isdigit() or int(text) < 1:
            return await msg.reply_html("❌ 1 se bada number bhejiye.")
        ss("refs_per_reward", int(text))
        context.user_data.pop("state", None)
        return await msg.reply_html(
            f"✅ Ab <b>{text} referral = 1 reward</b> set ho gaya.",
            reply_markup=back_kb("a_set"))

    simple = {
        "set_welcome": "welcome_text",
        "set_rwtext": "reward_text",
        "set_gate": "gate_text",
        "set_oos": "outofstock_text",
        "set_maint": "maintenance_text",
    }
    if state in simple:
        if not rich:
            return await msg.reply_html("❌ Text bhejiye.")
        # Pehle render karke check karo ki HTML valid hai (galat tag save na ho)
        preview = render(rich, get_user(tg.id), tg)
        try:
            await msg.reply_html("✅ <b>Text update ho gaya!</b>\n\n<b>Preview:</b>\n\n" + preview,
                                 reply_markup=back_kb("a_set"), disable_web_page_preview=True)
        except BadRequest as e:
            return await msg.reply_html(
                f"❌ <b>Text save nahi hua</b> — HTML me galti hai:\n<code>{esc(e)}</code>\n\n"
                "Tags check karke dobara bhejiye.")
        ss(simple[state], rich)
        context.user_data.pop("state", None)
        return

    if state == "set_photo":
        if msg.photo:
            ss("start_photo", msg.photo[-1].file_id)
        elif text.lower() == "clear":
            ss("start_photo", "")
        elif text.startswith("http"):
            ss("start_photo", text)
        else:
            return await msg.reply_html("❌ Photo bhejiye, URL bhejiye, ya <code>clear</code>.")
        context.user_data.pop("state", None)
        return await msg.reply_html("✅ Start photo update ho gaya.", reply_markup=back_kb("a_set"))

    if state == "set_log":
        if text.lower() == "clear":
            ss("log_channel", "")
            context.user_data.pop("state", None)
            return await msg.reply_html("🧹 Log channel hata diya.", reply_markup=back_kb("a_set"))
        try:
            cid = int(text)
            await context.bot.send_message(cid, "✅ Log channel connected!")
            ss("log_channel", cid)
            context.user_data.pop("state", None)
            return await msg.reply_html("✅ Log channel set ho gaya.", reply_markup=back_kb("a_set"))
        except Exception as e:  # noqa: BLE001
            return await msg.reply_html(f"❌ Fail: {esc(e)}\nBot ko wahan admin banaiye.")

    # ── ADD CHANNEL ──
    if state == "ch_add":
        cid = None
        fo = getattr(msg, "forward_origin", None)
        if fo is not None and getattr(fo, "chat", None) is not None:
            cid = fo.chat.id
        elif getattr(msg, "forward_from_chat", None):
            cid = msg.forward_from_chat.id
        elif text.lstrip("-").isdigit():
            cid = int(text)
        elif text.startswith("@"):
            cid = text
        elif "t.me/" in text and "+" not in text and "joinchat" not in text:
            cid = "@" + text.rstrip("/").rsplit("/", 1)[-1]
        if cid is None:
            return await msg.reply_html("❌ Channel ka message forward kijiye, ID ya @username bhejiye.")
        try:
            title, link, real_id = await add_channel_by_id(context, cid)
        except Exception as e:  # noqa: BLE001
            return await msg.reply_html(
                f"❌ <b>Add nahi hua:</b> {esc(e)}\n\nBot ko us channel me admin banaiye "
                f"(Invite Users via Link permission ke sath) aur dobara try kijiye.")
        context.user_data.pop("state", None)
        c = get_channel(real_id)
        link_txt = esc(link) if link else ("⚠️ link nahi bana — bot ko invite permission dijiye, "
                                           "ya niche Change Link se apna link set kijiye")
        return await msg.reply_html(
            f"✅ <b>Channel add ho gaya!</b>\n\n📢 {esc(title)}\n🆔 <code>{real_id}</code>\n"
            f"🔗 {link_txt}\n\n"
            "Niche se naam / link / button text edit kar sakte hain 👇",
            reply_markup=channel_card_kb(c), disable_web_page_preview=True)

    # ── EDIT CHANNEL ──
    if state in ("ch_name", "ch_link", "ch_btn"):
        cid = context.user_data.get("target_ch")
        c = get_channel(cid) if cid else None
        if not c:
            context.user_data.pop("state", None)
            return await msg.reply_html("❌ Channel nahi mila.", reply_markup=back_kb("a_ch"))
        if state == "ch_name":
            if not rich:
                return await msg.reply_html("❌ Naam bhejiye.")
            q("UPDATE channels SET title=? WHERE chat_id=?", (plain_text(rich)[:64], cid))
            done = "✅ Channel ka naam update ho gaya."
        elif state == "ch_link":
            if text.lower() == "auto":
                q("UPDATE channels SET invite_link=NULL, link_manual=0 WHERE chat_id=?", (cid,))
                link = await ensure_link(context.bot, get_channel(cid))
                done = ("✅ Naya auto link ban gaya." if link else
                        "⚠️ Auto link nahi bana — bot ko 'Invite Users via Link' permission dijiye.")
            else:
                link = normalize_channel_link(text)
                if not link:
                    return await msg.reply_html(
                        "❌ Valid link bhejiye: <code>https://t.me/+xxxx</code>, "
                        "<code>https://t.me/username</code> ya <code>@username</code>.")
                q("UPDATE channels SET invite_link=?, link_manual=1 WHERE chat_id=?", (link, cid))
                done = f"✅ Link update ho gaya:\n{esc(link)}"
        else:  # ch_btn
            if text.lower() == "clear":
                q("UPDATE channels SET btn_text=NULL WHERE chat_id=?", (cid,))
                done = "🧹 Button text default par set ho gaya."
            else:
                q("UPDATE channels SET btn_text=? WHERE chat_id=?", (rich[:64], cid))
                done = "✅ Join button text update ho gaya."
        _join_cache.clear()
        context.user_data.pop("state", None)
        c = get_channel(cid)
        return await msg.reply_html(done + "\n\n" + channel_card(c), reply_markup=channel_card_kb(c),
                                    disable_web_page_preview=True)

    # ── GIFT CODES ──
    if state in ("gc_num", "gc_lim", "gc_exp"):
        code = context.user_data.get("target_gc")
        g = gc_get(code) if code else None
        if not g:
            context.user_data.pop("state", None)
            return await msg.reply_html("❌ Gift code nahi mila.", reply_markup=back_kb("a_gc"))
        if state == "gc_num":
            if text.lower() == "pool":
                q("UPDATE giftcodes SET agent_number='POOL', active=1 WHERE code=?", (code,))
            elif is_phone(text):
                q("UPDATE giftcodes SET agent_number=?, active=1 WHERE code=?",
                  (normalize_phone(text), code))
            elif len(text) >= 5:
                q("UPDATE giftcodes SET agent_number=?, active=1 WHERE code=?", (text, code))
            else:
                return await msg.reply_html("❌ 10-digit number bhejiye ya <code>pool</code>.")
            done = "✅ Reward number set ho gaya — code ab ACTIVE hai."
        elif state == "gc_lim":
            if not text.isdigit():
                return await msg.reply_html("❌ Number bhejiye (0 = unlimited).")
            q("UPDATE giftcodes SET max_uses=? WHERE code=?", (int(text), code))
            done = "✅ Usage limit update ho gayi."
        else:
            ts = parse_expiry(text)
            if ts is None:
                return await msg.reply_html("❌ Format: <code>24h</code>, <code>7d</code>, "
                                            "<code>25-12-2026</code> ya <code>never</code>.")
            q("UPDATE giftcodes SET expires_at=? WHERE code=?", (ts, code))
            done = "✅ Expiry update ho gayi."
        context.user_data.pop("state", None)
        g = gc_get(code)
        return await msg.reply_html(done + "\n\n" + gc_card(g), reply_markup=gc_card_kb(g))

    if state == "gc_custom":
        code = re.sub(r"[^A-Za-z0-9]", "", text).upper()
        if not (4 <= len(code) <= 32):
            return await msg.reply_html("❌ 4–32 letters/digits ka code bhejiye.")
        if gc_get(code):
            return await msg.reply_html("❌ Ye code already exist karta hai. Doosra bhejiye.")
        g = gc_create(code, tg.id)
        context.user_data["target_gc"] = code
        context.user_data["state"] = "gc_num"
        return await msg.reply_html(
            f"✅ <b>Code bana:</b> <code>{esc(code)}</code>\n\n" + ASK_TEXT["gc_num"],
            reply_markup=back_kb(f"a_gc_v:{code}"))

    if state == "gc_prefix":
        p = re.sub(r"[^A-Za-z0-9]", "", text).upper()[:20]
        if not p:
            return await msg.reply_html("❌ Letters/digits ka prefix bhejiye.")
        ss("gc_prefix", p)
        context.user_data.pop("state", None)
        return await msg.reply_html(f"✅ Prefix set: <code>{esc(p)}</code> → e.g. "
                                    f"<code>{esc(gc_generate(p))}</code>", reply_markup=back_kb("a_gc"))

    # ── USER TOOLS ──
    if state == "u_find":
        target = None
        if text.lstrip("-").isdigit():
            target = get_user(int(text))
        else:
            uname = text.lstrip("@")
            target = one("SELECT * FROM users WHERE lower(username)=?", (uname.lower(),))
        if not target:
            return await msg.reply_html("❌ User nahi mila. ID/@username check kijiye.")
        context.user_data.pop("state", None)
        return await msg.reply_html(user_card(target), reply_markup=user_card_kb(target))

    if state == "u_addref":
        tid = context.user_data.get("target")
        try:
            n = int(text)
        except ValueError:
            return await msg.reply_html("❌ Number bhejiye.")
        q("UPDATE users SET refs = MAX(0, refs + ?) WHERE user_id=?", (n, tid))
        context.user_data.pop("state", None)
        t = get_user(tid)
        return await msg.reply_html(
            f"✅ Referrals update: <b>{int(t['refs'] or 0)}</b>",
            reply_markup=user_card_kb(t))

    if state == "u_msg":
        tid = context.user_data.get("target")
        try:
            await context.bot.copy_message(chat_id=tid, from_chat_id=msg.chat_id,
                                           message_id=msg.message_id)
            context.user_data.pop("state", None)
            return await msg.reply_html("✅ Message bhej diya.", reply_markup=back_kb("a_users"))
        except Exception as e:  # noqa: BLE001
            return await msg.reply_html(f"❌ Fail: {esc(e)}")

    if state == "adm_add":
        if not text.isdigit():
            return await msg.reply_html("❌ Sirf numeric ID bhejiye.")
        q("INSERT OR REPLACE INTO admins(user_id,added_by,added_at) VALUES(?,?,?)",
          (int(text), tg.id, now()))
        context.user_data.pop("state", None)
        return await msg.reply_html(f"✅ <code>{text}</code> ab sub-admin hai.",
                                    reply_markup=back_kb("a_admins"))

    context.user_data.pop("state", None)


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel active input and return to a clean fresh Admin home screen."""
    for k in ("state", "bc", "bc_button", "restore_file_id", "target", "target_ch",
              "target_gc", "target_button"):
        context.user_data.pop(k, None)
    try:
        await update.effective_message.delete()
    except Exception:  # noqa: BLE001
        pass
    if is_admin(update.effective_user.id):
        await admin_home(update, context, edit=False)
    else:
        await show_menu(update, context)


# ════════════════════════════════════════════════════════════════════════
#  JOIN REQUEST / MEMBERSHIP TRACKING
# ════════════════════════════════════════════════════════════════════════

async def on_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Private channel me join request aate hi: record → (auto approve) → user unlock."""
    req = update.chat_join_request
    uid = req.from_user.id
    cid = req.chat.id
    if not one("SELECT chat_id FROM channels WHERE chat_id=?", (cid,)):
        return
    q("""INSERT INTO requests(user_id,chat_id,status,ts) VALUES(?,?,?,?)
         ON CONFLICT(user_id,chat_id) DO UPDATE SET status='pending', ts=excluded.ts""",
      (uid, cid, "pending", now()))
    _join_cache.pop(uid, None)

    if gi("auto_approve", 1) == 1:
        try:
            await req.approve()
            q("UPDATE requests SET status='approved' WHERE user_id=? AND chat_id=?", (uid, cid))
        except Exception as e:  # noqa: BLE001
            log.warning("approve fail: %s", e)

    register_user(req.from_user)
    if maintenance_on() and not is_admin(uid):
        return          # request record ho gaya; maintenance khatam hone par verify hoga
    missing = await pending_channels(context.bot, uid, force_fresh=True)
    if missing:
        # Baaki channels abhi bhi pending → gate screen refresh karo (agar khula hai)
        u = get_user(uid)
        gate_msg = u["gate_msg"] if u and "gate_msg" in u.keys() else None
        if gate_msg:
            try:
                kb = await gate_keyboard(context.bot, missing)
                await context.bot.edit_message_text(
                    chat_id=uid, message_id=int(gate_msg),
                    text=render(gs("gate_text"), u, req.from_user)
                         + f"\n\n✅ <i>{esc(req.chat.title)} — request received</i>",
                    reply_markup=kb, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
            except Exception:  # noqa: BLE001
                pass
        return
    await unlock_after_join(context, uid, req.from_user)


async def on_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cmu = update.chat_member
    if not cmu:
        return
    cid = cmu.chat.id
    if not one("SELECT chat_id FROM channels WHERE chat_id=?", (cid,)):
        return
    uid = cmu.new_chat_member.user.id
    status = cmu.new_chat_member.status
    _join_cache.pop(uid, None)
    if status in OK_STATUS:
        q("""INSERT INTO requests(user_id,chat_id,status,ts) VALUES(?,?,?,?)
             ON CONFLICT(user_id,chat_id) DO UPDATE SET status='approved', ts=excluded.ts""",
          (uid, cid, "approved", now()))
        # Public channel direct join → agar gate screen khula hai to auto-unlock
        u = get_user(uid)
        if u and u["gate_msg"] and not (maintenance_on() and not is_admin(uid)):
            missing = await pending_channels(context.bot, uid, force_fresh=True)
            if not missing:
                await unlock_after_join(context, uid, cmu.new_chat_member.user)
        return
    if status in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED):
        q("DELETE FROM requests WHERE user_id=? AND chat_id=?", (uid, cid))
        if gi("leave_penalty", 1) == 1 and get_user(uid):
            q("UPDATE users SET verified=0 WHERE user_id=?", (uid,))
            try:
                await context.bot.send_message(
                    uid,
                    f"⚠️ <b>Aapne <i>{esc(cmu.chat.title)}</i> channel chhod diya.</b>\n\n"
                    "Bot use karne ke liye dobara join karke Continue dabaiye 👇",
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup(
                        [[ubtn("continue")]]))
            except Exception:  # noqa: BLE001
                pass


# ════════════════════════════════════════════════════════════════════════
#  MISC COMMANDS
# ════════════════════════════════════════════════════════════════════════

async def cmd_claim(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/claim → Agent Number claim  |  /claim CODE → Gift Code redeem."""
    if update.effective_chat and update.effective_chat.type != "private":
        return
    if context.args:
        return await redeem_gift(update, context, " ".join(context.args))
    await do_claim(update, context)


async def cmd_refer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await require_join(update, context):
        return
    await show_refer(update, context)


async def cmd_top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await require_join(update, context):
        return
    await show_top(update, context)


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    c = update.effective_chat
    await update.effective_message.reply_html(
        f"🆔 Your ID : <code>{update.effective_user.id}</code>\n"
        f"💬 Chat ID : <code>{c.id}</code>")


async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t0 = time.perf_counter()
    m = await update.effective_message.reply_html("🏓 Pinging…")
    await m.edit_text(f"🏓 <b>Pong!</b> <code>{(time.perf_counter() - t0) * 1000:.0f} ms</code>",
                      parse_mode=ParseMode.HTML)


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    await update.effective_message.reply_html(
        f"👥 Users : <b>{int(scalar('SELECT COUNT(*) FROM users'))}</b>\n"
        f"🎟 Claims : <b>{int(scalar('SELECT COUNT(*) FROM claims'))}</b>\n"
        f"📦 Numbers Left : <b>{stock_label()}</b>")


async def cmd_maint(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/maintenance on|off  (admin shortcut)"""
    if not is_admin(update.effective_user.id):
        return
    arg = (context.args[0].lower() if context.args else "")
    if arg in ("on", "1", "true"):
        ss("maintenance", 1)
    elif arg in ("off", "0", "false"):
        ss("maintenance", 0)
    await update.effective_message.reply_html(
        f"🛠 Maintenance : <b>{'ON ⚠️ — sab users blocked' if maintenance_on() else 'OFF ✅'}</b>\n"
        "<i>Use: /maintenance on | off</i>")


async def cmd_dev(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_html(
        f"👨‍💻 <b>Dᴇᴠᴇʟᴏᴘᴇʀ</b>\n\n<b>{DEV_NAME}</b>\n"
        f"🔗 t.me/{esc(DEV_USERNAME)}\n\n<i>Mʀ. Dᴋ Sʜᴀʀᴍᴀ • Premium Bot Developer</i>",
        reply_markup=InlineKeyboardMarkup(
            [[ikb("💬 Cᴏɴᴛᴀᴄᴛ", url=f"https://t.me/{DEV_USERNAME}")]]))


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.error("Update error: %s", context.error, exc_info=context.error)


# ════════════════════════════════════════════════════════════════════════
#  MAIN
# ════════════════════════════════════════════════════════════════════════

async def post_init(app: Application):
    global BOT_USERNAME
    me = await app.bot.get_me()
    BOT_USERNAME = me.username
    try:
        from telegram import BotCommand
        await app.bot.set_my_commands([
            BotCommand("start", "Bot start / menu"),
            BotCommand("claim", "Agent Number claim  •  /claim CODE = gift code"),
            BotCommand("refer", "Referral link"),
            BotCommand("top", "Leaderboard"),
        ])
    except Exception:  # noqa: BLE001
        pass
    log.info("Bot @%s started | Developer: %s", BOT_USERNAME, DEV_NAME)
    for a in admin_ids():
        try:
            await app.bot.send_message(
                a, f"✅ <b>Bot Online!</b>\n@{BOT_USERNAME}\n\n"
                   f"Admin panel : /admin\n"
                   f"🛠 Maintenance : <b>{'ON ⚠️' if maintenance_on() else 'OFF'}</b>\n\n"
                   f"<i>Bᴏᴛ ʙʏ</i> <b>{DEV_NAME}</b>",
                parse_mode=ParseMode.HTML)
        except Exception:  # noqa: BLE001
            pass


def main():
    if not BOT_TOKEN:
        raise SystemExit("❌ BOT_TOKEN missing! Railway → Variables me BOT_TOKEN daaliye.")
    if not OWNER_ID:
        log.warning("⚠️ OWNER_ID set nahi hai — admin panel kaam nahi karega!")
    db_init()

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .concurrent_updates(True)
        .build()
    )

    # Group -1: maintenance gate — HAR update (button, command, text) sabse pehle yahan
    app.add_handler(TypeHandler(Update, maintenance_gate), group=-1)

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("claim", cmd_claim))
    app.add_handler(CommandHandler("refer", cmd_refer))
    app.add_handler(CommandHandler("top", cmd_top))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("panel", cmd_admin))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler(["maintenance", "maint"], cmd_maint))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("dev", cmd_dev))
    app.add_handler(CommandHandler("developer", cmd_dev))

    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(ChatJoinRequestHandler(on_join_request))
    app.add_handler(ChatMemberHandler(on_chat_member, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & ~filters.COMMAND & ~filters.StatusUpdate.ALL, on_message))
    app.add_error_handler(on_error)

    log.info("Polling start… Developer: %s", DEV_NAME)
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
