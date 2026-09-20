"""交易日历与数据发布探测（baostock）。

背景（2026-09-20 自检）：原 wrapper 的交易日守卫只比较「库内最新数据日」与
「日历最近已收盘日」，且日历回退只跳周末、不识别节假日，导致两类误判：
  1. 交易日但 baostock 尚未发布当日数据 → 误报 NO_TRADING_DAY，整条流水线短路
     （2026-09-18 实例：20:00 同步完仍只有 09-17 数据，数据实际 21:00 才发布）；
  2. 法定节假日（非周末）→ recent_closed 算错。
本模块用 baostock 的权威交易日历（query_trade_dates）替代日历推算，并把
「真休市」与「数据未发布」彻底分开：前者 NO_TRADING_DAY，后者 DATA_NOT_READY。

纯函数（last_closed_trading_day / is_trading_day_in）接受 is_trading 回调，
便于单测注入假日历；网络访问集中在 fetch_trade_dates / probe_published。
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Optional

# 数据发布探针用样本（流动性好、几乎不可能停牌）：任一有当日收盘价即视为已发布
PROBE_SYMBOLS = ("sh.600000", "sz.000001", "sh.600519", "sz.300750")

# 日历回溯/前视窗口（天）
CAL_LOOKBACK = 40
CAL_LOOKAHEAD = 10

# 当日算作「已收盘」的小时（北京时间）。A 股 15:00 收盘。
CLOSE_HOUR = 15


def beijing_now() -> datetime:
    """当前北京时间（naive，避免依赖 tzdata）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=8)


def is_trading_day_in(day: date, trade_dates: dict[str, bool]) -> Optional[bool]:
    """查日历。日历中无该日记录时返回 None（未知）。"""
    return trade_dates.get(day.isoformat())


def last_closed_trading_day(
    now: datetime,
    is_trading: Callable[[date], Optional[bool]],
    max_back: int = CAL_LOOKBACK,
) -> Optional[date]:
    """最近一个「已收盘」的交易日。

    - 当天是交易日且已过 CLOSE_HOUR → 返回当天
    - 否则 → 返回当天之前最近的一个交易日（自然跨周末与节假日）
    - 日历未知（返回 None）或回溯超限 → 返回 None，调用方应回退旧逻辑
    """
    d = now.date()
    if not (is_trading(d) and now.hour >= CLOSE_HOUR):
        d -= timedelta(days=1)
    for _ in range(max_back):
        if is_trading(d):
            return d
        d -= timedelta(days=1)
    return None


# ───────────────────────── 网络层（baostock） ─────────────────────────


def _login(bs):
    return bs.login()


def fetch_trade_dates(
    start: Optional[date] = None, end: Optional[date] = None
) -> Optional[dict[str, bool]]:
    """取交易日历 {iso_date: is_trading_day}。失败返回 None。"""
    try:
        import baostock as bs
    except Exception:
        return None
    now = beijing_now()
    start = start or (now.date() - timedelta(days=CAL_LOOKBACK))
    end = end or (now.date() + timedelta(days=CAL_LOOKAHEAD))
    lg = _login(bs)
    if lg.error_code != "0":
        return None
    try:
        rs = bs.query_trade_dates(start_date=start.isoformat(), end_date=end.isoformat())
        out: dict[str, bool] = {}
        while rs.next():
            row = rs.get_row_data()
            out[row[0]] = row[1] == "1"
        return out or None
    except Exception:
        return None
    finally:
        try:
            bs.logout()
        except Exception:
            pass


def probe_published(day: date, symbols: tuple[str, ...] = PROBE_SYMBOLS) -> Optional[bool]:
    """探测某交易日的日 K 是否已发布。

    True  = 已发布（至少一个样本有收盘价）
    False = 未发布（样本全空；视为数据未就绪，应等待重试）
    None  = 未知（baostock 异常/不可达；调用方应直接继续，勿因此阻塞）
    """
    try:
        import baostock as bs
    except Exception:
        return None
    lg = _login(bs)
    if lg.error_code != "0":
        return None
    try:
        d = day.isoformat()
        for sym in symbols:
            try:
                rs = bs.query_history_k_data_plus(
                    sym,
                    "date,close",
                    start_date=d,
                    end_date=d,
                    frequency="d",
                    adjustflag="3",
                )
                while rs.next():
                    row = rs.get_row_data()
                    if row and len(row) > 1 and row[1].strip():
                        return True
            except Exception:
                continue
        return False
    except Exception:
        return None
    finally:
        try:
            bs.logout()
        except Exception:
            pass


# ───────────────────────── CLI（供 wrapper 调用） ─────────────────────────


def _cli(argv: list[str]) -> int:
    """用法:
      --recent-closed            输出 IS_TRADING_TODAY=1/0/UNKNOWN RECENT_CLOSED=<date|UNKNOWN>
      --probe <YYYY-MM-DD>       输出 PUBLISHED=1/0/UNKNOWN
    """
    if "--recent-closed" in argv:
        now = beijing_now()
        cal = fetch_trade_dates()
        if not cal:
            print("IS_TRADING_TODAY=UNKNOWN RECENT_CLOSED=UNKNOWN")
            return 0
        today_flag = cal.get(now.date().isoformat())
        is_today = "UNKNOWN" if today_flag is None else ("1" if today_flag else "0")
        rc = last_closed_trading_day(now, lambda d: is_trading_day_in(d, cal))
        print(f"IS_TRADING_TODAY={is_today} RECENT_CLOSED={rc.isoformat() if rc else 'UNKNOWN'}")
        return 0

    if "--probe" in argv:
        i = argv.index("--probe")
        try:
            day = date.fromisoformat(argv[i + 1])
        except (IndexError, ValueError):
            print("PUBLISHED=UNKNOWN")
            return 0
        v = probe_published(day)
        print("PUBLISHED=UNKNOWN" if v is None else f"PUBLISHED={1 if v else 0}")
        return 0

    print("usage: python -m sequoia_x.trading_calendar --recent-closed | --probe <date>")
    return 2


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
