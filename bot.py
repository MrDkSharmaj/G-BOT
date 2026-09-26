#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Premium Referral & Reward Bot + Telegram Mini App  —  single-file edition
Developer / branding: DK Sharma (configurable via DEV_NAME / DEV_USERNAME)

Run:
    pip install -r requirements.txt
    python bot.py                 # bot + Mini App server + workers (one process)
    python bot.py --self-test     # embedded tests on a temporary database
    python bot.py --check-config  # validate environment and exit

SECTION MAP  (search for "# ==== " to jump)
  1  Configuration and validation          11 Reward ledger and claims
  2  Constants and localization            12 Engagement: promo, check-in, leaderboards, waitlist
  3  Utilities                             13 Jobs, broadcasts, backups
  4  Database connections / transactions   14 Native user handlers
  5  Schema and versioned migrations       15 Native admin handlers and routers
  6  Settings and content                  16 Embedded Mini App (HTML/CSS/JS)
  7  Roles, permissions, audit, rate limit 17 HTTP API (FastAPI)
  8  Mini App auth, sessions, challenges   18 Workers
  9  Referral engine                       19 Self-tests
 10  Channel verification (force-join)     20 Startup and graceful shutdown
"""
from __future__ import annotations

import asyncio
import contextlib
import csv
import hashlib
import hmac
import html
import io
import json
import logging
import os
import re
import secrets
import shutil
import signal
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from telegram import (BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions,
                      MenuButtonWebApp, Update, WebAppInfo)
from telegram.constants import ChatMemberStatus, ParseMode
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TelegramError, TimedOut
from telegram.ext import (Application, ApplicationBuilder, CallbackQueryHandler, ChatJoinRequestHandler,
                          ChatMemberHandler, CommandHandler, ContextTypes, MessageHandler, filters)

from fastapi import FastAPI, Header, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel, Field
import uvicorn

# ==== 1. CONFIGURATION AND VALIDATION =========================================================

def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


@dataclass
class Config:
    bot_token: str = field(default_factory=lambda: _env("BOT_TOKEN"))
    owner_id: int = field(default_factory=lambda: _env_int("OWNER_ID", 0))
    admin_ids: set = field(default_factory=lambda: {int(x) for x in _env("ADMIN_IDS").replace(" ", "").split(",") if x.isdigit()})
    dev_name: str = field(default_factory=lambda: _env("DEV_NAME", "DK Sharma"))
    dev_username: str = field(default_factory=lambda: _env("DEV_USERNAME", "DkSharma").lstrip("@"))
    db_path: str = field(default_factory=lambda: _env("DB_PATH", "bot_data.db"))
    public_base_url: str = field(default_factory=lambda: _env("PUBLIC_BASE_URL").rstrip("/"))
    host: str = field(default_factory=lambda: _env("HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: _env_int("PORT", 8080))
    app_env: str = field(default_factory=lambda: _env("APP_ENV", "production").lower())
    session_secret: str = field(default_factory=lambda: _env("SESSION_SECRET"))
    bot_mode: str = field(default_factory=lambda: _env("BOT_MODE", "polling").lower())
    webhook_secret: str = field(default_factory=lambda: _env("WEBHOOK_SECRET"))
    report_tz: str = field(default_factory=lambda: _env("REPORT_TIMEZONE", "Asia/Kolkata"))
    session_policy: str = field(default_factory=lambda: _env("SESSION_POLICY", "notify").lower())
    session_ttl: int = field(default_factory=lambda: _env_int("SESSION_TTL_SECONDS", 6 * 3600))
    challenge_ttl: int = field(default_factory=lambda: _env_int("CHALLENGE_TTL_SECONDS", 600))
    approval_ttl: int = field(default_factory=lambda: _env_int("APPROVAL_TTL_SECONDS", 30 * 86400))
    max_sessions: int = field(default_factory=lambda: _env_int("MAX_ACTIVE_SESSIONS", 5))
    retention_days: int = field(default_factory=lambda: _env_int("RETENTION_DAYS", 90))
    init_data_max_age: int = field(default_factory=lambda: _env_int("INITDATA_MAX_AGE_SECONDS", 900))
    trusted_proxy: str = field(default_factory=lambda: _env("TRUSTED_PROXY_CONFIG", "none").lower())
    dev_auth_bypass: str = field(default_factory=lambda: _env("DEV_AUTH_BYPASS_USER_ID"))
    backup_dir: str = field(default_factory=lambda: _env("BACKUP_DIR", "backups"))
    drop_pending: bool = field(default_factory=lambda: _env("DROP_PENDING_UPDATES", "0") == "1")

    @property
    def production(self) -> bool:
        return self.app_env == "production"

    def validate(self, need_token: bool = True) -> List[str]:
        errors: List[str] = []
        if need_token and not self.bot_token:
            errors.append("BOT_TOKEN is required")
        if not self.owner_id:
            errors.append("OWNER_ID is required (numeric Telegram user id)")
        if self.bot_mode not in ("polling", "webhook"):
            errors.append("BOT_MODE must be 'polling' or 'webhook'")
        if self.bot_mode == "webhook" and (not self.public_base_url.startswith("https://") or not self.webhook_secret):
            errors.append("BOT_MODE=webhook needs an https PUBLIC_BASE_URL and WEBHOOK_SECRET")
        if self.public_base_url and not self.public_base_url.startswith(("https://", "http://")):
            errors.append("PUBLIC_BASE_URL must start with https://")
        if self.production and self.public_base_url.startswith("http://"):
            errors.append("PUBLIC_BASE_URL must be https in production")
        if self.production and self.session_secret and len(self.session_secret) < 32:
            errors.append("SESSION_SECRET must be at least 32 characters")
        if self.production and self.dev_auth_bypass:
            errors.append("DEV_AUTH_BYPASS_USER_ID is forbidden in production")
        if self.session_policy not in ("off", "notify", "approve", "risk"):
            errors.append("SESSION_POLICY must be off|notify|approve|risk")
        if self.trusted_proxy not in ("none", "xff"):
            errors.append("TRUSTED_PROXY_CONFIG must be 'none' or 'xff'")
        try:
            ZoneInfo(self.report_tz)
        except Exception:  # noqa: BLE001
            errors.append("REPORT_TIMEZONE is not a valid IANA zone")
        return errors


CFG = Config()
logging.basicConfig(format="%(asctime)s | %(levelname)s | %(name)s | %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
log = logging.getLogger("rewardbot")

SCHEMA_VERSION = 2
BOT_USERNAME = ""          # filled at startup
APP_STARTED_AT = int(time.time())


class Runtime:
    """Process-wide handles shared by bot handlers, API routes and workers."""
    application: Optional[Application] = None
    stop_event: Optional[asyncio.Event] = None
    writes_paused: bool = False     # set during restore
    ready: bool = False
    session_secret: bytes = b""


RT = Runtime()

# ==== 2. CONSTANTS AND LOCALIZATION ===========================================================

ROLE_OWNER, ROLE_ADMIN, ROLE_REWARD, ROLE_SUPPORT, ROLE_ANALYST = "owner", "admin", "reward_manager", "support", "analyst"
ROLE_LABELS = {ROLE_OWNER: "Owner", ROLE_ADMIN: "Administrator", ROLE_REWARD: "Reward Manager",
               ROLE_SUPPORT: "Support Agent", ROLE_ANALYST: "Analyst"}
PERMS = {
    "view_overview", "view_analytics", "vault_view", "vault_manage", "vault_reveal", "vault_export",
    "users_view", "users_manage", "users_adjust", "referrals_review", "channels_manage", "broadcast",
    "content_manage", "support_view", "support_reply", "sessions_view", "sessions_manage", "roles_manage",
    "audit_view", "backup", "restore", "settings_manage", "export",
}
ROLE_PERMS: Dict[str, set] = {
    ROLE_OWNER: set(PERMS),
    ROLE_ADMIN: set(PERMS) - {"roles_manage", "restore"},
    ROLE_REWARD: {"view_overview", "vault_view", "vault_manage", "vault_reveal", "users_view", "users_adjust", "audit_view"},
    ROLE_SUPPORT: {"view_overview", "users_view", "support_view", "support_reply", "sessions_view", "referrals_review"},
    ROLE_ANALYST: {"view_overview", "view_analytics", "audit_view", "users_view"},
}
CLAIM_KINDS = {"bonus": "Welcome bonus", "referral": "Referral reward", "gift": "Admin gift",
               "promo": "Promo code", "checkin": "Check-in reward"}
TEMPLATE_KEYS = {
    "welcome_text": "Onboarding / dashboard", "gate_text": "Channel gate", "reward_text": "Claim success",
    "outofstock_text": "Out of stock", "restock_text": "Restock alert", "rules_text": "Rules",
    "help_text": "Help", "support_text": "Support intro", "maintenance_text": "Maintenance",
    "referral_text": "Referral instructions", "share_text": "Share message",
}
ALLOWED_PLACEHOLDERS = {"name", "per", "refs", "claims", "stock", "dev", "bot", "need", "balance"}

I18N: Dict[str, Dict[str, str]] = {
    "en": {
        "menu": "Home", "back": "Back", "cancel": "Cancel", "refresh": "Refresh", "confirm": "Confirm", "help": "Help",
        "claim_bonus": "Claim Welcome Bonus", "claim_reward": "Claim Available Reward", "verify": "Complete Channel Verification",
        "approve_session": "Approve This Session", "invite": "Invite More Friends", "restock": "Subscribe to Restock Alerts",
        "refer": "Refer & Earn", "profile": "Profile", "rewards": "My Rewards", "top": "Leaderboard", "activity": "Referral Activity",
        "security": "Security & Sessions", "support": "Support", "open_app": "Open Mini App", "promo": "Redeem Promo Code",
        "checkin": "Daily Check-in", "lang": "भाषा: हिन्दी", "suspended": "Your account is suspended.\nReason: {reason}\nContact support if you think this is a mistake.",
        "maintenance": "The bot is under maintenance. Please try again later.",
        "verification_unavailable": "Verification is temporarily unavailable (Telegram did not answer). Please try again in a minute.",
        "verified": "Verified!", "still_pending": "Still pending: {names}\nJoin / request and try again.",
        "claimed": "Reward unlocked!", "locked": "Reward locked. {need} more referral(s) needed ({per} referral = 1 reward).",
        "oos": "Rewards are out of stock right now.", "expired_menu": "This menu has expired. Use /start to open a fresh one.",
        "not_allowed": "You are not allowed to do that.", "session_needed": "A session approval is required before claiming. Tap Approve in the message we just sent you.",
    },
    "hi": {
        "menu": "होम", "back": "वापस", "cancel": "रद्द", "refresh": "रीफ़्रेश", "confirm": "पुष्टि", "help": "मदद",
        "claim_bonus": "वेलकम बोनस लें", "claim_reward": "उपलब्ध रिवॉर्ड लें", "verify": "चैनल वेरिफ़िकेशन पूरा करें",
        "approve_session": "यह सेशन अप्रूव करें", "invite": "और दोस्तों को बुलाएँ", "restock": "रीस्टॉक अलर्ट चालू करें",
        "refer": "रेफ़र करें", "profile": "प्रोफ़ाइल", "rewards": "मेरे रिवॉर्ड", "top": "लीडरबोर्ड", "activity": "रेफ़रल गतिविधि",
        "security": "सुरक्षा और सेशन", "support": "सहायता", "open_app": "मिनी ऐप खोलें", "promo": "प्रोमो कोड",
        "checkin": "डेली चेक-इन", "lang": "Language: English", "suspended": "आपका अकाउंट निलंबित है।\nकारण: {reason}",
        "maintenance": "बॉट रखरखाव में है। कृपया बाद में प्रयास करें।",
        "verification_unavailable": "वेरिफ़िकेशन अभी उपलब्ध नहीं है (Telegram ने जवाब नहीं दिया)। एक मिनट बाद प्रयास करें।",
        "verified": "वेरिफ़ाइड!", "still_pending": "अभी बाकी: {names}\nजॉइन करके फिर प्रयास करें।",
        "claimed": "रिवॉर्ड अनलॉक!", "locked": "रिवॉर्ड लॉक है। {need} और रेफ़रल चाहिए ({per} रेफ़रल = 1 रिवॉर्ड)।",
        "oos": "अभी रिवॉर्ड स्टॉक में नहीं हैं।", "expired_menu": "यह मेनू पुराना हो गया। /start से नया खोलें।",
        "not_allowed": "आपको इसकी अनुमति नहीं है।", "session_needed": "क्लेम से पहले सेशन अप्रूवल ज़रूरी है। हमने जो संदेश भेजा है उसमें Approve दबाएँ।",
    },
}


def T(lang: str, key: str, **kw: Any) -> str:
    s = I18N.get(lang or "en", I18N["en"]).get(key) or I18N["en"].get(key, key)
    for k, v in kw.items():
        s = s.replace("{" + k + "}", str(v))
    return s


DEFAULT_SETTINGS: Dict[str, str] = {
    "refs_per_reward": "1", "bonus_enabled": "1", "force_join": "1", "auto_approve": "1", "maintenance": "0",
    "reward_mode": "unique", "leave_penalty": "1", "notify_referrer": "1", "log_channel": "", "start_photo": "",
    "reward_buttons": "[]", "session_policy": "", "low_stock_threshold": "3", "restock_cooldown_hours": "12",
    "checkin_points": "0", "leaderboard_enabled": "1", "qualification_delay_seconds": "0", "support_enabled": "1",
    "gift_consumes_credit": "0", "support_contact": "", "welcome_media_type": "photo",
    "welcome_text": ("👋 <b>Welcome {name}!</b>\n\nHere you can unlock <b>exclusive reward codes</b>.\n"
                     "🎁 Your first reward is a <b>free welcome bonus</b>.\n👥 After that, every <b>{per} referral</b> unlocks a new reward.\n\nStart with the buttons below 👇"),
    "reward_text": "🎉 <b>Congratulations {name}!</b>\n\nYour reward is unlocked 👇",
    "gate_text": "🔒 <b>Access locked</b>\n\nJoin the channel(s) below, then tap <b>Verify</b>.",
    "outofstock_text": ("😔 <b>Rewards are out of stock</b>\n\nAll current rewards have been claimed. "
                        "Turn on restock alerts and we will message you when new codes arrive."),
    "restock_text": "🎉 <b>New rewards added!</b>\n\nStock has been refilled — claim your reward now 👇",
    "rules_text": ("📜 <b>Rules</b>\n\n1. Join all required channels and stay joined.\n2. One account per person; self-referrals do not count.\n"
                   "3. A referral counts when your friend starts the bot and completes verification.\n4. Rewards are limited by stock.\n"
                   "5. Abuse leads to suspension. Decisions are audited and can be reviewed via Support."),
    "help_text": ("❓ <b>How it works</b>\n\n1️⃣ Join the required channels.\n2️⃣ Claim your welcome bonus.\n3️⃣ Every <b>{per} referral</b> unlocks another reward.\n"
                  "4️⃣ Share your link from Refer & Earn.\n\n/start – menu\n/claim – claim\n/refer – link\n/rewards – my rewards\n/support – help desk\n/security – sessions"),
    "support_text": "🛟 <b>Support</b>\n\nDescribe your problem in one message. Include a claim reference if it is about a reward.",
    "maintenance_text": "🛠 <b>Maintenance</b>\n\nThe bot is being updated. Please try again shortly.",
    "referral_text": "Every <b>{per} referral</b> = <b>1 reward</b>. A referral counts only after your friend joins the channels and verifies.",
    "share_text": "🎁 Get free reward codes from this bot!",
}

# ==== 3. UTILITIES ============================================================================

def now() -> int:
    return int(time.time())


def esc(x: Any) -> str:
    return html.escape("" if x is None else str(x), quote=True)


def tz() -> ZoneInfo:
    try:
        return ZoneInfo(CFG.report_tz)
    except Exception:  # noqa: BLE001
        return ZoneInfo("UTC")


def fmt_ts(ts: Any) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(int(ts), tz()).strftime("%d %b %Y, %H:%M")


def day_key(ts: Optional[int] = None) -> str:
    return datetime.fromtimestamp(ts or now(), tz()).strftime("%Y-%m-%d")


def period_start(kind: str) -> int:
    d = datetime.fromtimestamp(now(), tz())
    if kind == "week":
        d = d - timedelta(days=d.weekday())
    elif kind == "month":
        d = d.replace(day=1)
    else:
        return 0
    return int(d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


def sha256_hex(s: str | bytes) -> str:
    return hashlib.sha256(s.encode() if isinstance(s, str) else s).hexdigest()


def new_id(prefix: str = "") -> str:
    return prefix + secrets.token_urlsafe(18)


def claim_reference() -> str:
    return "CL-" + secrets.token_hex(4).upper()


def mask_code(code: str) -> str:
    code = code or ""
    if len(code) <= 4:
        return "•" * len(code)
    return code[:2] + "•" * max(3, len(code) - 4) + code[-2:]


def csv_safe(v: Any) -> str:
    s = "" if v is None else str(v)
    if s and s[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + s
    return s


def safe_url(u: str) -> Optional[str]:
    u = (u or "").strip()
    try:
        p = urllib.parse.urlsplit(u)
    except ValueError:
        return None
    if p.scheme in ("http", "https") and p.netloc:
        return u
    if p.scheme == "tg" and u.startswith("tg://"):
        return u
    return None


def parse_buttons(raw: str) -> List[Dict[str, Any]]:
    try:
        data = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    out = []
    for b in data if isinstance(data, list) else []:
        if isinstance(b, dict) and safe_url(str(b.get("url", ""))) and str(b.get("text", "")).strip():
            out.append({"text": str(b["text"])[:40], "url": str(b["url"]), "row": int(b.get("row", 0) or 0)})
    return out


def buttons_markup(btns: List[Dict[str, Any]], tail: Optional[List[List[InlineKeyboardButton]]] = None) -> InlineKeyboardMarkup:
    rows: Dict[int, List[InlineKeyboardButton]] = {}
    for i, b in enumerate(btns):
        rows.setdefault(b.get("row") or (i // 2 + 1), []).append(InlineKeyboardButton(b["text"], url=b["url"]))
    kb = [rows[k][:4] for k in sorted(rows)]
    if tail:
        kb.extend(tail)
    return InlineKeyboardMarkup(kb)


def validate_template(text: str, limit: int = 4096) -> Optional[str]:
    if not text or not text.strip():
        return "Text is empty."
    if len(text) > limit:
        return f"Text is too long ({len(text)} > {limit})."
    unknown = {m for m in re.findall(r"\{([a-zA-Z_]+)\}", text)} - ALLOWED_PLACEHOLDERS
    if unknown:
        return "Unknown placeholder(s): " + ", ".join(sorted("{" + u + "}" for u in unknown))
    tags = re.findall(r"</?([a-zA-Z]+)[^>]*>", text)
    bad = {t.lower() for t in tags} - {"b", "i", "u", "s", "code", "pre", "a", "tg-spoiler", "blockquote", "strong", "em"}
    if bad:
        return "Unsupported HTML tag(s): " + ", ".join(sorted(bad))
    return None


def render_template(text: str, values: Dict[str, Any]) -> str:
    out = text or ""
    for k in ALLOWED_PLACEHOLDERS:
        out = out.replace("{" + k + "}", esc(values.get(k, "")))
    return out


def tg_link_off() -> LinkPreviewOptions:
    return LinkPreviewOptions(is_disabled=True)


def ref_link(uid: int, campaign: str = "") -> str:
    tail = f"{uid}" if not campaign else f"{uid}_{campaign}"
    return f"https://t.me/{BOT_USERNAME}?start={tail}"


def app_url(path: str = "") -> str:
    return (CFG.public_base_url or "") + "/app" + path

# ==== 4. DATABASE CONNECTION MANAGEMENT =======================================================

class Database:
    """Transaction-scoped connections. No transaction is ever held across an await."""

    def __init__(self, path: str):
        self.path = path
        self._init_lock = threading.Lock()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def init_pragmas(self) -> None:
        with self._init_lock:
            c = self.connect()
            try:
                c.execute("PRAGMA journal_mode=WAL")
                c.execute("PRAGMA synchronous=NORMAL")
            finally:
                c.close()

    @contextlib.contextmanager
    def tx(self):
        if RT.writes_paused:
            raise RuntimeError("writes are paused for maintenance/restore")
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    @contextlib.contextmanager
    def ro(self):
        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()

    def one(self, sql: str, args: Sequence = ()) -> Optional[sqlite3.Row]:
        with self.ro() as c:
            return c.execute(sql, args).fetchone()

    def all(self, sql: str, args: Sequence = ()) -> List[sqlite3.Row]:
        with self.ro() as c:
            return c.execute(sql, args).fetchall()

    def scalar(self, sql: str, args: Sequence = (), default: Any = 0) -> Any:
        r = self.one(sql, args)
        if r is None or r[0] is None:
            return default
        return r[0]

    def run(self, sql: str, args: Sequence = ()) -> int:
        with self.tx() as c:
            return c.execute(sql, args).rowcount


DB = Database(CFG.db_path)


def use_database(path: str) -> None:
    """Switch the global database (used by self-tests and restore)."""
    global DB
    DB = Database(path)

# ==== 5. SCHEMA AND VERSIONED MIGRATIONS ======================================================

def _table_exists(c: sqlite3.Connection, name: str) -> bool:
    return c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _cols(c: sqlite3.Connection, table: str) -> set:
    return {r[1] for r in c.execute(f"PRAGMA table_info({table})")}


def _add_col(c: sqlite3.Connection, table: str, col: str, decl: str) -> None:
    if col not in _cols(c, table):
        c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def migrate_v1(c: sqlite3.Connection) -> None:
    """Baseline = the legacy schema (created only when missing)."""
    c.executescript("""
    CREATE TABLE IF NOT EXISTS users(user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, ref_by INTEGER,
        ref_done INTEGER DEFAULT 0, refs INTEGER DEFAULT 0, claims INTEGER DEFAULT 0, verified INTEGER DEFAULT 0,
        blocked INTEGER DEFAULT 0, joined_at INTEGER, last_seen INTEGER);
    CREATE TABLE IF NOT EXISTS rewards(id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT, used_by INTEGER, used_at INTEGER, added_at INTEGER);
    CREATE TABLE IF NOT EXISTS claims(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, reward_id INTEGER, code TEXT, kind TEXT, claimed_at INTEGER);
    CREATE TABLE IF NOT EXISTS channels(chat_id INTEGER PRIMARY KEY, title TEXT, invite_link TEXT, is_private INTEGER DEFAULT 1, added_at INTEGER);
    CREATE TABLE IF NOT EXISTS requests(user_id INTEGER, chat_id INTEGER, status TEXT, ts INTEGER, PRIMARY KEY(user_id, chat_id));
    CREATE TABLE IF NOT EXISTS admins(user_id INTEGER PRIMARY KEY, added_by INTEGER, added_at INTEGER);
    CREATE TABLE IF NOT EXISTS waitlist(user_id INTEGER PRIMARY KEY, ts INTEGER);
    CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, val TEXT);
    """)


def migrate_v2(c: sqlite3.Connection) -> None:
    """Ledger, referrals, sessions, jobs, support, audit, content and legacy reconciliation."""
    for col, decl in [("lang", "TEXT DEFAULT 'en'"), ("suspended", "INTEGER DEFAULT 0"), ("suspended_reason", "TEXT"),
                      ("suspended_at", "INTEGER"), ("unreachable_at", "INTEGER"), ("bonus_consumed_at", "INTEGER"),
                      ("notify_new_session", "INTEGER DEFAULT 1"), ("public_name", "INTEGER DEFAULT 1"),
                      ("deleted_at", "INTEGER"), ("notes", "TEXT"), ("tags", "TEXT"), ("campaign", "TEXT")]:
        _add_col(c, "users", col, decl)
    for col, decl in [("status", "TEXT"), ("category", "TEXT DEFAULT 'default'"), ("batch_id", "INTEGER"),
                      ("expires_at", "INTEGER"), ("note", "TEXT")]:
        _add_col(c, "rewards", col, decl)
    for col, decl in [("claim_ref", "TEXT"), ("idem_key", "TEXT"), ("delivery_status", "TEXT DEFAULT 'delivered'"),
                      ("delivered_at", "INTEGER"), ("delivery_attempts", "INTEGER DEFAULT 0"), ("rule_snapshot", "TEXT"),
                      ("category", "TEXT DEFAULT 'default'"), ("issued_by", "INTEGER"), ("note", "TEXT")]:
        _add_col(c, "claims", col, decl)
    for col, decl in [("policy", "TEXT DEFAULT 'member'"), ("enabled", "INTEGER DEFAULT 1"), ("sort_order", "INTEGER DEFAULT 0"),
                      ("auto_approve", "INTEGER DEFAULT 1"), ("last_error", "TEXT"), ("last_checked", "INTEGER"), ("username", "TEXT")]:
        _add_col(c, "channels", col, decl)
    _add_col(c, "admins", "role", "TEXT DEFAULT 'admin'")
    c.executescript("""
    CREATE TABLE IF NOT EXISTS schema_meta(key TEXT PRIMARY KEY, val TEXT);
    CREATE TABLE IF NOT EXISTS ledger(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, delta INTEGER NOT NULL,
        kind TEXT NOT NULL, ref_type TEXT, ref_id TEXT, note TEXT, rule_snapshot TEXT, idem_key TEXT UNIQUE, actor_id INTEGER,
        created_at INTEGER NOT NULL);
    CREATE INDEX IF NOT EXISTS ix_ledger_user ON ledger(user_id);
    CREATE TABLE IF NOT EXISTS referrals(id INTEGER PRIMARY KEY AUTOINCREMENT, referee_id INTEGER NOT NULL UNIQUE, referrer_id INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending', reason TEXT, campaign TEXT, created_at INTEGER NOT NULL, qualified_at INTEGER,
        updated_at INTEGER, eligible_at INTEGER);
    CREATE INDEX IF NOT EXISTS ix_ref_referrer ON referrals(referrer_id, status);
    CREATE TABLE IF NOT EXISTS waitlist_v2(user_id INTEGER NOT NULL, category TEXT NOT NULL DEFAULT 'default', created_at INTEGER,
        last_notified_at INTEGER, active INTEGER DEFAULT 1, PRIMARY KEY(user_id, category));
    CREATE TABLE IF NOT EXISTS installations(id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, key_hash TEXT NOT NULL UNIQUE, platform TEXT,
        status TEXT NOT NULL DEFAULT 'pending', first_seen INTEGER, last_seen INTEGER, approved_until INTEGER, label TEXT);
    CREATE INDEX IF NOT EXISTS ix_inst_user ON installations(user_id);
    CREATE TABLE IF NOT EXISTS app_sessions(id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, token_hash TEXT NOT NULL UNIQUE,
        installation_id TEXT, platform TEXT, created_at INTEGER, last_used_at INTEGER, expires_at INTEGER, revoked_at INTEGER);
    CREATE INDEX IF NOT EXISTS ix_sess_user ON app_sessions(user_id);
    CREATE TABLE IF NOT EXISTS challenges(id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, installation_id TEXT, action TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending', created_at INTEGER, expires_at INTEGER, consumed_at INTEGER, message_id INTEGER, meta TEXT);
    CREATE INDEX IF NOT EXISTS ix_chal_user ON challenges(user_id, status);
    CREATE TABLE IF NOT EXISTS confirmations(id TEXT PRIMARY KEY, actor_id INTEGER NOT NULL, action TEXT NOT NULL, target TEXT,
        payload TEXT, created_at INTEGER, expires_at INTEGER, used_at INTEGER);
    CREATE TABLE IF NOT EXISTS jobs(id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, payload TEXT, status TEXT NOT NULL DEFAULT 'pending',
        attempts INTEGER DEFAULT 0, next_run_at INTEGER, last_error TEXT, created_at INTEGER, done_at INTEGER, idem_key TEXT UNIQUE);
    CREATE INDEX IF NOT EXISTS ix_jobs_due ON jobs(status, next_run_at);
    CREATE TABLE IF NOT EXISTS broadcasts(id INTEGER PRIMARY KEY AUTOINCREMENT, created_by INTEGER, from_chat_id INTEGER, message_id INTEGER,
        audience TEXT DEFAULT 'all', status TEXT DEFAULT 'draft', pin INTEGER DEFAULT 0, scheduled_at INTEGER, created_at INTEGER,
        started_at INTEGER, finished_at INTEGER, total INTEGER DEFAULT 0, sent INTEGER DEFAULT 0, failed INTEGER DEFAULT 0,
        skipped INTEGER DEFAULT 0, cursor INTEGER DEFAULT 0, buttons TEXT);
    CREATE TABLE IF NOT EXISTS broadcast_targets(broadcast_id INTEGER NOT NULL, user_id INTEGER NOT NULL, status TEXT DEFAULT 'pending',
        error TEXT, attempts INTEGER DEFAULT 0, updated_at INTEGER, PRIMARY KEY(broadcast_id, user_id));
    CREATE TABLE IF NOT EXISTS support_tickets(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, category TEXT, subject TEXT,
        status TEXT DEFAULT 'open', claim_ref TEXT, assigned_to INTEGER, created_at INTEGER, updated_at INTEGER);
    CREATE TABLE IF NOT EXISTS support_messages(id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id INTEGER NOT NULL, sender_id INTEGER,
        is_staff INTEGER DEFAULT 0, internal INTEGER DEFAULT 0, body TEXT, file_id TEXT, created_at INTEGER);
    CREATE TABLE IF NOT EXISTS audit_events(id INTEGER PRIMARY KEY AUTOINCREMENT, actor_id INTEGER, action TEXT NOT NULL, target TEXT,
        before TEXT, after TEXT, reason TEXT, result TEXT, created_at INTEGER);
    CREATE INDEX IF NOT EXISTS ix_audit_time ON audit_events(created_at);
    CREATE TABLE IF NOT EXISTS content_versions(id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL, lang TEXT DEFAULT 'en', body TEXT,
        version INTEGER, status TEXT DEFAULT 'draft', created_by INTEGER, created_at INTEGER);
    CREATE TABLE IF NOT EXISTS promo_codes(id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT NOT NULL UNIQUE, points INTEGER NOT NULL,
        max_total INTEGER DEFAULT 0, max_per_user INTEGER DEFAULT 1, expires_at INTEGER, enabled INTEGER DEFAULT 1, redeemed INTEGER DEFAULT 0, created_at INTEGER);
    CREATE TABLE IF NOT EXISTS promo_redemptions(promo_id INTEGER NOT NULL, user_id INTEGER NOT NULL, n INTEGER NOT NULL, created_at INTEGER,
        PRIMARY KEY(promo_id, user_id, n));
    CREATE TABLE IF NOT EXISTS checkins(user_id INTEGER NOT NULL, day TEXT NOT NULL, created_at INTEGER, PRIMARY KEY(user_id, day));
    CREATE TABLE IF NOT EXISTS backups(id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT, size INTEGER, created_at INTEGER, ok INTEGER, note TEXT);
    CREATE TABLE IF NOT EXISTS reward_batches(id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT, category TEXT, created_by INTEGER,
        count INTEGER, created_at INTEGER);
    CREATE TABLE IF NOT EXISTS announcements(id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, body TEXT, created_by INTEGER,
        created_at INTEGER, enabled INTEGER DEFAULT 1);
    CREATE TABLE IF NOT EXISTS activity(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, kind TEXT, meta TEXT, created_at INTEGER);
    CREATE INDEX IF NOT EXISTS ix_activity_user ON activity(user_id, id);
    CREATE UNIQUE INDEX IF NOT EXISTS ux_claims_ref ON claims(claim_ref);
    CREATE UNIQUE INDEX IF NOT EXISTS ux_claims_idem ON claims(idem_key);
    CREATE INDEX IF NOT EXISTS ix_claims_user ON claims(user_id, id);
    CREATE INDEX IF NOT EXISTS ix_rewards_status ON rewards(status, category, id);
    """)
    for k, v in DEFAULT_SETTINGS.items():
        c.execute("INSERT OR IGNORE INTO settings(key,val) VALUES(?,?)", (k, v))
    ts = now()
    report: Dict[str, Any] = {"migrated_at": ts, "notes": []}
    # rewards: derive status; detect duplicate *available* codes (allocated history is never touched)
    c.execute("UPDATE rewards SET status=CASE WHEN used_by IS NULL THEN 'available' ELSE 'allocated' END WHERE status IS NULL")
    dups = c.execute("SELECT code, COUNT(*) n FROM rewards WHERE status='available' GROUP BY code HAVING n>1").fetchall()
    for d in dups:
        ids = [r[0] for r in c.execute("SELECT id FROM rewards WHERE code=? AND status='available' ORDER BY id", (d["code"],)).fetchall()]
        for rid in ids[1:]:
            c.execute("UPDATE rewards SET status='disabled', note='legacy duplicate of available code' WHERE id=?", (rid,))
        report["notes"].append(f"duplicate available code {mask_code(d['code'])}: kept id {ids[0]}, disabled {ids[1:]}")
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_rewards_code_available ON rewards(code) WHERE status='available'")
    # claims: references, delivery state (legacy claims were shown to the user in-chat => treated as delivered)
    for r in c.execute("SELECT id FROM claims WHERE claim_ref IS NULL").fetchall():
        c.execute("UPDATE claims SET claim_ref=?, idem_key=?, delivery_status='delivered', delivered_at=claimed_at, "
                  "rule_snapshot='legacy' WHERE id=?", (f"CL-L{r['id']}", f"legacy:{r['id']}", r["id"]))
    # users: bonus consumption + suspension + referral records
    c.execute("UPDATE users SET bonus_consumed_at=(SELECT MIN(claimed_at) FROM claims WHERE claims.user_id=users.user_id AND kind='bonus') "
              "WHERE bonus_consumed_at IS NULL")
    c.execute("UPDATE users SET suspended=1, suspended_reason='legacy block', suspended_at=? WHERE blocked=1 AND suspended=0", (ts,))
    for u in c.execute("SELECT user_id, ref_by, ref_done, joined_at FROM users WHERE ref_by IS NOT NULL AND ref_by<>user_id").fetchall():
        st = "qualified" if int(u["ref_done"] or 0) == 1 else "pending"
        c.execute("INSERT OR IGNORE INTO referrals(referee_id,referrer_id,status,reason,created_at,qualified_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                  (u["user_id"], u["ref_by"], st, "legacy import", u["joined_at"] or ts, u["joined_at"] if st == "qualified" else None, ts))
    # ledger opening balances. Legacy economics: refs counter minus (referral claims * per-at-migration).
    try:
        per = max(1, int(c.execute("SELECT val FROM settings WHERE key='refs_per_reward'").fetchone()[0]))
    except (TypeError, ValueError):
        per = 1
    exists = c.execute("SELECT 1 FROM ledger WHERE kind='legacy_opening' LIMIT 1").fetchone()
    if not exists:
        for u in c.execute("SELECT user_id, refs, claims FROM users").fetchall():
            earned = int(u["refs"] or 0)
            ref_claims = int(c.execute("SELECT COUNT(*) FROM claims WHERE user_id=? AND kind='referral'", (u["user_id"],)).fetchone()[0])
            consumed = ref_claims * per
            if earned:
                c.execute("INSERT INTO ledger(user_id,delta,kind,ref_type,note,rule_snapshot,idem_key,created_at) VALUES(?,?,?,?,?,?,?,?)",
                          (u["user_id"], earned, "legacy_opening", "users.refs", "legacy referral counter", json.dumps({"per": per}),
                           f"legacy_open:{u['user_id']}", ts))
            if consumed:
                c.execute("INSERT INTO ledger(user_id,delta,kind,ref_type,note,rule_snapshot,idem_key,created_at) VALUES(?,?,?,?,?,?,?,?)",
                          (u["user_id"], -consumed, "legacy_consumed", "claims", f"{ref_claims} legacy referral claims x {per}",
                           json.dumps({"per": per}), f"legacy_cons:{u['user_id']}", ts))
            if consumed > earned:
                report["notes"].append(f"user {u['user_id']}: legacy claims exceed referral counter (balance {earned - consumed}); owner review suggested")
        report["opening_rule"] = f"balance = refs - referral_claims*{per} (per-reward setting at migration time)"
    # waitlist
    c.execute("INSERT OR IGNORE INTO waitlist_v2(user_id,category,created_at,active) SELECT user_id,'default',ts,1 FROM waitlist")
    c.execute("UPDATE channels SET policy=COALESCE(policy,'member'), enabled=COALESCE(enabled,1), auto_approve=COALESCE(auto_approve,1)")
    c.execute("INSERT OR REPLACE INTO settings(key,val) VALUES('legacy_reconciliation',?)", (json.dumps(report),))


MIGRATIONS: List[Tuple[int, Callable[[sqlite3.Connection], None]]] = [(1, migrate_v1), (2, migrate_v2)]


def current_schema_version(c: sqlite3.Connection) -> int:
    if not _table_exists(c, "schema_meta"):
        return 1 if _table_exists(c, "users") else 0
    r = c.execute("SELECT val FROM schema_meta WHERE key='schema_version'").fetchone()
    return int(r[0]) if r else 1


def run_migrations(db: Optional[Database] = None, backup_first: bool = True) -> Dict[str, Any]:
    db = db or DB
    db.init_pragmas()
    with db.ro() as c:
        v = current_schema_version(c)
    result: Dict[str, Any] = {"from": v, "to": SCHEMA_VERSION, "backup": None}
    if v >= SCHEMA_VERSION:
        with db.ro() as c:
            bad = c.execute("PRAGMA quick_check").fetchone()[0]
        result["integrity"] = bad
        return result
    if v > 0 and backup_first:
        result["backup"] = create_backup(db, note=f"pre-migration v{v}->v{SCHEMA_VERSION}")["path"]
    for ver, fn in MIGRATIONS:
        if ver <= v:
            continue
        with db.tx() as c:
            fn(c)
            c.execute("CREATE TABLE IF NOT EXISTS schema_meta(key TEXT PRIMARY KEY, val TEXT)")
            c.execute("INSERT OR REPLACE INTO schema_meta(key,val) VALUES('schema_version',?)", (str(ver),))
        log.info("migration v%s applied", ver)
    with db.ro() as c:
        result["integrity"] = c.execute("PRAGMA quick_check").fetchone()[0]
        result["fk_violations"] = len(c.execute("PRAGMA foreign_key_check").fetchall())
    return result

# ==== 6. SETTINGS AND CONTENT =================================================================

def gs(key: str, default: str = "") -> str:
    r = DB.one("SELECT val FROM settings WHERE key=?", (key,))
    if r is None or r["val"] is None:
        return DEFAULT_SETTINGS.get(key, default)
    return r["val"]


def gi(key: str, default: int = 0) -> int:
    try:
        return int(gs(key, str(default)))
    except (TypeError, ValueError):
        return default


def set_setting(key: str, val: Any, actor: Optional[int] = None) -> None:
    before = gs(key)
    DB.run("INSERT INTO settings(key,val) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET val=excluded.val", (key, str(val)))
    if actor is not None:
        audit(actor, "setting.update", key, before=before if key not in ("log_channel",) else "…", after=str(val))


def content(key: str, lang: str = "en") -> str:
    """Published content for key/lang, falling back to en, then settings, then defaults."""
    for lg in ([lang, "en"] if lang != "en" else ["en"]):
        r = DB.one("SELECT body FROM content_versions WHERE key=? AND lang=? AND status='published' ORDER BY version DESC LIMIT 1", (key, lg))
        if r:
            return r["body"]
    return gs(key)


def publish_content(key: str, body: str, actor: int, lang: str = "en") -> Optional[str]:
    err = validate_template(body, 1024 if key == "welcome_text" and gs("start_photo") else 4096)
    if err:
        return err
    with DB.tx() as c:
        v = int(c.execute("SELECT COALESCE(MAX(version),0) FROM content_versions WHERE key=? AND lang=?", (key, lang)).fetchone()[0]) + 1
        c.execute("UPDATE content_versions SET status='archived' WHERE key=? AND lang=? AND status='published'", (key, lang))
        c.execute("INSERT INTO content_versions(key,lang,body,version,status,created_by,created_at) VALUES(?,?,?,?,'published',?,?)",
                  (key, lang, body, v, actor, now()))
        if lang == "en":
            c.execute("INSERT INTO settings(key,val) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET val=excluded.val", (key, body))
    audit(actor, "content.publish", f"{key}/{lang}", after=f"v{v}")
    return None


def restore_default_content(key: str, actor: int, lang: str = "en") -> None:
    publish_content(key, DEFAULT_SETTINGS.get(key, ""), actor, lang)


def template_values(u: Optional[sqlite3.Row], tg_user: Any = None) -> Dict[str, Any]:
    per = max(1, gi("refs_per_reward", 1))
    name = (getattr(tg_user, "first_name", None) if tg_user else None) or (u["first_name"] if u else None) or "User"
    bal = credit_balance(int(u["user_id"])) if u else 0
    return {"name": name, "per": per, "refs": qualified_count(int(u["user_id"])) if u else 0,
            "claims": int(u["claims"] or 0) if u else 0, "stock": stock_label(), "dev": CFG.dev_name,
            "bot": BOT_USERNAME, "need": max(0, per - bal), "balance": bal}


def render(key: str, u: Optional[sqlite3.Row], tg_user: Any = None) -> str:
    lang = (u["lang"] if u and u["lang"] else "en")
    return render_template(content(key, lang), template_values(u, tg_user))


# ==== 7. ROLES, PERMISSIONS, AUDIT, RATE LIMITING ============================================

class PermissionDenied(Exception):
    pass


def role_of(uid: int) -> Optional[str]:
    uid = int(uid)
    if uid and uid == CFG.owner_id:
        return ROLE_OWNER
    if uid in CFG.admin_ids:
        return ROLE_ADMIN
    r = DB.one("SELECT role FROM admins WHERE user_id=?", (uid,))
    if not r:
        return None
    return r["role"] if r["role"] in ROLE_PERMS else ROLE_ADMIN


def is_owner(uid: int) -> bool:
    return bool(CFG.owner_id) and int(uid) == CFG.owner_id


def is_staff(uid: int) -> bool:
    return role_of(uid) is not None


def has_perm(uid: int, perm: str) -> bool:
    role = role_of(uid)
    return bool(role) and perm in ROLE_PERMS.get(role, set())


def require_perm(uid: int, perm: str) -> None:
    if not has_perm(uid, perm):
        raise PermissionDenied(perm)


def staff_ids() -> List[int]:
    ids = {CFG.owner_id} | set(CFG.admin_ids) | {int(r["user_id"]) for r in DB.all("SELECT user_id FROM admins")}
    ids.discard(0)
    return sorted(ids)


def alert_recipients() -> List[int]:
    return [i for i in staff_ids() if role_of(i) in (ROLE_OWNER, ROLE_ADMIN)]


def audit(actor: Optional[int], action: str, target: Any = None, before: Any = None, after: Any = None,
          reason: Optional[str] = None, result: str = "ok") -> None:
    def js(v: Any) -> Optional[str]:
        if v is None:
            return None
        return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)
    try:
        DB.run("INSERT INTO audit_events(actor_id,action,target,before,after,reason,result,created_at) VALUES(?,?,?,?,?,?,?,?)",
               (actor, action, None if target is None else str(target), js(before), js(after), reason, result, now()))
    except sqlite3.Error as e:
        log.warning("audit write failed: %s", e)


def activity(uid: int, kind: str, meta: Any = None) -> None:
    with contextlib.suppress(sqlite3.Error):
        DB.run("INSERT INTO activity(user_id,kind,meta,created_at) VALUES(?,?,?,?)",
               (uid, kind, json.dumps(meta, ensure_ascii=False, default=str) if meta is not None else None, now()))


class RateLimiter:
    """Small in-memory sliding window limiter keyed per account (never per shared IP alone)."""

    def __init__(self) -> None:
        self._hits: Dict[str, List[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, limit: int, window: float) -> bool:
        t = time.monotonic()
        with self._lock:
            arr = [x for x in self._hits.get(key, []) if t - x < window]
            if len(arr) >= limit:
                self._hits[key] = arr
                return False
            arr.append(t)
            self._hits[key] = arr
            if len(self._hits) > 20000:
                self._hits = {k: v for k, v in self._hits.items() if v and t - v[-1] < window}
            return True


RL = RateLimiter()


def create_confirmation(actor: int, action: str, target: Any, payload: Any = None, ttl: int = 300) -> str:
    cid = new_id("cf")
    DB.run("INSERT INTO confirmations(id,actor_id,action,target,payload,created_at,expires_at) VALUES(?,?,?,?,?,?,?)",
           (cid, actor, action, str(target) if target is not None else None,
            json.dumps(payload, ensure_ascii=False) if payload is not None else None, now(), now() + ttl))
    return cid


def consume_confirmation(cid: str, actor: int, action: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Single-use, actor-bound, time-bound. Returns {action,target,payload} or None."""
    with DB.tx() as c:
        row = c.execute("SELECT * FROM confirmations WHERE id=? AND actor_id=? AND used_at IS NULL AND expires_at>?",
                        (cid, actor, now())).fetchone()
        if not row or (action and row["action"] != action):
            return None
        c.execute("UPDATE confirmations SET used_at=? WHERE id=?", (now(), cid))
    return {"action": row["action"], "target": row["target"], "payload": json.loads(row["payload"]) if row["payload"] else None}


# ---- users repository ----

def get_user(uid: int) -> Optional[sqlite3.Row]:
    return DB.one("SELECT * FROM users WHERE user_id=?", (int(uid),))


def find_user(q: str) -> Optional[sqlite3.Row]:
    q = (q or "").strip()
    if q.lstrip("-").isdigit():
        return get_user(int(q))
    return DB.one("SELECT * FROM users WHERE lower(username)=?", (q.lstrip("@").lower(),))


def ensure_user(tg_user: Any, ref_by: Optional[int] = None, campaign: Optional[str] = None) -> Tuple[sqlite3.Row, bool]:
    """Insert or refresh the user row. Returns (row, created)."""
    uid = int(tg_user.id)
    ts = now()
    with DB.tx() as c:
        row = c.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
        if row:
            c.execute("UPDATE users SET username=?, first_name=?, last_seen=? WHERE user_id=?",
                      (tg_user.username, tg_user.first_name, ts, uid))
            created = False
        else:
            c.execute("INSERT INTO users(user_id,username,first_name,ref_by,ref_done,refs,claims,verified,blocked,joined_at,last_seen,lang,campaign) "
                      "VALUES(?,?,?,?,0,0,0,0,0,?,?,'en',?)",
                      (uid, tg_user.username, tg_user.first_name, None, ts, ts, campaign))
            created = True
    if ref_by:
        attach_referral(uid, int(ref_by), campaign)
    if created:
        activity(uid, "joined", {"ref_by": ref_by, "campaign": campaign})
    return get_user(uid), created  # type: ignore[return-value]


def user_lang(u: Optional[sqlite3.Row]) -> str:
    return (u["lang"] if u and u["lang"] in I18N else "en")


def is_suspended(u: Optional[sqlite3.Row]) -> bool:
    return bool(u) and int(u["suspended"] or 0) == 1


def maintenance_on() -> bool:
    return gi("maintenance", 0) == 1


def suspend_user(uid: int, actor: int, reason: str, on: bool = True) -> None:
    before = get_user(uid)
    DB.run("UPDATE users SET suspended=?, suspended_reason=?, suspended_at=?, blocked=? WHERE user_id=?",
           (1 if on else 0, reason if on else None, now() if on else None, 1 if on else 0, uid))
    audit(actor, "user.suspend" if on else "user.reinstate", uid, before=dict(before) if before else None, reason=reason)
    invalidate_join_cache(uid)

# ==== 8. MINI APP AUTHENTICATION, SESSIONS, INSTALLATIONS, CHALLENGES =========================

class AuthError(Exception):
    def __init__(self, code: str, status: int = 401):
        super().__init__(code)
        self.code, self.status = code, status


def validate_init_data(init_data: str, bot_token: str, max_age: int, now_ts: Optional[int] = None) -> Dict[str, Any]:
    """Official Telegram Mini App initData verification (HMAC-SHA256 with 'WebAppData' key)."""
    now_ts = now_ts or now()
    if not init_data or not isinstance(init_data, str) or len(init_data) > 8192:
        raise AuthError("init_data_malformed")
    try:
        pairs = urllib.parse.parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        raise AuthError("init_data_malformed")
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        raise AuthError("init_data_duplicate_param")
    data = dict(pairs)
    received = data.pop("hash", "")
    if not re.fullmatch(r"[0-9a-f]{64}", received or ""):
        raise AuthError("init_data_malformed")
    if "auth_date" not in data or "user" not in data:
        raise AuthError("init_data_missing_fields")
    check_string = "\n".join(f"{k}={data[k]}" for k in sorted(data))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, received):
        raise AuthError("init_data_bad_signature")
    try:
        auth_date = int(data["auth_date"])
    except ValueError:
        raise AuthError("init_data_malformed")
    if auth_date - now_ts > 60:
        raise AuthError("init_data_future")
    if now_ts - auth_date > max_age:
        raise AuthError("init_data_expired")
    try:
        user = json.loads(data["user"])
        uid = int(user["id"])
    except (ValueError, KeyError, TypeError):
        raise AuthError("init_data_malformed")
    return {"user": user, "user_id": uid, "auth_date": auth_date, "start_param": data.get("start_param", "")}


