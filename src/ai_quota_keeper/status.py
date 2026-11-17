"""狀態彙整與系統檢查（全部唯讀；不涉及兌換）。"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from .config import Config
from .models import CreditsSnapshot
from .orchestrator import Deps
from .redemption.policy import evaluate, next_redemption_point
from .runner import find_executable, with_node_path
from .scheduler import next_trigger, previous_trigger


def _run(cmd: list[str], timeout: int = 15, env: dict | None = None) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env,
                           stdin=subprocess.DEVNULL)
        return p.returncode, (p.stdout + p.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, f"{type(e).__name__}: {e}"


def timer_state(cfg: Config) -> dict:
    rc, active = _run(["systemctl", "--user", "is-active", cfg.timer_unit])
    rc2, enabled = _run(["systemctl", "--user", "is-enabled", cfg.timer_unit])
    nxt_dt = None
    rc3, js = _run(["systemctl", "--user", "list-timers", cfg.timer_unit, "--output=json", "--no-pager"])
    try:
        for row in json.loads(js):
            if row.get("unit") == cfg.timer_unit and row.get("next"):
                nxt_dt = datetime.fromtimestamp(row["next"] / 1e6, timezone.utc)
    except (json.JSONDecodeError, TypeError):
        pass
    return {"active": active.splitlines()[0] if active else "unknown",
            "enabled": enabled.splitlines()[0] if enabled else "unknown", "next_elapse": nxt_dt}


def _fmt(dt: datetime | None, cfg: Config) -> str | None:
    return dt.astimezone(cfg.tz).strftime("%Y-%m-%d %H:%M:%S") if dt else None


def credits_info(snap: CreditsSnapshot, d: Deps, now: datetime) -> dict:
    cfg = d.cfg
    nxt = next_trigger(now, cfg.triggers, cfg.tz)
    decs = evaluate(snap, now, nxt)
    point = next_redemption_point(snap, now, lambda t: next_trigger(t, cfg.triggers, cfg.tz))
    windows = {w.name: {"used_percent": w.used_percent, "resets_at": _fmt(w.resets_at, cfg)}
               for w in snap.windows}
    return {"ok": snap.ok, "error": snap.error, "available_count": snap.available_count,
            "details_available": snap.credits is not None,
            "nearest_expiry": _fmt(snap.nearest_expiry(), cfg),
            "next_redemption_decision": _fmt(point, cfg),
            "redeem_at_current_slot": any(x.redeem for x in decs),
            "redemption_support": snap.redemption_support,
            "usage_windows": windows,
            "credits": [{"id_tail": c.id[-6:], "status": c.status, "type": c.reset_type,
                         "expires_at": _fmt(c.expires_at, cfg), "count": c.count}
                        for c in (snap.credits or [])]}


def collect_status(d: Deps, live: bool = True) -> dict:
    cfg, now = d.cfg, d.clock()
    nxt = next_trigger(now, cfg.triggers, cfg.tz)
    prev = previous_trigger(now, cfg.triggers, cfg.tz)
    ts = timer_state(cfg)
    out: dict = {"now_taipei": _fmt(now, cfg), "now_utc": now.astimezone(timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S"), "timezone": cfg.timezone,
        "triggers": [t.strftime("%H:%M") for t in cfg.triggers],
        "scheduler": {"state": ts["active"], "enabled": ts["enabled"],
                      "systemd_next": _fmt(ts["next_elapse"], cfg)},
        "next_trigger": _fmt(nxt, cfg), "previous_trigger": _fmt(prev, cfg)}
    for name, spec, prov in (("codex", cfg.codex, d.codex_prov), ("claude", cfg.claude, d.claude_prov)):
        last = d.store.last_run(name)
        last_sched = d.store.last_run(name, "scheduled")
        info: dict = {"model": spec.model, "effort": spec.effort,
                      "last_run": ({"time": _fmt(datetime.fromisoformat(last["started_at"]), cfg),
                                    "result": last["status"], "source": last["source"],
                                    "slot": last["slot"], "exit_code": last["exit_code"]}
                                   if last else None),
                      "last_scheduled": ({"time": _fmt(datetime.fromisoformat(
                          last_sched["started_at"]), cfg), "result": last_sched["status"]}
                          if last_sched else None)}
        if live:
            info["credits"] = credits_info(prov().fetch_credits(), d, now)
        else:
            s = d.store.get_snapshot(name)
            info["credits"] = ({"ok": bool(s["ok"]), "available_count": s["available_count"],
                                "nearest_expiry": s["nearest_expiry"], "error": s["error"],
                                "from_cache": s["taken_at"]} if s else {"ok": False,
                                                                        "error": "尚無快照"})
        out[name] = info
    out["claude"]["credits"]["redemption_support"] = "supported"
    return out


def render_status(s: dict) -> str:
    L = ["AI Quota Keeper Status", "=" * 40,
         f"Current Time (Taipei): {s['now_taipei']}   (UTC {s['now_utc']})", "",
         f"Scheduler: {s['scheduler']['state']} (enabled={s['scheduler']['enabled']})",
         f"Fixed Triggers (Taipei): {', '.join(s['triggers'])}",
         f"Next Trigger (CLI hi): {s['next_trigger']}   [systemd 回報: {s['scheduler']['systemd_next']}]", ""]
    for name in ("codex", "claude"):
        i, c = s[name], s[name]["credits"]
        L.append(f"{name.capitalize()}:")
        L.append(f"  Model: {i['model']}")
        L.append(f"  Effort: {i['effort']}")
        lr = i["last_run"]
        L.append(f"  Last Run: {lr['time']} {lr['result']} (來源 {lr['source']}, exit {lr['exit_code']})"
                 if lr else "  Last Run: 尚無紀錄")
        ls = i["last_scheduled"]
        L.append(f"  Last Scheduled Trigger: {ls['time']} {ls['result']}" if ls
                 else "  Last Scheduled Trigger: 尚無")
        if c.get("ok"):
            L.append(f"  Reset Credits: {c['available_count']}"
                     + ("" if c.get("details_available", True) else "（僅總數，無明細）"))
            L.append(f"  Nearest Credit Expiry: {c.get('nearest_expiry') or 'n/a'}")
            L.append(f"  Next Redemption Decision Slot: {c.get('next_redemption_decision') or 'n/a'}"
                     + ("   ← 本時段即需判斷兌換" if c.get("redeem_at_current_slot") else ""))
            for w, v in (c.get("usage_windows") or {}).items():
                L.append(f"  Usage Window {w}: used {v['used_percent']}% resets {v['resets_at']}"
                         "  (平台額度視窗，非 Reset 券)")
        else:
            L.append(f"  Reset Credits: unavailable（{c.get('error')}）")
        L.append(f"  Redemption Support: {c.get('redemption_support', 'n/a')}")
        L.append("")
    L.append("=" * 40)
    return "\n".join(L)


def health_checks(d: Deps) -> list[tuple[str, bool, str]]:
    cfg, env = d.cfg, d.env
    res: list[tuple[str, bool, str]] = []
    v = sys.version_info
    res.append(("Python >= 3.11", v >= (3, 11), f"{v.major}.{v.minor}.{v.micro}"))
    for name, args in (("codex", ["login", "status"]), ("claude", ["auth", "status"])):
        exe = find_executable(name, env)
        if not exe:
            res.append((f"{name} 指令", False, "找不到"))
            continue
        e2 = with_node_path(env, exe)
        _, ver = _run([exe, "--version"], env=e2)
        res.append((f"{name} 指令", True, f"{exe} ({ver.splitlines()[0] if ver else '?'})"))
        rc, txt = _run([exe, *args], env=e2)
        logged = rc == 0 and ("logged in" in txt.lower() or '"loggedIn": true' in txt)
        res.append((f"{name} 已登入", logged, "是" if logged else "否/無法判斷"))
    rc, pid1 = _run(["ps", "-p", "1", "-o", "comm="])
    res.append(("systemd 為 PID 1", pid1.strip() == "systemd", pid1.strip()))
    ts = timer_state(cfg)
    res.append(("timer active", ts["active"] == "active", f"{ts['active']}/{ts['enabled']}"))
    res.append(("timer 下次觸發", ts["next_elapse"] is not None, _fmt(ts["next_elapse"], cfg) or "n/a"))
    ok = True
    try:
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        cfg.logs_dir.mkdir(parents=True, exist_ok=True)
        (cfg.data_dir / ".w").write_text("x")
        (cfg.data_dir / ".w").unlink()
    except OSError:
        ok = False
    res.append(("data/logs 可寫入", ok, str(cfg.data_dir)))
    cred = Path(env["HOME"]) / ".claude/.credentials.json"
    scope_ok = False
    try:
        scope_ok = "user:profile" in (json.loads(cred.read_text())["claudeAiOauth"].get("scopes") or [])
    except (OSError, KeyError, json.JSONDecodeError):
        pass
    res.append(("Claude 券查詢憑證(user:profile)", scope_ok, "具備" if scope_ok else "缺少，券查詢不可用"))
    c = d.codex_prov().fetch_credits()
    res.append(("Codex App Server 查券", c.ok, c.error or f"券 {c.available_count}"))
    return res
