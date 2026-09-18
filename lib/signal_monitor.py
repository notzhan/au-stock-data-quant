"""Scan completed US bars and deliver deduplicated strategy alerts."""

import json
import os
import tempfile
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from lib.ashare import get_price
from lib.strategies import STRATEGY_MAP, strategy_ensemble
from lib.us_market import normalize_us_symbol


NEW_YORK = ZoneInfo('America/New_York')
# The longest built-in warm-up is MACD(12, 26, 9); 40 bars also covers MA20.
MINIMUM_BARS = 40
INTRADAY_MINIMUM_BARS = 30


def evaluate_intraday(symbol, now=None):
    """Five-minute EMA9/EMA21 up-cross confirmed above today's regular VWAP."""
    symbol = normalize_us_symbol(symbol)
    now = (now or datetime.now(NEW_YORK)).astimezone(NEW_YORK)
    if now.weekday() >= 5 or not time(9, 35) <= now.time() < time(16):
        return None

    df = get_price(symbol, count=450, frequency='5m')
    if df.empty:
        return None
    # SDK bars are stamped at their start. Exclude the changing bar and all
    # pre/post-market records even if the data provider includes them.
    regular = df[(df.index.time >= time(9, 30)) & (df.index.time < time(16))]
    cutoff = now.replace(tzinfo=None) - timedelta(minutes=5, seconds=10)
    regular = regular[regular.index <= cutoff]
    if len(regular) < INTRADAY_MINIMUM_BARS:
        return None
    bar_start = regular.index[-1]
    bar_end = bar_start + timedelta(minutes=5)
    if bar_start.date() != now.date() or now.replace(tzinfo=None) - bar_end > timedelta(minutes=3):
        return None

    close = regular['close'].astype(float)
    ema9 = close.ewm(span=9, adjust=False).mean()
    ema21 = close.ewm(span=21, adjust=False).mean()
    today = regular[regular.index.date == now.date()]
    volume = today['volume'].astype(float)
    if volume.sum() <= 0:
        return None
    typical = (today['high'] + today['low'] + today['close']) / 3
    vwap = float((typical * volume).sum() / volume.sum())
    price = float(close.iloc[-1])
    crossed_up = ema9.iloc[-2] <= ema21.iloc[-2] and ema9.iloc[-1] > ema21.iloc[-1]
    crossed_down = ema9.iloc[-2] >= ema21.iloc[-2] and ema9.iloc[-1] < ema21.iloc[-1]
    if crossed_up and price > vwap:
        side = 'buy'
        rules = ['EMA9 上穿 EMA21', '收盘价高于当日 VWAP']
    elif crossed_down and price < vwap:
        side = 'sell'
        rules = ['EMA9 下穿 EMA21', '收盘价低于当日 VWAP']
    else:
        return None
    beijing = bar_end.replace(tzinfo=NEW_YORK).astimezone(ZoneInfo('Asia/Shanghai'))
    return {
        'symbol': symbol, 'strategy': 'intraday', 'side': side, 'period': '5m',
        'bar_date': bar_start.isoformat(timespec='minutes'),
        'signal_time': bar_end.strftime('%Y-%m-%d %H:%M'),
        'beijing_time': beijing.strftime('%Y-%m-%d %H:%M'),
        'close': price, 'vwap': vwap,
        'votes': rules, 'min_agree': None,
    }


