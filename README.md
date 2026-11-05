# VEIL-X Telegram Bot (Render)

Telegram interface for the existing VEIL-X Instagram list visibility mutation.
It supports **Hide lists** and **Unhide lists** using inline buttons attached to
the bot's messages.

## Important security notes

- Use only with an Instagram account you own or are explicitly authorized to manage.
- Your Instagram `sessionid` is an account credential. Anyone who gets it may be
  able to access your account. Do not send it to a bot you do not control.
- Set `ALLOWED_USER_IDS` to your own numeric Telegram user ID. The app refuses
  to start if this allowlist is missing. Anyone else who messages the bot gets
  an "not authorised" reply with a **🆘 Contact Support** button linking to
  `https://t.me/veilxsupportbot`.
- The bot holds a submitted session in RAM only until an action or `/cancel`.
  It does not write session IDs to disk or intentionally log message contents.
- Telegram chat history may retain the message you sent. Delete that message
  after submitting it, and consider using a private chat with the bot.
- Instagram's private/undocumented endpoint can change or reject requests.
  The bot reports success only when the response confirms the requested value.
  It does not retry automatically.

## Deploy to Render

1. Create a Telegram bot with **@BotFather** and copy its token.
2. Find your numeric Telegram user ID (for example, use a trusted user-ID bot).
3. Put these files in a GitHub repository:
   - `app.py`
   - `requirements.txt`
   - `render.yaml`
4. In Render, choose **New + → Blueprint** and connect that repository.
5. In the service's Environment settings, set:
   - `BOT_TOKEN` = your BotFather token
   - `ALLOWED_USER_IDS` = your numeric Telegram user ID
     (multiple IDs can be comma-separated)
6. Deploy, then open your Telegram bot and send `/start`.

Do not commit your bot token or Instagram session IDs to GitHub.

## UptimeRobot

After deployment, copy the public Render service URL, such as
`https://your-service.onrender.com`.

Create an UptimeRobot **HTTP(s)** monitor:
- URL: `https://your-service.onrender.com/health`
- Monitoring interval: 5 minutes (or the shortest interval your UptimeRobot plan allows)
- Expected response: HTTP 200

The health endpoint confirms the web process is responding. A periodic ping may
reduce idle spin-down on services/plans that support it, but it is not a guarantee
of continuous uptime. Render plan limits and UptimeRobot interval/plan rules apply.

## Button colors

Buttons use Telegram's native `style` field (`primary` blue, `success` green,
`danger` red), added in Bot API support via `python-telegram-bot` 22.7. This
only renders as color on Telegram clients released after 2026-02-09; older
clients show the same buttons unstyled, with no error.

## Local run

```bash
python -m pip install -r requirements.txt
```

Set environment variables, then:

```bash
python app.py
```

On PowerShell:

```powershell
$env:BOT_TOKEN="YOUR_BOT_TOKEN"
$env:ALLOWED_USER_IDS="YOUR_NUMERIC_TELEGRAM_USER_ID"
python app.py
```

## Bot flow

1. `/start` → shows a **🔐 Login** button.
2. Tap **Login**, then send your `sessionid` cookie value (without `sessionid=`).
3. On success, you'll see **🙈 Hide**, **👁 Unhide**, and **🚪 Logout**.
4. Tap **Hide** or **Unhide** to apply the change; the session is removed from
   memory immediately after, and the start screen is sent again automatically.
5. Tap **Logout**, or send `/cancel`, to clear a pending session at any point —
   the start screen is sent again automatically either way.
