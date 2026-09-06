"""
Central configuration for the FTMO scalping bot.
Loads secrets from .env and defines every tunable constant in one place,
so nothing risk-related is buried inside logic files.
"""
import os
from dotenv import load_dotenv

load_dotenv()

# --- Broker / MT5 ---
MT5_LOGIN = int(os.getenv("MT5_LOGIN", "0"))
MT5_PASSWORD = os.getenv("MT5_PASSWORD", "")
MT5_SERVER = os.getenv("MT5_SERVER", "")
MT5_TERMINAL_PATH = os.getenv("MT5_TERMINAL_PATH", "")

# --- Telegram ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# --- Symbols traded ---
# NOTE: exact symbol names vary by broker (FTMO's own feed vs. the underlying
# liquidity provider). Confirm these match what's in your MT5 Market Watch
# window exactly (e.g. some brokers use "US500.cash", others "US500" or
# "SP500m") before going live — a typo here just means the symbol silently
# never gets scanned.
# EURUSD removed per explicit instruction — a 60-day backtest (with the H4
# bias filter below, and separately without it) never turned profitable on
# EURUSD while the same filter turned both GBPUSD and USDJPY from
# drawdown-breaching losers into solid winners. A straight swap to XAUUSD was
# considered (it lost far less than EURUSD at baseline) but explicitly
# rejected in favor of just running GBPUSD + USDJPY. XAUUSD was ALSO
# excluded earlier for underperforming GBPUSD/USTEC across three prior
# backtests (see SYMBOL_TRAIL_ATR_MULTIPLE note below) — and the H4 bias
# filter actively hurt it in the swap-candidate backtest (opposite of its
# effect on GBPUSD/USDJPY), so it's not a drop-in fit for this filter either.
# USOIL.cash added 2026-08-21 after standalone-screening XAUUSD, EURAUD,
# EURUSD, and GBPJPY (60d, confidence 0.35, exhaustion filter ON): all four
# lost money standalone and three of them (XAUUSD, EURUSD, GBPJPY) breached
# the drawdown cap on their own. USOIL.cash was the only winner (49 trades,
# 53.1% WR, +$5,603.24, lowest max DD of the whole batch) and held up
# combined with the existing pair: GBPUSD+USDJPY+USOIL.cash hit 118 trades,
# 55.9% WR, +$14,983.60, max DD $2,717.27 -- the best result of any
# configuration tested this entire project, on every metric at once.
# USOIL.cash was pulled for a few hours on 2026-08-25 after two live oil
# losses that morning -- live diagnosis + a dedicated basket comparison
# (USOIL.cash vs swapping in XAUUSD, both 60d/full current config) both
# confirmed no logic bug: SL/TP math checked out exactly on every trade, the
# losses were normal fill-slippage variance, and XAUUSD as a replacement was
# dramatically worse (45.3% WR, +$471.04, actually breached the drawdown cap
# in backtest, vs USOIL.cash's 56.8% WR, +$17,239.98, DD safely under cap).
# Restored same day. See the comment above for the original basket result.
# CHANGED 2026-08-26 (permanent, per explicit instruction): USOIL.cash
# dropped from the basket entirely. GBPUSD and USDJPY (the other two thirds
# of the basket result quoted above) carried forward unchanged, trading
# NORMAL rules (standard H4 bias, M1's own liquidity levels) -- same as
# USDCAD.
# CHANGED 2026-08-30: XAUUSD pulled from the live basket after a 35-day
# combined test showed it dominating trade count via a structurally looser
# H1 gate, plus an unresolved reversal-cluster problem the reversal filters
# hadn't fixed yet at that point.
# CHANGED 2026-08-31 (permanent, per explicit instruction): each symbol's
# rules individually tuned end to end (H4 timeframe, confidence, exhaustion
# lookback, structure-break lookback -- see the four SYMBOL_*_OVERRIDE
# dicts below) after standalone 60d backtests found every one of them
# individually profitable: USDCAD +$12,549.26 (69.4% WR), USDJPY +$7,816.49
# (58.5% WR), GBPUSD +$3,469.64 (54.5% WR), XAUUSD +$2,841.84 (51.9% WR).
# XAUUSD restored to the live basket on that basis.
# IMPORTANT CAVEAT found the same day: combining all four with these exact
# settings in ONE basket does NOT just add the four results together -- a
# 60d combined backtest showed a single bad week (all four symbols drawing
# down together, correlated USD/broad-sentiment exposure) breaching the
# account-wide drawdown cap almost immediately, after which the guard
# permanently blocked the remaining ~53 days (final result: -$1,560.69,
# only 29 total trades vs 185 if the four ran in isolation). Each symbol's
# OWN settings are validated; running all four together at full per-symbol
# risk simultaneously is NOT yet validated as safe -- there is no
# portfolio-level risk control here, only the per-trade 0.75% sizing and
# the shared account-wide compliance guard. COMPLIANCE_GUARD_ENABLED is
# currently False live (left off per earlier explicit instruction) -- with
# no portfolio risk control AND the guard off, a repeat of that correlated
# week has nothing live stopping it. Flagged for the user; not changed
# without being asked.
SYMBOLS = ["GBPUSD", "USDJPY", "USDCAD", "XAUUSD"]

