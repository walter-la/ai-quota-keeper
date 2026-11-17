import logging
from pathlib import Path

import pytest

from ai_quota_keeper import runner as R
from ai_quota_keeper.models import CliStatus
from ai_quota_keeper.orchestrator import global_lock, run_scheduled, run_test_hi
from ai_quota_keeper.providers import claude as claude_p, codex as codex_p
from ai_quota_keeper.runner import ProcResult
from conftest import FakeProvider, FakeRunner, tpe, snap

T = tpe(2026, 11, 17, 7, 0, 5)


def test_commands_match_spec(cfg):
    c = codex_p.build_command("/x/codex", cfg.codex, "none")
    assert c[:4] == ["/x/codex", "exec", "--model", "gpt-6-luna"]
    assert "model_reasoning_effort=none" in c and "read-only" in c and "--ephemeral" in c
    assert "--skip-git-repo-check" in c and c[-1] == "hi"
    k = claude_p.build_command("/x/claude", cfg.claude)
    assert k[:2] == ["/x/claude", "-p"] and "claude-haiku-5-5" in k and "low" in k
    assert k[k.index("--tools") + 1] == "" and "--no-session-persistence" in k
    assert k[k.index("--max-turns") + 1] == "1"


def test_both_succeed_isolated_workdirs(make_deps, cfg):
    r = FakeRunner()
    d = make_deps(T, runner=r)
    res = run_test_hi(d)
    assert [x.status for x in res] == [CliStatus.SUCCESS] * 2
    cwds = {Path(c[1]).name for c in r.calls}
    assert cwds == {"w1", "w2"}                     # 獨立空白工作目錄
    assert all(c[2] == 60 for c in r.calls)         # 預設逾時 60s


def test_codex_fail_does_not_affect_claude(make_deps):
    d = make_deps(T, runner=FakeRunner({"codex": ProcResult(1, "", "boom")}))
    res = {x.cli: x for x in run_test_hi(d)}
    assert res["codex"].status == CliStatus.FAILED and res["claude"].status == CliStatus.SUCCESS
    assert res["codex"].exit_code == 1


def test_claude_fail_does_not_affect_codex(make_deps):
    d = make_deps(T, runner=FakeRunner({"claude": ProcResult(2, "", "x")}))
    res = {x.cli: x for x in run_test_hi(d)}
    assert res["claude"].status == CliStatus.FAILED and res["codex"].status == CliStatus.SUCCESS


def test_exception_in_runner_isolated(make_deps):
    d = make_deps(T, runner=FakeRunner({"codex": RuntimeError("kaput")}))
    res = {x.cli: x for x in run_test_hi(d)}
    assert res["codex"].status == CliStatus.FAILED and res["claude"].status == CliStatus.SUCCESS


def test_cli_not_found(make_deps, tmp_path):
    d = make_deps(T)
    d.env = {"HOME": str(tmp_path / "nohome"), "PATH": "/nonexistent"}
    res = run_test_hi(d)
    assert all(x.status == CliStatus.NOT_FOUND for x in res)


def test_auth_failure(make_deps):
    d = make_deps(T, runner=FakeRunner({"claude": ProcResult(1, "", "Not logged in · Please run /login")}))
    assert {x.cli: x.status for x in run_test_hi(d)}["claude"] == CliStatus.AUTH_ERROR


def test_model_unsupported(make_deps):
    d = make_deps(T, runner=FakeRunner({"codex": ProcResult(1, "", "The model `gpt-6-luna` is not supported")}))
    assert {x.cli: x.status for x in run_test_hi(d)}["codex"] == CliStatus.MODEL_UNSUPPORTED


def test_timeout_status(make_deps):
    d = make_deps(T, runner=FakeRunner({"codex": ProcResult(None, "", "", timed_out=True)}))
    assert {x.cli: x.status for x in run_test_hi(d)}["codex"] == CliStatus.TIMEOUT


