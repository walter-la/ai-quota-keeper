"""管理指令。status/credits/dry-run/test-hi/history/logs/health 一律不可能兌換。"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from .config import Config, load_config
from .logging_setup import setup_logging
from .models import CliStatus
from .orchestrator import Deps, run_scheduled, run_test_hi
from .redemption.policy import evaluate
from .scheduler import next_trigger, previous_trigger
from .state_store import StateStore
from .status import collect_status, credits_info, health_checks, render_status


def make_deps(cfg: Config | None = None) -> Deps:
    cfg = cfg or load_config()
    setup_logging(cfg.logs_dir)
    return Deps(cfg=cfg, store=StateStore(cfg.db_path))


def _local(iso: str | None, cfg: Config) -> str:
    return datetime.fromisoformat(iso).astimezone(cfg.tz).strftime("%m-%d %H:%M:%S") if iso else "-"


def cmd_status(a, d: Deps) -> int:
    s = collect_status(d, live=not a.cached)
    print(json.dumps(s, ensure_ascii=False, indent=2) if a.json else render_status(s))
    return 0


def cmd_health(a, d: Deps) -> int:
    bad = 0
    for name, ok, detail in health_checks(d):
        print(f"[{'OK ' if ok else 'FAIL'}] {name}: {detail}")
        bad += (not ok)
    print("\n整體:", "健康" if not bad else f"{bad} 項異常")
    return 1 if bad else 0


def cmd_history(a, d: Deps) -> int:
    rows = d.store.recent_runs(a.limit, a.cli)
    if a.json:
        print(json.dumps([dict(r) for r in rows], ensure_ascii=False, indent=1))
        return 0
    print(f"{'時間(台北)':<16}{'CLI':<8}{'結果':<10}{'來源':<10}{'exit':<6}{'耗時':<7}模型/effort  錯誤")
    for r in rows:
        print(f"{_local(r['started_at'], d.cfg):<16}{r['cli']:<8}{r['status']:<10}{r['source']:<10}"
              f"{str(r['exit_code']):<6}{r['duration_s']:<7.1f}{r['model']}/{r['effort']}  {r['error'] or ''}")
    if not rows:
        print("(尚無紀錄)")
    return 0


def cmd_credits(a, d: Deps) -> int:
    now = d.clock()
    out = {}
    for name, prov in (("codex", d.codex_prov), ("claude", d.claude_prov)):
        out[name] = credits_info(prov().fetch_credits(), d, now)
    if a.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    for name, c in out.items():
        print(f"== {name} ==")
        if not c["ok"]:
            print(f"  unavailable: {c['error']}")
            continue
        print(f"  可用券: {c['available_count']}  最近到期: {c['nearest_expiry']}  兌換支援: {c['redemption_support']}")
        for x in c["credits"]:
            print(f"   - …{x['id_tail']} {x['status']} {x['type']} 到期 {x['expires_at']} x{x['count']}")
        for w, v in c["usage_windows"].items():
            print(f"  額度視窗 {w}: {v['used_percent']}% 重置於 {v['resets_at']}（非 Reset 券）")
    return 0


def cmd_dry_run(a, d: Deps) -> int:
    """模擬目前 Reset 判斷；完全不建立兌換 client，不寫兌換紀錄。"""
    now = a.now and datetime.fromisoformat(a.now).astimezone(timezone.utc) or d.clock()
    cfg = d.cfg
    slot = previous_trigger(now, cfg.triggers, cfg.tz)
    nxt = next_trigger(now, cfg.triggers, cfg.tz)
    print(f"[DRY-RUN] now={now.astimezone(cfg.tz):%F %T} 目前時段={slot:%F %H:%M} 下一排程={nxt:%F %H:%M}")
    for name, prov in (("codex", d.codex_prov), ("claude", d.claude_prov)):
        snap = prov().fetch_credits()
        if not snap.ok:
            print(f"  {name}: 無法讀取（{snap.error}）")
            continue
        decs = evaluate(snap, now, nxt)
        print(f"  {name}: 可用券 {snap.available_count}，兌換支援 {snap.redemption_support}")
        for x in decs:
            exp = x.expires_at.astimezone(cfg.tz).strftime("%F %T") if x.expires_at else "-"
            print(f"    …{x.credit_id[-6:] or '(無明細)'} 到期 {exp} → {'會兌換' if x.redeem else '不兌換'}：{x.reason}")
        if not decs:
            print("    無券")
    print("[DRY-RUN] 未發送任何 consume 請求。")
    return 0


def cmd_test_hi(a, d: Deps) -> int:
    results = run_test_hi(d)
    rc = 0
    for r in results:
        print(f"{r.cli}: {r.status.value} model={r.effective_model or r.model} "
              f"effort={r.effective_effort or r.effort} exit={r.exit_code} {r.duration_s:.1f}s"
              + (f"  錯誤: {r.error}" if r.error else ""))
        rc |= r.status != CliStatus.SUCCESS
    print("(test-hi 不會也不可能兌換 Reset 券)")
    return int(rc)


def cmd_logs(a, d: Deps) -> int:
    path = d.cfg.logs_dir / ("error.log" if a.errors else "application.log")
    lines = path.read_text(encoding="utf-8").splitlines()[-a.lines:] if path.exists() else []
    print(f"--- {path} (最後 {len(lines)} 行) ---")
    print("\n".join(lines) or "(空)")
    j = d.cfg.logs_dir / "trigger.jsonl"
    if j.exists() and not a.errors:
        last = j.read_text(encoding="utf-8").splitlines()[-1:]
        print("--- 最近一次排程紀錄 (trigger.jsonl) ---")
        print(json.dumps(json.loads(last[0]), ensure_ascii=False, indent=1) if last else "(無)")
    return 0


def cmd_run_scheduled(a, d: Deps) -> int:
    rec = run_scheduled(d, mode="production", allow_live=True)
    print(json.dumps({k: rec.get(k) for k in ("slot", "result", "reason")}, ensure_ascii=False))
    return 0 if rec["result"] in ("success", "skipped", "missed") else 1


def cmd_repin(a, d: Deps) -> int:
    snap = d.codex_prov().fetch_credits()
    if not snap.ok or not snap.account_id_hash:
        print("無法取得帳號識別")
        return 1
    d.store.set_meta("account:codex", snap.account_id_hash)
    print("已重新綁定 Codex 帳號（僅保存雜湊）。")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ai-quota-keeper", description="AI Quota Keeper 管理工具")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("status", help="系統狀態（唯讀）"); s.add_argument("--json", action="store_true")
    s.add_argument("--cached", action="store_true", help="不連線查券，使用最近快照")
    sub.add_parser("health", help="健康檢查")
    h = sub.add_parser("history", help="歷史執行紀錄"); h.add_argument("--limit", type=int, default=20)
    h.add_argument("--cli", choices=["codex", "claude"]); h.add_argument("--json", action="store_true")
    c = sub.add_parser("credits", help="只讀取券資訊"); c.add_argument("--json", action="store_true")
    dr = sub.add_parser("dry-run", help="模擬 Reset 判斷（不兌換）")
    dr.add_argument("--now", help="模擬時間 ISO8601，例如 2026-11-17T12:30:00+08:00")
    sub.add_parser("test-hi", help="真實呼叫兩個 CLI 的 hi（不兌換）")
    lg = sub.add_parser("logs", help="查看最近日誌"); lg.add_argument("--lines", type=int, default=30)
    lg.add_argument("--errors", action="store_true")
    sub.add_parser("run-scheduled", help="【systemd 專用】執行目前時段；唯一可真實兌換的入口")
    sub.add_parser("repin-account", help="重新綁定 Codex 帳號")
    return p


HANDLERS = {"status": cmd_status, "health": cmd_health, "history": cmd_history,
            "credits": cmd_credits, "dry-run": cmd_dry_run, "test-hi": cmd_test_hi,
            "logs": cmd_logs, "run-scheduled": cmd_run_scheduled, "repin-account": cmd_repin}


def main(argv: list[str] | None = None, deps: Deps | None = None) -> int:
    a = build_parser().parse_args(argv)
    return HANDLERS[a.cmd](a, deps or make_deps())