# --- Timeframes (MT5 constants, imported where needed) ---
HTF_TIMEFRAME = "M5"   # trend bias -- unchanged for the 2026-08-26 XAUUSD trial
LTF_TIMEFRAME = "M1"    # entry trigger (use M1 if you want higher frequency)
# H4 bias confluence (see H4_BIAS_FILTER_ENABLED below): a coarser trend read
# than the M5 bias, required to agree before a signal is allowed through.
# Only the EMA9/21 direction check is used, not a range/boundary check —
# backtesting found the boundary component added ~nothing on GBPUSD/USDJPY
# (in one case producing results IDENTICAL to no filter at all) and made
# things WORSE than bias-alone on EURUSD, so it was dropped rather than kept
# as dead weight. An H1 swap was tried and rejected on GBPUSD/USDJPY with M1
# entry (2026-08-14/15, see H4_BIAS_FILTER_ENABLED below) -- H1's EMA9/21
# flipped too often relative to that fast entry timeframe. Re-tried on
# XAUUSD specifically 2026-08-26, isolated 20d test: H1 bias alone was MORE
# permissive than H4 for XAUUSD (26 vs 14 trades, higher P&L), the opposite
# of what happened on GBPUSD/USDJPY.
# CHANGED 2026-08-28 (permanent, per explicit instruction): H1 applied to
# ALL symbols, not just XAUUSD -- reasoning being H4 reacts too slowly for
# a London-open strategy. IMPORTANT CAVEAT: this specifically re-applies
# the H1 setup to GBPUSD/USDJPY that was tested and REJECTED on 2026-08-14/15
# (looked good at zero slippage, -$3,776.73/60d and a drawdown breach once
# slippage was simulated). A lot of other config has changed since then
# (confidence threshold, exhaustion filter, the basket itself), so it's
# worth re-testing rather than assuming the old rejection still holds --
# but it hasn't been re-validated as of this change. The per-symbol
# override mechanism is no longer needed now that every symbol uses the
# same value, so it's cleared rather than kept as unused dead weight.
# REVERTED 2026-08-28: H1-for-all tested (30d, full basket) and confirmed
# worse across the board -- 19 trades/21.1% WR/-$4,340.17 baseline, and
# neither the structure-break nor momentum-divergence filter rescued it
# (still losers, still a real daily-loss-limit breach both ways). Back to
# H4 as the global default, with XAUUSD's H1 override restored -- the one
# case where H1 was actually validated as an improvement over H4 (2026-08-26,
# 26 vs 14 trades, higher P&L, isolated 20d test).
# CHANGED 2026-08-30 (temporary working value, per explicit instruction --
# CHANGED 2026-08-31 (permanent, per explicit instruction): per-symbol
# confluence timeframe re-tuned individually once structure-break/confidence/
# exhaustion were also being tuned per symbol (see SYMBOL_MIN_CONFIDENCE_OVERRIDE
# etc. below) -- H4 turned out best for GBPUSD, USDCAD, AND XAUUSD once
# paired with each symbol's own confidence/exhaustion/structure settings
# (XAUUSD's earlier H1 preference from 2026-08-26 was found under different,
# now-superseded settings). Only USDJPY still wants M30. H4_TIMEFRAME is the
# global default (covers GBPUSD/USDCAD/XAUUSD); USDJPY is the one override.
H4_TIMEFRAME = "H4"
SYMBOL_H4_TIMEFRAME_OVERRIDE = {"USDJPY": "M30"}

# --- Compliance guard toggle ---
# Live-bot-only kill switch for the FTMO daily-loss/max-drawdown circuit
# breaker (compliance.py). backtest.py does NOT read this flag and always
# enforces the guard, so backtests still show where the account would have
# breached even while this is off live.
# TEMPORARY -- REVERT TO True TONIGHT: disabled 2026-08-26 per explicit
# instruction, as part of today's one-off XAUUSD/USDCAD trial. With this
# False, our OWN guard will not block trades after a daily-loss/drawdown
# breach -- FTMO's server-side enforcement is still real and separate (it
# force-closed a position and disabled trade_allowed earlier today
# regardless of this flag), so a real breach still gets the account
# locked out, just without our own advance warning/blocking.
COMPLIANCE_GUARD_ENABLED = False

# --- FTMO Phase 1 account rules ($10k tier) ---
STARTING_BALANCE = float(os.getenv("STARTING_BALANCE","9676.88"))
DAILY_LOSS_LIMIT_PCT = 5.0      # FTMO official limit
MAX_DRAWDOWN_PCT = 10.0         # FTMO official limit
SAFETY_BUFFER = 0.8             # applies to max drawdown only (permanent breach, so extra margin).
                                 # Daily loss is NOT buffered — it stops trading for the day at the
                                 # exact official limit (see compliance.py), per explicit instruction.
