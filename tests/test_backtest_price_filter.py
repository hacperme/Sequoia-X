"""backtest 股价过滤（--min-price）单元测试 —— 只测纯函数，不联网。

`_filter_events_by_price` 是回测里唯一有分支的过滤逻辑：真实的不复权价拉取
（`_raw_close_frame`，走 baostock）在这里用帧替掉。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sequoia_x.backtest import _filter_events_by_price  # noqa: E402


def _events() -> dict[str, pd.DataFrame]:
    return {
        "甲策略": pd.DataFrame({
            "symbol": ["600000", "600001", "600002"],
            "date": pd.to_datetime(["2026-09-21", "2026-09-21", "2026-09-22"]),
            "seq": [10, 11, 12],
        }),
        "空策略": pd.DataFrame({"symbol": [], "date": pd.to_datetime([]), "seq": []}),
    }


def _px() -> pd.DataFrame:
    return pd.DataFrame({
        "symbol": ["600000", "600001"],          # 600002 缺价（停牌/未缓存）
        "date": pd.to_datetime(["2026-09-21", "2026-09-21"]),
        "close": [25.0, 8.5],
    })


def test_disabled_returns_events_untouched():
    ev = _events()
    out, stats = _filter_events_by_price(ev, _px(), 0.0)
    assert out is ev and stats == {}


def test_filter_keeps_only_above_threshold():
    ev = _events()
    out, stats = _filter_events_by_price(ev, _px(), 10.0)
    kept = out["甲策略"]
    assert list(kept["symbol"]) == ["600000"]           # 8.5 剔除、缺价 600002 剔除
    assert list(kept["seq"]) == [10]                     # seq 列保留（后续按 seq 取未来价）
    assert stats["甲策略"] == {"kept": 1, "dropped": 2, "no_price": 1}
    assert stats["空策略"] == {"kept": 0, "dropped": 0, "no_price": 0}


def test_threshold_inclusive_and_date_matched():
    """阈值含等号；价格必须匹配到**信号日**那一行（不同日期不混用）。"""
    ev = {"甲策略": pd.DataFrame({
        "symbol": ["600001", "600002"],
        "date": pd.to_datetime(["2026-09-21", "2026-09-22"]),
        "seq": [1, 2],
    })}
    px = pd.DataFrame({
        "symbol": ["600001", "600002"],
        "date": pd.to_datetime(["2026-09-21", "2026-09-21"]),   # 600002 只有 09-21 的价
        "close": [10.0, 99.0],
    })
    out, stats = _filter_events_by_price(ev, px, 10.0)
    assert list(out["甲策略"]["symbol"]) == ["600001"]   # 10.0 含等号通过
    assert stats["甲策略"]["no_price"] == 1              # 09-22 无价 → 剔除
