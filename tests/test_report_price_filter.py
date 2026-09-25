"""report.py 股价下限过滤（--min-price）单元测试 —— 全程不联网。

真实路径里「不复权收盘价」与「流通市值」来自同一次 baostock 查询
（MarketCapLookup.fetch），这里用桩替掉，只验过滤语义与计数口径：

- min_price <= 0 = 关闭，行为与加过滤前一致（研究/回测口径不受影响）
- min_price > 0 时：各策略 TOP 与共振票都必须剔除股价不达标的票
- count_in_range 语义保持「仅市值区间」（wrapper 的备援重试防呆依赖它），
  过滤后计数单列 count_price_ok
- 取不到价格（当日停牌 → 0.0）在过滤开启时被剔除
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sequoia_x.report import ReportBuilder  # noqa: E402

CAPS = {"600000": 500.0, "600001": 300.0, "600002": 120.0, "600003": 800.0}
PRICES = {"600000": 25.00, "600001": 8.50, "600002": 9.99, "600003": 12.00}
NAMES = {"600000": "甲股", "600001": "乙股", "600002": "丙股", "600003": "丁股"}


class _FakeMeta:
    names = dict(NAMES)

    def load(self) -> None:  # noqa: D102
        return None

    def is_junk(self, code: str) -> bool:  # noqa: D102
        return False


class _FakeCaps:
    def fetch(self, codes, as_of=None) -> None:  # noqa: D102
        return None

    def latest_trade_date(self) -> str:  # noqa: D102
        return "2026-09-22"

    def get(self, code: str) -> float:  # noqa: D102
        return CAPS.get(code, 0.0)

    def get_price(self, code: str) -> float:  # noqa: D102
        return PRICES.get(code, 0.0)


class _FakeEngine:
    db_path = "/nonexistent/sequoia_price_filter_test.db"


def _strategy(hits: list[str]):
    class _S:
        def __init__(self, engine=None, settings=None) -> None:
            pass

        def run(self) -> list[str]:
            return list(hits)

    return _S


def _builder(min_price: float) -> ReportBuilder:
    """构造 ReportBuilder 并保留桩：甲策略命中 A/B/C，乙策略命中 B/C/D（B、C 共振）。"""
    b = ReportBuilder(_FakeEngine(), strategies=None, min_price=min_price)
    b.strategies = [
        (_strategy(["600000", "600001", "600002"]), "甲策略"),
        (_strategy(["600001", "600002", "600003"]), "乙策略"),
    ]
    b.meta = _FakeMeta()
    b.caps = _FakeCaps()
    return b


def _by_name(result: dict) -> dict:
    return {s["name"]: s for s in result["strategies"]}


def test_min_price_off_keeps_legacy_behavior():
    """min_price=0：不过滤，count_in_range == count_price_ok，共振票齐全。"""
    res = _builder(0.0).build()
    assert res["min_price"] == 0.0
    jia = _by_name(res)["甲策略"]
    assert jia["count_total"] == 3
    assert jia["count_in_range"] == 3
    assert jia["count_price_ok"] == 3
    assert jia["top"] == ["600000", "600001", "600002"]  # 市值降序
    assert sorted(c["code"] for c in res["cross_hits"]) == ["600001", "600002"]


def test_min_price_filters_strategy_top():
    """min_price=10：8.50 / 9.99 元的票被剔出 TOP，计数单列。"""
    res = _builder(10.0).build()
    jia = _by_name(res)["甲策略"]
    assert jia["count_total"] == 3
    assert jia["count_in_range"] == 3          # 市值区间口径不变
    assert jia["count_price_ok"] == 1          # 仅 600000（25.00 元）
    assert jia["top"] == ["600000"]
    yi = _by_name(res)["乙策略"]
    assert yi["top"] == ["600003"]             # 12.00 元
    assert yi["count_price_ok"] == 1


def test_min_price_filters_cross_hits_and_carries_close():
    """共振票同样过滤；条目带不复权收盘价，便于日报展示。"""
    res10 = _builder(10.0).build()
    assert res10["cross_hits"] == []
    res9 = _builder(9.0).build()
    codes = [c["code"] for c in res9["cross_hits"]]
    assert codes == ["600002"]                 # 9.99 ≥ 9，8.50 < 9
    assert res9["cross_hits"][0]["close"] == 9.99
    assert res9["cross_hits"][0]["cap_yi"] == 120.0
    assert res9["cross_hits"][0]["strategies"] == ["乙策略", "甲策略"]


def test_missing_price_is_excluded_when_filter_on():
    """取不到价格（停牌 → 0.0）：过滤开启时剔除，关闭时保留（兼容旧行为）。"""
    b = _builder(10.0)
    b.strategies = [(_strategy(["999999"]), "甲策略")]
    b.caps = _FakeCaps()
    CAPS["999999"] = 200.0                     # 有市值但无价格
    try:
        res = b.build()
        assert _by_name(res)["甲策略"]["count_price_ok"] == 0
        assert _by_name(res)["甲策略"]["top"] == []
        assert _builder(0.0).build()["min_price"] == 0.0
    finally:
        CAPS.pop("999999", None)