PROFIT_TARGET_PCT = 10.0
MIN_TRADING_DAYS = 4

# --- Risk per trade ---
# Lowered from 0.75 after a 30-day/3-symbol backtest showed 0.75 (and even
# 0.5) permanently breached FTMO's max-drawdown safety cap (a ONE-WAY trip,
# see compliance.py) within ~2 weeks, silently truncating the rest of the
# test. 0.35 was the highest value that survived the full 30-day span with
# max drawdown ($562.84) staying clear of the $800 cap (10% x 0.8 buffer).
# That test predates the H4/London-only/spread-guard/fixed-1.5R config below
# — raised to 0.75 on 2026-08-17 after a 60-day backtest of the CURRENT
# config (0.75%, same H4/London/spread-guard/fixed-RR setup) cleared the
# $5,000 FTMO target in 15 calendar days / 10 trading days with only 1.92%
# ($960) max drawdown en route and zero compliance breaches over the full
# 60-day window. 1.00% was also tested and reaches target 1 day faster but
# breaches the buffered drawdown cap later in the window — 0.75% was the
# better risk/speed tradeoff of the three tested and the real, validated
# value.
# TEMPORARY -- REVERT TO 0.75 (per explicit instruction, "just for live
# trial purposes" set 2026-09-01): 3.0% is 4x the validated risk and has
# NOT been backtested at this size on the current per-symbol-tuned basket.
# At 3% per trade, just two losing trades in a row nearly exhausts the
# entire 5% daily loss limit -- combined with COMPLIANCE_GUARD_ENABLED
# currently False and no portfolio-level risk control across the 4-symbol
# basket (which already showed a near-breach in backtest at 0.75% during a
# correlated bad week), this is real, elevated live risk. Revert once the
# trial is done -- don't let this become the next "Aug 22 incident".
RISK_PERCENT_PER_TRADE = 0.75   # TEMPORARY trial value -- real/validated value is 0.75

# Pads the stop distance used for LIVE position sizing only (see risk.py) --
# the actual sl sent to the broker is never affected. Found live 2026-08-19:
# a real trade sized against an ~11pt tick-snapshot distance but filled at
# ~16pt (1.45x), realizing ~1.09% risk instead of the intended 0.75%. This
# is NOT modeled by any backtest here — backtest.py sizes directly off
# signal.entry_price/stop_loss with no separate live-tick-vs-fill step, so
# it structurally cannot see this failure mode. 1.3 is a first, reasoned
# guess at a safety margin (not yet validated against a real sample of
# fills) -- revisit once there's enough live trade history to check whether
# realized risk is actually landing at/under the 0.75% target.
LOT_SIZING_SAFETY_MARGIN = 1.3

# --- Execution filters ---
# Spread filter is PER-SYMBOL because gold, silver, and index CFDs trade on
# a completely different point/price scale than forex majors — one global
# "25 points" threshold would be meaningless for XAUUSD or US100.cash.
# These starting values are illustrative only: pull up each symbol's typical
# live spread in MT5 (Market Watch -> right-click -> Symbols) and calibrate
# on demo before trusting them.
DEFAULT_MAX_SPREAD_POINTS = 25       # fallback for any symbol not listed below
SYMBOL_MAX_SPREAD_POINTS = {
    "EURUSD": 25,        # ~2.5 pips
    "GBPUSD": 15,        # ~2.5 pips
    "USDCHF": 25,        # ~2.5 pips
    "USDJPY": 25,        # ~2.5 pips (JPY pairs quote 3 decimals, point = 0.001)
    "XAUUSD": 50,        # gold spreads run wider in raw points; recalibrate on demo
    "XAGUSD": 50,        # silver, same caveat as gold
    "US500": 100,   # index CFD points are not comparable to FX pips
    "UK100": 150,   # Nasdaq CFD typically has the widest raw-point spread
    "USTEC": 150,        # same underlying (Nasdaq) as US100.cash — same caveat applies
    # Calibrated 2026-08-21 from this broker's actual recent M1 spread data
    # (was falling back to DEFAULT_MAX_SPREAD_POINTS=25 for both, which badly
    # understated real cost for USOIL.cash specifically):
    "EURAUD": 15,        # measured: median 6pts, p90 12pts, occasional spikes to 250+
    "USOIL.cash": 75,    # measured: median 68pts, mean 70pts, p90 71pts -- 25 would be nowhere close to real
    "GBPJPY": 25,        # measured: median 16pts, p90 23pts -- already close to DEFAULT, explicit for clarity
    "USDCAD": 4.5,       # measured 2026-08-25/26: median 3.0pts -- threshold = 1.5x median
}
# Disabled (was 15) — the allowed trading window is now a narrow 4-hour
# London-open block (see TRADING_ALLOWED_START/END_HOUR_UTC below), so a
# per-symbol cooldown on top of that was just skipping valid signals.
SYMBOL_COOLDOWN_MINUTES = 0
LIQUIDITY_SWEEP_LOOKBACK = 20   # candles, on LTF_TIMEFRAME (M1)

