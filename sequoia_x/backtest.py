"""事件研究法回测引擎 v2（向量化，不改策略代码）。

v2 相对 v1 的修复（2026-09-04，详见 .hermes/plans/2026-09-04-sequoia-review-fix.md）：
1. 分板涨跌停：涨停洗盘/上升跌停按板块幅度（主板10%/双创20%）判定，替代死值 1.095/0.905
2. 海龟成交额直接用库内真实成交额列（turnover 列存 baostock amount），删 volume×close 估算
3. 幸存者偏差：panel = 现役表 UNION 退市表（stock_daily_delisted，2024 后退市 101 只）
4. 成本模型：--cost-bps 双边基点，默认 25；胜率/收益输出 net 口径 + gross_* 原始
5. Wilson 95% 置信区间
6. MAE/止损统计：信号后 10 日内最大不利偏移 + 触发 -5%/-8% 比例
7. 简化组合层：按信号日等权 → 净值曲线 → 年化/最大回撤/相对沪深300 超额

口径：信号日收盘判定 → 次日收盘买入 → 未来 N 交易日收盘卖出；
股价过滤（--min-price）：按**信号日不复权收盘价**（真实盘口价）剔除低价票，
与 `sequoia_x.report --min-price` 同口径；库内 close 是后复权价，不能直接判股价。
胜率 = 净收益 > 0 占比；净收益 = 毛收益 - cost_bps/10000。

用法：
    python -m sequoia_x.backtest --json-out data/backtest_1y.json
    python -m sequoia_x.backtest --period 1y --cost-bps 25 --grid
"""
from __future__ import annotations

import argparse
import json
import math
import os
import signal
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from dotenv import load_dotenv

load_dotenv()

from sequoia_x.core.config import get_settings
from sequoia_x.core.logger import get_logger
from sequoia_x.data.engine import DataEngine
from sequoia_x.market_rules import limit_up_ratio, limit_down_ratio
from sequoia_x.strategy import hold_days_for

logger = get_logger(__name__)

HORIZONS = (5, 10, 20)
MAE_H = 10          # 止损统计窗口（交易日）
STOP_LEVELS = (0.05, 0.08)
DEFAULT_COST_BPS = 25  # 双边合计成本基点（佣金+印花税+滑点）
RAW_CLOSE_DB = "data/raw_close.db"   # 不复权收盘价增量缓存（股价过滤用）


def _load_panel(db_path: str) -> pd.DataFrame:
    """全市场日线面板（现役 UNION 退市），含真实成交额（turnover 列）与分板涨跌停倍率。"""
    with sqlite3.connect(db_path) as conn:
        q = """
        SELECT symbol, date, open, high, low, close, volume, turnover FROM stock_daily
        UNION ALL
        SELECT symbol, date, open, high, low, close, volume, turnover FROM stock_daily_delisted
        """
        df = pd.read_sql(q, conn)
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values(["symbol", "date"]).reset_index(drop=True)
    df["seq"] = df.groupby("symbol").cumcount()
    # 分板涨跌停倍率（逐行，向量化判定用）
    df["lim_up"] = df["symbol"].map(limit_up_ratio)
    df["lim_dn"] = df["symbol"].map(limit_down_ratio)
    # 昨收（涨跌停判定用）
    df["prev_close"] = df.groupby("symbol")["close"].shift(1)
    return df


# ── 全表向量化信号（groupby.transform 一次完成）──

