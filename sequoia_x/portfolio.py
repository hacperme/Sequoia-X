"""组合模拟器 P0：信号 → 持仓 → 退出/止损 → 仓位 → 净值曲线。

设计（评审结论路线 B：不引入 vn.py/AKQuant 运行时，移植其规则模型）：
- 成交模型：每日收盘后决策，次一交易日开盘成交（NextOpen，vn.py FillMode 思想，无前视）
- 入场：信号日 T 收盘确认 → T+1 开盘买入；T+1 开盘触涨停（买不进）→ 放弃并计数
- 退出规则引擎（vn.py CTA 出场类型）：
    time        纯时间退出：持有 hold_days 个交易日 → 次日开盘卖
    stop        固定止损 -stop_loss%（收盘价触发） + 时间退出
    chandelier  吊灯止损：peak_close - k×ATR(14)（收盘价触发） + 时间退出
- 涨跌停卖出约束：T+1 开盘触跌停 → 顺延至首个非跌停开盘日（≤5 日，仍封死则按当日开盘强平）
- 仓位风控（AKQuant RiskConfig 思想）：等权 alloc = capital×pos_size，
    单票一手 100 股，现金不足则降仓，低于一半目标跳过；max-pos 持仓上限 + daily-k 每日新仓上限
- 成本：每边 half = cost_bps/2 bp（佣金+滑点），净扣

输出：逐日净值序列 → 总收益/年化/最大回撤/沪深300超额；平仓交易盈亏/胜率/持仓天数/退出原因分布。

用法：
    python -m sequoia_x.portfolio --strategy 高窄旗形 --exit chandelier --json-out data/portfolio_htf_ch.json
    python -m sequoia_x.portfolio --strategy 海龟突破 --strategy 高窄旗形 --combined --json-out pf.json
    # 选项：--exit time|stop|chandelier；--max-pos/--daily-k/--hold-days/--stop-loss/
    #       --chandelier-k/--capital/--pos-size/--cost-bps；--quality 质量排序；
    #       --combined 多策略联合；--regime-filter 状态机过滤；--risk-budget 状态仓位
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from dotenv import load_dotenv

load_dotenv()

from sequoia_x.core.config import get_settings
from sequoia_x.core.logger import get_logger
from sequoia_x.data.engine import DataEngine
from sequoia_x.backtest import _load_panel, _fetch_index, compute_events

logger = get_logger(__name__)

DEFAULT_HOLD = 20
DEFAULT_STOP = 0.08
CHANDELIER_K = 3.0
ATR_WIN = 14
LOT = 100


def _add_atr(panel: pd.DataFrame) -> pd.DataFrame:
    """预计算 ATR(14)（全表向量化）。"""
    prev = panel["prev_close"]
    tr = pd.concat(
        [panel["high"] - panel["low"],
         (panel["high"] - prev).abs(),
         (panel["low"] - prev).abs()],
        axis=1,
    ).max(axis=1)
    panel["atr"] = tr.groupby(panel["symbol"]).transform(
        lambda s: s.rolling(ATR_WIN).mean()
    )
    return panel


def _add_features(panel: pd.DataFrame) -> pd.DataFrame:
    """信号质量评分所需特征列（全表向量化）。"""
    g = panel.groupby("symbol", sort=False)
    panel["hi40"] = g["high"].transform(lambda s: s.rolling(40).max())
    panel["lo40"] = g["low"].transform(lambda s: s.rolling(40).min())
    panel["hi10"] = g["high"].transform(lambda s: s.rolling(10).max())
    panel["lo10"] = g["low"].transform(lambda s: s.rolling(10).min())
    panel["vol_ma20"] = g["volume"].transform(lambda s: s.rolling(20).mean())
    panel["high20_prev"] = g["high"].transform(
        lambda s: s.shift(1).rolling(20).max()
    )
    return panel


def _score_events(panel: pd.DataFrame, events: pd.DataFrame, name: str) -> pd.DataFrame:
    """给事件附横截面质量分（当日全部信号内 rank 均值，越大越优先入场）。

    策略各自的"信号质量"直觉（P1 研究用，参数非定论）：
    - 高窄旗形：收敛越紧、缩量越深 → 越接近变盘点（rank 1/(hi10/lo10) + rank vol_ma20/volume）
    - 海龟突破：突破幅度越小（刚突破）越新鲜，幅度大=已透支（rank -幅度）
    - 均线放量：放量倍数越大动能越强（rank volume/vol_ma20）
    其余策略无明确排序 → score 恒 0（随机先到先得，与 P0 一致）。
    """
    ev = events.copy()
    need = ["symbol", "date", "hi40", "lo40", "hi10", "lo10", "vol_ma20",
            "volume", "high20_prev", "close"]
    cols = ev[["symbol", "date"]].merge(
        panel[need], on=["symbol", "date"], how="left"
    )
    if name == "高窄旗形":
        m1 = 1 / (cols["hi10"] / cols["lo10"])          # 收敛（触发即 <1.15，越紧越大）
        m2 = cols["vol_ma20"] / cols["volume"]          # 缩量（触发即 <0.6 均量，越深越大）
        score = (
            m1.groupby(cols["date"]).rank(pct=True)
            + m2.groupby(cols["date"]).rank(pct=True)
        ) / 2 * 100
    elif name == "海龟突破":
        m = cols["close"] / cols["high20_prev"] - 1     # 突破幅度，越小=刚突破
        score = (-m).groupby(cols["date"]).rank(pct=True) * 100
    elif name == "均线放量":
        m = cols["volume"] / cols["vol_ma20"]
        score = m.groupby(cols["date"]).rank(pct=True) * 100
    else:
        score = pd.Series(0.0, index=ev.index)
    ev["score"] = score.fillna(0.0).values
    return ev


class PortfolioSim:
    """单策略组合模拟器。"""

    def __init__(
        self,
        panel: pd.DataFrame,
        events: pd.DataFrame,
        capital: float = 1_000_000.0,
        max_pos: int = 10,
        daily_k: int = 5,
        pos_size: float | None = None,
        cost_bps: int = 25,
        hold_days: int = DEFAULT_HOLD,
        exit_mode: str = "time",
        stop_loss: float = DEFAULT_STOP,
        chandelier_k: float = CHANDELIER_K,
        use_risk_budget: bool = False,
        states: pd.DataFrame | None = None,
    ) -> None:
        self.panel = panel
        self.events = events.sort_values(["date", "symbol"]).reset_index(drop=True)
        self.capital = capital
        self.max_pos = max_pos
        self.daily_k = daily_k
        # 单票目标金额：默认满仓等权 capital/max_pos（避免 alloc×max_pos > capital 造成大量现金不足）
        self.alloc = capital * pos_size if pos_size else capital / max_pos
        self.half_cost = cost_bps / 2 / 10000
        self.hold_days = hold_days
        self.exit_mode = exit_mode
        self.stop_loss = stop_loss
        self.chandelier_k = chandelier_k
        self.use_risk_budget = use_risk_budget
        self._states = states if states is not None else pd.DataFrame()

        # 每股价格序列缓存 {symbol: df.set_index(date)}
        self._px: dict[str, pd.DataFrame] = {}
        for s, g in panel.groupby("symbol"):
            self._px[s] = g.set_index("date")

        # 信号按日期聚合（收盘后确认，次日开盘执行）
        # 若事件带 score 列：当日按 score 降序（质量优先入场）；否则保持原序（先到先得）
        self.by_quality = "score" in self.events.columns
        self.signal_by_date: dict[pd.Timestamp, list[str]] = {}
        for d, g in self.events.groupby("date"):
            if self.by_quality:
                g = g.sort_values("score", ascending=False)
            self.signal_by_date[d] = list(g["symbol"])

        self.dates = sorted(panel["date"].unique())

        # 状态
        self.cash = capital
        self.positions: dict[str, dict] = {}      # symbol -> 持仓详情
        self.trades: list[dict] = []
        self._row_cache: dict[tuple, pd.Series | None] = {}  # P1a: 当日 bar 缓存
        self.stats = {"entry_blocked": 0, "skipped_cap": 0, "skipped_cash": 0,
                      "skipped_budget": 0,
                      "exit_extended": 0, "exit_still_blocked": 0,
                      "sell_failed_no_bar": 0}

    # ── 行情查询 ──
    def _row(self, symbol: str, d: pd.Timestamp) -> pd.Series | None:
        """当日 bar（P1a 性能优化：同日重复访问同一持仓走缓存，省 Series 重建）。"""
        key = (symbol, d)
        hit = self._row_cache.get(key)
        if hit is not None:
            return hit if hit is not ... else None
        df = self._px.get(symbol)
        if df is None or d not in df.index:
            self._row_cache[key] = ...
            return None
        row = df.loc[d]
        self._row_cache[key] = row
        return row

    # ── 卖出队列：每日开盘先处理（顺延逻辑）──
    def _process_sells(self, d: pd.Timestamp) -> None:
        for sym in list(self.positions.keys()):
            pos = self.positions[sym]
            if pos.get("sell_pending_since") is None:
                continue
            row = self._row(sym, d)
            if row is None:
                continue  # 停牌无行情，继续挂
            if row["open"] <= row["prev_close"] * row["lim_dn"] * 1.005:
                # 开盘触跌停卖不出 → 顺延
                pos["extend_days"] = pos.get("extend_days", 0) + 1
                self.stats["exit_extended"] += 1
                if pos["extend_days"] > 5:
                    self.stats["exit_still_blocked"] += 1
                    self._close(sym, d, row["open"], pos["sell_reason"], force=True)
                continue
            self._close(sym, d, row["open"], pos["sell_reason"], force=False)

    def _close(self, sym: str, d: pd.Timestamp, px: float, reason: str, force: bool) -> None:
        pos = self.positions.pop(sym)
        proceeds = pos["shares"] * px * (1 - self.half_cost)
        self.cash += proceeds
        ret = (px / pos["entry_px"] - 1) * 100
        hold = int((d - pos["entry_date"]).days)
        self.trades.append({
            "symbol": sym, "entry_date": str(pos["entry_date"].date()),
            "exit_date": str(d.date()), "entry_px": round(pos["entry_px"], 3),
            "exit_px": round(px, 3), "ret_pct": round(ret, 2),
            "hold_days": hold, "reason": reason, "force": force,
        })

    # ── 风险预算（regime → 最大总仓位比例）──
    # 源自 2y 分状态研究：up_low 顺风满仓 / up_high 情绪顶防御 / down_high 控仓抄底 / down_low 谨慎
    RISK_BUDGET = {
        "up_low": 1.0,
        "up_high": 0.4,
        "down_high": 0.6,
        "down_low": 0.3,
        "na": 0.5,  # 状态未知保守半仓
    }

    def _position_limit(self, d: pd.Timestamp) -> float:
        """当日按市场状态允许的最大仓位金额。无 regime 数据则满仓。"""
        if not self.use_risk_budget:
            return float("inf")
        regime = self._regime_on(d)
        return self.capital * self.RISK_BUDGET.get(regime, 0.5)

    def _regime_on(self, d: pd.Timestamp) -> str:
        """日期 d 的市场状态（前向取最近，防未来函数）。"""
        st = self._states.set_index("date")["regime"].sort_index()
        idx = st.index.searchsorted(pd.Timestamp(d), side="right") - 1
        return st.iloc[idx] if idx >= 0 else "na"

    def _portfolio_value(self, d: pd.Timestamp) -> float:
        """当日持仓市值（不含现金，用于仓位上限判断）。"""
        mv = 0.0
        for sym, pos in self.positions.items():
            row = self._row(sym, d)
            if row is not None:
                mv += pos["shares"] * row["close"]
        return mv

    # ── 买入（T+1 开盘执行）──
    def _try_buy(self, sym: str, d: pd.Timestamp) -> None:
        row = self._row(sym, d)
        if row is None:
            return
        if row["open"] >= row["prev_close"] * row["lim_up"] * 0.995:
            self.stats["entry_blocked"] += 1  # 开盘一字/触涨停买不进
            return
        # 风险预算：持仓市值已达状态上限则停买
        if self._portfolio_value(d) >= self._position_limit(d):
            self.stats["skipped_budget"] += 1
            return
        if len(self.positions) >= self.max_pos:
            self.stats["skipped_cap"] += 1
            return
        target = self.alloc * (1 - self.half_cost)
        px = row["open"]
        avail = min(target, self.cash * (1 - self.half_cost))
        if avail < self.alloc * 0.5:
            self.stats["skipped_cash"] += 1
            return
        shares = int(avail / px // LOT) * LOT
        if shares <= 0:
            self.stats["skipped_cash"] += 1
            return
        cost = shares * px * (1 + self.half_cost)
        self.cash -= cost
        self.positions[sym] = {
            "symbol": sym, "entry_date": d, "entry_px": px, "shares": shares,
            "cost": cost, "peak": px, "sell_pending_since": None,
            "sell_reason": None, "extend_days": 0, "bars_held": 0,
        }

    # ── 收盘后：新信号挂买入 + 持仓退出检查 ──
    def _on_close(self, d: pd.Timestamp) -> None:
        # 新信号：次日开盘买（当日收盘确认）
        sigs = self.signal_by_date.get(d, [])
        if sigs and d != self.dates[-1]:  # 最后一日信号无次日可买
            for sym in sigs[: self.daily_k]:
                self._pending_buy[sym] = True
        # 退出检查（收盘判定）
        for sym, pos in list(self.positions.items()):
            if pos["sell_pending_since"] is not None:
                continue  # 已在卖出队列
            row = self._row(sym, d)
            if row is None:
                continue
            pos["bars_held"] += 1
            pos["peak"] = max(pos["peak"], row["close"])
            reason = self._check_exit(pos, row)
            if reason:
                pos["sell_pending_since"] = d
                pos["sell_reason"] = reason

    def _check_exit(self, pos: dict, row: pd.Series) -> str | None:
        close = row["close"]
        if self.exit_mode in ("stop", "chandelier") and close <= 0:
            return None
        if self.exit_mode == "stop":
            if close <= pos["entry_px"] * (1 - self.stop_loss):
                return f"stop-{int(self.stop_loss*100)}%"
        elif self.exit_mode == "chandelier":
            atr = row.get("atr")
            if atr and not np.isnan(atr):
                trail = pos["peak"] - self.chandelier_k * atr
                if close <= trail:
                    return "chandelier"
        if pos["bars_held"] >= self.hold_days:
            return f"time-{self.hold_days}d"
        return None

    # ── 主循环 ──
    def run(self) -> dict:
        self._pending_buy: dict[str, bool] = {}
        nav: list[dict] = []
        for d in self.dates:
            self._row_cache = {}  # P1a: 每日清 bar 缓存（键含日期无需逐日清，防无限增长双保险）
            self._process_sells(d)
            # 开盘执行昨日挂的买单（当日多个信号同日时按信号序；简单逐日先到先得）
            if self._pending_buy:
                for sym in list(self._pending_buy):
                    self._try_buy(sym, d)
                self._pending_buy.clear()
            self._on_close(d)
            # 净值（收盘）
            mv = self.cash
            for sym, pos in self.positions.items():
                row = self._row(sym, d)
                if row is not None:
                    mv += pos["shares"] * row["close"]
            nav.append({"date": str(d.date()), "nav": round(mv, 2)})
        # 期末强平（未卖出持仓按最后收盘价）
        last_d = self.dates[-1]
        for sym in list(self.positions.keys()):
            pos = self.positions[sym]
            row = self._row(sym, last_d)
            px = row["close"] if row is not None else pos["entry_px"]
            self._close(sym, last_d, px, "eod-force", force=True)
        return self._evaluate(nav)

    def _evaluate(self, nav: list[dict]) -> dict:
        s = pd.Series([n["nav"] for n in nav], index=[n["date"] for n in nav])
        if len(s) < 10:
            return {"error": "样本过少"}
        total = float(s.iloc[-1] / s.iloc[0] - 1)
        span = max((pd.Timestamp(s.index[-1]) - pd.Timestamp(s.index[0])).days / 365.25, 1 / 252)
        ann = float((1 + total) ** (1 / span) - 1) if total > -1 else -1.0
        dd = float((s / s.cummax() - 1).min())
        index_df = _fetch_index()
        idx_ret = excess = None
        if index_df is not None and len(index_df) > 5:
            seg = index_df[(index_df["date"] >= pd.Timestamp(s.index[0]))
                           & (index_df["date"] <= pd.Timestamp(s.index[-1]))]
            if len(seg) > 5:
                idx_ret = float(seg["close"].iloc[-1] / seg["close"].iloc[0] - 1)
                excess = total - idx_ret

        tr = pd.DataFrame(self.trades)
        out = {
            "nav_start": s.iloc[0], "nav_end": s.iloc[-1],
            "total_ret": round(total * 100, 1),
            "ann_ret": round(ann * 100, 1),
            "max_drawdown": round(dd * 100, 1),
            "idx_ret": round(idx_ret * 100, 1) if idx_ret is not None else None,
            "excess": round(excess * 100, 1) if excess is not None else None,
            "n_trades": int(len(tr)),
            "win_rate": round(float((tr["ret_pct"] > 0).mean()) * 100, 1) if len(tr) else None,
            "avg_ret": round(float(tr["ret_pct"].mean()), 2) if len(tr) else None,
            "avg_win": round(float(tr.loc[tr["ret_pct"] > 0, "ret_pct"].mean()), 2) if len(tr) and (tr["ret_pct"] > 0).any() else None,
            "avg_loss": round(float(tr.loc[tr["ret_pct"] <= 0, "ret_pct"].mean()), 2) if len(tr) and (tr["ret_pct"] <= 0).any() else None,
            "avg_hold_days": round(float(tr["hold_days"].mean()), 1) if len(tr) else None,
            "reason_dist": {k: int(v) for k, v in tr["reason"].value_counts().items()} if len(tr) else {},
            "stats": self.stats,
        }
        return out


def run_portfolio(
    period: str = "1y",
    strategies: list[str] | None = None,
    exit_mode: str = "time",
    capital: float = 1_000_000.0,
    max_pos: int = 10,
    daily_k: int = 5,
    pos_size: float | None = None,
    cost_bps: int = 25,
    hold_days: int = DEFAULT_HOLD,
    stop_loss: float = DEFAULT_STOP,
    chandelier_k: float = CHANDELIER_K,
    by_quality: bool = False,
    combined: bool = False,
    regime_filter: bool = False,
    risk_budget: bool = False,
    json_out: str | None = None,
) -> dict:
    settings = get_settings()
    engine = DataEngine(settings)
    panel = _load_panel(engine.db_path)   # 已含 lim_up/lim_dn/prev_close（全量口径）
    panel = _add_atr(panel)               # ATR 全量算，过滤后窗口首日即有值
    panel = _add_features(panel)          # 评分特征
    if period.endswith("y"):
        years = int(period[:-1])
        cutoff = panel["date"].max() - pd.DateOffset(years=years)
        panel = panel[panel["date"] > cutoff].reset_index(drop=True)
    panel["seq"] = panel.groupby("symbol").cumcount()

    events = compute_events(panel, strategies=strategies)
    if by_quality:
        events = {n: _score_events(panel, ev, n) for n, ev in events.items()}
    if regime_filter:
        from sequoia_x.regime import get_market_states
        from sequoia_x.strategy_map import apply_regime_filter

        states = get_market_states(engine.db_path, refresh=True)
        events, dropped = apply_regime_filter(events, states)
        logger.info(f"regime 过滤: {dropped}")
    # 风险预算需要市场状态序列
    states_df = None
    if risk_budget:
        from sequoia_x.regime import get_market_states

        states_df = get_market_states(engine.db_path, refresh=True)
    out: dict[str, dict] = {}

    if combined and events:
        # 多策略联合组合：全部策略事件合并，同 (symbol,date) 多策略命中只保留一次
        # （quality 模式保留分最高者；否则保留首个）
        merged = pd.concat(
            [ev.assign(strategy=n) for n, ev in events.items()],
            ignore_index=True,
        )
        if by_quality:
            merged = merged.sort_values("score", ascending=False) \
                .drop_duplicates(subset=["symbol", "date"]).reset_index(drop=True)
        else:
            merged = merged.drop_duplicates(subset=["symbol", "date"]).reset_index(drop=True)
        sim = PortfolioSim(
            panel, merged, capital=capital, max_pos=max_pos, daily_k=daily_k,
            pos_size=pos_size, cost_bps=cost_bps, hold_days=hold_days,
            exit_mode=exit_mode, stop_loss=stop_loss, chandelier_k=chandelier_k,
            use_risk_budget=risk_budget, states=states_df,
        )
        out["多策略联合"] = sim.run()
        logger.info(f"多策略联合组合模拟完成（exit={exit_mode}, quality={by_quality}, budget={risk_budget}）")
    else:
        for name, ev in events.items():
            sim = PortfolioSim(
                panel, ev, capital=capital, max_pos=max_pos, daily_k=daily_k,
                pos_size=pos_size, cost_bps=cost_bps, hold_days=hold_days,
                exit_mode=exit_mode, stop_loss=stop_loss, chandelier_k=chandelier_k,
                use_risk_budget=risk_budget, states=states_df,
            )
            out[name] = sim.run()
            logger.info(f"{name} 组合模拟完成（exit={exit_mode} quality={by_quality} budget={risk_budget}）")
    res = {
        "method": "portfolio-sim-p1",
        "note": (
            f"收盘决策次日开盘成交；exit={exit_mode}；max_pos={max_pos} daily_k={daily_k} "
            f"pos_size={pos_size} hold={hold_days}d 成本{cost_bps}bp；一手100股；"
            f"{'quality 质量排序入场' if by_quality else '先到先得'}"
            f"{'；多策略联合' if combined else ''}"
        ),
        "range": f"{panel['date'].min().date()} ~ {panel['date'].max().date()}",
        "strategies": out,
    }
    if json_out:
        Path(json_out).parent.mkdir(parents=True, exist_ok=True)
        Path(json_out).write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    return res


def _print_res(res: dict) -> None:
    print(f"组合模拟（{res['range']}）| {res['note']}\n")
    for name, s in res["strategies"].items():
        if "error" in s:
            print(f"【{name}】{s['error']}")
            continue
        print(f"【{name}】")
        print(f"  净收益 {s['total_ret']}% | 年化 {s['ann_ret']}% | 最大回撤 {s['max_drawdown']}%"
              + (f" | 沪深300 {s['idx_ret']}% 超额 {s['excess']}%" if s["excess"] is not None else ""))
        print(f"  交易 {s['n_trades']} 笔 | 胜率 {s['win_rate']}% | 均盈 {s['avg_ret']}%"
              f"（赢 {s['avg_win']} / 亏 {s['avg_loss']}）| 均持 {s['avg_hold_days']} 日")
        print(f"  退出分布: {s['reason_dist']}")
        st = s["stats"]
        budget_note = (
            f" | 预算上限跳过 {st['skipped_budget']}" if st.get("skipped_budget", 0) else ""
        )
        print(f"  统计: 入场涨停剔除 {st['entry_blocked']} | 满仓跳过 {st['skipped_cap']}"
              f" | 现金不足 {st['skipped_cash']}{budget_note}"
              f" | 卖出顺延 {st['exit_extended']} 仍封死 {st['exit_still_blocked']}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Sequoia-X 组合模拟器 P0")
    parser.add_argument("--period", default="1y")
    parser.add_argument("--strategy", action="append",
                        help="策略名（可多次；缺省=全部）")
    parser.add_argument("--exit", default="time", choices=["time", "stop", "chandelier"])
    parser.add_argument("--capital", type=float, default=1_000_000.0)
    parser.add_argument("--max-pos", type=int, default=10)
    parser.add_argument("--daily-k", type=int, default=5)
    parser.add_argument("--pos-size", type=float, default=None,
                        help="单票占初始资金比例；缺省=1/max_pos 满仓等权")
    parser.add_argument("--cost-bps", type=int, default=25)
    parser.add_argument("--hold-days", type=int, default=DEFAULT_HOLD)
    parser.add_argument("--stop-loss", type=float, default=DEFAULT_STOP)
    parser.add_argument("--chandelier-k", type=float, default=CHANDELIER_K)
    parser.add_argument("--quality", action="store_true",
                        help="按信号质量分排序入场（默认先到先得）")
    parser.add_argument("--combined", action="store_true",
                        help="多策略联合组合（同一资金池）")
    parser.add_argument("--regime-filter", action="store_true",
                        help="按市场状态机过滤信号（avoid 状态剔除）")
    parser.add_argument("--risk-budget", action="store_true",
                        help="按市场状态缩放总仓位（up_low满仓/up_high 40%/down_high 60%/down_low 30%）")
    parser.add_argument("--json-out")
    args = parser.parse_args()

    res = run_portfolio(
        period=args.period, strategies=args.strategy, exit_mode=args.exit,
        capital=args.capital, max_pos=args.max_pos, daily_k=args.daily_k,
        pos_size=args.pos_size, cost_bps=args.cost_bps, hold_days=args.hold_days,
        stop_loss=args.stop_loss, chandelier_k=args.chandelier_k,
        by_quality=args.quality, combined=args.combined,
        regime_filter=args.regime_filter, risk_budget=args.risk_budget,
        json_out=args.json_out,
    )
    _print_res(res)


if __name__ == "__main__":
    sys.exit(main())