def evaluate(symbol, strategy='ensemble', min_agree=3, now=None, count=120):
    """Return a buy alert for the latest completed, fresh daily bar, or None."""
    if strategy == 'intraday':
        return evaluate_intraday(symbol, now=now)
    symbol = normalize_us_symbol(symbol)
    if strategy not in STRATEGY_MAP or strategy == 'buy_hold':
        raise ValueError(f'不支持监控策略: {strategy}')
    if not 1 <= min_agree <= 5:
        raise ValueError('min_agree 必须在 1–5 之间')
    now = (now or datetime.now(NEW_YORK)).astimezone(NEW_YORK)
    df = get_price(symbol, count=count, frequency='1d')
    if len(df) < MINIMUM_BARS:
        raise ValueError(f'{symbol} 只有 {len(df)} 根日 K，至少需要 {MINIMUM_BARS} 根')

    # Candlesticks may contain today's changing bar before the regular close.
    df = df[df.index.date < now.date()] if now.time() < time(16, 10) else df[df.index.date <= now.date()]
    if len(df) < MINIMUM_BARS:
        return None
    bar_day = df.index[-1].date()
    close_at = datetime.combine(bar_day, time(16), tzinfo=NEW_YORK)
    age = now - close_at
    if age < timedelta(minutes=10) or age > timedelta(hours=36):
        return None

    signal = (strategy_ensemble(df, min_agree=min_agree) if strategy == 'ensemble'
              else STRATEGY_MAP[strategy](df))
    if int(signal[-1]) != 1:
        return None
    votes = [name for name in ('ma_cross', 'macd', 'rsi', 'boll', 'kdj')
             if int(STRATEGY_MAP[name](df)[-1]) == 1]
    return {
        'symbol': symbol, 'strategy': strategy, 'side': 'buy', 'bar_date': bar_day.isoformat(),
        'close': float(df['close'].iloc[-1]), 'votes': votes,
        'min_agree': min_agree if strategy == 'ensemble' else None,
    }


def alert_text(alert):
    if alert['strategy'] == 'intraday':
        is_sell = alert.get('side') == 'sell'
        title = '美股盘中卖出指标' if is_sell else '美股盘中买入信号'
        caution = ('技术转弱提醒，未核对你的持仓和成本；请结合实时价及交易计划判断。'
                   if is_sell else
                   '技术信号未经收益验证，不保证后续上涨或 5% 收益；请核对实时价和交易成本。')
        return (f"{title} · {alert['symbol']}\n"
                f"美东时间：{alert['signal_time']}（5 分钟 K 收盘确认）\n"
                f"北京时间：{alert['beijing_time']}\n"
                f"信号价：${alert['close']:.2f}，当日 VWAP：${alert['vwap']:.2f}\n"
                f"条件：{', '.join(alert['votes'])}\n"
                f"{caution}")
    label = f"ensemble ≥{alert['min_agree']}" if alert['strategy'] == 'ensemble' else alert['strategy']
    return (f"美股日线买入信号 · {alert['symbol']}\n"
            f"美东交易日：{alert['bar_date']}（收盘确认）\n"
            f"前复权收盘价：${alert['close']:.2f}\n"
            f"触发策略：{label}\n"
            f"同日买入规则：{', '.join(alert['votes']) or '无'}\n"
            "技术信号仅供参考，不保证后续收益；请核对行情和交易成本。")


def load_state(path):
    path = Path(path)
    if not path.exists():
        return {}
    with path.open(encoding='utf-8') as file:
        data = json.load(file)
    if not isinstance(data, dict):
        raise ValueError(f'监控状态文件格式无效: {path}')
    return data


def save_state(path, state):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix='.monitor-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as file:
            os.fchmod(file.fileno(), 0o600)
            json.dump(state, file, ensure_ascii=False, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def scan(symbols, strategy, min_agree, state_path, notify, now=None, dry_run=False,
         sides=('buy', 'sell')):
    """Scan every symbol; keep failed alerts eligible for a later retry."""
    state = load_state(state_path)
    results = []
    for raw_symbol in symbols:
        symbol = normalize_us_symbol(raw_symbol)
        try:
            alert = evaluate(symbol, strategy, min_agree, now=now)
            if alert is None:
                results.append((symbol, '无新信号'))
                continue
            if alert.get('side', 'buy') not in sides:
                results.append((symbol, '信号类型已关闭'))
                continue
            key = f"{symbol}|{strategy}|{min_agree if strategy == 'ensemble' else '-'}"
            if state.get(key) == alert['bar_date']:
                results.append((symbol, '已通知过'))
                continue
            notify(alert_text(alert))
            if not dry_run:
                state[key] = alert['bar_date']
                save_state(state_path, state)
            results.append((symbol, '试运行触发' if dry_run else '已通知'))
        except Exception as exc:
            results.append((symbol, f'失败: {exc}'))
    return results