def compute_events(
    panel: pd.DataFrame,
    strategies: list[str] | None = None,
    turtle_window: int | None = None,
    rps_period: int | None = None,
    rps_threshold: int | None = None,
) -> dict[str, pd.DataFrame]:
    """对每个策略算出信号事件表 {strategy: DataFrame[symbol, date, seq]}。

    P0 重构（2026-09-04）：改为**驱动**——调用各策略类 signal_mask()
    （权威向量化实现），不再复制粘贴信号条件（原双实现导致 RPS shift(1)
    不一致、高窄旗形缺高位抗跌等漂移）。panel 需由 _load_panel 准备
    （含 prev_close/lim_up/lim_dn）。

    ⚠️ 参数默认值单一来源（2026-09-17 修）：turtle_window / rps_period /
       rps_threshold 默认均为 None = **沿用各策略类自身的默认属性**，仅在显式
       传参时才覆盖（网格 _run_grid 仍显式传值）。此前本函数与 run_backtest、
       argparse 各自硬编码 90/20 三份副本，且本函数会 setattr 覆盖类属性 ——
       改策略类默认值对回测/组合**不生效**（2026-09-17 A/B 实测踩坑：注入类属性
       后 90 与 95 结果完全相同才发现）。
    """
    from sequoia_x.strategy import STRATEGY_REGISTRY

    # 策略名 → (类, 参数覆盖)，源自中央注册表（仅 backtest=True 参与）。
    registry: dict[str, tuple[type, dict]] = {
        spec.cn_name: (spec.cls, {}) for spec in STRATEGY_REGISTRY if spec.backtest
    }
    # 仅显式传参才覆盖；None 则沿用类默认（单一来源）
    if turtle_window is not None and "海龟突破" in registry:
        registry["海龟突破"] = (registry["海龟突破"][0], {"breakout_window": turtle_window})
    if "RPS 突破" in registry:
        params: dict = {}
        if rps_period is not None:
            params["rps_period"] = rps_period
        if rps_threshold is not None:
            params["rps_threshold"] = rps_threshold
        registry["RPS 突破"] = (registry["RPS 突破"][0], params)

    def mask_to_events(mask: pd.Series) -> pd.DataFrame:
        return panel.loc[mask, ["symbol", "date", "seq"]].reset_index(drop=True)

    def dedupe(ev: pd.DataFrame, cooldown: int = 20) -> pd.DataFrame:
        """同股冷却：同一股票 cooldown 个交易日内只保留首个信号，避免重叠窗口。"""
        if ev.empty:
            return ev
        ev = ev.sort_values(["symbol", "seq"])
        keep: list[bool] = []
        last_seq: dict[str, int] = {}
        for _, row in ev.iterrows():
            prev = last_seq.get(row["symbol"])
            if prev is None or row["seq"] - prev >= cooldown:
                keep.append(True)
                last_seq[row["symbol"]] = row["seq"]
            else:
                keep.append(False)
        return ev.loc[keep].reset_index(drop=True)

    names = strategies or list(registry)
    events: dict[str, pd.DataFrame] = {}
    for name in names:
        cls, params = registry[name]
        strat = cls(engine=None, settings=None)  # signal_mask 纯函数式，不需 engine
        for k, v in params.items():
            setattr(strat, k, v)
        mask = strat.signal_mask(panel)
        events[name] = dedupe(mask_to_events(mask))
        logger.info(f"{name}: {len(events[name])} 信号")
    return events


def _future_close(panel: pd.DataFrame, events: pd.DataFrame, h: int) -> pd.Series:
    """每个事件在 seq+h 日的收盘价。返回按事件行序的 Series（h=1 即入场价）。"""
    if events.empty:
        return pd.Series(dtype=float)
    ev = events[["symbol", "seq"]].reset_index()
    ev.columns = ["eid", "symbol", "seq_in"]
    ev["seq_t"] = ev["seq_in"] + h
    m = ev[["eid", "symbol", "seq_t"]].rename(columns={"seq_t": "seq"}).merge(
        panel[["symbol", "seq", "close"]], on=["symbol", "seq"], how="left"
    )
    m = m.sort_values("eid")
    return m["close"].reset_index(drop=True)


