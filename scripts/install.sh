#!/usr/bin/env bash
# 安裝 systemd user timer。本腳本不會執行任何觸發或兌換，只負責部署與驗證。
set -euo pipefail
ROOT="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
UNIT_DIR="$HOME/.config/systemd/user"
NODE_BIN="$(dirname "$(command -v codex || command -v claude || echo /usr/bin/node)")"

[ -x "$ROOT/.venv/bin/python" ] || { echo "找不到 $ROOT/.venv，請先建立 venv 並 pip install -e ."; exit 1; }
systemctl --user is-system-running >/dev/null 2>&1 || systemctl --user show-environment >/dev/null \
  || { echo "systemctl --user 不可用（需 WSL systemd 並啟用 linger：sudo loginctl enable-linger $USER）"; exit 1; }

mkdir -p "$UNIT_DIR" "$ROOT/data" "$ROOT/logs"
for u in ai-quota-keeper.service ai-quota-keeper.timer; do
  sed -e "s|@PROJECT_ROOT@|$ROOT|g" -e "s|@NODE_BIN@|$NODE_BIN|g" "$ROOT/systemd/$u" > "$UNIT_DIR/$u"
done

echo "== 驗證 OnCalendar =="
for t in "07:00:00" "12:30:00" "18:00:00"; do
  systemd-analyze calendar "*-*-* $t Asia/Taipei" | grep -E "Normalized|Next elapse|\(in UTC\)" | sed 's/^/  /'
done
systemd-analyze --user verify "$UNIT_DIR/ai-quota-keeper.service" "$UNIT_DIR/ai-quota-keeper.timer" || true

systemctl --user daemon-reload
systemctl --user enable --now ai-quota-keeper.timer
echo "== timer 狀態 =="
systemctl --user is-enabled ai-quota-keeper.timer
systemctl --user is-active ai-quota-keeper.timer
systemctl --user list-timers ai-quota-keeper.timer --no-pager
echo "（安裝完成。未立即執行任何觸發/兌換；等待下一個排程時間。）"