def init_session_secret() -> None:
    if CFG.session_secret:
        RT.session_secret = CFG.session_secret.encode()
        return
    r = DB.one("SELECT val FROM settings WHERE key='session_secret_auto'")
    if r and r["val"]:
        RT.session_secret = r["val"].encode()
    else:
        s = secrets.token_urlsafe(48)
        DB.run("INSERT OR REPLACE INTO settings(key,val) VALUES('session_secret_auto',?)", (s,))
        RT.session_secret = s.encode()
        log.warning("SESSION_SECRET not set; generated one and stored it in the database (set SESSION_SECRET in production).")


def token_digest(token: str) -> str:
    return hmac.new(RT.session_secret or b"unset", token.encode(), hashlib.sha256).hexdigest()


def effective_policy() -> str:
    p = (gs("session_policy") or CFG.session_policy or "notify").lower()
    return p if p in ("off", "notify", "approve", "risk") else "notify"


def installation_approved(inst: Optional[sqlite3.Row]) -> bool:
    return bool(inst) and inst["status"] == "approved" and (inst["approved_until"] is None or int(inst["approved_until"]) > now())


def register_installation(uid: int, install_key: str, platform: str) -> Tuple[sqlite3.Row, bool]:
    """Bind a client-generated random identifier to the authenticated account. Server stores only a digest.
    Onboarding policy: the first installation of an account is trusted (auto-approved); later ones follow the policy."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", install_key or ""):
        raise AuthError("bad_installation_key", 400)
    key_hash = sha256_hex(f"{uid}:{install_key}")
    platform = re.sub(r"[^a-z0-9_ .-]", "", (platform or "unknown").lower())[:32] or "unknown"
    ts = now()
    with DB.tx() as c:
        row = c.execute("SELECT * FROM installations WHERE key_hash=?", (key_hash,)).fetchone()
        if row:
            c.execute("UPDATE installations SET last_seen=?, platform=? WHERE id=?", (ts, platform, row["id"]))
            return c.execute("SELECT * FROM installations WHERE id=?", (row["id"],)).fetchone(), False
        others = int(c.execute("SELECT COUNT(*) FROM installations WHERE user_id=? AND status<>'revoked'", (uid,)).fetchone()[0])
        policy = effective_policy()
        status = "approved" if (others == 0 or policy in ("off", "notify")) else "pending"
        iid = new_id("in")
        c.execute("INSERT INTO installations(id,user_id,key_hash,platform,status,first_seen,last_seen,approved_until,label) VALUES(?,?,?,?,?,?,?,?,?)",
                  (iid, uid, key_hash, platform, status, ts, ts, (ts + CFG.approval_ttl) if status == "approved" else None, platform))
        row = c.execute("SELECT * FROM installations WHERE id=?", (iid,)).fetchone()
    activity(uid, "installation.new", {"platform": platform, "status": row["status"]})
    return row, True


def risk_requires_approval(uid: int, inst: sqlite3.Row) -> bool:
    """Documented signals only: >=3 new installations in 24h, or an approved installation on a different platform label."""
    recent = int(DB.scalar("SELECT COUNT(*) FROM installations WHERE user_id=? AND first_seen>?", (uid, now() - 86400)))
    other_platform = DB.one("SELECT 1 FROM installations WHERE user_id=? AND status='approved' AND platform<>? AND id<>?",
                            (uid, inst["platform"], inst["id"]))
    return recent >= 3 or other_platform is not None


def approval_needed(uid: int, inst: Optional[sqlite3.Row]) -> bool:
    policy = effective_policy()
    if policy in ("off", "notify") or inst is None:
        return False
    if installation_approved(inst):
        return False
    if policy == "risk" and not risk_requires_approval(uid, inst):
        DB.run("UPDATE installations SET status='approved', approved_until=? WHERE id=? AND status='pending'", (now() + CFG.approval_ttl, inst["id"]))
        return False
    return True


def native_claim_needs_approval(uid: int) -> bool:
    """Native claims obey the same policy: under approve/risk a short-lived account confirmation is required."""
    policy = effective_policy()
    if policy == "approve":
        return True
    if policy == "risk":
        return DB.one("SELECT 1 FROM installations WHERE user_id=? AND status='pending' AND first_seen>?", (uid, now() - 86400)) is not None
    return False


def create_session(uid: int, installation_id: Optional[str], platform: str) -> Tuple[str, sqlite3.Row]:
    token = secrets.token_urlsafe(32)
    sid = new_id("se")
    ts = now()
    with DB.tx() as c:
        c.execute("INSERT INTO app_sessions(id,user_id,token_hash,installation_id,platform,created_at,last_used_at,expires_at) VALUES(?,?,?,?,?,?,?,?)",
                  (sid, uid, token_digest(token), installation_id, platform, ts, ts, ts + CFG.session_ttl))
        extra = c.execute("SELECT id FROM app_sessions WHERE user_id=? AND revoked_at IS NULL AND expires_at>? ORDER BY created_at DESC LIMIT -1 OFFSET ?",
                          (uid, ts, max(1, CFG.max_sessions))).fetchall()
        for e in extra:
            c.execute("UPDATE app_sessions SET revoked_at=? WHERE id=?", (ts, e["id"]))
        row = c.execute("SELECT * FROM app_sessions WHERE id=?", (sid,)).fetchone()
    return token, row


def session_from_token(token: Optional[str]) -> Optional[sqlite3.Row]:
    if not token or len(token) > 200:
        return None
    row = DB.one("SELECT * FROM app_sessions WHERE token_hash=?", (token_digest(token),))
    if not row or row["revoked_at"] or int(row["expires_at"]) <= now():
        return None
    if now() - int(row["last_used_at"] or 0) > 60:
        DB.run("UPDATE app_sessions SET last_used_at=? WHERE id=?", (now(), row["id"]))
    return row


def revoke_session(uid: int, sid: str, actor: Optional[int] = None) -> bool:
    n = DB.run("UPDATE app_sessions SET revoked_at=? WHERE id=? AND user_id=? AND revoked_at IS NULL", (now(), sid, uid))
    if n:
        audit(actor or uid, "session.revoke", sid)
    return n > 0


def revoke_other_sessions(uid: int, keep_sid: Optional[str], actor: Optional[int] = None) -> int:
    n = DB.run("UPDATE app_sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL AND id<>COALESCE(?, '')", (now(), uid, keep_sid))
    audit(actor or uid, "session.revoke_others", uid, after={"revoked": n})
    return n


def revoke_installation(uid: int, iid: str, actor: Optional[int] = None) -> bool:
    with DB.tx() as c:
        n = c.execute("UPDATE installations SET status='revoked' WHERE id=? AND user_id=? AND status<>'revoked'", (iid, uid)).rowcount
        c.execute("UPDATE app_sessions SET revoked_at=? WHERE installation_id=? AND user_id=? AND revoked_at IS NULL", (now(), iid, uid))
    if n:
        audit(actor or uid, "installation.revoke", iid)
    return n > 0


def create_challenge(uid: int, action: str, installation_id: Optional[str] = None, meta: Any = None) -> sqlite3.Row:
    ts = now()
    with DB.tx() as c:
        c.execute("UPDATE challenges SET status='expired' WHERE user_id=? AND status='pending' AND expires_at<=?", (uid, ts))
        row = c.execute("SELECT * FROM challenges WHERE user_id=? AND action=? AND COALESCE(installation_id,'')=COALESCE(?, '') AND status='pending'",
                        (uid, action, installation_id)).fetchone()
        if row:
            return row
        cid = new_id("ch")
        c.execute("INSERT INTO challenges(id,user_id,installation_id,action,status,created_at,expires_at,meta) VALUES(?,?,?,?,'pending',?,?,?)",
                  (cid, uid, installation_id, action, ts, ts + CFG.challenge_ttl, json.dumps(meta) if meta is not None else None))
        return c.execute("SELECT * FROM challenges WHERE id=?", (cid,)).fetchone()


def consume_challenge(cid: str, uid: int, decision: str) -> Optional[sqlite3.Row]:
    """Atomically consume a pending challenge owned by `uid`. decision: approved|denied."""
    ts = now()
    with DB.tx() as c:
        n = c.execute("UPDATE challenges SET status=?, consumed_at=? WHERE id=? AND user_id=? AND status='pending' AND expires_at>?",
                      (decision, ts, cid, uid, ts)).rowcount
        if n != 1:
            return None
        row = c.execute("SELECT * FROM challenges WHERE id=?", (cid,)).fetchone()
        if row["installation_id"]:
            if decision == "approved":
                c.execute("UPDATE installations SET status='approved', approved_until=? WHERE id=? AND user_id=?",
                          (ts + CFG.approval_ttl, row["installation_id"], uid))
            else:
                c.execute("UPDATE installations SET status='denied' WHERE id=? AND user_id=?", (row["installation_id"], uid))
                c.execute("UPDATE app_sessions SET revoked_at=? WHERE installation_id=? AND revoked_at IS NULL", (ts, row["installation_id"]))
    audit(uid, f"challenge.{decision}", cid, after={"action": row["action"]})
    return row


def challenge_message(ch: sqlite3.Row, lang: str = "en") -> Tuple[str, InlineKeyboardMarkup]:
    inst = DB.one("SELECT platform, first_seen FROM installations WHERE id=?", (ch["installation_id"],)) if ch["installation_id"] else None
    what = {"session": "A new Mini App session wants access to your account.",
            "native_claim": "Confirm that you want to claim a reward from this chat.",
            "recovery": "Confirm account recovery for a new device/session."}.get(ch["action"], "Confirm this action.")
    txt = (f"🔐 <b>Approval needed</b>\n\n{esc(what)}\n"
           + (f"Client: <b>{esc(inst['platform'])}</b>\n" if inst else "")
           + f"Expires: <b>{fmt_ts(ch['expires_at'])}</b>\n\n"
           "<i>Approving here confirms your intent from the same Telegram account. It is not an independent second factor.</i>")
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Approve", callback_data=f"sa:{ch['id']}:ok"),
                                InlineKeyboardButton("❌ Deny", callback_data=f"sa:{ch['id']}:no")]])
    return txt, kb


async def send_challenge(bot: Any, ch: sqlite3.Row) -> bool:
    txt, kb = challenge_message(ch)
    try:
        m = await bot.send_message(int(ch["user_id"]), txt, parse_mode=ParseMode.HTML, reply_markup=kb)
        DB.run("UPDATE challenges SET message_id=? WHERE id=?", (m.message_id, ch["id"]))
        return True
    except TelegramError as e:
        log.warning("challenge send failed: %s", e)
        return False


def sessions_overview(uid: int, current_sid: Optional[str] = None) -> Dict[str, Any]:
    ts = now()
    sess = [dict(r) for r in DB.all("SELECT id,installation_id,platform,created_at,last_used_at,expires_at FROM app_sessions "
                                    "WHERE user_id=? AND revoked_at IS NULL AND expires_at>? ORDER BY last_used_at DESC", (uid, ts))]
    for s in sess:
        s["current"] = s["id"] == current_sid
    inst = [dict(r) for r in DB.all("SELECT id,platform,status,first_seen,last_seen,approved_until,label FROM installations "
                                    "WHERE user_id=? AND status<>'revoked' ORDER BY last_seen DESC", (uid,))]
    for i in inst:
        i["approved"] = i["status"] == "approved" and (i["approved_until"] is None or int(i["approved_until"]) > ts)
    u = get_user(uid)
    return {"policy": effective_policy(), "sessions": sess, "installations": inst,
            "notify_new_session": int(u["notify_new_session"] or 1) if u else 1,
            "explain": ["1. Telegram account authenticated (initData signature).",
                        "2. Required channel membership verified (checked live with Telegram).",
                        "3. Browser/app installation recognized (random identifier stored as a digest; it can be lost when storage is cleared).",
                        "4. Session approved for sensitive actions (bounded validity, revocable)."]}


def retention_cleanup() -> Dict[str, int]:
    cutoff = now() - CFG.retention_days * 86400
    out = {}
    with DB.tx() as c:
        out["sessions"] = c.execute("DELETE FROM app_sessions WHERE (revoked_at IS NOT NULL AND revoked_at<?) OR expires_at<?", (cutoff, cutoff)).rowcount
        out["challenges"] = c.execute("DELETE FROM challenges WHERE created_at<?", (cutoff,)).rowcount
        out["confirmations"] = c.execute("DELETE FROM confirmations WHERE expires_at<?", (now() - 86400,)).rowcount
        out["jobs"] = c.execute("DELETE FROM jobs WHERE status IN ('done','dead') AND created_at<?", (cutoff,)).rowcount
        out["installations"] = c.execute("DELETE FROM installations WHERE status='revoked' AND last_seen<?", (cutoff,)).rowcount
        out["activity"] = c.execute("DELETE FROM activity WHERE created_at<?", (cutoff,)).rowcount
    return out


# ==== 9. REFERRAL ENGINE ======================================================================

def attach_referral(referee_id: int, referrer_id: int, campaign: Optional[str] = None) -> bool:
    """Attribution is final once a referrals row exists. A user row created earlier without attribution
    (e.g. from a join request) may still be attributed as long as it has not completed verification."""
    if referee_id == referrer_id or referrer_id <= 0:
        return False
    with DB.tx() as c:
        if not c.execute("SELECT 1 FROM users WHERE user_id=?", (referrer_id,)).fetchone():
            return False
        me = c.execute("SELECT verified FROM users WHERE user_id=?", (referee_id,)).fetchone()
        if not me or int(me["verified"] or 0) == 1:
            return False
        if c.execute("SELECT 1 FROM referrals WHERE referee_id=?", (referee_id,)).fetchone():
            return False
        delay = max(0, gi("qualification_delay_seconds", 0))
        c.execute("INSERT INTO referrals(referee_id,referrer_id,status,campaign,created_at,updated_at,eligible_at) VALUES(?,?,'pending',?,?,?,?)",
                  (referee_id, referrer_id, campaign, now(), now(), now() + delay))
        c.execute("UPDATE users SET ref_by=? WHERE user_id=? AND ref_by IS NULL", (referrer_id, referee_id))
    return True


def qualify_referral(referee_id: int) -> Optional[Dict[str, Any]]:
    """Mark a pending referral qualified and credit the referrer atomically (idempotent)."""
    ts = now()
    with DB.tx() as c:
        r = c.execute("SELECT * FROM referrals WHERE referee_id=? AND status='pending'", (referee_id,)).fetchone()
        if not r:
            return None
        if r["eligible_at"] and int(r["eligible_at"]) > ts:
            return None
        ref = c.execute("SELECT suspended FROM users WHERE user_id=?", (r["referrer_id"],)).fetchone()
        if not ref:
            c.execute("UPDATE referrals SET status='rejected', reason='referrer missing', updated_at=? WHERE id=?", (ts, r["id"]))
            return None
        n = c.execute("UPDATE referrals SET status='qualified', qualified_at=?, updated_at=?, reason='verified channels' WHERE id=? AND status='pending'",
                      (ts, ts, r["id"])).rowcount
        if n != 1:
            return None
        c.execute("INSERT OR IGNORE INTO ledger(user_id,delta,kind,ref_type,ref_id,note,rule_snapshot,idem_key,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                  (r["referrer_id"], 1, "referral", "referral", str(r["id"]), f"referee {referee_id}",
                   json.dumps({"per": max(1, gi("refs_per_reward", 1))}), f"ref:{r['id']}", ts))
        c.execute("UPDATE users SET refs=refs+1 WHERE user_id=?", (r["referrer_id"],))
        c.execute("UPDATE users SET ref_done=1 WHERE user_id=?", (referee_id,))
    activity(int(r["referrer_id"]), "referral.qualified", {"referee": referee_id})
    return {"referrer_id": int(r["referrer_id"]), "referee_id": referee_id, "id": int(r["id"])}


def reverse_referral(ref_id: int, actor: int, reason: str) -> bool:
    ts = now()
    with DB.tx() as c:
        r = c.execute("SELECT * FROM referrals WHERE id=? AND status='qualified'", (ref_id,)).fetchone()
        if not r:
            return False
        c.execute("UPDATE referrals SET status='reversed', reason=?, updated_at=? WHERE id=?", (reason, ts, ref_id))
        c.execute("INSERT OR IGNORE INTO ledger(user_id,delta,kind,ref_type,ref_id,note,idem_key,actor_id,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                  (r["referrer_id"], -1, "referral_reversal", "referral", str(ref_id), reason, f"refrev:{ref_id}", actor, ts))
        c.execute("UPDATE users SET refs=MAX(0, refs-1) WHERE user_id=?", (r["referrer_id"],))
    audit(actor, "referral.reverse", ref_id, reason=reason)
    return True


def qualified_count(uid: int) -> int:
    return int(DB.scalar("SELECT COUNT(*) FROM referrals WHERE referrer_id=? AND status='qualified'", (uid,)))


def referral_stats(uid: int) -> Dict[str, int]:
    out = {"invited": 0, "pending": 0, "qualified": 0, "rejected": 0, "reversed": 0}
    for r in DB.all("SELECT status, COUNT(*) n FROM referrals WHERE referrer_id=? GROUP BY status", (uid,)):
        out[r["status"]] = int(r["n"])
    out["invited"] = sum(out.values())
    return out


def referral_page(uid: int, page: int = 0, size: int = 10) -> Tuple[List[Dict[str, Any]], int]:
    total = int(DB.scalar("SELECT COUNT(*) FROM referrals WHERE referrer_id=?", (uid,)))
    rows = DB.all("SELECT r.*, u.first_name FROM referrals r LEFT JOIN users u ON u.user_id=r.referee_id WHERE r.referrer_id=? "
                  "ORDER BY r.id DESC LIMIT ? OFFSET ?", (uid, size, page * size))
    return [{"id": r["id"], "name": (r["first_name"] or "User")[:24], "status": r["status"], "reason": r["reason"],
             "created_at": r["created_at"], "qualified_at": r["qualified_at"]} for r in rows], total

# ==== 10. CHANNEL VERIFICATION (FORCE-JOIN) ===================================================

OK_STATUS = {ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER}
_join_cache: Dict[int, Tuple[int, "VerifyResult"]] = {}
JOIN_CACHE_TTL = 45


@dataclass
class VerifyResult:
    missing: List[sqlite3.Row] = field(default_factory=list)
    unavailable: bool = False
    states: Dict[int, str] = field(default_factory=dict)   # chat_id -> member|requested|missing|unknown|optional

    @property
    def ok(self) -> bool:
        return not self.missing and not self.unavailable


def invalidate_join_cache(uid: Optional[int] = None) -> None:
    if uid is None:
        _join_cache.clear()
    else:
        _join_cache.pop(int(uid), None)


def required_channels() -> List[sqlite3.Row]:
    return DB.all("SELECT * FROM channels WHERE enabled=1 ORDER BY sort_order, added_at")


def has_request(uid: int, chat_id: int) -> bool:
    r = DB.one("SELECT status FROM requests WHERE user_id=? AND chat_id=?", (uid, chat_id))
    return bool(r and r["status"] in ("pending", "approved"))


async def check_membership(bot: Any, uid: int, force: bool = False) -> VerifyResult:
    """Never fails open: Telegram errors yield `unavailable`, not eligibility."""
    if gi("force_join", 1) != 1 or is_staff(uid):
        return VerifyResult()
    chans = required_channels()
    if not chans:
        return VerifyResult()
    hit = _join_cache.get(uid)
    if hit and not force and now() - hit[0] < JOIN_CACHE_TTL:
        return hit[1]
    res = VerifyResult()
    for ch in chans:
        cid = int(ch["chat_id"])
        if ch["policy"] == "optional":
            res.states[cid] = "optional"
            continue
        state = "missing"
        try:
            m = await bot.get_chat_member(cid, uid)
            if m.status in OK_STATUS or (m.status == ChatMemberStatus.RESTRICTED and getattr(m, "is_member", False)):
                state = "member"
        except (Forbidden,) as e:
            state = "unknown"
            res.unavailable = True
            DB.run("UPDATE channels SET last_error=?, last_checked=? WHERE chat_id=?", (str(e)[:200], now(), cid))
            notify_admins("channel_perm", f"⚠️ Bot lost access to channel <b>{esc(ch['title'])}</b> (<code>{cid}</code>): {esc(e)}", 3600)
        except BadRequest as e:
            msg = str(e).lower()
            if "user not found" in msg or "participant_id_invalid" in msg or "member not found" in msg:
                state = "missing"
            else:
                state = "unknown"
                res.unavailable = True
                DB.run("UPDATE channels SET last_error=?, last_checked=? WHERE chat_id=?", (str(e)[:200], now(), cid))
        except (TimedOut, NetworkError, RetryAfter, TelegramError):
            state = "unknown"
            res.unavailable = True
        if state == "missing" and ch["policy"] == "request" and has_request(uid, cid):
            state = "requested"
        res.states[cid] = state
        if state in ("missing", "unknown"):
            res.missing.append(ch)
    if not res.unavailable:
        _join_cache[uid] = (now(), res)
    return res


async def ensure_invite_link(bot: Any, ch: sqlite3.Row) -> Optional[str]:
    if ch["invite_link"]:
        return ch["invite_link"]
    if ch["username"]:
        link = f"https://t.me/{ch['username']}"
    else:
        link = None
        try:
            lk = await bot.create_chat_invite_link(int(ch["chat_id"]), creates_join_request=True, name="Bot Gate")
            link = lk.invite_link
        except TelegramError:
            with contextlib.suppress(TelegramError):
                link = await bot.export_chat_invite_link(int(ch["chat_id"]))
    if link:
        DB.run("UPDATE channels SET invite_link=?, last_error=NULL WHERE chat_id=?", (link, ch["chat_id"]))
    return link


async def add_channel(bot: Any, ident: Any, actor: int) -> Tuple[str, Optional[str]]:
    chat = await bot.get_chat(ident)          # canonical chat id is what we persist
    cid = int(chat.id)
    me = await bot.get_chat_member(cid, bot.id)
    if me.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
        raise RuntimeError("The bot is not an administrator in that channel.")
    username = getattr(chat, "username", None)
    link = None
    if username:
        link = f"https://t.me/{username}"
    else:
        with contextlib.suppress(TelegramError):
            lk = await bot.create_chat_invite_link(cid, creates_join_request=True, name="Bot Gate")
            link = lk.invite_link
    order = int(DB.scalar("SELECT COALESCE(MAX(sort_order),0)+1 FROM channels"))
    DB.run("INSERT INTO channels(chat_id,title,invite_link,is_private,added_at,username,policy,enabled,sort_order,auto_approve) "
           "VALUES(?,?,?,?,?,?,'member',1,?,1) ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title, invite_link=excluded.invite_link, "
           "is_private=excluded.is_private, username=excluded.username, last_error=NULL",
           (cid, chat.title or "Channel", link, 0 if username else 1, now(), username, order))
    invalidate_join_cache()
    audit(actor, "channel.add", cid, after={"title": chat.title, "link": bool(link)})
    return chat.title or "Channel", link

# ==== 11. REWARD LEDGER AND CLAIMS ============================================================

class ClaimError(Exception):
    def __init__(self, code: str, **info: Any):
        super().__init__(code)
        self.code, self.info = code, info


def credit_balance(uid: int) -> int:
    return int(DB.scalar("SELECT COALESCE(SUM(delta),0) FROM ledger WHERE user_id=?", (uid,)))


def stock_count(category: str = "default") -> int:
    return int(DB.scalar("SELECT COUNT(*) FROM rewards WHERE status='available' AND category=? AND (expires_at IS NULL OR expires_at>?)",
                         (category, now())))


def stock_label() -> str:
    if gs("reward_mode") == "shared":
        n = stock_count()
        return f"shared pool of {n}" if n else "0"
    return str(stock_count())


def eligibility(u: sqlite3.Row) -> Dict[str, Any]:
    per = max(1, gi("refs_per_reward", 1))
    bal = credit_balance(int(u["user_id"]))
    bonus = gi("bonus_enabled", 1) == 1 and not u["bonus_consumed_at"]
    return {"per": per, "balance": bal, "bonus_available": bonus, "referral_available": bal >= per,
            "need": max(0, per - bal), "stock": stock_count(), "mode": gs("reward_mode")}


def perform_claim(uid: int, kind: str, idem_key: str, actor: Optional[int] = None, category: str = "default") -> Dict[str, Any]:
    """One transaction: recheck eligibility, allocate stock, consume entitlement, create claim, audit, enqueue delivery."""
    ts = now()
    with DB.tx() as c:
        dup = c.execute("SELECT * FROM claims WHERE idem_key=?", (idem_key,)).fetchone()
        if dup:
            return {"claim_ref": dup["claim_ref"], "code": dup["code"], "kind": dup["kind"], "duplicate": True}
        u = c.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
        if not u:
            raise ClaimError("no_user")
        if int(u["suspended"] or 0) == 1:
            raise ClaimError("suspended")
        per = max(1, gi("refs_per_reward", 1))
        mode = gs("reward_mode")
        bal = int(c.execute("SELECT COALESCE(SUM(delta),0) FROM ledger WHERE user_id=?", (uid,)).fetchone()[0])
        consume = 0
        if kind == "bonus":
            if gi("bonus_enabled", 1) != 1 or u["bonus_consumed_at"]:
                raise ClaimError("bonus_unavailable")
        elif kind == "referral":
            if bal < per:
                raise ClaimError("insufficient", need=per - bal)
            consume = per
        elif kind == "gift":
            if gi("gift_consumes_credit", 0) == 1:
                if bal < per:
                    raise ClaimError("insufficient", need=per - bal)
                consume = per
        elif kind in ("promo", "checkin"):
            if bal < per:
                raise ClaimError("insufficient", need=per - bal)
            consume = per
        else:
            raise ClaimError("bad_kind")
        if mode == "shared":
            pool = c.execute("SELECT id, code FROM rewards WHERE status='available' AND category=? AND (expires_at IS NULL OR expires_at>?) ORDER BY id",
                             (category, ts)).fetchall()
            if not pool:
                raise ClaimError("out_of_stock")
            n = int(c.execute("SELECT COUNT(*) FROM claims WHERE user_id=?", (uid,)).fetchone()[0])
            rw = pool[n % len(pool)]
        else:
            rw = c.execute("SELECT id, code FROM rewards WHERE status='available' AND category=? AND (expires_at IS NULL OR expires_at>?) ORDER BY id LIMIT 1",
                           (category, ts)).fetchone()
            if not rw:
                raise ClaimError("out_of_stock")
            if c.execute("UPDATE rewards SET status='allocated', used_by=?, used_at=? WHERE id=? AND status='available'", (uid, ts, rw["id"])).rowcount != 1:
                raise ClaimError("out_of_stock")
        ref = claim_reference()
        snapshot = json.dumps({"per": per, "mode": mode, "consumed": consume, "bonus_enabled": gi("bonus_enabled", 1)})
        c.execute("INSERT INTO claims(user_id,reward_id,code,kind,claimed_at,claim_ref,idem_key,delivery_status,rule_snapshot,category,issued_by) "
                  "VALUES(?,?,?,?,?,?,?,'pending',?,?,?)", (uid, rw["id"], rw["code"], kind, ts, ref, idem_key, snapshot, category, actor))
        if kind == "bonus":
            c.execute("UPDATE users SET bonus_consumed_at=? WHERE user_id=?", (ts, uid))
        if consume:
            c.execute("INSERT INTO ledger(user_id,delta,kind,ref_type,ref_id,note,rule_snapshot,idem_key,actor_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                      (uid, -consume, f"{kind}_consume", "claim", ref, f"{kind} reward", snapshot, f"consume:{ref}", actor, ts))
        c.execute("UPDATE users SET claims=claims+1 WHERE user_id=?", (uid,))
        c.execute("INSERT INTO audit_events(actor_id,action,target,after,result,created_at) VALUES(?,?,?,?,?,?)",
                  (actor or uid, "claim.create", ref, json.dumps({"kind": kind, "user": uid, "reward_id": rw["id"]}), "ok", ts))
        c.execute("INSERT OR IGNORE INTO jobs(kind,payload,status,next_run_at,created_at,idem_key) VALUES('deliver_claim',?,'pending',?,?,?)",
                  (json.dumps({"claim_ref": ref}), ts, ts, f"deliver:{ref}"))
    activity(uid, "claim", {"ref": ref, "kind": kind})
    left = stock_count(category)
    if mode != "shared" and left <= gi("low_stock_threshold", 3):
        notify_admins(f"lowstock:{left}", f"⚠️ <b>Low stock</b>: {left} reward code(s) left in <b>{esc(category)}</b>.", 6 * 3600)
    return {"claim_ref": ref, "code": rw["code"], "kind": kind, "duplicate": False}


def claim_by_ref(ref: str, uid: Optional[int] = None) -> Optional[sqlite3.Row]:
    if uid is None:
        return DB.one("SELECT * FROM claims WHERE claim_ref=?", (ref,))
    return DB.one("SELECT * FROM claims WHERE claim_ref=? AND user_id=?", (ref, uid))


def claims_page(uid: int, page: int = 0, size: int = 10, kind: Optional[str] = None) -> Tuple[List[sqlite3.Row], int]:
    where, args = "user_id=?", [uid]
    if kind:
        where += " AND kind=?"
        args.append(kind)
    total = int(DB.scalar(f"SELECT COUNT(*) FROM claims WHERE {where}", args))
    rows = DB.all(f"SELECT * FROM claims WHERE {where} ORDER BY id DESC LIMIT ? OFFSET ?", args + [size, page * size])
    return rows, total


def mark_delivery(ref: str, ok: bool, err: Optional[str] = None) -> None:
    if ok:
        DB.run("UPDATE claims SET delivery_status='delivered', delivered_at=?, delivery_attempts=delivery_attempts+1 WHERE claim_ref=?", (now(), ref))
    else:
        DB.run("UPDATE claims SET delivery_status='failed', delivery_attempts=delivery_attempts+1, note=? WHERE claim_ref=?", ((err or "")[:200], ref))


def adjust_credits(uid: int, delta: int, actor: int, reason: str) -> int:
    with DB.tx() as c:
        c.execute("INSERT INTO ledger(user_id,delta,kind,ref_type,note,idem_key,actor_id,created_at) VALUES(?,?,?,?,?,?,?,?)",
                  (uid, delta, "adjustment", "admin", reason, f"adj:{new_id()}", actor, now()))
        bal = int(c.execute("SELECT COALESCE(SUM(delta),0) FROM ledger WHERE user_id=?", (uid,)).fetchone()[0])
        c.execute("UPDATE users SET refs=MAX(0, refs+?) WHERE user_id=?", (delta, uid))
    audit(actor, "credits.adjust", uid, after={"delta": delta, "balance": bal}, reason=reason)
    return bal


def ledger_page(uid: int, page: int = 0, size: int = 15) -> Tuple[List[sqlite3.Row], int]:
    total = int(DB.scalar("SELECT COUNT(*) FROM ledger WHERE user_id=?", (uid,)))
    return DB.all("SELECT * FROM ledger WHERE user_id=? ORDER BY id DESC LIMIT ? OFFSET ?", (uid, size, page * size)), total


CODE_RE = re.compile(r"^[\x21-\x7E]{3,64}$")


def import_preview(codes: Iterable[str], category: str = "default") -> Dict[str, Any]:
    seen, valid, invalid, dup_in, dup_db = set(), [], [], [], []
    existing = {r["code"] for r in DB.all("SELECT code FROM rewards WHERE status='available' AND category=?", (category,))}
    for raw in codes:
        c = (raw or "").strip()
        if not c:
            continue
        if not CODE_RE.match(c):
            invalid.append(c[:40])
        elif c in seen:
            dup_in.append(c)
        elif c in existing:
            dup_db.append(c)
        else:
            seen.add(c)
            valid.append(c)
    return {"valid": valid, "invalid": invalid, "duplicates_in_input": dup_in, "duplicates_in_vault": dup_db}


def import_commit(codes: List[str], actor: int, category: str = "default", label: str = "", expires_at: Optional[int] = None) -> Dict[str, Any]:
    pv = import_preview(codes, category)
    ts = now()
    with DB.tx() as c:
        c.execute("INSERT INTO reward_batches(label,category,created_by,count,created_at) VALUES(?,?,?,?,?)",
                  (label or f"import {fmt_ts(ts)}", category, actor, len(pv["valid"]), ts))
        bid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        for code in pv["valid"]:
            c.execute("INSERT INTO rewards(code,added_at,status,category,batch_id,expires_at) VALUES(?,?,'available',?,?,?)",
                      (code, ts, category, bid, expires_at))
    audit(actor, "vault.import", bid, after={"added": len(pv["valid"]), "invalid": len(pv["invalid"]),
                                             "dup_vault": len(pv["duplicates_in_vault"]), "category": category})
    pv["batch_id"] = bid
    pv["restock_notified"] = restock_enqueue(category) if pv["valid"] else 0
    return pv


def vault_set_status(reward_id: int, status: str, actor: int) -> bool:
    if status not in ("available", "disabled", "archived", "expired"):
        return False
    n = DB.run("UPDATE rewards SET status=? WHERE id=? AND status<>'allocated'", (status, reward_id))
    if n:
        audit(actor, "vault.status", reward_id, after=status)
        invalidate_join_cache()
    return n > 0


def vault_page(status: Optional[str], page: int, size: int = 15, category: Optional[str] = None, q: str = "") -> Tuple[List[sqlite3.Row], int]:
    where, args = ["1=1"], []
    if status:
        where.append("status=?"); args.append(status)
    if category:
        where.append("category=?"); args.append(category)
    if q:
        where.append("code LIKE ?"); args.append(f"%{q}%")
    w = " AND ".join(where)
    total = int(DB.scalar(f"SELECT COUNT(*) FROM rewards WHERE {w}", args))
    return DB.all(f"SELECT * FROM rewards WHERE {w} ORDER BY id DESC LIMIT ? OFFSET ?", args + [size, page * size]), total

# ==== 12. ENGAGEMENT: PROMO CODES, CHECK-INS, LEADERBOARDS, WAITLIST ==========================

def redeem_promo(uid: int, code: str) -> Dict[str, Any]:
    code = (code or "").strip().upper()
    ts = now()
    with DB.tx() as c:
        p = c.execute("SELECT * FROM promo_codes WHERE code=?", (code,)).fetchone()
        if not p or int(p["enabled"] or 0) != 1:
            return {"ok": False, "error": "invalid"}
        if p["expires_at"] and int(p["expires_at"]) < ts:
            return {"ok": False, "error": "expired"}
        if int(p["max_total"] or 0) and int(p["redeemed"] or 0) >= int(p["max_total"]):
            return {"ok": False, "error": "exhausted"}
        mine = int(c.execute("SELECT COUNT(*) FROM promo_redemptions WHERE promo_id=? AND user_id=?", (p["id"], uid)).fetchone()[0])
        if mine >= max(1, int(p["max_per_user"] or 1)):
            return {"ok": False, "error": "already_used"}
        c.execute("INSERT INTO promo_redemptions(promo_id,user_id,n,created_at) VALUES(?,?,?,?)", (p["id"], uid, mine + 1, ts))
        c.execute("INSERT INTO ledger(user_id,delta,kind,ref_type,ref_id,note,idem_key,created_at) VALUES(?,?,?,?,?,?,?,?)",
                  (uid, int(p["points"]), "promo", "promo", str(p["id"]), code, f"promo:{p['id']}:{uid}:{mine + 1}", ts))
        c.execute("UPDATE promo_codes SET redeemed=redeemed+1 WHERE id=?", (p["id"],))
    activity(uid, "promo", {"code": code})
    return {"ok": True, "points": int(p["points"]), "balance": credit_balance(uid)}


def do_checkin(uid: int) -> Dict[str, Any]:
    pts = gi("checkin_points", 0)
    if pts <= 0:
        return {"ok": False, "error": "disabled"}
    day = day_key()
    try:
        with DB.tx() as c:
            c.execute("INSERT INTO checkins(user_id,day,created_at) VALUES(?,?,?)", (uid, day, now()))
            c.execute("INSERT INTO ledger(user_id,delta,kind,ref_type,ref_id,note,idem_key,created_at) VALUES(?,?,?,?,?,?,?,?)",
                      (uid, pts, "checkin", "checkin", day, "daily check-in", f"checkin:{uid}:{day}", now()))
    except sqlite3.IntegrityError:
        return {"ok": False, "error": "already", "day": day}
    return {"ok": True, "points": pts, "day": day, "balance": credit_balance(uid)}


def checked_in_today(uid: int) -> bool:
    return DB.one("SELECT 1 FROM checkins WHERE user_id=? AND day=?", (uid, day_key())) is not None


def leaderboard(period: str = "all", limit: int = 10) -> List[Dict[str, Any]]:
    since = period_start(period)
    excluded = set(staff_ids())
    rows = DB.all("SELECT r.referrer_id uid, COUNT(*) n, MAX(r.qualified_at) last_q, u.first_name, u.public_name, u.suspended "
                  "FROM referrals r JOIN users u ON u.user_id=r.referrer_id WHERE r.status='qualified' AND r.qualified_at>=? AND u.suspended=0 "
                  "GROUP BY r.referrer_id ORDER BY n DESC, last_q ASC, r.referrer_id ASC LIMIT ?", (since, limit + len(excluded)))
    out = []
    for r in rows:
        if int(r["uid"]) in excluded:
            continue
        out.append({"user_id": int(r["uid"]), "name": (r["first_name"] or "User")[:20] if int(r["public_name"] or 1) else "Anonymous", "count": int(r["n"])})
        if len(out) >= limit:
            break
    return out


def my_rank(uid: int, period: str = "all") -> Tuple[int, int]:
    since = period_start(period)
    mine = int(DB.scalar("SELECT COUNT(*) FROM referrals WHERE referrer_id=? AND status='qualified' AND qualified_at>=?", (uid, since)))
    better = int(DB.scalar("SELECT COUNT(*) FROM (SELECT referrer_id, COUNT(*) n FROM referrals WHERE status='qualified' AND qualified_at>=? "
                           "GROUP BY referrer_id HAVING n>?)", (since, mine)))
    return better + 1, mine


def waitlist_set(uid: int, on: bool, category: str = "default") -> None:
    if on:
        DB.run("INSERT INTO waitlist_v2(user_id,category,created_at,active) VALUES(?,?,?,1) ON CONFLICT(user_id,category) DO UPDATE SET active=1",
               (uid, category, now()))
    else:
        DB.run("UPDATE waitlist_v2 SET active=0 WHERE user_id=? AND category=?", (uid, category))


def waitlist_on(uid: int, category: str = "default") -> bool:
    return DB.one("SELECT 1 FROM waitlist_v2 WHERE user_id=? AND category=? AND active=1", (uid, category)) is not None


def restock_enqueue(category: str = "default") -> int:
    """Stock-aware, deduplicated restock notifications. Subscriptions are never cleared by an attempt."""
    if stock_count(category) <= 0:
        return 0
    cooldown = max(1, gi("restock_cooldown_hours", 12)) * 3600
    ts = now()
    n = 0
    with DB.tx() as c:
        rows = c.execute("SELECT w.user_id FROM waitlist_v2 w JOIN users u ON u.user_id=w.user_id WHERE w.category=? AND w.active=1 AND u.suspended=0 "
                         "AND (w.last_notified_at IS NULL OR w.last_notified_at<?) AND (u.unreachable_at IS NULL OR u.unreachable_at<?)",
                         (category, ts - cooldown, ts - 7 * 86400)).fetchall()
        for r in rows:
            cur = c.execute("INSERT OR IGNORE INTO jobs(kind,payload,status,next_run_at,created_at,idem_key) VALUES('restock',?,'pending',?,?,?)",
                            (json.dumps({"user_id": r["user_id"], "category": category}), ts, ts, f"restock:{r['user_id']}:{category}:{day_key()}"))
            if cur.rowcount:
                c.execute("UPDATE waitlist_v2 SET last_notified_at=? WHERE user_id=? AND category=?", (ts, r["user_id"], category))
                n += 1
    return n

# ==== 13. JOBS, BROADCASTS, BACKUPS ===========================================================

def enqueue_job(kind: str, payload: Any, idem_key: Optional[str] = None, delay: int = 0) -> bool:
    with DB.tx() as c:
        cur = c.execute("INSERT OR IGNORE INTO jobs(kind,payload,status,next_run_at,created_at,idem_key) VALUES(?,?,'pending',?,?,?)",
                        (kind, json.dumps(payload, ensure_ascii=False), now() + delay, now(), idem_key))
        return cur.rowcount > 0


def notify_admins(key: str, text: str, throttle: int = 3600) -> None:
    bucket = now() // max(1, throttle)
    enqueue_job("admin_alert", {"text": text}, idem_key=f"alert:{key}:{bucket}")


def claim_due_jobs(limit: int = 25) -> List[sqlite3.Row]:
    ts = now()
    with DB.tx() as c:
        rows = c.execute("SELECT * FROM jobs WHERE status='pending' AND next_run_at<=? ORDER BY id LIMIT ?", (ts, limit)).fetchall()
        for r in rows:
            c.execute("UPDATE jobs SET status='running', attempts=attempts+1 WHERE id=?", (r["id"],))
    return rows


def finish_job(job_id: int, ok: bool, err: Optional[str] = None, retry_in: Optional[int] = None, max_attempts: int = 8) -> None:
    with DB.tx() as c:
        if ok:
            c.execute("UPDATE jobs SET status='done', done_at=?, last_error=NULL WHERE id=?", (now(), job_id))
            return
        r = c.execute("SELECT attempts FROM jobs WHERE id=?", (job_id,)).fetchone()
        if retry_in is None or (r and int(r["attempts"]) >= max_attempts):
            c.execute("UPDATE jobs SET status='dead', last_error=?, done_at=? WHERE id=?", ((err or "")[:300], now(), job_id))
        else:
            c.execute("UPDATE jobs SET status='pending', next_run_at=?, last_error=? WHERE id=?", (now() + retry_in, (err or "")[:300], job_id))


def recover_running_jobs() -> int:
    n = DB.run("UPDATE jobs SET status='pending', next_run_at=? WHERE status='running'", (now(),))
    n += DB.run("UPDATE broadcasts SET status='queued' WHERE status='running'")
    return n


AUDIENCES = {"all": "All active users", "verified": "Verified users", "unverified": "Unverified users",
             "claimed": "Users with at least one claim", "never_claimed": "Users without claims", "waitlist": "Restock subscribers"}


def audience_sql(aud: str) -> str:
    base = "SELECT user_id FROM users u WHERE suspended=0 AND deleted_at IS NULL AND (unreachable_at IS NULL OR unreachable_at<?)"
    return {"verified": base + " AND verified=1", "unverified": base + " AND verified=0", "claimed": base + " AND claims>0",
            "never_claimed": base + " AND claims=0",
            "waitlist": base + " AND EXISTS(SELECT 1 FROM waitlist_v2 w WHERE w.user_id=u.user_id AND w.active=1)"}.get(aud, base)


def audience_count(aud: str) -> int:
    return int(DB.scalar(f"SELECT COUNT(*) FROM ({audience_sql(aud)})", (now() - 30 * 86400,)))


def create_broadcast(actor: int, from_chat: int, message_id: int, audience: str = "all", pin: bool = False,
                     scheduled_at: Optional[int] = None) -> int:
    with DB.tx() as c:
        c.execute("INSERT INTO broadcasts(created_by,from_chat_id,message_id,audience,status,pin,scheduled_at,created_at) VALUES(?,?,?,?,'draft',?,?,?)",
                  (actor, from_chat, message_id, audience if audience in AUDIENCES else "all", 1 if pin else 0, scheduled_at, now()))
        return int(c.execute("SELECT last_insert_rowid()").fetchone()[0])


def launch_broadcast(bid: int, actor: int) -> bool:
    """Idempotent: only a draft can be launched; repeated confirmations return False."""
    ts = now()
    with DB.tx() as c:
        b = c.execute("SELECT * FROM broadcasts WHERE id=? AND status='draft'", (bid,)).fetchone()
        if not b:
            return False
        c.execute(f"INSERT OR IGNORE INTO broadcast_targets(broadcast_id,user_id,status,updated_at) SELECT ?, user_id, 'pending', ? FROM ({audience_sql(b['audience'])})",
                  (bid, ts, ts - 30 * 86400))
        total = int(c.execute("SELECT COUNT(*) FROM broadcast_targets WHERE broadcast_id=?", (bid,)).fetchone()[0])
        status = "scheduled" if b["scheduled_at"] and int(b["scheduled_at"]) > ts else "queued"
        c.execute("UPDATE broadcasts SET status=?, total=? WHERE id=? AND status='draft'", (status, total, bid))
    audit(actor, "broadcast.launch", bid, after={"total": total, "status": status})
    return True


def broadcast_control(bid: int, actor: int, action: str) -> bool:
    allowed = {"pause": ("queued", "running", "scheduled"), "resume": ("paused",), "cancel": ("draft", "queued", "running", "paused", "scheduled")}
    target = {"pause": "paused", "resume": "queued", "cancel": "cancelled"}[action]
    q = ",".join("?" * len(allowed[action]))
    n = DB.run(f"UPDATE broadcasts SET status=? WHERE id=? AND status IN ({q})", (target, bid, *allowed[action]))
    if n:
        audit(actor, f"broadcast.{action}", bid)
    return n > 0


def broadcast_summary(bid: int) -> Optional[Dict[str, Any]]:
    b = DB.one("SELECT * FROM broadcasts WHERE id=?", (bid,))
    if not b:
        return None
    d = dict(b)
    for st in ("pending", "sent", "failed", "skipped"):
        d[st] = int(DB.scalar("SELECT COUNT(*) FROM broadcast_targets WHERE broadcast_id=? AND status=?", (bid, st)))
    return d


def create_backup(db: Optional[Database] = None, note: str = "") -> Dict[str, Any]:
    """Consistent WAL-safe backup through SQLite's online backup API."""
    db = db or DB
    os.makedirs(CFG.backup_dir, exist_ok=True)
    name = f"backup-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}.db"
    path = os.path.join(CFG.backup_dir, name)
    src = db.connect()
    try:
        dst = sqlite3.connect(path)
        try:
            src.backup(dst)
            ok = dst.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            dst.close()
    finally:
        src.close()
    size = os.path.getsize(path)
    with contextlib.suppress(sqlite3.Error):
        with db.tx() as c:
            if _table_exists(c, "backups"):
                c.execute("INSERT INTO backups(path,size,created_at,ok,note) VALUES(?,?,?,?,?)", (path, size, now(), 1 if ok else 0, note))
    keep = sorted((p for p in os.listdir(CFG.backup_dir) if p.startswith("backup-") and p.endswith(".db")), reverse=True)
    for old in keep[15:]:
        with contextlib.suppress(OSError):
            os.remove(os.path.join(CFG.backup_dir, old))
    return {"path": path, "size": size, "ok": ok}


