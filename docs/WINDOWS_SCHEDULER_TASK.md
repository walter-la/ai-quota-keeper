# Windows Task Scheduler 備援任務規格（交給 Windows Agent 部署）

> 本文件只是規格，**尚未建立任何 Windows 排程**。Windows 只負責喚起同一套 Ubuntu Python 程式，不含任何業務邏輯。

## 為什麼需要
Ubuntu 的 systemd user timer 已啟用，但 WSL2 在沒有任何 Windows 端程序連線時可能被自動停止（閒置回收），
此時 Linux timer 無法在 07:00 / 12:30 / 18:00 準時觸發，且依需求 `Persistent=false` 不補跑。
Windows Task Scheduler 可以在指定時間用 `wsl.exe` 喚起 WSL2 並執行同一個入口。

## 環境事實
| 項目 | 值 |
|---|---|
| WSL distro | `<DISTRO>`（WSL2） |
| Linux 使用者 | `<USER>`（不要用 root） |
| 專案路徑 | `<PROJECT_ROOT>` |
| 入口 | `<PROJECT_ROOT>/ai-quota-keeper run-scheduled` |
| 時區 | Asia/Taipei（UTC+8，Windows 本機時區也須為台北時間） |

## 要建立的任務：三個觸發器（同一個 Task）
- 任務名稱：`AI Quota Keeper`
- 觸發器（每天）：**07:00**、**12:30**、**18:00**（本機時間 = 台北時間）
- 動作：啟動程式
  - Program：`C:\Windows\System32\wsl.exe`
  - Arguments：
    ```
    -d <DISTRO> --user <USER> --cd <PROJECT_ROOT> -- <PROJECT_ROOT>/ai-quota-keeper run-scheduled
    ```
- 設定：
  - 以目前登入使用者執行（不要最高權限）；「只在使用者登入時執行」或「不論是否登入」皆可
  - 「如果錯過排程，儘快啟動」→ **關閉**（不補跑）
  - 「如果工作已在執行，則」→ **不要啟動新執行個體**
  - 執行時間上限：5 分鐘
  - 喚醒電腦以執行：視需要

### PowerShell 範例（供 Windows Agent 參考，由其審視後執行）
```powershell
$action = New-ScheduledTaskAction -Execute "C:\Windows\System32\wsl.exe" `
  -Argument "-d <DISTRO> --user <USER> --cd <PROJECT_ROOT> -- <PROJECT_ROOT>/ai-quota-keeper run-scheduled"
$triggers = "07:00","12:30","18:00" | ForEach-Object { New-ScheduledTaskTrigger -Daily -At $_ }
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 5) -StartWhenAvailable:$false
Register-ScheduledTask -TaskName "AI Quota Keeper" -Action $action -Trigger $triggers -Settings $settings
```

## 重複執行保護（不需停用 Linux timer）
`run-scheduled` 內建兩層保護，Linux timer 與 Windows 任務同時觸發也只會執行一次：
1. 全域檔案鎖 `data/run.lock`（`fcntl.flock`）：另一程序執行中 → 記錄 `skipped`。
2. SQLite 時段去重（`slot_runs`）：同一個時段（例如 `2026-11-17T12:30+08:00`）只會執行一次。

另有 120 秒延遲容許值：遲到超過 120 秒記錄為 `missed`，不補跑。
如仍想只保留單一排程器，可執行 `<PROJECT_ROOT>/scripts/uninstall.sh` 停用 Linux timer（非必要）。

## 驗證（不會觸發兌換）
```powershell
wsl.exe -d <DISTRO> --user <USER> --cd <PROJECT_ROOT> -- ./ai-quota-keeper status --cached
wsl.exe -d <DISTRO> --user <USER> --cd <PROJECT_ROOT> -- ./ai-quota-keeper history --limit 10
```
排程時間到之後，`history` 應出現 `來源=scheduled` 的 codex/claude 各一筆。
**不要**在驗證時手動執行 `run-scheduled`：在排程時段內執行會真正走正式流程（含符合條件時的 Codex Reset 兌換）。
