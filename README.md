# AI Quota Keeper

在固定時間（預設台灣時間 **07:00 / 12:30 / 18:00 / 23:30**）各對 Codex CLI 與 Claude Code CLI 送出一次 `hi`，
讓 5 小時額度視窗按固定節奏開始計時；並在券即將於下一個排程前到期時，自動兌換 Codex 與 Claude 的 Reset 券。

## 需求
- Linux / WSL2（systemd user timer）、Python 3.11+
- 已登入的 `codex` 與／或 `claude` CLI

## 安裝
```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
cp config.example.toml config.toml   # 視需要修改
./scripts/install.sh                 # 安裝 systemd user timer
```
WSL2 若會被閒置回收，可另設 Windows 排程備援，見 [docs/WINDOWS_SCHEDULER_TASK.md](docs/WINDOWS_SCHEDULER_TASK.md)。

## 指令
```bash
./ai-quota-keeper status [--json] [--cached]
./ai-quota-keeper health | history | credits | logs [--errors]
./ai-quota-keeper dry-run [--now 2026-11-30T18:00:00+08:00]   # 模擬 Reset 判斷，不兌換
./ai-quota-keeper test-hi                                     # 真實送出 hi，不兌換
./scripts/status.sh | uninstall.sh
.venv/bin/pytest -v
```
`run-scheduled` 僅供排程器呼叫，是唯一可真實兌換的入口，**請勿手動執行**。

## 文件
- [架構](docs/ARCHITECTURE.md)
- [Reset 券政策](docs/RESET_POLICY.md)
- [測試](docs/TEST_REPORT.md)
- [Windows 排程備援](docs/WINDOWS_SCHEDULER_TASK.md)

## 注意
Claude 券的查詢與兌換使用非公開的 OAuth 介面（與 Claude Code CLI 相同），可能隨時變動。Token 只在記憶體中讀取，不寫入、不刷新、不記錄。

## 授權
MIT
