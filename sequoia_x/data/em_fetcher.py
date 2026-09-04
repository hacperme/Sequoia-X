"""东方财富直连 fetcher：baostock 备援数据源【⚠️ 限流严重，仅手动低频用】。

与 akshare stock_zh_a_hist 同源 API（push2his.eastmoney.com kline/get），但**去掉
被东财风控断连的 ut token**（实测：带 ut=7eea3ed... 的请求 RemoteDisconnected，
去掉后 HTTP 200 正常返回）。

⚠️ 2026-09-04 实测限流（重要）：
- 本环境 IP 高频请求会触发东财动态风控：约 4-5 次/分钟请求后开始 RemoteDisconnected，
  且 ban 持续 >4 分钟未恢复（curl/requests 均 HTTP 000）。
- 结论：**不可用于批量/每日自动补数据**；仅适合偶尔手动补单只（间隔 ≥60s）。
- 本项目自动备源已放弃东财（改走 baostock 自身重试增强）；此模块保留作手动工具。

输出与 baostock 对齐（供 engine 同 schema 落库）：
- 后复权价（fqt=2）——⚠️ 注意复权基准与 baostock 不同（茅台 2026-09-03：
  东财 hfq≈8132 vs baostock≈9961），**不可与 baostock 数据混入同一库**
- volume 单位=股（东财返回"手"，×100 转换）
- turnover 列 = 成交额（元）——库内 turnover 列实际语义=baostock amount，
  成交额本身各源一致（实测茅台 09-03=23.05 亿）
- 列序: [symbol, date, open, high, low, close, volume, turnover]
"""
from __future__ import annotations

import time

import pandas as pd
import requests

_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://quote.eastmoney.com/",
}
# f51日期 f52开 f53收 f54高 f55低 f56量(手) f57额(元) f58振幅 f59涨跌幅 f60涨跌额 f61换手率%
_FIELDS2 = "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"


def _secid(symbol: str) -> str:
    """东财 secid：6/9 开头沪市=1，其余(0/3)深市=0。北交所 baostock 无，不处理。"""
    return f"{'1' if symbol.startswith(('6', '9')) else '0'}.{symbol}"


def fetch_hist(
    symbol: str,
    start_date: str,
    end_date: str,
    adjust: str = "hfq",
    timeout: float = 15,
    retries: int = 3,
    pause: float = 1.5,
) -> pd.DataFrame:
    """拉单只股票日线。成功返回 df[symbol,date,open,high,low,close,volume,turnover]；
    无数据/最终失败返回空 DataFrame（不抛异常，调用方按缺失处理）。"""
    fqt = {"hfq": "2", "qfq": "1", "": "0"}[adjust]
    params = {
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": _FIELDS2,
        "klt": "101",
        "fqt": fqt,
        "secid": _secid(symbol),
        "beg": start_date,
        "end": end_date,
    }
    for attempt in range(retries):
        try:
            r = requests.get(_URL, params=params, headers=_HEADERS, timeout=timeout)
            r.raise_for_status()
            payload = r.json() or {}
            data = payload.get("data") or {}
            klines = data.get("klines") or []
            if not klines:
                return pd.DataFrame()
            rows = [k.split(",") for k in klines]
            df = pd.DataFrame(
                rows,
                columns=["date", "open", "close", "high", "low",
                         "volume", "amount", "amplitude", "pct_chg", "chg", "turn"],
            )
            df = df[["date", "open", "close", "high", "low", "volume", "amount"]]
            df = df.rename(columns={"amount": "turnover"})  # 兼容库内列语义=成交额
            for col in ["open", "close", "high", "low", "volume", "turnover"]:
                df[col] = pd.to_numeric(df[col], errors="coerce")
            df["volume"] = df["volume"] * 100  # 手 → 股（与 baostock 对齐）
            df["symbol"] = symbol
            df = df.dropna(subset=["close"])
            df = df[df["volume"] > 0]
            df = df[["symbol", "date", "open", "high", "low", "close", "volume", "turnover"]]
            df["date"] = df["date"].str.replace("-", "")
            return df.reset_index(drop=True)
        except requests.RequestException as exc:
            if attempt < retries - 1:
                time.sleep(pause * (attempt + 1))
            else:
                import logging

                logging.getLogger(__name__).warning(
                    f"[东财 {symbol}] {retries} 次请求失败: {exc}"
                )
    return pd.DataFrame()
