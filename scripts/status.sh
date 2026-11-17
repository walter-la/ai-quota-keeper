#!/usr/bin/env bash
# 排程狀態（唯讀）。
ROOT="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
systemctl --user list-timers ai-quota-keeper.timer --no-pager
echo
systemctl --user status ai-quota-keeper.timer --no-pager -n 0 | head -8
echo
journalctl --user -u ai-quota-keeper.service -n 15 --no-pager 2>/dev/null || true
echo
"$ROOT/ai-quota-keeper" status --cached