def test_effort_none_unsupported_falls_back_to_low(make_deps):
    seq = iter([ProcResult(1, "", "invalid value for reasoning effort: none"), ProcResult(0, "ok", "")])

    class R2(FakeRunner):
        def __call__(self, cmd, **kw):
            if Path(cmd[0]).name == "codex":
                self.calls.append((cmd, "", 0, None)); return next(seq)
            return super().__call__(cmd, **kw)
    d = make_deps(T, runner=R2())
    res = {x.cli: x for x in run_test_hi(d)}
    assert res["codex"].status == CliStatus.SUCCESS and res["codex"].effort == "low"


def test_real_subprocess_timeout_kills_process_group(tmp_path):
    import time
    t0 = time.time()
    res = R.subprocess_runner(["sh", "-c", "sleep 30 & sleep 30"], cwd=tmp_path, timeout=1)
    assert res.timed_out and time.time() - t0 < 10


def test_real_subprocess_not_found(tmp_path):
    assert R.subprocess_runner(["/no/such/bin"], cwd=tmp_path, timeout=1).not_found


def test_different_home_path_env(make_deps, tmp_path):
    bin2 = tmp_path / "other"; bin2.mkdir()
    for n in ("codex", "claude"):
        (bin2 / n).write_text("#!/bin/sh\n"); (bin2 / n).chmod(0o755)
    d = make_deps(T)
    d.env = {"HOME": str(tmp_path), "PATH": str(bin2)}
    r = FakeRunner(); d.runner = r
    run_test_hi(d)
    assert r.calls[0][0][0].startswith(str(bin2))
    assert str(bin2) in r.calls[0][3]["PATH"]


# ---- 排程層：去重、missed、鎖、復原 ----
def test_duplicate_slot_not_run_twice(make_deps):
    r = FakeRunner(); d = make_deps(T, runner=r)
    a = run_scheduled(d); n = len(r.calls)
    b = run_scheduled(d)
    assert a["result"] == "success" and b["result"] == "skipped" and len(r.calls) == n


def test_missed_slot_is_not_backfilled(make_deps):
    r = FakeRunner(); d = make_deps(tpe(2026, 11, 17, 7, 10), runner=r)
    rec = run_scheduled(d)
    assert rec["result"] == "missed" and not r.calls


def test_global_lock_prevents_overlap(make_deps, cfg):
    r = FakeRunner(); d = make_deps(T, runner=r)
    with global_lock(cfg) as got:
        assert got
        rec = run_scheduled(d)
    assert rec["result"] == "skipped" and not r.calls


def test_crashed_run_recoverable(make_deps, store):
    from datetime import timedelta
    r = FakeRunner(); d = make_deps(T, runner=r)
    old = (T - timedelta(hours=1)).astimezone()
    assert store.claim_slot("2026-11-17T07:00+08:00", "triggers", old)   # 模擬異常結束留下 started
    assert run_scheduled(d)["result"] == "success"                        # 逾時未完成 → 接手


def test_fresh_started_slot_blocks(make_deps, store):
    d = make_deps(T, runner=FakeRunner())
    assert store.claim_slot("2026-11-17T07:00+08:00", "triggers", T)
    assert run_scheduled(d)["result"] == "skipped"


def test_record_contains_required_fields(make_deps, cfg):
    d = make_deps(T, runner=FakeRunner())
    rec = run_scheduled(d)
    for k in ("date_taipei", "now_taipei", "now_utc", "trigger_taipei", "next_trigger", "cli", "credits"):
        assert k in rec
    assert rec["trigger_taipei"].startswith("2026-11-17T07:00:00+08:00")
    c = rec["cli"]["codex"]
    for k in ("model", "effort", "started_at", "finished_at", "duration_s", "exit_code"):
        assert k in c
    assert (cfg.logs_dir / "trigger.jsonl").exists()


def test_credit_query_failure_does_not_break_hi(make_deps):
    class Boom:
        redemption_support = "x"
        def fetch_credits(self): raise RuntimeError("down")
    d = make_deps(T, runner=FakeRunner(), codex=Boom(), claude=Boom())
    rec = run_scheduled(d)
    assert rec["result"] == "success" and "error" in rec["credits"]["codex"]
