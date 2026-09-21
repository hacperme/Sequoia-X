"""组合模拟器回归测试：已持仓股票不重复建仓（防资金泄漏）。

背景（2026-09-21 实测发现）：`PortfolioSim._try_buy` 原先没有「已持有该股则跳过」的检查。
当 `hold_days` 超过信号冷却期（20 交易日）时，同一只股票的第二次信号会在旧仓仍持有期间
到达 → `self.positions[sym] = {...}` 直接覆盖旧仓 → 旧仓的成本凭空消失、trades 少记一笔。
实测 RPS/2y/hold=30：857 次买入 vs 756 笔平仓、211 次覆盖 → NAV 假摔 -86%（真值应由
逐笔盈亏决定，量级相差 ~13 倍）。该 bug 会污染所有 hold_days > 冷却期的回测结果。
"""
import pandas as pd
import pytest

from sequoia_x.market_rules import limit_down_ratio, limit_up_ratio
from sequoia_x.portfolio import PortfolioSim

CAPITAL = 1_000_000.0


def _flat_panel(symbol: str = "600000", n_days: int = 60) -> pd.DataFrame:
    """价格恒为 10 元的平台面板：任何净值变化只来自成本与记账错误。"""
    dates = pd.bdate_range("2025-01-01", periods=n_days)
    df = pd.DataFrame({
        "symbol": symbol, "date": dates,
        "open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0,
        "volume": 2_000_000.0, "turnover": 2.0e7,
    })
    df["seq"] = range(len(df))
    df["lim_up"] = limit_up_ratio(symbol)
    df["lim_dn"] = limit_down_ratio(symbol)
    df["prev_close"] = df["close"].shift(1).fillna(df["close"])
    return df


def _sim(panel: pd.DataFrame, events: pd.DataFrame, hold_days: int) -> PortfolioSim:
    return PortfolioSim(
        panel, events, capital=CAPITAL, max_pos=100, daily_k=10,
        cost_bps=25, hold_days=hold_days, exit_mode="time",
    )


def test_same_symbol_signal_while_held_is_skipped():
    """持有期内收到同股二次信号 → 拒绝建仓（skipped_held=1），不覆盖旧仓。"""
    panel = _flat_panel()
    d_first, d_second = panel["date"].iloc[5], panel["date"].iloc[15]
    events = pd.DataFrame({"symbol": ["600000", "600000"], "date": [d_first, d_second]})

    sim = _sim(panel, events, hold_days=30)  # 30 > 20 交易日冷却期
    res = sim.run()

    assert sim.stats["skipped_held"] == 1, "同股二次信号应被 skipped_held 拒绝"
    assert len(sim.trades) == 1, "只应有一次建仓"
    # 修复前：旧仓 1 万元成本被吞 → 净值跌约 -1%；修复后仅成本拖累（~23 元）
    assert res["total_ret"] > -0.1, f"净值异常下跌 {res['total_ret']}%，疑似资金泄漏"


def test_cash_reconciles_with_trades():
    """现金流对账：期末现金 = 本金 − 买入成本 + 卖出所得（不容许凭空蒸发）。"""
    panel = _flat_panel(n_days=80)
    events = pd.DataFrame({
        "symbol": ["600000", "600000", "600000"],
        "date": [panel["date"].iloc[d] for d in (5, 15, 25)],
    })
    sim = _sim(panel, events, hold_days=40)
    buys = {"n": 0, "cost": 0.0}
    sells = {"n": 0, "proceeds": 0.0}
    orig_buy, orig_close = sim._try_buy, sim._close

    def buy(sym, d):
        before = sim.cash
        orig_buy(sym, d)
        if sim.cash < before:
            buys["n"] += 1
            buys["cost"] += before - sim.cash

    def close(sym, d, px, reason, force):
        before = sim.cash
        orig_close(sym, d, px, reason, force)
        sells["n"] += 1
        sells["proceeds"] += sim.cash - before

    sim._try_buy, sim._close = buy, close
    res = sim.run()

    expected = CAPITAL - buys["cost"] + sells["proceeds"]
    assert buys["n"] == len(sim.trades) + len(sim.positions), (
        f"买入 {buys['n']} 次与平仓 {len(sim.trades)} 笔 + 期末持仓 {len(sim.positions)} 不匹配"
        "（差异=被覆盖吞掉的仓位）"
    )
    assert abs(sim.cash - expected) < 1.0, f"现金 {sim.cash:.2f} ≠ 流水对账 {expected:.2f}"
    assert res["n_trades"] == buys["n"] == 1, "同股连续信号只应建仓一次"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