# Experimental, off by default (only used when a caller passes df_level_tf
# to detect_liquidity_sweep — see scanner.py). Deliberately decoupled from
# LIQUIDITY_SWEEP_LOOKBACK: reusing 20 bars against M15 data means a 5-hour
# window, which a single M1 wick essentially never breaches (empirically:
# 0/3000 in a sample backtest). 6 bars of M15 = 1.5 hours, the middle of the
# 4-8 bar / 1-2 hour range this was scoped to test.
LIQUIDITY_LEVEL_LOOKBACK = 6   # candles, on whatever timeframe df_level_tf is

# --- Momentum/trend-continuation strategy (experimental, off by default) ---
# A second, genuinely separate signal type alongside the liquidity-sweep
# reversal strategy above — added 2026-08-19 after GBPUSD had a real 124-point
# decline (08:18-08:56 UTC) that the sweep strategy structurally could never
# trade, because H4/M5 bias read UP the entire time (it only fades wicks IN
# the direction of that bias). This strategy deliberately does NOT check
# H4/M5 bias — it determines its own direction from the move itself, since
# inheriting the sweep strategy's bias filter would miss the exact same
# moves for the exact same reason.
#
# V1 (raw breakout chase: enter the instant net move clears the ATR
# threshold) backtested a clear loser: 33 trades/60d, 30.3% win rate,
# -$4,274.40, and it breached the $2,500 daily loss cap at least once.
# Diagnosis: no exhaustion check meant it was buying tops/selling bottoms as
# often as catching real continuations.
#
# V2 (this version, untested until the next backtest run) requires a
# pullback-then-resume structure instead of chasing the raw breakout: the
# candle just before entry must be a COUNTER-move (a down-candle inside an
# up-move, or vice versa), and the entry candle must resume in the trend
# direction AND break past that pullback candle's extreme — a real
# continuation pattern, not "any candle still going the right way". Also
# adds an ADX floor (must show genuine trending conditions) and a ceiling on
# how extended the move can already be (MOMENTUM_MAX_ATR_MULTIPLE) to avoid
# entering moves that are already near exhaustion.
MOMENTUM_STRATEGY_ENABLED = False
MOMENTUM_LOOKBACK = 10   # candles, on LTF_TIMEFRAME (M1)
MOMENTUM_MIN_ATR_MULTIPLE = 2.0   # net move over the lookback must clear this x ATR
MOMENTUM_MAX_ATR_MULTIPLE = 5.0   # ...but not exceed this -- likely exhausted beyond it
MOMENTUM_MIN_ADX = 20   # same threshold MIN_ADX uses elsewhere, computed on LTF here
MOMENTUM_STOP_ATR_MULTIPLE = 1.5   # wider than sweep's 0.75x -- entering an already-moving market
MOMENTUM_RR_MULTIPLE = 1.5   # reuses the sweep strategy's validated fixed-RR approach

# --- Microstructure risk filters ---
# Real Volume confirmation: reject the sweep candle unless its volume clears
# a short-period MA of the PRECEDING candles (never including the sweep
# candle itself, to avoid lookahead). Uses tick_volume, not real_volume —
# verified empirically (2026-08-17) that real_volume is 0 for every single
# M1 candle on both GBPUSD and USDJPY on this broker (FTMO-Demo doesn't
# report actual traded volume for FX/CFDs), so a literal "real volume"
# filter would reject every trade. tick_volume (tick count per candle) is
# the standard proxy used for this in retail FX for exactly that reason.
# Tested 2026-08-17 (60d, H4/London-only baseline): cut trade count 36% and
# profit 76% for only a modest drawdown improvement — rejected too many
# profitable setups, not a selective enough filter. Left OFF.
VOLUME_CONFIRMATION_ENABLED = False
VOLUME_MA_PERIOD = 10   # candles, on LTF_TIMEFRAME (M1)

# Dynamic spread protection: reject the sweep candle if its own spread
# exceeds SPREAD_PROTECTION_MULTIPLE x the rolling average spread of the
# preceding SPREAD_LOOKBACK_BARS candles (also excluding the sweep candle).
# Tested 2026-08-17 (60d, H4/London-only baseline): 1.2x improved win rate,
# P&L (+25.7%), AND max drawdown (-31%) simultaneously, for only 7% fewer
# trades — a clean win, not a tradeoff. 1.5x tested weaker on every metric.
# Enabled at 1.2x.
SPREAD_PROTECTION_ENABLED = True
SPREAD_PROTECTION_MULTIPLE = 1.2   # 20% above rolling average
SPREAD_LOOKBACK_BARS = 1440   # ~24h of M1 candles

