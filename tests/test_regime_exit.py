"""移动止损按「来源策略声明」启停（2026-09-22 落地 + 当日复核收窄）的测试。

依据：`退出规则评估_Sequoia_20260922`（5 变体 × 5 单元 × {1y,2y,all} 共 60 组）+
2026-09-22 按单元复核（生产路径 `--no-trail-off` 对照）：
- RPS 突破 ✅ 三窗口从不变差（1y −0.2 持平 / 2y +5.6pp / 全量 +5.5pp）
- 海龟突破 ❌ 1y +0.7 但 2y −1.7pp
- 多策略联合 ❌ 1y −6.3→−9.1（−2.8pp）、2y +8.8→+10.7（+1.9pp）两期不同向
→ 只有 RPS 突破声明关闭；联合池取参与策略交集 → 全关（= 旧口径）。
硬约束：时间上限不可取消、K 不可收紧。
"""
import pandas as pd
import pytest

import sequoia_x.portfolio as pf
from sequoia_x import tracker as tk
from sequoia_x.strategy import (
    STRATEGY_REGISTRY,
    trail_off_regimes_all,
    trail_off_regimes_for,
    trail_off_regimes_map,
)
from sequoia_x.strategy_map import TRAIL_OFF_REGIMES, trail_enabled


# ---------- ① 白名单与按策略声明（单一来源） ----------

def test_evidence_whitelist():
    assert TRAIL_OFF_REGIMES == ("up_low",)      # 有证据的状态白名单，扩大需新证据


def test_declarations_are_subset_of_whitelist():
    """每个策略声明的状态都必须在白名单内 —— 防手滑声明出「无证据状态」。"""
    for spec in STRATEGY_REGISTRY:
        for st in spec.trail_off_regimes:
            assert st in TRAIL_OFF_REGIMES, f"{spec.cn_name} 声明了白名单外的状态 {st}"


def test_only_rps_declares():
    assert trail_off_regimes_for("RPS 突破") == ("up_low",)
    for other in ("海龟突破", "均线放量", "高窄旗形", "涨停洗盘", "上升跌停", "定增公告", "不存在"):
        assert trail_off_regimes_for(other) == ()
    assert trail_off_regimes_map() == {"RPS 突破": ("up_low",)}


def test_combined_takes_intersection():
    """联合池持仓不记来源策略 → 保守取交集（只有全部参与策略都声明才关闭）。"""
    assert trail_off_regimes_all(["RPS 突破"]) == ("up_low",)
    assert trail_off_regimes_all(["RPS 突破", "海龟突破"]) == ()   # ← 联合池=旧口径
    assert trail_off_regimes_all(["海龟突破", "均线放量"]) == ()
    assert trail_off_regimes_all([]) == ()
    assert trail_off_regimes_all(["不存在"]) == ()


def test_trail_enabled():
    assert trail_enabled("up_low", off=("up_low",)) is False        # 已声明关闭 → 停用
    for other in ("up_high", "down_high", "down_low", "na"):
        assert trail_enabled(other, off=("up_low",)) is True        # 其余状态保留止损
    assert trail_enabled(None, off=("up_low",)) is True             # 状态未知 → 保守启用
    assert trail_enabled("up_low") is True                         # 缺省 off=() → 不关闭（保守）
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


def test_trail_suppressed_when_declared():
    sim = _make_sim(("up_low",), "up_low")           # 已声明的关闭状态
    pos, row = _pos_row()                            # 收盘 9.0 < 吊灯线 11-3*0.5=9.5 → 本应触发
    assert sim._check_exit(pos, row, D) is None      # up_low → 吊灯被关闭
    assert sim.stats["trail_suppressed"] == 1
    pos, row = _pos_row(bars=30)                     # 但时间上限照旧生效（不可取消）
    assert sim._check_exit(pos, row, D) == "time-30d"


def test_trail_kept_in_other_states():
    sim = _make_sim(("up_low",), "up_high")          # 情绪顶必须保留止损
    pos, row = _pos_row()
    assert sim._check_exit(pos, row, D) == "chandelier"
    assert sim.stats["trail_suppressed"] == 0


def test_no_declaration_keeps_trail():
    """未声明（含老调用方直接构造 PortfolioSim）→ 不减噪 = 旧行为。"""
    sim = _make_sim((), "up_low")
    pos, row = _pos_row()
    assert sim._check_exit(pos, row, D) == "chandelier"


def test_unknown_regime_keeps_trail():
    sim = _make_sim(("up_low",), None)
    sim._states = pd.DataFrame(columns=["date", "regime"])   # 空状态表
    pos, row = _pos_row()
    assert sim._check_exit(pos, row, D) == "chandelier"


def test_stop_mode_also_respects_trail():
    sim = _make_sim(("up_low",), "up_low")
    sim.exit_mode = "stop"
    sim.stop_loss = 0.08
    pos, row = _pos_row(close=9.0)                   # 跌破 entry*0.92=9.2 → 本应触发
    assert sim._check_exit(pos, row, D) is None
    sim2 = _make_sim(("up_low",), "up_high")
    sim2.exit_mode, sim2.stop_loss = "stop", 0.08
    assert sim2._check_exit(*_pos_row(close=9.0), D) == "stop-8%"


# ---------- ③ tracker：按来源策略减噪 + 老批次保守 ----------

def _synthetic_state(code="600000", entry="2026-08-03", with_trail_off=True):
    b = {
        "signal_date": entry, "regime": "up_low", "status": "bought", "entry_date": entry,
        "n_target": 1, "codes": [code], "holds": {code: 30},
        "entries": [{"code": code, "qty": 100, "price": 10.0, "date": entry}],
        "equity_dates": [], "equity_values": [], "hs300_values": [],
    }
    if with_trail_off:
        b["trail_off"] = {code: ("up_low",)}          # 模拟 RPS 来源
    return {"batches": [b]}


@pytest.fixture(scope="module")
def rows_pair():
    """同一状态分别在 up_low / up_high 下算离场（依赖本地库行情，缺失则跳过）。"""
    try:
        low = tk.exit_signals(_synthetic_state(), regime="up_low")
        high = tk.exit_signals(_synthetic_state(), regime="up_high")
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


def test_tracker_old_batch_not_suppressed():
    """老批次（无 trail_off 字段）→ 不减噪（保守，保持旧行为）。"""
    try:
        rows = tk.exit_signals(_synthetic_state(with_trail_off=False), regime="up_low")
    except Exception as e:
        pytest.skip(f"需要本地行情库: {e}")
    if not rows:
        pytest.skip("库内无该股行情")
    assert rows[0]["trail_on"] is True


def test_tracker_hold_still_applies(rows_pair):
    """时间上限不受 trail 启停影响（口径独立）。"""
    low, _ = rows_pair
    assert ("time" in low["triggers"]) == (low["bars"] >= low["hold_days"])


def test_tracker_declared_display():
    assert tk._trail_off_declared() == {"RPS 突破": ("up_low",)}


def test_code_trail_off_uses_intersection():
    cross = [{"code": "600000", "strategies": ["RPS 突破"]},
             {"code": "600001", "strategies": ["RPS 突破", "海龟突破"]},
             {"code": "600002", "strategies": ["海龟突破"]}]
    got = tk._code_trail_off(cross)
    assert got == {"600000": ("up_low",), "600001": (), "600002": ()}
