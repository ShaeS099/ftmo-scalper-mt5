"""
Handles everything that touches the MT5 terminal directly:
login, account info, open positions, and OHLCV candle retrieval.
"""
import logging
import MetaTrader5 as mt5
import pandas as pd

import config

logger = logging.getLogger("mt5_connector")

TIMEFRAME_MAP = {
    "M1": mt5.TIMEFRAME_M1,
    "M5": mt5.TIMEFRAME_M5,
    "M15": mt5.TIMEFRAME_M15,
    "M30": mt5.TIMEFRAME_M30,
    "H1": mt5.TIMEFRAME_H1,
    "H4": mt5.TIMEFRAME_H4,
}


class MT5Connector:
    def __init__(self):
        self.connected = False

    def connect(self) -> bool:
        kwargs = {}
        if config.MT5_TERMINAL_PATH:
            kwargs["path"] = config.MT5_TERMINAL_PATH

        if not mt5.initialize(**kwargs):
            logger.error("mt5.initialize() failed: %s", mt5.last_error())
            return False

        authorized = mt5.login(
            login=config.MT5_LOGIN,
            password=config.MT5_PASSWORD,
            server=config.MT5_SERVER,
        )
        if not authorized:
            logger.error("mt5.login() failed: %s", mt5.last_error())
            mt5.shutdown()
            return False

        self.connected = True
        logger.info("Connected to MT5 as login %s on %s", config.MT5_LOGIN, config.MT5_SERVER)
        return True

    def shutdown(self):
        mt5.shutdown()
        self.connected = False

    def get_account_info(self):
        info = mt5.account_info()
        if info is None:
            logger.error("account_info() returned None: %s", mt5.last_error())
            return None
        return info

    def get_open_positions(self, symbol: str = None):
        positions = mt5.positions_get(symbol=symbol) if symbol else mt5.positions_get()
        return list(positions) if positions is not None else []

    def get_candles(self, symbol: str, timeframe_key: str, num_candles: int = 200) -> pd.DataFrame | None:
        tf = TIMEFRAME_MAP.get(timeframe_key)
        if tf is None:
            raise ValueError(f"Unsupported timeframe: {timeframe_key}")

        # pos=0 is the currently-forming candle, not a closed one — fetch one
        # extra and drop it so the strategy only ever sees closed bars, same
        # as backtest.py's copy_rates_range (which can't return a bar that
        # hasn't closed yet for a past date range). Without this, live can
        # react to a sweep/reclaim that's still forming and may not hold once
        # the candle actually closes — a signal the backtest can never see or
        # reproduce.
        rates = mt5.copy_rates_from_pos(symbol, tf, 0, num_candles + 1)
        if rates is None or len(rates) == 0:
            logger.warning("No candle data returned for %s %s", symbol, timeframe_key)
            return None

        df = pd.DataFrame(rates)
        df["time"] = pd.to_datetime(df["time"], unit="s")
        return df.iloc[:-1].reset_index(drop=True)

    def get_symbol_info(self, symbol: str):
        info = mt5.symbol_info(symbol)
        if info is None:
            logger.error("symbol_info(%s) returned None", symbol)
            return None
        if not info.visible:
            mt5.symbol_select(symbol, True)
        return info

    def get_current_spread_points(self, symbol: str) -> float | None:
        tick = mt5.symbol_info_tick(symbol)
        info = mt5.symbol_info(symbol)
        if tick is None or info is None or info.point == 0:
            return None
        return (tick.ask - tick.bid) / info.point
