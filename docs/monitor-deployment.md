# 美股盘中监控：部署与配置

本文对应仓库当前的 `bin/monitor.py`、`deploy/stock-monitor.service.in` 和 `deploy/stock-monitor.timer`。监控使用 LongPort 读取行情，命中盘中技术信号后通过 Bark 通知。当前配置的标的是 `MU.US`、`MRVL.US`、`SKHY.US`、`DRAM.US`。

## 1. 工作方式

- **扫描时段**：美东时间周一至周五 09:35–16:00；遇美股休市、停牌或没有当日 K 线时不发信号。系统定时器约每分钟执行一轮。
- **数据**：LongPort 的美股常规交易时段 5 分钟 K 线，只使用已经收完的 K 线；盘前、盘后和仍在变化的当前 K 线不参与计算。
- **买入提醒**：EMA9 从下往上穿过 EMA21，且该根 K 线收盘价高于当日常规时段 VWAP。
- **卖出指标提醒**：EMA9 从上往下穿过 EMA21，且该根 K 线收盘价低于当日常规时段 VWAP。它只表示技术转弱，程序不知道你的持仓与成本，不会自动下单。
- **推送**：消息包含方向、代码、信号价、VWAP、美东时间和北京时间。每只股票的同一根信号 K 线只推送一次；发送失败且信号 K 线仍在 3 分钟新鲜期内时，下一轮会重试。两种信号均至少需要 30 根常规时段 5 分钟 K 线。

这是**5 分钟 K 收盘确认后的分钟级提醒**，不是逐笔成交级推送。通常在信号 K 线收盘后的下一轮扫描发送，实际延迟还取决于 LongPort 和网络。买卖技术信号均未验证长期收益；卖出指标也不是按你的买入成本计算的 5% 止盈。若要监控实际止盈、止损，需要另行提供每只股票的持仓成本与规则。

## 2. 环境准备

在要持续运行监控的 Linux 主机上，需要 Python、用户级 systemd、可访问 LongPort 与 Bark 的网络，以及有效的 LongPort 美股行情权限。首次部署可直接克隆此 fork：

```bash
git clone https://github.com/notzhan/au-stock-data-quant.git
cd au-stock-data-quant

# 已有 .venv 时跳过创建与安装
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

当前主机已安装依赖并配置了 LongPort；换主机部署时才需要重新执行安装。LongPort 行情接口参考其[官方 K 线文档](https://open.longbridge.com/docs/quote/pull/candlestick)。

## 3. 配置 `.env`

监控配置文件是仓库根目录的 `.env`。从 `.env.example` 复制后，按下面格式填写，**使用自己的凭证与 Bark 设备地址**；不要把真实值写进 `README.md`、`config.yaml` 或提交到 Git。

```bash
cp .env.example .env
```

```dotenv
LONGPORT_APP_KEY=你的应用Key
LONGPORT_APP_SECRET=你的应用Secret
LONGPORT_ACCESS_TOKEN=你的访问Token

MONITOR_SYMBOLS=MU.US,MRVL.US,SKHY.US,DRAM.US
MONITOR_STRATEGY=intraday
MONITOR_SIDES=buy,sell
MONITOR_CHANNEL=bark
BARK_URL=https://api.day.app/你的设备密钥
```

```bash
chmod 600 .env
```

`MONITOR_SYMBOLS` 用英文逗号分隔，可增删美股代码；`MU` 与 `MU.US` 都会被规范化为 `MU.US`。`MONITOR_SIDES=buy,sell` 同时提醒两个方向；只要买入提醒可设为 `buy`，只要卖出指标提醒可设为 `sell`。`MONITOR_STRATEGY=intraday` 是盘中规则。`ensemble`、`ma_cross` 等是旧版**日线收盘买入**策略，切换过去后提醒时点也会变成收盘后。Bark 的 POST/JSON 用法见[官方文档](https://github.com/Finb/Bark/blob/master/docs/en-us/tutorial.md)。

`.env` 已被仓库的 `.gitignore` 排除；修改文件后下一轮服务启动时自动读取，无需重装定时器。代码没有提供通过 `.env` 调整 EMA 周期或 VWAP 条件的参数；要改盘中信号定义需修改 `lib/signal_monitor.py` 并验证。

## 4. 部署前验证

```bash
.venv/bin/python bin/monitor.py --check-config       # 只检查配置，不联网、不推送
.venv/bin/python bin/monitor.py --test-notification # 发送一条标明“测试”的 Bark 消息
.venv/bin/python bin/monitor.py --dry-run           # 扫描一次，不推送、不记录已通知状态
```

`--dry-run` 在美股常规交易时段之外会显示“无新信号”；这不代表服务或 Bark 故障。`--test-notification` 会真的发送一条测试消息，但不会冒充交易信号。

## 5. 安装、启动和停止

首次安装并启动：

```bash
bash deploy/install-user-service.sh
```

安装脚本会先检查配置，再把服务文件和定时器安装到用户级 systemd 目录（默认 `~/.config/systemd/user/`，设置了 `XDG_CONFIG_HOME` 时使用该目录），最后启用并启动定时器。它不会自动下单。

日常操作：

```bash
systemctl --user status stock-monitor.timer          # 查看是否 active (waiting)
systemctl --user disable --now stock-monitor.timer   # 停止并禁止开机自动启动
systemctl --user enable --now stock-monitor.timer    # 重新启用并启动
systemctl --user restart stock-monitor.timer         # 重新启动定时器
journalctl --user -u stock-monitor.service -n 50 --no-pager  # 最近 50 条执行日志
journalctl --user -u stock-monitor.service -f        # 持续查看执行日志
```

修改 `.env` 中的股票名单或 Bark 地址后，下一轮扫描就会使用新值；修改 `deploy/` 里的 service/timer 模板后，重新运行安装脚本。若需登出后仍持续监控，可用 `loginctl show-user "$USER" -p Linger` 核对；如果是 `Linger=no`，请由该主机管理员启用。

## 6. 状态、去重和排障

已发送状态保存在 `cache/monitor-state.json`，锁文件为 `cache/monitor-state.lock`。只有实际发送成功才记录状态；普通重启不会重复推送同一根 K 线。请保留该状态文件以维持去重。`cache/` 已被 Git 忽略。

| 现象 | 检查方法 |
| --- | --- |
| 没有提醒 | 先看定时器状态和 service 日志；盘前、盘后、休市以及未触发交叉时本来就不会发信号。 |
| Bark 测试消息收不到 | 运行 `--test-notification`，确认 Bark App 的设备地址、手机通知权限和主机网络。 |
| 日志出现 LongPort 错误 | 检查 `.env` 中三项 LongPort 凭证、行情权限和网络连通性。 |
| `systemctl --user` 不能连接 | 确认以部署服务的同一用户运行命令，并检查该用户的 systemd 会话。 |
| 修改配置后未生效 | 运行 `--check-config`，检查 `.env` 文件格式；查看 service 日志确认新一轮执行。 |

美股常规时段是美东 09:30–16:00；夏令时对应北京时间约 21:30–04:00，冬令时约 22:30–05:00。监控在美东 09:35 后才可能发出第一条 5 分钟 K 信号，时间转换由程序按纽约时区自动处理。交易所时段可见 [Nasdaq Market Activity](https://www.nasdaq.com/market-activity)。