# --- Volatility & trend-strength filter (HTF) ---
# Blocks trading in dead sessions and choppy/ranging conditions, which is
# where the liquidity-sweep entry produces the most false signals live.
ATR_LOOKBACK = 14
ATR_AVG_LOOKBACK = 50            # how many bars of ATR history to average against
MIN_ATR_RATIO = 0.8              # current ATR must be >= 80% of its own recent average
ADX_LOOKBACK = 14
MIN_ADX = 20                     # below this, ADX considers the market non-trending

# --- Minimum stop-loss distance (LTF) ---
# Raw sweep-wick stops are frequently noise-level (a few points), which gets
# them stopped out by ordinary chop rather than a real reversal failing.
# If the raw wick distance is tighter than this, the stop is buffered out to
# this minimum instead of being rejected outright.
LTF_ATR_LOOKBACK = 14
MIN_STOP_ATR_MULTIPLE = 0.75     # minimum stop = 0.75x the 5M ATR

# --- Weekend / session gap protection ---
# A large time gap between consecutive candles (e.g. Friday close -> Monday
# open) can make the "recent swing high/low" span pre-gap and post-gap
# prices, producing a nonsensical stop distance. Skip signal generation if
# a gap this large appears inside the lookback window used for the sweep.
GAP_SKIP_HOURS = 6

# --- Entry confirmation ---
# When True, a sweep only triggers a trade once the FOLLOWING candle also
# closes further in the trade's favor than the sweep candle did. This
# filters out reclaims that immediately fail on the next candle (a common
# losing pattern seen in backtesting) at the cost of a slightly later,
# slightly wider entry and fewer total signals.
REQUIRE_CONFIRMATION_CANDLE = False

# --- Broker server clock offset ---
# MT5 candle/tick timestamps are stamped with the BROKER's server wall clock,
# not true UTC. Confirmed empirically on 2026-07-30: FTMO-Demo's server clock
# read 3 hours ahead of real UTC (mt5.symbol_info_tick(...).time vs.
# datetime.now(timezone.utc)). This can shift with the broker's own DST
# changes — recheck it the same way if session-based filtering starts
# looking off, and update this constant.
BROKER_UTC_OFFSET_HOURS = 3

# --- Trading session window (true UTC, converted via the offset above) ---
# Changed from a narrow overnight block-list to a strict ALLOW-list per
# explicit instruction: trade ONLY the London open, strictly avoid the
# Asian session AND the NY open/session entirely — not just the prior
# 21:00-01:00 dead patch. Start moved back to 06:00 UTC (from 07:00) per
# explicit instruction to capture the pre-London liquidity ramp-up hour,
# on top of the 07:00-08:00 official London open and its follow-through
# before NY pre-market activity picks up around 11:00-12:00 UTC. Adjust
# these two bounds if a narrower/wider window is wanted — everything
# outside [START, END) is blocked.
TRADING_ALLOWED_START_HOUR_UTC = 6
# Was temporarily extended 6->12 for 2026-08-22 only (catch the pre-NY
# ramp-up, per explicit instruction) -- never reverted, because 2026-08-22
# was a Saturday with the market closed, so nobody was watching it trade and
# no one caught it staying at 12. It silently carried into 2026-08-24 (the
# next real trading day) and let a real trade fire at 10:12 UTC, a time
# never actually validated for the current locked config -- the basket/
# exhaustion-filter/confidence-0.35 combination was only tested against
# 06:00-10:00. Found and reverted 2026-08-24 during a live activity review.
# Was temporarily extended 10->11->16 on 2026-08-26 (through the NY session,
# for that day's XAUUSD/USDCAD trial) -- reverted back to 10 same day once
# testing was done for the day. See the Aug 22 incident two lines above for
# why this always gets reverted explicitly rather than left to "expire" on
# its own.
# CHANGED 2026-08-28 (permanent, per explicit instruction): window now
# closes at 10:30 instead of 10:00. Fractional hours are supported here
# (10.5 = 10:30) -- in_trading_window()/_hour_in_window() in scanner.py
# were changed the same day to compare against the timestamp's minutes too,
# not just the whole hour, specifically so this value could be exact rather
# than rounded to the nearest hour.
# Was temporarily extended to 11:00 UTC on 2026-09-01 (per explicit
# instruction, that day only) and reverted back same night.
TRADING_ALLOWED_END_HOUR_UTC = 10.5   # exclusive -- 10:30 UTC

