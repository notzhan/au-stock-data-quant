#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python_bin="$repo_dir/.venv/bin/python"
unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

if [[ ! -x "$python_bin" ]]; then
  echo "缺少 $python_bin；请先安装项目依赖" >&2
  exit 1
fi

"$python_bin" "$repo_dir/bin/monitor.py" --check-config

mkdir -p "$unit_dir"
sed -e "s|@REPO@|$repo_dir|g" -e "s|@PYTHON@|$python_bin|g" \
  "$repo_dir/deploy/stock-monitor.service.in" > "$unit_dir/stock-monitor.service"
cp "$repo_dir/deploy/stock-monitor.timer" "$unit_dir/stock-monitor.timer"
systemctl --user daemon-reload
systemctl --user enable --now stock-monitor.timer
echo "已启动 stock-monitor.timer"
