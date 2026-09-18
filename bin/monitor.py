#!/usr/bin/env python3
"""One-shot US intraday/daily signal scanner, intended for a systemd timer."""

import argparse
import fcntl
import os
import smtplib
import sys
from datetime import datetime, time
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from lib.env_file import load_env_file

STRATEGIES = ('intraday', 'ma_cross', 'macd', 'rsi', 'boll', 'kdj', 'ensemble')


def notifier(channel):
    if channel == 'bark':
        bark_url = os.environ.get('BARK_URL', '')
        parsed = urlparse(bark_url)
        if (parsed.scheme != 'https' or parsed.hostname != 'api.day.app'
                or parsed.port is not None or parsed.username or parsed.password
                or parsed.query or parsed.fragment or len(parsed.path.strip('/').split('/')) != 1
                or not parsed.path.strip('/')):
            raise ValueError('BARK_URL 应为 https://api.day.app/设备密钥')
        device_key = parsed.path.strip('/')
        def send(message):
            title, _, body = message.partition('\n')
            try:
                response = requests.post('https://api.day.app/push', json={
                    'device_key': device_key, 'title': title, 'body': body or title,
                    'group': '股票监控',
                }, timeout=15)
                response.raise_for_status()
                if response.json().get('code') not in (0, 200):
                    raise RuntimeError('Bark 拒绝消息')
            except (requests.RequestException, ValueError):
                raise RuntimeError('Bark 通知请求失败') from None
        return send
    if channel == 'wecom':
        url = os.environ.get('WECHAT_WEBHOOK_QUANT')
        if not url:
            raise ValueError('缺少 WECHAT_WEBHOOK_QUANT')
        def send(message):
            try:
                response = requests.post(url, json={'msgtype': 'text', 'text': {'content': message}}, timeout=15)
                response.raise_for_status()
                if response.json().get('errcode') != 0:
                    raise RuntimeError('企业微信拒绝消息')
            except requests.RequestException:
                raise RuntimeError('企业微信通知请求失败') from None
        return send
    if channel == 'dingtalk':
        url = os.environ.get('DINGTALK_WEBHOOK_QUANT')
        if not url:
            raise ValueError('缺少 DINGTALK_WEBHOOK_QUANT')
        def send(message):
            try:
                response = requests.post(url, json={'msgtype': 'text', 'text': {'content': message}}, timeout=15)
                response.raise_for_status()
                if response.json().get('errcode') != 0:
                    raise RuntimeError('钉钉拒绝消息')
            except requests.RequestException:
                raise RuntimeError('钉钉通知请求失败') from None
        return send
    if channel == 'telegram':
        token = os.environ.get('TELEGRAM_BOT_TOKEN')
        chat_id = os.environ.get('TELEGRAM_CHAT_ID')
        if not token or not chat_id:
            raise ValueError('缺少 TELEGRAM_BOT_TOKEN 或 TELEGRAM_CHAT_ID')
        def send(message):
            try:
                response = requests.post(f'https://api.telegram.org/bot{token}/sendMessage',
                                         json={'chat_id': chat_id, 'text': message}, timeout=15)
                response.raise_for_status()
                if response.json().get('ok') is not True:
                    raise RuntimeError('Telegram 拒绝消息')
            except requests.RequestException:
                raise RuntimeError('Telegram 通知请求失败') from None
        return send
    if channel == 'email':
        host = os.environ.get('SMTP_HOST')
        user = os.environ.get('SMTP_USER')
        password = os.environ.get('SMTP_PASSWORD')
        recipient = os.environ.get('ALERT_EMAIL_TO')
        sender = os.environ.get('ALERT_EMAIL_FROM') or user
        if not all((host, user, password, recipient, sender)):
            raise ValueError('邮件通知需 SMTP_HOST、SMTP_USER、SMTP_PASSWORD、ALERT_EMAIL_TO')
        port = int(os.environ.get('SMTP_PORT', '465'))
        def send(message):
            mail = EmailMessage()
            mail['Subject'] = '美股买入信号提醒'
            mail['From'] = sender
            mail['To'] = recipient
            mail.set_content(message)
            try:
                with smtplib.SMTP_SSL(host, port, timeout=15) as smtp:
                    smtp.login(user, password)
                    smtp.send_message(mail)
            except (OSError, smtplib.SMTPException):
                raise RuntimeError('邮件通知发送失败') from None
        return send
    raise ValueError(f'未知通知渠道: {channel}')