def validate_candidate_db(path: str) -> Optional[str]:
    try:
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            if c.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                return "integrity_check failed"
            names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"users", "rewards", "claims"} <= names:
                return "not a reward-bot database (missing core tables)"
        finally:
            c.close()
    except sqlite3.Error as e:
        return f"cannot open: {e}"
    return None


def restore_pending_path() -> str:
    return CFG.db_path + ".restore-pending"


def stage_restore(candidate_bytes: bytes, actor: int) -> Optional[str]:
    tmp = restore_pending_path() + ".tmp"
    with open(tmp, "wb") as f:
        f.write(candidate_bytes)
    err = validate_candidate_db(tmp)
    if err:
        os.remove(tmp)
        return err
    os.replace(tmp, restore_pending_path())
    audit(actor, "backup.restore_staged", restore_pending_path())
    return None


def apply_pending_restore() -> Optional[str]:
    """Called at startup before anything opens the database. Never destroys the working file on failure."""
    p = restore_pending_path()
    if not os.path.exists(p):
        return None
    err = validate_candidate_db(p)
    if err:
        log.error("pending restore rejected: %s", err)
        os.replace(p, p + ".rejected")
        return None
    if os.path.exists(CFG.db_path):
        pre = create_backup(Database(CFG.db_path), note="pre-restore")["path"]
        log.info("pre-restore backup: %s", pre)
    for suffix in ("-wal", "-shm"):
        with contextlib.suppress(OSError):
            os.remove(CFG.db_path + suffix)
    os.replace(p, CFG.db_path)
    return CFG.db_path


# ==== 14. NATIVE USER HANDLERS ================================================================

STATE_TTL = 900


def set_state(context: ContextTypes.DEFAULT_TYPE, name: Optional[str], **data: Any) -> None:
    if name is None:
        context.user_data.pop("state", None)
    else:
        context.user_data["state"] = {"name": name, "ts": now(), **data}


def get_state(context: ContextTypes.DEFAULT_TYPE) -> Optional[Dict[str, Any]]:
    st = context.user_data.get("state")
    if not st or now() - int(st.get("ts", 0)) > STATE_TTL:
        context.user_data.pop("state", None)
        return None
    return st


def B(text: str, data: Optional[str] = None, url: Optional[str] = None, web_app: Optional[str] = None) -> InlineKeyboardButton:
    if url:
        return InlineKeyboardButton(text, url=url)
    if web_app:
        return InlineKeyboardButton(text, web_app=WebAppInfo(url=web_app))
    return InlineKeyboardButton(text, callback_data=(data or "u:menu")[:64])


def KB(*rows: List[InlineKeyboardButton]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([r for r in rows if r])


def footer() -> str:
    return f"\n\n<i>Bot by</i> <b>{esc(CFG.dev_name)}</b>"


async def show(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, markup: Optional[InlineKeyboardMarkup] = None,
               photo: Optional[str] = None, edit: bool = True) -> None:
    """Edit the active menu when possible, otherwise send. Handles caption<->text transitions."""
    cq = update.callback_query
    chat_id = update.effective_chat.id if update.effective_chat else update.effective_user.id
    if cq and cq.message and edit:
        try:
            if cq.message.photo or cq.message.video or cq.message.document:
                if photo or len(text) <= 1024:
                    await cq.edit_message_caption(caption=text, reply_markup=markup, parse_mode=ParseMode.HTML)
                    return
                with contextlib.suppress(TelegramError):
                    await cq.message.delete()
            else:
                await cq.edit_message_text(text, reply_markup=markup, parse_mode=ParseMode.HTML, link_preview_options=tg_link_off())
                return
        except BadRequest as e:
            if "not modified" in str(e).lower():
                return
    if photo:
        try:
            await context.bot.send_photo(chat_id, photo, caption=text[:1024], reply_markup=markup, parse_mode=ParseMode.HTML)
            return
        except TelegramError:
            pass
    await context.bot.send_message(chat_id, text, reply_markup=markup, parse_mode=ParseMode.HTML, link_preview_options=tg_link_off())


async def answer(update: Update, text: Optional[str] = None, alert: bool = False) -> None:
    if update.callback_query and not context_answered(update):
        with contextlib.suppress(TelegramError):
            await update.callback_query.answer(text, show_alert=alert)


def context_answered(update: Update) -> bool:
    cq = update.callback_query
    if getattr(cq, "_answered", False):
        return True
    with contextlib.suppress(Exception):
        cq._answered = True  # type: ignore[attr-defined]
    return False


async def guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Optional[sqlite3.Row]:
    """Common entry: register/refresh the user, enforce suspension and maintenance."""
    tg = update.effective_user
    if not tg or (update.effective_chat and update.effective_chat.type != "private"):
        return None
    u, _ = ensure_user(tg)
    lang = user_lang(u)
    if is_suspended(u) and not is_staff(tg.id):
        await answer(update, T(lang, "not_allowed"), True)
        await show(update, context, T(lang, "suspended", reason=esc(u["suspended_reason"] or "—")) + footer(),
                   KB([B(T(lang, "support"), "u:support")]))
        return None
    if maintenance_on() and not is_staff(tg.id):
        await answer(update, T(lang, "maintenance"), True)
        await show(update, context, render("maintenance_text", u, tg) + footer(), KB([B(T(lang, "refresh"), "u:menu")]))
        return None
    return u


def primary_action(u: sqlite3.Row, vr: VerifyResult, lang: str) -> Tuple[str, InlineKeyboardButton]:
    el = eligibility(u)
    if not vr.ok:
        return "verify", B(T(lang, "verify"), "u:verify")
    if el["bonus_available"]:
        return "bonus", B(T(lang, "claim_bonus"), "u:claim")
    if el["referral_available"]:
        return "referral", B(T(lang, "claim_reward"), "u:claim")
    if el["stock"] == 0 and not waitlist_on(int(u["user_id"])):
        return "restock", B(T(lang, "restock"), "u:waitlist")
    return "invite", B(T(lang, "invite"), "u:refer")


def menu_markup(u: sqlite3.Row, vr: VerifyResult) -> InlineKeyboardMarkup:
    lang = user_lang(u)
    _, primary = primary_action(u, vr, lang)
    rows = [[primary],
            [B(T(lang, "refer"), "u:refer"), B(T(lang, "rewards"), "u:rewards:0")],
            [B(T(lang, "activity"), "u:activity:0"), B(T(lang, "profile"), "u:profile")],
            [B(T(lang, "top"), "u:top:all"), B(T(lang, "security"), "u:security")],
            [B(T(lang, "support"), "u:support"), B(T(lang, "help"), "u:help")]]
    extra = []
    if gi("checkin_points", 0) > 0:
        extra.append(B(T(lang, "checkin"), "u:checkin"))
    extra.append(B(T(lang, "promo"), "u:promo"))
    rows.append(extra)
    if CFG.public_base_url.startswith("https://"):
        rows.append([B("✨ " + T(lang, "open_app"), web_app=app_url())])
    rows.append([B(T(lang, "lang"), "u:lang"), B(f"👨‍💻 {CFG.dev_name}"[:30], url=f"https://t.me/{CFG.dev_username}")])
    return KB(*rows)


async def show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, u: sqlite3.Row, edit: bool = True) -> None:
    tg = update.effective_user
    vr = await check_membership(context.bot, tg.id)
    if not vr.ok:
        return await show_gate(update, context, u, vr, edit=edit)
    el = eligibility(u)
    lang = user_lang(u)
    txt = render("welcome_text", u, tg)
    txt += (f"\n\n👥 Qualified referrals: <b>{qualified_count(tg.id)}</b>  •  Credits: <b>{el['balance']}</b>\n"
            f"🎟 Rewards received: <b>{int(u['claims'] or 0)}</b>  •  Stock: <b>{esc(stock_label())}</b>")
    if el["bonus_available"]:
        txt += "\n\n🎁 Your <b>welcome bonus</b> is ready to claim."
    elif el["referral_available"]:
        txt += "\n\n🎉 A <b>referral reward</b> is ready to claim."
    else:
        txt += f"\n\n⏳ <b>{el['need']}</b> more referral(s) until your next reward ({el['per']} per reward)."
    ann = DB.one("SELECT title, body FROM announcements WHERE enabled=1 ORDER BY id DESC LIMIT 1")
    if ann:
        txt += f"\n\n📣 <b>{esc(ann['title'])}</b>\n{esc(ann['body'])[:300]}"
    txt += footer()
    photo = gs("start_photo").strip() or None
    await show(update, context, txt, menu_markup(u, vr), photo=photo if not (edit and update.callback_query) else None, edit=edit)


async def show_gate(update: Update, context: ContextTypes.DEFAULT_TYPE, u: sqlite3.Row, vr: VerifyResult, edit: bool = True) -> None:
    tg = update.effective_user
    lang = user_lang(u)
    txt = render("gate_text", u, tg) + "\n"
    rows = []
    for ch in required_channels():
        state = vr.states.get(int(ch["chat_id"]), "missing")
        icon = {"member": "✅", "requested": "🕒", "missing": "❌", "unknown": "⚠️", "optional": "➖"}[state]
        txt += f"\n{icon} <b>{esc(ch['title'])}</b>" + (" — request pending" if state == "requested" else "")
        if state in ("missing", "unknown", "requested", "optional"):
            link = await ensure_invite_link(context.bot, ch)
            if link:
                rows.append([B(f"📢 Join • {ch['title']}"[:60], url=link)])
    if vr.unavailable:
        txt += "\n\n⚠️ " + T(lang, "verification_unavailable")
    rows.append([B("✅ " + T(lang, "verify"), "u:verify")])
    rows.append([B(T(lang, "support"), "u:support"), B(T(lang, "help"), "u:help")])
    await show(update, context, txt + footer(), KB(*rows), edit=edit)


async def notify_referrer(bot: Any, info: Dict[str, Any]) -> None:
    if gi("notify_referrer", 1) != 1:
        return
    ref = get_user(info["referrer_id"])
    if not ref:
        return
    el = eligibility(ref)
    tail = "🎉 A reward is <b>ready</b> — claim it now!" if el["referral_available"] else f"<b>{el['need']}</b> more referral(s) to your next reward."
    with contextlib.suppress(TelegramError):
        await bot.send_message(int(ref["user_id"]),
                               f"🎊 <b>New qualified referral!</b>\n\nTotal qualified: <b>{qualified_count(int(ref['user_id']))}</b>\n{tail}",
                               parse_mode=ParseMode.HTML, reply_markup=KB([B(T(user_lang(ref), "claim_reward"), "u:claim")]))
    await send_log(bot, f"➕ <b>Referral qualified</b>\n👤 <code>{info['referee_id']}</code> → <code>{info['referrer_id']}</code>")


async def send_log(bot: Any, text: str) -> None:
    ch = gs("log_channel").strip()
    if ch:
        with contextlib.suppress(Exception):
            await bot.send_message(int(ch), text, parse_mode=ParseMode.HTML, link_preview_options=tg_link_off())


async def complete_verification(update: Update, context: ContextTypes.DEFAULT_TYPE, u: sqlite3.Row) -> None:
    uid = int(u["user_id"])
    if int(u["verified"] or 0) == 0:
        DB.run("UPDATE users SET verified=1 WHERE user_id=?", (uid,))
        activity(uid, "verified")
    info = qualify_referral(uid)
    if info:
        await notify_referrer(context.bot, info)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg = update.effective_user
    if not tg or update.effective_chat.type != "private":
        return
    ref, campaign = None, None
    if context.args:
        a = str(context.args[0]).strip()[:64]
        m = re.fullmatch(r"(\d{3,15})(?:_([A-Za-z0-9-]{1,24}))?", a)
        if m:
            ref, campaign = int(m.group(1)), m.group(2)
    u, created = ensure_user(tg, ref_by=ref, campaign=campaign)
    if created:
        await send_log(context.bot, f"🆕 <b>New user</b> {esc(tg.first_name)} (<code>{tg.id}</code>) ref: <code>{ref or '—'}</code>")
    u = await guard(update, context)
    if not u:
        return
    vr = await check_membership(context.bot, tg.id, force=True)
    if not vr.ok:
        return await show_gate(update, context, u, vr, edit=False)
    await complete_verification(update, context, u)
    await show_menu(update, context, get_user(tg.id), edit=False)  # type: ignore[arg-type]


async def verify_cb(update: Update, context: ContextTypes.DEFAULT_TYPE, u: sqlite3.Row) -> None:
    lang = user_lang(u)
    vr = await check_membership(context.bot, int(u["user_id"]), force=True)
    if vr.unavailable:
        await answer(update, T(lang, "verification_unavailable"), True)
        return await show_gate(update, context, u, vr)
    if vr.missing:
        await answer(update, T(lang, "still_pending", names=", ".join((c["title"] or "Channel") for c in vr.missing))[:190], True)
        return await show_gate(update, context, u, vr)
    await answer(update, T(lang, "verified"))
    await complete_verification(update, context, u)
    await show_menu(update, context, get_user(int(u["user_id"])))  # type: ignore[arg-type]


def reward_text(u: sqlite3.Row, tg: Any, claim: sqlite3.Row) -> str:
    el = eligibility(u)
    body = render("reward_text", u, tg)
    txt = (f"{body}\n\n🎁 <b>Your code</b>\n<code>{esc(claim['code'])}</code>\n\n"
           f"🏷 Type: <b>{CLAIM_KINDS.get(claim['kind'], claim['kind'])}</b>\n🧾 Reference: <code>{esc(claim['claim_ref'])}</code>\n"
           f"📅 Issued: {fmt_ts(claim['claimed_at'])}\n")
    if el["referral_available"] or el["bonus_available"]:
        txt += "\n✅ Another reward is ready — tap Claim again."
    else:
        txt += f"\n👥 <b>{el['need']}</b> more referral(s) for the next reward."
    return txt + footer()


async def do_claim(update: Update, context: ContextTypes.DEFAULT_TYPE, u: sqlite3.Row, approved: bool = False) -> None:
    tg = update.effective_user
    uid = tg.id
    lang = user_lang(u)
    if not RL.allow(f"claim:{uid}", 10, 60):
        return await answer(update, "Too many attempts. Please wait a minute.", True)
    vr = await check_membership(context.bot, uid, force=True)
    if vr.unavailable:
        await answer(update, T(lang, "verification_unavailable"), True)
        return await show_gate(update, context, u, vr)
    if vr.missing:
        await answer(update, "🔒 Join the required channels first.", True)
        return await show_gate(update, context, u, vr)
    await complete_verification(update, context, u)
    u = get_user(uid)  # type: ignore[assignment]
    el = eligibility(u)
    kind = "bonus" if el["bonus_available"] else ("referral" if el["referral_available"] else None)
    if not kind:
        await answer(update, T(lang, "locked", need=el["need"], per=el["per"]), True)
        return await show_refer(update, context, u)
    if el["stock"] <= 0:
        return await show_out_of_stock(update, context, u)
    if not approved and native_claim_needs_approval(uid):
        ch = create_challenge(uid, "native_claim", meta={"kind": kind})
        await send_challenge(context.bot, ch)
        return await answer(update, T(lang, "session_needed"), True)
    msg_id = update.callback_query.message.message_id if update.callback_query and update.callback_query.message else update.effective_message.message_id
    idem = f"native:{uid}:{msg_id}:{kind}:{int(u['claims'] or 0)}"
    try:
        res = perform_claim(uid, kind, idem)
    except ClaimError as e:
        if e.code == "out_of_stock":
            return await show_out_of_stock(update, context, u)
        if e.code == "suspended":
            return await answer(update, T(lang, "not_allowed"), True)
        await answer(update, T(lang, "locked", need=e.info.get("need", el["need"]), per=el["per"]), True)
        return await show_refer(update, context, u)
    claim = claim_by_ref(res["claim_ref"], uid)
    await answer(update, T(lang, "claimed"))
    tail = [[B(T(lang, "refer"), "u:refer"), B(T(lang, "rewards"), "u:rewards:0")], [B("🚩 Report a problem", f"u:report:{claim['claim_ref']}"), B(T(lang, "menu"), "u:menu")]]
    await show(update, context, reward_text(get_user(uid), tg, claim), buttons_markup(parse_buttons(gs("reward_buttons")), tail))  # type: ignore[arg-type]
    mark_delivery(claim["claim_ref"], True)
    if not res["duplicate"]:
        await send_log(context.bot, f"🎁 <b>Claim</b> {esc(tg.first_name)} (<code>{uid}</code>) {claim['kind']} • {esc(claim['claim_ref'])} • stock {stock_label()}")


async def show_out_of_stock(update: Update, context: ContextTypes.DEFAULT_TYPE, u: sqlite3.Row) -> None:
    lang = user_lang(u)
    on = waitlist_on(int(u["user_id"]))
    await answer(update, T(lang, "oos"), True)
    txt = render("outofstock_text", u, update.effective_user) + ("\n\n🔔 Restock alerts: <b>ON</b>" if on else "") + footer()
    await show(update, context, txt, KB([B("🔕 Turn off restock alerts" if on else "🔔 " + T(lang, "restock"), "u:waitlist")], [B(T(lang, "menu"), "u:menu")]))


async def show_refer(update: Update, context: ContextTypes.DEFAULT_TYPE, u: sqlite3.Row) -> None:
    tg = update.effective_user
    lang = user_lang(u)
    el = eligibility(u)
    st = referral_stats(tg.id)
    link = ref_link(tg.id, u["campaign"] or "")
    share = "https://t.me/share/url?url=" + urllib.parse.quote(link, safe="") + "&text=" + urllib.parse.quote(re.sub("<[^>]+>", "", gs("share_text")), safe="")
    txt = (f"👥 <b>{T(lang, 'refer')}</b>\n\n{render('referral_text', u, tg)}\n\n🔗 <b>Your link</b>\n<code>{esc(link)}</code>\n\n"
           f"Invited: <b>{st['invited']}</b> • Pending: <b>{st['pending']}</b> • Qualified: <b>{st['qualified']}</b>\n"
           f"Credits: <b>{el['balance']}</b> • Next reward in: <b>{el['need']}</b>" + footer())
    await show(update, context, txt, KB([B("📤 Share link", url=share)], [B(T(lang, "claim_reward"), "u:claim"), B(T(lang, "activity"), "u:activity:0")], [B(T(lang, "menu"), "u:menu")]))


async def show_activity(update: Update, context: ContextTypes.DEFAULT_TYPE, u: sqlite3.Row, page: int) -> None:
    lang = user_lang(u)
    items, total = referral_page(int(u["user_id"]), page, 8)
    icon = {"pending": "🕒", "qualified": "✅", "rejected": "❌", "reversed": "↩️"}
    lines = [f"📈 <b>{T(lang, 'activity')}</b>  ({total} total)\n"]
    if not items:
        lines.append("<i>No referrals yet. Share your link to get started.</i>")
    for it in items:
        lines.append(f"{icon.get(it['status'], '•')} <b>{esc(it['name'])}</b> — {it['status']}" + (f" <i>({esc(it['reason'])})</i>" if it["reason"] and it["status"] in ("rejected", "reversed") else "") + f"\n   {fmt_ts(it['created_at'])}")
    nav = []
    if page > 0:
        nav.append(B("◀️", f"u:activity:{page - 1}"))
    if (page + 1) * 8 < total:
        nav.append(B("▶️", f"u:activity:{page + 1}"))
    await show(update, context, "\n".join(lines) + footer(), KB(nav, [B(T(lang, "refer"), "u:refer"), B(T(lang, "menu"), "u:menu")]))


