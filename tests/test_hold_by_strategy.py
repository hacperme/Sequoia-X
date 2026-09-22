"""持有期按策略分档（单一来源）的测试。

背景（2026-09-21）：5/10/20/30/40 日 × 1Y+2Y 全扫描定标后，把持有期收敛到
`sequoia_x.strategy.StrategySpec.hold_days` 一处声明，由 backtest / portfolio / tracker 三处引用。
本文件锁定：① 注册表里的档位值；② portfolio 默认按策略取档（显式 --hold-days 仍可统一覆盖）；
③ tracker 按信号来源策略定档、老批次回退 EXIT_HOLD_DAYS；④ 报告按行显示各自档位。
"""

import json

import pandas as pd
import pytest

from sequoia_x import tracker as tk
from sequoia_x.strategy import (DEFAULT_HOLD_DAYS, STRATEGY_REGISTRY,
                               hold_days_for, hold_days_map)


# ---------- ① 注册表档位 ----------

def test_registry_hold_values():
    hm = hold_days_map()
    assert hm["海龟突破"] == 20      # 两期同向最优
    assert hm["RPS 突破"] == 30      # 逐笔均收益两期一致升到 30 日
    assert hm["上升跌停"] == 40
    assert hm["涨停洗盘"] == 20
    assert hm["均线放量"] == 20      # 不实盘，仅作回测基准


def test_every_backtested_strategy_has_hold():
    for spec in STRATEGY_REGISTRY:
        assert spec.hold_days >= 10, f"{spec.cn_name} 持有期过短"
        assert hold_days_for(spec.cn_name) == spec.hold_days


def test_unknown_strategy_falls_back():
    assert hold_days_for("不存在的策略") == DEFAULT_HOLD_DAYS
    assert hold_days_for("不存在的策略", default=7) == 7


# ---------- ② portfolio 默认按策略定档 ----------

def _stub_run_portfolio(monkeypatch, events_names):
    """把 run_portfolio 的重依赖（DB/面板/事件）全部 stub，记录每个 sim 收到的 hold_days。"""
    import sequoia_x.portfolio as pf

    seen: dict[str, int] = {}

    class StubSim:
        def __init__(self, panel, events, **kw):
            name = getattr(events, "attrs", {}).get("name") or "多策略联合"
            seen[name] = kw["hold_days"]

        def run(self):
            return {"total_ret": 0.0}

    monkeypatch.setattr(pf, "PortfolioSim", StubSim)
    monkeypatch.setattr(pf, "get_settings", lambda: None)
    monkeypatch.setattr(pf, "DataEngine", lambda settings: type("E", (), {"db_path": ":memory:"})())
    monkeypatch.setattr(pf, "_load_panel", lambda p: pd.DataFrame(
        {"symbol": ["600000"], "date": [pd.Timestamp("2025-01-02")]}))
    monkeypatch.setattr(pf, "_add_atr", lambda p: p)
    monkeypatch.setattr(pf, "_add_features", lambda p: p)

    def fake_events(panel, strategies=None):
        out = {}
        for n in events_names:
            ev = pd.DataFrame({"symbol": ["600000"], "date": [pd.Timestamp("2025-01-02")]})
            ev.attrs["name"] = n
            out[n] = ev
        return out

    monkeypatch.setattr(pf, "compute_events", fake_events)
    return pf, seen


def test_portfolio_default_is_per_strategy(monkeypatch):
    pf, seen = _stub_run_portfolio(monkeypatch, ["海龟突破", "RPS 突破"])
    res = pf.run_portfolio(period="all", strategies=["海龟突破", "RPS 突破"])
    assert seen == {"海龟突破": 20, "RPS 突破": 30}
    assert "按策略定档" in res["note"]


def test_portfolio_explicit_hold_overrides_all(monkeypatch):
    pf, seen = _stub_run_portfolio(monkeypatch, ["海龟突破", "RPS 突破"])
    res = pf.run_portfolio(period="all", strategies=["海龟突破", "RPS 突破"], hold_days=7)
    assert seen == {"海龟突破": 7, "RPS 突破": 7}
    assert "hold=7d（统一指定）" in res["note"]


def test_portfolio_combined_takes_max(monkeypatch):
    pf, seen = _stub_run_portfolio(monkeypatch, ["海龟突破", "RPS 突破"])
    res = pf.run_portfolio(period="all", strategies=["海龟突破", "RPS 突破"], combined=True)
    assert seen == {"多策略联合": 30}          # max(20, 30)
    assert "联合 30d" in res["note"]


# ---------- ③ tracker 按信号来源定档 ----------

def _register_cross(monkeypatch, tmp_path, cross):
    monkeypatch.setattr(tk, "STATE_PATH", str(tmp_path / "track_state.json"))
    rep = {"date": "2026-09-21", "regime": {"regime": "bull"}, "cross_hits": cross}
    p = tmp_path / "daily_report.json"
    p.write_text(json.dumps(rep, ensure_ascii=False), encoding="utf-8")
    state = {"batches": []}
    tk.register(state, str(p))
    return state


def test_code_holds_uses_strategy_map(monkeypatch, tmp_path):
    state = _register_cross(monkeypatch, tmp_path, [
        {"code": "600000", "name": "A", "strategies": ["海龟突破"]},
        {"code": "600001", "name": "B", "strategies": ["海龟突破", "RPS 突破"]},  # 取最大
        {"code": "600002", "name": "C", "strategies": ["未知策略"]},               # 回退
        {"code": "600003", "name": "D", "strategies": []},                        # 回退
    ])
    b = state["batches"][0]
    assert b["holds"] == {"600000": 20, "600001": 30,
                          "600002": tk.EXIT_HOLD_DAYS, "600003": tk.EXIT_HOLD_DAYS}
    assert tk._hold_for_code(state, "600001") == 30
    assert tk._hold_for_code(state, "600000") == 20
    assert tk._hold_for_code(state, "999999") == tk.EXIT_HOLD_DAYS      # 老批次/未知代码
    assert tk.EXIT_HOLD_DAYS == DEFAULT_HOLD_DAYS                        # 回退值与注册表同源


def test_hold_for_code_takes_max_across_batches(monkeypatch, tmp_path):
    monkeypatch.setattr(tk, "STATE_PATH", str(tmp_path / "s.json"))
    state = {"batches": [
        {"holds": {"600000": 20}},          # 老批次结构
        {"holds": {"600000": 30}},
        {},                                  # 无 holds 的老批次
    ]}
    assert tk._hold_for_code(state, "600000") == 30


# ---------- ④ 报告按行显示各自档位 ----------

def test_report_renders_per_row_hold():
    rows = [{
        "code": "600001", "n_batches": 1, "qty": 100, "first_date": "2026-08-01",
        "entry_px": 10.0, "close": 10.1, "peak": 10.2, "atr14": 0.2, "trail": 9.6,
        "bars": 30, "ret_pct": 1.0, "hold_days": 30, "due_date": "2026-09-21",
        "triggers": ["time"],
    }, {
        "code": "600002", "n_batches": 1, "qty": 100, "first_date": "2026-09-01",
        "entry_px": 10.0, "close": 10.1, "peak": 10.2, "atr14": 0.2, "trail": 9.6,
        "bars": 19, "ret_pct": 1.0, "hold_days": 20, "due_date": "2026-09-30",
        "triggers": [],
    }]
    txt = tk.report({"batches": []}, rows)
    assert "到期30日" in txt          # 到期标签用该股档位，而非全局值
    assert "还需1日" in txt           # 即将到期预告也按该股档位（20-19=1）
    assert "定档" in txt


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