# London/NY overlap window, would ADD on top of the London-open window above
# (not a replacement). 12:00-16:00 UTC is the standard definition of the
# overlap (NY opens ~12:00-13:00 UTC while London is still active; London
# closes ~16:00 UTC). Tested 2026-08-17 (60d, H4 bias, 0.5 pip slippage,
# GBPUSD+USDJPY, lookahead-fixed code): ON more than doubled trade count
# (125->292) but flipped P&L from +$2,018.52 to -$284.84 and grew max
# drawdown 79% ($1,921.70->$3,443.58) — the extra overlap-session volume is
# net-negative for this strategy, not just diluted. Disabled based on that
# result.
#
# Reverted back to False 2026-08-21 (end of day) -- was temporarily True
# that day only, for faster forward-test feedback on the basket/exhaustion-
# filter/confidence-0.35/TP-correction/lot-sizing-margin change set. Back to
# London-open-only for 2026-08-22 onward, per explicit instruction.
# TEMPORARY -- REVERT TO FALSE TONIGHT (2026-09-03 ONLY): enabled again per
# explicit instruction, today only, ADDED alongside the London window (not
# replacing it) -- both windows are active today. Same mistake class as the
# Aug 22 incident elsewhere in this file (a "today only" change left in and
# silently carried into a later real trading day). Also worth remembering:
# the last time this was actually backtested (see the net-negative result a
# few lines above), NY overlap was found NET-NEGATIVE for this strategy --
# but that was under a completely different basket/config (no per-symbol
# tuning, no structure-break filter, different confidence/exhaustion), so it
# hasn't been re-validated against the current setup either way.
NY_OVERLAP_ENABLED = True
NY_OVERLAP_START_HOUR_UTC = 12
NY_OVERLAP_END_HOUR_UTC = 16   # exclusive

# --- Trend-exhaustion filter ---
# Added 2026-08-21 after two separate real losing clusters (2026-08-19,
# 2026-08-21) where the bot took several sweep-reversal trades in a row
# against one still-ongoing move, each stopped out before the move actually
# reversed. The existing HTF/H4 bias check only asks "which way is the
# trend", not "is this trend still strong" -- a market can be making fresh
# extremes with full conviction and still pass that check every single bar.
# This asks the second question: requires HTF (M5) ADX to be DECLINING over
# EXHAUSTION_ADX_LOOKBACK_BARS bars, not just above a floor -- a rising or
# peaking ADX means the move is still gathering strength (worst time for a
# reversal bet); a declining ADX means it's already lost some, regardless of
# its absolute level. Enabled 2026-08-21 after backtesting (60d, GBPUSD+
# USDJPY): cut trade count 61% (125->49) but win rate rose 53.6%->59.2% and
# max drawdown fell 46% (2927.97->1574.46), no breach. Real quality-for-
# quantity tradeoff -- the lower MIN_CONFIDENCE_SCORE (0.35) and the 3rd
# basket symbol (USOIL.cash) are what recovered the lost trade volume; see
# their own comments. Don't disable this without re-lowering confidence back
# toward 0.40, or reconsidering the basket -- they were validated together.
EXHAUSTION_FILTER_ENABLED = True
# 5 -> 4 on 2026-08-25 after a lookback sweep (3/4/5/7/8/10, 60d, full
# current basket+confidence-0.30 config): 4 bars beat 5 on every metric at
# once -- 135 trades vs 124, 57.0% WR vs 56.5%, +$19,420.37 vs +$17,089.13,
# and LOWER max drawdown ($2,041.09 vs $2,307.79, the best in the whole
# sweep). 3 bars pulled more trades (144) but drawdown blew out to
# $3,366.86; 7/8/10 bars trended toward fewer trades and lower P&L. 4 is a
# clean win, not a tradeoff.
EXHAUSTION_ADX_LOOKBACK_BARS = 4   # HTF (M5) candles
# CHANGED 2026-08-31: per-symbol overrides, tuned together with
# SYMBOL_MIN_CONFIDENCE_OVERRIDE and SYMBOL_STRUCTURE_LOOKBACK_OVERRIDE
# above/below -- see that comment for the full per-symbol result numbers.
SYMBOL_EXHAUSTION_LOOKBACK_OVERRIDE = {
    "USDCAD": 3,
    "USDJPY": 10,
    "GBPUSD": 3,
    "XAUUSD": 3,
}