async def show_profile(update: Update, context: ContextTypes.DEFAULT_TYPE, u: sqlite3.Row) -> None:
    tg = update.effective_user
    lang = user_lang(u)
    el = eligibility(u)
    rank, mine = my_rank(tg.id)
    inst = int(DB.scalar("SELECT COUNT(*) FROM installations WHERE user_id=? AND status='approved'", (tg.id,)))
    txt = (f"👤 <b>{T(lang, 'profile')}</b>\n\n🏷 {esc(u['first_name'])}  •  <code>{tg.id}</code>\n📅 Joined: {fmt_ts(u['joined_at'])}\n"
           f"✅ Channels verified: <b>{'Yes' if int(u['verified'] or 0) else 'No'}</b>\n🔐 Approved sessions: <b>{inst}</b>\n\n"
           f"👥 Qualified referrals: <b>{qualified_count(tg.id)}</b>\n💠 Credits: <b>{el['balance']}</b>\n🎟 Rewards: <b>{int(u['claims'] or 0)}</b>\n"
           f"🏆 Rank: <b>#{rank}</b>\n🎁 Next reward: <b>{'READY' if el['referral_available'] or el['bonus_available'] else str(el['need']) + ' referral(s) away'}</b>\n"
           f"👁 Leaderboard name: <b>{'visible' if int(u['public_name'] or 1) else 'hidden'}</b>" + footer())
    await show(update, context, txt, KB([B("👁 Toggle leaderboard name", "u:pubname"), B("🔔 Alerts: " + ("ON" if waitlist_on(tg.id) else "OFF"), "u:waitlist")],
                                        [B("🔒 Privacy", "u:privacy"), B("📜 Rules", "u:rules")], [B(T(lang, "menu"), "u:menu")]))


async def show_top(update: Update, context: ContextTypes.DEFAULT_TYPE, u: sqlite3.Row, period: str) -> None:
    lang = user_lang(u)
    if gi("leaderboard_enabled", 1) != 1:
        return await show(update, context, "🏆 Leaderboard is disabled." + footer(), KB([B(T(lang, "menu"), "u:menu")]))
    period = period if period in ("all", "week", "month") else "all"
    top = leaderboard(period, 10)
    medals = ["🥇", "🥈", "🥉"] + ["🏅"] * 7
    lines = [f"🏆 <b>{T(lang, 'top')}</b> — {dict(all='All time', week='This week', month='This month')[period]} ({CFG.report_tz})\n"]
    if not top:
        lines.append("<i>No qualified referrals in this period yet.</i>")
    for i, r in enumerate(top):
        lines.append(f"{medals[i]} <b>{esc(r['name'])}</b> — {r['count']}")
    rank, mine = my_rank(int(u["user_id"]), period)
    lines.append(f"\n👤 You: <b>#{rank}</b> ({mine})")
    await show(update, context, "\n".join(lines) + footer(),
               KB([B("All", "u:top:all"), B("Week", "u:top:week"), B("Month", "u:top:month")], [B(T(lang, "refer"), "u:refer"), B(T(lang, "menu"), "u:menu")]))


async def show_rewards(update: Update, context: ContextTypes.DEFAULT_TYPE, u: sqlite3.Row, page: int) -> None:
    lang = user_lang(u)
    rows, total = claims_page(int(u["user_id"]), page, 6)
    lines = [f"🎟 <b>{T(lang, 'rewards')}</b>  ({total})\n"]
    kb_rows = []
    if not rows:
        lines.append("<i>No rewards yet.</i>")
    for c in rows:
        dot = "✅" if c["delivery_status"] == "delivered" else "📨"
        lines.append(f"{dot} {CLAIM_KINDS.get(c['kind'], c['kind'])} • <code>{esc(mask_code(c['code']))}</code> • {fmt_ts(c['claimed_at'])}")
        kb_rows.append([B(f"🔎 {c['claim_ref']}", f"u:reward:{c['claim_ref']}")])
    nav = []
    if page > 0:
        nav.append(B("◀️", f"u:rewards:{page - 1}"))
    if (page + 1) * 6 < total:
        nav.append(B("▶️", f"u:rewards:{page + 1}"))
    await show(update, context, "\n".join(lines) + footer(), KB(*kb_rows, nav, [B(T(lang, "menu"), "u:menu")]))


async def show_reward_detail(update: Update, context: ContextTypes.DEFAULT_TYPE, u: sqlite3.Row, ref: str) -> None:
    lang = user_lang(u)
    c = claim_by_ref(ref, int(u["user_id"]))
    if not c:
        return await answer(update, "Reward not found.", True)
    txt = (f"🎁 <b>Reward details</b>\n\n<code>{esc(c['code'])}</code>\n\n🧾 Reference: <code>{esc(c['claim_ref'])}</code>\n"
           f"🏷 Type: <b>{CLAIM_KINDS.get(c['kind'], c['kind'])}</b>\n📅 Issued: {fmt_ts(c['claimed_at'])}\n"
           f"📨 Delivery: <b>{c['delivery_status']}</b>\n\n<i>Tap and hold the code to copy it. Redemption happens on the partner site/app; this bot only issues codes.</i>" + footer())
    if c["delivery_status"] != "delivered":
        mark_delivery(c["claim_ref"], True)
    await show(update, context, txt, buttons_markup(parse_buttons(gs("reward_buttons")), [[B("🚩 Report a problem", f"u:report:{ref}")], [B(T(lang, "back"), "u:rewards:0"), B(T(lang, "menu"), "u:menu")]]))


async def show_security(update: Update, context: ContextTypes.DEFAULT_TYPE, u: sqlite3.Row) -> None:
    lang = user_lang(u)
    ov = sessions_overview(int(u["user_id"]))
    lines = [f"🔐 <b>{T(lang, 'security')}</b>\n", f"Policy: <b>{ov['policy']}</b>", ""]
    if not ov["installations"] and not ov["sessions"]:
        lines.append("<i>No Mini App sessions yet.</i>")
    rows = []
    for i in ov["installations"][:6]:
        lines.append(f"{'✅' if i['approved'] else '🕒' if i['status'] == 'pending' else '⛔'} <b>{esc(i['platform'])}</b> — first {fmt_ts(i['first_seen'])}, last {fmt_ts(i['last_seen'])}")
        rows.append([B(f"🗑 Revoke {i['platform']}"[:40], f"u:inst_revoke:{i['id']}")])
    lines.append(f"\nActive app sessions: <b>{len(ov['sessions'])}</b>")
    lines.append("\n".join(["", *ov["explain"]]))
    lines.append("\n<i>Lost your device or storage? Open the Mini App again — a fresh Telegram login plus account confirmation restores access. No approved session is required to recover.</i>")
    rows.append([B("🚪 Revoke all app sessions", "u:sess_revoke_all"), B(("🔔" if ov["notify_new_session"] else "🔕") + " New-session alerts", "u:notify_toggle")])
    rows.append([B("🔒 Privacy", "u:privacy"), B(T(lang, "menu"), "u:menu")])
    await show(update, context, "\n".join(lines) + footer(), KB(*rows))


async def show_help(update: Update, context: ContextTypes.DEFAULT_TYPE, u: sqlite3.Row) -> None:
    lang = user_lang(u)
    await show(update, context, render("help_text", u, update.effective_user) + footer(),
               KB([B("📜 Rules", "u:rules"), B("🔒 Privacy", "u:privacy")], [B(T(lang, "support"), "u:support"), B(T(lang, "menu"), "u:menu")]))


PRIVACY_TEXT = ("🔒 <b>Privacy</b>\n\nWe store your Telegram id, name, username, referral and reward history, support messages and a random "
                "installation identifier (as a digest) for session protection. We do not access IMEI, MAC addresses, contacts, installed apps "
                "or account creation dates, and we do not fingerprint your browser. IP addresses are not stored by default. "
                "Data is kept while your account is active; security logs are retained for {days} days. Use Support to request deletion; "
                "accounting records needed for abuse prevention are kept for the retention period.")


async def show_support(update: Update, context: ContextTypes.DEFAULT_TYPE, u: sqlite3.Row) -> None:
    lang = user_lang(u)
    if gi("support_enabled", 1) != 1:
        return await show(update, context, "🛟 Support is currently closed." + footer(), KB([B(T(lang, "menu"), "u:menu")]))
    tickets = DB.all("SELECT id, subject, status, updated_at FROM support_tickets WHERE user_id=? ORDER BY id DESC LIMIT 5", (u["user_id"],))
    lines = [render("support_text", u, update.effective_user), ""]
    rows = []
    for t in tickets:
        lines.append(f"#{t['id']} • {esc((t['subject'] or '')[:40])} — <b>{t['status']}</b>")
        rows.append([B(f"💬 Ticket #{t['id']}", f"u:ticket:{t['id']}")])
    contact = gs("support_contact").strip()
    rows.append([B("✍️ New ticket", "u:support_new")] + ([B("👤 Contact", url=f"https://t.me/{contact.lstrip('@')}")] if contact else []))
    rows.append([B(T(lang, "menu"), "u:menu")])
    await show(update, context, "\n".join(lines) + footer(), KB(*rows))


async def show_ticket(update: Update, context: ContextTypes.DEFAULT_TYPE, u: sqlite3.Row, tid: int) -> None:
    lang = user_lang(u)
    t = DB.one("SELECT * FROM support_tickets WHERE id=? AND user_id=?", (tid, u["user_id"]))
    if not t:
        return await answer(update, "Ticket not found.", True)
    msgs = DB.all("SELECT * FROM support_messages WHERE ticket_id=? AND internal=0 ORDER BY id DESC LIMIT 8", (tid,))
    lines = [f"🎫 <b>Ticket #{tid}</b> — {t['status']}\n<b>{esc(t['subject'])}</b>\n"]
    for m in reversed(msgs):
        lines.append(f"{'🛠' if m['is_staff'] else '🙋'} {esc(m['body'])[:400]}\n<i>{fmt_ts(m['created_at'])}</i>\n")
    rows = [[B("✍️ Add message", f"u:ticket_msg:{tid}")]]
    if t["status"] == "resolved":
        rows.append([B("🔁 Reopen", f"u:ticket_reopen:{tid}")])
    rows.append([B(T(lang, "back"), "u:support"), B(T(lang, "menu"), "u:menu")])
    await show(update, context, "\n".join(lines) + footer(), KB(*rows))


def create_ticket(uid: int, subject: str, body: str, category: str = "other", claim_ref: Optional[str] = None) -> Optional[int]:
    if not RL.allow(f"ticket:{uid}", 3, 3600):
        return None
    with DB.tx() as c:
        c.execute("INSERT INTO support_tickets(user_id,category,subject,status,claim_ref,created_at,updated_at) VALUES(?,?,?,'open',?,?,?)",
                  (uid, category, subject[:80], claim_ref, now(), now()))
        tid = int(c.execute("SELECT last_insert_rowid()").fetchone()[0])
        c.execute("INSERT INTO support_messages(ticket_id,sender_id,is_staff,body,created_at) VALUES(?,?,0,?,?)", (tid, uid, body[:2000], now()))
    notify_admins(f"ticket:{tid}", f"🎫 New support ticket <b>#{tid}</b>: {esc(subject[:60])}", 1)
    return tid


def add_ticket_message(tid: int, uid: int, body: str, staff: bool = False, internal: bool = False) -> bool:
    if not RL.allow(f"tmsg:{uid}", 20, 3600):
        return False
    with DB.tx() as c:
        t = c.execute("SELECT * FROM support_tickets WHERE id=?", (tid,)).fetchone()
        if not t or (not staff and int(t["user_id"]) != uid):
            return False
        c.execute("INSERT INTO support_messages(ticket_id,sender_id,is_staff,internal,body,created_at) VALUES(?,?,?,?,?,?)",
                  (tid, uid, 1 if staff else 0, 1 if internal else 0, body[:2000], now()))
        c.execute("UPDATE support_tickets SET updated_at=?, status=CASE WHEN status='resolved' AND ?=0 THEN 'open' ELSE status END WHERE id=?",
                  (now(), 1 if staff else 0, tid))
    return True


async def user_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, u: sqlite3.Row, parts: List[str]) -> None:
    tg = update.effective_user
    uid = tg.id
    lang = user_lang(u)
    op = parts[0]
    arg = parts[1] if len(parts) > 1 else ""
    if op == "menu":
        await answer(update)
        return await show_menu(update, context, u)
    if op == "verify":
        return await verify_cb(update, context, u)
    if op == "claim":
        return await do_claim(update, context, u)
    if op == "refer":
        await answer(update)
        return await show_refer(update, context, u)
    if op == "activity":
        await answer(update)
        return await show_activity(update, context, u, int(arg or 0))
    if op == "profile":
        await answer(update)
        return await show_profile(update, context, u)
    if op == "top":
        await answer(update)
        return await show_top(update, context, u, arg or "all")
    if op == "rewards":
        await answer(update)
        return await show_rewards(update, context, u, int(arg or 0))
    if op == "reward":
        await answer(update)
        return await show_reward_detail(update, context, u, arg)
    if op == "security":
        await answer(update)
        return await show_security(update, context, u)
    if op == "inst_revoke":
        await answer(update, "Revoked." if revoke_installation(uid, arg) else "Not found.")
        return await show_security(update, context, u)
    if op == "sess_revoke_all":
        n = revoke_other_sessions(uid, None)
        await answer(update, f"{n} session(s) revoked.")
        return await show_security(update, context, u)
    if op == "notify_toggle":
        DB.run("UPDATE users SET notify_new_session=1-COALESCE(notify_new_session,1) WHERE user_id=?", (uid,))
        await answer(update, "Updated.")
        return await show_security(update, context, u)
    if op == "pubname":
        DB.run("UPDATE users SET public_name=1-COALESCE(public_name,1) WHERE user_id=?", (uid,))
        await answer(update, "Updated.")
        return await show_profile(update, context, get_user(uid))  # type: ignore[arg-type]
    if op == "waitlist":
        on = not waitlist_on(uid)
        waitlist_set(uid, on)
        await answer(update, "Restock alerts ON" if on else "Restock alerts OFF")
        return await show_menu(update, context, u)
    if op == "help":
        await answer(update)
        return await show_help(update, context, u)
    if op == "rules":
        await answer(update)
        return await show(update, context, render("rules_text", u, tg) + footer(), KB([B(T(lang, "help"), "u:help"), B(T(lang, "menu"), "u:menu")]))
    if op == "privacy":
        await answer(update)
        return await show(update, context, PRIVACY_TEXT.replace("{days}", str(CFG.retention_days)) + footer(), KB([B(T(lang, "security"), "u:security"), B(T(lang, "menu"), "u:menu")]))
    if op == "lang":
        new = "hi" if lang == "en" else "en"
        DB.run("UPDATE users SET lang=? WHERE user_id=?", (new, uid))
        await answer(update, "Language updated")
        return await show_menu(update, context, get_user(uid))  # type: ignore[arg-type]
    if op == "checkin":
        r = do_checkin(uid)
        msg = {"disabled": "Check-ins are not enabled.", "already": "Already checked in today."}.get(r.get("error", ""), f"+{r.get('points')} credits! Balance {r.get('balance')}")
        await answer(update, msg, True)
        return await show_menu(update, context, get_user(uid))  # type: ignore[arg-type]
    if op == "promo":
        await answer(update)
        set_state(context, "promo")
        return await show(update, context, "🎟 <b>Promo code</b>\n\nSend the promo code as a message.\n\n/cancel to abort" + footer(), KB([B(T(lang, "cancel"), "u:menu")]))
    if op == "support":
        await answer(update)
        return await show_support(update, context, u)
    if op == "support_new":
        await answer(update)
        set_state(context, "support_new")
        return await show(update, context, "✍️ <b>New ticket</b>\n\nDescribe your problem in one message (max 2000 chars). Mention a claim reference like <code>CL-XXXX</code> if relevant.\n\n/cancel to abort" + footer(), KB([B(T(lang, "cancel"), "u:support")]))
    if op == "report":
        await answer(update)
        set_state(context, "support_new", claim_ref=arg)
        return await show(update, context, f"🚩 <b>Report a problem</b> with <code>{esc(arg)}</code>\n\nDescribe the issue in one message.\n\n/cancel to abort" + footer(), KB([B(T(lang, "cancel"), f"u:reward:{arg}")]))
    if op == "ticket":
        await answer(update)
        return await show_ticket(update, context, u, int(arg))
    if op == "ticket_msg":
        await answer(update)
        set_state(context, "ticket_msg", tid=int(arg))
        return await show(update, context, f"✍️ Send your message for ticket #{arg}.\n\n/cancel to abort", KB([B(T(lang, "cancel"), f"u:ticket:{arg}")]))
    if op == "ticket_reopen":
        DB.run("UPDATE support_tickets SET status='open', updated_at=? WHERE id=? AND user_id=?", (now(), int(arg), uid))
        await answer(update, "Reopened.")
        return await show_ticket(update, context, u, int(arg))
    await answer(update, T(lang, "expired_menu"), True)


async def user_text_state(update: Update, context: ContextTypes.DEFAULT_TYPE, u: sqlite3.Row, st: Dict[str, Any], text: str) -> bool:
    """Non-admin text states. Returns True when handled."""
    uid = int(u["user_id"])
    lang = user_lang(u)
    msg = update.effective_message
    if st["name"] == "promo":
        set_state(context, None)
        r = redeem_promo(uid, text)
        out = {"invalid": "❌ Invalid promo code.", "expired": "❌ This promo code has expired.", "exhausted": "❌ This promo code is fully redeemed.",
               "already_used": "❌ You already used this code."}.get(r.get("error", ""), f"✅ +{r.get('points')} credits! Balance: <b>{r.get('balance')}</b>")
        await msg.reply_html(out, reply_markup=KB([B(T(lang, "menu"), "u:menu")]))
        return True
    if st["name"] == "support_new":
        set_state(context, None)
        tid = create_ticket(uid, text.splitlines()[0][:60] or "Support request", text, "reward" if st.get("claim_ref") else "other", st.get("claim_ref"))
        await msg.reply_html(f"✅ Ticket <b>#{tid}</b> created. We will reply here." if tid else "⏳ Too many tickets in the last hour. Please wait.",
                             reply_markup=KB([B(T(lang, "support"), "u:support"), B(T(lang, "menu"), "u:menu")]))
        return True
    if st["name"] == "ticket_msg":
        set_state(context, None)
        ok = add_ticket_message(int(st["tid"]), uid, text)
        await msg.reply_html("✅ Message added." if ok else "❌ Could not add the message.", reply_markup=KB([B("💬 Ticket", f"u:ticket:{st['tid']}"), B(T(lang, "menu"), "u:menu")]))
        return True
    return False


# ==== 15. NATIVE ADMIN HANDLERS ===============================================================

def A(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=("a:" + data)[:64])


def back_row(target: str = "home") -> List[InlineKeyboardButton]:
    return [A("⬅️ Back", target), A("🏠 Control Center", "home")]


def pager(prefix: str, page: int, total: int, size: int) -> List[InlineKeyboardButton]:
    row = []
    if page > 0:
        row.append(A("◀️", f"{prefix}:{page - 1}"))
    if (page + 1) * size < total:
        row.append(A("▶️", f"{prefix}:{page + 1}"))
    return row


async def deny(update: Update) -> None:
    await answer(update, "🚫 You do not have permission for that.", True)


async def confirm_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, action: str, target: Any, text: str,
                         back: str, payload: Any = None) -> None:
    cid = create_confirmation(update.effective_user.id, action, target, payload)
    await show(update, context, f"⚠️ <b>Please confirm</b>\n\n{text}\n\n<i>This confirmation expires in 5 minutes.</i>",
               KB([A("✅ Confirm", f"cf:{cid}"), A("❌ Cancel", back)]))


async def admin_home(update: Update, context: ContextTypes.DEFAULT_TYPE, edit: bool = True) -> None:
    uid = update.effective_user.id
    set_state(context, None)
    role = role_of(uid) or ROLE_ANALYST
    open_t = int(DB.scalar("SELECT COUNT(*) FROM support_tickets WHERE status='open'"))
    pend = int(DB.scalar("SELECT COUNT(*) FROM jobs WHERE status='pending'"))
    txt = (f"🛠 <b>Control Center</b> — {ROLE_LABELS[role]}\n\n"
           f"👥 Users: <b>{int(DB.scalar('SELECT COUNT(*) FROM users'))}</b>  •  📦 Stock: <b>{esc(stock_label())}</b>\n"
           f"🔗 Referrals per reward: <b>{gi('refs_per_reward', 1)}</b>  •  🎛 Mode: <b>{gs('reward_mode')}</b>\n"
           f"📢 Force join: <b>{'ON' if gi('force_join', 1) else 'OFF'}</b>  •  🛠 Maintenance: <b>{'ON' if maintenance_on() else 'OFF'}</b>\n"
           f"🔐 Session policy: <b>{effective_policy()}</b>  •  🎫 Open tickets: <b>{open_t}</b>  •  ⏳ Jobs: <b>{pend}</b>")
    cand = [("view_overview", A("📊 Overview", "stats")), ("vault_view", A("🎁 Vault", "v:0")),
            ("channels_manage", A("📢 Channels", "ch")), ("settings_manage", A("⚙️ Settings", "set")),
            ("users_view", A("👥 Users", "users")), ("broadcast", A("📣 Broadcasts", "bc")),
            ("support_view", A(f"🎫 Support ({open_t})", "sup:0")), ("content_manage", A("✍️ Content", "ct")),
            ("sessions_view", A("🔐 Sessions", "sess")), ("audit_view", A("🧾 Audit", "audit:0")),
            ("view_analytics", A("📈 Analytics", "ana")), ("backup", A("💾 Tools & Backup", "tools")),
            ("roles_manage", A("👮 Roles", "roles")), ("vault_manage", A("🎟 Promo codes", "promo")),
            ("content_manage", A("📣 Announcements", "ann"))]
    btns = [b for p, b in cand if has_perm(uid, p)]
    rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    rows.append([B("🏠 User menu", "u:menu")])
    await show(update, context, txt, KB(*rows), edit=edit)


async def a_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    d, w = now() - 86400, now() - 604800

    def s(q: str, a: Sequence = ()) -> int:
        return int(DB.scalar(q, a))

    v = {
        "users": s("SELECT COUNT(*) FROM users"), "verified": s("SELECT COUNT(*) FROM users WHERE verified=1"), "susp": s("SELECT COUNT(*) FROM users WHERE suspended=1"),
        "d": s("SELECT COUNT(*) FROM users WHERE joined_at>?", (d,)), "w": s("SELECT COUNT(*) FROM users WHERE joined_at>?", (w,)),
        "rp": s("SELECT COUNT(*) FROM referrals WHERE status='pending'"), "rq": s("SELECT COUNT(*) FROM referrals WHERE status='qualified'"), "rr": s("SELECT COUNT(*) FROM referrals WHERE status='reversed'"),
        "cl": s("SELECT COUNT(*) FROM claims"), "cb": s("SELECT COUNT(*) FROM claims WHERE kind='bonus'"), "cr": s("SELECT COUNT(*) FROM claims WHERE kind='referral'"), "cg": s("SELECT COUNT(*) FROM claims WHERE kind='gift'"),
        "dl": s("SELECT COUNT(*) FROM claims WHERE delivery_status<>'delivered'"),
        "va": s("SELECT COUNT(*) FROM rewards WHERE status='available'"), "vu": s("SELECT COUNT(*) FROM rewards WHERE status='allocated'"), "vd": s("SELECT COUNT(*) FROM rewards WHERE status IN ('disabled','expired','archived')"),
        "wl": s("SELECT COUNT(*) FROM waitlist_v2 WHERE active=1"), "ia": s("SELECT COUNT(*) FROM installations WHERE status='approved'"), "cp": s("SELECT COUNT(*) FROM challenges WHERE status='pending'"),
        "jp": s("SELECT COUNT(*) FROM jobs WHERE status='pending'"), "jd": s("SELECT COUNT(*) FROM jobs WHERE status='dead'"),
    }
    txt = ("📊 <b>Overview</b>\n\n"
           "👥 Users: <b>{users}</b> • verified <b>{verified}</b> • suspended <b>{susp}</b>\n🆕 24h: <b>{d}</b> • 7d: <b>{w}</b>\n\n"
           "🔗 Referrals: pending <b>{rp}</b> • qualified <b>{rq}</b> • reversed <b>{rr}</b>\n🎟 Claims: <b>{cl}</b> (bonus {cb}, referral {cr}, gift {cg})\n"
           "📨 Delivery pending/failed: <b>{dl}</b>\n\n📦 Vault: available <b>{va}</b> • allocated <b>{vu}</b> • disabled/expired <b>{vd}</b>\n"
           "🔔 Restock subscribers: <b>{wl}</b>\n🔐 Approved installations: <b>{ia}</b> • pending challenges <b>{cp}</b>\n⏳ Jobs pending <b>{jp}</b> • dead <b>{jd}</b>\n").format(**v)
    txt += (f"📢 Channels: <b>{len(required_channels())}</b> • 👮 Staff: <b>{len(staff_ids())}</b>\n\n"
            f"⏱ Uptime: <b>{(now() - APP_STARTED_AT) // 60} min</b> • Schema v{SCHEMA_VERSION} • TZ {esc(CFG.report_tz)}")
    await show(update, context, txt, KB([A("🔄 Refresh", "stats")], back_row()))


async def a_analytics(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lines = ["📈 <b>Last 7 days</b> (" + esc(CFG.report_tz) + ")\n", "<code>day         new  qual  claims</code>"]
    for i in range(6, -1, -1):
        d = datetime.now(tz()).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=i)
        a, b = int(d.timestamp()), int((d + timedelta(days=1)).timestamp())
        n = int(DB.scalar("SELECT COUNT(*) FROM users WHERE joined_at>=? AND joined_at<?", (a, b)))
        q = int(DB.scalar("SELECT COUNT(*) FROM referrals WHERE qualified_at>=? AND qualified_at<?", (a, b)))
        c = int(DB.scalar("SELECT COUNT(*) FROM claims WHERE claimed_at>=? AND claimed_at<?", (a, b)))
        lines.append(f"<code>{d.strftime('%Y-%m-%d')} {n:>4} {q:>5} {c:>7}</code>")
    camp = DB.all("SELECT COALESCE(campaign,'(none)') c, COUNT(*) n FROM referrals GROUP BY c ORDER BY n DESC LIMIT 5")
    lines.append("\n<b>Top campaigns</b>: " + ", ".join(f"{esc(r['c'])} {r['n']}" for r in camp))
    await show(update, context, "\n".join(lines), KB([A("🔄 Refresh", "ana")], back_row()))

# ---- vault ----

async def a_vault(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 0, status: str = "") -> None:
    uid = update.effective_user.id
    set_state(context, None)
    rows, total = vault_page(status or None, page, 10)
    lines = [f"🎁 <b>Reward Vault</b> — {status or 'all'} ({total})\n",
             f"Available <b>{stock_count()}</b> • Mode <b>{gs('reward_mode')}</b> • Low-stock alert at <b>{gi('low_stock_threshold', 3)}</b>\n"]
    kb = []
    for r in rows:
        icon = {"available": "🆓", "allocated": "✅", "disabled": "⛔", "expired": "⌛", "archived": "📁"}.get(r["status"], "•")
        lines.append(f"{icon} <code>{esc(mask_code(r['code']))}</code> #{r['id']}" + (f" → <code>{r['used_by']}</code>" if r["used_by"] else ""))
        act = [A(f"#{r['id']} 👁", f"v_reveal:{r['id']}")] if has_perm(uid, "vault_reveal") else []
        if has_perm(uid, "vault_manage") and r["status"] != "allocated":
            act.append(A("⛔ Disable" if r["status"] == "available" else "♻️ Enable", f"v_st:{r['id']}:{'disabled' if r['status'] == 'available' else 'available'}"))
        if act:
            kb.append(act)
    prefix = f"v" if not status else f"vf:{status}"
    top = []
    if has_perm(uid, "vault_manage"):
        top = [A("➕ Import codes", "v_add"), A("🔘 Reward buttons", "v_btn")]
    filt = [A("All", "v:0"), A("Free", "vf:available:0"), A("Used", "vf:allocated:0"), A("Off", "vf:disabled:0")]
    tail = []
    if has_perm(uid, "settings_manage"):
        tail.append(A(f"🎛 Mode: {gs('reward_mode')}", "v_mode"))
    if has_perm(uid, "vault_export"):
        tail.append(A("📤 Export", "v_exp"))
    await show(update, context, "\n".join(lines), KB(top, filt, *kb, pager(prefix, page, total, 10), tail, back_row()))


async def a_vault_reveal(update: Update, context: ContextTypes.DEFAULT_TYPE, rid: int) -> None:
    r = DB.one("SELECT * FROM rewards WHERE id=?", (rid,))
    if not r:
        return await answer(update, "Not found.", True)
    audit(update.effective_user.id, "vault.reveal", rid)
    await answer(update, f"#{rid}: {r['code']}\nstatus {r['status']}", True)


