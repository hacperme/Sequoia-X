"""策略基类模块：定义所有选股策略的抽象接口。"""

from abc import ABC, abstractmethod

import pandas as pd

from sequoia_x.core.config import Settings
from sequoia_x.data.engine import DataEngine


class BaseStrategy(ABC):
    """选股策略抽象基类。

    所有具体策略必须继承此类并实现 run() 方法。

    Attributes:
        webhook_key: 策略对应的飞书 webhook 标识，用于路由到不同机器人。
            默认为 'default'，将使用 Settings.feishu_webhook_url。
            子类可覆盖此属性以路由到专属机器人，例如 'ma_volume'。
    """

    webhook_key: str = "default"

    def __init__(self, engine: DataEngine, settings: Settings) -> None:
        """
        初始化策略。

        Args:
            engine: DataEngine 实例，用于读取行情数据。
            settings: Settings 实例，用于读取配置。
        """
        self.engine = engine
        self.settings = settings
        self._market_last_date: str | None = None

    @property
    def market_last_date(self) -> str:
        """全市场最新交易日（库内 MAX(date)，惰性缓存）。"""
        if self._market_last_date is None:
            import sqlite3

            with sqlite3.connect(self.engine.db_path) as conn:
                row = conn.execute("SELECT MAX(date) FROM stock_daily").fetchone()
            self._market_last_date = row[0] if row else ""
        return self._market_last_date

    def _df_is_current(self, df: pd.DataFrame) -> bool:
        """该股行情是否覆盖到市场最新交易日（停牌/数据缺失股应跳过，
        避免把停牌前旧 K 线当最新信号误选——2026-09-04 P0 修复）。"""
        if df is None or df.empty:
            return False
        return str(df["date"].iloc[-1]) == self.market_last_date

    @abstractmethod
    def run(self) -> list[str]:
        """
        执行选股逻辑，返回选中的股票代码列表。

        Returns:
            满足策略条件的股票代码列表，如 ['000001', '600519']。
            无选股结果时返回空列表。
        """
        ...

    # ── 信号单一来源（P0 重构，2026-09-04）──
    # 背景：回测引擎 compute_events 曾独立复制各策略信号条件 → 双实现漂移
    # （已现 RPS shift(1) 不一致、高窄旗形缺"高位抗跌"条件）。
    # 约定：signal_mask 为信号的权威向量化实现（回测 compute_events 调用）；
    #       run() 为实盘逐股适配（经一致性测试与 signal_mask 锁定同口径）。

    def signal_mask(self, panel: pd.DataFrame) -> pd.Series:
        """全表向量化信号掩码（True = 该 (symbol, date) 满足信号，无未来函数）。

        Args:
            panel: 全市场日线 DataFrame，需含列 open/high/low/close/volume/turnover
                   （turnover 列语义=成交额元）、prev_close、lim_up、lim_dn
                   （后三者由 backtest._load_panel / regime 数据准备提供）。
        Returns:
            pd.Series[bool]，index 与 panel 对齐。
        """
        raise NotImplementedError(f"{type(self).__name__} 未实现 signal_mask")
