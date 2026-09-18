"""LongPort read-only US market data adapter for the quant engine."""

import re
from datetime import datetime, time
from pathlib import Path

import pandas as pd
from lib.env_file import load_env_file


_US_SYMBOL = re.compile(r"^[A-Z][A-Z0-9]*(?:[.-][A-Z0-9]+)*$")


def is_us_symbol(code):
    """Recognize US tickers without mistaking Chinese exchange codes for them."""
    value = str(code).strip().upper()
    if value.endswith('.US'):
        value = value[:-3]
    elif '.' in value and value.endswith(('.XSHG', '.XSHE', '.HK')):
        return False
    return bool(_US_SYMBOL.fullmatch(value)) and not value.isdigit() and not re.fullmatch(r'(SH|SZ)\d{6}', value)


def normalize_us_symbol(code):
    if not is_us_symbol(code):
        raise ValueError(f"无效的美股代码: {code}（示例: AAPL 或 AAPL.US）")
    value = str(code).strip().upper()
    return value if value.endswith('.US') else value + '.US'


def _quote_context():
    load_env_file(Path(__file__).resolve().parent.parent / '.env')
    try:
        from longport.openapi import Config, QuoteContext
    except ImportError as exc:
        raise RuntimeError("美股行情需要 LongPort SDK，请安装: pip install longport>=3.0.23") from exc
    try:
        config = Config.from_apikey_env() if hasattr(Config, 'from_apikey_env') else Config.from_env()
        return QuoteContext(config)
    except Exception as exc:
        raise RuntimeError("LongPort 连接失败；请配置 LONGPORT_APP_KEY、LONGPORT_APP_SECRET、LONGPORT_ACCESS_TOKEN") from exc


def get_us_price(code, end_date='', count=10, frequency='1d'):
    """Return regular-session OHLCV bars in the same schema as Ashare."""
    try:
        from longport.openapi import AdjustType, Period
    except ImportError as exc:
        raise RuntimeError("美股行情需要 LongPort SDK，请安装: pip install longport>=3.0.23") from exc

    periods = {
        '1d': Period.Day, '1w': Period.Week, '1M': Period.Month,
        '1m': Period.Min_1, '5m': Period.Min_5, '15m': Period.Min_15,
        '30m': Period.Min_30, '60m': Period.Min_60,
    }
    if frequency not in periods:
        raise ValueError(f"不支持的美股 K 线周期: {frequency}")
    if not 1 <= count <= 1000:
        raise ValueError("LongPort 单次 K 线请求支持 1–1000 条")

    symbol = normalize_us_symbol(code)
    ctx = _quote_context()
    if end_date:
        cutoff = datetime.combine(pd.Timestamp(end_date).date(), time.max)
        bars = ctx.history_candlesticks_by_offset(
            symbol, periods[frequency], AdjustType.ForwardAdjust, False, count, cutoff
        )
    else:
        bars = ctx.candlesticks(symbol, periods[frequency], count, AdjustType.ForwardAdjust)

    rows = [{
        'time': bar.timestamp,
        'open': float(bar.open), 'close': float(bar.close),
        'high': float(bar.high), 'low': float(bar.low),
        'volume': float(bar.volume),
    } for bar in bars]
    df = pd.DataFrame(rows, columns=['time', 'open', 'close', 'high', 'low', 'volume'])
    if df.empty:
        return df.set_index('time')
    timestamps = pd.DatetimeIndex(pd.to_datetime(df.pop('time')))
    if timestamps.tz is None:
        # LongPort's Python SDK returns naive timestamps in Asia/Shanghai.
        timestamps = timestamps.tz_localize('Asia/Shanghai')
    timestamps = timestamps.tz_convert('America/New_York').tz_localize(None)
    df.index = timestamps
    df.index.name = ''
    if end_date:
        df = df[df.index.date <= cutoff.date()]
    return df.sort_index().tail(count)


def get_us_quotes(codes):
    """Read US quotes without creating a trading context."""
    symbols = [normalize_us_symbol(code) for code in codes]
    quotes = _quote_context().quote(symbols)
    result = []
    for quote in quotes:
        last = float(quote.last_done)
        previous = float(quote.prev_close)
        timestamp = pd.Timestamp(quote.timestamp)
        if timestamp.tz is None:
            timestamp = timestamp.tz_localize('Asia/Shanghai')
        market_time = timestamp.tz_convert('America/New_York').strftime('%Y-%m-%d %H:%M:%S %Z')
        result.append({
            'code': quote.symbol, 'name': quote.symbol, 'now': last,
            'percent': (last - previous) / previous * 100 if previous else 0,
            'high': float(quote.high), 'low': float(quote.low),
            'yesterday': previous, 'volume': float(quote.volume), 'volume_unit': '股',
            'time': market_time,
        })
    return result