# --- Reversal detection (trial, both default off) ---
# Added 2026-08-28 after diagnosing repeated loss clusters (GBPUSD, then
# twice on XAUUSD) where EMA9/21 bias + the ADX-decline exhaustion filter
# both stayed on the wrong side of a trend that was already reversing --
# neither can distinguish "healthy pullback" from "trend actually flipping".
# Two independent detectors, meant to be A/B tested against each other
# (and against neither), not necessarily run together:
#   STRUCTURE_BREAK_FILTER_ENABLED: vetoes a trade if price already made a
#     lower low (for a BUY) or higher high (for a SELL) versus the prior
#     swing window -- reads market structure directly instead of waiting on
#     a lagging average.
#   MOMENTUM_DIVERGENCE_FILTER_ENABLED: vetoes a trade if RSI is diverging
#     against the bias (price makes a fresh extreme, RSI doesn't confirm
#     it) -- different information from ADX, which only measures trend
#     strength and can't see this kind of weakening.
# See structure_intact() / momentum_diverging_against_bias() in scanner.py.
# ENABLED 2026-08-31 (permanent, per explicit instruction): part of the
# per-symbol tuning made permanent above (SYMBOL_STRUCTURE_LOOKBACK_OVERRIDE
# etc.) -- every one of today's individually-profitable per-symbol results
# depends on this being on. Momentum-divergence stays off; it never beat
# baseline in any test this project.
STRUCTURE_BREAK_FILTER_ENABLED = True
MOMENTUM_DIVERGENCE_FILTER_ENABLED = False
# CHANGED 2026-08-30/31: 10 was the untested original default (found to
# silently never apply due to a late-binding default-argument bug, fixed
# 2026-08-30). 5 was the best generic value found once the bug was fixed
# and tested across a shared basket; kept as the fallback for any symbol
# not listed in SYMBOL_STRUCTURE_LOOKBACK_OVERRIDE below, whose per-symbol
# values (tuned individually, see that comment) supersede this for the
# current basket entirely.
STRUCTURE_LOOKBACK = 5   # HTF (M5) candles per window (2x this many looked at total)
SYMBOL_STRUCTURE_LOOKBACK_OVERRIDE = {
    "USDCAD": 8,
    "USDJPY": 10,
    "GBPUSD": 15,
    "XAUUSD": 3,
}
RSI_LOOKBACK = 14

# --- Session / schedule rules ---
FRIDAY_FLATTEN_HOUR_UTC = 20    # force-close all positions before weekend
NEWS_PAUSE_MINUTES_BEFORE = 15
NEWS_PAUSE_MINUTES_AFTER = 15

# --- Entry confirmation strength (lever #1: better edge, not just more filters) ---
# Requiring the confirmation candle to progress by ANY amount let through
# marginal reclaims that barely continued before reversing. Requiring a
# minimum progress relative to ATR filters those out.
CONFIRMATION_MIN_PROGRESS_ATR_MULTIPLE = 0.2

# --- Let winners run (lever #2) --- RETIRED FROM LIVE USE 2026-08-17.
# Instead of capping every trade at a fixed reward:risk multiple, the trade
# moves to breakeven once it's ahead by BREAKEVEN_TRIGGER_R, then trails
# behind price by TRAIL_ATR_MULTIPLE x the 5M ATR once ahead by
# TRAIL_TRIGGER_R. HARD_CAP_R_MULTIPLE was a wide safety-net take-profit far
# beyond normal expectations, back when the trailing stop was what actually
# closed most winning trades.
# Superseded by FIXED_RR_MULTIPLE below: a 60-day backtest (H4 bias,
# London-only, spread guard 1.2x, 0.5 pip slippage, GBPUSD+USDJPY) showed a
# static 1.5R take-profit beating this trailing system on every metric —
# win rate 53.0% vs 47.4%, P&L +$3,754.52 vs +$2,537.44, equal drawdown.
# Kept here (and executor.manage_trailing_stops still exists) only so
# backtest.py's trailing-mode comparison path keeps working — main.py no
# longer calls manage_trailing_stops() while FIXED_RR_ENABLED is True.
BREAKEVEN_TRIGGER_R = 1.0
TRAIL_TRIGGER_R = 1.5
TRAIL_ATR_MULTIPLE = 1.0   # fallback for any symbol not listed below
HARD_CAP_R_MULTIPLE = 6.0   # only takes effect if FIXED_RR_ENABLED is False

# --- Fixed take-profit (lever #2 replacement, live since 2026-08-17) ---
# Every signal's take_profit is entry +/- FIXED_RR_MULTIPLE x risk instead of
# the wide HARD_CAP_R_MULTIPLE safety net, and the position is left alone
# after entry — no breakeven, no trailing. The SL+TP bracket placed at entry
# is what closes the trade. See the backtest numbers in the comment above.
FIXED_RR_ENABLED = True
FIXED_RR_MULTIPLE = 1.5

# XAUUSD has underperformed USTEC/GBPUSD across three separate tests even
# after the confirmation-strength and trailing-stop fixes helped the other
# two. Gold's bigger, faster swings likely need more room before the trail
# starts working, rather than getting clipped by the same 1.0x ATR that
# suits forex/index instruments.
SYMBOL_TRAIL_ATR_MULTIPLE = {
    "XAUUSD": 1.5,
}

