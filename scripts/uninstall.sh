#!/usr/bin/env bash
# 停用並移除 systemd user timer / service。不刪除專案、日誌與資料。
set -euo pipefail
UNIT_DIR="$HOME/.config/systemd/user"
systemctl --user disable --now ai-quota-keeper.timer 2>/dev/null || true
systemctl --user stop ai-quota-keeper.service 2>/dev/null || true
rm -f "$UNIT_DIR/ai-quota-keeper.service" "$UNIT_DIR/ai-quota-keeper.timer"
systemctl --user daemon-reload
echo "已卸載 ai-quota-keeper（專案檔、logs、data 保留）。"
