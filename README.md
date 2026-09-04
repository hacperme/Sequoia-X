# Sequoia-X: 王者回归 | The King Returns

> A 股量化选股与策略研究系统 V2 | A-Share Quantitative Stock Selection & Strategy Research System V2

---

## 简介 | Introduction

Sequoia-X V2 是面向 A 股市场的**量化选股与策略研究系统**（研究定位，非实盘下单）。每日收盘后自动完成：
数据同步 → 六策略选股 → 市值过滤 → 共振识别 → 市场状态判定 → 日报推送（飞书）；
并配套**事件研究回测引擎**与**组合模拟器**，支持 Regime 状态机过滤与风险预算的滚动验证。

数据层使用 [baostock](http://baostock.com)（免费、无需注册）拉取历史及增量日 K 数据（后复权），
存储于本地 SQLite。

> ⚠️ 2026-09-04 重要结论：baostock 对**并发登录/查询触发风控**（偶发"黑名单用户"），
> 全量同步必须**单进程串行**（实测 ~0.19s/只 × 5215 只 ≈ 16 分钟）。此前 8 进程并发的
> 偶发失败即源于此，非网络抖动。数据更新时间：日 K 17:30 / 复权因子 18:00 入库。

---

## 架构总览 | Architecture

```
                    ┌─────────────────────────────────────────────┐
                    │              cron（工作日北京 20:00）          │
                    │          sequoia_report.sh wrapper           │
                    └──────────────────┬──────────────────────────┘
                                       │
        ┌──────────────────────────────▼──────────────────────────────┐
        │ 1. sync（单进程串行，~16min，可超时容错）                       │
        │ 2. 交易日检测（数据日 ≥ 最近已收盘交易日）                      │
        │ 3. 回测缓存刷新（滚动 1y，失败沿用旧缓存）                      │
        │ 4. report：六策略 signal_mask → 市值过滤 → 共振 → 🌡️regime   │
        │ 5. tracker：日报名单注册 → T+1 开盘 akquant 撮合 → 逐日跟踪   │
        └────────────────────────────────────────────────────────────┘
                                       │
        ┌──────────────────────────────▼──────────────────────────────┐
        │ 研究工具链（CLI）                                              │
        │ backtest  ── 向量化事件研究（信号→未来 5/10/20 日收益）        │
        │ portfolio ── 组合模拟（NextOpen 撮合 / time·stop·chandelier）│
        │ regime    ── 沪深300×MA200 × 波动率 → 四态市场状态机          │
        │ tracker   ── 前瞻跟踪（akquant 独立引擎交叉验证）              │
        └─────────────────────────────────────────────────────────────┘
```

---

## 内置策略 | Strategies

| 策略 | 说明 |
|---|---|
| **TurtleTrade** | 海龟突破：20日新高 + 成交额过亿 + 阳线防诱多，按涨幅排序 |
| **MaVolume** | 均线+放量突破 |
| **HighTightFlag** | 高而窄的旗形整理突破（⚠️ 回测证伪，已从 regime 主攻降级） |
| **LimitUpShakeout** | 涨停洗盘回踩确认 |
| **UptrendLimitDown** | 上升趋势中的跌停反包 |
| **RpsBreakout** | 欧奈尔 RPS 相对强度突破 |
| **PrivatePlacement** | 定增公告（共振参考，不单独选股） |

> 全部策略以 `signal_mask(panel)` 向量化实现为**单一信号来源**，实盘 `run()` 与
> 回测 `compute_events` 共用同一实现，杜绝信号口径漂移（2026-09-04 P0 重构）。

---

## 快速开始 | Quick Start

### 环境要求

- Python >= 3.10（推荐 uv 管理）
- A 股日 K 数据量约 430MB（330 万行 + 退市股 2.5 万行 + 指数日线）

### 1. 安装依赖

```bash
uv sync                       # 或 pip install .
uv pip install akquant        # tracker 交叉验证引擎
```

### 2. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env（飞书 Webhook 等）
```

### 3. 首次回填历史数据

```bash
.venv/bin/python main.py --backfill
```

全市场 ~5200 只现存 + 101 只退市股（2024 后仍在交易）历史后复权日 K 灌入，
约 2 小时（单线程，0 失败——**勿并行**，会触发 baostock 风控）。

### 4. 日常运行（增量同步 + 日报）

```bash
.venv/bin/python main.py          # 单进程增量同步（~16min，交易日 17:30 后才有当日数据）
```

### 5. 日报 wrapper（cron 推荐）

```bash
bash /opt/data/scripts/sequoia_report.sh
# 环境变量：
#   SEQUOIA_FORCE=1      跳过交易日检测（跨日强制用最近数据）
#   SEQUOIA_SKIP_SYNC=1  跳过增量同步（验证/应急）
```

工作日北京 20:00 由 cron 触发（数据 17:30/18:00 入库后留足缓冲）。

---

## 研究工具链 | Research Toolchain

### 事件研究回测（backtest）

```bash
.venv/bin/python -m sequoia_x.backtest --period 1y --json-out data/backtest_1y.json
.venv/bin/python -m sequoia_x.backtest --period 2y --grid --cost-bps 25
```

- **向量化事件研究**：全表预计算特征 → 信号事件表（20 交易日冷却去重）→ 未来 5/10/20 日真实收益
- **口径 v2（2026-09-04 评审固化）**：分板涨跌停（`market_rules.py` 单一来源）、真实成交额、
  退市股入回测（修正幸存者偏差）、25bp 成本默认、Wilson 95% CI、MAE/止损统计
- 1y 全市场 ~40s / 参数网格 ~108s

### 组合模拟（portfolio）

```bash
.venv/bin/python -m sequoia_x.portfolio --strategy 高窄旗形 --exit chandelier \
    --max-pos 100 --period 1y [--regime-filter | --risk-budget | --quality]
```

- **成交模型**：收盘决策 → 次一交易日开盘成交（NextOpen，无前视）；一字涨停买不进、跌停卖不出顺延
- **退出引擎**：time（纯时间）/ stop（固定止损）/ chandelier（吊灯，peak−3×ATR14）
- **仓位**：满仓等权；`--regime-filter` 按市场状态丢弃 avoid 名单策略信号；
  `--risk-budget` 按状态截断总仓位（up_low 100% / up_high 40% / down_high 60% / down_low 30%）

### 市场状态机（regime × strategy_map）

```bash
.venv/bin/python -c "from sequoia_x.strategy_map import regime_markdown; print(regime_markdown())"
```

- **四态定义**：沪深300 收盘 vs MA200（up/down）× 20日年化波动率 vs 1y 中位（high/low），收盘后判定无前视
- **2y 分状态结论**（signal_mask 修复后重跑 2026-09-04）：
  - `down_high`（恐慌下跌）：主攻超跌反抽——上升跌停 86.9%/+9.58%、涨停洗盘 55.6%/+6.38%
  - `up_low`（慢牛）：海龟/均线/RPS 主场（20日 +2.6% 上下）
  - `up_high`（情绪顶）：追高类全灭，回避
  - `down_low`：观望为主
  - ⚠️ 高窄旗形修复"高位抗跌"缺失后 2y 不再正期望（旧结论为回测漂移假象），已降级

### 前瞻跟踪（tracker，akquant 交叉验证）

```bash
.venv/bin/python -m sequoia_x.tracker update    # 注册 → T+1 买入 → 逐日跟踪 → 报告
```

- 日报 cross_hits 共振名单 → T+1 开盘经 **akquant 引擎**撮合买入（佣金万2.5/滑点千1/一字涨停跳过）
- 逐日真实收盘更新市值 → T+5/10/20 收益 vs 同期沪深300
- 意义：**第二引擎独立背书**——akquant 撮合模型（成本/滑点/T+1）比自研模拟器细，
  若系统性偏差说明自研回测口径需修正

---

## 目录结构 | Project Structure

```
Sequoia-X/
├── main.py                      # 入口：增量同步 / --backfill 回填
├── pyproject.toml               # 依赖 + ruff/pytest 配置
├── .env.example                 # 环境变量模板
├── data/                        # SQLite + 研究 JSON 缓存（运行时生成，不入 git）
├── sequoia_x/
│   ├── core/config.py           # Pydantic-settings 配置
│   ├── data/engine.py           # baostock 回填/增量（单进程 + 3 次重试重连）
│   │   └── em_fetcher.py        # 东财数据（⚠️ 限流严重仅手动低频，非自动备源）
│   ├── strategy/                # 6 策略（signal_mask 权威实现）+ base
│   ├── market_rules.py          # 分板涨跌停（实盘+回测单一来源）
│   ├── backtest.py              # 向量化事件研究引擎 v2
│   ├── portfolio.py             # 组合模拟器（NextOpen 撮合 + 退出引擎 + 仓位）
│   ├── regime.py                # 四态市场状态判定
│   ├── strategy_map.py          # Regime→策略映射（focus/avoid）+ 风险预算
│   ├── report.py                # 日报构建（TOP10+共振+🌡️状态+回测参考）
│   ├── tracker.py               # 前瞻跟踪（akquant 引擎）
│   └── notify/feishu.py         # 飞书推送
└── tests/                       # pytest（分板边界/CI/事件结构/防回归）
```

---

## 数据说明

- **数据源**：[baostock](http://baostock.com)（免费、无需注册）
- **复权方式**：后复权（hfq），历史价格不变，适合增量存储
- **存储**：本地 SQLite（`data/sequoia_v2.db`）：`stock_daily`(330万行) +
  `stock_daily_delisted`(101只/24635行) + `index_daily`(沪深300)
- **增量同步**：单进程串行（并发触发 baostock 风控，2026-09-04 实测修复），
  每只 3 次重试（2/4/8s）+ logout/login 重连；全市场 ~16min，wrapper 超时 1200s
- **数据时间**：当日日 K 17:30 入库、复权因子 18:00（后复权价以 18:00 后为准）

---

## 关键研究结论 | Research Notes

- **"正期望策略"需审慎**：高窄旗形曾被判定唯一正期望，实为回测条件比实盘宽
  （缺"高位抗跌"过滤）的实现漂移；signal_mask 单一来源重构后证伪
- **形态策略 = 风格 beta**：全市场平均化掩盖分状态真相——正确做法是 Regime 择时
  切换策略子集，而非寻找全天候策略
- **多策略合并前先淘汰负期望策略**：组合研究证明负 alpha 会淹没正 alpha
- **数据源单一化**：东财/腾讯后复权基准与 baostock 不同（9961 vs 9113 vs 8132），
  跨源混库会污染价格尺度——只强化 baostock 自身重试

---

## 许可证 | License

MIT