# --- Signal quality score (lever #3: reject weak setups outright, not just invalid ones) ---
# confidence_score is a composite of three components, each 0-1, averaged:
#   - HTF trend strength (ADX above the MIN_ADX floor, scaled up to ADX 40)
#   - sweep depth (how far price wicked past the swing level, in ATR units)
#   - confirmation strength (how far past the minimum required progress the
#     confirmation candle actually went)
# Setups scoring below MIN_CONFIDENCE_SCORE are rejected even though they
# technically passed every other filter — this is the "some valid setups
# are still worse than others" lever the earlier filters didn't have.
# History: 0.40 (original) -> 0.35 on 2026-08-21 (tested standalone without
# the exhaustion filter first and that was clearly worse/breached -- only
# works paired WITH EXHAUSTION_FILTER_ENABLED) -> 0.30 on 2026-08-24 after a
# full sweep (0.20/0.25/0.30/0.35/0.40, 60d, full current basket config).
# 0.20 confirmed too low (worst win rate of the sweep AND breached the
# drawdown cap) -- that's the floor. 0.30 beat every other value on BOTH
# axes at once, not a tradeoff: highest P&L of the batch (+$19,096.19) AND
# lower max drawdown than 0.35 had ($2,386.55 vs $2,837.74). Profit/drawdown
# ratio ~8.0x, clearly the best of the five tested. Don't lower below 0.25
# without re-confirming against the drawdown cap -- 0.20 already showed
# where it breaks.
MIN_CONFIDENCE_SCORE = 0.30
# CHANGED 2026-08-31: per-symbol overrides, from individually tuning each
# of the 4 current-basket symbols standalone (60d each) after the basket-
# wide value stopped working for any of them. A symbol not listed here uses
# MIN_CONFIDENCE_SCORE above. See SYMBOL_EXHAUSTION_LOOKBACK_OVERRIDE and
# SYMBOL_STRUCTURE_LOOKBACK_OVERRIDE below -- all three were tuned together
# per symbol, not independently, so don't change one without re-checking
# the other two for that symbol.
SYMBOL_MIN_CONFIDENCE_OVERRIDE = {
    "USDCAD": 0.35,   # 49 trades, 69.4% WR, +$12,549.26 (with exh_lb=3, structure_lb=8)
    "USDJPY": 0.30,   # 65 trades, 58.5% WR, +$7,816.49 (with exh_lb=10, structure_lb=10)
    "GBPUSD": 0.35,   # 44 trades, 54.5% WR, +$3,469.64 (with exh_lb=3, structure_lb=15)
    "XAUUSD": 0.25,   # 27 trades, 51.9% WR, +$2,841.84 (with exh_lb=3, structure_lb=3)
}

# --- H4 bias confluence (lever #4: agree with the bigger trend, not just M5) ---
# Requires H4 EMA9/21 direction to agree with the trade direction before a
# signal is allowed through — on top of, not instead of, the existing M5
# bias/M1 sweep/confidence-score checks. Backtested per-symbol (60 days,
# ~0.5 pip slippage assumed on stop-loss exits): GBPUSD -$4,184 -> +$715,
# USDJPY -$2,717 -> +$2,165, both also clearing their prior max-drawdown
# breach. EURUSD never turned profitable under this filter either (that's
# why it was dropped from SYMBOLS above, not swapped for another symbol).
#
# An H1 swap was tried (2026-08-14/15) and rejected: with slippage actually
# simulated in backtest.py (not just assumed), H1 went from +$5,871/60d
# (zero slippage) to -$3,776.73/60d at just 0.5 pips — and tripped the
# buffered max-drawdown breach, permanently halting the guard partway
# through the window. The same 0.5-pip run on H4 stayed profitable
# (+$2,732.39/60d, no breach). Tightening the stop floor (MIN_STOP_ATR_MULTIPLE
# 0.75 -> 0.5) did not rescue H1 under slippage — confirms H1's EMA9/21 flips
# too often relative to the entry timeframe, not a stop-sizing problem. See
# backtest_results.csv runs from that date for the raw trade logs.
H4_BIAS_FILTER_ENABLED = True
H4_BIAS_MIN_BARS = 25   # EMA21 needs this many H4 candles before it's trustworthy

# --- Liquidity level timeframe override (TEMPORARY, added 2026-08-26 for the
# XAUUSD/H1/M15/M5/M1 trial) ---
# When set (per-symbol, see below), the swing high/low that gets swept is
# computed from THAT timeframe's last LIQUIDITY_LEVEL_LOOKBACK closed
# candles instead of LTF_TIMEFRAME's own -- the sweep/reclaim candle itself
# is still detected on LTF_TIMEFRAME (M1). This wires scanner.py's
# df_liquidity_tf parameter (previously only ever exercised in backtest.py
# via run_backtest's liquidity_level_timeframe arg) into the LIVE path for
# the first time. A symbol not listed here uses its own LTF swing high/low
# as before (the validated London-session basket's behavior, and what makes
# USDCAD trade "under normal rules" alongside XAUUSD's special setup in
# today's basket). REVERT (remove entirely) once the 2026-08-26 XAUUSD
# trial is over, unless separately validated first.
# Empty 2026-08-26: switched XAUUSD from H1-bias+M15-level (3 trades/20d)
# to H1-bias+its-own-M1-level (26 trades/20d) per explicit instruction --
# the M15 override was isolated as the actual volume bottleneck (H1 bias
# alone was MORE permissive than the original H4, not less).
SYMBOL_LIQUIDITY_LEVEL_TIMEFRAME_OVERRIDE = {}
