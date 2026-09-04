# Sequoia-X — 开发运维与研究 Skill（仓库自含版）

> 供任何 agent / 新会话拿到本仓库即可继续工作。维护者：hacper（fork 自 sngyai/Sequoia-X）。
> 本文件随项目演进持续更新；Hermes 侧完整版见 skill `sequoia-x-reporting`（含 references 全档）。

## 1. 定位与现状（2026-09-04）

A 股日频量化选股 + 日报推送系统，经两轮深度增强后已成**状态感知的组合决策研究平台**：
「全市场扫描出信号 → 可信回测 → 组合模拟（退出/止损/仓位）→ 市场风格过滤 → 风险预算 → 日报」。

**能力链全貌**：
```
事件研究回测(backtest.py, 33s/1y 向量化)         —— 信号胜率/收益/CI/MAE，理想+可成交双口径
组合模拟器(portfolio.py)                          —— 退出引擎×3 + 仓位模型 + 净值/回撤/超额
市场风格(regime.py)                               —— 沪深300 → up/down×low/high 四态
策略状态机(strategy_map.py)                       —— regime→主攻/回避映射 + 信号过滤
风险预算(portfolio --risk-budget)                 —— 状态→总仓位上限
日报(report.py + cron 0fccfa4dd1f7 交易日20:00)   —— TOP10+共振+回测+🌡️市场状态区块
```

**核心研究结论（1y/2y 实证，net 25bp 成本，含退市股防幸存者偏差）**：
- 无全天候策略。**regime 决定一切**：down_high(恐慌)→超跌反抽类最强（上升跌停 5日胜率86.9%/20日均+9.58%、高窄旗形+8.93%）；up_low(慢牛)→趋势主场（海龟/均线/RPS 20日 52%+/+2.5%+）；up_high(情绪顶)→追高类全灭（上升跌停 -4.30%）
- 全状态合并统计是平均化假象——先分 regime 看，再谈结论
- 组合级 A/B/C 验证（2y 联合4策略）：不过滤 +11.9% → regime过滤 +26.3% → +风险预算 26.1% 但回撤 -16.5→-12.5→**-9.2%**、胜率 45.3→49.7→50.6%
- 吊灯止损(chandelier, peak-3×ATR14) 优于固定-8%止损优于纯时间
- 信号质量排序只在容量受限时有效（max_pos=10: 随机-6.6% vs 排序+0.1%）
- 多策略简单联合 = 负 alpha 污染正 alpha（先淘汰负 alpha 策略再谈合并）

## 2. 架构速览

```
main.py                   日常入口：sync增量(8进程) + 7策略 + 飞书
sequoia_x/
  core/config.py          pydantic-settings(.env)  STRATEGY_WEBHOOK_* 路由
  data/engine.py          SQLite + baostock；sync(重试3次)/backfill/退市股回填
  data/em_fetcher.py      东财直连【仅手动低频】⚠️IP限流狠+复权基准不同
  strategy/*.py           7策略(BaseStrategy)：海龟/均线放量/高窄旗形/涨停洗盘/上升跌停/RPS/定增
  market_rules.py         分板涨跌停(主板10/双创20/北交30, 容差0.98)——实盘+回测共用
  backtest.py             向量化事件研究：compute_events/_future_ret/_exec_ret/_stats(CI)/_mae_stats/_portfolio/_run_grid
  portfolio.py            组合模拟器：PortfolioSim(退出×3/仓位/风险预算) + quality评分 + combined + regime-filter
  regime.py               沪深300(本地 index_daily)→趋势MA200×波动率→四态；asof防未来
  strategy_map.py         REGIME_ADVICE 映射表 + apply_regime_filter + regime_markdown
  report.py               ReportBuilder：ST/次新滤除 + 市值50-800亿 + TOP10 + 共振 + 🌡️regime区块
```

## 3. 常用命令（venv .venv/bin/python）

```bash
# 日报（wrapper 已封装全部；单跑 report）
python -m sequoia_x.report --markdown --json-out data/daily_report.json
# 回测（理想+可成交双口径 + 成本/CI/MAE/组合）
python -m sequoia_x.backtest --period 1y --json-out data/backtest_1y.json
python -m sequoia_x.backtest --grid            # 参数网格
# 组合模拟（退出/仓位/质量/状态机/预算可组合）
python -m sequoia_x.portfolio --strategy 高窄旗形 --exit chandelier --max-pos 100 --daily-k 10 \
    --quality --combined --regime-filter --risk-budget --json-out data/pf.json
# 市场状态（日报会内嵌，也可手查）
python -m sequoia_x.regime   # 若加 CLI
python -c "from sequoia_x.regime import get_market_states; ..."
# 全量回填 / 退市股回填（首次/补数据）
python main.py --backfill
python -c "from sequoia_x.core.config import get_settings; from sequoia_x.data.engine import DataEngine; DataEngine(get_settings()).backfill_delisted()"
```

