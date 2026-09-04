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


@dataclass(frozen=True)
class StrategySpec:
    """单个策略的注册信息。

    Attributes:
        cls: 策略类（BaseStrategy 子类）。
        cn_name: 中文名（日报/回测的统一标识键，须与 strategy_map 映射一致）。
        backtest: 是否参与向量化事件回测（事件型策略如定增无 signal_mask，置 False）。
    """

    cls: type[BaseStrategy]
    cn_name: str
    backtest: bool = True


# 顺序即实盘/日报的执行与展示顺序；新增策略在此追加即可
STRATEGY_REGISTRY: list[StrategySpec] = [
    StrategySpec(TurtleTradeStrategy, "海龟突破"),
    StrategySpec(MaVolumeStrategy, "均线放量"),
    StrategySpec(HighTightFlagStrategy, "高窄旗形"),
    StrategySpec(LimitUpShakeoutStrategy, "涨停洗盘"),
    StrategySpec(UptrendLimitDownStrategy, "上升跌停"),
    StrategySpec(RpsBreakoutStrategy, "RPS 突破"),
    StrategySpec(PrivatePlacementStrategy, "定增公告", backtest=False),
]
