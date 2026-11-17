# Reset 券政策

## 兩種「重置」不可混淆
- **Usage window reset**：5 小時 / 每週額度視窗的正常重置（`resetsAt`），無法也不需要兌換。
- **Redeemable reset credit**：平台授予、可主動兌換、帶到期時間的免費 Reset 券。本系統只處理這一種。

## 兌換規則（`redemption/policy.py`）
`now < expires_at <= next_trigger`，且券 `status=available`、未暫停、已生效、`resetType=codexRateLimits`。
- 到期時間剛好等於下一排程 → 現在兌換（避免邊界風險）。
- 無到期時間、僅有總數無明細、讀取失敗、種類不符、帳號與綁定不符 → 保守不兌換並記錄原因。
- 多張券依 `expires_at` 由早到晚（同時到期以 id 排序）；每次嘗試後重新查詢；僅在 `reset` 成功且仍有已使用額度時才考慮下一張。

## 冪等與不確定結果
- 每次兌換先寫入 SQLite（`redemptions`，state=pending）再呼叫 consume。
- `reset` / `alreadyRedeemed` → state=done，該券永不再送。
- `nothingToReset` / `noCredit` → 平台確定未消耗；之後的排程可用新 UUID 重試。
- `timeout` / `authError` / `unexpected` → 結果不確定，保持 pending；下次**重用同一把 idempotency key**。

## 防止誤兌換（多層）
1. 只有 `run-scheduled`（systemd / Windows 排程唯一入口）會建立 `LiveRedemptionClient(mode=production, allow_live=True)`。
2. `status / credits / dry-run / test-hi / history / logs / health / install` 不建立任何可兌換 client。
3. `LiveRedemptionClient` 在 pytest 執行中或設定 `AI_QUOTA_KEEPER_FORBID_CONSUME` 時一律拒絕。
4. 測試 fixture 會封鎖真實 `codex app-server` 啟動。

## Claude
券的查詢與兌換都沿用 Claude Code CLI 自己的呼叫（非公開介面，只送往 api.anthropic.com，不刷新、不記錄 token）：
- 查詢：`GET /api/oauth/usage?cedar_ember=1`，讀取 `cedar_ember.grants`（id、`resets_left`、`ends_at`、`paused`）。
- 兌換：`POST /api/organizations/{organizationUuid}/reset_rate_limits`，body `{"program":"cedar_ember","grant_id":<券 id>,"request_id":<idempotency key>}`。
  回應 `result`：`reset` / `already_used` 視為已消耗；`not_limited` / `ineligible` / `cooldown` 視為未消耗，之後可用新 key 重試；
  逾時、HTTP 錯誤、未知結果視為不確定，下次重用同一把 key。
- 判斷規則與 Codex 相同（`now < expires_at <= next_trigger` 才兌換），防護層也相同（只有 `run-scheduled` 會建立可兌換的 client）。
- 此為非公開介面，Claude Code 改版後可能失效；失效時會記錄錯誤、不會重複亂送。
