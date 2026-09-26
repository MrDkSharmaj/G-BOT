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