def _exec_ret(
    panel: pd.DataFrame, events: pd.DataFrame, h: int,
    entry_delay: int = 1, max_exit_extend: int = 5,
) -> tuple[list[float], dict]:
    """真实可成交口径收益（评审建议，对齐 AKQuant 撮合约束）：

    - 入场：seq+entry_delay 收盘买入；若当日收盘触及涨停（买不进）→ 事件剔除
    - 退出：seq+h 收盘卖出；若当日收盘触及跌停（卖不出）→ 顺延至首个非跌停日，
      最多顺延 max_exit_extend 个交易日，仍跌停则按最后一日价成交
    - 需 panel 带 prev_close / lim_up / lim_dn 列
    """
    stats = {"entry_blocked": 0, "exit_extended": 0, "exit_still_blocked": 0}
    if events.empty:
        return [], stats
    ev = events[["symbol", "seq"]].reset_index()
    ev.columns = ["eid", "symbol", "seq_in"]

    # 入场价
    ent = ev[["eid", "symbol"]].copy()
    ent["seq"] = ev["seq_in"] + entry_delay
    ent = ent.merge(
        panel[["symbol", "seq", "open", "high", "low", "close",
               "prev_close", "lim_up", "lim_dn"]],
        on=["symbol", "seq"], how="left",
    ).sort_values("eid").reset_index(drop=True)

    # 入场不可成交：收盘触及涨停（涨停价四舍五入容差内）→ 剔除
    blocked = ent["close"] >= ent["prev_close"] * ent["lim_up"] * 0.995
    stats["entry_blocked"] = int(blocked.fillna(False).sum())
    keep = ent.loc[~blocked.fillna(False) & ent["close"].notna()].copy()
    if keep.empty:
        return [], stats
    keep_eids = set(keep["eid"])
    evk = ev[ev["eid"].isin(keep_eids)]

    # 退出：seq+h 起最多顺延 max_exit_extend 日
    ex_rows = []
    for k in range(max_exit_extend + 1):
        tmp = evk[["eid", "symbol"]].copy()
        tmp["seq"] = evk["seq_in"] + h + k
        tmp["k"] = k
        ex_rows.append(tmp)
    ext = pd.concat(ex_rows, ignore_index=True)
    ext = ext.merge(
        panel[["symbol", "seq", "close", "prev_close", "lim_dn"]],
        on=["symbol", "seq"], how="left",
    )
    ext = ext.dropna(subset=["close"])
    ext = ext.sort_values(["eid", "k"])
    # 首个非跌停日（跌停 = close <= prev*lim_dn*1.005）
    ext["is_ld"] = ext["close"] <= ext["prev_close"] * ext["lim_dn"] * 1.005
    ext["exit_ok"] = ~ext["is_ld"]
    # 每事件取首个可卖日；全跌停则取最后一日
    picks = []
    for eid, g in ext.groupby("eid"):
        ok = g[g["exit_ok"]]
        picks.append(g.iloc[0] if ok.empty else ok.iloc[0])
    pick_df = pd.DataFrame(picks)
    stats["exit_extended"] = int((pick_df["k"] > 0).sum())
    stats["exit_still_blocked"] = int(((pick_df["k"] > 0) & ~pick_df["exit_ok"]).sum())

    merged = keep.merge(pick_df[["eid", "close", "k"]].rename(columns={"close": "exit"}),
                        on="eid", how="inner")
    merged = merged.dropna(subset=["close", "exit"])
    if merged.empty:
        return [], stats
    rets = (merged["exit"] / merged["close"] - 1).replace([np.inf, -np.inf], np.nan)
    rets = rets.dropna()
    return [r for r in rets if np.isfinite(r)], stats


def _future_ret(panel: pd.DataFrame, events: pd.DataFrame, h: int) -> list[float]:
    """每事件毛收益：次日收盘买入 → 第 h 日收盘卖出。"""
    if events.empty:
        return []
    entry = _future_close(panel, events, 1)
    exit_ = _future_close(panel, events, h)
    rets = (exit_ / entry - 1).replace([np.inf, -np.inf], np.nan).dropna()
    return [r for r in rets if np.isfinite(r)]


