# 架構

```
systemd timer (07:00/12:30/18:00 Asia/Taipei) ─┐
Windows Task Scheduler（選用備援）─────────────┴─> ai-quota-keeper run-scheduled
                                                      │
   orchestrator.run_scheduled
   ├─ current_slot()           容許 120s，超過 → missed（不補跑）
   ├─ global_lock (flock) + StateStore.claim_slot（時段去重、異常結束可接手）
   ├─ codex.run_hi  ─┐ 互相獨立；各自逾時 60s、獨立空白工作目錄、process group 終止
   ├─ claude.run_hi ─┘
   └─ credits phase
       ├─ Codex: AppServer(account/rateLimits/read) → policy.evaluate → redemption.service → client
       └─ Claude: ClaudeResetProvider → policy.evaluate → redemption.service → ClaudeRedemptionClient
```
| 模組 | 職責 |
|---|---|
| scheduler.py | 純函式排程計算，可注入 Clock |
| runner.py | 無 shell 的子行程、逾時終止、錯誤分類、CLI 路徑解析 |
| providers/ | codex / claude 的 hi 指令與券查詢 |
| appserver.py | JSON-RPC stdio 客戶端（握手、id 配對、逾時、生命週期） |
| redemption/ | policy（決策）、service（流程+持久化）、client（Live/DryRun/Unsupported）、mock |
| state_store.py | SQLite：slot_runs、runs、redemptions、snapshots、meta |
| logging_setup.py | 輪替日誌 + trigger.jsonl + token 遮罩 |
| status.py / cli.py | 狀態彙整與管理指令 |

資料：`data/state.db`（0600）、`logs/application.log|error.log|trigger.jsonl`（輪替）。
Codex 實際使用 CLI 既有登入；Claude 券查詢與兌換只在記憶體讀取 access token（不寫入、不刷新、不離開 api.anthropic.com）。