def main(argv=None):
    parser = argparse.ArgumentParser(description='美股盘中或日线买入信号监控')
    parser.add_argument('--symbols', help='逗号分隔的美股代码，默认 MU.US')
    parser.add_argument('--strategy', choices=STRATEGIES, default=None)
    parser.add_argument('--sides', help='逗号分隔的信号类型：buy,sell；默认两者都提醒')
    parser.add_argument('--min-agree', type=int, default=3, help='ensemble 至少同时触发的规则数，默认 3')
    parser.add_argument('--channel', choices=['bark', 'wecom', 'dingtalk', 'telegram', 'email'])
    parser.add_argument('--dry-run', action='store_true', help='只打印触发消息，不发送且不记录已通知状态')
    parser.add_argument('--check-config', action='store_true', help='检查凭证和通知配置，不访问网络')
    parser.add_argument('--test-notification', action='store_true', help='发送一条明确标记为测试的通知')
    parser.add_argument('--state', type=Path, default=ROOT / 'cache' / 'monitor-state.json')
    args = parser.parse_args(argv)
    if not 1 <= args.min_agree <= 5:
        parser.error('--min-agree 必须在 1–5 之间')
    load_env_file(ROOT / '.env')
    args.symbols = args.symbols or os.environ.get('MONITOR_SYMBOLS', 'MU.US')
    args.strategy = args.strategy or os.environ.get('MONITOR_STRATEGY', 'intraday')
    if args.strategy not in STRATEGIES:
        parser.error(f'不支持的监控策略: {args.strategy}')
    args.channel = args.channel or os.environ.get('MONITOR_CHANNEL')
    sides = tuple(dict.fromkeys(s.strip().lower() for s in
                                (args.sides or os.environ.get('MONITOR_SIDES', 'buy,sell')).split(',')
                                if s.strip()))
    if not sides or any(s not in ('buy', 'sell') for s in sides):
        parser.error('--sides / MONITOR_SIDES 只支持 buy、sell')
    if not args.dry_run and not args.channel:
        parser.error('发送通知需指定 --channel；仅查看信号使用 --dry-run')
    if (args.strategy == 'intraday' and not args.dry_run and not args.check_config
            and not args.test_notification):
        ny_now = datetime.now(ZoneInfo('America/New_York'))
        if ny_now.weekday() >= 5 or not time(9, 35) <= ny_now.time() < time(16):
            return 0
    from lib.us_market import normalize_us_symbol
    try:
        symbols = list(dict.fromkeys(normalize_us_symbol(s) for s in args.symbols.split(',') if s.strip()))
    except ValueError as exc:
        parser.error(str(exc))
    if not symbols:
        parser.error('--symbols 不能为空')
    try:
        send = print if args.dry_run else notifier(args.channel)
    except ValueError as exc:
        parser.error(str(exc))
    if args.check_config:
        if not all(os.environ.get(key) for key in
                   ('LONGPORT_APP_KEY', 'LONGPORT_APP_SECRET', 'LONGPORT_ACCESS_TOKEN')):
            parser.error('缺少 LongPort 行情凭证')
        print(f'配置已就绪: {len(symbols)} 只股票，策略 {args.strategy}，信号 {",".join(sides)}，通知渠道 {args.channel}')
        return 0
    if args.test_notification:
        if args.dry_run:
            parser.error('--test-notification 不能与 --dry-run 同时使用')
        send('股票监控服务测试\n通知通道已连通。这条消息不是交易信号。')
        print('测试通知已发送')
        return 0
    from lib.signal_monitor import scan
    lock_path = args.state.with_suffix('.lock')
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open('a+') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('已有监控扫描在运行，跳过本轮')
            return 0
        results = scan(symbols, args.strategy, args.min_agree, args.state, send,
                       dry_run=args.dry_run, sides=sides)
    for symbol, status in results:
        print(f'{symbol}: {status}')
    return 1 if any(status.startswith('失败') for _, status in results) else 0


if __name__ == '__main__':
    raise SystemExit(main())
