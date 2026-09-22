"""quality 大成交额惩罚因子（2026-09-22 新增，默认关闭）的测试。

依据：流动性分层观察——按成交额四分位，低额段更优、Q4（最大额）最差，
海龟约 51%、均线约 48% 的信号落在最差 Q4；落地形态 = quality 分叠加成交额惩罚。
默认权重 0（关闭），仅在 A/B 两期同向改善时才设为非零。
"""
import pandas as pd
import pytest

import sequoia_x.portfolio as pf


# ---------- ④ quality 大成交额惩罚 ----------

def _liq_panel_events():
    panel = pd.DataFrame({
        "symbol": ["600000", "600001"],
        "date": pd.to_datetime(["2026-01-05", "2026-01-05"]),
        "close": [11.0, 11.0], "high20_prev": [10.0, 10.0],
        "hi40": [12.0, 12.0], "lo40": [9.0, 9.0], "hi10": [11.0, 11.0], "lo10": [10.0, 10.0],
        "vol_ma20": [100.0, 100.0], "volume": [100.0, 100.0],
        "turnover": [1.0e8, 5.0e9],          # 600001 成交额大得多（按发现应被惩罚）
    })
    events = pd.DataFrame({"symbol": ["600000", "600001"],
                           "date": pd.to_datetime(["2026-01-05", "2026-01-05"])})
    return panel, events


def test_liq_penalty_off_is_unchanged():
    panel, events = _liq_panel_events()
    ev = pf._score_events(panel, events, "海龟突破", liq_penalty=0.0)
    assert ev["score"].nunique() == 1                    # 两条基本面相同 → 同分


def test_liq_penalty_penalizes_large_turnover():
    panel, events = _liq_panel_events()
    ev = pf._score_events(panel, events, "海龟突破", liq_penalty=20.0)
    s = dict(zip(ev["symbol"], ev["score"]))
    assert s["600001"] < s["600000"]                     # 成交额大的被扣分
    assert s["600000"] - s["600001"] == pytest.approx(10.0)   # 20 * (1.0 - 0.5)


def test_liq_penalty_applies_to_scoreless_strategies():
    """score 恒 0 的策略（RPS）也应受影响 —— 否则惩罚只对部分策略生效。"""
    panel, events = _liq_panel_events()
    ev = pf._score_events(panel, events, "RPS 突破", liq_penalty=20.0)
    assert dict(zip(ev["symbol"], ev["score"]))["600001"] < 0 < 0.1
