"""方案 A：日报名单前瞻跟踪器（akquant 独立引擎交叉验证）

思路：日报 T0 收盘产生 cross_hits 共振名单 → T1（下一交易日）开盘用 akquant
引擎等权买入（限价=开盘价，含佣金/滑点；开盘一字涨停跳过买不进）→ 逐日按真实
收盘更新市值 → 统计每批 T+5/T+10/T+20 交易日收益，并与同期沪深300对比。

这是 Sequoia 自研回测之外的"第二引擎"验证：akquant 撮合模型更细
（佣金/印花税/滑点/T+1），若两组数字系统性偏差，说明自研回测口径需修正。

用法：
    python -m sequoia_x.tracker update   # 一键：注册新名单 → 补买入 → 更新 → 报告
    python -m sequoia_x.tracker report   # 只看报告
状态文件：data/track_state.json（含已注册批次、成交、每日市值快照）
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "sequoia_v2.db")
DB_PATH = os.path.abspath(DB_PATH)
STATE_PATH = os.path.join(os.path.dirname(DB_PATH), "track_state.json")
CAPITAL = 1_000_000.0        # 单批名义本金
COMMISSION = 0.00025          # 佣金 万2.5
STAMP_TAX = 0.0005            # 印花税（卖出才收，买入不计）
SLIPPAGE = 0.001              # 滑点 千1
HOLD_DAYS = 20                # 最长跟踪交易日
_BATCH_HDR = {"signal_date", "regime", "status", "entry_date", "n_target", "entries", "equity_dates", "equity_values", "hs300_values"}


# ---------- 数据读取 ----------

def _conn() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH)


def _trading_dates() -> list[str]:
    """全部交易日（升序）。"""
    with _conn() as c:
        rows = c.execute("SELECT DISTINCT date FROM stock_daily ORDER BY date").fetchall()
    return [r[0] for r in rows]


def _next_trade_date(dates: list[str], after: str) -> str | None:
    for d in dates:
        if d > after:
            return d
    return None


def _kline(symbol: str, start: str, end: str) -> pd.DataFrame:
    with _conn() as c:
        df = pd.read_sql(
            "SELECT date,open,high,low,close,volume FROM stock_daily "
            "WHERE symbol=? AND date>=? AND date<=? ORDER BY date",
            c, params=(symbol, start, end),
        )
    return df


def _prev_close(symbol: str, d: str) -> float | None:
    """d 日之前最近一个交易日的收盘价（复权口径与库一致）。"""
    with _conn() as c:
        row = c.execute(
            "SELECT close FROM stock_daily WHERE symbol=? AND date<? ORDER BY date DESC LIMIT 1",
            (symbol, d),
        ).fetchone()
    return row[0] if row else None


def _hs300_value(d: str) -> float | None:
    with _conn() as c:
        row = c.execute(
            "SELECT close FROM index_daily WHERE code='sh.000300' AND date<=? ORDER BY date DESC LIMIT 1",
            (d,),
        ).fetchone()
    return row[0] if row else None


def _limit_up_price(prev_close: float, symbol: str) -> float:
    """分板涨停价（10%/20%/30%），与 market_rules 同源逻辑（防依赖循环内置简化）。"""
    if symbol.startswith(("30", "68")):
        return round(prev_close * 1.20, 2)
    if symbol.startswith(("8", "4", "92")):
        return round(prev_close * 1.30, 2)
    return round(prev_close * 1.10, 2)


# ---------- 状态 ----------

def _load_state() -> dict:
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {"batches": []}


def _save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


# ---------- 注册（读 daily_report.json 的 cross_hits） ----------

def register(state: dict, report_path: str, meta_path: str | None = None) -> int:
    if not os.path.exists(report_path):
        print(f"[register] 无日报文件 {report_path}，跳过")
        return 0
    with open(report_path, encoding="utf-8") as f:
        rep = json.load(f)
    sig_date = rep.get("date")
    cross = rep.get("cross_hits") or []
    if not sig_date or not cross:
        print(f"[register] 日报 {sig_date} 无 cross_hits，跳过")
        return 0
    if any(b["signal_date"] == sig_date for b in state["batches"]):
        print(f"[register] {sig_date} 批次已注册，跳过")
        return 0
    codes = [c["code"] for c in cross]
    state["batches"].append({
        "signal_date": sig_date,
        "regime": (rep.get("regime") or {}).get("regime", "?"),
        "status": "pending",
        "entry_date": None,
        "n_target": len(codes),
        "codes": codes,
        "entries": [],   # [{code, qty, price, date}]
        "equity_dates": [],
        "equity_values": [],
        "hs300_values": [],
    })
    _save_state(state)
    print(f"[register] 注册 {sig_date} 批次：cross_hits {len(codes)} 只 "
          f"({'/'.join(c['name'] for c in cross[:5])}... 等)")
    return len(codes)


# ---------- 买入（akquant 撮合 T1 开盘） ----------

def fill_pending(state: dict, dates: list[str]) -> int:
    """对所有 pending 批次：若下一交易日已收盘（库内有该日数据）则 akquant 开盘买入。"""
    import akquant as aq
    from akquant import Strategy

    latest = dates[-1]
    done = 0
    for b in state["batches"]:
        if b["status"] != "pending":
            continue
        t1 = _next_trade_date(dates, b["signal_date"])
        if t1 is None or t1 > latest:
            print(f"[fill] {b['signal_date']} 批：买入日 {t1} 数据未入库（库最新 {latest}），等待")
            continue

        # 名单 = 该批次注册时的 cross_hits 代码（状态里只存了数量，需从日报重建？）
        # → 注册时已把 codes 存 entries 为空，这里从 state 补存 codes
        # （v1：批内 codes 存于 batch['codes']，注册时写入）
        codes = b.get("codes", [])
        if not codes:
            print(f"[fill] {b['signal_date']} 批无 codes 记录（旧版状态），跳过")
            continue
        # 等权目标仓位
        target_pct = 1.0 / len(codes)

        # 取买入日 K 线（确认可成交 + 捕获开盘价）；市值逐日由 track 更新
        data = {}
        skipped = []
        for code in codes:
            df = _kline(code, t1, t1)
            if df.empty:
                skipped.append(code)
                continue
            pc = _prev_close(code, t1)
            row0 = df.iloc[0]
            # 开盘一字涨停（open=high=low=涨停价）→ 买不进，真实约束
            if pc and row0["open"] >= _limit_up_price(pc, code) - 1e-6:
                if abs(row0["open"] - row0["high"]) < 1e-6 and abs(row0["open"] - row0["low"]) < 1e-6:
                    skipped.append(code)
                    continue
            df = df.copy()
            df["symbol"] = code
            data[code] = df
        if not data:
            print(f"[fill] {b['signal_date']} 批全部无法买入（{len(codes)} 只全一字/无数据），标记 done")
            b["status"] = "done"
            _save_state(state)
            continue

        class EntryStrategy(Strategy):
            fills: list[dict] = []

            def __init__(self, *a, **k):
                super().__init__(*a, **k)
                EntryStrategy.fills = []

            def on_bar(self, bar) -> None:
                px = bar.open
                qty = int(CAPITAL * target_pct / px / 100) * 100
                if qty >= 100:
                    self.buy(symbol=bar.symbol, quantity=qty, price=px)
                    EntryStrategy.fills.append(
                        {"code": bar.symbol, "qty": float(qty), "price": float(px), "date": t1})

        try:
            aq.run_backtest(
                data=data,
                strategy=EntryStrategy,
                symbols=list(data.keys()),
                initial_cash=CAPITAL,
                commission_rate=COMMISSION,
                slippage={"type": "percent", "value": SLIPPAGE},
                timezone="Asia/Shanghai",
            )
        except Exception as e:  # 单批失败不致命
            print(f"[fill] {b['signal_date']} 批 akquant 回测失败: {e}")
            continue

        entries = [f for f in EntryStrategy.fills]
        if not entries:
            print(f"[fill] {b['signal_date']} 批无成交（可能现金不足/全部被拒），标记 done")
            b["status"] = "done"
            _save_state(state)
            continue

        b["entries"] = entries
        b["entry_date"] = t1
        b["status"] = "bought"
        _save_state(state)
        bought_codes = sorted({e["code"] for e in entries})
        skipped_all = sorted(set(skipped) | set(codes) - set(bought_codes))
        print(f"[fill] {b['signal_date']} 批：{t1} 开盘买入 {len(entries)}/{len(codes)} 只"
              + (f"，跳过: {skipped_all}" if skipped_all else ""))
        done += 1
    return done


# ---------- 逐日跟踪 ----------

def track(state: dict) -> None:
    """对 bought 批次：用库内最新真实收盘更新市值与沪深300对照。"""
    latest = max(_trading_dates())
    changed = False
    for b in state["batches"]:
        if b["status"] != "bought":
            continue
        ed = b["entry_date"]
        # 买入日到今天的交易日序列
        with _conn() as c:
            days = [r[0] for r in c.execute(
                "SELECT DISTINCT date FROM stock_daily WHERE date>=? AND date<=? ORDER BY date",
                (ed, latest)).fetchall()]
        if not days:
            continue
        # 逐日市值 = Σ qty×close（用每只股票当日 close）
        equity_series = []
        hs_series = []
        for d in days:
            mv = 0.0
            for e in b["entries"]:
                code = e["code"]
                with _conn() as c:
                    row = c.execute(
                        "SELECT close FROM stock_daily WHERE symbol=? AND date<=? "
                        "ORDER BY date DESC LIMIT 1", (code, d)).fetchone()
                if row:
                    mv += e["qty"] * row[0]
            equity_series.append(round(mv, 2))
            hs_series.append(_hs300_value(d))
        b["equity_dates"] = days
        b["equity_values"] = equity_series
        b["hs300_values"] = [round(v, 4) if v else None for v in hs_series]
        changed = True
    if changed:
        _save_state(state)


# ---------- 报告 ----------

def report(state: dict) -> str:
    out = []
    out.append("=" * 62)
    out.append("📡 方案 A：日报名单前瞻跟踪（akquant 独立引擎）")
    out.append("=" * 62)
    active = 0
    for b in state["batches"]:
        sig = b["signal_date"]
        reg = b["regime"]
        if b["status"] == "pending":
            out.append(f"\n▸ {sig} [{reg}] 待买入（等下一交易日数据）—— {b['n_target']} 只")
            continue
        if b["status"] == "bought" and b["equity_values"]:
            active += 1
            ed = b["entry_date"]
            vals = b["equity_values"]
            dates = b["equity_dates"]
            hs = b["hs300_values"]
            ret_now = (vals[-1] / CAPITAL - 1) * 100
            # 找到 T+5/T+10/T+20 索引（从买入日起第 6/11/21 根 bar 含买入日? 买入日=T1=T+0）
            def ret_at(k: int) -> str:
                # k = 5/10/20 交易日后的收盘
                idx = min(k, len(vals) - 1)
                if k > len(vals) - 1:
                    return f"(未到期,已{len(vals)-1}日){(vals[-1]/CAPITAL-1)*100:+.2f}%"
                return f"{(vals[idx]/CAPITAL-1)*100:+.2f}%"
            hs0 = hs[0] if hs[0] else None
            hs_now = hs[-1] if hs[-1] else None
            hs_ret = (hs_now / hs0 - 1) * 100 if (hs0 and hs_now) else float("nan")
            out.append(f"\n▸ {sig} [{reg}] 买入日 {ed}，{b['n_target']} 只（成交 {len(b['entries'])}）")
            out.append(f"  当前市值 {vals[-1]:,.0f} ｜ 浮盈 {ret_now:+.2f}% ｜ "
                       f"同期沪深300 {hs_ret:+.2f}% ｜ 超额 {ret_now - hs_ret:+.2f}%")
            out.append(f"  T+5 {ret_at(5)} ｜ T+10 {ret_at(10)} ｜ T+20 {ret_at(20)}")
        elif b["status"] == "bought":
            out.append(f"\n▸ {sig} [{reg}] 已买入但无跟踪数据（状态异常）")
    out.append(f"\n批次总计 {len(state['batches'])}，跟踪中 {active}")
    return "\n".join(out)


def update(report_path: str, quiet: bool = False) -> str:
    state = _load_state()
    dates = _trading_dates()
    register(state, report_path)
    fill_pending(state, dates)
    track(state)
    r = report(state)
    if not quiet:
        print(r)
    return r


def main() -> None:
    parser = argparse.ArgumentParser(description="Sequoia 日报名单前瞻跟踪（akquant 交叉验证）")
    parser.add_argument("cmd", nargs="?", default="update", choices=["update", "report"])
    parser.add_argument("--report-json", default="data/daily_report.json")
    args = parser.parse_args()
    report_path = os.path.join(os.path.dirname(__file__), "..", args.report_json)
    if args.cmd == "update":
        update(os.path.abspath(report_path))
    else:
        state = _load_state()
        print(report(state))


if __name__ == "__main__":
    main()
