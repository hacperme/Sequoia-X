"""上升趋势跌停策略：趋势中放量跌停，捕捉错杀机会。"""

import pandas as pd

from sequoia_x.core.logger import get_logger
from sequoia_x.market_rules import is_limit_down
from sequoia_x.strategy.base import BaseStrategy

logger = get_logger(__name__)


class UptrendLimitDownStrategy(BaseStrategy):
    """上升趋势跌停策略。

    选股条件（向量化，严禁 iterrows）：
    1. 处于上升趋势：昨日20日均线 > 昨日60日均线
    2. 放量跌停：今日 close <= 昨日 close * 0.905
                且今日 volume > 20日均量的 2.0 倍

    Attributes:
        webhook_key: 路由到 'limit_down' 专属飞书机器人。
    """

    webhook_key: str = "limit_down"
    _MIN_BARS: int = 60  # 至少需要 60 根 K 线（60日均线）

    def run(self) -> list[str]:
        """
        遍历全市场，返回满足上升趋势跌停条件的股票代码列表。

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

                # 向量化计算均线
                df["ma20"] = df["close"].rolling(20).mean()
                df["ma60"] = df["close"].rolling(60).mean()
                df["vol_ma20"] = df["volume"].rolling(20).mean()

                prev = df.iloc[-2]  # 昨日
                today = df.iloc[-1]  # 今日

                if pd.isna(prev["ma20"]) or pd.isna(prev["ma60"]) or pd.isna(today["vol_ma20"]):
                    continue

                # 条件 1：上升趋势（昨日均线多头排列）
                uptrend = prev["ma20"] > prev["ma60"]
                # 条件 2：放量跌停（按板块幅度判定：主板10%/双创20%/北交所30%）
                limit_down = is_limit_down(prev["close"], today["close"], symbol)
                volume_surge = today["volume"] > today["vol_ma20"] * 2.0

                if uptrend and limit_down and volume_surge:
                    selected.append(symbol)

            except Exception as exc:
                logger.warning(f"[{symbol}] UptrendLimitDownStrategy 计算失败：{exc}")
                continue

        logger.info(f"UptrendLimitDownStrategy 选出 {len(selected)} 只股票")
        return selected

    def signal_mask(self, panel: pd.DataFrame) -> pd.Series:
        """权威向量化信号（与 run() 同口径）：昨多头(MA20>MA60) ∧ 跌停(分板) ∧ 放量2倍。

        panel 需含 lim_dn 列（跌停倍率）。MA 用 shift(1) 前值防未来。
        """
        g = panel.groupby("symbol", sort=False)
        ma20 = g["close"].transform(lambda s: s.rolling(20).mean())
        ma60 = g["close"].transform(lambda s: s.rolling(60).mean())
        vol_ma20 = g["volume"].transform(lambda s: s.rolling(20).mean())
        close_prev = g["close"].transform(lambda s: s.shift(1))
        return (
            (ma20.shift(1) > ma60.shift(1))                # 昨多头排列（run 用 prev）
            & (panel["close"] <= close_prev * panel["lim_dn"])  # 今跌停（分板）
            & (panel["volume"] > vol_ma20 * 2.0)           # 放量 2 倍
        ).fillna(False)
