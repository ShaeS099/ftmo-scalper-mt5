"""
FTMOComplianceGuard: the single source of truth for "is a new trade allowed
right now". Every order must pass through check_trade_allowed() first.

Deliberately separated from risk.py (position sizing) so the two can be
tested and audited independently, and so a bug in sizing logic can never
accidentally bypass the account-level circuit breakers.
"""
import json
import logging
import os
from datetime import date, datetime, timezone

import config

logger = logging.getLogger("compliance")

DEFAULT_STATE_FILE = "compliance_state.json"


class FTMOComplianceGuard:
    def __init__(
        self,
        starting_balance: float = config.STARTING_BALANCE,
        daily_loss_limit_pct: float = config.DAILY_LOSS_LIMIT_PCT,
        max_drawdown_pct: float = config.MAX_DRAWDOWN_PCT,
        profit_target_pct: float = config.PROFIT_TARGET_PCT,
        safety_buffer: float = config.SAFETY_BUFFER,
        state_file: str = DEFAULT_STATE_FILE,
        current_equity: float = None,
    ):
        self.state_file = state_file
        self.starting_balance = starting_balance
        # Daily loss cap is the exact FTMO limit, no extra buffer — "loses
        # $500 in a session -> no more auto trades that day" per user
        # instruction, which for a $10k account IS the real 5% limit itself.
        # Max drawdown keeps the safety_buffer margin since that breach is
        # permanent/account-ending, unlike the daily loss which just resets
        # tomorrow.
        self.daily_loss_cap = starting_balance * (daily_loss_limit_pct / 100)
        self.max_drawdown_cap = starting_balance * (max_drawdown_pct / 100) * safety_buffer
        self.profit_target = starting_balance * (profit_target_pct / 100)

        # peak_equity correctly seeds from starting_balance (the true
        # evaluation high-water mark, for max-drawdown-from-peak purposes).
        # day_start_equity is a DIFFERENT concept — "equity when today
        # began" — and seeding IT from starting_balance too was a bug found
        # live 2026-08-19: on a fresh state file (no prior day recorded),
        # this conflated ALL prior drawdown (days/weeks of earlier trading)
        # with "today's" loss. E.g. real equity was ~$48,985 but
        # day_start_equity read $50,000, computing a false $2,627 "daily
        # loss" against the real $2,500 cap from drawdown that had nothing
        # to do with today. current_equity (the actual live balance/equity
        # at construction time) is the correct seed for a genuinely fresh
        # start; falls back to starting_balance only if the caller has no
        # live figure to pass (e.g. offline/test construction).
        self.day_start_equity = current_equity if current_equity is not None else starting_balance
        self.peak_equity = starting_balance
        self.current_trading_day = datetime.now(timezone.utc).date()
        self.trading_days_active = set()
        self.daily_pnl_start_of_day: dict = {}

        # Max drawdown is a ONE-WAY breach: once tripped, it stays tripped
        # even if equity later recovers, because that's how FTMO actually
        # treats it — breach the limit once and the account/challenge is
        # over, not paused. Daily loss resets naturally at day rollover
        # (handled in roll_day_if_needed) since that IS how FTMO treats it.
        self.max_drawdown_breached = False
        self.max_drawdown_breach_reason = None

        # Everything above is in-memory only, which used to mean a restart
        # (crash, Ctrl+C, laptop closing) silently cleared the "permanent"
        # breach flag and today's day_start_equity — found 2026-08-19 during
        # a pre-live review. Loading persisted state (if any) overrides the
        # fresh values set above with whatever was last saved to disk.
        self._load_state()

    def _load_state(self):
        if not os.path.exists(self.state_file):
            return
        try:
            with open(self.state_file) as f:
                data = json.load(f)
            self.starting_balance = data["starting_balance"]
            self.day_start_equity = data["day_start_equity"]
            self.peak_equity = data["peak_equity"]
            self.current_trading_day = date.fromisoformat(data["current_trading_day"])
            self.trading_days_active = {date.fromisoformat(d) for d in data["trading_days_active"]}
            self.max_drawdown_breached = data["max_drawdown_breached"]
            self.max_drawdown_breach_reason = data["max_drawdown_breach_reason"]
            # Recompute caps from the PERSISTED starting_balance (not whatever
            # was passed to __init__ this run) so a restart can never silently
            # shrink/grow the real limits by re-deriving them off a different
            # balance than the evaluation actually started with.
            self.daily_loss_cap = self.starting_balance * (config.DAILY_LOSS_LIMIT_PCT / 100)
            self.max_drawdown_cap = self.starting_balance * (config.MAX_DRAWDOWN_PCT / 100) * config.SAFETY_BUFFER
            self.profit_target = self.starting_balance * (config.PROFIT_TARGET_PCT / 100)
            logger.info("Loaded persisted compliance state from %s (starting_balance=%.2f, "
                        "max_drawdown_breached=%s)", self.state_file, self.starting_balance,
                        self.max_drawdown_breached)
        except Exception:
            logger.exception("Failed to load %s — starting fresh this run. If a breach was "
                              "previously recorded, THIS DELETES THAT PROTECTION — check the "
                              "file manually before trusting a fresh start.", self.state_file)

    def _save_state(self):
        try:
            with open(self.state_file, "w") as f:
                json.dump({
                    "starting_balance": self.starting_balance,
                    "day_start_equity": self.day_start_equity,
                    "peak_equity": self.peak_equity,
                    "current_trading_day": self.current_trading_day.isoformat(),
                    "trading_days_active": sorted(d.isoformat() for d in self.trading_days_active),
                    "max_drawdown_breached": self.max_drawdown_breached,
                    "max_drawdown_breach_reason": self.max_drawdown_breach_reason,
                }, f, indent=2)
        except Exception:
            logger.exception("Failed to save compliance state to %s", self.state_file)

    def reset_max_drawdown_breach(self):
        """Manual override only — call this deliberately after reviewing
        why the breach happened, never automatically."""
        logger.warning("Max drawdown breach flag manually reset")
        self.max_drawdown_breached = False
        self.max_drawdown_breach_reason = None
        self._save_state()

    # --- daily bookkeeping ---
    def roll_day_if_needed(self, current_equity: float, now: datetime = None):
        today = (now or datetime.now(timezone.utc)).date()
        if today != self.current_trading_day:
            logger.info("New trading day detected. Resetting day_start_equity to %.2f", current_equity)
            self.current_trading_day = today
            self.day_start_equity = current_equity

    def mark_trading_day_active(self):
        self.trading_days_active.add(self.current_trading_day)
        self._save_state()

    # --- core checks ---
    def check_trade_allowed(self, current_equity: float, now: datetime = None) -> tuple[bool, str]:
        self.roll_day_if_needed(current_equity, now=now)
        self.peak_equity = max(self.peak_equity, current_equity)

        if self.max_drawdown_breached:
            self._save_state()
            return False, self.max_drawdown_breach_reason

        daily_loss = self.day_start_equity - current_equity
        total_drawdown = self.peak_equity - current_equity

        if daily_loss >= self.daily_loss_cap:
            self._save_state()
            return False, (f"Daily loss limit reached "
                            f"(${daily_loss:.2f} of ${self.daily_loss_cap:.2f}) — no more auto trades for the day")

        if total_drawdown >= self.max_drawdown_cap:
            self.max_drawdown_breached = True
            self.max_drawdown_breach_reason = (
                f"Max drawdown safety buffer breached "
                f"(${total_drawdown:.2f} of ${self.max_drawdown_cap:.2f}) — bot permanently disabled for this "
                f"account. This does not clear on its own even if equity recovers; call "
                f"reset_max_drawdown_breach() manually after reviewing what happened."
            )
            self._save_state()
            return False, self.max_drawdown_breach_reason

        self._save_state()
        return True, "OK"

    def target_reached(self, current_equity: float) -> bool:
        return (current_equity - self.starting_balance) >= self.profit_target

    def status_summary(self, current_equity: float) -> str:
        daily_loss = self.day_start_equity - current_equity
        total_drawdown = self.peak_equity - current_equity
        return (
            f"Equity: ${current_equity:.2f}\n"
            f"Today's P&L: ${current_equity - self.day_start_equity:+.2f}\n"
            f"Daily loss buffer used: ${max(daily_loss, 0):.2f} / ${self.daily_loss_cap:.2f}\n"
            f"Drawdown buffer used: ${max(total_drawdown, 0):.2f} / ${self.max_drawdown_cap:.2f}\n"
            f"Trading days active: {len(self.trading_days_active)} / {config.MIN_TRADING_DAYS}"
        )
