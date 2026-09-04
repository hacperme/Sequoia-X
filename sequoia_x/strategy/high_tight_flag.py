"""高旗形整理策略：强动量后极度收敛缩量。"""

import pandas as pd

from sequoia_x.core.logger import get_logger
from sequoia_x.strategy.base import BaseStrategy

logger = get_logger(__name__)


class HighTightFlagStrategy(BaseStrategy):
    """高旗形整理策略。

    选股条件（向量化，严禁 iterrows）：
    1. 强动量：过去40天区间最高价 / 区间最低价 > 1.6（涨幅超60%）
    2. 极度收敛：最近10天区间最高价 / 区间最低价 < 1.15（振幅低于15%）
    3. 缩量：今日 volume < 过去20日 volume 均值的 0.6 倍

    Attributes:
        webhook_key: 路由到 'flag' 专属飞书机器人。
    """

    webhook_key: str = "flag"
    _MIN_BARS: int = 40  # 至少需要 40 根 K 线

    def run(self) -> list[str]:
        """
        遍历全市场，返回满足高旗形整理条件的股票代码列表。

        Returns:
            满足条件的股票代码列表。
        """
        symbols = self.engine.get_local_symbols()
        selected: list[str] = []

        for symbol in symbols:
            try:
                df = self.engine.get_ohlcv(symbol)
                if len(df) < self._MIN_BARS or not self._df_is_current(df):
                    continue

                # 向量化计算各窗口指标
                tail40 = df.tail(40)
                tail10 = df.tail(10)

                high40 = tail40["high"].max()
                low40 = tail40["low"].min()
                high10 = tail10["high"].max()
                low10 = tail10["low"].min()

                if low40 == 0 or low10 == 0:
                    continue

                # 条件 1：强动量
                momentum = high40 / low40 > 1.6
                # 条件 2：极度收敛
                consolidation = high10 / low10 < 1.15
                # 条件 3：高位抗跌（近10天最低点不得低于40天最高点的80%）
                high_level = low10 >= high40 * 0.8
                # 条件 4：缩量（向量化均值）
                vol_ma20 = df["volume"].iloc[-21:-1].mean()
                shrink = df["volume"].iloc[-1] < vol_ma20 * 0.6

                if momentum and consolidation and high_level and shrink:
                    selected.append(symbol)

            except Exception as exc:
                logger.warning(f"[{symbol}] HighTightFlagStrategy 计算失败：{exc}")
                continue

        logger.info(f"HighTightFlagStrategy 选出 {len(selected)} 只股票")
        return selected

    def signal_mask(self, panel: pd.DataFrame) -> pd.Series:
        """权威向量化信号（与 run() 同口径，含**高位抗跌**条件）。

        条件：强动量 hi40/lo40>1.6 ∧ 极度收敛 hi10/lo10<1.15 ∧
        高位抗跌 low10 ≥ hi40×0.8 ∧ 缩量 volume < 20日均量×0.6。

        ⚠️ 修复历史漂移：compute_events 旧实现缺"高位抗跌"→ 回测信号多于实盘。
        """
        g = panel.groupby("symbol", sort=False)
        hi40 = g["high"].transform(lambda s: s.rolling(40).max())
        lo40 = g["low"].transform(lambda s: s.rolling(40).min())
        hi10 = g["high"].transform(lambda s: s.rolling(10).max())
        lo10 = g["low"].transform(lambda s: s.rolling(10).min())
        # 缩量基准：前一日为止的 20 日均量（对齐 run() 的 iloc[-21:-1]，不含当日）
        vol_ma20_excl = g["volume"].transform(
            lambda s: s.shift(1).rolling(20).mean()
        )
        return (
            (hi40 / lo40 > 1.6)
            & (hi10 / lo10 < 1.15)
            & (lo10 >= hi40 * 0.8)  # 高位抗跌（run() 有、旧回测缺）
            & (panel["volume"] < vol_ma20_excl * 0.6)
        ).fillna(False)