## 4. 数据源与红线（2026-09-04 实测）

- **baostock 主源**：免费、稳定（实测 20 连查 0 失败）；sync worker 已加 3 次重试+重连（勿回退！原无重试=单次抖动丢当日数据）
- akshare `stock_zh_a_hist` **不可用**：ut token 被东财风控断连（RemoteDisconnected）
- 东财 API 直连去 ut 可通，但 **IP 动态限流极狠**（~4-5 请求/min 即 ban >4min）→ 不做自动备源
- 腾讯 fqkline 稳定但无成交额
- 🔴 **红线：三源后复权基准不同**（茅台 09-03：baostock 9961 vs 腾讯 9113 vs 东财 8132）——**跨源混库=价格尺度污染**，策略/回测全错。成交额各源一致。
- baostock 无北交所（库 5215 只全沪深，双创占 38%）

## 5. 回测口径约定（勿混）

- **理想口径** `{h}`：信号日收盘判定 → 次日收盘买 → N日收盘卖；net=毛-成本25bp；gross_* 保留
- **可成交口径** `{h}_exec`：入场日收盘触涨停→剔除(entry_blocked)；退出触跌停→顺延≤5日(exit_extended)
- panel = 现役 UNION 退市表（stock_daily_delisted，101只/24635行）+ 分板涨跌停列 + prev_close
- 同股 20 交易日冷却去重；RPS 横截面指标单独实现（每日 rank≥90 + 收盘≥120日高×0.9）
- ⚠️ 实盘与回测都 shift(1) 防未来函数（RPS roll_high 原含当日=未来函数，已修）

## 6. 状态机与预算参数（strategy_map.py REGIME_ADVICE / PortfolioSim.RISK_BUDGET）

| regime | 主攻 | 回避 | 风险预算(最大仓位) |
|--------|------|------|------|
| up_low 低波慢牛 | 海龟/均线放量/RPS | 上升跌停/涨停洗盘 | 100% |
| up_high 情绪顶 | 高窄旗形 | 上升跌停/涨停洗盘/海龟 | 40% |
| down_high 恐慌 | 上升跌停/高窄旗形/涨停洗盘 | 海龟突破 | 60% |
| down_low 阴跌 | 高窄旗形 | 海龟/RPS | 30%（观望） |
| na 未知 | - | - | 50% |

## 7. 运维

- **cron `0fccfa4dd1f7`**：交易日北京 20:00（`0 12 * * 1-5` UTC，勿改盘前——数据日≠今日会误判非交易日）；deliver=origin（定时 tick 投递可靠，agent.log 有 `delivered ... via live adapter` 铁证）；script=`/opt/data/scripts/sequoia_report.sh`（sync容错 → 回测缓存刷新 → report → 摘要）
- wrapper `/opt/data/scripts/sequoia_report.sh`：sync timeout 240 容错(exit=124 降级) → backtest 1y 刷新缓存(失败沿用旧) → report → 摘要注入 agent；`SEQUOIA_FORCE=1` 跳过交易日检测（跨日验证）；防呆"滤后>0但区间内全0"→60s重试
- ⚠️ 手动 `cronjob run` **不投递**到 chat（走 delegation，自报 delivered 是假的）——验证投递用一次性定时 job 或等正式 tick
- 僵尸 running 记录卡防重入：`UPDATE executions SET status='completed' WHERE status='running'`（/opt/data/cron/executions.db）
- wrapper 禁止并发（多实例 sync 互踩曾删库——engine 已改 symbol+date 精确删除）

## 8. 开发纪律

- 开发在 **dev 分支**（fork hacperme/Sequoia-X，跟踪 origin/dev）；commit 本地 → 用户确认后 push
- 只 `git add` 功能文件（sequoia_x/*、main.py）；辅助脚本(data/*.json、*.py 工具)与 data/ 不入库
- git 身份：hacper / **git@hacperme.com**（2026-09-04 起，全局+仓库级）
- 每个功能"实现+测试验证"通过才提交；测试先行（策略改动跑 run_local_test.py；回测改动跑冒烟）
- 提交信息中文要点式，标注实测结论与踩坑
- **同步更新本 SKILL.md**（本文档与代码同版本演进；新增功能/新研究结论/新坑必记）
