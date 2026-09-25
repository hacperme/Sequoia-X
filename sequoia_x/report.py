"""选股结果报告模块。

把 daily_report 的过滤/聚合/输出逻辑提炼为正式模块，供 main.py 与独立脚本复用：

    python -m sequoia_x.report --json-out data/last_result.json
    python -m sequoia_x.report --markdown           # 打印 markdown 日报

特性：
- 股票元数据缓存：名称 / 上市日期（baostock 一次性拉取）
- 过滤：排除 ST/*ST/退市整理 + 上市不满 min_ipo_days 天的新股 + 股价下限（min_price，0=关）
- 流通市值：baostock 换手率反推（不复权收盘价 × 流通股本），仅对候选集查询
- 多策略共振标注（同一代码被多个策略命中）
- 输出：JSON（结构稳定，供投递/归档）或 Markdown（日报正文）
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable

import baostock as bs
from dotenv import load_dotenv

load_dotenv()

from sequoia_x.core.config import get_settings
from sequoia_x.core.logger import get_logger
from sequoia_x.data.engine import DataEngine
from sequoia_x.strategy import STRATEGY_REGISTRY
from sequoia_x.strategy.base import BaseStrategy

logger = get_logger(__name__)

# 默认策略集：(策略类, 中文名)——源自中央注册表，避免与 main/backtest 漂移
DEFAULT_STRATEGIES: list[tuple[type[BaseStrategy], str]] = [
    (spec.cls, spec.cn_name) for spec in STRATEGY_REGISTRY
]


def _to_bs_code(code: str) -> str:
    prefix = "sh" if code.startswith(("6", "9")) else "sz"
    return f"{prefix}.{code}"


class StockMeta:
    """股票基础信息缓存：名称 / 上市日期（baostock query_stock_basic）。"""

    def __init__(self, min_ipo_days: int = 60) -> None:
        self.names: dict[str, str] = {}
        self.ipo_dates: dict[str, str] = {}
        self._min_ipo_date = (date.today() - timedelta(days=min_ipo_days)).strftime("%Y-%m-%d")

    def load(self) -> None:
        lg = bs.login()
        if lg.error_code != "0":
            logger.error(f"baostock 登录失败: {lg.error_msg}")
            return
        try:
            rs = bs.query_stock_basic(code_name="", code="")
            while rs.next():
                row = rs.get_row_data()  # [code, code_name, ipoDate, outDate, type, status]
                code = row[0].split(".")[1]
                self.names[code] = row[1]
                self.ipo_dates[code] = row[2]
        finally:
            bs.logout()
        logger.info(f"股票元数据加载完成: {len(self.names)} 只")

    def is_junk(self, code: str) -> bool:
        """ST/*ST/退市/次新股 → 过滤。"""
        name = self.names.get(code, "")
        if "ST" in name.upper() or "退" in name:
            return True
        ipo = self.ipo_dates.get(code, "9999-01-01")
        return ipo > self._min_ipo_date


class MarketCapLookup:
    """流通市值查询（baostock 换手率反推，仅对候选集）。"""

    def __init__(self, engine: DataEngine) -> None:
        self.engine = engine
        self._cache: dict[str, float] = {}
        # 不复权收盘价（与市值同一次 baostock 查询顺带缓存，零额外请求）。
        # ⚠️ 不能用库内 stock_daily.close 判「股价」——库里存的是后复权价。
        self._prices: dict[str, float] = {}

    def latest_trade_date(self) -> str:
        with sqlite3.connect(self.engine.db_path) as conn:
            row = conn.execute("SELECT MAX(date) FROM stock_daily").fetchone()
        return row[0] if row else ""

    def fetch(self, codes: Iterable[str], as_of: str | None = None) -> None:
        """查询候选集流通市值（亿元）。as_of 缺省用库内最新交易日。"""
        as_of = as_of or self.latest_trade_date()
        if not as_of:
            logger.warning("本地无行情数据，无法计算市值")
            return
        lg = bs.login()
        if lg.error_code != "0":
            logger.error(f"baostock 登录失败: {lg.error_msg}")
            return
        try:
            for code in codes:
                rs = bs.query_history_k_data_plus(
                    _to_bs_code(code), "close,volume,turn",
                    start_date=as_of, end_date=as_of,
                    frequency="d", adjustflag="3",  # 不复权
                )
                while rs.next():
                    row = rs.get_row_data()
                    try:
                        close, volume, turn = float(row[0]), float(row[1]), float(row[2])
                        self._prices[code] = close
                        if turn > 0:
                            self._cache[code] = volume / (turn / 100) * close / 1e8
                    except (ValueError, ZeroDivisionError):
                        continue
        finally:
            bs.logout()

    def get(self, code: str) -> float:
        return self._cache.get(code, 0.0)

    def get_price(self, code: str) -> float:
        """不复权收盘价（元）。当日停牌/取不到 → 0.0（会被股价下限过滤掉）。"""
        return self._prices.get(code, 0.0)


class ReportBuilder:
    """聚合多策略选股结果 → 过滤 → 市值排序 → 共振标注。"""

    def __init__(
        self,
        engine: DataEngine,
        strategies: list[tuple[type[BaseStrategy], str]] | None = None,
        min_cap_yi: float = 50.0,
        max_cap_yi: float = 800.0,
        min_ipo_days: int = 60,
        min_price: float = 0.0,
    ) -> None:
        self.engine = engine
        self.strategies = strategies or DEFAULT_STRATEGIES
        self.min_cap = min_cap_yi
        self.max_cap = max_cap_yi
        # 股价下限（元，不复权收盘价）。0 = 关闭（研究/回测口径不受影响）。
        self.min_price = min_price
        self.meta = StockMeta(min_ipo_days=min_ipo_days)
        self.caps = MarketCapLookup(engine)

    def build(self) -> dict:
        """返回结构化结果。

        {
          "date": "2026-09-02",
          "generated_at": "...",
          "strategies": [{"name", "count_total", "count_in_range", "top": [code...]}],
          "cross_hits": [{"code","name","cap_yi","strategies"}],   # 多策略共振
          "regime": {"regime": "up_low", "date": "..."}            # 市场状态（asof）
        }
        """
        self.meta.load()

        # 1. 收集所有策略原始结果
        raw: dict[str, set[str]] = {}   # code -> 命中策略中文名集合
        strategy_raw: dict[str, list[str]] = {}
        for cls, label in self.strategies:
            strat = cls(engine=self.engine, settings=get_settings())
            try:
                codes = strat.run()
            except Exception as exc:  # 单策略失败不拖垮整体
                logger.error(f"策略 {label} 执行失败: {exc}")
                codes = []
            strategy_raw[label] = codes
            for code in codes:
                if not self.meta.is_junk(code):
                    raw.setdefault(code, set()).add(label)

        # 2. 查候选市值（只在候选集内）
        self.caps.fetch(raw.keys())

        # 3. 汇总
        result_date = self.caps.latest_trade_date()

        strategies_out = []
        for cls, label in self.strategies:
            codes = [c for c in strategy_raw.get(label, [])
                     if not self.meta.is_junk(c)]
            in_range = [c for c in codes if self.min_cap <= self.caps.get(c) <= self.max_cap]
            in_range.sort(key=lambda c: self.caps.get(c), reverse=True)
            # 股价下限过滤（count_in_range 保持「仅市值区间」语义不动——wrapper 的
            # 备援重试防呆依赖它；过滤后计数单列 count_price_ok）
            price_ok = [c for c in in_range if self._price_ok(c)]
            strategies_out.append({
                "name": label,
                "count_total": len(codes),          # 滤 ST/新股后
                "count_in_range": len(in_range),    # 市值区间内
                "count_price_ok": len(price_ok),    # 市值区间内 + 股价达标
                "top": price_ok[:10],
            })

        # 共振票（>=2 策略命中，市值区间内）
        cross = []
        for code, labels in raw.items():
            if (len(labels) >= 2 and self.min_cap <= self.caps.get(code) <= self.max_cap
                    and self._price_ok(code)):
                cross.append({
                    "code": code,
                    "name": self.meta.names.get(code, code),
                    "cap_yi": round(self.caps.get(code), 1),
                    "close": round(self.caps.get_price(code), 2),
                    "strategies": sorted(labels),
                })
        cross.sort(key=lambda x: -x["cap_yi"])

        # 市场状态（数据日当日的 regime，供日报状态机建议）
        regime_info: dict = {}
        try:
            from sequoia_x.regime import get_market_states

            states = get_market_states(self.engine.db_path, refresh=True)
            if not states.empty:
                # states.date 为 Timestamp；转 date 与 result_date(str) 比对 asof
                st = states.copy()
                st["d"] = st["date"].dt.date
                # 数据日 result_date 是 str（YYYY-MM-DD），取 <= 它的最后一行
                target = date.fromisoformat(result_date)
                past = st[st["d"] <= target]
                if not past.empty:
                    last = past.iloc[-1]
                    regime_info = {
                        "regime": last["regime"],
                        "date": str(last["d"]),
                    }
        except Exception as exc:
            logger.warning(f"市场状态获取失败（跳过）: {exc}")

        return {
            "date": result_date,
            "generated_at": date.today().isoformat(),
            "min_price": self.min_price,
            "strategies": strategies_out,
            "cross_hits": cross,
            "regime": regime_info,
        }

    def _price_ok(self, code: str) -> bool:
        """股价下限判定。min_price<=0 视为关闭（不改变既有行为）。"""
        if self.min_price <= 0:
            return True
        return self.caps.get_price(code) >= self.min_price

    def to_markdown(self, result: dict) -> str:
        lines = [f"📈 Sequoia-X 选股日报 | 数据日 {result['date']}（收盘后）", ""]
        # 市场状态区块（今日 regime → 主攻/回避）
        if result.get("regime", {}).get("regime"):
            try:
                from sequoia_x.strategy_map import regime_markdown

                strat_names = [s["name"] for s in result["strategies"]]
                lines.append(
                    regime_markdown(result["regime"]["regime"], strat_names)
                )
                lines.append("")
            except Exception as exc:
                logger.warning(f"regime 建议生成失败（跳过）: {exc}")
        for s in result["strategies"]:
            if not s["count_in_range"]:
                lines.append(f"【{s['name']}】无（滤后 {s['count_total']} 只）")
                continue
            kept = s.get("count_price_ok", s["count_in_range"])
            extra = f"，剔除股价<{self.min_price:g}元 {s['count_in_range'] - kept} 只" if self.min_price > 0 else ""
            lines.append(f"【{s['name']}】区间内 {kept} 只（市值区间 {s['count_in_range']}{extra}；TOP{min(10, len(s['top']))}）")
            for i, code in enumerate(s["top"], 1):
                lines.append(f"  {i:2d}. {code} {self.meta.names.get(code, '?'):6s} 市值{self.caps.get(code):.0f}亿 股价{self.caps.get_price(code):.2f}元")
            lines.append("")
        if result["cross_hits"]:
            lines.append("⭐ 多策略共振：")
            for c in result["cross_hits"]:
                px = f" 股价{c['close']:.2f}元" if c.get("close") is not None else ""
                lines.append(f"  {c['code']} {c['name']} 市值{c['cap_yi']:.0f}亿{px}（{' + '.join(c['strategies'])}）")
            # 入场跳空提醒（2026-09-18 验证加入）。跳空只有次日开盘才知道，日报在
            # 收盘后生成、无法预过滤，故只作可执行提示：次日开盘若高开 ≥5%（相对
            # 前收）直接放弃。依据 1Y/2Y 事件研究，该组均为负期望（1Y −3.58%/n=56、
            # 2Y −1.79%/n=212，胜率 38~39%），但只占信号 4.9%，全样本仅改善
            # 0.05~0.38pp，故**不做硬过滤**（详见 SKILL.md 第 6 节）。
            lines.append("  ⚠️ 次日开盘若高开 ≥5%，历史该组负期望（1Y −3.6%/2Y −1.8%），建议放弃该标的")
        # 回测参考（若有）
        if result.get("backtest"):
            bt = result["backtest"]
            lines.append("")
            lines.append(f"📊 回测参考（{bt['range']}，次日买入→N日卖出，已含双边成本 {bt.get('cost_bps', 25)}bp）")
            for name, hs in bt["strategies"].items():
                if not any(v.get("count") for v in hs.values()):
                    continue
                cells = []
                for h in (5, 10, 20):
                    s = hs.get(str(h)) or hs.get(h)
                    if s and s.get("count"):
                        cells.append(f"{h}日胜率{s['win_rate']}%")
                    else:
                        cells.append(f"{h}日—")
                lines.append(f"  {name}: {' | '.join(cells)}")
            lines.append("  ⚠️ 历史胜率≠未来收益，仅供策略参考")
        return "\n".join(lines).rstrip() + "\n"


def run_report(
    json_out: str | None = None,
    markdown: bool = False,
    cap_range: tuple[float, float] = (50.0, 800.0),
    backtest_json: str | None = "data/backtest_1y.json",
    min_price: float = 0.0,
) -> dict:
    """执行报告构建并按要求输出。返回结构化结果 dict。

    backtest_json: 回测缓存 JSON 路径（None 则不含回测区块）。
    回测较慢（~30s），建议 cron 单独更新缓存，日报直接加载。
    """
    settings = get_settings()
    engine = DataEngine(settings)
    builder = ReportBuilder(engine, min_cap_yi=cap_range[0], max_cap_yi=cap_range[1],
                            min_price=min_price)
    result = builder.build()

    if backtest_json:
        path = Path(backtest_json)
        if path.exists():
            try:
                result["backtest"] = json.loads(path.read_text(encoding="utf-8"))
                logger.info(f"回测参考已加载: {path}")
            except Exception as exc:
                logger.warning(f"回测 JSON 解析失败: {exc}")
        else:
            logger.warning(f"回测缓存不存在（跳过）: {path}")

    if json_out:
        path = Path(json_out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info(f"JSON 结果已写入: {path}")
    if markdown:
        print(builder.to_markdown(result))
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Sequoia-X 选股报告")
    parser.add_argument("--json-out", help="将结构化结果写入 JSON 文件")
    parser.add_argument("--markdown", action="store_true", help="打印 Markdown 日报")
    parser.add_argument("--min-cap", type=float, default=50.0, help="市值下限（亿元）")
    parser.add_argument("--max-cap", type=float, default=800.0, help="市值上限（亿元）")
    parser.add_argument("--min-price", type=float, default=0.0,
                        help="股价下限（元，不复权收盘价；0=不过滤）")
    args = parser.parse_args()

    if not args.json_out and not args.markdown:
        parser.error("至少指定 --json-out 或 --markdown 之一")

    run_report(args.json_out, args.markdown, (args.min_cap, args.max_cap),
               min_price=args.min_price)


if __name__ == "__main__":
    sys.exit(main())
