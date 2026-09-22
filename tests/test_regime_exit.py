"""移动止损按市场状态启停（2026-09-22 落地）的测试。

依据：`退出规则评估_Sequoia_20260922`（5 变体 × 5 单元 × {1y,2y,all} 共 60 组）——
仅 up_low（低波慢牛）关闭移动止损时 RPS 1y 持平(-0.2)/2y +5.6pp/all +5.5pp；
而 up_high 若也关闭则 1y -6.3pp（情绪顶必须保留止损）；时间上限不可取消、K 不可收紧。
"""
import pandas as pd
import pytest

import sequoia_x.portfolio as pf
from sequoia_x import tracker as tk
from sequoia_x.strategy_map import TRAIL_OFF_REGIMES, trail_enabled


# ---------- ① 单一声明与纯函数 ----------

def test_trail_off_regimes_single_source():
    assert TRAIL_OFF_REGIMES == ("up_low",)      # 改这里等于改全系统行为，需两期同向证据


def test_trail_enabled():
    assert trail_enabled("up_low") is False          # 关闭区间
    for other in ("up_high", "down_high", "down_low", "na"):
        assert trail_enabled(other) is True          # 其余状态必须保留止损（up_high 尤其）
    assert trail_enabled(None) is True               # 状态未知 → 保守启用
    assert trail_enabled("up_low", off=()) is True   # 显式清空 → 全状态启用
    assert trail_enabled("up_high", off=("up_high",)) is False


# ---------- ② 组合层：_check_exit 的 trail 启停 ----------

def _make_sim(trail_off, regime, hold=30, k=3.0):
    """最小合成 sim：只为测 _check_exit 的启停分支，不跑完整回测。"""
    panel = pd.DataFrame({
        "symbol": ["600000", "600000"],
        "date": pd.to_datetime(["2026-01-02", "2026-01-05"]),
        "open": [10.0, 10.0], "high": [10.0, 10.0], "low": [10.0, 10.0],
        "close": [10.0, 10.0],
    })
    events = pd.DataFrame({"symbol": ["600000"], "date": pd.to_datetime(["2026-01-02"])})
    sim = pf.PortfolioSim(panel, events, capital=1_000_000.0, max_pos=10, daily_k=5,
                         hold_days=hold, exit_mode="chandelier", chandelier_k=k,
                         trail_off_regimes=trail_off)
    sim._states = pd.DataFrame({"date": pd.to_datetime(["2026-01-01"]), "regime": [regime]})
    return sim


def _pos_row(peak=11.0, close=9.0, atr=0.5, bars=5):
    return ({"peak": peak, "entry_px": 10.0, "bars_held": bars},
            pd.Series({"close": close, "atr": atr}))


D = pd.Timestamp("2026-01-05")


def test_trail_suppressed_in_up_low():
    sim = _make_sim(None, "up_low")                  # None = 用单一声明(up_low)
    pos, row = _pos_row()                            # 收盘 9.0 < 吊灯线 11-3*0.5=9.5 → 本应触发
    assert sim._check_exit(pos, row, D) is None      # up_low → 吊灯被关闭
    assert sim.stats["trail_suppressed"] == 1
    pos, row = _pos_row(bars=30)                     # 但时间上限照旧生效（不可取消）
    assert sim._check_exit(pos, row, D) == "time-30d"


def test_trail_kept_in_up_high():
    sim = _make_sim(None, "up_high")                 # 情绪顶必须保留止损
    pos, row = _pos_row()
    assert sim._check_exit(pos, row, D) == "chandelier"
    assert sim.stats["trail_suppressed"] == 0


def test_trail_off_can_be_disabled():
    sim = _make_sim((), "up_low")                    # 显式清空 → 回到旧行为
    pos, row = _pos_row()
    assert sim._check_exit(pos, row, D) == "chandelier"


def test_unknown_regime_keeps_trail():
    sim = _make_sim(None, None)
    sim._states = pd.DataFrame(columns=["date", "regime"])   # 空状态表
    pos, row = _pos_row()
    assert sim._check_exit(pos, row, D) == "chandelier"


def test_stop_mode_also_respects_trail():
    sim = _make_sim(None, "up_low")
    sim.exit_mode = "stop"
    sim.stop_loss = 0.08
    pos, row = _pos_row(close=9.0)                   # 跌破 entry*0.92=9.2 → 本应触发
    assert sim._check_exit(pos, row, D) is None
    sim2 = _make_sim(None, "up_high")
    sim2.exit_mode, sim2.stop_loss = "stop", 0.08
    assert sim2._check_exit(*_pos_row(close=9.0), D) == "stop-8%"


# ---------- ③ tracker：离场提醒同一策略 ----------

def _synthetic_state(code="600000", entry="2026-08-03"):
    return {"batches": [{
        "signal_date": entry, "regime": "up_low", "status": "bought", "entry_date": entry,
        "n_target": 1, "codes": [code], "holds": {code: 20},
        "entries": [{"code": code, "qty": 100, "price": 10.0, "date": entry}],
        "equity_dates": [], "equity_values": [], "hs300_values": [],
    }]}


@pytest.fixture(scope="module")
def rows_pair():
    """同一状态分别在 up_low / up_high 下算离场（依赖本地库行情，缺失则跳过）。"""
    state = _synthetic_state()
    try:
        low = tk.exit_signals(state, regime="up_low")
        high = tk.exit_signals(state, regime="up_high")
    except Exception as e:                      # 无库/无行情环境
        pytest.skip(f"需要本地行情库: {e}")
    if not low:
        pytest.skip("库内无该股行情")
    return low[0], high[0]


def test_tracker_trail_policy_flags(rows_pair):
    low, high = rows_pair
    assert low["regime"] == "up_low" and low["trail_on"] is False
    assert high["regime"] == "up_high" and high["trail_on"] is True


def test_tracker_suppression_matches_rule(rows_pair):
    """吊灯触发只受 trail_on 影响：up_low 下永不出现 chandelier，其余状态与破位条件一致。"""
    low, high = rows_pair
    breached = (low["trail"] is not None and low["close"] <= low["trail"])
    assert "chandelier" not in low["triggers"]
    assert ("chandelier" in high["triggers"]) == breached


def test_tracker_hold_still_applies(rows_pair):
    """时间上限不受 trail 启停影响（口径独立）。"""
    low, _ = rows_pair
    assert ("time" in low["triggers"]) == (low["bars"] >= low["hold_days"])
