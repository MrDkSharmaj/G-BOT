#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
═══════════════════════════════════════════════════════════════════════════
   ⏤͟͟͞͞ 🇮🇳 Dᴋ Sʜᴀʀᴍᴀ  —  PREMIUM REFERRAL & REWARD BOT
───────────────────────────────────────────────────────────────────────────
   • Force Join (Private Channel Join-Request auto detect + auto approve)
   • Unlimited Force-Join Channels (add / remove from Admin Panel)
   • First Bonus Claim  →  then 1 Reward per N Referrals
   • Reward Code Vault (add 5-6 codes, har user ko ek-ek alag code)
   • Custom Buttons on Reward Message (Admin Panel se set)
   • Referral Tracking (unique link:  t.me/bot?start=USERID)
   • Full Admin Panel : Stats • Rewards • Channels • Settings •
     Users Manage (block/unblock/gift/refs) • Broadcast • Leaderboard •
     Waitlist Auto-Notify • CSV Export • DB Backup • Sub-Admins • Maintenance
───────────────────────────────────────────────────────────────────────────
   Developer : ⏤͟͟͞͞ 🇮🇳 Dᴋ Sʜᴀʀᴍᴀ
═══════════════════════════════════════════════════════════════════════════
"""

import os
import io
import csv
import html
import json
import time
import asyncio
import logging
import sqlite3
import threading
from datetime import datetime, timezone

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ParseMode, ChatMemberStatus
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    ChatJoinRequestHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
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
    "reward_mode": "unique",          # unique | shared
    "leave_penalty": "1",             # channel chhoda to dobara verify
    "notify_referrer": "1",
    "log_channel": "",
    "start_photo": "",
    "welcome_text": (
        "👋 <b>Wᴇʟᴄᴏᴍᴇ {name}!</b>\n\n"
        "🎁 Yahan aapko <b>exclusive reward codes</b> milte hain.\n"
        "🔑 Pehla reward <b>FREE Bonus</b> hai — bas claim kijiye.\n"
        "👥 Uske baad har <b>{per} referral</b> par ek naya reward.\n\n"
        "Niche button se shuru kijiye 👇"
    ),
    "reward_text": (
        "🎉 <b>Cᴏɴɢʀᴀᴛᴜʟᴀᴛɪᴏɴs {name}!</b>\n\n"
        "Aapka reward unlock ho gaya hai 👇"
    ),
    "reward_buttons": "[]",
    "gate_text": (
        "🔒 <b>Aᴄᴄᴇss Lᴏᴄᴋᴇᴅ</b>\n\n"
        "Bot use karne ke liye niche diye gaye channel(s) join kijiye, "
        "phir <b>✅ Vᴇʀɪғɪᴇᴅ</b> button dabaiye."
    ),
    "outofstock_text": (
        "😔 <b>Rewards ᴏᴜᴛ ᴏғ sᴛᴏᴄᴋ!</b>\n\n"
        "Filhaal saare rewards claim ho chuke hain. Admin naye rewards add karega "
        "to bot aapko <b>automatically notify</b> kar dega. Aapka number "
        "waiting list me safe hai ✅"
    ),
}


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
    for k, v in DEFAULT_SETTINGS.items():
        q("INSERT OR IGNORE INTO settings(key,val) VALUES(?,?)", (k, v))


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


# ════════════════════════════════════════════════════════════════════════
#  SMALL UTILS
# ════════════════════════════════════════════════════════════════════════

def now() -> int:
    return int(time.time())


def esc(x) -> str:
    return html.escape(str(x if x is not None else ""))


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
#  FORCE JOIN ENGINE
# ════════════════════════════════════════════════════════════════════════

OK_STATUS = {
    ChatMemberStatus.MEMBER,
    ChatMemberStatus.ADMINISTRATOR,
    ChatMemberStatus.OWNER,
}
_join_cache: dict = {}     # uid -> (ts, [pending channels])
CACHE_TTL = 45


def all_channels():
    return rows("SELECT * FROM channels ORDER BY added_at")


def has_request(uid: int, chat_id: int) -> bool:
    r = one("SELECT status FROM requests WHERE user_id=? AND chat_id=?", (uid, chat_id))
    return bool(r and r["status"] in ("pending", "approved"))


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
        q("UPDATE channels SET invite_link=? WHERE chat_id=?", (link, ch["chat_id"]))
    return link


async def pending_channels(bot, uid: int, force_fresh: bool = False):
    """User ne jo channel join nahi kiye unki list."""
    if gi("force_join", 1) != 1:
        return []
    if is_admin(uid):
        return []
    chans = all_channels()
    if not chans:
        return []
    hit = _join_cache.get(uid)
    if hit and not force_fresh and now() - hit[0] < CACHE_TTL:
        return hit[1]

    missing = []
    for ch in chans:
        ok = False
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
        if not ok and has_request(uid, ch["chat_id"]):
            ok = True          # private channel me request daal chuka hai
        if not ok:
            missing.append(ch)
    _join_cache[uid] = (now(), missing)
    return missing


async def gate_keyboard(bot, missing):
    kb = []
    for ch in missing:
        link = await ensure_link(bot, ch)
        title = ch["title"] or "Channel"
        if link:
            kb.append([InlineKeyboardButton(f"📢 Jᴏɪɴ • {title}"[:60], url=link)])
    kb.append([InlineKeyboardButton("✅ Vᴇʀɪғɪᴇᴅ • Cᴏɴᴛɪɴᴜᴇ", callback_data="verify")])
    kb.append([InlineKeyboardButton(f"👨‍💻 Dᴇᴠᴇʟᴏᴘᴇʀ • {DEV_NAME}", url=f"https://t.me/{DEV_USERNAME}")])
    return InlineKeyboardMarkup(kb)


# ════════════════════════════════════════════════════════════════════════
#  REWARD ENGINE
# ════════════════════════════════════════════════════════════════════════

def rewards_left() -> int:
    if gs("reward_mode") == "shared":
        total = scalar("SELECT COUNT(*) FROM rewards")
        return 9999 if total else 0
    return int(scalar("SELECT COUNT(*) FROM rewards WHERE used_by IS NULL"))


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
        if mode == "shared":
            pool = rows("SELECT * FROM rewards ORDER BY id")
            if not pool:
                return None
            u = get_user(uid)
            idx = int(u["claims"] or 0) % len(pool)
            r = pool[idx]
        else:
            r = one("SELECT * FROM rewards WHERE used_by IS NULL ORDER BY id LIMIT 1")
            if not r:
                return None
            q("UPDATE rewards SET used_by=?, used_at=? WHERE id=?", (uid, now(), r["id"]))
        q("INSERT INTO claims(user_id,reward_id,code,kind,claimed_at) VALUES(?,?,?,?,?)",
          (uid, r["id"], r["code"], kind, now()))
        q("UPDATE users SET claims=claims+1 WHERE user_id=?", (uid,))
    return r


def reward_buttons_kb(extra_back=True):
    kb = []
    try:
        data = json.loads(gs("reward_buttons") or "[]")
    except Exception:  # noqa: BLE001
        data = []
    row = []
    for b in data:
        txt, url = b.get("text"), b.get("url")
        if not txt or not url:
            continue
        row.append(InlineKeyboardButton(txt[:40], url=url))
        if len(row) == 2:
            kb.append(row)
            row = []
    if row:
        kb.append(row)
    if extra_back:
        kb.append([
            InlineKeyboardButton("👥 Rᴇғᴇʀ & Eᴀʀɴ", callback_data="refer"),
            InlineKeyboardButton("🏠 Mᴇɴᴜ", callback_data="menu"),
        ])
    return InlineKeyboardMarkup(kb)


# ════════════════════════════════════════════════════════════════════════
#  USER SIDE UI
# ════════════════════════════════════════════════════════════════════════

def main_menu_kb(u):
    can, need, kind = claim_state(u)
    claim_label = "🎁 Cʟᴀɪᴍ Yᴏᴜʀ Fɪʀsᴛ Bᴏɴᴜs" if int(u["claims"] or 0) == 0 else "🎁 Cʟᴀɪᴍ Rᴇᴡᴀʀᴅ"
    kb = [
        [InlineKeyboardButton(claim_label, callback_data="claim")],
        [InlineKeyboardButton("👥 Rᴇғᴇʀ & Eᴀʀɴ", callback_data="refer"),
         InlineKeyboardButton("👤 Mʏ Pʀᴏғɪʟᴇ", callback_data="profile")],
        [InlineKeyboardButton("🏆 Lᴇᴀᴅᴇʀʙᴏᴀʀᴅ", callback_data="top"),
         InlineKeyboardButton("🎟 Mʏ Rᴇᴡᴀʀᴅs", callback_data="myrewards")],
        [InlineKeyboardButton("❓ Hᴇʟᴘ", callback_data="help"),
         InlineKeyboardButton(f"👨‍💻 {DEV_NAME}", url=f"https://t.me/{DEV_USERNAME}")],
    ]
    return InlineKeyboardMarkup(kb)


def render(text: str, u_row, tg_user=None) -> str:
    per = max(1, gi("refs_per_reward", 1))
    name = (tg_user.first_name if tg_user else None) or (u_row["first_name"] if u_row else "User")
    return (text or "")\
        .replace("{name}", esc(name))\
        .replace("{per}", str(per))\
        .replace("{refs}", str(int(u_row["refs"] or 0) if u_row else 0))\
        .replace("{claims}", str(int(u_row["claims"] or 0) if u_row else 0))\
        .replace("{stock}", str(rewards_left()))\
        .replace("{dev}", DEV_NAME)


FOOTER = "\n\n<i>Bᴏᴛ ʙʏ</i> <b>{dev}</b>"


async def show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, edit=False):
    tg = update.effective_user
    u = get_user(tg.id)
    if not u:
        register_user(tg)
        u = get_user(tg.id)
    can, need, kind = claim_state(u)
    txt = render(gs("welcome_text"), u, tg)
    txt += (
        f"\n\n━━━━━━━━━━━━━━━\n"
        f"👥 Rᴇғᴇʀʀᴀʟs : <b>{int(u['refs'] or 0)}</b>\n"
        f"🎁 Rᴇᴡᴀʀᴅs Cʟᴀɪᴍᴇᴅ : <b>{int(u['claims'] or 0)}</b>\n"
        f"📦 Sᴛᴏᴄᴋ Lᴇғᴛ : <b>{'∞' if gs('reward_mode') == 'shared' and rewards_left() else rewards_left()}</b>\n"
        f"━━━━━━━━━━━━━━━"
    )
    if not can:
        txt += f"\n\n⚠️ Next reward ke liye <b>{need}</b> referral chahiye."
    txt += FOOTER.replace("{dev}", DEV_NAME)

    kb = main_menu_kb(u)
    photo = gs("start_photo").strip()
    if edit and update.callback_query:
        try:
            await update.callback_query.edit_message_text(
                txt, reply_markup=kb, parse_mode=ParseMode.HTML,
                disable_web_page_preview=True)
            return
        except BadRequest:
            pass
    if photo and not edit:
        try:
            await context.bot.send_photo(tg.id, photo, caption=txt,
                                         reply_markup=kb, parse_mode=ParseMode.HTML)
            return
        except Exception:  # noqa: BLE001
            pass
    await context.bot.send_message(tg.id, txt, reply_markup=kb,
                                   parse_mode=ParseMode.HTML,
                                   disable_web_page_preview=True)


async def show_gate(update: Update, context: ContextTypes.DEFAULT_TYPE, missing, edit=False):
    tg = update.effective_user
    u = get_user(tg.id)
    txt = render(gs("gate_text"), u, tg) + FOOTER.replace("{dev}", DEV_NAME)
    kb = await gate_keyboard(context.bot, missing)
    if edit and update.callback_query:
        try:
            await update.callback_query.edit_message_text(
                txt, reply_markup=kb, parse_mode=ParseMode.HTML,
                disable_web_page_preview=True)
            return
        except BadRequest:
            pass
    await context.bot.send_message(tg.id, txt, reply_markup=kb,
                                   parse_mode=ParseMode.HTML,
                                   disable_web_page_preview=True)


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
        tail = ("🎉 Aapka reward <b>ready</b> hai — Claim kijiye!"
                if can else f"Aur <b>{need}</b> referral me next reward unlock hoga.")
        try:
            await context.bot.send_message(
                rb,
                f"🎊 <b>Nᴇᴡ Rᴇғᴇʀʀᴀʟ!</b>\n\n"
                f"👤 <b>{esc(u['first_name'])}</b> ne aapke link se join kiya.\n"
                f"👥 Total Referrals : <b>{int(ref['refs'] or 0)}</b>\n\n{tail}",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🎁 Cʟᴀɪᴍ Rᴇᴡᴀʀᴅ", callback_data="claim")]]),
            )
        except Exception:  # noqa: BLE001
            pass
    await send_log(context,
                   f"➕ <b>Referral</b>\n👤 {esc(u['first_name'])} (<code>{uid}</code>)\n"
                   f"🔗 By: <code>{rb}</code>")


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

    if gi("maintenance", 0) == 1 and not is_admin(tg.id):
        return await context.bot.send_message(
            tg.id, "🛠 <b>Bot maintenance mode me hai.</b>\nThodi der baad try kijiye.",
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


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    per = max(1, gi("refs_per_reward", 1))
    txt = (
        "❓ <b>Hᴏᴡ ɪᴛ ᴡᴏʀᴋs</b>\n\n"
        "1️⃣ Saare force-join channels join kijiye.\n"
        "2️⃣ <b>Claim Your First Bonus</b> dabaiye — free reward code milega.\n"
        f"3️⃣ Uske baad har <b>{per} referral</b> par ek naya reward code unlock hota hai.\n"
        "4️⃣ Refer & Earn se apna link share kijiye.\n\n"
        "<b>Cᴏᴍᴍᴀɴᴅs</b>\n"
        "/start – menu\n/claim – reward claim\n/refer – referral link\n"
        "/profile – aapki details\n/top – leaderboard\n/help – ye message"
        + FOOTER.replace("{dev}", DEV_NAME)
    )
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Mᴇɴᴜ", callback_data="menu")]])
    if update.callback_query:
        try:
            return await update.callback_query.edit_message_text(
                txt, reply_markup=kb, parse_mode=ParseMode.HTML)
        except BadRequest:
            return
    await update.effective_message.reply_html(txt, reply_markup=kb)


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
        popup = (f"🔒 Reward locked!\n\nAur {need} referral chahiye "
                 f"({per} referral = 1 reward).\n\nRefer & Earn button dabaiye 👇")
        if cq:
            await cq.answer(popup, show_alert=True)
        return await show_refer(update, context, edit=bool(cq))

    r = take_reward(tg.id, kind)
    if not r:
        q("INSERT OR IGNORE INTO waitlist(user_id,ts) VALUES(?,?)", (tg.id, now()))
        txt = gs("outofstock_text") + FOOTER.replace("{dev}", DEV_NAME)
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Mᴇɴᴜ", callback_data="menu")]])
        if cq:
            await cq.answer("😔 Rewards out of stock!", show_alert=True)
            try:
                return await cq.edit_message_text(txt, reply_markup=kb, parse_mode=ParseMode.HTML)
            except BadRequest:
                return
        return await update.effective_message.reply_html(txt, reply_markup=kb)

    u2 = get_user(tg.id)
    can2, need2, _ = claim_state(u2)
    body = render(gs("reward_text"), u2, tg)
    txt = (
        f"{body}\n\n"
        f"╭━━━━━━━━━━━━━━━━╮\n"
        f"🎁 <b>Yᴏᴜʀ Rᴇᴡᴀʀᴅ</b>\n"
        f"╰━━━━━━━━━━━━━━━━╯\n"
        f"<code>{esc(r['code'])}</code>\n\n"
        f"🏷 Type : <b>{'Free Bonus' if kind == 'bonus' else 'Referral Reward'}</b>\n"
        f"👥 Referrals : <b>{int(u2['refs'] or 0)}</b>\n"
        f"🎟 Total Claimed : <b>{int(u2['claims'] or 0)}</b>\n"
    )
    txt += ("\n✅ Next reward bhi ready hai — dobara Claim dabaiye!"
            if can2 else f"\n👥 Next reward ke liye <b>{need2}</b> referral chahiye.")
    txt += FOOTER.replace("{dev}", DEV_NAME)

    if cq:
        await cq.answer("🎉 Reward unlocked!")
        try:
            await cq.edit_message_text(txt, reply_markup=reward_buttons_kb(),
                                       parse_mode=ParseMode.HTML,
                                       disable_web_page_preview=True)
        except BadRequest:
            await context.bot.send_message(tg.id, txt, reply_markup=reward_buttons_kb(),
                                           parse_mode=ParseMode.HTML)
    else:
        await update.effective_message.reply_html(txt, reply_markup=reward_buttons_kb())

    await send_log(context,
                   f"🎁 <b>Reward Claimed</b>\n👤 {esc(tg.first_name)} (<code>{tg.id}</code>)\n"
                   f"🔑 <code>{esc(r['code'])}</code>\n🏷 {kind}\n📦 Left: {rewards_left()}")

    left = rewards_left()
    if gs("reward_mode") != "shared" and left in (0, 1, 2):
        for a in admin_ids():
            try:
                await context.bot.send_message(
                    a, f"⚠️ <b>Rewards Stock Alert</b>\nSirf <b>{left}</b> reward bache hain.\n"
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
        f"Har <b>{per} referral</b> = <b>1 Reward Code</b> 🎁\n\n"
        "🔗 <b>Yᴏᴜʀ Lɪɴᴋ</b>\n"
        f"<code>{esc(link)}</code>\n\n"
        "━━━━━━━━━━━━━━━\n"
        f"👤 Total Invited : <b>{invited}</b>\n"
        f"✅ Verified : <b>{verified}</b>\n"
        f"🎟 Rewards Claimed : <b>{int(u['claims'] or 0)}</b>\n"
        "━━━━━━━━━━━━━━━\n\n"
        + ("🎉 Aapka reward ready hai — Claim kijiye!"
           if can else f"⏳ Next reward : <b>{need}</b> referral aur.")
        + "\n\n<i>Note: Referral tab count hoga jab aapka friend saare channels join kar le.</i>"
        + FOOTER.replace("{dev}", DEV_NAME)
    )
    share = (
        "https://t.me/share/url?url=" + link +
        "&text=" + "🎁%20Free%20Reward%20Codes%20le%20lo%20is%20bot%20se!"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 Sʜᴀʀᴇ Lɪɴᴋ", url=share)],
        [InlineKeyboardButton("🎁 Cʟᴀɪᴍ Rᴇᴡᴀʀᴅ", callback_data="claim")],
        [InlineKeyboardButton("🏠 Mᴇɴᴜ", callback_data="menu")],
    ])
    if edit and update.callback_query:
        try:
            return await update.callback_query.edit_message_text(
                txt, reply_markup=kb, parse_mode=ParseMode.HTML,
                disable_web_page_preview=True)
        except BadRequest:
            return
    await context.bot.send_message(tg.id, txt, reply_markup=kb, parse_mode=ParseMode.HTML,
                                   disable_web_page_preview=True)


async def show_profile(update: Update, context: ContextTypes.DEFAULT_TYPE, edit=False):
    tg = update.effective_user
    u = get_user(tg.id)
    if not u:
        register_user(tg)
        u = get_user(tg.id)
    rank = int(scalar("SELECT COUNT(*) FROM users WHERE refs > ?", (int(u["refs"] or 0),))) + 1
    can, need, _ = claim_state(u)
    txt = (
        "👤 <b>Mʏ Pʀᴏғɪʟᴇ</b>\n\n"
        f"🏷 Name : <b>{esc(u['first_name'])}</b>\n"
        f"🆔 ID : <code>{tg.id}</code>\n"
        f"📛 Username : @{esc(u['username']) if u['username'] else '—'}\n"
        f"📅 Joined : <b>{fmt_ts(u['joined_at'])}</b>\n"
        f"✅ Verified : <b>{'Yes' if int(u['verified'] or 0) else 'No'}</b>\n"
        "━━━━━━━━━━━━━━━\n"
        f"👥 Referrals : <b>{int(u['refs'] or 0)}</b>\n"
        f"🎟 Rewards : <b>{int(u['claims'] or 0)}</b>\n"
        f"🏆 Rank : <b>#{rank}</b>\n"
        f"🎁 Next Reward : <b>{'READY ✅' if can else str(need) + ' referral baaki'}</b>\n"
        + FOOTER.replace("{dev}", DEV_NAME)
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🎁 Cʟᴀɪᴍ", callback_data="claim"),
         InlineKeyboardButton("👥 Rᴇғᴇʀ", callback_data="refer")],
        [InlineKeyboardButton("🏠 Mᴇɴᴜ", callback_data="menu")],
    ])
    if edit and update.callback_query:
        try:
            return await update.callback_query.edit_message_text(txt, reply_markup=kb,
                                                                 parse_mode=ParseMode.HTML)
        except BadRequest:
            return
    await update.effective_message.reply_html(txt, reply_markup=kb)


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
    txt = "\n".join(lines) + FOOTER.replace("{dev}", DEV_NAME)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 Rᴇғᴇʀ & Eᴀʀɴ", callback_data="refer")],
        [InlineKeyboardButton("🏠 Mᴇɴᴜ", callback_data="menu")],
    ])
    if edit and update.callback_query:
        try:
            return await update.callback_query.edit_message_text(txt, reply_markup=kb,
                                                                 parse_mode=ParseMode.HTML)
        except BadRequest:
            return
    await update.effective_message.reply_html(txt, reply_markup=kb)


async def show_myrewards(update: Update, context: ContextTypes.DEFAULT_TYPE, edit=False):
    uid = update.effective_user.id
    cl = rows("SELECT code,kind,claimed_at FROM claims WHERE user_id=? "
              "ORDER BY id DESC LIMIT 10", (uid,))
    lines = ["🎟 <b>Mʏ Rᴇᴡᴀʀᴅs</b>\n"]
    if not cl:
        lines.append("<i>Abhi tak koi reward claim nahi kiya.</i>")
    for c in cl:
        tag = "🎁 Bonus" if c["kind"] == "bonus" else "👥 Referral"
        lines.append(f"{tag} • <code>{esc(c['code'])}</code>\n<i>{fmt_ts(c['claimed_at'])}</i>\n")
    txt = "\n".join(lines) + FOOTER.replace("{dev}", DEV_NAME)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Mᴇɴᴜ", callback_data="menu")]])
    if edit and update.callback_query:
        try:
            return await update.callback_query.edit_message_text(txt, reply_markup=kb,
                                                                 parse_mode=ParseMode.HTML)
        except BadRequest:
            return
    await update.effective_message.reply_html(txt, reply_markup=kb)


# ════════════════════════════════════════════════════════════════════════
#  ADMIN PANEL
# ════════════════════════════════════════════════════════════════════════

def admin_home_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Sᴛᴀᴛɪsᴛɪᴄs", callback_data="a_stats"),
         InlineKeyboardButton("🎁 Rᴇᴡᴀʀᴅs", callback_data="a_rw")],
        [InlineKeyboardButton("📢 Fᴏʀᴄᴇ Jᴏɪɴ", callback_data="a_ch"),
         InlineKeyboardButton("⚙️ Sᴇᴛᴛɪɴɢs", callback_data="a_set")],
        [InlineKeyboardButton("👥 Usᴇʀs Mᴀɴᴀɢᴇ", callback_data="a_users"),
         InlineKeyboardButton("📣 Bʀᴏᴀᴅᴄᴀsᴛ", callback_data="a_bc")],
        [InlineKeyboardButton("🏆 Tᴏᴘ Rᴇғᴇʀʀᴇʀs", callback_data="a_top"),
         InlineKeyboardButton("🎟 Cʟᴀɪᴍ Lᴏɢs", callback_data="a_logs")],
        [InlineKeyboardButton("🛠 Tᴏᴏʟs & Bᴀᴄᴋᴜᴘ", callback_data="a_tools"),
         InlineKeyboardButton("👮 Sᴜʙ-Aᴅᴍɪɴs", callback_data="a_admins")],
        [InlineKeyboardButton("🏠 Usᴇʀ Mᴇɴᴜ", callback_data="menu")],
    ])


def back_kb(target="a_home"):
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Bᴀᴄᴋ", callback_data=target)]])


async def admin_home(update: Update, context: ContextTypes.DEFAULT_TYPE, edit=True):
    context.user_data.pop("state", None)
    total = int(scalar("SELECT COUNT(*) FROM users"))
    left = rewards_left()
    txt = (
        "👑 <b>Aᴅᴍɪɴ Cᴏɴᴛʀᴏʟ Pᴀɴᴇʟ</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"👥 Users : <b>{total}</b>\n"
        f"📦 Rewards Left : <b>{'∞' if gs('reward_mode') == 'shared' and left else left}</b>\n"
        f"🔗 Refs / Reward : <b>{gi('refs_per_reward', 1)}</b>\n"
        f"📢 Force Join : <b>{'ON ✅' if gi('force_join', 1) else 'OFF ❌'}</b>\n"
        f"🛠 Maintenance : <b>{'ON ⚠️' if gi('maintenance', 0) else 'OFF ✅'}</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"<i>Pᴀɴᴇʟ ʙʏ</i> <b>{DEV_NAME}</b>"
    )
    if edit and update.callback_query:
        try:
            return await update.callback_query.edit_message_text(
                txt, reply_markup=admin_home_kb(), parse_mode=ParseMode.HTML)
        except BadRequest:
            return
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
        "━━━━━━━━━━━━━━━━━━\n"
        f"📦 Rewards Total : <b>{int(scalar('SELECT COUNT(*) FROM rewards'))}</b>\n"
        f"✅ Used : <b>{int(scalar('SELECT COUNT(*) FROM rewards WHERE used_by IS NOT NULL'))}</b>\n"
        f"🆓 Left : <b>{rewards_left()}</b>\n"
        f"⏳ Waiting List : <b>{int(scalar('SELECT COUNT(*) FROM waitlist'))}</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"📢 Channels : <b>{len(all_channels())}</b>\n"
        f"👮 Admins : <b>{len(admin_ids())}</b>"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Rᴇғʀᴇsʜ", callback_data="a_stats")],
        [InlineKeyboardButton("⬅️ Bᴀᴄᴋ", callback_data="a_home")],
    ])
    try:
        await update.callback_query.edit_message_text(txt, reply_markup=kb,
                                                     parse_mode=ParseMode.HTML)
    except BadRequest:
        pass


# ─────────────────────────── REWARDS MANAGER ───────────────────────────

async def a_rewards(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("state", None)
    total = int(scalar("SELECT COUNT(*) FROM rewards"))
    used = int(scalar("SELECT COUNT(*) FROM rewards WHERE used_by IS NOT NULL"))
    mode = gs("reward_mode")
    txt = (
        "🎁 <b>Rᴇᴡᴀʀᴅ Vᴀᴜʟᴛ</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"📦 Total Codes : <b>{total}</b>\n"
        f"✅ Used : <b>{used}</b>\n"
        f"🆓 Available : <b>{total - used}</b>\n"
        f"🎛 Mode : <b>{'UNIQUE (1 code = 1 user)' if mode == 'unique' else 'SHARED (repeat allowed)'}</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "<b>Aᴅᴅ Cᴏᴅᴇs :</b> ek message me 5-6 codes bhi daal sakte hain — "
        "har line par ek code. User ko upar se niche ek-ek karke jayega."
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Aᴅᴅ Rᴇᴡᴀʀᴅ Cᴏᴅᴇs", callback_data="a_rw_add")],
        [InlineKeyboardButton("📋 Lɪsᴛ / Dᴇʟᴇᴛᴇ", callback_data="a_rw_list")],
        [InlineKeyboardButton("🔘 Rᴇᴡᴀʀᴅ Bᴜᴛᴛᴏɴs", callback_data="a_rw_btn"),
         InlineKeyboardButton("✍️ Rᴇᴡᴀʀᴅ Tᴇxᴛ", callback_data="a_set_rwtext")],
        [InlineKeyboardButton(f"🎛 Mᴏᴅᴇ : {mode.upper()}", callback_data="a_rw_mode")],
        [InlineKeyboardButton("🧹 Cʟᴇᴀʀ Usᴇᴅ", callback_data="a_rw_clrused"),
         InlineKeyboardButton("🗑 Cʟᴇᴀʀ Aʟʟ", callback_data="a_rw_clrall")],
        [InlineKeyboardButton("⬅️ Bᴀᴄᴋ", callback_data="a_home")],
    ])
    try:
        await update.callback_query.edit_message_text(txt, reply_markup=kb,
                                                     parse_mode=ParseMode.HTML)
    except BadRequest:
        pass


async def a_rw_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rw = rows("SELECT * FROM rewards ORDER BY id LIMIT 30")
    lines = ["📋 <b>Rᴇᴡᴀʀᴅ Cᴏᴅᴇs</b> (max 30 shown)\n"]
    kb = []
    if not rw:
        lines.append("<i>Koi reward code add nahi hai.</i>")
    for r in rw:
        mark = "✅" if r["used_by"] else "🆓"
        lines.append(f"{mark} <code>{esc(r['code'])}</code>"
                     + (f" → <code>{r['used_by']}</code>" if r["used_by"] else ""))
        kb.append([InlineKeyboardButton(f"🗑 {r['code'][:28]}", callback_data=f"a_rw_del:{r['id']}")])
    kb.append([InlineKeyboardButton("⬅️ Bᴀᴄᴋ", callback_data="a_rw")])
    try:
        await update.callback_query.edit_message_text(
            "\n".join(lines), reply_markup=InlineKeyboardMarkup(kb),
            parse_mode=ParseMode.HTML)
    except BadRequest:
        pass


async def notify_waitlist(context: ContextTypes.DEFAULT_TYPE):
    wl = rows("SELECT user_id FROM waitlist")
    if not wl:
        return 0
    sent = 0
    for r in wl:
        try:
            await context.bot.send_message(
                int(r["user_id"]),
                "🎉 <b>Nᴇᴡ Rᴇᴡᴀʀᴅs Aᴅᴅᴇᴅ!</b>\n\n"
                "Stock refill ho gaya hai — abhi apna reward claim kijiye 👇",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🎁 Cʟᴀɪᴍ Nᴏᴡ", callback_data="claim")]]))
            sent += 1
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(0.05)
    q("DELETE FROM waitlist")
    return sent


# ─────────────────────────── CHANNELS MANAGER ───────────────────────────

async def a_channels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("state", None)
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
        lines.append(f"• <b>{esc(c['title'])}</b>\n  <code>{c['chat_id']}</code>")
        kb.append([InlineKeyboardButton(f"🗑 Rᴇᴍᴏᴠᴇ • {(c['title'] or '')[:24]}",
                                        callback_data=f"a_ch_del:{c['chat_id']}")])
    lines.append("\n<b>Aᴅᴅ ᴋᴀʀɴᴇ ᴋᴀ ᴛᴀʀɪᴋᴀ:</b> Bot ko channel me <b>admin</b> banaiye "
                 "(Invite Users via Link permission ke saath), phir channel ka koi message "
                 "forward kijiye ya channel ID (-100...) bhejiye.")
    kb.append([InlineKeyboardButton("➕ Aᴅᴅ Cʜᴀɴɴᴇʟ", callback_data="a_ch_add")])
    kb.append([InlineKeyboardButton(
        f"🔐 Fᴏʀᴄᴇ Jᴏɪɴ : {'ON' if gi('force_join', 1) else 'OFF'}", callback_data="a_t_force"),
        InlineKeyboardButton(
        f"⚡ Aᴜᴛᴏ Aᴘᴘʀᴏᴠᴇ : {'ON' if gi('auto_approve', 1) else 'OFF'}",
        callback_data="a_t_approve")])
    kb.append([InlineKeyboardButton("♻️ Rᴇғʀᴇsʜ Iɴᴠɪᴛᴇ Lɪɴᴋs", callback_data="a_ch_relink")])
    kb.append([InlineKeyboardButton("⬅️ Bᴀᴄᴋ", callback_data="a_home")])
    try:
        await update.callback_query.edit_message_text(
            "\n".join(lines), reply_markup=InlineKeyboardMarkup(kb),
            parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    except BadRequest:
        pass


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
                chat_id, creates_join_request=True, name="Bot Gate")
            link = lk.invite_link
        else:
            link = f"https://t.me/{chat.username}"
    except Exception:  # noqa: BLE001
        link = None
    q("""INSERT INTO channels(chat_id,title,invite_link,is_private,added_at)
         VALUES(?,?,?,?,?)
         ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title,
         invite_link=excluded.invite_link, is_private=excluded.is_private""",
      (chat_id, chat.title or "Channel", link, is_priv, now()))
    _join_cache.clear()
    return chat.title or "Channel", link


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
        f"🛠 Maintenance : <b>{'ON ⚠️' if gi('maintenance', 0) else 'OFF ✅'}</b>\n"
        f"🗒 Log Channel : <b>{esc(gs('log_channel')) or '—'}</b>\n"
        f"🖼 Start Photo : <b>{'SET ✅' if gs('start_photo') else '—'}</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "<i>Text me {name} {per} {refs} {claims} {stock} {dev} variables use kar sakte hain.</i>"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔗 Rᴇғs ᴘᴇʀ Rᴇᴡᴀʀᴅ", callback_data="a_set_refs")],
        [InlineKeyboardButton(f"🎁 Bᴏɴᴜs : {'ON' if gi('bonus_enabled', 1) else 'OFF'}",
                              callback_data="a_t_bonus"),
         InlineKeyboardButton(f"🛠 Mᴀɪɴᴛ : {'ON' if gi('maintenance', 0) else 'OFF'}",
                              callback_data="a_t_maint")],
        [InlineKeyboardButton(f"🚪 Lᴇᴀᴠᴇ Pᴇɴᴀʟᴛʏ : {'ON' if gi('leave_penalty', 1) else 'OFF'}",
                              callback_data="a_t_leave"),
         InlineKeyboardButton(f"🔔 Nᴏᴛɪғʏ : {'ON' if gi('notify_referrer', 1) else 'OFF'}",
                              callback_data="a_t_notify")],
        [InlineKeyboardButton("✍️ Wᴇʟᴄᴏᴍᴇ Tᴇxᴛ", callback_data="a_set_welcome"),
         InlineKeyboardButton("✍️ Rᴇᴡᴀʀᴅ Tᴇxᴛ", callback_data="a_set_rwtext")],
        [InlineKeyboardButton("🔒 Gᴀᴛᴇ Tᴇxᴛ", callback_data="a_set_gate"),
         InlineKeyboardButton("😔 Oᴜᴛ-ᴏғ-Sᴛᴏᴄᴋ Tᴇxᴛ", callback_data="a_set_oos")],
        [InlineKeyboardButton("🖼 Sᴛᴀʀᴛ Pʜᴏᴛᴏ", callback_data="a_set_photo"),
         InlineKeyboardButton("🗒 Lᴏɢ Cʜᴀɴɴᴇʟ", callback_data="a_set_log")],
        [InlineKeyboardButton("⬅️ Bᴀᴄᴋ", callback_data="a_home")],
    ])
    try:
        await update.callback_query.edit_message_text(txt, reply_markup=kb,
                                                     parse_mode=ParseMode.HTML)
    except BadRequest:
        pass


# ─────────────────────────── USERS MANAGER ───────────────────────────

def user_card(u) -> str:
    can, need, _ = claim_state(u)
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
        f"🔗 Invited By : <code>{u['ref_by'] or '—'}</code>\n"
        f"✅ Verified : <b>{'Yes' if int(u['verified'] or 0) else 'No'}</b>\n"
        f"🚫 Blocked : <b>{'YES ⛔' if int(u['blocked'] or 0) else 'No'}</b>\n"
        f"🎁 Next Reward : <b>{'READY' if can else str(need) + ' refs baaki'}</b>"
    )


def user_card_kb(u):
    uid = int(u["user_id"])
    blocked = int(u["blocked"] or 0) == 1
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Uɴʙʟᴏᴄᴋ" if blocked else "🚫 Bʟᴏᴄᴋ",
                              callback_data=f"a_u_{'unblock' if blocked else 'block'}:{uid}")],
        [InlineKeyboardButton("🎁 Gɪғᴛ Rᴇᴡᴀʀᴅ", callback_data=f"a_u_gift:{uid}"),
         InlineKeyboardButton("➕ Aᴅᴅ Rᴇғs", callback_data=f"a_u_addref:{uid}")],
        [InlineKeyboardButton("♻️ Rᴇsᴇᴛ Cʟᴀɪᴍs", callback_data=f"a_u_reset:{uid}"),
         InlineKeyboardButton("✉️ Sᴇɴᴅ Mᴇssᴀɢᴇ", callback_data=f"a_u_msg:{uid}")],
        [InlineKeyboardButton("🗑 Dᴇʟᴇᴛᴇ Usᴇʀ", callback_data=f"a_u_del:{uid}")],
        [InlineKeyboardButton("🔍 Aɴᴏᴛʜᴇʀ Usᴇʀ", callback_data="a_u_find"),
         InlineKeyboardButton("⬅️ Bᴀᴄᴋ", callback_data="a_users")],
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
        [InlineKeyboardButton("🔍 Fɪɴᴅ Usᴇʀ", callback_data="a_u_find")],
        [InlineKeyboardButton("⛔ Bʟᴏᴄᴋᴇᴅ Lɪsᴛ", callback_data="a_u_blist"),
         InlineKeyboardButton("🆕 Lᴀᴛᴇsᴛ Usᴇʀs", callback_data="a_u_latest")],
        [InlineKeyboardButton("📤 Exᴘᴏʀᴛ CSV", callback_data="a_exp_users")],
        [InlineKeyboardButton("⬅️ Bᴀᴄᴋ", callback_data="a_home")],
    ])
    try:
        await update.callback_query.edit_message_text(txt, reply_markup=kb,
                                                     parse_mode=ParseMode.HTML)
    except BadRequest:
        pass


async def send_user_card(update: Update, context: ContextTypes.DEFAULT_TYPE, uid: int):
    u = get_user(uid)
    cq = update.callback_query
    if not u:
        if cq:
            return await cq.answer("❌ User database me nahi mila.", show_alert=True)
        return await update.effective_message.reply_html("❌ User nahi mila.")
    if cq:
        try:
            return await cq.edit_message_text(user_card(u), reply_markup=user_card_kb(u),
                                              parse_mode=ParseMode.HTML)
        except BadRequest:
            return
    await update.effective_message.reply_html(user_card(u), reply_markup=user_card_kb(u))


# ─────────────────────────── BROADCAST ───────────────────────────

async def a_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["state"] = "bc_wait"
    txt = (
        "📣 <b>Bʀᴏᴀᴅᴄᴀsᴛ</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Jo message sabko bhejna hai wo <b>abhi bhej dijiye</b> — text, photo, video, "
        "document, sticker, buttons wala forward — sab support hai.\n\n"
        "Bhejne ke baad confirm button aayega.\n\n"
        "❌ Cancel ke liye /cancel"
    )
    try:
        await update.callback_query.edit_message_text(txt, reply_markup=back_kb(),
                                                     parse_mode=ParseMode.HTML)
    except BadRequest:
        pass


async def run_broadcast(context: ContextTypes.DEFAULT_TYPE, admin_id: int,
                        from_chat: int, msg_id: int, status_msg_id: int, pin: bool = False):
    users = rows("SELECT user_id FROM users WHERE blocked=0")
    total = len(users)
    ok = fail = 0
    t0 = now()
    for i, r in enumerate(users, 1):
        uid = int(r["user_id"])
        try:
            m = await context.bot.copy_message(chat_id=uid, from_chat_id=from_chat,
                                               message_id=msg_id)
            ok += 1
            if pin:
                try:
                    await context.bot.pin_chat_message(uid, m.message_id,
                                                       disable_notification=True)
                except Exception:  # noqa: BLE001
                    pass
        except RetryAfter as e:
            await asyncio.sleep(float(e.retry_after) + 1)
            try:
                await context.bot.copy_message(chat_id=uid, from_chat_id=from_chat,
                                               message_id=msg_id)
                ok += 1
            except Exception:  # noqa: BLE001
                fail += 1
        except Forbidden:
            fail += 1
            q("UPDATE users SET blocked=1 WHERE user_id=?", (uid,))
        except Exception:  # noqa: BLE001
            fail += 1
        if i % 25 == 0:
            try:
                await context.bot.edit_message_text(
                    chat_id=admin_id, message_id=status_msg_id,
                    text=(f"📣 <b>Bʀᴏᴀᴅᴄᴀsᴛɪɴɢ…</b>\n\n"
                          f"📊 Progress : <b>{i}/{total}</b>\n"
                          f"✅ Sent : <b>{ok}</b>\n❌ Failed : <b>{fail}</b>"),
                    parse_mode=ParseMode.HTML)
            except Exception:  # noqa: BLE001
                pass
        await asyncio.sleep(0.05)
    try:
        await context.bot.edit_message_text(
            chat_id=admin_id, message_id=status_msg_id,
            text=(f"✅ <b>Bʀᴏᴀᴅᴄᴀsᴛ Cᴏᴍᴘʟᴇᴛᴇ</b>\n"
                  f"━━━━━━━━━━━━━━━━━━\n"
                  f"👥 Total : <b>{total}</b>\n"
                  f"✅ Delivered : <b>{ok}</b>\n"
                  f"❌ Failed : <b>{fail}</b>\n"
                  f"⏱ Time : <b>{now() - t0}s</b>"),
            parse_mode=ParseMode.HTML, reply_markup=back_kb())
    except Exception:  # noqa: BLE001
        pass
    await send_log(context, f"📣 <b>Broadcast</b> — sent {ok}/{total}, failed {fail}")


# ════════════════════════════════════════════════════════════════════════
#  TOOLS / EXPORT / SUB-ADMINS
# ════════════════════════════════════════════════════════════════════════

async def a_tools(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("state", None)
    txt = (
        "🛠 <b>Tᴏᴏʟs & Bᴀᴄᴋᴜᴘ</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "• Users CSV export\n• Reward codes export\n• Full database backup\n"
        "• Join-cache clear (force re-check)\n• Waitlist ko manually notify"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 Usᴇʀs CSV", callback_data="a_exp_users"),
         InlineKeyboardButton("📤 Cᴏᴅᴇs TXT", callback_data="a_exp_codes")],
        [InlineKeyboardButton("💾 DB Bᴀᴄᴋᴜᴘ", callback_data="a_backup")],
        [InlineKeyboardButton("♻️ Cʟᴇᴀʀ Jᴏɪɴ Cᴀᴄʜᴇ", callback_data="a_clr_cache"),
         InlineKeyboardButton("🔔 Nᴏᴛɪғʏ Wᴀɪᴛʟɪsᴛ", callback_data="a_wl_notify")],
        [InlineKeyboardButton("⬅️ Bᴀᴄᴋ", callback_data="a_home")],
    ])
    try:
        await update.callback_query.edit_message_text(txt, reply_markup=kb,
                                                     parse_mode=ParseMode.HTML)
    except BadRequest:
        pass


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
        kb.append([InlineKeyboardButton(f"🗑 Rᴇᴍᴏᴠᴇ {r['user_id']}",
                                        callback_data=f"a_adm_del:{r['user_id']}")])
    kb.append([InlineKeyboardButton("➕ Aᴅᴅ Sᴜʙ-Aᴅᴍɪɴ", callback_data="a_adm_add")])
    kb.append([InlineKeyboardButton("⬅️ Bᴀᴄᴋ", callback_data="a_home")])
    try:
        await update.callback_query.edit_message_text(
            "\n".join(lines), reply_markup=InlineKeyboardMarkup(kb),
            parse_mode=ParseMode.HTML)
    except BadRequest:
        pass


async def a_claim_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cl = rows("""SELECT c.code, c.kind, c.claimed_at, u.first_name, u.user_id
                 FROM claims c LEFT JOIN users u ON u.user_id=c.user_id
                 ORDER BY c.id DESC LIMIT 15""")
    lines = ["🎟 <b>Lᴀsᴛ 15 Cʟᴀɪᴍs</b>\n"]
    if not cl:
        lines.append("<i>Abhi koi claim nahi hua.</i>")
    for c in cl:
        tag = "🎁" if c["kind"] == "bonus" else "👥"
        lines.append(f"{tag} <b>{esc(c['first_name'])}</b> (<code>{c['user_id']}</code>)\n"
                     f"   <code>{esc(c['code'])}</code> • <i>{fmt_ts(c['claimed_at'])}</i>")
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Rᴇғʀᴇsʜ", callback_data="a_logs")],
        [InlineKeyboardButton("⬅️ Bᴀᴄᴋ", callback_data="a_home")],
    ])
    try:
        await update.callback_query.edit_message_text("\n".join(lines), reply_markup=kb,
                                                     parse_mode=ParseMode.HTML)
    except BadRequest:
        pass


async def a_top_refs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    top = rows("SELECT user_id,first_name,username,refs,claims FROM users "
               "ORDER BY refs DESC, claims DESC LIMIT 20")
    lines = ["🏆 <b>Tᴏᴘ 20 Rᴇғᴇʀʀᴇʀs</b>\n"]
    for i, r in enumerate(top, 1):
        lines.append(f"<b>{i}.</b> {esc(r['first_name'])} (<code>{r['user_id']}</code>) — "
                     f"👥 {int(r['refs'] or 0)} • 🎟 {int(r['claims'] or 0)}")
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Bᴀᴄᴋ", callback_data="a_home")]])
    try:
        await update.callback_query.edit_message_text("\n".join(lines), reply_markup=kb,
                                                     parse_mode=ParseMode.HTML)
    except BadRequest:
        pass


# ════════════════════════════════════════════════════════════════════════
#  ASK HELPER (admin text inputs)
# ════════════════════════════════════════════════════════════════════════

ASK_TEXT = {
    "rw_add": ("➕ <b>Aᴅᴅ Rᴇᴡᴀʀᴅ Cᴏᴅᴇs</b>\n\nHar line par ek code bhejiye.\n\n"
               "<b>Example :</b>\n<code>GOOGLE-MAP-AGENT-01\nGOOGLE-MAP-AGENT-02\n"
               "GOOGLE-MAP-AGENT-03</code>\n\n❌ /cancel"),
    "set_refs": ("🔗 <b>Rᴇғᴇʀʀᴀʟs ᴘᴇʀ Rᴇᴡᴀʀᴅ</b>\n\nEk number bhejiye (jaise <code>1</code> "
                 "ya <code>3</code>).\nMatlab: itne referral = 1 reward code.\n\n❌ /cancel"),
    "set_welcome": ("✍️ <b>Wᴇʟᴄᴏᴍᴇ Tᴇxᴛ</b>\n\nNaya text bhejiye. HTML allowed "
                    "(&lt;b&gt;bold&lt;/b&gt;).\nVariables: {name} {per} {refs} {claims} "
                    "{stock} {dev}\n\n❌ /cancel"),
    "set_rwtext": ("✍️ <b>Rᴇᴡᴀʀᴅ Mᴇssᴀɢᴇ Tᴇxᴛ</b>\n\nReward ke sath jo message jayega wo "
                   "bhejiye.\nVariables: {name} {refs} {claims} {dev}\n\n❌ /cancel"),
    "set_gate": ("🔒 <b>Fᴏʀᴄᴇ-Jᴏɪɴ Tᴇxᴛ</b>\n\nJoin screen ka message bhejiye.\n\n❌ /cancel"),
    "set_oos": ("😔 <b>Oᴜᴛ-ᴏғ-Sᴛᴏᴄᴋ Tᴇxᴛ</b>\n\nRewards khatam hone par jo message jayega.\n\n❌ /cancel"),
    "set_photo": ("🖼 <b>Sᴛᴀʀᴛ Pʜᴏᴛᴏ</b>\n\nEk photo bhejiye ya image URL.\n"
                  "Hatane ke liye <code>clear</code> bhejiye.\n\n❌ /cancel"),
    "set_log": ("🗒 <b>Lᴏɢ Cʜᴀɴɴᴇʟ</b>\n\nChannel ID bhejiye (<code>-100...</code>) — bot "
                "wahan admin ho.\nHatane ke liye <code>clear</code>.\n\n❌ /cancel"),
    "rw_btn": ("🔘 <b>Rᴇᴡᴀʀᴅ Bᴜᴛᴛᴏɴs</b>\n\nHar line par: <code>Button Text - https://link</code>\n\n"
               "<b>Example :</b>\n<code>🌐 Open Site - https://google.com\n"
               "📢 Updates - https://t.me/YourChannel</code>\n\n"
               "Hatane ke liye <code>clear</code> bhejiye.\n\n❌ /cancel"),
    "ch_add": ("➕ <b>Aᴅᴅ Fᴏʀᴄᴇ-Jᴏɪɴ Cʜᴀɴɴᴇʟ</b>\n\n1️⃣ Bot ko channel me <b>admin</b> banaiye "
               "(Invite Users via Link ✅)\n2️⃣ Us channel ka koi message yahan <b>forward</b> "
               "kijiye, ya channel ID bhejiye (<code>-100xxxxxxxxxx</code>)\n\n"
               "Private channel ke liye bot khud <b>join-request link</b> bana lega, aur "
               "request aate hi user verify ho jayega ⚡\n\n❌ /cancel"),
    "u_find": ("🔍 <b>Fɪɴᴅ Usᴇʀ</b>\n\nUser ka ID ya @username bhejiye.\n\n❌ /cancel"),
    "u_msg": ("✉️ <b>Sᴇɴᴅ Mᴇssᴀɢᴇ</b>\n\nJo message us user ko bhejna hai wo bhejiye.\n\n❌ /cancel"),
    "u_addref": ("➕ <b>Aᴅᴅ Rᴇғᴇʀʀᴀʟs</b>\n\nKitne referral add karne hain? Number bhejiye "
                 "(minus bhi chalega, jaise <code>-2</code>).\n\n❌ /cancel"),
    "adm_add": ("➕ <b>Aᴅᴅ Sᴜʙ-Aᴅᴍɪɴ</b>\n\nUser ka Telegram ID bhejiye.\n\n❌ /cancel"),
}


async def ask(update: Update, context: ContextTypes.DEFAULT_TYPE, state: str, back="a_home"):
    context.user_data["state"] = state
    try:
        await update.callback_query.edit_message_text(
            ASK_TEXT.get(state, "Value bhejiye:"), reply_markup=back_kb(back),
            parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    except BadRequest:
        pass


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

    # ───── USER ─────
    if data == "menu":
        await cq.answer()
        missing = await pending_channels(context.bot, uid)
        if missing:
            return await show_gate(update, context, missing, edit=True)
        return await show_menu(update, context, edit=True)

    if data == "verify":
        missing = await pending_channels(context.bot, uid, force_fresh=True)
        if missing:
            names = ", ".join((c["title"] or "Channel") for c in missing)
            return await cq.answer(
                f"❌ Abhi bhi pending: {names}\n\nJoin/Request karke dobara try kijiye.",
                show_alert=True)
        await cq.answer("✅ Verified!")
        if int(u["verified"] or 0) == 0:
            q("UPDATE users SET verified=1 WHERE user_id=?", (uid,))
            await credit_referral(context, uid)
        return await show_menu(update, context, edit=True)

    if data == "claim":
        return await do_claim(update, context)
    if data == "refer":
        await cq.answer()
        return await show_refer(update, context, edit=True)
    if data == "profile":
        await cq.answer()
        return await show_profile(update, context, edit=True)
    if data == "top":
        await cq.answer()
        return await show_top(update, context, edit=True)
    if data == "myrewards":
        await cq.answer()
        return await show_myrewards(update, context, edit=True)
    if data == "help":
        await cq.answer()
        return await cmd_help(update, context)

    # ───── ADMIN ─────
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
        q("DELETE FROM rewards WHERE used_by IS NOT NULL")
        return await a_rewards(update, context)
    if data == "a_rw_clrall":
        q("DELETE FROM rewards")
        return await a_rewards(update, context)
    if data == "a_rw_mode":
        ss("reward_mode", "shared" if gs("reward_mode") == "unique" else "unique")
        return await a_rewards(update, context)

    if data == "a_ch":
        return await a_channels(update, context)
    if data == "a_ch_add":
        return await ask(update, context, "ch_add", "a_ch")
    if data == "a_ch_del":
        q("DELETE FROM channels WHERE chat_id=?", (int(arg),))
        _join_cache.clear()
        return await a_channels(update, context)
    if data == "a_ch_relink":
        for c in all_channels():
            q("UPDATE channels SET invite_link=NULL WHERE chat_id=?", (c["chat_id"],))
            await ensure_link(context.bot, one("SELECT * FROM channels WHERE chat_id=?",
                                               (c["chat_id"],)))
        _join_cache.clear()
        return await a_channels(update, context)

    if data == "a_set":
        return await a_settings(update, context)
    if data in ("a_set_refs", "a_set_welcome", "a_set_rwtext", "a_set_gate",
                "a_set_oos", "a_set_photo", "a_set_log"):
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
        ss("maintenance", 0 if gi("maintenance", 0) else 1)
        return await a_settings(update, context)
    if data == "a_t_leave":
        ss("leave_penalty", 0 if gi("leave_penalty", 1) else 1)
        return await a_settings(update, context)
    if data == "a_t_notify":
        ss("notify_referrer", 0 if gi("notify_referrer", 1) else 1)
        return await a_settings(update, context)

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
            kb.append([InlineKeyboardButton(f"✅ Uɴʙʟᴏᴄᴋ {r['user_id']}",
                                            callback_data=f"a_u_unblock:{r['user_id']}")])
        kb.append([InlineKeyboardButton("⬅️ Bᴀᴄᴋ", callback_data="a_users")])
        try:
            await cq.edit_message_text("\n".join(lines),
                                       reply_markup=InlineKeyboardMarkup(kb),
                                       parse_mode=ParseMode.HTML)
        except BadRequest:
            pass
        return
    if data == "a_u_latest":
        lt = rows("SELECT user_id,first_name,refs,claims FROM users "
                  "ORDER BY joined_at DESC LIMIT 15")
        lines = ["🆕 <b>Lᴀᴛᴇsᴛ Usᴇʀs</b>\n"]
        for r in lt:
            lines.append(f"• {esc(r['first_name'])} — <code>{r['user_id']}</code> "
                         f"(👥 {int(r['refs'] or 0)} • 🎟 {int(r['claims'] or 0)})")
        try:
            await cq.edit_message_text("\n".join(lines), reply_markup=back_kb("a_users"),
                                       parse_mode=ParseMode.HTML)
        except BadRequest:
            pass
        return
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
            return await cq.answer("❌ Stock khatam — pehle codes add kijiye.", show_alert=True)
        try:
            await context.bot.send_message(
                tid,
                f"🎁 <b>Aᴅᴍɪɴ Gɪғᴛ Rᴇᴡᴀʀᴅ!</b>\n\n<code>{esc(r['code'])}</code>",
                parse_mode=ParseMode.HTML, reply_markup=reward_buttons_kb())
        except Exception:  # noqa: BLE001
            pass
        await cq.answer("🎁 Gift bhej diya!")
        return await send_user_card(update, context, tid)

    if data == "a_bc":
        return await a_broadcast(update, context)
    if data in ("a_bc_go", "a_bc_pin"):
        bc = context.user_data.get("bc")
        if not bc:
            return await cq.answer("❌ Message expire ho gaya, dobara try kijiye.",
                                   show_alert=True)
        context.user_data.pop("state", None)
        try:
            await cq.edit_message_text("📣 <b>Bʀᴏᴀᴅᴄᴀsᴛ sᴛᴀʀᴛᴇᴅ…</b>",
                                       parse_mode=ParseMode.HTML)
        except BadRequest:
            pass
        context.application.create_task(
            run_broadcast(context, uid, bc[0], bc[1], cq.message.message_id,
                          pin=(data == "a_bc_pin")))
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
            with open(DB_PATH, "rb") as f:
                bio = io.BytesIO(f.read())
            bio.name = "backup.db"
            await context.bot.send_document(uid, bio, filename="backup.db",
                                            caption="💾 Database backup")
        except Exception as e:  # noqa: BLE001
            await context.bot.send_message(uid, f"❌ Backup fail: {esc(e)}",
                                           parse_mode=ParseMode.HTML)
        return


# ════════════════════════════════════════════════════════════════════════
#  STATE ROUTER (admin text / media inputs)
# ════════════════════════════════════════════════════════════════════════

async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    tg = update.effective_user
    if not msg or not tg:
        return
    u = get_user(tg.id)
    if not u:
        register_user(tg)
        u = get_user(tg.id)
    else:
        touch_user(tg)

    state = context.user_data.get("state")

    if not state:
        if int(u["blocked"] or 0) == 1:
            return
        missing = await pending_channels(context.bot, tg.id)
        if missing:
            return await show_gate(update, context, missing)
        return await show_menu(update, context)

    if not is_admin(tg.id):
        context.user_data.pop("state", None)
        return

    text = (msg.text or msg.caption or "").strip()

    # ── BROADCAST ──
    if state == "bc_wait":
        context.user_data["bc"] = (msg.chat_id, msg.message_id)
        total = int(scalar("SELECT COUNT(*) FROM users WHERE blocked=0"))
        return await msg.reply_html(
            f"📣 <b>Cᴏɴғɪʀᴍ Bʀᴏᴀᴅᴄᴀsᴛ</b>\n\nYe message <b>{total}</b> users ko jayega.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🚀 Sᴇɴᴅ Nᴏᴡ", callback_data="a_bc_go")],
                [InlineKeyboardButton("📌 Sᴇɴᴅ + Pɪɴ", callback_data="a_bc_pin")],
                [InlineKeyboardButton("❌ Cᴀɴᴄᴇʟ", callback_data="a_home")],
            ]))

    # ── REWARD CODES ──
    if state == "rw_add":
        codes = [c.strip() for c in text.splitlines() if c.strip()]
        if not codes:
            return await msg.reply_html("❌ Koi valid code nahi mila. Dobara bhejiye.")
        added = 0
        for c in codes:
            q("INSERT INTO rewards(code,added_at) VALUES(?,?)", (c, now()))
            added += 1
        context.user_data.pop("state", None)
        sent = await notify_waitlist(context)
        return await msg.reply_html(
            f"✅ <b>{added} reward code add ho gaye!</b>\n\n"
            f"📦 Available : <b>{rewards_left()}</b>\n"
            f"🔔 Waitlist notified : <b>{sent}</b>\n\n"
            f"<i>Order:</i> user ko upar se niche ek-ek code jayega.",
            reply_markup=back_kb("a_rw"))

    # ── REWARD BUTTONS ──
    if state == "rw_btn":
        if text.lower() == "clear":
            ss("reward_buttons", "[]")
            context.user_data.pop("state", None)
            return await msg.reply_html("🧹 Reward buttons hata diye.", reply_markup=back_kb("a_rw"))
        btns = []
        bad = []
        for line in text.splitlines():
            if "-" not in line:
                continue
            t, _, url = line.partition("-")
            t, url = t.strip(), url.strip()
            if url.startswith(("http://", "https://", "tg://")) and t:
                btns.append({"text": t, "url": url})
            elif line.strip():
                bad.append(line.strip())
        if not btns:
            return await msg.reply_html(
                "❌ Format galat hai.\nUse: <code>Button Text - https://link</code>")
        ss("reward_buttons", json.dumps(btns, ensure_ascii=False))
        context.user_data.pop("state", None)
        out = f"✅ <b>{len(btns)} button set ho gaye!</b>\n\n"
        for b in btns:
            out += f"• {esc(b['text'])} → {esc(b['url'])}\n"
        if bad:
            out += f"\n⚠️ Skip: {esc(', '.join(bad))}"
        return await msg.reply_html(out, reply_markup=back_kb("a_rw"),
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
    }
    if state in simple:
        if not text:
            return await msg.reply_html("❌ Text bhejiye.")
        ss(simple[state], text)
        context.user_data.pop("state", None)
        return await msg.reply_html("✅ <b>Text update ho gaya!</b>\n\n<b>Preview:</b>\n\n"
                                    + render(text, get_user(tg.id), tg),
                                    reply_markup=back_kb("a_set"))

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
        if cid is None:
            return await msg.reply_html("❌ Channel ka message forward kijiye ya ID bhejiye.")
        try:
            title, link = await add_channel_by_id(context, cid)
        except Exception as e:  # noqa: BLE001
            return await msg.reply_html(
                f"❌ <b>Add nahi hua:</b> {esc(e)}\n\nBot ko us channel me admin banaiye "
                f"(Invite Users via Link permission ke sath) aur dobara try kijiye.")
        context.user_data.pop("state", None)
        return await msg.reply_html(
            f"✅ <b>Channel add ho gaya!</b>\n\n📢 {esc(title)}\n🆔 <code>{cid}</code>\n"
            f"🔗 {esc(link) if link else '⚠️ link nahi bana — bot ko invite permission dijiye'}",
            reply_markup=back_kb("a_ch"), disable_web_page_preview=True)

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
    context.user_data.pop("state", None)
    context.user_data.pop("bc", None)
    await update.effective_message.reply_html("❌ Cancel ho gaya.")


# ════════════════════════════════════════════════════════════════════════
#  JOIN REQUEST / MEMBERSHIP TRACKING
# ════════════════════════════════════════════════════════════════════════

async def on_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    missing = await pending_channels(context.bot, uid, force_fresh=True)
    if missing:
        return
    u = get_user(uid)
    if u and int(u["verified"] or 0) == 0:
        q("UPDATE users SET verified=1 WHERE user_id=?", (uid,))
        await credit_referral(context, uid)
    try:
        await context.bot.send_message(
            uid,
            "✅ <b>Vᴇʀɪғɪᴄᴀᴛɪᴏɴ Cᴏᴍᴘʟᴇᴛᴇ!</b>\n\n"
            "Aapka access unlock ho gaya 🎉\nAbhi apna <b>First Bonus</b> claim kijiye 👇",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🎁 Cʟᴀɪᴍ Yᴏᴜʀ Fɪʀsᴛ Bᴏɴᴜs", callback_data="claim")],
                [InlineKeyboardButton("🏠 Mᴇɴᴜ", callback_data="menu")],
            ]))
    except Exception:  # noqa: BLE001
        pass


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
        return
    if status in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED):
        q("DELETE FROM requests WHERE user_id=? AND chat_id=?", (uid, cid))
        if gi("leave_penalty", 1) == 1 and get_user(uid):
            q("UPDATE users SET verified=0 WHERE user_id=?", (uid,))
            try:
                await context.bot.send_message(
                    uid,
                    f"⚠️ <b>Aapne <i>{esc(cmu.chat.title)}</i> channel chhod diya.</b>\n\n"
                    "Bot use karne ke liye dobara join karke verify kijiye 👇",
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("🔄 Vᴇʀɪғʏ Aɢᴀɪɴ", callback_data="verify")]]))
            except Exception:  # noqa: BLE001
                pass


# ════════════════════════════════════════════════════════════════════════
#  MISC COMMANDS
# ════════════════════════════════════════════════════════════════════════

async def cmd_claim(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await do_claim(update, context)


async def cmd_refer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    missing = await pending_channels(context.bot, update.effective_user.id)
    if missing:
        return await show_gate(update, context, missing)
    await show_refer(update, context)


async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_profile(update, context)


async def cmd_top(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        f"📦 Rewards Left : <b>{rewards_left()}</b>")


async def cmd_dev(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_html(
        f"👨‍💻 <b>Dᴇᴠᴇʟᴏᴘᴇʀ</b>\n\n<b>{DEV_NAME}</b>\n"
        f"🔗 t.me/{esc(DEV_USERNAME)}\n\n<i>Mʀ. Dᴋ Sʜᴀʀᴍᴀ • Premium Bot Developer</i>",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("💬 Cᴏɴᴛᴀᴄᴛ", url=f"https://t.me/{DEV_USERNAME}")]]))


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
            BotCommand("claim", "Reward claim kare"),
            BotCommand("refer", "Referral link"),
            BotCommand("profile", "Aapki details"),
            BotCommand("top", "Leaderboard"),
            BotCommand("help", "Help"),
        ])
    except Exception:  # noqa: BLE001
        pass
    log.info("Bot @%s started | Developer: %s", BOT_USERNAME, DEV_NAME)
    for a in admin_ids():
        try:
            await app.bot.send_message(
                a, f"✅ <b>Bot Online!</b>\n@{BOT_USERNAME}\n\n"
                   f"Admin panel : /admin\n\n<i>Bᴏᴛ ʙʏ</i> <b>{DEV_NAME}</b>",
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

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("claim", cmd_claim))
    app.add_handler(CommandHandler("refer", cmd_refer))
    app.add_handler(CommandHandler("profile", cmd_profile))
    app.add_handler(CommandHandler("top", cmd_top))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("panel", cmd_admin))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("dev", cmd_dev))
    app.add_handler(CommandHandler("developer", cmd_dev))

    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(ChatJoinRequestHandler(on_join_request))
    app.add_handler(ChatMemberHandler(on_chat_member, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, on_message))
    app.add_error_handler(on_error)

    log.info("Polling start… Developer: %s", DEV_NAME)
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
