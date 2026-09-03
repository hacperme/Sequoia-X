"""A股分板交易规则：涨跌停幅度按板块区分。

主板/中小板 10%，创业板(300/301)/科创板(688) 20%，北交所(4/8/920) 30%。
用于涨停洗盘/上升跌停等依赖涨跌停判定的策略（实盘与回测共用，保口径一致）。

局限（记录在案，不特判）：
- ST/*ST 5% 涨跌停不特判——回测无历史 ST 名单，且 ST 股多被成交额/趋势条件过滤，
  漏判影响小；实盘与回测保持同口径优先。
- 涨跌停判定用涨幅阈值近似（含 0.98 容差覆盖四舍五入到分），非精确涨停价比较——
  后复权价无法做 round(prev*1.1, 2) 精确比较。
"""

LIMIT_MAIN = 0.10   # 主板/中小板（600/601/603/605/000/001/002/003）
LIMIT_GEM = 0.20    # 创业板 300/301、科创板 688
LIMIT_BSE = 0.30    # 北交所 4/8/920 开头
TOL = 0.98          # 涨幅容差系数：覆盖涨停价四舍五入到分（真实涨停 ≥ pct*~0.995）


def limit_pct(symbol: str) -> float:
    """返回该股票的单日涨跌停幅度（小数，如 0.10 = ±10%）。"""
    if symbol.startswith(("300", "301", "688")):
        return LIMIT_GEM
    if symbol.startswith(("4", "8", "920")):
        return LIMIT_BSE
    return LIMIT_MAIN


def limit_up_ratio(symbol: str) -> float:
    """涨停价相对前收倍率（含容差）：1 + pct*TOL。"""
    return 1 + limit_pct(symbol) * TOL


def limit_down_ratio(symbol: str) -> float:
    """跌停价相对前收倍率（含容差）：1 - pct*TOL。"""
    return 1 - limit_pct(symbol) * TOL


def is_limit_up(prev_close: float, close: float, symbol: str) -> bool:
    """今日相对昨收是否达到涨停（close 为后复权或前复权同口径均可，比值口径）。"""
    if prev_close <= 0:
        return False
    return close >= prev_close * limit_up_ratio(symbol)


def is_limit_down(prev_close: float, close: float, symbol: str) -> bool:
    """今日相对昨收是否达到跌停。"""
    if prev_close <= 0:
        return False
    return close <= prev_close * limit_down_ratio(symbol)


def limit_ratio_series(symbols: "list[str]") -> "dict[str, float]":
    """批量：symbol → 涨停倍率映射（回测 panel 建列用）。"""
    return {s: _ratio(s) for s in symbols}
