import base64
import json
import logging
import os
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# Do not enable HTTP debug logging: Telegram messages can contain session cookies.
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("veil-x")

BOT_TOKEN = os.environ["BOT_TOKEN"]
# Comma-separated numeric Telegram user IDs. Required so strangers cannot use your bot.
_raw_ids = os.getenv("ALLOWED_USER_IDS", "").strip()
if not _raw_ids:
    raise RuntimeError("Set ALLOWED_USER_IDS to your Telegram numeric user ID(s).")
ALLOWED_USER_IDS = {
    int(value.strip()) for value in _raw_ids.split(",") if value.strip()
}
PORT = int(os.getenv("PORT", "10000"))

# Session IDs are kept in process memory only, per Telegram user, until action/logout.
pending_sessions: dict[int, str] = {}
session_lock = threading.Lock()

# --- Text -------------------------------------------------------------------

START_TEXT = (
    "🕶️ *VEIL-X*\n"
    "Control your Instagram list visibility.\n\n"
    "Tap below to log in."
)

LOGIN_PROMPT = (
    "🔑 Send your Instagram `sessionid` cookie value — just the value, "
    "without the `sessionid=` prefix.\n\n"
    "🔒 Kept in memory only, never saved to disk. Delete your message after sending.\n\n"
    "Use /cancel to go back."
)

LOGIN_SUCCESS = "✅ Logged in. Choose an action:"

ACCESS_DENIED = "🚫 You're not authorised to use this bot."
NOT_PRIVATE = "🔒 Please message me privately for security."
SESSION_EXPIRED = "⚠️ Session expired — please log in again."

SUPPORT_URL = "https://t.me/veilxsupportbot"

# --- Keyboards ----------------------------------------------------------
# `style` (PTB >= 22.7, Bot API with button styling) renders real colored
# buttons on Telegram clients released after 2026-02-09; older clients just
# show the button unstyled, so this degrades safely.


def start_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔐 Login", callback_data="login", style="primary")]]
    )


def action_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🙈 Hide", callback_data="hide", style="success"),
          InlineKeyboardButton("👁 Unhide", callback_data="unhide", style="primary")],
         [InlineKeyboardButton("🚪 Logout", callback_data="logout", style="danger")]]
    )


def access_denied_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🆘 Contact Support", url=SUPPORT_URL)]]
    )


def is_allowed(update: Update) -> bool:
    user = update.effective_user
    return bool(user and user.id in ALLOWED_USER_IDS)