def parse_button_lines(text: str) -> Tuple[List[Dict[str, Any]], List[str]]:
    out, bad = [], []
    for i, line in enumerate(text.splitlines()):
        line = line.strip()
        if not line:
            continue
        if " - " not in line:
            bad.append(line[:40])
            continue
        label, url = line.rsplit(" - ", 1)
        if not label.strip() or not safe_url(url.strip()):
            bad.append(line[:40])
            continue
        out.append({"text": label.strip()[:40], "url": url.strip(), "row": i // 2 + 1})
    return out, bad

# ---- channels ----

async def a_channels(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    set_state(context, None)
    chans = DB.all("SELECT * FROM channels ORDER BY sort_order, added_at")
    lines = [f"📢 <b>Channels</b> — force join <b>{'ON' if gi('force_join', 1) else 'OFF'}</b> • auto-approve <b>{'ON' if gi('auto_approve', 1) else 'OFF'}</b>\n"]
    kb = []
    if not chans:
        lines.append("<i>No channels configured.</i>")
    for c in chans:
        st = "✅" if c["enabled"] else "⏸"
        lines.append(f"{st} <b>{esc(c['title'])}</b> <code>{c['chat_id']}</code> • {c['policy']}" + (f"\n   ⚠️ {esc(c['last_error'][:60])}" if c["last_error"] else ""))
        kb.append([A(f"{c['title'][:14]} • {c['policy']}", f"ch_pol:{c['chat_id']}"), A("⏸" if c["enabled"] else "▶️", f"ch_en:{c['chat_id']}"),
                   A("🗑", f"ch_del:{c['chat_id']}")])
    lines.append("\n<i>Policy: member = must be a member • request = a pending join request counts • optional = shown, not enforced</i>")
    await show(update, context, "\n".join(lines), KB([A("➕ Add channel", "ch_add"), A("🔗 Refresh links", "ch_relink")],
                                                     [A(f"📢 Force join: {'ON' if gi('force_join', 1) else 'OFF'}", "t:force_join"),
                                                      A(f"🤝 Auto-approve: {'ON' if gi('auto_approve', 1) else 'OFF'}", "t:auto_approve")],
                                                     *kb, back_row()))

# ---- settings ----

async def a_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    set_state(context, None)
    on = lambda k, d=1: "ON" if gi(k, d) else "OFF"  # noqa: E731
    txt = ("⚙️ <b>Settings</b>\n\n"
           f"🔗 Referrals per reward: <b>{gi('refs_per_reward', 1)}</b>\n🎁 Welcome bonus: <b>{on('bonus_enabled')}</b>\n"
           f"🛠 Maintenance: <b>{on('maintenance', 0)}</b>\n🚪 Re-verify after leaving a channel: <b>{on('leave_penalty')}</b>\n"
           f"🔔 Notify referrer: <b>{on('notify_referrer')}</b>\n🔐 Session policy: <b>{effective_policy()}</b> (env default {CFG.session_policy})\n"
           f"🎟 Gifts consume credits: <b>{on('gift_consumes_credit', 0)}</b>\n📅 Check-in credits/day: <b>{gi('checkin_points', 0)}</b>\n"
           f"🏆 Leaderboard: <b>{on('leaderboard_enabled')}</b>\n🛟 Support: <b>{on('support_enabled')}</b>\n"
           f"⏱ Qualification delay: <b>{gi('qualification_delay_seconds', 0)}s</b>\n📦 Low-stock alert: <b>{gi('low_stock_threshold', 3)}</b>\n"
           f"🔁 Restock cooldown: <b>{gi('restock_cooldown_hours', 12)}h</b>\n🗒 Log channel: <code>{esc(gs('log_channel') or '—')}</code>\n"
           f"🖼 Start photo: <b>{'set' if gs('start_photo') else '—'}</b>\n👤 Support contact: <b>{esc(gs('support_contact') or '—')}</b>")
    kb = [[A("🔗 Refs/reward", "ask:set_refs"), A(f"🎁 Bonus {on('bonus_enabled')}", "t:bonus_enabled")],
          [A(f"🛠 Maintenance {on('maintenance', 0)}", "t:maintenance"), A(f"🚪 Leave penalty {on('leave_penalty')}", "t:leave_penalty")],
          [A(f"🔔 Notify referrer {on('notify_referrer')}", "t:notify_referrer"), A(f"🔐 Policy: {effective_policy()}", "cycle_policy")],
          [A(f"🎟 Gift credits {on('gift_consumes_credit', 0)}", "t:gift_consumes_credit"), A("📅 Check-in credits", "ask:set_checkin")],
          [A(f"🏆 Leaderboard {on('leaderboard_enabled')}", "t:leaderboard_enabled"), A(f"🛟 Support {on('support_enabled')}", "t:support_enabled")],
          [A("⏱ Qualification delay", "ask:set_delay"), A("📦 Low-stock", "ask:set_lowstock")],
          [A("🔁 Restock cooldown", "ask:set_cooldown"), A("🗒 Log channel", "ask:set_log")],
          [A("🖼 Start photo", "ask:set_photo"), A("👤 Support contact", "ask:set_contact")], back_row()]
    await show(update, context, txt, KB(*kb))


ASK_TEXT = {
    "set_refs": "🔗 <b>Referrals per reward</b>\n\nSend a number from 1 to 1000.", "set_checkin": "📅 <b>Check-in credits</b>\n\nSend credits per daily check-in (0 disables).",
    "set_delay": "⏱ <b>Qualification delay</b>\n\nSeconds a referral must wait after verification before it qualifies (0 = instant).",
    "set_lowstock": "📦 <b>Low-stock alert threshold</b>\n\nSend a number.", "set_cooldown": "🔁 <b>Restock cooldown</b>\n\nHours between restock alerts to the same user.",
    "set_log": "🗒 <b>Log channel</b>\n\nSend the channel id (<code>-100…</code>) where the bot is admin, or <code>clear</code>.",
    "set_photo": "🖼 <b>Start photo</b>\n\nSend a photo, an https image URL, or <code>clear</code>.",
    "set_contact": "👤 <b>Support contact</b>\n\nSend a @username or <code>clear</code>.",
    "v_add": "➕ <b>Import reward codes</b>\n\nSend one code per line (or upload a .txt/.csv). You will see a preview before anything is saved.",
    "v_btn": "🔘 <b>Reward buttons</b>\n\nOne per line: <code>Label - https://link</code>. Send <code>clear</code> to remove all.",
    "ch_add": "➕ <b>Add channel</b>\n\n1. Make the bot an admin (with invite-link permission).\n2. Forward any post from the channel here, or send its id (<code>-100…</code>) or @username.",
    "u_find": "🔍 <b>Find user</b>\n\nSend a user id or @username.", "u_suspend": "⛔ <b>Suspend</b>\n\nSend the reason (shown to the user).",
    "u_credit": "💠 <b>Adjust credits</b>\n\nSend <code>&lt;delta&gt; &lt;reason&gt;</code>, e.g. <code>-2 duplicate account</code>.",
    "u_msg": "✉️ <b>Direct message</b>\n\nSend the message to deliver (text, photo or any media).", "u_note": "📝 <b>Note</b>\n\nSend an internal note for this user.",
    "ref_reverse": "↩️ <b>Reverse referral</b>\n\nSend the reason.", "bc_msg": "📣 <b>New broadcast</b>\n\nSend the message to broadcast (any media, formatting preserved).",
    "sup_reply": "💬 <b>Reply</b>\n\nSend the reply text. It is delivered to the user.", "sup_note": "📝 <b>Internal note</b>\n\nVisible to staff only.",
    "role_add": "👮 <b>Add staff</b>\n\nSend the Telegram user id.", "sess_find": "🔐 <b>Session review</b>\n\nSend a user id or @username.",
    "restore_wait": "♻️ <b>Restore</b>\n\nUpload the backup .db file. It is validated and staged; it becomes active on the next restart, after a pre-restore backup.",
    "promo_add": "🎟 <b>New promo code</b>\n\nSend: <code>CODE credits max_total max_per_user days</code>\nExample: <code>WELCOME5 1 100 1 30</code>",
    "ann_add": "📣 <b>New announcement</b>\n\nSend: <code>Title | body</code>", "ct_edit": "✍️ Send the new text (HTML allowed: b, i, u, s, code, a). Placeholders: {name} {per} {refs} {claims} {stock} {dev} {need} {balance}",
}


async def ask(update: Update, context: ContextTypes.DEFAULT_TYPE, state: str, back: str = "home", **data: Any) -> None:
    set_state(context, state, back=back, **data)
    await show(update, context, ASK_TEXT.get(state, "Send a value:") + "\n\n/cancel to abort", KB([A("❌ Cancel", back)]))

# ---- content ----

async def a_content(update: Update, context: ContextTypes.DEFAULT_TYPE, key: str = "") -> None:
    set_state(context, None)
    if not key:
        rows = [[A(f"{lbl}", f"ct_k:{k}")] for k, lbl in TEMPLATE_KEYS.items()]
        return await show(update, context, "✍️ <b>Content</b>\n\nChoose a template. Every change is versioned; you can restore the default.", KB(*rows, back_row()))
    if key not in TEMPLATE_KEYS:
        return await answer(update, "Unknown template.", True)
    body = content(key)
    v = DB.one("SELECT version, created_at FROM content_versions WHERE key=? AND lang='en' AND status='published' ORDER BY version DESC LIMIT 1", (key,))
    u = get_user(update.effective_user.id)
    txt = (f"✍️ <b>{TEMPLATE_KEYS[key]}</b> (<code>{key}</code>) — version {v['version'] if v else 'default'}\n\n<b>Source</b>\n<code>{esc(body[:1500])}</code>\n\n<b>Preview</b>\n"
           + render_template(body, template_values(u, update.effective_user))[:1200])
    await show(update, context, txt, KB([A("✏️ Edit", f"ct_edit:{key}"), A("↩️ Restore default", f"ct_restore:{key}")], back_row("ct")))

# ---- users ----

async def a_users(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    set_state(context, None)
    lt = DB.all("SELECT user_id, first_name, refs, claims, suspended FROM users ORDER BY joined_at DESC LIMIT 8")
    lines = ["👥 <b>Users</b>\n", "<b>Latest</b>"]
    kb = []
    for r in lt:
        lines.append(f"{'⛔' if r['suspended'] else '•'} {esc(r['first_name'])} <code>{r['user_id']}</code> (👥{r['refs']} 🎟{r['claims']})")
        kb.append([A(f"{(r['first_name'] or 'User')[:18]} • {r['user_id']}", f"u_card:{r['user_id']}")])
    await show(update, context, "\n".join(lines), KB([A("🔍 Find", "ask:u_find"), A("⛔ Suspended", "u_susp:0")], *kb, back_row()))


async def a_user_card(update: Update, context: ContextTypes.DEFAULT_TYPE, tid: int) -> None:
    uid = update.effective_user.id
    set_state(context, None)
    u = get_user(tid)
    if not u:
        return await answer(update, "User not found.", True)
    el = eligibility(u)
    st = referral_stats(tid)
    sess = int(DB.scalar("SELECT COUNT(*) FROM app_sessions WHERE user_id=? AND revoked_at IS NULL AND expires_at>?", (tid, now())))
    tickets = int(DB.scalar("SELECT COUNT(*) FROM support_tickets WHERE user_id=? AND status='open'", (tid,)))
    refby = f"<code>{u['ref_by']}</code>" if u["ref_by"] else "—"
    txt = (f"👤 <b>{esc(u['first_name'])}</b> {'@' + esc(u['username']) if u['username'] else ''}\n<code>{tid}</code> • role: <b>{role_of(tid) or 'user'}</b>\n\n"
           f"📅 Joined {fmt_ts(u['joined_at'])} • seen {fmt_ts(u['last_seen'])}\n✅ Verified: <b>{'yes' if u['verified'] else 'no'}</b> • Referred by: {refby}\n"
           f"{'⛔ <b>SUSPENDED</b>: ' + esc(u['suspended_reason']) if u['suspended'] else '🟢 Active'}\n\n"
           f"👥 Referrals: invited {st['invited']} • pending {st['pending']} • qualified <b>{st['qualified']}</b> • reversed {st['reversed']}\n"
           f"💠 Credits: <b>{el['balance']}</b> • 🎁 Bonus: {'used ' + fmt_ts(u['bonus_consumed_at']) if u['bonus_consumed_at'] else 'available'}\n"
           f"🎟 Claims: <b>{int(u['claims'] or 0)}</b> • 🔐 Sessions: {sess} • 🎫 Open tickets: {tickets}\n"
           f"🔔 Restock alerts: {'on' if waitlist_on(tid) else 'off'} • 🌐 {u['lang']}" + (f"\n📝 {esc(u['notes'][:200])}" if u["notes"] else ""))
    kb = []
    if has_perm(uid, "users_manage"):
        kb.append([A("♻️ Reinstate", f"u_reinstate:{tid}") if u["suspended"] else A("⛔ Suspend", f"u_suspend:{tid}"), A("✉️ Message", f"u_msg:{tid}")])
    if has_perm(uid, "users_adjust"):
        kb.append([A("🎁 Gift reward", f"u_gift:{tid}"), A("💠 Adjust credits", f"u_credit:{tid}")])
    if has_perm(uid, "referrals_review"):
        kb.append([A("👥 Referrals", f"u_refs:{tid}:0"), A("📒 Ledger", f"u_ledger:{tid}:0")])
    kb.append([A("🎟 Claims", f"u_claims:{tid}:0"), A("🔐 Sessions", f"sess_u:{tid}")])
    if has_perm(uid, "users_manage"):
        kb.append([A("📝 Note", f"u_note:{tid}"), A("🗑 Anonymize", f"u_anon:{tid}")])
    kb.append(back_row("users"))
    await show(update, context, txt, KB(*kb))


REF_ICON = {"pending": "🕒", "qualified": "✅", "rejected": "❌", "reversed": "↩️"}


async def a_user_refs(update: Update, context: ContextTypes.DEFAULT_TYPE, tid: int, page: int) -> None:
    items, total = referral_page(tid, page, 8)
    lines = [f"👥 <b>Referrals of</b> <code>{tid}</code> ({total})\n"]
    kb = []
    for it in items:
        lines.append(f"{REF_ICON.get(it['status'], '•')} {esc(it['name'])} — {it['status']} • {fmt_ts(it['created_at'])}")
        if it["status"] == "qualified" and has_perm(update.effective_user.id, "referrals_review"):
            kb.append([A(f"↩️ Reverse #{it['id']} ({it['name'][:12]})", f"ref_rev:{it['id']}:{tid}")])
    await show(update, context, "\n".join(lines), KB(*kb, pager(f"u_refs:{tid}", page, total, 8), back_row(f"u_card:{tid}")))


async def a_user_ledger(update: Update, context: ContextTypes.DEFAULT_TYPE, tid: int, page: int) -> None:
    rows, total = ledger_page(tid, page, 12)
    lines = [f"📒 <b>Ledger</b> <code>{tid}</code> — balance <b>{credit_balance(tid)}</b> ({total} entries)\n"]
    for r in rows:
        lines.append(f"{'+' if r['delta'] > 0 else ''}{r['delta']} • {r['kind']} • {esc((r['note'] or '')[:40])} • {fmt_ts(r['created_at'])}")
    await show(update, context, "\n".join(lines), KB(pager(f"u_ledger:{tid}", page, total, 12), back_row(f"u_card:{tid}")))


async def a_user_claims(update: Update, context: ContextTypes.DEFAULT_TYPE, tid: int, page: int) -> None:
    rows, total = claims_page(tid, page, 10)
    lines = [f"🎟 <b>Claims</b> <code>{tid}</code> ({total})\n"]
    kb = []
    for c in rows:
        lines.append(f"{c['claim_ref']} • {c['kind']} • {esc(mask_code(c['code']))} • {c['delivery_status']} • {fmt_ts(c['claimed_at'])}")
        if c["delivery_status"] != "delivered":
            kb.append([A(f"🔁 Redeliver {c['claim_ref']}", f"redeliver:{c['claim_ref']}:{tid}")])
    await show(update, context, "\n".join(lines), KB(*kb, pager(f"u_claims:{tid}", page, total, 10), back_row(f"u_card:{tid}")))

# ---- broadcasts ----

async def a_broadcasts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    set_state(context, None)
    bl = DB.all("SELECT * FROM broadcasts ORDER BY id DESC LIMIT 6")
    lines = ["📣 <b>Broadcasts</b>\n"]
    kb = [[A("➕ New broadcast", "ask:bc_msg")]]
    for b in bl:
        lines.append(f"#{b['id']} • {b['status']} • {AUDIENCES.get(b['audience'], b['audience'])} • sent {b['sent']}/{b['total']} • {fmt_ts(b['created_at'])}")
        kb.append([A(f"#{b['id']} {b['status']}", f"bc_view:{b['id']}")])
    kb.append(back_row())
    await show(update, context, "\n".join(lines), KB(*kb))


async def a_broadcast_view(update: Update, context: ContextTypes.DEFAULT_TYPE, bid: int) -> None:
    b = broadcast_summary(bid)
    if not b:
        return await answer(update, "Not found.", True)
    txt = (f"📣 <b>Broadcast #{bid}</b> — <b>{b['status']}</b>\n\nAudience: {AUDIENCES.get(b['audience'], b['audience'])} ({b['total']})\n"
           f"Pin: {'yes' if b['pin'] else 'no'} • Scheduled: {fmt_ts(b['scheduled_at'])}\n"
           f"✅ Sent {b['sent']} • ❌ Failed {b['failed']} • ⏭ Skipped {b['skipped']} • ⏳ Pending {b['pending']}\n"
           f"Started {fmt_ts(b['started_at'])} • Finished {fmt_ts(b['finished_at'])}")
    kb = []
    if b["status"] == "draft":
        kb.append([A(f"👥 {v}"[:30], f"bc_aud:{bid}:{k}") for k, v in list(AUDIENCES.items())[:3]])
        kb.append([A(f"👥 {v}"[:30], f"bc_aud:{bid}:{k}") for k, v in list(AUDIENCES.items())[3:]])
        kb.append([A(f"📌 Pin: {'ON' if b['pin'] else 'OFF'}", f"bc_pin:{bid}"), A("👁 Preview", f"bc_prev:{bid}")])
        kb.append([A("🚀 Launch", f"bc_go:{bid}"), A("🗑 Discard", f"bc_cancel:{bid}")])
    if b["status"] in ("queued", "running", "scheduled"):
        kb.append([A("⏸ Pause", f"bc_pause:{bid}"), A("⛔ Cancel", f"bc_cancel:{bid}")])
    if b["status"] == "paused":
        kb.append([A("▶️ Resume", f"bc_resume:{bid}"), A("⛔ Cancel", f"bc_cancel:{bid}")])
    kb.append([A("🔄 Refresh", f"bc_view:{bid}")])
    kb.append(back_row("bc"))
    await show(update, context, txt, KB(*kb))

# ---- support ----

async def a_support(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int) -> None:
    set_state(context, None)
    total = int(DB.scalar("SELECT COUNT(*) FROM support_tickets WHERE status<>'resolved'"))
    rows = DB.all("SELECT t.*, u.first_name FROM support_tickets t LEFT JOIN users u ON u.user_id=t.user_id WHERE t.status<>'resolved' "
                  "ORDER BY t.updated_at DESC LIMIT 8 OFFSET ?", (page * 8,))
    lines = [f"🎫 <b>Support inbox</b> ({total} open)\n"]
    kb = []
    for t in rows:
        lines.append(f"#{t['id']} {esc(t['first_name'])} • {esc((t['subject'] or '')[:36])} • {t['status']}" + (f" • {t['claim_ref']}" if t["claim_ref"] else ""))
        kb.append([A(f"#{t['id']} {(t['subject'] or '')[:24]}", f"sup_t:{t['id']}")])
    await show(update, context, "\n".join(lines), KB(*kb, pager("sup", page, total, 8), [A("✅ Resolved", "sup_res:0")], back_row()))


async def a_ticket(update: Update, context: ContextTypes.DEFAULT_TYPE, tid: int) -> None:
    uid = update.effective_user.id
    t = DB.one("SELECT t.*, u.first_name FROM support_tickets t LEFT JOIN users u ON u.user_id=t.user_id WHERE t.id=?", (tid,))
    if not t:
        return await answer(update, "Not found.", True)
    msgs = DB.all("SELECT * FROM support_messages WHERE ticket_id=? ORDER BY id DESC LIMIT 8", (tid,))
    lines = [f"🎫 <b>Ticket #{tid}</b> — {t['status']} • {esc(t['category'])}\n👤 {esc(t['first_name'])} <code>{t['user_id']}</code>" + (f" • {t['claim_ref']}" if t["claim_ref"] else "")
             + (f"\n🧑‍💻 Assigned: <code>{t['assigned_to']}</code>" if t["assigned_to"] else ""), f"<b>{esc(t['subject'])}</b>\n"]
    for m in reversed(msgs):
        who = "📝 note" if m["internal"] else ("🛠 staff" if m["is_staff"] else "🙋 user")
        lines.append(f"{who}: {esc(m['body'])[:350]}\n<i>{fmt_ts(m['created_at'])}</i>\n")
    kb = []
    if has_perm(uid, "support_reply"):
        kb.append([A("💬 Reply", f"sup_reply:{tid}"), A("📝 Note", f"sup_note:{tid}")])
        kb.append([A("✅ Resolve" if t["status"] != "resolved" else "🔁 Reopen", f"sup_close:{tid}"), A("🙋 Assign to me", f"sup_assign:{tid}")])
    kb.append([A("👤 User card", f"u_card:{t['user_id']}")])
    kb.append(back_row("sup:0"))
    await show(update, context, "\n".join(lines), KB(*kb))

# ---- sessions / roles / audit / tools ----

async def a_sessions_user(update: Update, context: ContextTypes.DEFAULT_TYPE, tid: int) -> None:
    uid = update.effective_user.id
    ov = sessions_overview(tid)
    ch = DB.all("SELECT * FROM challenges WHERE user_id=? AND status='pending' AND expires_at>?", (tid, now()))
    lines = [f"🔐 <b>Sessions</b> <code>{tid}</code> — policy {ov['policy']}\n"]
    kb = []
    for i in ov["installations"]:
        lines.append(f"{'✅' if i['approved'] else '🕒' if i['status'] == 'pending' else '⛔'} {esc(i['platform'])} • first {fmt_ts(i['first_seen'])} • last {fmt_ts(i['last_seen'])}")
        if has_perm(uid, "sessions_manage"):
            kb.append([A(f"🗑 Revoke {i['platform'][:14]}", f"sess_rv:{tid}:{i['id']}")])
    lines.append(f"\nActive sessions: <b>{len(ov['sessions'])}</b> • Pending challenges: <b>{len(ch)}</b>")
    if has_perm(uid, "sessions_manage"):
        kb.append([A("🚪 Revoke all sessions", f"sess_all:{tid}"), A("🔁 Reset approvals", f"sess_reset:{tid}")])
    kb.append(back_row(f"u_card:{tid}"))
    await show(update, context, "\n".join(lines), KB(*kb))


async def a_roles(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    set_state(context, None)
    lines = ["👮 <b>Staff & roles</b>\n", f"👑 Owner: <code>{CFG.owner_id}</code>"]
    for a in sorted(CFG.admin_ids):
        lines.append(f"🛡 <code>{a}</code> — Administrator (from ADMIN_IDS env, not removable here)")
    kb = []
    for r in DB.all("SELECT a.*, u.first_name FROM admins a LEFT JOIN users u ON u.user_id=a.user_id ORDER BY added_at"):
        lines.append(f"• <code>{r['user_id']}</code> {esc(r['first_name'] or '')} — {ROLE_LABELS.get(r['role'], r['role'])}")
        kb.append([A(f"{r['user_id']} • {ROLE_LABELS.get(r['role'], r['role'])[:12]}", f"role_pick:{r['user_id']}"), A("🗑", f"role_del:{r['user_id']}")])
    lines.append("\n<i>Roles: Administrator (all but roles/restore), Reward Manager (vault, credits), Support Agent (tickets, sessions view), Analyst (read-only).</i>")
    await show(update, context, "\n".join(lines), KB([A("➕ Add staff", "ask:role_add")], *kb, back_row()))


async def a_role_pick(update: Update, context: ContextTypes.DEFAULT_TYPE, tid: int) -> None:
    rows = [[A(ROLE_LABELS[r], f"role_set:{tid}:{r}")] for r in (ROLE_ADMIN, ROLE_REWARD, ROLE_SUPPORT, ROLE_ANALYST)]
    await show(update, context, f"👮 Choose a role for <code>{tid}</code>:", KB(*rows, back_row("roles")))


async def a_audit(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int) -> None:
    total = int(DB.scalar("SELECT COUNT(*) FROM audit_events"))
    rows = DB.all("SELECT * FROM audit_events ORDER BY id DESC LIMIT 12 OFFSET ?", (page * 12,))
    lines = [f"🧾 <b>Audit log</b> ({total})\n"]
    for r in rows:
        lines.append(f"<code>{fmt_ts(r['created_at'])}</code> {esc(r['action'])} • by <code>{r['actor_id']}</code> • {esc((r['target'] or '')[:24])}" + (f" • {esc(r['reason'][:40])}" if r["reason"] else ""))
    await show(update, context, "\n".join(lines), KB(pager("audit", page, total, 12), back_row()))


async def a_tools(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    set_state(context, None)
    last = DB.one("SELECT * FROM backups ORDER BY id DESC LIMIT 1")
    pending = os.path.exists(restore_pending_path())
    txt = (f"💾 <b>Tools & Backup</b>\n\nDB: <code>{esc(CFG.db_path)}</code> ({os.path.getsize(CFG.db_path) // 1024 if os.path.exists(CFG.db_path) else 0} KB)\n"
           f"Last backup: {fmt_ts(last['created_at']) if last else '—'} {'✅' if last and last['ok'] else ''}\n"
           f"Staged restore: <b>{'YES — applies on next restart' if pending else 'no'}</b>\nMode: <b>{CFG.bot_mode}</b> • Env: <b>{CFG.app_env}</b>")
    kb = [[A("💾 Backup now", "bk_now"), A("📤 Users CSV", "exp:users")], [A("📤 Claims CSV", "exp:claims"), A("📤 Referrals CSV", "exp:referrals")],
          [A("🔔 Send restock alerts", "wl_notify"), A("♻️ Clear join cache", "clr_cache")], [A("🧬 Migration report", "mig_report"), A("🩺 Health", "health")]]
    if has_perm(uid, "restore"):
        kb.append([A("♻️ Stage restore", "ask:restore_wait")] + ([A("🗑 Discard staged", "restore_discard")] if pending else []))
    kb.append(back_row())
    await show(update, context, txt, KB(*kb))


async def a_promos(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    set_state(context, None)
    rows = DB.all("SELECT * FROM promo_codes ORDER BY id DESC LIMIT 10")
    lines = ["🎟 <b>Promo codes</b>\n"]
    kb = []
    for p in rows:
        lines.append(f"{'✅' if p['enabled'] else '⛔'} <code>{esc(p['code'])}</code> +{p['points']} • {p['redeemed']}/{p['max_total'] or '∞'} • per user {p['max_per_user']} • exp {fmt_ts(p['expires_at'])}")
        kb.append([A(f"{'⛔ Disable' if p['enabled'] else '✅ Enable'} {p['code'][:16]}", f"promo_t:{p['id']}")])
    await show(update, context, "\n".join(lines), KB([A("➕ New promo", "ask:promo_add")], *kb, back_row()))


async def a_announcements(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    set_state(context, None)
    rows = DB.all("SELECT * FROM announcements ORDER BY id DESC LIMIT 8")
    lines = ["📣 <b>Announcements</b> (latest enabled one is shown on the dashboard)\n"]
    kb = []
    for a in rows:
        lines.append(f"{'✅' if a['enabled'] else '⛔'} <b>{esc(a['title'])}</b> — {esc(a['body'][:60])}")
        kb.append([A(f"{'⛔' if a['enabled'] else '✅'} {a['title'][:20]}", f"ann_t:{a['id']}"), A("🗑", f"ann_del:{a['id']}")])
    await show(update, context, "\n".join(lines), KB([A("➕ New announcement", "ask:ann_add")], *kb, back_row()))


async def send_csv(update: Update, context: ContextTypes.DEFAULT_TYPE, name: str, header: List[str], rows: Iterable[Sequence[Any]]) -> None:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    for r in rows:
        w.writerow([csv_safe(v) for v in r])
    bio = io.BytesIO(buf.getvalue().encode("utf-8"))
    bio.name = name
    await context.bot.send_document(update.effective_user.id, bio, filename=name, caption=f"📤 {name} • {fmt_ts(now())}")
    audit(update.effective_user.id, "export", name)


async def do_export(update: Update, context: ContextTypes.DEFAULT_TYPE, what: str) -> None:
    if what == "users":
        rows = DB.all("SELECT user_id, first_name, username, ref_by, refs, claims, verified, suspended, joined_at, last_seen, lang FROM users ORDER BY joined_at")
        await send_csv(update, context, "users.csv", ["user_id", "first_name", "username", "ref_by", "legacy_refs", "claims", "verified", "suspended", "joined_at", "last_seen", "lang"],
                       ([r["user_id"], r["first_name"], r["username"], r["ref_by"], r["refs"], r["claims"], r["verified"], r["suspended"], fmt_ts(r["joined_at"]), fmt_ts(r["last_seen"]), r["lang"]] for r in rows))
    elif what == "claims":
        rows = DB.all("SELECT claim_ref, user_id, kind, code, delivery_status, claimed_at, category FROM claims ORDER BY id")
        reveal = has_perm(update.effective_user.id, "vault_reveal")
        await send_csv(update, context, "claims.csv", ["claim_ref", "user_id", "kind", "code", "delivery", "claimed_at", "category"],
                       ([r["claim_ref"], r["user_id"], r["kind"], r["code"] if reveal else mask_code(r["code"]), r["delivery_status"], fmt_ts(r["claimed_at"]), r["category"]] for r in rows))
    elif what == "referrals":
        rows = DB.all("SELECT id, referrer_id, referee_id, status, reason, campaign, created_at, qualified_at FROM referrals ORDER BY id")
        await send_csv(update, context, "referrals.csv", ["id", "referrer_id", "referee_id", "status", "reason", "campaign", "created_at", "qualified_at"],
                       ([r["id"], r["referrer_id"], r["referee_id"], r["status"], r["reason"], r["campaign"], fmt_ts(r["created_at"]), fmt_ts(r["qualified_at"])] for r in rows))
    elif what == "codes":
        rows = DB.all("SELECT id, code, status, category, used_by, used_at, added_at FROM rewards ORDER BY id")
        await send_csv(update, context, "reward_codes.csv", ["id", "code", "status", "category", "used_by", "used_at", "added_at"],
                       ([r["id"], r["code"], r["status"], r["category"], r["used_by"], fmt_ts(r["used_at"]), fmt_ts(r["added_at"])] for r in rows))


def health_report() -> Dict[str, Any]:
    out: Dict[str, Any] = {"ok": True, "schema": SCHEMA_VERSION, "uptime_s": now() - APP_STARTED_AT, "mode": CFG.bot_mode, "ready": RT.ready}
    try:
        with DB.ro() as c:
            out["quick_check"] = c.execute("PRAGMA quick_check").fetchone()[0]
            out["schema_version"] = current_schema_version(c)
        out["jobs_pending"] = int(DB.scalar("SELECT COUNT(*) FROM jobs WHERE status='pending'"))
        out["jobs_dead"] = int(DB.scalar("SELECT COUNT(*) FROM jobs WHERE status='dead'"))
        out["stock"] = stock_count()
        out["restore_staged"] = os.path.exists(restore_pending_path())
        out["ok"] = out["quick_check"] == "ok" and out["schema_version"] == SCHEMA_VERSION
    except Exception as e:  # noqa: BLE001
        out["ok"] = False
        out["error"] = str(e)[:200]
    return out


# ---- admin callback dispatcher ----

TOGGLE_PERMS = {"force_join": "channels_manage", "auto_approve": "channels_manage"}
ASK_PERMS = {"set_refs": "settings_manage", "set_checkin": "settings_manage", "set_delay": "settings_manage", "set_lowstock": "settings_manage",
             "set_cooldown": "settings_manage", "set_log": "settings_manage", "set_photo": "content_manage", "set_contact": "settings_manage",
             "v_add": "vault_manage", "v_btn": "vault_manage", "ch_add": "channels_manage", "u_find": "users_view", "bc_msg": "broadcast",
             "role_add": "roles_manage", "sess_find": "sessions_view", "restore_wait": "restore", "promo_add": "vault_manage", "ann_add": "content_manage"}
ASK_BACK = {"v_add": "v:0", "v_btn": "v:0", "ch_add": "ch", "u_find": "users", "bc_msg": "bc", "role_add": "roles", "sess_find": "sess",
            "restore_wait": "tools", "promo_add": "promo", "ann_add": "ann"}


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, u: sqlite3.Row, parts: List[str]) -> None:
    uid = update.effective_user.id
    op, args = parts[0], parts[1:]
    arg = args[0] if args else ""
    P = lambda perm: has_perm(uid, perm)  # noqa: E731
    await answer(update)
    if op == "home":
        return await admin_home(update, context)
    if op == "stats" and P("view_overview"):
        return await a_stats(update, context)
    if op == "ana" and P("view_analytics"):
        return await a_analytics(update, context)
    if op == "ask":
        if not P(ASK_PERMS.get(arg, "roles_manage")):
            return await deny(update)
        return await ask(update, context, arg, ASK_BACK.get(arg, "set"))
    if op == "t":
        if not P(TOGGLE_PERMS.get(arg, "settings_manage")):
            return await deny(update)
        set_setting(arg, 0 if gi(arg, 1 if arg not in ("maintenance", "gift_consumes_credit") else 0) else 1, uid)
        invalidate_join_cache()
        return await (a_channels if arg in TOGGLE_PERMS else a_settings)(update, context)
    if op == "cycle_policy" and P("settings_manage"):
        order = ["off", "notify", "approve", "risk"]
        set_setting("session_policy", order[(order.index(effective_policy()) + 1) % 4], uid)
        return await a_settings(update, context)
    if op == "set" and P("settings_manage"):
        return await a_settings(update, context)
    # vault
    if op in ("v", "vf") and P("vault_view"):
        if op == "v":
            return await a_vault(update, context, int(arg or 0))
        return await a_vault(update, context, int(args[1]) if len(args) > 1 else 0, arg)
    if op == "v_reveal" and P("vault_reveal"):
        return await a_vault_reveal(update, context, int(arg))
    if op == "v_st" and P("vault_manage"):
        vault_set_status(int(arg), args[1], uid)
        return await a_vault(update, context)
    if op == "v_mode" and P("settings_manage"):
        set_setting("reward_mode", "shared" if gs("reward_mode") == "unique" else "unique", uid)
        return await a_vault(update, context)
    if op == "v_exp" and P("vault_export"):
        return await do_export(update, context, "codes")
    # channels
    if op == "ch" and P("channels_manage"):
        return await a_channels(update, context)
    if op == "ch_pol" and P("channels_manage"):
        c = DB.one("SELECT policy FROM channels WHERE chat_id=?", (int(arg),))
        nxt = {"member": "request", "request": "optional", "optional": "member"}.get(c["policy"] if c else "member", "member")
        DB.run("UPDATE channels SET policy=? WHERE chat_id=?", (nxt, int(arg)))
        audit(uid, "channel.policy", arg, after=nxt)
        invalidate_join_cache()
        return await a_channels(update, context)
    if op == "ch_en" and P("channels_manage"):
        DB.run("UPDATE channels SET enabled=1-COALESCE(enabled,1) WHERE chat_id=?", (int(arg),))
        audit(uid, "channel.toggle", arg)
        invalidate_join_cache()
        return await a_channels(update, context)
    if op == "ch_del" and P("channels_manage"):
        c = DB.one("SELECT title FROM channels WHERE chat_id=?", (int(arg),))
        return await confirm_prompt(update, context, "ch_del", arg, f"Remove channel <b>{esc(c['title'] if c else arg)}</b> from force-join?", "ch")
    if op == "ch_relink" and P("channels_manage"):
        for c in DB.all("SELECT * FROM channels"):
            DB.run("UPDATE channels SET invite_link=NULL WHERE chat_id=?", (c["chat_id"],))
            await ensure_invite_link(context.bot, DB.one("SELECT * FROM channels WHERE chat_id=?", (c["chat_id"],)))
        invalidate_join_cache()
        return await a_channels(update, context)
    # content
    if op == "ct" and P("content_manage"):
        return await a_content(update, context)
    if op == "ct_k" and P("content_manage"):
        return await a_content(update, context, arg)
    if op == "ct_edit" and P("content_manage"):
        return await ask(update, context, "ct_edit", f"ct_k:{arg}", key=arg)
    if op == "ct_restore" and P("content_manage"):
        return await confirm_prompt(update, context, "ct_restore", arg, f"Restore the default text for <b>{esc(TEMPLATE_KEYS.get(arg, arg))}</b>?", f"ct_k:{arg}")
    # users
    if op == "users" and P("users_view"):
        return await a_users(update, context)
    if op == "u_susp" and P("users_view"):
        page = int(arg or 0)
        rows = DB.all("SELECT user_id, first_name, suspended_reason FROM users WHERE suspended=1 ORDER BY suspended_at DESC LIMIT 10 OFFSET ?", (page * 10,))
        total = int(DB.scalar("SELECT COUNT(*) FROM users WHERE suspended=1"))
        kb = [[A(f"{(r['first_name'] or 'User')[:16]} • {r['user_id']}", f"u_card:{r['user_id']}")] for r in rows]
        return await show(update, context, f"⛔ <b>Suspended users</b> ({total})\n\n" + "\n".join(f"• {esc(r['first_name'])} <code>{r['user_id']}</code> — {esc(r['suspended_reason'])}" for r in rows),
                          KB(*kb, pager("u_susp", page, total, 10), back_row("users")))
    if op == "u_card" and P("users_view"):
        return await a_user_card(update, context, int(arg))
    if op == "u_refs" and P("referrals_review"):
        return await a_user_refs(update, context, int(arg), int(args[1]) if len(args) > 1 else 0)
    if op == "u_ledger" and P("referrals_review"):
        return await a_user_ledger(update, context, int(arg), int(args[1]) if len(args) > 1 else 0)
    if op == "u_claims" and P("users_view"):
        return await a_user_claims(update, context, int(arg), int(args[1]) if len(args) > 1 else 0)
    if op == "u_suspend" and P("users_manage"):
        if is_staff(int(arg)) and not is_owner(uid):
            return await deny(update)
        return await ask(update, context, "u_suspend", f"u_card:{arg}", target=int(arg))
    if op == "u_reinstate" and P("users_manage"):
        suspend_user(int(arg), uid, "reinstated", on=False)
        return await a_user_card(update, context, int(arg))
    if op == "u_msg" and P("users_manage"):
        return await ask(update, context, "u_msg", f"u_card:{arg}", target=int(arg))
    if op == "u_note" and P("users_manage"):
        return await ask(update, context, "u_note", f"u_card:{arg}", target=int(arg))
    if op == "u_credit" and P("users_adjust"):
        return await ask(update, context, "u_credit", f"u_card:{arg}", target=int(arg))
    if op == "u_gift" and P("users_adjust"):
        return await confirm_prompt(update, context, "gift", arg, f"Send one reward code from the vault to <code>{arg}</code> as a gift? Stock: {stock_count()}", f"u_card:{arg}")
    if op == "u_anon" and P("users_manage"):
        return await confirm_prompt(update, context, "anon", arg, f"Anonymize <code>{arg}</code>? Name/username are erased and the account is suspended; accounting history is kept.", f"u_card:{arg}")
    if op == "ref_rev" and P("referrals_review"):
        return await ask(update, context, "ref_reverse", f"u_refs:{args[1]}:0", ref_id=int(arg), target=int(args[1]))
    if op == "redeliver" and P("users_manage"):
        enqueue_job("deliver_claim", {"claim_ref": arg}, idem_key=f"redeliver:{arg}:{now() // 60}")
        await answer(update, "Queued for delivery.", True)
        return await a_user_claims(update, context, int(args[1]), 0)
    # broadcasts
    if op == "bc" and P("broadcast"):
        return await a_broadcasts(update, context)
    if op == "bc_view" and P("broadcast"):
        return await a_broadcast_view(update, context, int(arg))
    if op == "bc_aud" and P("broadcast"):
        DB.run("UPDATE broadcasts SET audience=? WHERE id=? AND status='draft'", (args[1] if args[1] in AUDIENCES else "all", int(arg)))
        return await a_broadcast_view(update, context, int(arg))
    if op == "bc_pin" and P("broadcast"):
        DB.run("UPDATE broadcasts SET pin=1-pin WHERE id=? AND status='draft'", (int(arg),))
        return await a_broadcast_view(update, context, int(arg))
    if op == "bc_prev" and P("broadcast"):
        b = DB.one("SELECT * FROM broadcasts WHERE id=?", (int(arg),))
        if b:
            with contextlib.suppress(TelegramError):
                await context.bot.copy_message(uid, b["from_chat_id"], b["message_id"])
        return
    if op == "bc_go" and P("broadcast"):
        b = broadcast_summary(int(arg))
        if not b or b["status"] != "draft":
            return await answer(update, "Not a draft.", True)
        return await confirm_prompt(update, context, "bc_go", arg, f"Launch broadcast #{arg} to <b>{audience_count(b['audience'])}</b> users ({AUDIENCES[b['audience']]})?", f"bc_view:{arg}")
    if op in ("bc_pause", "bc_resume", "bc_cancel") and P("broadcast"):
        broadcast_control(int(arg), uid, op[3:])
        return await a_broadcast_view(update, context, int(arg))
    # support
    if op == "sup" and P("support_view"):
        return await a_support(update, context, int(arg or 0))
    if op == "sup_res" and P("support_view"):
        rows = DB.all("SELECT id, subject FROM support_tickets WHERE status='resolved' ORDER BY updated_at DESC LIMIT 10")
        return await show(update, context, "✅ <b>Resolved tickets</b>", KB(*[[A(f"#{t['id']} {(t['subject'] or '')[:24]}", f"sup_t:{t['id']}")] for t in rows], back_row("sup:0")))
    if op == "sup_t" and P("support_view"):
        return await a_ticket(update, context, int(arg))
    if op in ("sup_reply", "sup_note") and P("support_reply"):
        return await ask(update, context, op, f"sup_t:{arg}", tid=int(arg))
    if op == "sup_close" and P("support_reply"):
        t = DB.one("SELECT status, user_id FROM support_tickets WHERE id=?", (int(arg),))
        if t:
            new = "open" if t["status"] == "resolved" else "resolved"
            DB.run("UPDATE support_tickets SET status=?, updated_at=? WHERE id=?", (new, now(), int(arg)))
            audit(uid, f"ticket.{new}", arg)
            if new == "resolved":
                with contextlib.suppress(TelegramError):
                    await context.bot.send_message(int(t["user_id"]), f"✅ Your support ticket <b>#{arg}</b> was marked resolved. You can reopen it from Support.", parse_mode=ParseMode.HTML)
        return await a_ticket(update, context, int(arg))
    if op == "sup_assign" and P("support_reply"):
        DB.run("UPDATE support_tickets SET assigned_to=?, updated_at=? WHERE id=?", (uid, now(), int(arg)))
        return await a_ticket(update, context, int(arg))
    # sessions
    if op == "sess" and P("sessions_view"):
        return await ask(update, context, "sess_find", "home")
    if op == "sess_u" and P("sessions_view"):
        return await a_sessions_user(update, context, int(arg))
    if op == "sess_rv" and P("sessions_manage"):
        revoke_installation(int(arg), args[1], actor=uid)
        return await a_sessions_user(update, context, int(arg))
    if op == "sess_all" and P("sessions_manage"):
        revoke_other_sessions(int(arg), None, actor=uid)
        return await a_sessions_user(update, context, int(arg))
    if op == "sess_reset" and P("sessions_manage"):
        with DB.tx() as c:
            c.execute("UPDATE installations SET status='revoked' WHERE user_id=?", (int(arg),))
            c.execute("UPDATE app_sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL", (now(), int(arg)))
            c.execute("UPDATE challenges SET status='expired' WHERE user_id=? AND status='pending'", (int(arg),))
        audit(uid, "sessions.reset", arg)
        return await a_sessions_user(update, context, int(arg))
    # roles
    if op == "roles" and P("roles_manage"):
        return await a_roles(update, context)
    if op == "role_pick" and P("roles_manage"):
        return await a_role_pick(update, context, int(arg))
    if op == "role_set" and P("roles_manage"):
        tid, role = int(arg), args[1]
        if role not in ROLE_PERMS or role == ROLE_OWNER or tid == CFG.owner_id:
            return await deny(update)
        DB.run("INSERT INTO admins(user_id,added_by,added_at,role) VALUES(?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET role=excluded.role", (tid, uid, now(), role))
        audit(uid, "role.set", tid, after=role)
        return await a_roles(update, context)
    if op == "role_del" and P("roles_manage"):
        return await confirm_prompt(update, context, "role_del", arg, f"Remove staff access for <code>{arg}</code>?", "roles")
    # audit / tools
    if op == "audit" and P("audit_view"):
        return await a_audit(update, context, int(arg or 0))
    if op == "tools" and P("backup"):
        return await a_tools(update, context)
    if op == "bk_now" and P("backup"):
        b = create_backup(note=f"manual by {uid}")
        audit(uid, "backup.create", b["path"], after={"size": b["size"], "ok": b["ok"]})
        with open(b["path"], "rb") as f:
            await context.bot.send_document(uid, f, filename=os.path.basename(b["path"]), caption=f"💾 Backup {'OK' if b['ok'] else 'FAILED integrity'} • {b['size'] // 1024} KB")
        return await a_tools(update, context)
    if op == "exp" and P("export"):
        return await do_export(update, context, arg)
    if op == "wl_notify" and P("broadcast"):
        n = restock_enqueue()
        return await answer(update, f"🔔 {n} restock alert(s) queued.", True)
    if op == "clr_cache" and P("channels_manage"):
        invalidate_join_cache()
        return await answer(update, "♻️ Join cache cleared.", True)
    if op == "mig_report" and P("backup"):
        rep = gs("legacy_reconciliation") or "{}"
        return await show(update, context, "🧬 <b>Migration report</b>\n\n<code>" + esc(rep[:3500]) + "</code>", KB(back_row("tools")))
    if op == "health" and P("backup"):
        return await show(update, context, "🩺 <b>Health</b>\n\n<code>" + esc(json.dumps(health_report(), indent=1)) + "</code>", KB([A("🔄", "health")], back_row("tools")))
    if op == "restore_discard" and P("restore"):
        with contextlib.suppress(OSError):
            os.remove(restore_pending_path())
        audit(uid, "backup.restore_discarded")
        return await a_tools(update, context)
    if op == "promo" and P("vault_manage"):
        return await a_promos(update, context)
    if op == "promo_t" and P("vault_manage"):
        DB.run("UPDATE promo_codes SET enabled=1-enabled WHERE id=?", (int(arg),))
        audit(uid, "promo.toggle", arg)
        return await a_promos(update, context)
    if op == "ann" and P("content_manage"):
        return await a_announcements(update, context)
    if op == "ann_t" and P("content_manage"):
        DB.run("UPDATE announcements SET enabled=1-enabled WHERE id=?", (int(arg),))
        return await a_announcements(update, context)
    if op == "ann_del" and P("content_manage"):
        DB.run("DELETE FROM announcements WHERE id=?", (int(arg),))
        audit(uid, "announcement.delete", arg)
        return await a_announcements(update, context)
    if op == "cf":
        conf = consume_confirmation(arg, uid)
        if not conf:
            return await answer(update, "This confirmation expired or was already used.", True)
        return await run_confirmed(update, context, conf)
    await deny(update)


async def run_confirmed(update: Update, context: ContextTypes.DEFAULT_TYPE, conf: Dict[str, Any]) -> None:
    uid = update.effective_user.id
    act, target, payload = conf["action"], conf["target"], conf["payload"]
    if act == "gift" and has_perm(uid, "users_adjust"):
        tid = int(target)
        try:
            res = perform_claim(tid, "gift", f"gift:{uid}:{tid}:{now()}", actor=uid)
        except ClaimError as e:
            return await show(update, context, f"❌ Gift failed: <b>{esc(e.code)}</b>", KB(back_row(f"u_card:{tid}")))
        audit(uid, "gift.issue", tid, after=res["claim_ref"])
        await show(update, context, f"🎁 Gift <code>{esc(res['claim_ref'])}</code> issued to <code>{tid}</code>. Delivery is queued.", KB(back_row(f"u_card:{tid}")))
        return
    if act == "anon" and has_perm(uid, "users_manage"):
        tid = int(target)
        DB.run("UPDATE users SET first_name='Deleted user', username=NULL, suspended=1, suspended_reason='account anonymized', deleted_at=? WHERE user_id=?", (now(), tid))
        with DB.tx() as c:
            c.execute("UPDATE app_sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL", (now(), tid))
            c.execute("UPDATE installations SET status='revoked' WHERE user_id=?", (tid,))
            c.execute("UPDATE waitlist_v2 SET active=0 WHERE user_id=?", (tid,))
        audit(uid, "user.anonymize", tid)
        return await a_user_card(update, context, tid)
    if act == "ch_del" and has_perm(uid, "channels_manage"):
        DB.run("DELETE FROM channels WHERE chat_id=?", (int(target),))
        audit(uid, "channel.remove", target)
        invalidate_join_cache()
        return await a_channels(update, context)
    if act == "ct_restore" and has_perm(uid, "content_manage"):
        restore_default_content(target, uid)
        return await a_content(update, context, target)
    if act == "role_del" and has_perm(uid, "roles_manage"):
        DB.run("DELETE FROM admins WHERE user_id=?", (int(target),))
        audit(uid, "role.remove", target)
        return await a_roles(update, context)
    if act == "bc_go" and has_perm(uid, "broadcast"):
        launch_broadcast(int(target), uid)
        return await a_broadcast_view(update, context, int(target))
    if act == "vault_import" and has_perm(uid, "vault_manage"):
        res = import_commit(payload["codes"], uid, payload.get("category", "default"))
        return await show(update, context, f"✅ Imported <b>{len(res['valid'])}</b> code(s) (batch #{res['batch_id']}). Restock alerts queued: {res['restock_notified']}.", KB(back_row("v:0")))
    await deny(update)


async def admin_input(update: Update, context: ContextTypes.DEFAULT_TYPE, u: sqlite3.Row, st: Dict[str, Any]) -> bool:
    """Admin text/media states. Returns True when handled."""
    uid = update.effective_user.id
    msg = update.effective_message
    name = st["name"]
    text = (msg.text or msg.caption or "").strip()
    back = st.get("back", "home")
    kb_back = KB([A("⬅️ Back", back)])
    if name in ASK_PERMS and not has_perm(uid, ASK_PERMS[name]):
        return False

    async def done(t: str) -> bool:
        set_state(context, None)
        await msg.reply_html(t, reply_markup=kb_back)
        return True

    if name in ("set_refs", "set_checkin", "set_delay", "set_lowstock", "set_cooldown"):
        key, lo, hi = {"set_refs": ("refs_per_reward", 1, 1000), "set_checkin": ("checkin_points", 0, 100), "set_delay": ("qualification_delay_seconds", 0, 30 * 86400),
                       "set_lowstock": ("low_stock_threshold", 0, 10000), "set_cooldown": ("restock_cooldown_hours", 1, 720)}[name]
        if not re.fullmatch(r"-?\d+", text) or not lo <= int(text) <= hi:
            await msg.reply_html(f"❌ Send a number between {lo} and {hi}.")
            return True
        set_setting(key, int(text), uid)
        return await done(f"✅ <b>{key}</b> = {int(text)}")
    if name == "set_log":
        if text.lower() == "clear":
            set_setting("log_channel", "", uid)
            return await done("✅ Log channel cleared.")
        if not re.fullmatch(r"-100\d{5,}", text):
            await msg.reply_html("❌ Send an id like <code>-1001234567890</code> or <code>clear</code>.")
            return True
        try:
            await context.bot.send_message(int(text), "🗒 Log channel connected.")
        except TelegramError as e:
            await msg.reply_html(f"❌ Cannot post there: {esc(e)}")
            return True
        set_setting("log_channel", text, uid)
        return await done("✅ Log channel set.")
    if name == "set_photo":
        if msg.photo:
            set_setting("start_photo", msg.photo[-1].file_id, uid)
            return await done("✅ Start photo updated.")
        if text.lower() == "clear":
            set_setting("start_photo", "", uid)
            return await done("✅ Start photo removed.")
        if text.startswith("https://") and safe_url(text):
            set_setting("start_photo", text, uid)
            return await done("✅ Start photo URL saved.")
        await msg.reply_html("❌ Send a photo, an https URL, or <code>clear</code>.")
        return True
    if name == "set_contact":
        val = "" if text.lower() == "clear" else text.lstrip("@")
        if val and not re.fullmatch(r"[A-Za-z0-9_]{5,32}", val):
            await msg.reply_html("❌ Invalid username.")
            return True
        set_setting("support_contact", val, uid)
        return await done("✅ Support contact updated.")
    if name == "v_add":
        raw = text
        if msg.document:
            if (msg.document.file_size or 0) > 2_000_000:
                await msg.reply_html("❌ File too large (max 2 MB).")
                return True
            f = await context.bot.get_file(msg.document.file_id)
            raw = bytes(await f.download_as_bytearray()).decode("utf-8", "replace")
        codes = [c for line in raw.splitlines() for c in ([line.split(",")[0]] if msg.document and "," in line else [line])]
        pv = import_preview(codes)
        if not pv["valid"]:
            await msg.reply_html(f"❌ No importable codes. Invalid: {len(pv['invalid'])}, already in vault: {len(pv['duplicates_in_vault'])}, repeated: {len(pv['duplicates_in_input'])}.")
            return True
        set_state(context, None)
        cid = create_confirmation(uid, "vault_import", "default", {"codes": pv["valid"], "category": "default"}, ttl=600)
        sample = ", ".join(mask_code(c) for c in pv["valid"][:5])
        await msg.reply_html(f"📋 <b>Import preview</b>\n\n✅ Valid: <b>{len(pv['valid'])}</b> ({sample}{'…' if len(pv['valid']) > 5 else ''})\n"
                             f"⚠️ Invalid: {len(pv['invalid'])}\n🔁 Already in vault: {len(pv['duplicates_in_vault'])}\n🔁 Repeated in input: {len(pv['duplicates_in_input'])}\n\nNothing is saved until you confirm.",
                             reply_markup=KB([A("✅ Import", f"cf:{cid}"), A("❌ Cancel", "v:0")]))
        return True
    if name == "v_btn":
        if text.lower() == "clear":
            set_setting("reward_buttons", "[]", uid)
            return await done("✅ Reward buttons cleared.")
        btns, bad = parse_button_lines(text)
        if bad or not btns:
            await msg.reply_html("❌ Could not parse: " + esc("; ".join(bad) or "no lines") + "\nFormat: <code>Label - https://link</code>")
            return True
        set_setting("reward_buttons", json.dumps(btns, ensure_ascii=False), uid)
        return await done(f"✅ {len(btns)} button(s) saved.")
    if name == "ch_add":
        ident: Any = None
        origin = getattr(msg, "forward_origin", None)
        chat = getattr(origin, "chat", None)
        if chat is not None:
            ident = chat.id
        elif re.fullmatch(r"-100\d{5,}", text):
            ident = int(text)
        elif re.fullmatch(r"@?[A-Za-z0-9_]{5,32}", text):
            ident = "@" + text.lstrip("@")
        if ident is None:
            await msg.reply_html("❌ Forward a channel post or send an id / @username.")
            return True
        try:
            title, link = await add_channel(context.bot, ident, uid)
        except (TelegramError, RuntimeError) as e:
            await msg.reply_html(f"❌ {esc(e)}")
            return True
        return await done(f"✅ Added <b>{esc(title)}</b>" + ("" if link else "\n⚠️ No invite link could be created; grant the bot the invite-users permission and use Refresh links."))
    if name == "u_find":
        t = find_user(text)
        if not t:
            await msg.reply_html("❌ User not found.")
            return True
        set_state(context, None)
        await msg.reply_html(f"👤 Found <code>{t['user_id']}</code>", reply_markup=KB([A("Open card", f"u_card:{t['user_id']}")]))
        return True
    if name == "u_suspend":
        if not text:
            return True
        suspend_user(int(st["target"]), uid, text[:200], on=True)
        with contextlib.suppress(TelegramError):
            await context.bot.send_message(int(st["target"]), T(user_lang(get_user(int(st["target"]))), "suspended", reason=esc(text[:200])), parse_mode=ParseMode.HTML)
        return await done("⛔ User suspended.")
    if name == "u_note":
        DB.run("UPDATE users SET notes=? WHERE user_id=?", (text[:500], int(st["target"])))
        audit(uid, "user.note", st["target"])
        return await done("📝 Note saved.")
    if name == "u_credit":
        m = re.fullmatch(r"([+-]?\d{1,5})\s+(.{3,200})", text, re.S)
        if not m:
            await msg.reply_html("❌ Format: <code>-2 reason</code>")
            return True
        bal = adjust_credits(int(st["target"]), int(m.group(1)), uid, m.group(2).strip())
        return await done(f"✅ Adjusted by {int(m.group(1)):+d}. New balance: <b>{bal}</b>")
    if name == "u_msg":
        try:
            await context.bot.copy_message(int(st["target"]), msg.chat_id, msg.message_id)
        except TelegramError as e:
            return await done(f"❌ Could not deliver: {esc(e)}")
        audit(uid, "user.message", st["target"])
        return await done("✉️ Delivered.")
    if name == "ref_reverse":
        ok = reverse_referral(int(st["ref_id"]), uid, text[:200] or "reversed by staff")
        return await done("↩️ Referral reversed and credit removed." if ok else "❌ Referral not in qualified state.")
    if name == "bc_msg":
        bid = create_broadcast(uid, msg.chat_id, msg.message_id)
        set_state(context, None)
        await msg.reply_html(f"📣 Draft #{bid} created. Choose the audience and launch.", reply_markup=KB([A("Open draft", f"bc_view:{bid}")]))
        return True
    if name in ("sup_reply", "sup_note"):
        tid = int(st["tid"])
        add_ticket_message(tid, uid, text or "(media)", staff=True, internal=(name == "sup_note"))
        if name == "sup_reply":
            t = DB.one("SELECT user_id FROM support_tickets WHERE id=?", (tid,))
            if t:
                with contextlib.suppress(TelegramError):
                    await context.bot.send_message(int(t["user_id"]), f"🛠 <b>Support reply</b> (ticket #{tid})\n\n{esc(text)}", parse_mode=ParseMode.HTML,
                                                   reply_markup=KB([B("💬 Open ticket", f"u:ticket:{tid}")]))
        return await done("✅ Saved." if name == "sup_note" else "✅ Reply sent.")
    if name == "role_add":
        if not re.fullmatch(r"\d{3,15}", text):
            await msg.reply_html("❌ Send a numeric user id.")
            return True
        set_state(context, None)
        await msg.reply_html(f"👮 Pick a role for <code>{text}</code>:", reply_markup=KB(*[[A(ROLE_LABELS[r], f"role_set:{text}:{r}")] for r in (ROLE_ADMIN, ROLE_REWARD, ROLE_SUPPORT, ROLE_ANALYST)]))
        return True
    if name == "sess_find":
        t = find_user(text)
        if not t:
            await msg.reply_html("❌ User not found.")
            return True
        set_state(context, None)
        await msg.reply_html(f"🔐 Sessions for <code>{t['user_id']}</code>", reply_markup=KB([A("Open", f"sess_u:{t['user_id']}")]))
        return True
    if name == "restore_wait":
        if not msg.document:
            await msg.reply_html("❌ Upload a .db file.")
            return True
        if (msg.document.file_size or 0) > 45_000_000:
            return await done("❌ File exceeds the 45 MB download limit of the Bot API; restore it on the server manually (see docs).")
        f = await context.bot.get_file(msg.document.file_id)
        err = stage_restore(bytes(await f.download_as_bytearray()), uid)
        return await done(f"❌ Rejected: {esc(err)}" if err else "✅ Restore staged. Restart the process to apply it (a pre-restore backup is taken first).")
    if name == "promo_add":
        m = re.fullmatch(r"([A-Za-z0-9_-]{3,32})\s+(\d{1,4})\s+(\d{1,7})\s+(\d{1,3})\s+(\d{1,4})", text)
        if not m:
            await msg.reply_html("❌ Format: <code>CODE credits max_total max_per_user days</code>")
            return True
        try:
            DB.run("INSERT INTO promo_codes(code,points,max_total,max_per_user,expires_at,enabled,created_at) VALUES(?,?,?,?,?,1,?)",
                   (m.group(1).upper(), int(m.group(2)), int(m.group(3)), max(1, int(m.group(4))), now() + int(m.group(5)) * 86400, now()))
        except sqlite3.IntegrityError:
            await msg.reply_html("❌ That code already exists.")
            return True
        audit(uid, "promo.create", m.group(1).upper())
        return await done(f"✅ Promo <code>{esc(m.group(1).upper())}</code> created.")
    if name == "ann_add":
        if "|" not in text:
            await msg.reply_html("❌ Format: <code>Title | body</code>")
            return True
        title, body = [x.strip() for x in text.split("|", 1)]
        DB.run("INSERT INTO announcements(title,body,created_by,created_at,enabled) VALUES(?,?,?,?,1)", (title[:60], body[:500], uid, now()))
        audit(uid, "announcement.create", title[:60])
        return await done("✅ Announcement published.")
    if name == "ct_edit":
        err = publish_content(st["key"], text, uid)
        return await done(f"❌ {esc(err)}" if err else f"✅ <b>{esc(TEMPLATE_KEYS.get(st['key'], st['key']))}</b> published.")
    return False

# ---- routers ----

LEGACY_CALLBACKS = {"menu": "menu", "verify": "verify", "claim": "claim", "refer": "refer", "profile": "profile", "top": "top:all", "myrewards": "rewards:0", "help": "help"}


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cq = update.callback_query
    data = cq.data or ""
    uid = update.effective_user.id
    if data.startswith("sa:"):
        return await session_approval_cb(update, context, data)
    u = await guard(update, context)
    if not u:
        return
    try:
        if data.startswith("u:"):
            return await user_callback(update, context, u, data[2:].split(":"))
        if data.startswith("a:"):
            if not is_staff(uid):
                return await deny(update)
            return await admin_callback(update, context, u, data[2:].split(":"))
        if data in LEGACY_CALLBACKS:
            return await user_callback(update, context, u, LEGACY_CALLBACKS[data].split(":"))
        if data.startswith("a_") and is_staff(uid):
            await answer(update, "This admin menu is from an older version.", True)
            return await admin_home(update, context, edit=False)
        await answer(update, T(user_lang(u), "expired_menu"), True)
    except PermissionDenied:
        await deny(update)
    except (ValueError, IndexError):
        await answer(update, T(user_lang(u), "expired_menu"), True)


async def session_approval_cb(update: Update, context: ContextTypes.DEFAULT_TYPE, data: str) -> None:
    parts = data.split(":")
    if len(parts) != 3:
        return await answer(update, "Invalid.", True)
    uid = update.effective_user.id
    decision = "approved" if parts[2] == "ok" else "denied"
    ch = consume_challenge(parts[1], uid, decision)
    if not ch:
        return await answer(update, "This request has expired, was already handled, or is not yours.", True)
    await answer(update, "Approved ✅" if decision == "approved" else "Denied ❌")
    if ch["action"] == "native_claim" and decision == "approved":
        u = await guard(update, context)
        if u:
            return await do_claim(update, context, u, approved=True)
        return
    txt = ("✅ <b>Approved.</b> You can continue in the Mini App." if decision == "approved" else "❌ <b>Denied.</b> The session was revoked. If this was not you, open Security & Sessions and revoke all sessions.")
    await show(update, context, txt, KB([B("🔐 Security & Sessions", "u:security"), B("🏠 Home", "u:menu")]))


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    tg = update.effective_user
    if not msg or not tg or update.effective_chat.type != "private":
        return
    u = await guard(update, context)
    if not u:
        return
    st = get_state(context)
    text = (msg.text or msg.caption or "").strip()
    if st:
        try:
            if await user_text_state(update, context, u, st, text):
                return
            if is_staff(tg.id) and await admin_input(update, context, u, st):
                return
        except PermissionDenied:
            set_state(context, None)
            return await msg.reply_html("🚫 Not allowed.")
    if RL.allow(f"hint:{tg.id}", 2, 60):
        await msg.reply_html("Use the buttons below or /start to open the menu.", reply_markup=KB([B("🏠 Home", "u:menu")]))


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    set_state(context, None)
    await update.effective_message.reply_html("❌ Cancelled.", reply_markup=KB([B("🏠 Home", "u:menu")] + ([A("🛠 Control Center", "home")] if is_staff(update.effective_user.id) else [])))


def simple_cmd(op: str) -> Callable:
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        u = await guard(update, context)
        if u:
            await user_callback(update, context, u, op.split(":"))
    return handler


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_staff(update.effective_user.id):
        return await update.effective_message.reply_html("🚫 Staff only.")
    u = await guard(update, context)
    if u:
        await admin_home(update, context, edit=False)


async def cmd_app(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    u = await guard(update, context)
    if not u:
        return
    if not CFG.public_base_url.startswith("https://"):
        return await update.effective_message.reply_html("The Mini App is not configured (PUBLIC_BASE_URL must be https).")
    await update.effective_message.reply_html("✨ Open the Mini App:", reply_markup=KB([B("✨ Open Mini App", web_app=app_url())]))


async def on_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    req = update.chat_join_request
    if not req:
        return
    ch = DB.one("SELECT * FROM channels WHERE chat_id=?", (req.chat.id,))
    if not ch:
        return
    DB.run("INSERT INTO requests(user_id,chat_id,status,ts) VALUES(?,?,'pending',?) ON CONFLICT(user_id,chat_id) DO UPDATE SET status='pending', ts=excluded.ts", (req.from_user.id, req.chat.id, now()))
    ensure_user(req.from_user)
    if gi("auto_approve", 1) == 1 and int(ch["auto_approve"] or 1) == 1:
        try:
            await context.bot.approve_chat_join_request(req.chat.id, req.from_user.id)
            DB.run("UPDATE requests SET status='approved' WHERE user_id=? AND chat_id=?", (req.from_user.id, req.chat.id))
        except TelegramError as e:
            log.warning("auto-approve failed: %s", e)
    invalidate_join_cache(req.from_user.id)
    with contextlib.suppress(TelegramError):
        await context.bot.send_message(req.from_user.id, f"✅ Your request to join <b>{esc(ch['title'])}</b> was received. Tap Verify in the bot to continue.",
                                       parse_mode=ParseMode.HTML, reply_markup=KB([B("✅ Verify", "u:verify")]))


async def on_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cm = update.chat_member
    if not cm or not DB.one("SELECT 1 FROM channels WHERE chat_id=?", (cm.chat.id,)):
        return
    uid = cm.new_chat_member.user.id
    st = cm.new_chat_member.status
    if st in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED):
        DB.run("DELETE FROM requests WHERE user_id=? AND chat_id=?", (uid, cm.chat.id))
        if gi("leave_penalty", 1) == 1:
            DB.run("UPDATE users SET verified=0 WHERE user_id=?", (uid,))
            activity(uid, "left_channel", {"chat": cm.chat.id})
    invalidate_join_cache(uid)


async def on_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cm = update.my_chat_member
    if not cm:
        return
    if cm.chat.type == "private":
        uid = cm.chat.id
        if cm.new_chat_member.status in (ChatMemberStatus.BANNED, ChatMemberStatus.LEFT):
            DB.run("UPDATE users SET unreachable_at=? WHERE user_id=?", (now(), uid))
        else:
            DB.run("UPDATE users SET unreachable_at=NULL WHERE user_id=?", (uid,))


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    err = context.error
    if isinstance(err, (NetworkError, TimedOut)):
        log.warning("network error: %s", err)
        return
    log.exception("handler error", exc_info=err)
    if isinstance(update, Update) and update.effective_user:
        with contextlib.suppress(Exception):
            if update.callback_query:
                await update.callback_query.answer("Something went wrong. Please try again.", show_alert=True)
            else:
                await context.bot.send_message(update.effective_user.id, "⚠️ Something went wrong. Please try again with /start.")


def register_handlers(app: Application) -> None:
    app.add_handler(CommandHandler("start", cmd_start))
    for cmd, op in [("menu", "menu"), ("home", "menu"), ("claim", "claim"), ("refer", "refer"), ("rewards", "rewards:0"), ("profile", "profile"),
                    ("top", "top:all"), ("security", "security"), ("support", "support"), ("help", "help"), ("lang", "lang"), ("promo", "promo")]:
        app.add_handler(CommandHandler(cmd, simple_cmd(op)))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("app", cmd_app))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(ChatJoinRequestHandler(on_join_request))
    app.add_handler(ChatMemberHandler(on_chat_member, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, on_message))
    app.add_error_handler(on_error)


# ==== 16. EMBEDDED MINI APP (HTML / CSS / JS) =================================================
# Served at /app. "__NONCE__" is replaced per request; the CSP allows only that nonce and Telegram's bridge.

MINIAPP_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="light dark">
<title>Rewards</title>
<script nonce="__NONCE__" src="https://telegram.org/js/telegram-web-app.js"></script>
<style nonce="__NONCE__">
:root{--bg:#0f141c;--surface:#171e29;--surface2:#1f2836;--text:#e8edf5;--muted:#93a0b4;--border:rgba(255,255,255,.08);--accent:#5b8cff;--accent2:#8b6cff;--ok:#39c47c;--warn:#f2b544;--danger:#ff6b6b;--radius:16px;--shadow:0 6px 24px rgba(0,0,0,.25);--nav:64px;--sat:env(safe-area-inset-top,0px);--sab:env(safe-area-inset-bottom,0px)}
[data-theme="light"]{--bg:#f3f5f9;--surface:#ffffff;--surface2:#eef1f6;--text:#12161d;--muted:#5d6878;--border:rgba(0,0,0,.08);--shadow:0 6px 24px rgba(20,30,50,.08)}
*{box-sizing:border-box}html,body{margin:0;height:100%}
body{background:var(--bg);color:var(--text);font:16px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Inter,sans-serif;-webkit-font-smoothing:antialiased;padding-top:var(--sat)}
button{font:inherit;color:inherit;background:none;border:0;cursor:pointer;touch-action:manipulation}
a{color:var(--accent)}
:focus-visible{outline:3px solid var(--accent);outline-offset:2px;border-radius:8px}
.hidden{display:none!important}
#app{max-width:560px;margin:0 auto;padding:12px 16px calc(var(--nav) + var(--sab) + 16px)}
.top{display:flex;align-items:center;justify-content:space-between;margin:4px 0 12px}
.top h1{font-size:20px;margin:0;font-weight:700;letter-spacing:.2px}
.iconbtn{width:44px;height:44px;border-radius:12px;display:inline-flex;align-items:center;justify-content:center;background:var(--surface);border:1px solid var(--border)}
.card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:16px;margin:0 0 12px;box-shadow:var(--shadow)}
.card h2{font-size:15px;margin:0 0 8px;color:var(--muted);font-weight:600;text-transform:uppercase;letter-spacing:.6px}
.hero{background:linear-gradient(135deg,rgba(91,140,255,.18),rgba(139,108,255,.14));border-color:rgba(91,140,255,.35)}
.big{font-size:28px;font-weight:800;margin:0}
.muted{color:var(--muted)}.small{font-size:13px}
.row{display:flex;gap:10px;align-items:center}.between{justify-content:space-between}.wrap{flex-wrap:wrap}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.stat{background:var(--surface2);border-radius:12px;padding:12px}.stat b{display:block;font-size:22px}
.btn{display:flex;align-items:center;justify-content:center;gap:8px;min-height:48px;padding:0 16px;border-radius:14px;font-weight:600;width:100%;border:1px solid var(--border);background:var(--surface2)}
.btn.primary{background:linear-gradient(135deg,var(--accent),var(--accent2));color:#fff;border:0}
.btn.danger{color:var(--danger)}.btn.sm{min-height:40px;width:auto;padding:0 12px;font-size:14px}
.btn:disabled{opacity:.55;cursor:default}
.list{display:flex;flex-direction:column;gap:8px}
.item{display:flex;justify-content:space-between;align-items:center;gap:10px;background:var(--surface2);border-radius:12px;padding:12px;min-height:52px}
.item .t{font-weight:600}.item .s{font-size:13px;color:var(--muted)}
.pill{font-size:12px;padding:3px 9px;border-radius:999px;background:var(--surface);border:1px solid var(--border);white-space:nowrap}
.pill.ok{color:var(--ok)}.pill.warn{color:var(--warn)}.pill.bad{color:var(--danger)}
.progress{height:10px;background:var(--surface2);border-radius:999px;overflow:hidden;margin:8px 0}
.progress i{display:block;height:100%;background:linear-gradient(90deg,var(--accent),var(--accent2));border-radius:999px;transition:width .4s}
.code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:20px;letter-spacing:1px;background:var(--surface2);padding:12px;border-radius:12px;word-break:break-all;text-align:center;user-select:all}
input,textarea{width:100%;font:inherit;color:var(--text);background:var(--surface2);border:1px solid var(--border);border-radius:12px;padding:12px;min-height:48px}
textarea{min-height:110px;resize:vertical}
label{display:block;font-size:13px;color:var(--muted);margin:10px 0 6px}
.sk{background:linear-gradient(90deg,var(--surface2) 25%,var(--surface) 50%,var(--surface2) 75%);background-size:200% 100%;animation:sh 1.2s infinite;border-radius:10px;height:16px;margin:8px 0}
.sk.tall{height:60px}
@keyframes sh{0%{background-position:200% 0}100%{background-position:-200% 0}}
nav{position:fixed;left:0;right:0;bottom:0;background:var(--surface);border-top:1px solid var(--border);padding-bottom:var(--sab);z-index:5}
nav .in{max-width:560px;margin:0 auto;display:grid;grid-template-columns:repeat(5,1fr);height:var(--nav)}
nav button{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:2px;font-size:11px;color:var(--muted);border-radius:0}
nav button[aria-current="page"]{color:var(--accent)}
nav svg{width:22px;height:22px;fill:none;stroke:currentColor;stroke-width:1.9;stroke-linecap:round;stroke-linejoin:round}
.toast{position:fixed;left:50%;bottom:calc(var(--nav) + var(--sab) + 16px);transform:translateX(-50%);background:var(--text);color:var(--bg);padding:10px 16px;border-radius:12px;font-size:14px;z-index:9;max-width:90%;box-shadow:var(--shadow)}
.center{text-align:center;padding:40px 12px}
.logo{width:72px;height:72px;border-radius:22px;margin:0 auto 14px;background:linear-gradient(135deg,var(--accent),var(--accent2));display:flex;align-items:center;justify-content:center}
.logo svg{width:38px;height:38px;stroke:#fff;fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.seg{display:flex;background:var(--surface2);border-radius:12px;padding:4px;gap:4px}.seg button{flex:1;min-height:36px;border-radius:9px;font-size:14px}.seg button[aria-pressed="true"]{background:var(--surface);font-weight:700}
.medal{width:30px;text-align:center;font-weight:700}
.sub{display:flex;align-items:center;gap:8px;margin:0 0 12px}
.sub h2{margin:0;font-size:18px;color:var(--text);text-transform:none;letter-spacing:0}
.ann{border-left:3px solid var(--accent);padding-left:10px;margin:8px 0}
.switch{width:46px;height:28px;border-radius:999px;background:var(--surface2);border:1px solid var(--border);position:relative;flex:none}
.switch::after{content:"";position:absolute;top:3px;left:3px;width:20px;height:20px;border-radius:50%;background:var(--muted);transition:transform .2s}
.switch[aria-checked="true"]{background:var(--accent)}.switch[aria-checked="true"]::after{transform:translateX(18px);background:#fff}
.msg{padding:10px 12px;border-radius:12px;margin:6px 0;background:var(--surface2);max-width:88%}.msg.staff{margin-left:auto;background:rgba(91,140,255,.18)}
@media (prefers-reduced-motion: reduce){*{animation:none!important;transition:none!important}}
</style>
</head>
<body>
<div id="app" role="main"></div>
<nav id="nav" class="hidden" aria-label="Main"></nav>
<script nonce="__NONCE__">
(function(){
"use strict";
var tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;
var S = { token:null, me:null, tab:"home", view:null, arg:null, busy:false, pollTimer:null, theme:localStorage.getItem("rb_theme")||"" };
var app = document.getElementById("app"), nav = document.getElementById("nav");
var LOCAL_ID = "rb_install", NOTICE = "rb_notice_v1";

function h(tag, attrs, children){
  var el = document.createElement(tag);
  if (attrs) for (var k in attrs){
    if (k === "class") el.className = attrs[k];
    else if (k === "text") el.textContent = attrs[k];
    else if (k === "onclick") el.addEventListener("click", attrs[k]);
    else if (k === "oninput") el.addEventListener("input", attrs[k]);
    else if (k === "style") { for (var s in attrs[k]) el.style[s] = attrs[k][s]; }
    else if (attrs[k] !== null && attrs[k] !== undefined) el.setAttribute(k, attrs[k]);
  }
  (children||[]).forEach(function(c){ if (c === null || c === undefined || c === false) return; el.appendChild(typeof c === "string" ? document.createTextNode(c) : c); });
  return el;
}
function svg(path){ var s = document.createElementNS("http://www.w3.org/2000/svg","svg"); s.setAttribute("viewBox","0 0 24 24"); s.setAttribute("aria-hidden","true"); var p = document.createElementNS("http://www.w3.org/2000/svg","path"); p.setAttribute("d", path); s.appendChild(p); return s; }
var IC = { home:"M3 11l9-8 9 8v9a2 2 0 0 1-2 2h-4v-6H9v6H5a2 2 0 0 1-2-2z", gift:"M20 12v9H4v-9M2 7h20v5H2zM12 22V7M12 7H7.5a2.5 2.5 0 1 1 0-5C11 2 12 7 12 7zm0 0h4.5a2.5 2.5 0 1 0 0-5C13 2 12 7 12 7z",
  users:"M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM23 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75", act:"M22 12h-4l-3 9L9 3l-3 9H2",
  user:"M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2M12 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8z", lock:"M19 11H5v10h14zM7 11V7a5 5 0 0 1 10 0v4", back:"M15 18l-6-6 6-6", sun:"M12 17a5 5 0 1 0 0-10 5 5 0 0 0 0 10zM12 1v2M12 21v2M4.2 4.2l1.4 1.4M18.4 18.4l1.4 1.4M1 12h2M21 12h2M4.2 19.8l1.4-1.4M18.4 5.6l1.4-1.4" };

function toast(msg){ var t = document.querySelector(".toast"); if (t) t.remove(); t = h("div",{class:"toast",role:"status","aria-live":"polite",text:msg}); document.body.appendChild(t); setTimeout(function(){ t.remove(); }, 2600); }
function haptic(kind){ try { tg && tg.HapticFeedback && (kind==="ok" ? tg.HapticFeedback.notificationOccurred("success") : tg.HapticFeedback.impactOccurred("light")); } catch(e){} }
function fmt(ts){ if (!ts) return "—"; var d = new Date(ts*1000); return d.toLocaleDateString(undefined,{day:"2-digit",month:"short",year:"numeric"}) + " " + d.toLocaleTimeString(undefined,{hour:"2-digit",minute:"2-digit"}); }
function applyTheme(){
  var scheme = S.theme || (tg && tg.colorScheme) || (matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark");
  document.documentElement.setAttribute("data-theme", scheme);
  if (tg && tg.themeParams && !S.theme){ var p = tg.themeParams, r = document.documentElement.style; if (p.bg_color) r.setProperty("--bg", p.bg_color); if (p.secondary_bg_color) r.setProperty("--surface", p.secondary_bg_color); if (p.text_color) r.setProperty("--text", p.text_color); if (p.hint_color) r.setProperty("--muted", p.hint_color); if (p.button_color) r.setProperty("--accent", p.button_color); }
  else document.documentElement.style.cssText = "";
  try { tg && tg.setHeaderColor && tg.setHeaderColor(scheme==="light" ? "#f3f5f9" : "#0f141c"); } catch(e){}
}
function toggleTheme(){ var cur = document.documentElement.getAttribute("data-theme"); S.theme = cur === "light" ? "dark" : "light"; localStorage.setItem("rb_theme", S.theme); applyTheme(); }

function installKey(){
  var k = localStorage.getItem(LOCAL_ID);
  if (!k){ var a = new Uint8Array(24); crypto.getRandomValues(a); k = Array.prototype.map.call(a, function(b){ return ("0"+b.toString(16)).slice(-2); }).join(""); localStorage.setItem(LOCAL_ID, k); }
  return k;
}
function platform(){ return (tg && tg.platform ? tg.platform : "web") + " " + (tg && tg.version ? "v"+tg.version : ""); }

function api(method, path, body, extraHeaders){
  var headers = {"Content-Type":"application/json"};
  if (S.token) headers["Authorization"] = "Bearer " + S.token;
  if (extraHeaders) for (var k in extraHeaders) headers[k] = extraHeaders[k];
  return fetch("/api" + path, {method:method, headers:headers, body: body ? JSON.stringify(body) : undefined, credentials:"omit"}).then(function(r){
    return r.json().catch(function(){ return {error:"bad_response"}; }).then(function(j){
      if (r.status === 401 && path !== "/auth"){ S.token = null; return auth().then(function(ok){ if (!ok) throw {error:"unauthenticated", message:"Session expired. Reopen the app."}; return api(method, path, body, extraHeaders); }); }
      if (!r.ok) throw j;
      return j;
    });
  }, function(){ throw {error:"network", message:"Network error. Check your connection and retry."}; });
}

function auth(){
  var initData = tg && tg.initData ? tg.initData : "";
  var dev = !initData && location.search.indexOf("dev=1") >= 0;
  if (!initData && !dev){ screenOpenInTelegram(); return Promise.resolve(false); }
  return api("POST","/auth",{init_data: dev ? "dev" : initData, install_key: installKey(), platform: platform()}).then(function(j){
    S.token = j.token; S.me = j.me; return true;
  }).catch(function(e){ screenOpenInTelegram(e && e.message); return false; });
}

function refreshMe(){ return api("GET","/me").then(function(m){ S.me = m; return m; }); }

/* ---------- screens ---------- */
function clear(){ app.replaceChildren(); window.scrollTo(0,0); }
function header(title, backFn){
  return h("div",{class:"top"},[
    h("div",{class:"row"},[ backFn ? h("button",{class:"iconbtn","aria-label":"Back",onclick:backFn},[svgIcon(IC.back)]) : null, h("h1",{text:title}) ]),
    h("button",{class:"iconbtn","aria-label":"Toggle theme",onclick:toggleTheme},[svgIcon(IC.sun)])
  ]);
}
function svgIcon(p){ var s = svg(p); s.style.width = "22px"; s.style.height = "22px"; s.style.stroke = "currentColor"; s.style.fill = "none"; s.style.strokeWidth = "1.9"; return s; }
function skeleton(n){ var c = h("div"); for (var i=0;i<(n||4);i++) c.appendChild(h("div",{class:"card"},[h("div",{class:"sk",style:{width:"40%"}}),h("div",{class:"sk tall"})])); return c; }
function screenOpenInTelegram(reason){
  nav.classList.add("hidden"); clear();
  app.appendChild(h("div",{class:"center"},[
    h("div",{class:"logo"},[svg(IC.gift)]),
    h("h1",{text:"Open in Telegram"}),
    h("p",{class:"muted",text: reason ? "Sign-in failed: " + reason : "This Mini App only works inside Telegram, where your account is verified securely."}),
    h("button",{class:"btn primary",onclick:function(){ location.reload(); }},["Retry"])
  ]));
}
function screenNotice(next){
  clear(); nav.classList.add("hidden");
  app.appendChild(h("div",{class:"center"},[
    h("div",{class:"logo"},[svg(IC.lock)]),
    h("h1",{text:"Session protection"}),
    h("p",{class:"muted",text:"To protect rewards, this app stores a random identifier in this browser/app so new sessions can be recognised. It is not device tracking: we don't read hardware ids, contacts or apps, and clearing storage simply makes this session look new. You can review and revoke sessions any time under Profile → Security."}),
    h("button",{class:"btn primary",onclick:function(){ localStorage.setItem(NOTICE,"1"); next(); }},["Continue"])
  ]));
}
function screenError(e, retry){
  clear();
  app.appendChild(h("div",{class:"center"},[h("h1",{text:"Something went wrong"}), h("p",{class:"muted",text:(e && e.message) || (e && e.error) || "Unknown error"}), h("button",{class:"btn primary",onclick:retry},["Retry"])]));
}

function renderNav(){
  nav.classList.remove("hidden"); nav.replaceChildren();
  var inner = h("div",{class:"in"});
  [["home","Home",IC.home],["rewards","Rewards",IC.gift],["refer","Refer",IC.users],["activity","Activity",IC.act],["profile","Profile",IC.user]].forEach(function(t){
    inner.appendChild(h("button",{"aria-current": S.tab===t[0] ? "page" : null,"aria-label":t[1],onclick:function(){ go(t[0]); }},[svg(t[2]), h("span",{text:t[1]})]));
  });
  nav.appendChild(inner);
}
function go(tab, view, arg){ S.tab = tab; S.view = view || null; S.arg = arg || null; render(); }

function render(){
  renderNav();
  var m = S.me;
  if (S.tab === "home") return viewHome(m);
  if (S.tab === "rewards") return S.view === "detail" ? viewRewardDetail(S.arg) : viewRewards();
  if (S.tab === "refer") return viewRefer(m);
  if (S.tab === "activity") return viewActivity();
  if (S.tab === "profile"){
    if (S.view === "security") return viewSecurity();
    if (S.view === "top") return viewTop(S.arg || "all");
    if (S.view === "support") return viewSupport();
    if (S.view === "ticket") return viewTicket(S.arg);
    if (S.view === "newticket") return viewNewTicket(S.arg);
    if (S.view === "promo") return viewPromo();
    if (S.view === "text") return viewText(S.arg);
    if (S.view === "approval") return viewApproval();
    return viewProfile(m);
  }
}

function statusPill(v){
  if (v.unavailable) return h("span",{class:"pill warn",text:"Check unavailable"});
  return v.ok ? h("span",{class:"pill ok",text:"Verified"}) : h("span",{class:"pill bad",text:"Action needed"});
}
function primaryButton(m){
  var el = m.eligibility, p = m.primary, label, fn, primary = true;
  if (p === "verify"){ label = "Complete Channel Verification"; fn = doVerify; }
  else if (p === "approve"){ label = "Approve This Session"; fn = function(){ go("profile","approval"); }; }
  else if (p === "bonus"){ label = "Claim Welcome Bonus"; fn = doClaim; }
  else if (p === "referral"){ label = "Claim Available Reward"; fn = doClaim; }
  else if (p === "restock"){ label = "Subscribe to Restock Alerts"; fn = function(){ setWaitlist(true); }; }
  else { label = "Invite More Friends"; fn = function(){ go("refer"); }; primary = false; }
  return h("button",{class:"btn " + (primary ? "primary":""),onclick:fn,disabled:S.busy ? "disabled" : null},[label]);
}
function viewHome(m){
  clear();
  var el = m.eligibility, pct = Math.min(100, Math.round(100 * Math.min(el.balance, el.per) / el.per));
  app.appendChild(header("Hi, " + m.user.name));
  app.appendChild(h("div",{class:"card hero"},[
    h("h2",{text:"Next reward"}),
    h("p",{class:"big",text: el.bonus_available ? "Welcome bonus ready" : el.referral_available ? "Reward ready" : el.need + " referral" + (el.need===1?"":"s") + " to go"}),
    el.bonus_available || el.referral_available ? null : h("div",{class:"progress"},[h("i",{style:{width:pct+"%"}})]),
    h("p",{class:"small muted",text: el.per + " qualified referral" + (el.per===1?"":"s") + " = 1 reward • Credits: " + el.balance + " • Stock: " + el.stock + (el.mode==="shared"?" (shared)":"")}),
    primaryButton(m)
  ]));
  app.appendChild(h("div",{class:"grid2"},[
    h("div",{class:"stat"},[h("b",{text:String(m.referrals.qualified)}),h("span",{class:"small muted",text:"Qualified referrals"})]),
    h("div",{class:"stat"},[h("b",{text:String(m.referrals.pending)}),h("span",{class:"small muted",text:"Pending"})]),
    h("div",{class:"stat"},[h("b",{text:String(m.claims_count)}),h("span",{class:"small muted",text:"Rewards received"})]),
    h("div",{class:"stat"},[h("b",{text:String(el.balance)}),h("span",{class:"small muted",text:"Credits"})])
  ]));
  var v = m.verification;
  var vc = h("div",{class:"card"},[h("div",{class:"row between"},[h("h2",{text:"Channel verification"}), statusPill(v)])]);
  if (v.channels.length === 0) vc.appendChild(h("p",{class:"small muted",text:"No channels are required."}));
  v.channels.forEach(function(c){
    var st = c.state === "member" ? "Joined" : c.state === "requested" ? "Request pending" : c.state === "optional" ? "Optional" : c.state === "unknown" ? "Unknown" : "Not joined";
    vc.appendChild(h("div",{class:"item"},[h("div",[h("div",{class:"t",text:c.title}),h("div",{class:"s",text:st})]), c.link && c.state !== "member" ? h("a",{class:"btn sm",href:c.link,target:"_blank",rel:"noopener"},["Join"]) : h("span",{class:"pill " + (c.state==="member"?"ok":c.state==="optional"?"":"warn"),text:st})]));
  });
  if (v.channels.length) vc.appendChild(h("button",{class:"btn",onclick:doVerify},["Re-check membership"]));
  app.appendChild(vc);
  var sess = h("div",{class:"card"},[h("div",{class:"row between"},[h("h2",{text:"Session"}), h("span",{class:"pill " + (m.approval.required ? "warn":"ok"),text: m.approval.required ? "Approval needed" : "Approved for policy: " + m.approval.policy})]),
    h("p",{class:"small muted",text:"Account authenticated via Telegram. " + (m.approval.required ? "Sensitive actions need a one-tap confirmation in your Telegram chat." : "This installation is recognised.")}),
    m.approval.required ? h("button",{class:"btn",onclick:function(){ go("profile","approval"); }},["Approve this session"]) : null]);
  app.appendChild(sess);
  if (m.checkin.enabled) app.appendChild(h("div",{class:"card"},[h("div",{class:"row between"},[h("div",[h("div",{class:"t",text:"Daily check-in"}),h("div",{class:"small muted",text:"+" + m.checkin.points + " credit(s) per day (" + m.tz + ")"})]), h("button",{class:"btn sm " + (m.checkin.done?"":"primary"),disabled:m.checkin.done?"disabled":null,onclick:doCheckin},[m.checkin.done ? "Done today" : "Check in"])])]));
  if (m.announcements.length){ var ac = h("div",{class:"card"},[h("h2",{text:"Announcements"})]); m.announcements.forEach(function(a){ ac.appendChild(h("div",{class:"ann"},[h("div",{class:"t",text:a.title}),h("div",{class:"small",text:a.body}),h("div",{class:"small muted",text:fmt(a.created_at)})])); }); app.appendChild(ac); }
  app.appendChild(h("p",{class:"small muted center",text:"Bot by " + m.dev.name}));
}

function doVerify(){
  if (S.busy) return; S.busy = true; render();
  api("POST","/verify").then(function(m){ S.me = m; haptic(m.verification.ok ? "ok" : "tap"); toast(m.verification.unavailable ? "Verification temporarily unavailable — try again shortly." : m.verification.ok ? "Verified!" : "Some channels are still missing."); })
    .catch(function(e){ toast(e.message || e.error || "Failed"); }).then(function(){ S.busy = false; render(); });
}
function doClaim(){
  if (S.busy) return; S.busy = true; render();
  var key = "app-" + Date.now() + "-" + Math.random().toString(36).slice(2,10);
  api("POST","/claim",{}, {"Idempotency-Key": key}).then(function(j){ haptic("ok"); return refreshMe().then(function(){ go("rewards","detail", j.claim.claim_ref); toast("Reward unlocked!"); }); })
    .catch(function(e){
      if (e.error === "approval_required"){ toast("Approve this session first."); return refreshMe().then(function(){ go("profile","approval"); }); }
      if (e.error === "out_of_stock"){ toast("Out of stock — subscribe to restock alerts."); return refreshMe().then(render); }
      if (e.error === "not_verified"){ toast("Join the required channels first."); return refreshMe().then(render); }
      toast(e.message || e.error || "Claim failed"); return refreshMe().then(render);
    }).then(function(){ S.busy = false; if (S.tab === "home") render(); });
}
function doCheckin(){ api("POST","/checkin").then(function(j){ haptic("ok"); toast("+" + j.points + " credit(s)!"); return refreshMe().then(render); }).catch(function(e){ toast(e.message || "Already checked in today."); }); }
function setWaitlist(on){ api("POST","/waitlist",{on:on}).then(function(){ toast(on ? "Restock alerts on" : "Restock alerts off"); return refreshMe().then(render); }).catch(function(e){ toast(e.message||"Failed"); }); }

function viewRewards(){
  clear(); app.appendChild(header("My Rewards")); var box = skeleton(3); app.appendChild(box);
  api("GET","/rewards?page=0").then(function(j){
    box.replaceChildren();
    if (!j.items.length) box.appendChild(h("div",{class:"card center"},[h("p",{class:"muted",text:"No rewards yet. Claim your welcome bonus or invite friends."}), h("button",{class:"btn primary",onclick:function(){ go("home"); }},["Go to Home"])]));
    var list = h("div",{class:"list"});
    j.items.forEach(function(c){ list.appendChild(h("button",{class:"item",onclick:function(){ go("rewards","detail",c.claim_ref); }},[h("div",[h("div",{class:"t",text:c.kind_label}),h("div",{class:"s",text:c.code_masked + " • " + fmt(c.claimed_at)})]), h("span",{class:"pill " + (c.delivery_status==="delivered"?"ok":"warn"),text:c.claim_ref})])); });
    box.appendChild(list);
  }).catch(function(e){ box.replaceChildren(); box.appendChild(h("div",{class:"card"},[h("p",{text:e.message||"Failed to load"}),h("button",{class:"btn",onclick:render},["Retry"])])); });
}
function viewRewardDetail(ref){
  clear(); app.appendChild(header("Reward", function(){ go("rewards"); })); var box = skeleton(2); app.appendChild(box);
  api("GET","/rewards/" + encodeURIComponent(ref)).then(function(c){
    box.replaceChildren();
    var card = h("div",{class:"card hero"},[h("h2",{text:c.kind_label}), h("div",{class:"code",text:c.code}),
      h("button",{class:"btn",onclick:function(){ navigator.clipboard && navigator.clipboard.writeText(c.code).then(function(){ toast("Copied"); haptic("tap"); }, function(){ toast("Select the code to copy it"); }); }},["Copy code"]),
      h("p",{class:"small muted",text:"Reference " + c.claim_ref + " • " + fmt(c.claimed_at) + " • Redeem on the partner site/app; this bot only issues codes."})]);
    c.buttons.forEach(function(b){ card.appendChild(h("a",{class:"btn",href:b.url,target:"_blank",rel:"noopener"},[b.text])); });
    box.appendChild(card);
    box.appendChild(h("button",{class:"btn",onclick:function(){ go("profile","newticket",c.claim_ref); }},["Report a problem"]));
  }).catch(function(e){ box.replaceChildren(); box.appendChild(h("div",{class:"card"},[h("p",{text:e.message||"Not found"})])); });
}

function viewRefer(m){
  clear(); app.appendChild(header("Refer & Earn"));
  var shareUrl = "https://t.me/share/url?url=" + encodeURIComponent(m.ref_link) + "&text=" + encodeURIComponent(m.share_text);
  app.appendChild(h("div",{class:"card hero"},[h("h2",{text:"Your link"}), h("div",{class:"code",text:m.ref_link}),
    h("div",{class:"row"},[h("button",{class:"btn",onclick:function(){ navigator.clipboard && navigator.clipboard.writeText(m.ref_link).then(function(){ toast("Link copied"); }); }},["Copy"]),
      h("button",{class:"btn primary",onclick:function(){ if (tg && tg.openTelegramLink) tg.openTelegramLink(shareUrl); else window.open(shareUrl,"_blank"); }},["Share"])])]));
  app.appendChild(h("div",{class:"card"},[h("h2",{text:"How it works"}), h("p",{class:"small",text:"Every " + m.eligibility.per + " qualified referral" + (m.eligibility.per===1?"":"s") + " unlocks one reward. A referral qualifies when your friend starts the bot with your link and completes channel verification. Self-referrals and duplicate accounts don't count."}),
    h("div",{class:"grid2"},[h("div",{class:"stat"},[h("b",{text:String(m.referrals.invited)}),h("span",{class:"small muted",text:"Invited"})]), h("div",{class:"stat"},[h("b",{text:String(m.referrals.qualified)}),h("span",{class:"small muted",text:"Qualified"})])]),
    h("button",{class:"btn",onclick:function(){ go("activity"); }},["View referral activity"])]));
}

function viewActivity(){
  clear(); app.appendChild(header("Activity")); var box = skeleton(4); app.appendChild(box);
  api("GET","/referrals?page=0").then(function(j){
    box.replaceChildren();
    if (!j.items.length) box.appendChild(h("div",{class:"card center"},[h("p",{class:"muted",text:"No referrals yet."}),h("button",{class:"btn primary",onclick:function(){ go("refer"); }},["Get your link"])]));
    var list = h("div",{class:"list"});
    j.items.forEach(function(r){ var cls = r.status==="qualified"?"ok":r.status==="pending"?"warn":"bad"; list.appendChild(h("div",{class:"item"},[h("div",[h("div",{class:"t",text:r.name}),h("div",{class:"s",text:fmt(r.created_at) + (r.reason && r.status!=="qualified" && r.status!=="pending" ? " • " + r.reason : "")})]), h("span",{class:"pill "+cls,text:r.status})])); });
    box.appendChild(list);
    if (j.total > j.items.length) box.appendChild(h("p",{class:"small muted center",text:"Showing " + j.items.length + " of " + j.total + ". Older entries are in the bot chat (/refer → Activity)."}));
  }).catch(function(e){ box.replaceChildren(); box.appendChild(h("div",{class:"card"},[h("p",{text:e.message||"Failed"}),h("button",{class:"btn",onclick:render},["Retry"])])); });
}

function viewProfile(m){
  clear(); app.appendChild(header("Profile"));
  app.appendChild(h("div",{class:"card"},[h("div",{class:"row between"},[h("div",[h("div",{class:"t",text:m.user.name}),h("div",{class:"small muted",text:"ID " + m.user.id + " • member since " + fmt(m.user.joined_at)})]), h("span",{class:"pill",text:"#" + m.rank})])]));
  function item(label, onclick){ return h("button",{class:"item",onclick:onclick},[h("div",{class:"t",text:label}), h("span",{class:"muted",text:"›"})]); }
  app.appendChild(h("div",{class:"list"},[
    item("Security & Sessions", function(){ go("profile","security"); }), item("Leaderboard", function(){ go("profile","top","all"); }),
    item("Redeem promo code", function(){ go("profile","promo"); }), item("Support tickets", function(){ go("profile","support"); }),
    item("Rules", function(){ go("profile","text","rules_text"); }), item("Help", function(){ go("profile","text","help_text"); }), item("Privacy", function(){ go("profile","text","privacy"); })
  ]));
  var prefs = h("div",{class:"card"},[h("h2",{text:"Preferences"})]);
  function sw(label, key, val){ return h("div",{class:"row between",style:{margin:"10px 0"}},[h("span",{text:label}), h("button",{class:"switch",role:"switch","aria-checked":val?"true":"false","aria-label":label,onclick:function(){ var b={}; b[key]=!val; api("POST","/prefs",b).then(refreshMe).then(render).catch(function(e){ toast(e.message||"Failed"); }); }})]); }
  prefs.appendChild(sw("Show my name on the leaderboard","public_name",!!m.user.public_name));
  prefs.appendChild(sw("Notify me about new sessions","notify_new_session",!!m.user.notify_new_session));
  prefs.appendChild(sw("Restock alerts","waitlist",!!m.waitlist));
  prefs.appendChild(h("div",{class:"row between",style:{margin:"10px 0"}},[h("span",{text:"Language"}), h("div",{class:"seg",style:{width:"160px"}},[h("button",{"aria-pressed":m.user.lang==="en"?"true":"false",onclick:function(){ api("POST","/prefs",{lang:"en"}).then(refreshMe).then(render); }},["English"]), h("button",{"aria-pressed":m.user.lang==="hi"?"true":"false",onclick:function(){ api("POST","/prefs",{lang:"hi"}).then(refreshMe).then(render); }},["हिन्दी"])])]));
  app.appendChild(prefs);
  app.appendChild(h("button",{class:"btn danger",onclick:function(){ api("POST","/logout").then(function(){ S.token=null; toast("Signed out"); location.reload(); }); }},["Sign out of this session"]));
  app.appendChild(h("p",{class:"small muted center",text:"Bot by " + m.dev.name + " • @" + m.dev.username}));
}

function viewSecurity(){
  clear(); app.appendChild(header("Security & Sessions", function(){ go("profile"); })); var box = skeleton(3); app.appendChild(box);
  api("GET","/sessions").then(function(j){
    box.replaceChildren();
    box.appendChild(h("div",{class:"card"},[h("h2",{text:"What is verified"}), h("ol",{class:"small"}, j.explain.map(function(t){ return h("li",{text:t.replace(/^\d+\.\s*/,"")}); })), h("p",{class:"small muted",text:"Policy: " + j.policy + ". Approving in Telegram confirms intent from the same account; it is not an independent second factor."})]));
    var ic = h("div",{class:"card"},[h("h2",{text:"Installations"})]);
    if (!j.installations.length) ic.appendChild(h("p",{class:"small muted",text:"None."}));
    j.installations.forEach(function(i){ ic.appendChild(h("div",{class:"item"},[h("div",[h("div",{class:"t",text:i.platform + (i.id===j.current_installation?" (this one)":"")}),h("div",{class:"s",text:"First " + fmt(i.first_seen) + " • Last " + fmt(i.last_seen)})]), h("div",{class:"row"},[h("span",{class:"pill "+(i.approved?"ok":i.status==="pending"?"warn":"bad"),text:i.approved?"approved":i.status}), h("button",{class:"btn sm danger",onclick:function(){ api("POST","/installations/revoke",{id:i.id}).then(function(){ toast("Revoked"); if (i.id===j.current_installation) location.reload(); else render(); }); }},["Revoke"])])])); });
    box.appendChild(ic);
    var sc = h("div",{class:"card"},[h("h2",{text:"Active app sessions (" + j.sessions.length + ")"})]);
    j.sessions.forEach(function(s){ sc.appendChild(h("div",{class:"item"},[h("div",[h("div",{class:"t",text:(s.platform||"web") + (s.current?" (current)":"")}),h("div",{class:"s",text:"Last used " + fmt(s.last_used_at) + " • expires " + fmt(s.expires_at)})]), s.current ? h("span",{class:"pill ok",text:"current"}) : h("button",{class:"btn sm danger",onclick:function(){ api("POST","/sessions/revoke",{id:s.id}).then(render); }},["Revoke"])])); });
    sc.appendChild(h("button",{class:"btn",onclick:function(){ api("POST","/sessions/revoke_others").then(function(r){ toast(r.revoked + " session(s) revoked"); render(); }); }},["Revoke other sessions"]));
    box.appendChild(sc);
    box.appendChild(h("div",{class:"card"},[h("h2",{text:"Recovery"}), h("p",{class:"small muted",text:"New phone or cleared storage? Just open the app again: Telegram signs you in and, depending on the policy, you confirm the new session with one tap in the bot chat. No previously approved session is needed. Denied or expired requests can be re-sent from the approval screen."})]));
  }).catch(function(e){ box.replaceChildren(); box.appendChild(h("div",{class:"card"},[h("p",{text:e.message||"Failed"}),h("button",{class:"btn",onclick:render},["Retry"])])); });
}

function viewApproval(){
  clear(); app.appendChild(header("Approve session", function(){ stopPoll(); go("home"); }));
  var st = h("p",{class:"muted",text:"We sent an approval request to your Telegram chat with this bot. Tap Approve there, then come back."});
  var card = h("div",{class:"card center"},[h("div",{class:"logo"},[svg(IC.lock)]), h("h1",{text:"Confirm in Telegram"}), st,
    h("button",{class:"btn primary",onclick:requestApproval},["Send request again"]),
    h("button",{class:"btn",onclick:function(){ if (tg && tg.close) tg.close(); }},["Open chat (close app)"])]);
  app.appendChild(card);
  stopPoll();
  function poll(){ if (!S.me || !S.me.approval.challenge_id){ return; } api("POST","/approval/status",{challenge_id:S.me.approval.challenge_id}).then(function(j){
    if (j.status === "approved"){ stopPoll(); haptic("ok"); toast("Session approved"); refreshMe().then(function(){ go("home"); }); }
    else if (j.status === "denied"){ stopPoll(); st.textContent = "The request was denied. If that was you, you can send a new request."; }
    else if (j.status === "expired"){ stopPoll(); st.textContent = "The request expired. Send a new one."; }
    else S.pollTimer = setTimeout(poll, 4000);
  }).catch(function(){ S.pollTimer = setTimeout(poll, 6000); }); }
  if (!S.me.approval.challenge_id) requestApproval(); else poll();
  function requestApproval(){ api("POST","/approval/request").then(function(j){ S.me.approval.challenge_id = j.challenge_id; st.textContent = "Request sent (expires " + fmt(j.expires_at) + "). Tap Approve in the bot chat."; stopPoll(); S.pollTimer = setTimeout(poll, 3000); }).catch(function(e){ st.textContent = e.message || "Could not send the request."; }); }
}
function stopPoll(){ if (S.pollTimer){ clearTimeout(S.pollTimer); S.pollTimer = null; } }

function viewTop(period){
  clear(); app.appendChild(header("Leaderboard", function(){ go("profile"); }));
  var seg = h("div",{class:"seg"}, ["all","week","month"].map(function(p){ return h("button",{"aria-pressed":p===period?"true":"false",onclick:function(){ go("profile","top",p); }},[p==="all"?"All time":p==="week"?"This week":"This month"]); }));
  app.appendChild(h("div",{class:"card"},[seg])); var box = skeleton(3); app.appendChild(box);
  api("GET","/leaderboard?period=" + period).then(function(j){
    box.replaceChildren(); var list = h("div",{class:"list"});
    if (!j.items.length) list.appendChild(h("p",{class:"muted center",text:"No qualified referrals in this period."}));
    j.items.forEach(function(r,i){ list.appendChild(h("div",{class:"item"},[h("div",{class:"row"},[h("span",{class:"medal",text:i<3?["🥇","🥈","🥉"][i]:String(i+1)}), h("span",{class:"t",text:r.name})]), h("span",{class:"pill",text:r.count + " referrals"})])); });
    box.appendChild(list); box.appendChild(h("p",{class:"small muted center",text:"You: #" + j.me.rank + " with " + j.me.count + " • periods follow " + j.tz}));
  }).catch(function(e){ box.replaceChildren(); box.appendChild(h("p",{text:e.message||"Failed"})); });
}

function viewPromo(){
  clear(); app.appendChild(header("Promo code", function(){ go("profile"); }));
  var inp = h("input",{placeholder:"Enter code","aria-label":"Promo code",autocapitalize:"characters"});
  app.appendChild(h("div",{class:"card"},[h("label",{text:"Code"}), inp, h("div",{style:{height:"10px"}}), h("button",{class:"btn primary",onclick:function(){ if (!inp.value.trim()) return; api("POST","/promo",{code:inp.value.trim()}).then(function(j){ haptic("ok"); toast("+" + j.points + " credit(s)! Balance " + j.balance); refreshMe().then(function(){ go("home"); }); }).catch(function(e){ toast(e.message || "Invalid code"); }); }},["Redeem"])]));
}
function viewText(key){
  clear(); app.appendChild(header(key==="rules_text"?"Rules":key==="help_text"?"Help":"Privacy", function(){ go("profile"); })); var box = skeleton(2); app.appendChild(box);
  api("GET","/content?key=" + key).then(function(j){ box.replaceChildren(); var c = h("div",{class:"card"}); j.text.split("\n").forEach(function(line){ c.appendChild(h("p",{class: line ? "" : "small", text: line || " "})); }); box.appendChild(c); }).catch(function(e){ box.replaceChildren(); box.appendChild(h("p",{text:e.message||"Failed"})); });
}

function viewSupport(){
  clear(); app.appendChild(header("Support", function(){ go("profile"); })); var box = skeleton(2); app.appendChild(box);
  api("GET","/support").then(function(j){
    box.replaceChildren();
    box.appendChild(h("button",{class:"btn primary",onclick:function(){ go("profile","newticket"); }},["New ticket"]));
    box.appendChild(h("div",{style:{height:"10px"}}));
    if (!j.tickets.length) box.appendChild(h("p",{class:"muted center",text:"No tickets yet."}));
    var list = h("div",{class:"list"});
    j.tickets.forEach(function(t){ list.appendChild(h("button",{class:"item",onclick:function(){ go("profile","ticket",t.id); }},[h("div",[h("div",{class:"t",text:"#" + t.id + " " + t.subject}),h("div",{class:"s",text:fmt(t.updated_at)})]), h("span",{class:"pill " + (t.status==="open"?"warn":"ok"),text:t.status})])); });
    box.appendChild(list);
  }).catch(function(e){ box.replaceChildren(); box.appendChild(h("p",{text:e.message||"Failed"})); });
}
function viewNewTicket(claimRef){
  clear(); app.appendChild(header("New ticket", function(){ go("profile","support"); }));
  var subj = h("input",{placeholder:"Short subject","aria-label":"Subject",maxlength:"80",value: claimRef ? "Problem with " + claimRef : ""});
  var body = h("textarea",{placeholder:"Describe the issue…","aria-label":"Message",maxlength:"2000"});
  app.appendChild(h("div",{class:"card"},[h("label",{text:"Subject"}), subj, h("label",{text:"Message"}), body, h("div",{style:{height:"10px"}}),
    h("button",{class:"btn primary",onclick:function(){ if (!subj.value.trim() || body.value.trim().length < 5){ toast("Please fill in subject and message."); return; } api("POST","/support",{subject:subj.value.trim(),body:body.value.trim(),claim_ref:claimRef||null}).then(function(j){ toast("Ticket #" + j.id + " created"); go("profile","ticket",j.id); }).catch(function(e){ toast(e.message||"Failed"); }); }},["Send"])]));
}
function viewTicket(id){
  clear(); app.appendChild(header("Ticket #" + id, function(){ go("profile","support"); })); var box = skeleton(3); app.appendChild(box);
  api("GET","/support/" + id).then(function(j){
    box.replaceChildren();
    var c = h("div",{class:"card"},[h("div",{class:"row between"},[h("div",{class:"t",text:j.ticket.subject}), h("span",{class:"pill " + (j.ticket.status==="open"?"warn":"ok"),text:j.ticket.status})])]);
    j.messages.forEach(function(m){ c.appendChild(h("div",{class:"msg " + (m.is_staff?"staff":"")},[h("div",{text:m.body}), h("div",{class:"small muted",text:(m.is_staff?"Support • ":"You • ") + fmt(m.created_at)})])); });
    var ta = h("textarea",{placeholder:"Write a reply…","aria-label":"Reply",maxlength:"2000"});
    c.appendChild(ta); c.appendChild(h("div",{style:{height:"8px"}}));
    c.appendChild(h("button",{class:"btn primary",onclick:function(){ if (ta.value.trim().length < 2) return; api("POST","/support/" + id + "/message",{body:ta.value.trim()}).then(render).catch(function(e){ toast(e.message||"Failed"); }); }},["Send"]));
    box.appendChild(c);
  }).catch(function(e){ box.replaceChildren(); box.appendChild(h("p",{text:e.message||"Failed"})); });
}

/* ---------- boot ---------- */
function boot(){
  applyTheme();
  if (tg){ try { tg.ready(); tg.expand(); tg.onEvent("themeChanged", applyTheme); tg.onEvent("viewportChanged", function(){}); } catch(e){} }
  clear(); app.appendChild(skeleton(3));
  auth().then(function(ok){ if (!ok) return; render(); });
}
if (!localStorage.getItem(NOTICE) && (tg && tg.initData || location.search.indexOf("dev=1")>=0)) screenNotice(boot); else boot();
document.addEventListener("visibilitychange", function(){ if (!document.hidden && S.token && S.tab === "home") refreshMe().then(render).catch(function(){}); });
})();
</script>
</body>
</html>"""


# ==== 17. HTTP API (FastAPI) ==================================================================

from types import SimpleNamespace  # noqa: E402

api = FastAPI(title="Reward Bot", docs_url=None, redoc_url=None, openapi_url=None)


def json_error(code: str, status: int = 400, message: Optional[str] = None, **extra: Any) -> JSONResponse:
    return JSONResponse({"error": code, "message": message or code.replace("_", " ").capitalize(), **extra}, status_code=status)


def strip_html(s: str) -> str:
    s = re.sub(r"<br\s*/?>", "\n", s or "")
    return html.unescape(re.sub(r"<[^>]+>", "", s))


def origin_ok(request: Request) -> bool:
    origin = request.headers.get("origin")
    if not CFG.public_base_url:
        return True
    if not origin:
        return not CFG.production
    a, b = urllib.parse.urlsplit(origin), urllib.parse.urlsplit(CFG.public_base_url)
    return (a.scheme, a.netloc.lower()) == (b.scheme, b.netloc.lower())


def client_key(request: Request) -> str:
    if CFG.trusted_proxy == "xff":
        xff = request.headers.get("x-forwarded-for", "")
        if xff:
            return xff.split(",")[-1].strip()[:64]  # nearest hop appended by the trusted proxy
    return request.client.host if request.client else "unknown"


@api.middleware("http")
async def security_middleware(request: Request, call_next: Callable) -> Response:
    path = request.url.path
    if path.startswith("/api/") and request.method in ("POST", "PUT", "PATCH", "DELETE") and not origin_ok(request):
        return json_error("bad_origin", 403, "Cross-origin requests are not allowed.")
    resp: Response = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=()"
    if path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    if CFG.production:
        resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return resp


@api.exception_handler(AuthError)
async def _auth_error(request: Request, exc: AuthError) -> JSONResponse:
    return json_error(exc.code, exc.status, {"init_data_expired": "Sign-in data expired. Reopen the Mini App.",
                                             "init_data_bad_signature": "Sign-in data is not valid."}.get(exc.code))


@api.exception_handler(PermissionDenied)
async def _perm_error(request: Request, exc: PermissionDenied) -> JSONResponse:
    return json_error("forbidden", 403, "You are not allowed to do that.")


@api.exception_handler(Exception)
async def _any_error(request: Request, exc: Exception) -> JSONResponse:
    log.exception("api error on %s", request.url.path, exc_info=exc)
    return json_error("internal_error", 500, "Something went wrong. Please try again.")


async def require_session(request: Request) -> Tuple[sqlite3.Row, sqlite3.Row]:
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    sess = session_from_token(token)
    if not sess:
        raise AuthError("unauthenticated", 401)
    u = get_user(int(sess["user_id"]))
    if not u:
        raise AuthError("unauthenticated", 401)
    if not RL.allow(f"api:{u['user_id']}", 240, 60):
        raise AuthError("rate_limited", 429)
    return sess, u


def bot_ref() -> Any:
    if not RT.application:
        raise RuntimeError("bot not started")
    return RT.application.bot


def current_installation(sess: sqlite3.Row) -> Optional[sqlite3.Row]:
    return DB.one("SELECT * FROM installations WHERE id=?", (sess["installation_id"],)) if sess["installation_id"] else None


def pending_session_challenge(uid: int, inst: Optional[sqlite3.Row]) -> Optional[sqlite3.Row]:
    return DB.one("SELECT * FROM challenges WHERE user_id=? AND action='session' AND status='pending' AND expires_at>? AND COALESCE(installation_id,'')=? ORDER BY created_at DESC LIMIT 1",
                  (uid, now(), inst["id"] if inst else ""))


async def me_payload(u: sqlite3.Row, sess: sqlite3.Row, force_check: bool = False) -> Dict[str, Any]:
    uid = int(u["user_id"])
    bot = bot_ref()
    vr = await check_membership(bot, uid, force=force_check)
    if vr.ok:
        info = qualify_referral(uid) if int(u["verified"] or 0) == 0 or DB.one("SELECT 1 FROM referrals WHERE referee_id=? AND status='pending'", (uid,)) else None
        if int(u["verified"] or 0) == 0:
            DB.run("UPDATE users SET verified=1 WHERE user_id=?", (uid,))
            activity(uid, "verified")
        if info:
            await notify_referrer(bot, info)
        u = get_user(uid)  # type: ignore[assignment]
    chans = []
    for ch in required_channels():
        st = vr.states.get(int(ch["chat_id"]), "member" if vr.ok and not vr.states else "missing")
        link = ch["invite_link"] or (await ensure_invite_link(bot, ch) if st != "member" else None)
        chans.append({"id": int(ch["chat_id"]), "title": ch["title"], "state": st, "link": link})
    el = eligibility(u)
    inst = current_installation(sess)
    needs = approval_needed(uid, inst)
    ch_row = pending_session_challenge(uid, inst) if needs else None
    st = referral_stats(uid)
    lang = user_lang(u)
    if not vr.ok:
        primary = "verify"
    elif needs:
        primary = "approve"
    elif el["bonus_available"]:
        primary = "bonus"
    elif el["referral_available"]:
        primary = "referral"
    elif el["stock"] == 0 and not waitlist_on(uid):
        primary = "restock"
    else:
        primary = "invite"
    rank, _ = my_rank(uid)
    return {
        "user": {"id": uid, "name": u["first_name"] or "User", "lang": lang, "public_name": int(u["public_name"] or 1), "notify_new_session": int(u["notify_new_session"] or 1),
                 "joined_at": u["joined_at"], "suspended": bool(u["suspended"]), "suspended_reason": u["suspended_reason"], "staff": is_staff(uid)},
        "verification": {"ok": vr.ok, "unavailable": vr.unavailable, "channels": chans},
        "eligibility": el, "referrals": st, "claims_count": int(u["claims"] or 0), "rank": rank,
        "approval": {"required": needs, "policy": effective_policy(), "challenge_id": ch_row["id"] if ch_row else None, "installation_id": inst["id"] if inst else None},
        "announcements": [dict(a) for a in DB.all("SELECT id,title,body,created_at FROM announcements WHERE enabled=1 ORDER BY id DESC LIMIT 3")],
        "waitlist": waitlist_on(uid), "checkin": {"enabled": gi("checkin_points", 0) > 0, "points": gi("checkin_points", 0), "done": checked_in_today(uid)},
        "ref_link": ref_link(uid, u["campaign"] or ""), "share_text": strip_html(gs("share_text")), "dev": {"name": CFG.dev_name, "username": CFG.dev_username},
        "primary": primary, "tz": CFG.report_tz, "maintenance": maintenance_on(), "version": SCHEMA_VERSION,
    }


class AuthBody(BaseModel):
    init_data: str = Field(min_length=1, max_length=8192)
    install_key: str = Field(min_length=16, max_length=128)
    platform: str = Field(default="unknown", max_length=64)


class IdBody(BaseModel):
    id: str = Field(min_length=1, max_length=64)


class ChallengeBody(BaseModel):
    challenge_id: str = Field(min_length=1, max_length=64)


class OnBody(BaseModel):
    on: bool


class PromoBody(BaseModel):
    code: str = Field(min_length=1, max_length=64)


class PrefsBody(BaseModel):
    lang: Optional[str] = None
    public_name: Optional[bool] = None
    notify_new_session: Optional[bool] = None
    waitlist: Optional[bool] = None


class TicketBody(BaseModel):
    subject: str = Field(min_length=1, max_length=80)
    body: str = Field(min_length=5, max_length=2000)
    claim_ref: Optional[str] = Field(default=None, max_length=32)


class MessageBody(BaseModel):
    body: str = Field(min_length=1, max_length=2000)


@api.get("/", include_in_schema=False)
async def root() -> Response:
    return PlainTextResponse("ok")


@api.get("/healthz")
async def healthz() -> JSONResponse:
    rep = health_report()
    return JSONResponse(rep, status_code=200 if rep["ok"] else 503)


@api.get("/app", response_class=HTMLResponse)
@api.get("/app/", response_class=HTMLResponse, include_in_schema=False)
async def miniapp() -> HTMLResponse:
    nonce = secrets.token_urlsafe(16)
    csp = (f"default-src 'none'; script-src 'nonce-{nonce}' https://telegram.org; style-src 'nonce-{nonce}'; connect-src 'self'; "
           "img-src 'self' data: https:; font-src 'self'; base-uri 'none'; form-action 'none'; "
           "frame-ancestors https://web.telegram.org https://*.telegram.org https://*.t.me; object-src 'none'")
    resp = HTMLResponse(MINIAPP_HTML.replace("__NONCE__", nonce))
    resp.headers["Content-Security-Policy"] = csp
    resp.headers["Cache-Control"] = "no-store"
    return resp


@api.post("/api/auth")
async def api_auth(body: AuthBody, request: Request) -> JSONResponse:
    if not RL.allow(f"auth:{client_key(request)}", 60, 60):
        return json_error("rate_limited", 429, "Too many sign-in attempts. Try again in a minute.")
    if body.init_data == "dev":
        if CFG.production or not CFG.dev_auth_bypass:
            raise AuthError("init_data_bad_signature")
        tg_user: Any = SimpleNamespace(id=int(CFG.dev_auth_bypass), first_name="Dev User", username="dev")
        uid = int(CFG.dev_auth_bypass)
        start_param = ""
    else:
        data = validate_init_data(body.init_data, CFG.bot_token, CFG.init_data_max_age)
        uid = data["user_id"]
        tg_user = SimpleNamespace(id=uid, first_name=str(data["user"].get("first_name") or "User")[:64], username=data["user"].get("username"))
        start_param = data.get("start_param", "")
    ref = None
    m = re.fullmatch(r"(\d{3,15})(?:_([A-Za-z0-9-]{1,24}))?", start_param or "")
    if m:
        ref = int(m.group(1))
    u, created = ensure_user(tg_user, ref_by=ref, campaign=m.group(2) if m else None)
    if maintenance_on() and not is_staff(uid):
        return json_error("maintenance", 503, strip_html(gs("maintenance_text")))
    inst, new_inst = register_installation(uid, body.install_key, body.platform)
    token, sess = create_session(uid, inst["id"], body.platform[:32])
    audit(uid, "session.create", sess["id"], after={"platform": body.platform[:32], "new_installation": new_inst})
    if new_inst and not created:
        policy = effective_policy()
        if policy == "notify" and int(u["notify_new_session"] or 1) == 1:
            enqueue_job("session_notify", {"user_id": uid, "platform": body.platform[:32]}, idem_key=f"sessnotify:{inst['id']}")
    if approval_needed(uid, inst) and not pending_session_challenge(uid, inst):
        ch = create_challenge(uid, "session", inst["id"], {"platform": body.platform[:32]})
        await send_challenge(bot_ref(), ch)
    me = await me_payload(u, sess)
    return JSONResponse({"token": token, "expires_at": sess["expires_at"], "me": me})


@api.get("/api/me")
async def api_me(request: Request) -> JSONResponse:
    sess, u = await require_session(request)
    return JSONResponse(await me_payload(u, sess))


@api.post("/api/verify")
async def api_verify(request: Request) -> JSONResponse:
    sess, u = await require_session(request)
    if not RL.allow(f"verify:{u['user_id']}", 12, 60):
        return json_error("rate_limited", 429, "Please wait a moment before re-checking.")
    return JSONResponse(await me_payload(u, sess, force_check=True))


@api.post("/api/claim")
async def api_claim(request: Request) -> JSONResponse:
    sess, u = await require_session(request)
    uid = int(u["user_id"])
    idem = request.headers.get("idempotency-key", "")
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{8,80}", idem):
        return json_error("missing_idempotency_key", 400, "Idempotency-Key header is required.")
    if not RL.allow(f"claim:{uid}", 10, 60):
        return json_error("rate_limited", 429, "Too many attempts.")
    if is_suspended(u):
        return json_error("suspended", 403, "Your account is suspended.")
    if maintenance_on() and not is_staff(uid):
        return json_error("maintenance", 503)
    vr = await check_membership(bot_ref(), uid, force=True)
    if vr.unavailable:
        return json_error("verification_unavailable", 503, "Verification is temporarily unavailable. Try again shortly.")
    if not vr.ok:
        return json_error("not_verified", 403, "Join the required channels first.")
    info = qualify_referral(uid)
    if int(u["verified"] or 0) == 0:
        DB.run("UPDATE users SET verified=1 WHERE user_id=?", (uid,))
    if info:
        await notify_referrer(bot_ref(), info)
    inst = current_installation(sess)
    if approval_needed(uid, inst):
        if not pending_session_challenge(uid, inst):
            ch = create_challenge(uid, "session", inst["id"] if inst else None)
            await send_challenge(bot_ref(), ch)
        return json_error("approval_required", 403, "Approve this session in your Telegram chat first.")
    el = eligibility(get_user(uid))  # type: ignore[arg-type]
    kind = "bonus" if el["bonus_available"] else ("referral" if el["referral_available"] else None)
    if not kind:
        return json_error("not_eligible", 409, f"{el['need']} more referral(s) needed.", need=el["need"])
    try:
        res = perform_claim(uid, kind, f"app:{uid}:{idem}")
    except ClaimError as e:
        status = {"out_of_stock": 409, "insufficient": 409, "bonus_unavailable": 409, "suspended": 403}.get(e.code, 400)
        return json_error(e.code, status, {"out_of_stock": "Rewards are out of stock right now.", "insufficient": "Not enough referrals yet."}.get(e.code), **e.info)
    c = claim_by_ref(res["claim_ref"], uid)
    activity(uid, "claim.app", {"ref": res["claim_ref"]})
    if not res["duplicate"]:
        await send_log(bot_ref(), f"🎁 <b>Claim (app)</b> <code>{uid}</code> {c['kind']} • {esc(c['claim_ref'])} • stock {stock_label()}")
    return JSONResponse({"claim": {"claim_ref": c["claim_ref"], "code": c["code"], "kind": c["kind"], "kind_label": CLAIM_KINDS.get(c["kind"], c["kind"]), "claimed_at": c["claimed_at"]}})


@api.get("/api/rewards")
async def api_rewards(request: Request, page: int = 0) -> JSONResponse:
    sess, u = await require_session(request)
    rows, total = claims_page(int(u["user_id"]), max(0, page), 20)
    return JSONResponse({"items": [{"claim_ref": r["claim_ref"], "kind": r["kind"], "kind_label": CLAIM_KINDS.get(r["kind"], r["kind"]), "code_masked": mask_code(r["code"]),
                                    "claimed_at": r["claimed_at"], "delivery_status": r["delivery_status"]} for r in rows], "total": total})


@api.get("/api/rewards/{ref}")
async def api_reward(ref: str, request: Request) -> JSONResponse:
    sess, u = await require_session(request)
    c = claim_by_ref(ref[:32], int(u["user_id"]))
    if not c:
        return json_error("not_found", 404)
    return JSONResponse({"claim_ref": c["claim_ref"], "code": c["code"], "kind": c["kind"], "kind_label": CLAIM_KINDS.get(c["kind"], c["kind"]), "claimed_at": c["claimed_at"],
                         "delivery_status": c["delivery_status"], "buttons": [{"text": b["text"], "url": b["url"]} for b in parse_buttons(gs("reward_buttons"))]})


@api.get("/api/referrals")
async def api_referrals(request: Request, page: int = 0) -> JSONResponse:
    sess, u = await require_session(request)
    items, total = referral_page(int(u["user_id"]), max(0, page), 30)
    return JSONResponse({"items": items, "total": total, "stats": referral_stats(int(u["user_id"]))})


@api.get("/api/leaderboard")
async def api_leaderboard(request: Request, period: str = "all") -> JSONResponse:
    sess, u = await require_session(request)
    if gi("leaderboard_enabled", 1) != 1:
        return json_error("disabled", 404, "The leaderboard is disabled.")
    period = period if period in ("all", "week", "month") else "all"
    rank, mine = my_rank(int(u["user_id"]), period)
    return JSONResponse({"items": leaderboard(period, 20), "me": {"rank": rank, "count": mine}, "period": period, "tz": CFG.report_tz})


@api.get("/api/sessions")
async def api_sessions(request: Request) -> JSONResponse:
    sess, u = await require_session(request)
    ov = sessions_overview(int(u["user_id"]), sess["id"])
    ov["current_installation"] = sess["installation_id"]
    return JSONResponse(ov)


@api.post("/api/sessions/revoke")
async def api_session_revoke(body: IdBody, request: Request) -> JSONResponse:
    sess, u = await require_session(request)
    return JSONResponse({"ok": revoke_session(int(u["user_id"]), body.id)})


@api.post("/api/sessions/revoke_others")
async def api_session_revoke_others(request: Request) -> JSONResponse:
    sess, u = await require_session(request)
    return JSONResponse({"revoked": revoke_other_sessions(int(u["user_id"]), sess["id"])})


@api.post("/api/installations/revoke")
async def api_inst_revoke(body: IdBody, request: Request) -> JSONResponse:
    sess, u = await require_session(request)
    return JSONResponse({"ok": revoke_installation(int(u["user_id"]), body.id)})


@api.post("/api/approval/status")
async def api_approval_status(body: ChallengeBody, request: Request) -> JSONResponse:
    sess, u = await require_session(request)
    ch = DB.one("SELECT status, expires_at FROM challenges WHERE id=? AND user_id=?", (body.challenge_id, u["user_id"]))
    if not ch:
        return json_error("not_found", 404)
    status = ch["status"] if not (ch["status"] == "pending" and int(ch["expires_at"]) <= now()) else "expired"
    return JSONResponse({"status": status, "approved": status == "approved" and not approval_needed(int(u["user_id"]), current_installation(sess))})


@api.post("/api/approval/request")
async def api_approval_request(request: Request) -> JSONResponse:
    sess, u = await require_session(request)
    uid = int(u["user_id"])
    if not RL.allow(f"chal:{uid}", 5, 600):
        return json_error("rate_limited", 429, "Too many requests. Wait a few minutes.")
    inst = current_installation(sess)
    if not approval_needed(uid, inst):
        return JSONResponse({"challenge_id": None, "status": "approved", "expires_at": None})
    with DB.tx() as c:
        c.execute("UPDATE challenges SET status='expired' WHERE user_id=? AND action='session' AND status='pending'", (uid,))
    ch = create_challenge(uid, "session", inst["id"] if inst else None)
    ok = await send_challenge(bot_ref(), ch)
    if not ok:
        return json_error("delivery_failed", 502, "Could not message you in Telegram. Open the bot chat and press Start, then retry.")
    return JSONResponse({"challenge_id": ch["id"], "status": "pending", "expires_at": ch["expires_at"]})


@api.post("/api/waitlist")
async def api_waitlist(body: OnBody, request: Request) -> JSONResponse:
    sess, u = await require_session(request)
    waitlist_set(int(u["user_id"]), body.on)
    return JSONResponse({"on": body.on})


@api.post("/api/promo")
async def api_promo(body: PromoBody, request: Request) -> JSONResponse:
    sess, u = await require_session(request)
    if not RL.allow(f"promo:{u['user_id']}", 10, 600):
        return json_error("rate_limited", 429)
    if is_suspended(u):
        return json_error("suspended", 403)
    r = redeem_promo(int(u["user_id"]), body.code)
    if not r["ok"]:
        return json_error(r["error"], 409, {"invalid": "Invalid promo code.", "expired": "This promo code has expired.", "exhausted": "This promo code is fully redeemed.", "already_used": "You already used this code."}[r["error"]])
    return JSONResponse(r)


@api.post("/api/checkin")
async def api_checkin(request: Request) -> JSONResponse:
    sess, u = await require_session(request)
    r = do_checkin(int(u["user_id"]))
    if not r["ok"]:
        return json_error(r["error"], 409, "Already checked in today." if r["error"] == "already" else "Check-ins are disabled.")
    return JSONResponse(r)


@api.post("/api/prefs")
async def api_prefs(body: PrefsBody, request: Request) -> JSONResponse:
    sess, u = await require_session(request)
    uid = int(u["user_id"])
    if body.lang is not None:
        if body.lang not in I18N:
            return json_error("bad_lang", 400)
        DB.run("UPDATE users SET lang=? WHERE user_id=?", (body.lang, uid))
    if body.public_name is not None:
        DB.run("UPDATE users SET public_name=? WHERE user_id=?", (1 if body.public_name else 0, uid))
    if body.notify_new_session is not None:
        DB.run("UPDATE users SET notify_new_session=? WHERE user_id=?", (1 if body.notify_new_session else 0, uid))
    if body.waitlist is not None:
        waitlist_set(uid, body.waitlist)
    return JSONResponse({"ok": True})


@api.get("/api/content")
async def api_content(request: Request, key: str = "rules_text") -> JSONResponse:
    sess, u = await require_session(request)
    if key == "privacy":
        return JSONResponse({"key": key, "text": strip_html(PRIVACY_TEXT.replace("{days}", str(CFG.retention_days)))})
    if key not in ("rules_text", "help_text", "referral_text"):
        return json_error("not_found", 404)
    return JSONResponse({"key": key, "text": strip_html(render(key, u))})


@api.get("/api/support")
async def api_support(request: Request) -> JSONResponse:
    sess, u = await require_session(request)
    rows = DB.all("SELECT id, subject, status, updated_at, claim_ref FROM support_tickets WHERE user_id=? ORDER BY id DESC LIMIT 20", (u["user_id"],))
    return JSONResponse({"tickets": [dict(r) for r in rows], "enabled": gi("support_enabled", 1) == 1, "contact": gs("support_contact")})


@api.post("/api/support")
async def api_support_new(body: TicketBody, request: Request) -> JSONResponse:
    sess, u = await require_session(request)
    if gi("support_enabled", 1) != 1:
        return json_error("disabled", 403, "Support is currently closed.")
    ref = body.claim_ref if body.claim_ref and claim_by_ref(body.claim_ref, int(u["user_id"])) else None
    tid = create_ticket(int(u["user_id"]), body.subject, body.body, "reward" if ref else "other", ref)
    if not tid:
        return json_error("rate_limited", 429, "Too many tickets in the last hour.")
    return JSONResponse({"id": tid})


@api.get("/api/support/{tid}")
async def api_ticket(tid: int, request: Request) -> JSONResponse:
    sess, u = await require_session(request)
    t = DB.one("SELECT id, subject, status, claim_ref, created_at, updated_at FROM support_tickets WHERE id=? AND user_id=?", (tid, u["user_id"]))
    if not t:
        return json_error("not_found", 404)
    msgs = DB.all("SELECT id, is_staff, body, created_at FROM support_messages WHERE ticket_id=? AND internal=0 ORDER BY id", (tid,))
    return JSONResponse({"ticket": dict(t), "messages": [dict(m) for m in msgs]})


@api.post("/api/support/{tid}/message")
async def api_ticket_msg(tid: int, body: MessageBody, request: Request) -> JSONResponse:
    sess, u = await require_session(request)
    if not add_ticket_message(tid, int(u["user_id"]), body.body):
        return json_error("not_allowed", 403)
    return JSONResponse({"ok": True})


@api.post("/api/logout")
async def api_logout(request: Request) -> JSONResponse:
    sess, u = await require_session(request)
    revoke_session(int(u["user_id"]), sess["id"])
    return JSONResponse({"ok": True})


@api.post("/telegram/webhook/{secret}")
async def telegram_webhook(secret: str, request: Request) -> Response:
    if CFG.bot_mode != "webhook" or not RT.application:
        return PlainTextResponse("not found", status_code=404)
    header = request.headers.get("x-telegram-bot-api-secret-token", "")
    if not (hmac.compare_digest(secret, CFG.webhook_secret) and hmac.compare_digest(header, CFG.webhook_secret)):
        return PlainTextResponse("forbidden", status_code=403)
    data = await request.json()
    await RT.application.update_queue.put(Update.de_json(data, RT.application.bot))
    return PlainTextResponse("ok")


# ==== 18. WORKERS =============================================================================

async def run_job(bot: Any, job: sqlite3.Row) -> None:
    kind = job["kind"]
    p = json.loads(job["payload"] or "{}")
    try:
        if kind == "deliver_claim":
            c = claim_by_ref(p["claim_ref"])
            if not c:
                return finish_job(job["id"], True)
            if c["delivery_status"] == "delivered" and c["kind"] != "gift" and not p.get("force"):
                return finish_job(job["id"], True)
            u = get_user(int(c["user_id"]))
            if not u:
                return finish_job(job["id"], True)
            head = {"gift": "🎁 <b>You received a gift reward!</b>", "bonus": "🎁 <b>Your welcome bonus</b>", "referral": "🎉 <b>Your referral reward</b>"}.get(c["kind"], "🎁 <b>Your reward</b>")
            txt = (f"{head}\n\n<code>{esc(c['code'])}</code>\n\n🧾 Reference: <code>{esc(c['claim_ref'])}</code>\n📅 {fmt_ts(c['claimed_at'])}" + footer())
            await bot.send_message(int(c["user_id"]), txt, parse_mode=ParseMode.HTML,
                                   reply_markup=buttons_markup(parse_buttons(gs("reward_buttons")), [[B("🎟 My Rewards", "u:rewards:0"), B("🏠 Home", "u:menu")]]))
            mark_delivery(c["claim_ref"], True)
            return finish_job(job["id"], True)
        if kind == "restock":
            u = get_user(int(p["user_id"]))
            if not u or u["suspended"] or stock_count(p.get("category", "default")) <= 0:
                return finish_job(job["id"], True)
            await bot.send_message(int(u["user_id"]), render("restock_text", u) + footer(), parse_mode=ParseMode.HTML,
                                   reply_markup=KB([B(T(user_lang(u), "claim_reward"), "u:claim")], [B("🔕 Stop alerts", "u:waitlist")]))
            return finish_job(job["id"], True)
        if kind == "admin_alert":
            for a in alert_recipients():
                with contextlib.suppress(TelegramError):
                    await bot.send_message(a, p["text"], parse_mode=ParseMode.HTML)
            return finish_job(job["id"], True)
        if kind == "session_notify":
            await bot.send_message(int(p["user_id"]), f"🔔 <b>New Mini App session</b>\n\nClient: <b>{esc(p.get('platform', 'unknown'))}</b> • {fmt_ts(now())}\n"
                                   "If this was not you, open Security & Sessions and revoke it.", parse_mode=ParseMode.HTML,
                                   reply_markup=KB([B("🔐 Security & Sessions", "u:security")]))
            return finish_job(job["id"], True)
        return finish_job(job["id"], False, f"unknown job kind {kind}")
    except Forbidden:
        uid = p.get("user_id")
        if kind == "deliver_claim":
            c = claim_by_ref(p["claim_ref"])
            uid = int(c["user_id"]) if c else None
            mark_delivery(p["claim_ref"], False, "user blocked the bot")
        if uid:
            DB.run("UPDATE users SET unreachable_at=? WHERE user_id=?", (now(), uid))
        finish_job(job["id"], False, "forbidden", retry_in=None)
    except RetryAfter as e:
        finish_job(job["id"], False, "flood", retry_in=int(e.retry_after) + 1)
    except (TimedOut, NetworkError) as e:
        finish_job(job["id"], False, str(e), retry_in=30)
    except TelegramError as e:
        if kind == "deliver_claim":
            mark_delivery(p["claim_ref"], False, str(e))
        finish_job(job["id"], False, str(e), retry_in=300)


async def job_worker(bot: Any, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            jobs = claim_due_jobs(20)
            for j in jobs:
                await run_job(bot, j)
                await asyncio.sleep(0.05)
            if not jobs:
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=2.0)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("job worker error")
            await asyncio.sleep(3)


async def broadcast_worker(bot: Any, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            ts = now()
            DB.run("UPDATE broadcasts SET status='queued' WHERE status='scheduled' AND scheduled_at<=?", (ts,))
            b = DB.one("SELECT * FROM broadcasts WHERE status IN ('queued','running') ORDER BY id LIMIT 1")
            if not b:
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=3.0)
                continue
            if b["status"] == "queued":
                DB.run("UPDATE broadcasts SET status='running', started_at=COALESCE(started_at,?) WHERE id=?", (ts, b["id"]))
            targets = DB.all("SELECT user_id FROM broadcast_targets WHERE broadcast_id=? AND status='pending' ORDER BY user_id LIMIT 25", (b["id"],))
            if not targets:
                DB.run("UPDATE broadcasts SET status='finished', finished_at=? WHERE id=? AND status='running'", (now(), b["id"]))
                s = broadcast_summary(int(b["id"]))
                with contextlib.suppress(TelegramError):
                    await bot.send_message(int(b["created_by"]), f"📣 Broadcast #{b['id']} finished: ✅ {s['sent']} • ❌ {s['failed']} • ⏭ {s['skipped']}")
                continue
            for t in targets:
                if stop.is_set():
                    return
                cur = DB.one("SELECT status FROM broadcasts WHERE id=?", (b["id"],))
                if not cur or cur["status"] != "running":
                    break
                uid = int(t["user_id"])
                st, err = "sent", None
                try:
                    m = await bot.copy_message(uid, b["from_chat_id"], b["message_id"])
                    if b["pin"]:
                        with contextlib.suppress(TelegramError):
                            await bot.pin_chat_message(uid, m.message_id, disable_notification=True)
                except Forbidden:
                    st, err = "skipped", "blocked"
                    DB.run("UPDATE users SET unreachable_at=? WHERE user_id=?", (now(), uid))
                except RetryAfter as e:
                    await asyncio.sleep(int(e.retry_after) + 1)
                    continue
                except BadRequest as e:
                    st, err = ("skipped", "chat not found") if "not found" in str(e).lower() else ("failed", str(e)[:120])
                except (TimedOut, NetworkError):
                    await asyncio.sleep(2)
                    continue
                except TelegramError as e:
                    st, err = "failed", str(e)[:120]
                with DB.tx() as c:
                    c.execute("UPDATE broadcast_targets SET status=?, error=?, attempts=attempts+1, updated_at=? WHERE broadcast_id=? AND user_id=?", (st, err, now(), b["id"], uid))
                    c.execute(f"UPDATE broadcasts SET {st}={st}+1, cursor=? WHERE id=?", (uid, b["id"]))
                await asyncio.sleep(0.04)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("broadcast worker error")
            await asyncio.sleep(5)


async def scheduler(bot: Any, stop: asyncio.Event) -> None:
    last_backup_day = None
    last_hourly = 0
    while not stop.is_set():
        try:
            ts = now()
            if ts - last_hourly >= 3600:
                last_hourly = ts
                DB.run("UPDATE challenges SET status='expired' WHERE status='pending' AND expires_at<=?", (ts,))
                DB.run("UPDATE rewards SET status='expired' WHERE status='available' AND expires_at IS NOT NULL AND expires_at<=?", (ts,))
                DB.run("UPDATE installations SET status='pending' WHERE status='approved' AND approved_until IS NOT NULL AND approved_until<=?", (ts,))
                retention_cleanup()
                restock_enqueue()
                dead = int(DB.scalar("SELECT COUNT(*) FROM jobs WHERE status='dead' AND done_at>?", (ts - 3600,)))
                if dead:
                    notify_admins("deadjobs", f"⚠️ {dead} background job(s) failed permanently in the last hour. See Tools → Health.", 3600)
            day = day_key(ts)
            if day != last_backup_day and datetime.fromtimestamp(ts, tz()).hour >= 3:
                last_backup_day = day
                b = await asyncio.to_thread(create_backup, None, "scheduled daily")
                log.info("daily backup %s (%s bytes, ok=%s)", b["path"], b["size"], b["ok"])
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("scheduler error")
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=60)

# ==== 19. SELF-TESTS ==========================================================================

def _legacy_fixture(path: str) -> None:
    c = sqlite3.connect(path)
    c.executescript("""
    CREATE TABLE users(user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, ref_by INTEGER, ref_done INTEGER DEFAULT 0, refs INTEGER DEFAULT 0,
        claims INTEGER DEFAULT 0, verified INTEGER DEFAULT 0, blocked INTEGER DEFAULT 0, joined_at INTEGER, last_seen INTEGER);
    CREATE TABLE rewards(id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT, used_by INTEGER, used_at INTEGER, added_at INTEGER);
    CREATE TABLE claims(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, reward_id INTEGER, code TEXT, kind TEXT, claimed_at INTEGER);
    CREATE TABLE channels(chat_id INTEGER PRIMARY KEY, title TEXT, invite_link TEXT, is_private INTEGER DEFAULT 1, added_at INTEGER);
    CREATE TABLE requests(user_id INTEGER, chat_id INTEGER, status TEXT, ts INTEGER, PRIMARY KEY(user_id, chat_id));
    CREATE TABLE admins(user_id INTEGER PRIMARY KEY, added_by INTEGER, added_at INTEGER);
    CREATE TABLE waitlist(user_id INTEGER PRIMARY KEY, ts INTEGER);
    CREATE TABLE settings(key TEXT PRIMARY KEY, val TEXT);
    INSERT INTO settings VALUES('refs_per_reward','2');
    INSERT INTO users VALUES(100,'alice','Alice',NULL,0,3,2,1,0,1700000000,1700000000);
    INSERT INTO users VALUES(200,'bob','Bob',100,1,0,0,1,1,1700000100,1700000100);
    INSERT INTO users VALUES(300,NULL,'Cara',100,0,0,0,0,0,1700000200,1700000200);
    INSERT INTO rewards(code,used_by,used_at,added_at) VALUES('CODE-A',100,1700000300,1700000000),('CODE-B',100,1700000400,1700000000),('DUP',NULL,NULL,1),('DUP',NULL,NULL,2),('FREE-1',NULL,NULL,3);
    INSERT INTO claims(user_id,reward_id,code,kind,claimed_at) VALUES(100,1,'CODE-A','bonus',1700000300),(100,2,'CODE-B','referral',1700000400);
    INSERT INTO waitlist VALUES(300,1700000500);
    """)
    c.commit()
    c.close()


def _make_init_data(token: str, user: Dict[str, Any], auth_date: int, extra: Optional[Dict[str, str]] = None) -> str:
    data = {"auth_date": str(auth_date), "user": json.dumps(user, separators=(",", ":")), "query_id": "AAE"}
    data.update(extra or {})
    check = "\n".join(f"{k}={data[k]}" for k in sorted(data))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    data["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urllib.parse.urlencode(data)


def run_self_tests() -> int:
    global CFG
    tmp = tempfile.mkdtemp(prefix="rbtest-")
    path = os.path.join(tmp, "legacy.db")
    _legacy_fixture(path)
    CFG.db_path, CFG.backup_dir, CFG.owner_id, CFG.app_env = path, os.path.join(tmp, "bk"), 1, "test"
    CFG.session_secret = "x" * 40
    use_database(path)
    results: List[Tuple[str, bool, str]] = []

    def check(name: str, cond: bool, info: str = "") -> None:
        results.append((name, bool(cond), info))

    # migration
    rep = run_migrations()
    check("migration applied", rep["from"] == 1 and rep["to"] == SCHEMA_VERSION and rep["integrity"] == "ok", str(rep))
    check("pre-migration backup written", bool(rep["backup"]) and os.path.exists(rep["backup"]))
    check("legacy claims delivered + referenced", DB.scalar("SELECT COUNT(*) FROM claims WHERE claim_ref IS NULL OR delivery_status<>'delivered'") == 0)
    check("legacy bonus consumed", get_user(100)["bonus_consumed_at"] == 1700000300)
    check("legacy block -> suspended", get_user(200)["suspended"] == 1)
    check("legacy referrals imported", DB.scalar("SELECT COUNT(*) FROM referrals WHERE referrer_id=100") == 2 and DB.scalar("SELECT status FROM referrals WHERE referee_id=200") == "qualified")
    check("legacy opening balance (3 refs - 1 claim*2)", credit_balance(100) == 1, str(credit_balance(100)))
    check("duplicate available code disabled, one kept", DB.scalar("SELECT COUNT(*) FROM rewards WHERE code='DUP' AND status='available'") == 1)
    check("waitlist migrated", waitlist_on(300))
    check("idempotent re-migration", run_migrations()["from"] == SCHEMA_VERSION)
    init_session_secret()
    # referral engine
    u4 = SimpleNamespace(id=400, first_name="Dan", username=None)
    ensure_user(u4, ref_by=100)
    check("self-referral rejected", not attach_referral(400, 400))
    check("attribution final", not attach_referral(400, 300))
    q1 = qualify_referral(400)
    check("qualify credits referrer", q1 is not None and credit_balance(100) == 2)
    check("qualify idempotent", qualify_referral(400) is None and credit_balance(100) == 2)
    # claims & concurrency
    import_commit([f"T-{i}" for i in range(1, 6)], 1)
    set_setting("refs_per_reward", 2)
    ensure_user(SimpleNamespace(id=500, first_name="Eve", username=None))
    b = perform_claim(500, "bonus", "t:bonus1")
    check("bonus claim", b["kind"] == "bonus" and not b["duplicate"])
    check("bonus one-time", _raises(lambda: perform_claim(500, "bonus", "t:bonus2"), "bonus_unavailable"))
    check("idempotent claim returns same ref", perform_claim(500, "bonus", "t:bonus1")["claim_ref"] == b["claim_ref"])
    check("insufficient credits", _raises(lambda: perform_claim(500, "referral", "t:r1"), "insufficient"))
    r = perform_claim(100, "referral", "t:r100")
    check("referral claim consumes credits", r["kind"] == "referral" and credit_balance(100) == 0)
    set_setting("refs_per_reward", 5)
    check("rule change preserves ledger", credit_balance(100) == 0 and eligibility(get_user(100))["need"] == 5)
    set_setting("refs_per_reward", 2)
    # concurrent claims for the last codes
    adjust_credits(100, 20, 1, "test credits")
    left = stock_count()
    errors: List[str] = []
    refs: List[str] = []

    def worker(i: int) -> None:
        try:
            refs.append(perform_claim(100, "referral", f"t:conc{i}")["claim_ref"])
        except ClaimError as e:
            errors.append(e.code)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(left + 4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    check("concurrency: exactly stock allocated", len(refs) == left and errors.count("out_of_stock") == 4, f"{len(refs)}/{left} errs={errors}")
    check("no double allocation", DB.scalar("SELECT COUNT(*) FROM (SELECT reward_id FROM claims WHERE kind<>'gift' GROUP BY reward_id HAVING COUNT(*)>1 AND reward_id IN (SELECT id FROM rewards WHERE status='allocated'))") == 0)
    check("gift does not consume credits", (lambda before: (import_commit(["G-1"], 1), perform_claim(500, "gift", "t:g1", actor=1), credit_balance(500) == before)[2])(credit_balance(500)))
    check("delivery job queued", DB.scalar("SELECT COUNT(*) FROM jobs WHERE kind='deliver_claim'") >= 1)
    # vault validation
    import_commit(["V-1"], 1)
    pv = import_preview(["OK-1", "OK-1", "bad code", "V-1", ""])
    check("import validation", pv["valid"] == ["OK-1"] and pv["duplicates_in_input"] == ["OK-1"] and pv["invalid"] == ["bad code"] and pv["duplicates_in_vault"] == ["V-1"], str(pv))
    check("csv injection neutralised", csv_safe("=HYPERLINK()").startswith("'") and csv_safe("plain") == "plain")
    # initData
    tok = "123456:ABC-DEF"
    good = _make_init_data(tok, {"id": 777, "first_name": "Zed"}, now())
    check("initData valid", validate_init_data(good, tok, 900)["user_id"] == 777)
    check("initData tampered", _auth_fails(lambda: validate_init_data(good.replace("Zed", "Zap"), tok, 900), "init_data_bad_signature"))
    check("initData expired", _auth_fails(lambda: validate_init_data(_make_init_data(tok, {"id": 1}, now() - 5000), tok, 900), "init_data_expired"))
    check("initData future", _auth_fails(lambda: validate_init_data(_make_init_data(tok, {"id": 1}, now() + 5000), tok, 900), "init_data_future"))
    check("initData duplicate param", _auth_fails(lambda: validate_init_data(good + "&auth_date=1", tok, 900), "init_data_duplicate_param"))
    check("initData malformed", _auth_fails(lambda: validate_init_data("hash=zz", tok, 900), "init_data_malformed"))
    # sessions / challenges
    set_setting("session_policy", "approve")
    inst1, new1 = register_installation(777, "k" * 32, "ios")
    check("first installation trusted", new1 and inst1["status"] == "approved")
    inst2, _ = register_installation(777, "m" * 32, "android")
    check("second installation pending under approve", inst2["status"] == "pending" and approval_needed(777, inst2))
    ch = create_challenge(777, "session", inst2["id"])
    check("challenge not consumable by other account", consume_challenge(ch["id"], 778, "approved") is None)
    check("challenge consumed once", consume_challenge(ch["id"], 777, "approved") is not None and consume_challenge(ch["id"], 777, "approved") is None)
    check("installation approved after challenge", not approval_needed(777, DB.one("SELECT * FROM installations WHERE id=?", (inst2["id"],))))
    tk, sess = create_session(777, inst2["id"], "android")
    check("session token digest lookup", session_from_token(tk)["id"] == sess["id"] and session_from_token(tk + "x") is None)
    check("session revoke", revoke_session(777, sess["id"]) and session_from_token(tk) is None)
    check("native claim needs approval under policy", native_claim_needs_approval(777))
    set_setting("session_policy", "notify")
    # permissions
    DB.run("INSERT INTO admins(user_id,added_by,added_at,role) VALUES(900,1,1,'support')")
    check("support cannot manage vault", not has_perm(900, "vault_manage") and has_perm(900, "support_reply"))
    check("owner has all", has_perm(1, "roles_manage") and not has_perm(999, "view_overview"))
    check("role escalation blocked", ROLE_OWNER not in (ROLE_ADMIN, ROLE_REWARD, ROLE_SUPPORT, ROLE_ANALYST) and role_of(1) == ROLE_OWNER)
    # confirmations, broadcasts, promo, checkin
    cid = create_confirmation(1, "gift", "500")
    check("confirmation single use", consume_confirmation(cid, 1) is not None and consume_confirmation(cid, 1) is None)
    check("confirmation actor bound", consume_confirmation(create_confirmation(1, "x", None), 2) is None)
    bid = create_broadcast(1, 1, 1, "all")
    check("broadcast launch idempotent", launch_broadcast(bid, 1) and not launch_broadcast(bid, 1) and broadcast_summary(bid)["total"] >= 1)
    check("broadcast pause/cancel", broadcast_control(bid, 1, "pause") and broadcast_control(bid, 1, "cancel"))
    DB.run("INSERT INTO promo_codes(code,points,max_total,max_per_user,expires_at,enabled,created_at) VALUES('P1',2,2,1,?,1,?)", (now() + 100, now()))
    check("promo redeem once", redeem_promo(500, "p1")["ok"] and redeem_promo(500, "P1")["error"] == "already_used" and redeem_promo(400, "P1")["ok"] and redeem_promo(300, "P1")["error"] == "exhausted")
    set_setting("checkin_points", 1)
    check("check-in once per day", do_checkin(500)["ok"] and do_checkin(500)["error"] == "already")
    # templates
    check("template validation", validate_template("hi {name} {bogus}") is not None and validate_template("<script>x</script>") is not None and validate_template("<b>{name}</b>") is None)
    check("template render escapes", "&lt;" in render_template("{name}", {"name": "<x>"}))
    # suspension & maintenance
    suspend_user(500, 1, "test")
    check("suspended cannot claim", _raises(lambda: perform_claim(500, "gift", "t:g2", actor=1), "suspended"))
    check("maintenance flag", (set_setting("maintenance", 1), maintenance_on(), set_setting("maintenance", 0))[1])
    # jobs restart recovery + backup/restore validation
    DB.run("UPDATE jobs SET status='running'")
    check("running jobs recovered", recover_running_jobs() >= 1 and DB.scalar("SELECT COUNT(*) FROM jobs WHERE status='running'") == 0)
    bk = create_backup(note="test")
    check("backup integrity", bk["ok"] and validate_candidate_db(bk["path"]) is None)
    check("garbage restore rejected", validate_candidate_db(__file__) is not None)
    check("retention cleanup runs", isinstance(retention_cleanup(), dict))
    check("leaderboard excludes staff and suspended", all(r["user_id"] not in (1, 500) for r in leaderboard("all")))
    # report
    failed = [r for r in results if not r[1]]
    for name, ok, info in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  — {info}" if info and not ok else ""))
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    shutil.rmtree(tmp, ignore_errors=True)
    return 0 if not failed else 1


def _raises(fn: Callable[[], Any], code: str) -> bool:
    try:
        fn()
    except ClaimError as e:
        return e.code == code
    return False


def _auth_fails(fn: Callable[[], Any], code: str) -> bool:
    try:
        fn()
    except AuthError as e:
        return e.code == code
    return False

# ==== 20. STARTUP AND GRACEFUL SHUTDOWN =======================================================

BOT_COMMANDS = [("start", "Open the menu"), ("claim", "Claim a reward"), ("refer", "Your referral link"), ("rewards", "My rewards"),
                ("profile", "Profile"), ("top", "Leaderboard"), ("security", "Security & sessions"), ("support", "Support"),
                ("app", "Open the Mini App"), ("help", "Help"), ("cancel", "Cancel current input")]


async def post_init(app: Application) -> None:
    global BOT_USERNAME
    me = await app.bot.get_me()
    BOT_USERNAME = me.username or ""
    with contextlib.suppress(TelegramError):
        await app.bot.set_my_commands([BotCommand(c, d) for c, d in BOT_COMMANDS])
    if CFG.public_base_url.startswith("https://"):
        with contextlib.suppress(TelegramError):
            await app.bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Rewards", web_app=WebAppInfo(url=app_url())))
    log.info("bot @%s ready • mode=%s • app=%s", BOT_USERNAME, CFG.bot_mode, app_url() if CFG.public_base_url else "(no PUBLIC_BASE_URL)")


def bootstrap_storage() -> None:
    restored = apply_pending_restore()
    if restored:
        log.warning("restored database from staged backup")
    rep = run_migrations()
    log.info("schema v%s→v%s integrity=%s backup=%s", rep["from"], rep["to"], rep.get("integrity"), rep.get("backup"))
    if rep.get("integrity") != "ok":
        raise SystemExit("database integrity check failed; refusing to start")
    init_session_secret()
    n = recover_running_jobs()
    if n:
        log.info("recovered %s interrupted job(s)", n)


async def serve() -> None:
    stop = asyncio.Event()
    RT.stop_event = stop
    builder = ApplicationBuilder().token(CFG.bot_token).post_init(post_init).concurrent_updates(True)
    if CFG.bot_mode == "webhook":
        builder = builder.updater(None)
    application = builder.build()
    RT.application = application
    register_handlers(application)
    config = uvicorn.Config(api, host=CFG.host, port=CFG.port, log_level="info", proxy_headers=(CFG.trusted_proxy == "xff"),
                            forwarded_allow_ips="*" if CFG.trusted_proxy == "xff" else None, workers=1, lifespan="off")
    server = uvicorn.Server(config)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, stop.set)
    await application.initialize()
    await application.start()
    if CFG.bot_mode == "polling":
        await application.bot.delete_webhook(drop_pending_updates=CFG.drop_pending)
        await application.updater.start_polling(drop_pending_updates=CFG.drop_pending, allowed_updates=Update.ALL_TYPES)
    else:
        await application.bot.set_webhook(url=f"{CFG.public_base_url}/telegram/webhook/{CFG.webhook_secret}", secret_token=CFG.webhook_secret,
                                          allowed_updates=Update.ALL_TYPES, drop_pending_updates=CFG.drop_pending)
    workers = [asyncio.create_task(job_worker(application.bot, stop), name="jobs"),
               asyncio.create_task(broadcast_worker(application.bot, stop), name="broadcast"),
               asyncio.create_task(scheduler(application.bot, stop), name="scheduler")]
    server_task = asyncio.create_task(server.serve(), name="http")
    RT.ready = True
    log.info("HTTP listening on %s:%s (single worker; the bot and jobs live in this process)", CFG.host, CFG.port)
    try:
        while not stop.is_set() and not server.should_exit and not server_task.done():
            await asyncio.sleep(0.5)
    finally:
        RT.ready = False
        stop.set()
        server.should_exit = True
        log.info("shutting down…")
        with contextlib.suppress(Exception):
            await asyncio.wait_for(server_task, timeout=10)
        for w in workers:
            w.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        with contextlib.suppress(Exception):
            if CFG.bot_mode == "polling" and application.updater and application.updater.running:
                await application.updater.stop()
        with contextlib.suppress(Exception):
            await application.stop()
        with contextlib.suppress(Exception):
            await application.shutdown()
        log.info("bye")


def main(argv: List[str]) -> int:
    if "--self-test" in argv:
        return run_self_tests()
    errors = CFG.validate()
    if "--check-config" in argv:
        print("\n".join(errors) if errors else "config ok")
        return 1 if errors else 0
    if errors:
        for e in errors:
            log.error("config: %s", e)
        return 2
    if not CFG.public_base_url:
        log.warning("PUBLIC_BASE_URL not set — the Mini App button is hidden; only the native bot is available")
    bootstrap_storage()
    if "--migrate-only" in argv:
        return 0
    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
