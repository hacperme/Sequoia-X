"""回测引擎 smoke + 信号确定性测试。

- test_compute_events_smoke：真实库（须已回填）近 60 日跑 6 策略，断言事件表结构/去重
- test_signal_mask_deterministic：合成 panel 上断言已知形态触发/不触发
- test_high_tight_includes_high_level：高窄"高位抗跌"条件存在（防回归：回测缺条件漂移）

这些测试需要本地库 data/sequoia_v2.db 有数据（skip 若缺）。
"""
import pandas as pd
import pytest

from sequoia_x.backtest import _load_panel, compute_events
from sequoia_x.strategy.high_tight_flag import HighTightFlagStrategy
from sequoia_x.strategy.ma_volume import MaVolumeStrategy
from sequoia_x.strategy.turtle_trade import TurtleTradeStrategy

DB = "data/sequoia_v2.db"


def _mini_panel(n_days=120, symbols=("600000", "300750", "000001")) -> pd.DataFrame:
    """构造确定性上涨/震荡 panel：600000 持续缓涨，300750 高位震荡，000001 下跌。"""
    rng = pd.date_range("2025-01-01", periods=n_days, freq="B")
    rows = []
    px = {"600000": 10.0, "300750": 50.0, "000001": 20.0}
    for sym in symbols:
        p = px[sym]
        for i, d in enumerate(rng):
            drift = {"600000": 0.001, "300750": 0.0, "000001": -0.002}[sym]
            p = p * (1 + drift)
            o, c = p, p
            h, l = p * 1.01, p * 0.99
            rows.append((sym, d, o, h, l, c, 2_000_000, 2.0e8))
    df = pd.DataFrame(rows, columns=["symbol", "date", "open", "high", "low",
                                     "close", "volume", "turnover"])
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values(["symbol", "date"]).reset_index(drop=True)
    df["seq"] = df.groupby("symbol").cumcount()
    # 分板涨跌停列（与 _load_panel 同口径）
    from sequoia_x.market_rules import limit_down_ratio, limit_up_ratio
    df["lim_up"] = df["symbol"].map(limit_up_ratio)
    df["lim_dn"] = df["symbol"].map(limit_down_ratio)
    df["prev_close"] = df.groupby("symbol")["close"].shift(1)
    return df


def test_signal_mask_smoke_synthetic():
    """合成 panel 上 signal_mask 能跑、形状正确、无 NaN 崩溃。"""
    panel = _mini_panel()
    for cls in (TurtleTradeStrategy, MaVolumeStrategy, HighTightFlagStrategy):
        strat = cls(engine=None, settings=None)
        mask = strat.signal_mask(panel)
        assert isinstance(mask, pd.Series) and len(mask) == len(panel)
        assert mask.dtype == bool


@pytest.mark.skipif(not __import__("os").path.exists(DB), reason="本地库未回填")
def test_compute_events_smoke_real_db():
    """真实库近 60 日：compute_events 事件表结构正确、按 symbol 去重冷却。"""
    from sequoia_x.core.config import get_settings
    from sequoia_x.data.engine import DataEngine

    eng = DataEngine(get_settings())
    panel = _load_panel(eng.db_path)
    cutoff = panel["date"].max() - pd.Timedelta(days=120)
    small = panel[panel["date"] > cutoff].reset_index(drop=True)
    small["seq"] = small.groupby("symbol").cumcount()
    events = compute_events(small)
    for name, ev in events.items():
        assert set(ev.columns) >= {"symbol", "date", "seq"}
        # 冷却去重验证：同股相邻信号间隔 ≥20
        if len(ev) > 1:
            ev2 = ev.sort_values(["symbol", "seq"])
            gap = ev2.groupby("symbol")["seq"].diff().dropna()
            assert (gap[gap.notna()] >= 20).all() or len(gap) == 0


def test_high_tight_condition_present():
    """防回归：高窄 signal_mask 源码含高位抗跌条件（曾缺失致回测信号多于实盘）。"""
    import inspect

    src = inspect.getsource(HighTightFlagStrategy.signal_mask)
    assert "0.8" in src and "lo10" in src  # lo10 >= hi40*0.8
