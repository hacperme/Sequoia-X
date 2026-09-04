"""market_rules 分板涨跌停单测 + 统计函数已知值。"""
import numpy as np
import pandas as pd

from sequoia_x.market_rules import (
    limit_down_ratio, limit_up_ratio, is_limit_up, is_limit_down,
    limit_pct,
)
from sequoia_x.backtest import _wilson_ci, _stats


class TestLimitRules:
    def test_ratios(self):
        # 主板 ±10%（容差 0.98）：1.098 / 0.902
        assert abs(limit_up_ratio("600000") - 1.098) < 1e-9
        assert abs(limit_down_ratio("600000") - 0.902) < 1e-9
        # 双创 ±20%：1.196 / 0.804
        assert abs(limit_up_ratio("300750") - 1.196) < 1e-9
        assert abs(limit_up_ratio("688981") - 1.196) < 1e-9
        # 北交所 ±30%：1.294
        assert abs(limit_up_ratio("832566") - 1.294) < 1e-9
        assert limit_pct("920001") == 0.30

    def test_is_limit_up_boundaries(self):
        # 主板：+9.6% 未达（旧版死值 1.095 误判）；+9.9% 达（四舍五入涨停）
        assert not is_limit_up(10.0, 10.96, "600000")
        assert is_limit_up(10.0, 10.99, "600000")
        assert is_limit_up(10.0, 11.2, "000001")  # 越过涨停线
        # 创业板：+15% 未涨停（旧版误判）；+19.7% 涨停
        assert not is_limit_up(10.0, 11.5, "300750")
        assert is_limit_up(10.0, 11.97, "300750")

    def test_is_limit_down_boundaries(self):
        assert not is_limit_down(10.0, 9.04, "600000")  # -9.6% 旧版误判
        assert is_limit_down(10.0, 9.01, "600000")      # -9.9%
        assert not is_limit_down(10.0, 8.8, "300750")   # 创业板 -12% 未跌停


class TestStats:
    def test_wilson_known_value(self):
        # Wilson(450/1000) 标准 95% CI ≈ [41.9, 48.1]
        lo, hi = _wilson_ci(450, 1000)
        assert abs(lo - 41.9) < 0.2 and abs(hi - 48.1) < 0.2

    def test_stats_net_offset(self):
        # 5 赢 5 亏对称 → 毛胜率 50%；扣 25bp 后净均值 < 毛均值
        rets = [0.01] * 5 + [-0.01] * 5
        s = _stats(rets, 25)
        assert s["gross_win_rate"] == 50.0
        assert s["mean_ret"] < s["gross_mean_ret"]
        assert s["count"] == 10

    def test_stats_empty(self):
        assert _stats([], 25) == {"count": 0}
