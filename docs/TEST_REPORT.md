# 測試

執行：`.venv/bin/pytest -v`

涵蓋範圍：
- 排程計算（跨日/月/年、UTC 轉換、邊界、missed 不補跑）
- Reset 判斷（到期、生效、已使用、無到期、多張券、同時到期、明細為 null、僅有總數、不同類型、帳號不符）
- 兌換流程（以 Mock 驗證 reset / alreadyRedeemed / nothingToReset / noCredit / timeout 重用 key / 重複排程不重複兌換 / 平台拒絕）
- Live client 的規格 payload（fake transport）
- CLI 觸發（成功、互不影響、不存在、認證失敗、模型不支援、逾時、非零 exit、effort 降級、不同 HOME/PATH、process group 終止）
- 時段去重、全域鎖、異常結束復原
- 安全：dry-run / status / credits / test-hi 不會 consume、Live client 在測試中被拒、token 遮罩、安裝腳本不含兌換

所有測試都不會真實兌換 Reset 券。