async def send_start_screen(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    await context.bot.send_message(
        chat_id=chat_id,
        text=START_TEXT,
        reply_markup=start_keyboard(),
        parse_mode="Markdown",
    )


# --- Instagram call -----------------------------------------------------


def make_authorization(sessionid: str) -> tuple[str, str]:
    if not sessionid or any(
        ord(c) <= 32 or ord(c) >= 127 or c in ';,"\\' for c in sessionid
    ):
        raise ValueError("⚠️ Invalid input — send only the sessionid cookie value.")
    match = re.fullmatch(r"([1-9][0-9]*)(?::|%3[aA]).+", sessionid)
    if not match:
        raise ValueError(
            "⚠️ Invalid format. It should start with your numeric Instagram user ID "
            "followed by ':' (or '%3A'). Don't include 'sessionid='."
        )
    user_id = match.group(1)
    token = base64.b64encode(
        json.dumps(
            {
                "ds_user_id": user_id,
                "sessionid": sessionid,
                "should_use_header_over_cookies": True,
            },
            separators=(",", ":"),
        ).encode()
    ).decode("ascii")
    return user_id, "Bearer IGT:2:" + token


def set_list_visibility(sessionid: str, hide: bool) -> dict:
    user_id, authorization = make_authorization(sessionid)
    friendly_name = "IGSettingsBooleanMutation"
    form = {
        "method": "post",
        "format": "json",
        "server_timestamps": "true",
        "locale": "user",
        "fb_api_req_friendly_name": friendly_name,
        "client_doc_id": "2669261950526978627082629536",
        "variables": json.dumps(
            {
                "setting_id": "hide_followers_following_list",
                "allow_error_codes": True,
                "callsite": "account_privacy",
                "value": bool(hide),
            }
        ),
    }
    request = urllib.request.Request(
        "https://i.instagram.com/graphql/query",
        data=urllib.parse.urlencode(form).encode(),
        headers={
            "Authorization": authorization,
            "Cookie": f"sessionid={sessionid}; ds_user_id={user_id}",
            "IG-INTENDED-USER-ID": user_id,
            "X-IG-App-ID": "124024574287414",
            "X-Auth-Type": "instagram",
            "User-Agent": "Instagram 453.0.0",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept-Encoding": "identity",
            "X-GraphQL-Client-Library": "pando",
            "X-FB-Friendly-Name": friendly_name,
        },
        method="POST",
    )

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            raise RuntimeError("Instagram redirected the request; stopped for safety.")

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(request, timeout=20) as response:
            raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise RuntimeError("Instagram response was too large.")
            body = json.loads(raw)
    except urllib.error.HTTPError as error:
        with error:
            raw = error.read(16384)
        details = {}
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                for key in ("message", "error_type", "status", "code"):
                    if isinstance(parsed.get(key), (str, int, bool)):
                        details[key] = parsed[key]
        except (ValueError, UnicodeError):
            pass
        # Never include request headers, cookies, or response body in logs/messages.
        raise RuntimeError(
            f"Instagram returned HTTP {error.code}. "
            "Check that the session is valid and try again later."
        ) from None

    data = body.get("data") if isinstance(body, dict) else None
    root = "xdt_api__v1__settings__set_bool"
    matches = [
        value for key, value in data.items()
        if key == root or key.startswith(root + "(")
    ] if isinstance(data, dict) else []
    result = matches[0] if len(matches) == 1 and isinstance(matches[0], dict) else {}
    expected = bool(hide)
    if (
        not isinstance(body, dict)
        or body.get("errors")
        or body.get("error")
        or body.get("status") == "fail"
        or result.get("value") is not expected
        or any(
            result.get(key)
            for key in (
                "error_name_value", "errorNameValue",
                "fallback_message", "fallbackMessage",
            )
        )
    ):
        raise RuntimeError(
            "Instagram did not confirm the change. No automatic retry was made."
        )
    return body


# --- Handlers -------------------------------------------------------------


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await update.message.reply_text(ACCESS_DENIED, reply_markup=access_denied_keyboard())
        return
    context.user_data["awaiting_login"] = False
    await update.message.reply_text(
        START_TEXT, reply_markup=start_keyboard(), parse_mode="Markdown"
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await update.message.reply_text(ACCESS_DENIED, reply_markup=access_denied_keyboard())
        return
    with session_lock:
        pending_sessions.pop(update.effective_user.id, None)
    context.user_data["awaiting_login"] = False
    await update.message.reply_text("✖️ Cancelled. Session cleared.")
    await send_start_screen(update.effective_chat.id, context)


async def receive_session(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await update.message.reply_text(ACCESS_DENIED, reply_markup=access_denied_keyboard())
        return
    if not context.user_data.get("awaiting_login"):
        await update.message.reply_text("👋 Tap 🔐 Login to get started.")
        await send_start_screen(update.effective_chat.id, context)
        return
    if update.effective_chat.type != "private":
        await update.message.reply_text(NOT_PRIVATE)
        return
    value = (update.message.text or "").strip()
    try:
        make_authorization(value)
    except ValueError as error:
        await update.message.reply_text(str(error))
        return

    with session_lock:
        pending_sessions[update.effective_user.id] = value
    context.user_data["awaiting_login"] = False
    await update.message.reply_text(LOGIN_SUCCESS, reply_markup=action_keyboard())


async def choose_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user = query.from_user
    if user.id not in ALLOWED_USER_IDS:
        await query.edit_message_text(ACCESS_DENIED, reply_markup=access_denied_keyboard())
        return

    if query.data == "login":
        context.user_data["awaiting_login"] = True
        await query.edit_message_text(LOGIN_PROMPT, parse_mode="Markdown")
        return

    if query.data == "logout":
        with session_lock:
            pending_sessions.pop(user.id, None)
        context.user_data["awaiting_login"] = False
        await query.edit_message_text("🚪 Logged out. Session cleared.")
        await send_start_screen(query.message.chat_id, context)
        return

    if query.data not in ("hide", "unhide"):
        return
    with session_lock:
        sessionid = pending_sessions.pop(user.id, None)
    if not sessionid:
        await query.edit_message_text(SESSION_EXPIRED)
        await send_start_screen(query.message.chat_id, context)
        return

    hide = query.data == "hide"
    await query.edit_message_text("⏳ Applying the change…")
    try:
        # Run blocking network I/O off the async event loop.
        import asyncio
        await asyncio.to_thread(set_list_visibility, sessionid, hide)
        result = (
            "🙈 Done — your followers/following lists are now hidden."
            if hide
            else "👁 Done — your followers/following lists are now visible."
        )
        await query.message.reply_text(result)
    except (RuntimeError, ValueError, urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
        # Error messages are deliberately sanitized; never echo credentials.
        await query.message.reply_text(
            f"⚠️ Couldn't confirm the change: {str(error)[:400]}\n"
            "Please verify on Instagram before retrying."
        )
    finally:
        sessionid = None
    await send_start_screen(query.message.chat_id, context)


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in ("/", "/health"):
            self.send_response(404)
            self.end_headers()
            return
        body = b"VEIL-X is running"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        # Avoid noisy access logs.
        return


def start_health_server() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", PORT), HealthHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("Health endpoint listening on port %s", PORT)


def main() -> None:
    start_health_server()
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("cancel", cancel))
    application.add_handler(CallbackQueryHandler(choose_action))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, receive_session)
    )
    log.info("VEIL-X bot starting")
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
