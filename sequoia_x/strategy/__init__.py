"""策略模块：选股策略基类与具体策略实现。

中央策略注册表（STRATEGY_REGISTRY）——新增策略只需在此追加一条 StrategySpec，
main.py（实盘）/ report.py（日报）/ backtest.py（回测）三处自动同步，
避免此前"三处各维护一份策略清单"的漏改问题。
"""

from dataclasses import dataclass

from sequoia_x.strategy.base import BaseStrategy
from sequoia_x.strategy.high_tight_flag import HighTightFlagStrategy
from sequoia_x.strategy.limit_up_shakeout import LimitUpShakeoutStrategy
from sequoia_x.strategy.ma_volume import MaVolumeStrategy
from sequoia_x.strategy.private_placement import PrivatePlacementStrategy
from sequoia_x.strategy.rps_breakout import RpsBreakoutStrategy
from sequoia_x.strategy.turtle_trade import TurtleTradeStrategy
from sequoia_x.strategy.uptrend_limit_down import UptrendLimitDownStrategy


DEFAULT_HOLD_DAYS = 20
"""策略未定档时的回退持有期（交易日）。各策略的实际值见 StrategySpec.hold_days。"""


@dataclass(frozen=True)
class StrategySpec:
    """单个策略的注册信息。

    Attributes:
        cls: 策略类（BaseStrategy 子类）。
        cn_name: 中文名（日报/回测的统一标识键，须与 strategy_map 映射一致）。
        backtest: 是否参与向量化事件回测（事件型策略如定增无 signal_mask，置 False）。
        hold_days: 组合层默认持有期（交易日，**单一来源**，供 backtest / portfolio /
            tracker 三处引用）。2026-09-21 以 5/10/20/30/40 日 × 1Y/2Y 全扫描定标
            （chandelier/max_pos=100/quality+regime+budget/net 25bp）：
            海龟 20（两期同向最优）、RPS 30（逐笔均收益两期一致升到 30 日）、
            涨停洗盘 20、上升跌停 40（样本弱，仅 62~89 笔）、均线/高窄 20（不实盘，
            仅作回测基准）。**改动前须有两期同向证据**，勿按单期数据调。
    """

    cls: type[BaseStrategy]
    cn_name: str
    backtest: bool = True
    hold_days: int = DEFAULT_HOLD_DAYS
    trail_off_regimes: tuple[str, ...] = ()
    """该策略在哪些市场状态下**关闭移动止损**（空 = 不关闭，保守默认）。

    单一来源（按策略声明）：组合层与 tracker 据此决定「该策略来源的持仓」是否在
    这些状态下停用吊灯。⚠️ 只能声明 `strategy_map.TRAIL_OFF_REGIMES`（有证据的状态
    白名单）里的状态。2026-09-22 复核：R2 只在 **RPS 突破**上三窗口从不变差
    （1y 持平 / 2y +5.6pp / 全量 +5.5pp）；海龟（2y −1.7pp）与多策略联合
    （1y −2.8pp）两期不同向 → 只有 RPS 声明。**新增声明前必须按单元（含 --combined）
    跑两期 A/B**。
    """


# 顺序即实盘/日报的执行与展示顺序；新增策略在此追加即可
STRATEGY_REGISTRY: list[StrategySpec] = [
    StrategySpec(TurtleTradeStrategy, "海龟突破", hold_days=20),
    StrategySpec(MaVolumeStrategy, "均线放量", hold_days=20),
    StrategySpec(HighTightFlagStrategy, "高窄旗形", hold_days=20),
    StrategySpec(LimitUpShakeoutStrategy, "涨停洗盘", hold_days=20),
    StrategySpec(UptrendLimitDownStrategy, "上升跌停", hold_days=40),
    StrategySpec(RpsBreakoutStrategy, "RPS 突破", hold_days=30,
                 trail_off_regimes=("up_low",)),
    StrategySpec(PrivatePlacementStrategy, "定增公告", backtest=False, hold_days=20),
]


def hold_days_for(cn_name: str, default: int = DEFAULT_HOLD_DAYS) -> int:
    """策略中文名 → 组合层持有期（交易日）；未注册策略回退 default。"""
    for spec in STRATEGY_REGISTRY:
        if spec.cn_name == cn_name:
            return spec.hold_days
    return default


def hold_days_map() -> dict[str, int]:
    """{策略中文名: 持有期(交易日)}，供 tracker 按信号来源定档。"""
    return {spec.cn_name: spec.hold_days for spec in STRATEGY_REGISTRY}


def trail_off_regimes_for(cn_name: str) -> tuple[str, ...]:
    """策略中文名 → 该策略关闭移动止损的市场状态（未注册/未声明 → ()，保守全启用）。"""
    for spec in STRATEGY_REGISTRY:
        if spec.cn_name == cn_name:
            return tuple(spec.trail_off_regimes)
    return ()


def trail_off_regimes_map() -> dict[str, tuple[str, ...]]:
    """{策略中文名: 关闭移动止损的状态}（只含非空项），供 tracker 按信号来源定档。"""
    return {spec.cn_name: tuple(spec.trail_off_regimes)
            for spec in STRATEGY_REGISTRY if spec.trail_off_regimes}


def trail_off_regimes_all(cn_names: "list[str] | tuple[str, ...]") -> tuple[str, ...]:
    """多策略联合/同股多策略命中 → 关闭移动止损的状态 = 各参与策略声明的**交集**（保守）。

    联合池的持仓不记来源策略（合并事件里没有 strategy 标签），无法按策略区分，
    故取最保守口径：只有「所有参与策略都声明关闭」的状态才会在池内关闭。
    例：RPS=("up_low",) 与海龟=() 同池 → 交集 () → 联合池不关闭（= 旧口径）。
    """
    sets = [set(trail_off_regimes_for(n)) for n in cn_names]
    if not sets:
        return ()
    out = set.intersection(*sets)
    return tuple(s for s in ("up_low", "up_high", "down_high", "down_low") if s in out)

