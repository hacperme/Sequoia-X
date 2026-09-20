"""trading_calendar 纯函数单测（不触网）。"""
from datetime import date, datetime

from sequoia_x.trading_calendar import (
    CLOSE_HOUR,
    beijing_now,
    is_trading_day_in,
    last_closed_trading_day,
)


def _weekday_cal(extra_holidays=()):
    """构造假日历：周一~周五为交易日，可额外指定节假日。"""
    hol = {d.isoformat() for d in extra_holidays}

    def is_trading(d: date):
        if d.isoformat() in hol:
            return False
        return d.weekday() < 5

    return is_trading


def test_after_close_on_trading_day_returns_today():
    # 2026-09-18 是周五
    now = datetime(2026, 9, 18, CLOSE_HOUR, 0)
    assert last_closed_trading_day(now, _weekday_cal()) == date(2026, 9, 18)


def test_before_close_returns_previous_trading_day():
    now = datetime(2026, 9, 18, 10, 0)
    assert last_closed_trading_day(now, _weekday_cal()) == date(2026, 9, 17)


def test_weekend_rolls_back_over_saturday_and_sunday():
    now = datetime(2026, 9, 20, 12, 0)  # 周日
    assert last_closed_trading_day(now, _weekday_cal()) == date(2026, 9, 18)


def test_holiday_on_weekday_rolls_back():
    """节假日（非周末）必须回退到上一个交易日 —— 旧逻辑只跳周末会算错。"""
    # 2026-09-21(周一)、09-22(周二) 设为节假日
    cal = _weekday_cal(extra_holidays=(date(2026, 9, 21), date(2026, 9, 22)))
    now = datetime(2026, 9, 22, 20, 0)
    assert last_closed_trading_day(now, cal) == date(2026, 9, 18)


def test_unknown_calendar_returns_none():
    """日历未知时返回 None，调用方须回退旧逻辑而非静默出错。"""
    now = datetime(2026, 9, 20, 12, 0)
    assert last_closed_trading_day(now, lambda d: None) is None


def test_is_trading_day_in_lookup():
    cal = {"2026-09-18": True, "2026-09-19": False}
    assert is_trading_day_in(date(2026, 9, 18), cal) is True
    assert is_trading_day_in(date(2026, 9, 19), cal) is False
    assert is_trading_day_in(date(2026, 9, 20), cal) is None


def test_beijing_now_is_utc_plus_8():
    delta = beijing_now() - datetime.utcnow()
    assert 7.9 < delta.total_seconds() / 3600 < 8.1
