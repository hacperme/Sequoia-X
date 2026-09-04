#!/bin/bash
# Sequoia-X 选股日报 wrapper：增量同步 + 生成报告 + 方案A跟踪，输出注入 cron agent
# 用法: bash scripts/sequoia_report.sh   （仓库内维护；/opt/data/scripts/sequoia_report.sh 为软链）
# 返回 stdout 供 agent 解读；无新数据时输出 NO_TRADING_DAY
# 环境变量:
#   SEQUOIA_FORCE=1      跳过交易日检测（跨日强制用最近数据）
#   SEQUOIA_SKIP_SYNC=1  跳过增量同步（验证 tracker/report 链路或应急用）

# 仓库根 = 本脚本所在目录的上级（脚本位于 <repo>/scripts/）
PROJ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_PY="$PROJ/.venv/bin/python"
DATA_DIR="$PROJ/data"

# 1. 增量同步（单进程串行——baostock 并发触发风控 2026-09-04；全市场 5215 只
#    实测 ~0.19s/只 ≈ 16 分钟，故超时给 1200s；cron 20:00 触发约 20:20 完成）
#    注意：同步可能超时 —— 超时不算致命错误，继续尝试生成报告，
#    数据日正确性由下方 NO_TRADING_DAY 逻辑把关。
#    SEQUOIA_SKIP_SYNC=1 跳过同步（验证 tracker/report 链路或应急用）
SYNC_CODE=0
if [ "$SEQUOIA_SKIP_SYNC" = "1" ]; then
    echo "（注：SEQUOIA_SKIP_SYNC=1 跳过增量同步）" >&2
else
    SYNC_OUT=$(cd "$PROJ" && timeout 1200 $VENV_PY main.py 2>&1)
    SYNC_CODE=$?
    if [ $SYNC_CODE -ne 0 ] && [ $SYNC_CODE -ne 124 ]; then
        echo "SEQUOIA_ERROR: 同步失败 (exit=$SYNC_CODE)"
        echo "$SYNC_OUT" | tail -15
        exit 0
    fi
    [ $SYNC_CODE -eq 124 ] && echo "（注：增量同步超时 exit=124，继续用现有数据生成）" >&2
fi

