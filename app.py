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

# Session IDs are kept in process memory only, per Telegram user, until action/cancel.
pending_sessions: dict[int, str] = {}
session_lock = threading.Lock()


def is_allowed(update: Update) -> bool:
    user = update.effective_user
    return bool(user and user.id in ALLOWED_USER_IDS)


def make_authorization(sessionid: str) -> tuple[str, str]:
    if not sessionid or any(
        ord(c) <= 32 or ord(c) >= 127 or c in ';,"\\' for c in sessionid
    ):
        raise ValueError("Invalid input. Send only the sessionid cookie value.")
    match = re.fullmatch(r"([1-9][0-9]*)(?::|%3[aA]).+", sessionid)
    if not match:
        raise ValueError(
            "Invalid sessionid format. It should start with your numeric Instagram user ID "
            "followed by ':' (or '%3A'). Do not include 'sessionid='."
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


def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔒 Hide lists", callback_data="hide"),
          InlineKeyboardButton("👁 Unhide lists", callback_data="unhide")],
         [InlineKeyboardButton("✖ Cancel", callback_data="cancel")]]
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await update.message.reply_text("Access denied.")
        return
    await update.message.reply_text(
        "VEIL-X — Instagram list visibility\n\n"
        "Send your Instagram sessionid cookie value in this private chat "
        "(without the 'sessionid=' prefix). I’ll then show Hide and Unhide buttons.\n\n"
        "Security: only your own account; never share this cookie with anyone. "
        "This bot keeps it in RAM temporarily and does not save it to disk. "
        "Delete the message containing the cookie from this chat after sending it.\n\n"
        "Use /cancel to clear the pending session."
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await update.message.reply_text("Access denied.")
        return
    with session_lock:
        pending_sessions.pop(update.effective_user.id, None)
    await update.message.reply_text("Pending session cleared.")


async def receive_session(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await update.message.reply_text("Access denied.")
        return
    if update.effective_chat.type != "private":
        await update.message.reply_text("For safety, use this bot in a private chat.")
        return
    value = (update.message.text or "").strip()
    try:
        make_authorization(value)
    except ValueError as error:
        await update.message.reply_text(str(error))
        return

    with session_lock:
        pending_sessions[update.effective_user.id] = value
    await update.message.reply_text(
        "Session received temporarily. Choose an action:",
        reply_markup=main_keyboard(),
    )


async def choose_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user = query.from_user
    if user.id not in ALLOWED_USER_IDS:
        await query.edit_message_text("Access denied.")
        return

    if query.data == "cancel":
        with session_lock:
            pending_sessions.pop(user.id, None)
        await query.edit_message_text("Pending session cleared.")
        return

    if query.data not in ("hide", "unhide"):
        return
    with session_lock:
        sessionid = pending_sessions.pop(user.id, None)
    if not sessionid:
        await query.edit_message_text("No pending session. Send /start and submit it again.")
        return

    hide = query.data == "hide"
    await query.edit_message_text(
        "Applying the change… This may take a few seconds. No automatic retry."
    )
    try:
        # Run blocking network I/O off the async event loop.
        import asyncio
        await asyncio.to_thread(set_list_visibility, sessionid, hide)
        result = "🔒 Instagram confirmed: followers/following lists are now hidden."
        if not hide:
            result = "👁 Instagram confirmed: followers/following lists are now unhidden."
        await query.message.reply_text(
            result + "\n\nThe session was cleared from bot memory."
        )
    except (RuntimeError, ValueError, urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
        # Error messages are deliberately sanitized; never echo credentials.
        await query.message.reply_text(
            f"Could not confirm the change: {str(error)[:400]}\n"
            "The session was cleared from bot memory. Check Instagram manually before retrying."
        )
    finally:
        sessionid = None


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
