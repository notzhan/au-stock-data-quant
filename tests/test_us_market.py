import sys
import types
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from io import StringIO
from unittest.mock import patch

import numpy as np
import pandas as pd

from lib import us_market
from lib.ashare import get_price


class USMarketTests(unittest.TestCase):
    def test_symbol_routing(self):
        self.assertEqual(us_market.normalize_us_symbol('aapl'), 'AAPL.US')
        self.assertEqual(us_market.normalize_us_symbol('BRK.B.US'), 'BRK.B.US')
        self.assertTrue(us_market.is_us_symbol('SHY'))
        for code in ('sh600519', 'sz000001', '600519', '000001.XSHG', '00700.HK'):
            self.assertFalse(us_market.is_us_symbol(code), code)
        with patch('lib.us_market.get_us_price', return_value='us-bars') as fetch:
            self.assertEqual(get_price('AAPL', count=30), 'us-bars')
            fetch.assert_called_once_with('AAPL', end_date='', count=30, frequency='1d')

    def test_history_cutoff_and_chronological_ohlcv(self):
        class Period:
            Day = 'day'
            Week = 'week'
            Month = 'month'
            Min_1 = '1m'
            Min_5 = '5m'
            Min_15 = '15m'
            Min_30 = '30m'
            Min_60 = '60m'

        class AdjustType:
            ForwardAdjust = 'forward'

        fake_api = types.ModuleType('longport.openapi')
        fake_api.Period = Period
        fake_api.AdjustType = AdjustType
        fake_pkg = types.ModuleType('longport')
        fake_pkg.openapi = fake_api

        def bar(day, close):
            return types.SimpleNamespace(
                timestamp=datetime(2025, 1, day, 21, tzinfo=timezone.utc),
                open=close - 1, close=close, high=close + 1, low=close - 2,
                volume=100 * day,
            )

        class Context:
            def history_candlesticks_by_offset(self, *args):
                self.args = args
                return [bar(3, 13), bar(2, 12), bar(1, 11)]

        ctx = Context()
        with patch.dict(sys.modules, {'longport': fake_pkg, 'longport.openapi': fake_api}), \
             patch('lib.us_market._quote_context', return_value=ctx):
            df = us_market.get_us_price('aapl', end_date='2025-01-02', count=3)
        self.assertEqual(ctx.args[:5], ('AAPL.US', 'day', 'forward', False, 3))
        self.assertEqual(list(df['close']), [11.0, 12.0])
        self.assertEqual(df.index[0].strftime('%Y-%m-%d %H:%M'), '2025-01-01 16:00')
        self.assertEqual(list(df.columns), ['open', 'close', 'high', 'low', 'volume'])

    def test_missing_sdk_has_actionable_error(self):
        with patch.dict(sys.modules, {'longport': None}):
            with self.assertRaisesRegex(RuntimeError, 'pip install longport'):
                us_market.get_us_price('AAPL')

    def test_naive_sdk_minute_timestamp_is_beijing_time(self):
        class Period:
            Day = 'day'
            Week = 'week'
            Month = 'month'
            Min_1 = '1m'
            Min_5 = '5m'
            Min_15 = '15m'
            Min_30 = '30m'
            Min_60 = '60m'
        class AdjustType:
            ForwardAdjust = 'forward'
        fake_api = types.ModuleType('longport.openapi')
        fake_api.Period = Period
        fake_api.AdjustType = AdjustType
        fake_pkg = types.ModuleType('longport')
        fake_pkg.openapi = fake_api
        bar = types.SimpleNamespace(
            timestamp=datetime(2026, 9, 18, 3, 55),
            open=100, close=101, high=102, low=99, volume=500,
        )
        ctx = types.SimpleNamespace(candlesticks=lambda *args: [bar])
        with patch.dict(sys.modules, {'longport': fake_pkg, 'longport.openapi': fake_api}), \
             patch('lib.us_market._quote_context', return_value=ctx):
            df = us_market.get_us_price('MU', count=1, frequency='5m')
        self.assertEqual(df.index[-1].strftime('%Y-%m-%d %H:%M'), '2026-09-17 15:55')

    def test_us_quote_uses_share_volume(self):
        quote = types.SimpleNamespace(
            symbol='AAPL.US', last_done=105, prev_close=100,
            high=106, low=99, volume=12345,
            timestamp=datetime(2025, 1, 2, 21, tzinfo=timezone.utc),
        )
        ctx = types.SimpleNamespace(quote=lambda symbols: [quote])
        with patch('lib.us_market._quote_context', return_value=ctx):
            result = us_market.get_us_quotes(['aapl'])[0]
        self.assertEqual(result['code'], 'AAPL.US')
        self.assertEqual(result['percent'], 5)
        self.assertEqual(result['volume_unit'], '股')

    def test_us_analysis_runs_with_market_neutral_bars(self):
        from bin import quant

        close = np.linspace(100, 150, 260) + np.sin(np.arange(260) / 8)
        df = pd.DataFrame({
            'open': close - 0.5, 'close': close, 'high': close + 1,
            'low': close - 1, 'volume': np.full(260, 1000.0),
        }, index=pd.date_range('2025-01-01', periods=260, freq='B'))
        args = types.SimpleNamespace(
            code='AAPL.US', count=260, period='1d', end='', capital=100000,
            stop_loss=None, take_profit=None, html=False,
        )
        output = StringIO()
        with patch.object(quant, '_fetch', return_value=df), redirect_stdout(output):
            quant.cmd_analyze(args)
        self.assertIn('美元', output.getvalue())
        self.assertIn('综合评分', output.getvalue())


if __name__ == '__main__':
    unittest.main()
