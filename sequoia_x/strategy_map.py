"""Regime → 策略状态机：市场风格到策略启用的映射与建议。

源自 2y 分状态事件研究（regime.py + backtest 分状态统计，2026-09-04）：
不同市场状态下各策略收益差异极大——不存在全天候策略，应"看菜下饭"。

研究数据（net 25bp，20日持有）：
  down_high（恐慌/超跌）: 上升跌停 +9.58% / 高窄旗形 +8.93% / 涨停洗盘 +6.38%（超跌反抽最强）
  up_low（低波慢牛）   : 海龟 +2.63% / 均线放量 +2.49% / RPS +2.82%（趋势动量主场）
  up_high（情绪顶）    : 追高类全军覆没（上升跌停 -4.30% / 涨停洗盘 -3.50% / 海龟 -1.85%）
  down_low（阴跌）     : 样本少，观望为主

映射原则（保守）：
- 每状态标 1 个"主攻策略"（历史最强）+ 若干"关注/回避"
- 回避名单用于日报提示"当前状态该策略历史表现差，谨慎"
"""
from __future__ import annotations

import json
from pathlib import Path

from sequoia_x.core.logger import get_logger

logger = get_logger(__name__)

# 状态 → 策略建议（基于 2y 分状态净收益研究）
# 结构: {regime: {"focus": [策略名...], "avoid": [策略名...], "note": str}}
REGIME_ADVICE: dict[str, dict] = {
    "down_high": {
        "focus": ["上升跌停", "高窄旗形", "涨停洗盘"],
        "avoid": ["海龟突破"],
        "note": "恐慌下跌+高波动：超跌反抽类最强（错杀修复），追突破易被套",
    },
    "down_low": {
        "focus": ["高窄旗形"],
        "avoid": ["海龟突破", "RPS 突破"],
        "note": "阴跌低波动：样本少整体谨慎，仅高窄旗形低吸可关注",
    },
    "up_low": {
        "focus": ["海龟突破", "均线放量", "RPS 突破"],
        "avoid": ["上升跌停", "涨停洗盘"],
        "note": "低波慢牛：趋势/动量主场，顺势做多",
    },
    "up_high": {
        "focus": ["高窄旗形"],
        "avoid": ["上升跌停", "涨停洗盘", "海龟突破"],
        "note": "高波上涨（情绪顶）：追高类历史全灭，防御为主，等回调低吸",
    },
}

# 研究表数值快照（供日报"为什么"说明；来源 regime 分状态 2y 回测）
_REGIME_EVIDENCE = {
    "down_high": {"上升跌停": "+9.58%", "高窄旗形": "+8.93%", "涨停洗盘": "+6.38%", "海龟突破": "+0.37%"},
    "up_low": {"海龟突破": "+2.63%", "均线放量": "+2.49%", "RPS 突破": "+2.82%", "上升跌停": "-1.84%"},
    "up_high": {"上升跌停": "-4.30%", "涨停洗盘": "-3.50%", "海龟突破": "-1.85%", "高窄旗形": "+0.47%"},
    "down_low": {},
}


def get_advice(regime: str) -> dict:
    """返回该状态的策略建议（未知状态返回空建议）。"""
    return REGIME_ADVICE.get(regime, {"focus": [], "avoid": [], "note": ""})


def evidence_for(regime: str, strategy: str) -> str:
    """某状态下某策略的 20 日净收益研究值（展示用），未知返回空。"""
    return _REGIME_EVIDENCE.get(regime, {}).get(strategy, "")


def regime_markdown(regime: str, strategy_names: list[str]) -> str:
    """生成日报的'市场状态'区块（今日 regime → 主攻/回避/提示）。"""
    adv = get_advice(regime)
    if not adv["note"]:
        return f"🌡️ 市场状态：{regime}（无研究建议）"
    lines = [f"🌡️ 市场状态 {regime}：{adv['note']}"]
    focus = [f"**{s}**" for s in adv["focus"]]
    if focus:
        lines.append(f"  🎯 主攻策略：{' '.join(focus)}")
    if adv["avoid"]:
        ev = "，".join(
            f"{s}(历史{evidence_for(regime, s)})" for s in adv["avoid"] if s in strategy_names
        )
        if ev:
            lines.append(f"  ⚠️ 今日回避（状态不匹配）：{ev}")
    return "\n".join(lines)


def apply_regime_filter(
    events: dict[str, pd.DataFrame],
    states: pd.DataFrame,
    strategies: list[str] | None = None,
    keep_avoid: bool = False,
) -> tuple[dict[str, pd.DataFrame], dict[str, int]]:
    """按状态机过滤事件：信号日处于该策略 avoid 状态的信号剔除。

    Args:
        events: {策略名: 事件表}
        states: regime 状态表（regime.get_market_states 输出）
        strategies: 只处理这些策略（None=全部）
        keep_avoid: True 时保留 avoid 信号（用于对比研究，不剔除）
    Returns:
        (过滤后事件, {策略名: 剔除数})
    """
    import pandas as pd

    from sequoia_x.regime import label_events

    filtered: dict[str, pd.DataFrame] = {}
    dropped: dict[str, int] = {}
    for name, ev in events.items():
        if strategies and name not in strategies:
            filtered[name] = ev
            dropped[name] = 0
            continue
        if ev.empty:
            filtered[name] = ev
            dropped[name] = 0
            continue
        le = label_events(states, ev)
        # 该策略在所有出现状态中是否被列进 avoid
        avoid_all = set()
        for r in le["market_state"].unique():
            avoid_all |= set(REGIME_ADVICE.get(r, {}).get("avoid", []))
        if name in avoid_all:
            if keep_avoid:
                filtered[name] = le
                dropped[name] = 0
            else:
                keep_mask = ~le["market_state"].map(
                    lambda r: name in REGIME_ADVICE.get(r, {}).get("avoid", [])
                )
                dropped[name] = int((~keep_mask).sum())
                filtered[name] = le.loc[keep_mask].drop(columns=["market_state"])
                logger.info(f"{name}: regime 过滤剔除 {dropped[name]} 信号（状态不匹配）")
        else:
            filtered[name] = ev
            dropped[name] = 0
    return filtered, dropped


if __name__ == "__main__":
    # CLI 手动查看某状态建议（调试用）
    import argparse
    import sys

    parser = argparse.ArgumentParser()
    parser.add_argument("--regime", required=True, help="up_low / up_high / down_low / down_high")
    args = parser.parse_args()
    print(json.dumps(get_advice(args.regime), ensure_ascii=False, indent=2))
    sys.exit(0)
