import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from lib import signal_monitor
from bin import monitor


NY = ZoneInfo('America/New_York')


def bars():
    dates = pd.bdate_range('2026-06-01', '2026-09-18')
    closes = np.linspace(100, 120, len(dates))
    return pd.DataFrame({
        'open': closes, 'close': closes, 'high': closes + 1,
        'low': closes - 1, 'volume': np.full(len(dates), 1000),
    }, index=dates + pd.Timedelta(hours=16))


class SignalMonitorTests(unittest.TestCase):
    def test_current_daily_bar_is_ignored_until_after_close(self):
        df = bars()
        fake_signal = np.zeros(len(df))
        fake_signal[-1] = 1
        fake_signal[-2] = 1
        with patch('lib.signal_monitor.get_price', return_value=df), \
             patch.dict(signal_monitor.STRATEGY_MAP, {'macd': lambda frame: fake_signal[-len(frame):]}):
            before = signal_monitor.evaluate('MU', 'macd', now=datetime(2026, 9, 18, 15, 30, tzinfo=NY))
            after = signal_monitor.evaluate('MU', 'macd', now=datetime(2026, 9, 18, 16, 15, tzinfo=NY))
        self.assertEqual(before['bar_date'], '2026-09-17')
        self.assertEqual(after['bar_date'], '2026-09-18')

    def test_stale_bars_do_not_alert(self):
        df = bars()
        with patch('lib.signal_monitor.get_price', return_value=df):
            self.assertIsNone(signal_monitor.evaluate('MU', 'macd', now=datetime(2026, 9, 22, 17, tzinfo=NY)))

    def test_recent_listing_with_49_bars_can_be_monitored(self):
        df = bars().tail(49)
        with patch('lib.signal_monitor.get_price', return_value=df), \
             patch.dict(signal_monitor.STRATEGY_MAP, {'macd': lambda frame: np.r_[np.zeros(len(frame) - 1), 1]}):
            alert = signal_monitor.evaluate('SKHY', 'macd', now=datetime(2026, 9, 18, 16, 15, tzinfo=NY))
        self.assertEqual(alert['symbol'], 'SKHY.US')

    def test_intraday_uses_only_closed_regular_session_bars(self):
        prior = pd.date_range('2026-09-17 09:30', periods=30, freq='5min')
        current = pd.to_datetime(['2026-09-18 09:30', '2026-09-18 09:35', '2026-09-18 09:40'])
        idx = prior.append(current)
        closes = np.r_[np.full(31, 100.0), 101.0, 120.0]
        df = pd.DataFrame({'open': closes, 'close': closes, 'high': closes,
                           'low': closes, 'volume': np.full(len(idx), 1000)}, index=idx)
        # Extended-hours records must not affect EMA or VWAP.
        extra = df.tail(1).copy()
        extra.index = pd.to_datetime(['2026-09-18 08:00'])
        extra['close'] = 1000.0
        df = pd.concat([df, extra]).sort_index()
        with patch('lib.signal_monitor.get_price', return_value=df):
            alert = signal_monitor.evaluate('MU', 'intraday', now=datetime(2026, 9, 18, 9, 41, tzinfo=NY))
            before_close = signal_monitor.evaluate('MU', 'intraday', now=datetime(2026, 9, 18, 9, 39, tzinfo=NY))
            after_hours = signal_monitor.evaluate('MU', 'intraday', now=datetime(2026, 9, 18, 16, 1, tzinfo=NY))
        self.assertEqual(alert['symbol'], 'MU.US')
        self.assertEqual(alert['side'], 'buy')
        self.assertEqual(alert['close'], 101.0)
        self.assertEqual(alert['signal_time'], '2026-09-18 09:40')
        self.assertEqual(alert['beijing_time'], '2026-09-18 21:40')
        self.assertIsNone(before_close)
        self.assertIsNone(after_hours)

    def test_intraday_sell_signal_is_labeled_and_can_be_filtered(self):
        prior = pd.date_range('2026-09-17 09:30', periods=30, freq='5min')
        current = pd.to_datetime(['2026-09-18 09:30', '2026-09-18 09:35'])
        idx = prior.append(current)
        closes = np.r_[np.full(31, 100.0), 99.0]
        df = pd.DataFrame({'open': closes, 'close': closes, 'high': closes,
                           'low': closes, 'volume': np.full(len(idx), 1000)}, index=idx)
        now = datetime(2026, 9, 18, 9, 41, tzinfo=NY)
        with patch('lib.signal_monitor.get_price', return_value=df):
            alert = signal_monitor.evaluate('MU', 'intraday', now=now)
        self.assertEqual(alert['side'], 'sell')
        self.assertIn('EMA9 下穿 EMA21', alert['votes'])
        self.assertIn('卖出指标', signal_monitor.alert_text(alert))

        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / 'state.json'
            sent = []
            with patch('lib.signal_monitor.evaluate', return_value=alert):
                ignored = signal_monitor.scan(['MU'], 'intraday', 3, state, sent.append,
                                              sides=('buy',))
                notified = signal_monitor.scan(['MU'], 'intraday', 3, state, sent.append,
                                               sides=('buy', 'sell'))
            self.assertEqual(ignored[0][1], '信号类型已关闭')
            self.assertEqual(notified[0][1], '已通知')
            self.assertEqual(len(sent), 1)

    def test_intraday_new_bar_can_alert_again_but_same_bar_is_deduped(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / 'state.json'
            alert = {'symbol': 'MU.US', 'strategy': 'intraday', 'bar_date': '2026-09-18T09:35',
                     'signal_time': '2026-09-18 09:40', 'beijing_time': '2026-09-18 21:40',
                     'close': 101.0, 'vwap': 100.5,
                     'votes': ['EMA9 上穿 EMA21', '收盘价高于当日 VWAP'], 'min_agree': None}
            sent = []
            with patch('lib.signal_monitor.evaluate', return_value=alert):
                first = signal_monitor.scan(['MU'], 'intraday', 3, state, sent.append)
                second = signal_monitor.scan(['MU'], 'intraday', 3, state, sent.append)
                alert['bar_date'] = '2026-09-18T10:10'
                third = signal_monitor.scan(['MU'], 'intraday', 3, state, sent.append)
            self.assertEqual([first[0][1], second[0][1], third[0][1]],
                             ['已通知', '已通知过', '已通知'])
            self.assertEqual(len(sent), 2)

    def test_multisymbol_dedup_and_retry_after_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / 'state.json'
            def alert(symbol, strategy, min_agree, now=None):
                return {'symbol': symbol, 'strategy': strategy, 'bar_date': '2026-09-18',
                        'close': 100.0, 'votes': ['macd'], 'min_agree': None}

            delivered = []
            def send(message):
                if 'MRVL.US' in message and not delivered:
                    raise RuntimeError('send failed')
                delivered.append(message)

            with patch('lib.signal_monitor.evaluate', side_effect=alert):
                first = signal_monitor.scan(['MRVL', 'MU'], 'macd', 3, state, send)
                second = signal_monitor.scan(['MRVL', 'MU'], 'macd', 3, state, send)
            self.assertEqual(first[0][1], '失败: send failed')
            self.assertEqual(first[1][1], '已通知')
            self.assertEqual(second[0][1], '已通知')
            self.assertEqual(second[1][1], '已通知过')
            self.assertEqual(len(delivered), 2)

    def test_dry_run_does_not_change_state(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / 'state.json'
            alert = {'symbol': 'MU.US', 'strategy': 'macd', 'bar_date': '2026-09-18',
                     'close': 100.0, 'votes': ['macd'], 'min_agree': None}
            with patch('lib.signal_monitor.evaluate', return_value=alert):
                result = signal_monitor.scan(['MU'], 'macd', 3, state, lambda _: None, dry_run=True)
            self.assertEqual(result[0][1], '试运行触发')
            self.assertFalse(state.exists())

    def test_bark_notification_payload_and_rejection(self):
        secret_url = 'https://api.day.app/test-secret'
        with patch.dict('os.environ', {'BARK_URL': secret_url}):
            send = monitor.notifier('bark')
        response = unittest.mock.Mock()
        response.json.return_value = {'code': 200}
        with patch('bin.monitor.requests.post', return_value=response) as post:
            send('测试标题\n测试正文')
        self.assertEqual(post.call_args.args[0], 'https://api.day.app/push')
        self.assertEqual(post.call_args.kwargs['json']['device_key'], 'test-secret')
        self.assertEqual(post.call_args.kwargs['json']['title'], '测试标题')
        self.assertEqual(post.call_args.kwargs['json']['body'], '测试正文')

        response.json.return_value = {'code': 400}
        with patch('bin.monitor.requests.post', return_value=response):
            with self.assertRaisesRegex(RuntimeError, 'Bark 拒绝消息'):
                send('测试标题\n测试正文')


if __name__ == '__main__':
    unittest.main()