# 3. 最新数据日 = 本地库 MAX(date)；今日 = 北京时间
TODAY_CN=$(TZ=Asia/Shanghai date +%F)
DATA_DATE=$(cd "$PROJ" && $VENV_PY -c "
import sqlite3
conn = sqlite3.connect('data/sequoia_v2.db')
print(conn.execute('SELECT MAX(date) FROM stock_daily').fetchone()[0])
")

# 4. 交易日检测：数据日应 ≥ 最近一个"已收盘"的交易日。
#    - 工作日 15:30 后：最近已收盘=今日（数据日应=今日）
#    - 工作日 15:30 前（盘中/盘前）：最近已收盘=昨日（数据日=昨日正常，出昨日日报）
#    - 周末/节假日：回退到最近工作日，同理按时段判
#    仅当数据日 < 该基准（真非交易日/数据未同步）才 NO_TRADING_DAY。
#    SEQUOIA_FORCE=1 跳过此检查（一次性验证用）
if [ "$SEQUOIA_FORCE" != "1" ]; then
    RECENT_CLOSED=$($VENV_PY -c "
from datetime import date, datetime, timedelta
now = datetime.now()
d = now.date()
if d.weekday() >= 5 or (d.weekday() < 5 and now.hour < 15):  # 周末 或 工作日未收盘
    d -= timedelta(days=1)
while d.weekday() >= 5:  # 再回退周末
    d -= timedelta(days=1)
print(d.isoformat())
")
    if [[ "$DATA_DATE" < "$RECENT_CLOSED" ]]; then
        echo "NO_TRADING_DAY data_date=$DATA_DATE recent_closed=$RECENT_CLOSED"
        echo "（数据日早于最近已收盘交易日：真非交易日或数据未同步。若需强制用最近数据请 SEQUOIA_FORCE=1）"
        exit 0
    fi
fi

# 5. 更新回测参考缓存（滚动 1 年窗口随数据日推进；失败不阻塞日报，沿用旧缓存）
(cd "$PROJ" && timeout 240 $VENV_PY -m sequoia_x.backtest \
    --period 1y --json-out data/backtest_1y.json 2>&1) \
    | grep -vE "INFO|login|logout" >/dev/null
BT_CODE=$?
[ $BT_CODE -ne 0 ] && echo "（注：回测缓存刷新失败 exit=$BT_CODE，沿用旧缓存）" >&2

# 6. 生成日报 JSON（选股 + 回测参考缓存）+ markdown
REPORT_JSON="$DATA_DIR/daily_report.json"
gen_report() {
    (cd "$PROJ" && timeout 300 $VENV_PY -m sequoia_x.report \
        --json-out "$REPORT_JSON" 2>&1) | grep -vE "INFO|login|logout" >/dev/null
}
gen_report
REPORT_CODE=$?
if [ $REPORT_CODE -ne 0 ]; then
    echo "SEQUOIA_ERROR: 报告生成失败 (exit=$REPORT_CODE)"
    exit 0
fi
# 5b. 防呆：候选>0 但市值全空 → 疑似市值查询瞬时失败（2026-09-03 曾发生），等 60s 重试一次
EMPTY_CHECK=$($VENV_PY -c "
import json
d = json.load(open('$REPORT_JSON', encoding='utf-8'))
tot = sum(s['count_total'] for s in d['strategies'])
rin = sum(s['count_in_range'] for s in d['strategies'])
print('RETRY' if tot > 0 and rin == 0 else 'OK')
")
if [ "$EMPTY_CHECK" = "RETRY" ]; then
    echo "（注：首轮滤后候选>0 但市值区间内 0 只，疑似市值查询失败，60s 后重试）" >&2
    sleep 60
    gen_report
    REPORT_CODE=$?
    if [ $REPORT_CODE -ne 0 ]; then
        echo "SEQUOIA_ERROR: 报告重试仍失败 (exit=$REPORT_CODE)"
        exit 0
    fi
fi

# 5c. 方案 A 跟踪更新（2026-09-04 接入日报 cron）：注册新批次 → pending 批买入 →
#     逐日市值更新。依赖刚生成的 daily_report.json + 库内最新数据。
#     失败不致命（不影响日报本体），仅打印跟踪报告行供 agent 组织。
TRACK_OUT=$(cd "$PROJ" && timeout 300 $VENV_PY -m sequoia_x.tracker update 2>&1)
TRACK_CODE=$?
if [ $TRACK_CODE -eq 0 ]; then
    echo ""
    echo "----- 方案A 跟踪更新 -----"
    echo "$TRACK_OUT" | grep -vE "INFO \| akquant"
else
    echo "（注：跟踪更新失败 exit=$TRACK_CODE，不影响日报）" >&2
fi

# 6. 输出结构化摘要（策略计数 + 共振数），agent 读 JSON 详析
$VENV_PY -c "
import json
d = json.load(open('$REPORT_JSON', encoding='utf-8'))
bt = d.get('backtest', {})
lines = []
lines.append(f\"数据日: {d['date']}\")
lines.append(f\"共振票: {len(d['cross_hits'])} 只\")
for s in d['strategies']:
    lines.append(f\"  {s['name']}: 滤后 {s['count_total']} / 区间内 {s['count_in_range']} / TOP{min(10, len(s['top']))}\")
if bt:
    lines.append(f\"回测参考: {bt['range']}（JSON 内含 5/10/20 日胜率）\")
print(chr(10).join(lines))
"
echo ""
echo "完整结构化结果: $REPORT_JSON"
echo "（请读该 JSON 组织日报：TOP10 名单、共振票点评、回测参考。免责声明必带。）"