def _wilson_ci(win: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score 95% 置信区间（小样本也稳）。返回百分数。"""
    if n == 0:
        return (0.0, 0.0)
    p = win / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (round(max(centre - half, 0.0) * 100, 1), round(min(centre + half, 1.0) * 100, 1))


def _mae_stats(panel: pd.DataFrame, events: pd.DataFrame, h: int = MAE_H,
               levels: tuple[float, ...] = STOP_LEVELS) -> dict | None:
    """最大不利偏移：信号后 h 日内最低收盘相对入场价的跌幅分布 + 止损触发率。"""
    if events.empty:
        return None
    ev = events[["symbol", "seq"]].reset_index()
    ev.columns = ["eid", "symbol", "seq_in"]
    # 展开每事件到 seq_in+1 .. seq_in+h
    rows = ev.loc[ev.index.repeat(h)].copy()
    rows["k"] = rows.groupby(level=0).cumcount() + 1
    rows = rows.reset_index(drop=True)
    rows["seq"] = rows["seq_in"] + rows["k"]
    rows = rows.merge(panel[["symbol", "seq", "close"]], on=["symbol", "seq"], how="left")
    rows = rows.dropna(subset=["close"])
    ent = ev[["eid", "symbol", "seq_in"]].merge(
        panel[["symbol", "seq", "close"]],
        left_on=["symbol", "seq_in"], right_on=["symbol", "seq"], how="left"
    )[["eid", "close"]].rename(columns={"close": "entry"})
    merged = rows.merge(ent, on="eid")
    merged = merged.dropna(subset=["entry"])
    if merged.empty or merged["entry"].eq(0).any():
        return None
    merged["mae"] = merged["close"] / merged["entry"] - 1  # 负数=浮亏
    worst = merged.groupby("eid")["mae"].min()
    n = len(worst)
    out = {"h": h, "n": int(n)}
    qs = worst.quantile([0.05, 0.25, 0.5, 0.75, 0.95]).round(4)
    for q, v in qs.items():
        out[f"mae_q{int(q*100):02d}"] = float(v)
    for lv in levels:
        out[f"stop{int(lv*100)}"] = round(float((worst <= -lv).mean()) * 100, 1)
    return out


def _portfolio(panel: pd.DataFrame, events: pd.DataFrame, h: int,
               cost_bps: int, index_df: pd.DataFrame | None) -> dict | None:
    """简化组合层：按信号日等权聚合 h 日净收益 → 净值曲线 → 年化/最大回撤/超额。

    诚实口径：'信号日等权滚动指数'——每个有信号的交易日等权持有一组
    未来 h 日收益的均值，非真实重叠持仓模拟。
    """
    if events.empty or len(events) < 10:
        return None
    entry = _future_close(panel, events, 1)
    exit_ = _future_close(panel, events, h)
    rets = (exit_ / entry - 1).replace([np.inf, -np.inf], np.nan)
    tmp = events[["date"]].copy()
    tmp["ret"] = rets.values - cost_bps / 10000
    tmp = tmp.dropna(subset=["ret"])
    if tmp.empty:
        return None
    daily = tmp.groupby("date")["ret"].mean().sort_index()
    if len(daily) < 10:
        return None
    nav = (1 + daily).cumprod()
    total = float(nav.iloc[-1] / nav.iloc[0] - 1)
    n_days = len(daily)
    span_years = max((daily.index[-1] - daily.index[0]).days / 365.25, 1 / 252)
    ann = float((1 + total) ** (1 / span_years) - 1) if total > -1 else -1.0
    peak = nav.cummax()
    dd = (nav / peak - 1).min()
    out = {
        "h": h, "n_days": int(n_days), "n_events": int(len(tmp)),
        "total_ret": round(total * 100, 1),
        "ann_ret": round(ann * 100, 1),
        "max_drawdown": round(float(dd) * 100, 1),
        "span_years": round(span_years, 2),
    }
    if index_df is not None and not index_df.empty:
        # 同期沪深300 区间收益（首信号日 → 末信号日，用指数日线）
        s, e = daily.index[0], daily.index[-1]
        seg = index_df[(index_df["date"] >= s) & (index_df["date"] <= e)]
        if len(seg) > 5:
            idx_ret = float(seg["close"].iloc[-1] / seg["close"].iloc[0] - 1)
            out["idx_ret"] = round(idx_ret * 100, 1)
            out["excess"] = round((total - idx_ret) * 100, 1)
    return out


def _to_bs_market(code: str) -> str:
    return ("sh." if code.startswith(("6", "9")) else "sz.") + code


def _raw_close_frame(symbols: list[str], start: str, end: str,
                     cache_db: str = RAW_CLOSE_DB) -> pd.DataFrame:
    """不复权收盘价（元）→ DataFrame[symbol,date,close]。**股价过滤专用**。

    库内 `stock_daily.close` 是**后复权**价（adjustflag=1），与真实盘口价差异随分红送股
    累积放大 → 不能用它判「股价 < N 元」。这里按 symbol 逐只拉 adjustflag=3（不复权）日线，
    落 sqlite 增量缓存（`fetch_log` 记录已覆盖窗口，覆盖过就跳过，不重复联网）。

    单只查询独立看门狗（SIGALRM，`SEQUOIA_QUERY_TIMEOUT` 默认 25s）→ 到点放弃该只并记
    未覆盖（下轮重试），避免 baostock 收包挂死拖死整个回测（engine.py 同款防护）。
    """
    path = Path(cache_db)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=60)
    try:
        conn.execute("PRAGMA busy_timeout=60000")   # 与并发回测/cron 抢锁时等待而非立即报错
        conn.execute("CREATE TABLE IF NOT EXISTS raw_close "
                     "(symbol TEXT, date TEXT, close REAL, PRIMARY KEY(symbol,date))")
        conn.execute("CREATE TABLE IF NOT EXISTS fetch_log "
                     "(symbol TEXT, start TEXT, end TEXT, fetched_at TEXT)")
        # 增量判据：按**该股缓存内的日期范围**是否覆盖 [start,end]（窗口每天滑动，
        # 不能用「抓取窗口」判据——那样每天都会全量重拉 5000+ 只 ≈ 11 分钟）。
        # 新上市股（min>start）与长期停牌股（max<end）会小幅重拉，量级很小。
        need = []
        for sym in sorted(set(symbols)):
            row = conn.execute("SELECT MIN(date), MAX(date) FROM raw_close WHERE symbol=?",
                               (sym,)).fetchone()
            if not row or not row[0] or row[0] > start or row[1] < end:
                need.append(sym)
        if need:
            import baostock as bs

            timeout = int(os.environ.get("SEQUOIA_QUERY_TIMEOUT", "25"))
            watchdog = True
            try:
                signal.signal(signal.SIGALRM, lambda *_a: (_ for _ in ()).throw(TimeoutError("query timeout")))
            except (ValueError, OSError):   # 非主线程 → 退化为无看门狗
                watchdog = False
            logger.info(f"不复权价缓存：需拉取 {len(need)} 只（{start}~{end}）")
            lg = bs.login()
            if lg.error_code != "0":
                logger.warning("baostock 登录失败，股价过滤将降级（取不到价 → 该股剔除）")
            else:
                fetched = 0
                for sym in need:
                    rows: list = []
                    try:
                        if watchdog:
                            signal.alarm(timeout)
                        rs = bs.query_history_k_data_plus(
                            _to_bs_market(sym), "date,close", start_date=start,
                            end_date=end, frequency="d", adjustflag="3",
                        )
                        while rs.next():
                            rows.append(rs.get_row_data())
                    except TimeoutError:
                        logger.warning(f"{sym} 不复权价查询超时（跳过，下轮重试）")
                        continue
                    finally:
                        if watchdog:
                            signal.alarm(0)
                    if rows:
                        conn.executemany(
                            "INSERT OR REPLACE INTO raw_close VALUES (?,?,?)",
                            [(sym, r[0], float(r[1])) for r in rows if len(r) > 1 and r[1]],
                        )
                    conn.execute("DELETE FROM fetch_log WHERE symbol=?", (sym,))
                    conn.execute("INSERT INTO fetch_log VALUES (?,?,?,datetime('now'))",
                                 (sym, start, end))
                    fetched += 1
                    if fetched % 200 == 0:      # 分批提交：避免整个拉取期（~11 分钟）独占写锁
                        conn.commit()
                conn.commit()
                logger.info(f"不复权价缓存更新完成: {fetched}/{len(need)} 只")
            try:
                bs.logout()
            except Exception:  # noqa: BLE001
                pass
        df = pd.read_sql("SELECT symbol, date, close FROM raw_close", conn)
    finally:
        conn.close()
    if df.empty:
        return pd.DataFrame({"symbol": [], "date": [], "close": []})
    df["date"] = pd.to_datetime(df["date"])
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    return df


def _filter_events_by_price(
    events: dict[str, pd.DataFrame], px: pd.DataFrame, min_price: float,
) -> tuple[dict[str, pd.DataFrame], dict[str, dict]]:
    """按「信号日不复权收盘价 >= min_price」过滤各策略事件表。**纯函数，不联网。**

    取不到价（当日停牌/缓存缺）→ 视为不达标并单列计数（与 report 的
    `count_price_ok` 同口径：宁可少选不选错）。min_price<=0 时原样返回。
    """
    if min_price <= 0:
        return events, {}
    p = px.rename(columns={"close": "raw_close"})
    out: dict[str, pd.DataFrame] = {}
    stats: dict[str, dict] = {}
    for name, ev in events.items():
        if ev.empty:
            out[name], stats[name] = ev, {"kept": 0, "dropped": 0, "no_price": 0}
            continue
        m = ev.merge(p, on=["symbol", "date"], how="left")
        keep = m["raw_close"] >= min_price
        stats[name] = {
            "kept": int(keep.sum()),
            "dropped": int((~keep).sum()),
            "no_price": int(m["raw_close"].isna().sum()),
        }
        cols = [c for c in ("symbol", "date", "seq") if c in m.columns]
        out[name] = m.loc[keep, cols].reset_index(drop=True)
    return out, stats


def _fetch_index(index_code: str = "sh.000300", days_back: int = 800) -> pd.DataFrame | None:
    """取沪深300 日线（后复权 close），供组合层超额对比。

    P2 优化（2026-09-04）：优先读 regime 本地 index_daily 缓存（快、免网络），
    本地缺失再 baostock 现拉；失败返回 None 不阻塞。
    """
    try:
        from sequoia_x.core.config import get_settings
        from sequoia_x.regime import read_index_daily

        db_path = get_settings().db_path
        local = read_index_daily(db_path)
        if not local.empty:
            local = local[local["date"] >= pd.Timestamp.now() - pd.Timedelta(days=days_back)]
            return local.dropna(subset=["close"]).reset_index(drop=True)
    except Exception as exc:
        logger.warning(f"本地指数读取失败（转 baostock）：{exc}")
    try:
        import baostock as bs
        from datetime import date, timedelta
        bs.login()
        try:
            start = (date.today() - timedelta(days=days_back)).isoformat()
            rs = bs.query_history_k_data_plus(
                index_code, "date,close", start_date=start,
                end_date=date.today().isoformat(), frequency="d", adjustflag="3",
            )
            rows = []
            while rs.next():
                rows.append(rs.get_row_data())
            if not rows:
                return None
            df = pd.DataFrame(rows, columns=["date", "close"])
            df["date"] = pd.to_datetime(df["date"])
            df["close"] = pd.to_numeric(df["close"], errors="coerce")
            return df.dropna(subset=["close"])
        finally:
            bs.logout()
    except Exception as exc:
        logger.warning(f"沪深300 拉取失败（跳过超额对比）：{exc}")
        return None


def _stats(rets: list[float], cost_bps: int) -> dict:
    if not rets:
        return {"count": 0}
    arr = np.array(rets)
    net = arr - cost_bps / 10000
    n = len(net)
    ci = _wilson_ci(int((net > 0).sum()), n)
    return {
        "count": int(n),
        "win_rate": round(float((net > 0).mean()) * 100, 1),       # net 口径胜率
        "mean_ret": round(float(net.mean()) * 100, 2),             # net 口径均值
        "median_ret": round(float(np.median(net)) * 100, 2),       # net 口径中位数
        "gross_win_rate": round(float((arr > 0).mean()) * 100, 1),
        "gross_mean_ret": round(float(arr.mean()) * 100, 2),
        "ci_low": ci[0], "ci_high": ci[1],
    }


def run_backtest(
    period: str = "1y",
    json_out: str | None = None,
    cost_bps: int = DEFAULT_COST_BPS,
    turtle_window: int | None = None,
    rps_threshold: int | None = None,
    with_portfolio: bool = True,
    fetch_index: bool = True,
    portfolio_hold: int | None = None,
    min_price: float = 0.0,
) -> dict:
    settings = get_settings()
    engine = DataEngine(settings)
    panel = _load_panel(engine.db_path)

    if period.endswith("y"):
        years = int(period[:-1])
        cutoff = panel["date"].max() - pd.DateOffset(years=years)
        panel = panel[panel["date"] > cutoff].reset_index(drop=True)
    panel["seq"] = panel.groupby("symbol").cumcount()

    events = compute_events(panel, turtle_window=turtle_window, rps_threshold=rps_threshold)

    # 股价下限过滤（信号日不复权收盘价；与 report --min-price 同口径）
    price_filter: dict[str, dict] = {}
    if min_price > 0 and events:
        syms = sorted({str(s) for ev in events.values() for s in ev["symbol"].unique()})
        px = _raw_close_frame(syms, panel["date"].min().date().isoformat(),
                              panel["date"].max().date().isoformat())
        events, price_filter = _filter_events_by_price(events, px, min_price)
        _tot = sum(v.get("dropped", 0) for v in price_filter.values())
        _np = sum(v.get("no_price", 0) for v in price_filter.values())
        logger.info(f"股价过滤 ≥{min_price:g} 元：剔除事件 {_tot} 个（其中取不到价 {_np} 个）")

    index_df = _fetch_index() if (with_portfolio and fetch_index) else None

    result: dict[str, dict] = {}
    for name, ev in events.items():
        result[name] = {}
        for h in HORIZONS:
            rets = _future_ret(panel, ev, h)
            result[name][h] = _stats(rets, cost_bps)
            # 真实可成交口径（入场触涨停剔除 / 退出触跌停顺延）
            exec_rets, ex_stats = _exec_ret(panel, ev, h)
            st = _stats(exec_rets, cost_bps)
            st.update({
                "entry_blocked": ex_stats["entry_blocked"],
                "exit_extended": ex_stats["exit_extended"],
                "exit_still_blocked": ex_stats["exit_still_blocked"],
                "total_signals": int(len(ev)),
                "executable": int(st.get("count", 0)),
                "ideal_count": int(len(rets)),
            })
            result[name][f"{h}_exec"] = st
        # MAE/止损统计（固定 10 日窗口）
        result[name]["mae"] = _mae_stats(panel, ev)
        if price_filter:
            result[name]["min_price_filter"] = price_filter.get(name, {})
        # 简化组合层（持有期按策略注册表定档；portfolio_hold 显式指定时统一覆盖）
        if with_portfolio:
            h = portfolio_hold if portfolio_hold is not None else hold_days_for(name)
            result[name]["portfolio"] = _portfolio(panel, ev, h, cost_bps, index_df)
        logger.info(f"{name} 回测完成")

    out = {
        "method": "event-study-vectorized-v2",
        "note": (
            f"信号日收盘判定 → 次日收盘买入 → 未来N交易日收盘卖出；"
            f"胜率/收益为 net 口径（已扣双边成本 {cost_bps}bp）；"
            f"含 2024 后退市股（消除幸存者偏差）；分板涨跌停；后复权价"
            + (f"；已按信号日不复权收盘价过滤股价 < {min_price:g} 元" if min_price > 0 else "")
        ),
        "range": f"{panel['date'].min().date()} ~ {panel['date'].max().date()}",
        "n_stocks": int(panel["symbol"].nunique()),
        "n_rows": int(len(panel)),
        "cost_bps": cost_bps,
        "min_price": min_price,
        "strategies": result,
    }
    if json_out:
        Path(json_out).parent.mkdir(parents=True, exist_ok=True)
        Path(json_out).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def _run_grid(json_out: str | None = None, cost_bps: int = DEFAULT_COST_BPS) -> dict:
    """参数网格：海龟通道 {20,40,55}（经典海龟双通道）+ RPS 阈值 {80,90,95}。"""
    settings = get_settings()
    engine = DataEngine(settings)
    panel = _load_panel(engine.db_path)
    years = 1
    cutoff = panel["date"].max() - pd.DateOffset(years=years)
    panel = panel[panel["date"] > cutoff].reset_index(drop=True)
    panel["seq"] = panel.groupby("symbol").cumcount()

    grid: dict[str, dict] = {"海龟突破": {}, "RPS 突破": {}}
    for w in (20, 40, 55):
        ev = compute_events(panel, strategies=["海龟突破"], turtle_window=w)[
            "海龟突破"]
        grid["海龟突破"][str(w)] = {
            "n": int(len(ev)),
            "h": {str(h): {k: _stats(_future_ret(panel, ev, h), cost_bps)[k]
                           for k in ("count", "win_rate", "mean_ret")} for h in HORIZONS},
        }
    for t in (80, 90, 95):
        ev = compute_events(panel, strategies=["RPS 突破"], rps_threshold=t)[
            "RPS 突破"]
        grid["RPS 突破"][str(t)] = {
            "n": int(len(ev)),
            "h": {str(h): {k: _stats(_future_ret(panel, ev, h), cost_bps)[k]
                           for k in ("count", "win_rate", "mean_ret")} for h in HORIZONS},
        }

    # 打印对比表
    print(f"参数网格（近 1 年，net 口径含 {cost_bps}bp 成本）\n")
    for strat, params in grid.items():
        print(f"【{strat}】")
        for p, v in params.items():
            row = f"  参数 {p:>4}: n={v['n']:>6}"
            for h in (5, 10, 20):
                s = v["h"][str(h)]
                wr = s["win_rate"] if s["count"] else float("nan")
                row += f" | {h}日胜率 {wr}%"
            print(row)
        print()
    out = {"method": "param-grid", "range": f"{panel['date'].min().date()} ~ {panel['date'].max().date()}",
           "cost_bps": cost_bps, "grid": grid}
    if json_out:
        Path(json_out).parent.mkdir(parents=True, exist_ok=True)
        Path(json_out).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Sequoia-X 向量化事件研究回测 v2")
    parser.add_argument("--period", default="1y", help="回测期间（1y/2y/3y 或全量 all）")
    parser.add_argument("--json-out", help="结果写 JSON")
    parser.add_argument("--cost-bps", type=int, default=DEFAULT_COST_BPS,
                        help="双边合计成本基点（默认 25 = 0.25%）")
    parser.add_argument("--turtle-window", type=int, default=None,
                        help="海龟突破通道窗口（缺省=策略类默认 20）")
    parser.add_argument("--rps-threshold", type=int, default=None,
                        help="RPS 分位阈值（缺省=策略类默认 95，2026-09-17 起）")
    parser.add_argument("--no-portfolio", action="store_true", help="跳过组合层")
    parser.add_argument("--portfolio-hold", type=int, default=None,
                        help="组合层持有期(交易日)；缺省=按策略注册表定档")
    parser.add_argument("--no-index", action="store_true", help="跳过沪深300 超额对比")
    parser.add_argument("--grid", action="store_true", help="参数网格模式")
    parser.add_argument("--min-price", type=float, default=0.0,
                        help="股价下限（元，信号日不复权收盘价；0=不过滤）")
    args = parser.parse_args()

    if args.grid:
        _run_grid(json_out=args.json_out, cost_bps=args.cost_bps)
        return

    res = run_backtest(
        period=args.period, json_out=args.json_out, cost_bps=args.cost_bps,
        turtle_window=args.turtle_window, rps_threshold=args.rps_threshold,
        with_portfolio=not args.no_portfolio, fetch_index=not args.no_index,
        portfolio_hold=args.portfolio_hold, min_price=args.min_price,
    )
    print(f"回测区间 {res['range']} | {res['n_stocks']} 只(含退市) | {res['n_rows']} 行 | 成本 {args.cost_bps}bp\n")
    for name, horizons in res["strategies"].items():
        parts = []
        for h in (5, 10, 20):
            s = horizons.get(h) or {}
            ex = horizons.get(f"{h}_exec") or {}
            if s.get("count"):
                parts.append(
                    f"{h}日: 理想{s['win_rate']}%/{s['mean_ret']}%"
                )
                if ex.get("executable"):
                    parts.append(
                        f"可成交{ex['win_rate']}%/{ex['mean_ret']}%"
                        f"(剔{ex['entry_blocked']}顺延{ex['exit_extended']})"
                    )
            else:
                parts.append(f"{h}日: 无")
        pf = horizons.get("portfolio")
        pf_s = ""
        if pf:
            pf_s = f" | 组合: 年化{pf['ann_ret']}% 回撤{pf['max_drawdown']}%" \
                   + (f" 超额{pf['excess']}%" if "excess" in pf else "")
        mae = horizons.get("mae")
        mae_s = ""
        if mae:
            mae_s = f" | MAE: q50={mae.get('mae_q50')} 触发-5%={mae.get('stop5')}%"
        print(f"【{name}】{' | '.join(parts)}{pf_s}{mae_s}")


if __name__ == "__main__":
    sys.exit(main())
