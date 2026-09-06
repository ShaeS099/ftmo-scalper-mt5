"""
Sends every trade event and every compliance event to Telegram in real time,
even though the bot runs fully automatically — this is your audit trail and
early-warning system.
"""
import logging

import requests

import config

logger = logging.getLogger("notifier")

API_URL = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"


def _send(text: str):
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        logger.warning("Telegram not configured, skipping message: %s", text)
        return
    try:
        resp = requests.post(
            API_URL,
            data={"chat_id": config.TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=10,
        )
        # A non-2xx or {"ok": false} response doesn't raise — Telegram can
        # reject a message (bad/revoked token, bot blocked, rate limited)
        # while requests still sees a clean HTTP round-trip, so this was
        # silently swallowed before: found during pre-live review 2026-08-19.
        body = resp.json()
        if not resp.ok or not body.get("ok"):
            logger.error("Telegram rejected message (status=%s): %s", resp.status_code, body)
    except requests.RequestException as exc:
        logger.error("Failed to send Telegram message: %s", exc)


def notify_trade_opened(signal, lot_size: float, order_result: dict):
    _send(
        f"🟢 *Trade Opened*\n"
        f"Symbol: `{signal.symbol}`\n"
        f"Direction: *{signal.direction}*\n"
        f"Entry: `{order_result.get('price', signal.entry_price):.5f}`\n"
        f"Stop Loss: `{signal.stop_loss:.5f}`\n"
        f"Take Profit: `{order_result.get('take_profit', signal.take_profit):.5f}`\n"
        f"Lots: `{lot_size}`\n"
        f"Confidence: `{signal.confidence_score}`"
    )


def notify_trade_blocked(signal, reason: str):
    _send(
        f"⚪ *Signal Skipped*\n"
        f"Symbol: `{signal.symbol}` ({signal.direction})\n"
        f"Reason: {reason}"
    )


def notify_trade_closed(symbol: str, profit: float):
    emoji = "✅" if profit >= 0 else "🔴"
    _send(f"{emoji} *Trade Closed*\nSymbol: `{symbol}`\nP&L: `${profit:+.2f}`")


def notify_trade_closed_ticket(symbol: str, ticket: int):
    """Fired when manage_trailing_stops() notices a tracked position is no
    longer open. Realized P&L isn't available here without a deal-history
    lookup, so this just confirms the close happened — check MT5 history
    for the exact figure."""
    _send(f"✅ *Trade Closed*\nSymbol: `{symbol}`\nTicket: `{ticket}`")


def notify_stop_moved(symbol: str, kind: str, profit_r: float):
    label = "Moved to breakeven" if kind == "breakeven" else "Trailing stop updated"
    _send(f"🔒 *{label}*\nSymbol: `{symbol}`\nCurrently ahead by: `{profit_r:.2f}R`")


def notify_compliance_block(reason: str):
    _send(f"🛑 *Compliance Block*\n{reason}")


def notify_flatten_event(symbols: list, reason: str):
    if not symbols:
        return
    _send(f"⏹ *Positions Flattened* ({reason})\nSymbols: {', '.join(symbols)}")


def notify_daily_summary(summary_text: str):
    _send(f"📊 *Daily Summary*\n{summary_text}")


def notify_bot_status(text: str):
    _send(f"ℹ️ {text}")
