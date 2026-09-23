#!/bin/bash
# Sequoia-X 选股日报 wrapper：日历/数据就绪检查 + 增量同步 + 生成报告 + 方案A跟踪，输出注入 cron agent
# 用法: bash scripts/sequoia_report.sh   （仓库内维护；cron 经 /opt/data/scripts/sequoia_report.sh
#       真文件薄启动器 exec 本脚本——勿改成软链：cron runner realpath 校验拒链外解析，2026-09-04）
# 返回 stdout 供 agent 解读。标志行：
#   NO_TRADING_DAY   权威日历判定今日非交易日（真休市）
#   DATA_NOT_READY   交易日但 baostock 当日数据未发布/同步后仍缺 —— ⚠️ 不是非交易日
#   SEQUOIA_ERROR    同步或报告生成失败
# 环境变量:
#   SEQUOIA_FORCE=1       跳过日历与数据就绪检查（跨日强制用现有数据，一次性验证用）
#   SEQUOIA_SKIP_SYNC=1   跳过增量同步（验证 tracker/report 链路或应急用）
#   SEQUOIA_PROBE_WAIT    数据未发布时的轮询间隔秒（默认 600）
#   SEQUOIA_PROBE_MAX     最大轮询轮数（默认 8 → 最多等 80 分钟）

# 仓库根 = 本脚本所在目录的上级（脚本位于 <repo>/scripts/）
PROJ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_PY="$PROJ/.venv/bin/python"
DATA_DIR="$PROJ/data"
# ⚠️ pipefail（2026-09-04）：gen_report 走 "| grep -v" 管道，无 pipefail 时 grep 退出码
#    会掩盖 report 被 timeout 截杀（exit 124）——曾致超时后静默沿用旧 daily_report.json
#    （tracker/摘要读到 09-03 旧数据）。加 pipefail 让管道如实返回首条失败退出码。
set -o pipefail

DB_MAX_DATE() {
    (cd "$PROJ" && $VENV_PY -c "
import sqlite3
conn = sqlite3.connect('data/sequoia_v2.db')
print(conn.execute('SELECT MAX(date) FROM stock_daily').fetchone()[0])
")
}

# ─── 1. 交易日历 + 数据发布就绪检查（2026-09-20 重构）─────────────────────
# 旧实现只比较「库内最新数据日」与「日历最近已收盘日」，且日历回退只跳周末、不识别节假日，
# 造成 2026-09-18 事故：当日为真交易日，但 baostock 的 09-18 日 K 到 21:00 才发布
# （20:00 同步拿不到），被误判 NO_TRADING_DAY → 整条流水线短路（backtest/report/tracker
# 全跳过），靠 cron agent 临场复查重跑才补上，且漏掉 tracker 步骤（离场提示因此从未送达）。
# 现改为：① 用 baostock 权威日历（query_trade_dates）判「是否交易日」，节假日也算得准；
#        ② 交易日但数据未发布 → 探针轮询等待（PROBE_WAIT × PROBE_MAX）后仍无 → DATA_NOT_READY。
# ⚠️ 日历检查置于同步之前：避免数据未发布时白跑 ~48 分钟全市场同步。
TODAY_CN=$(TZ=Asia/Shanghai date +%F)
DATA_DATE=$(DB_MAX_DATE)
IS_TRADING_TODAY="UNKNOWN"
RECENT_CLOSED=""

if [ "$SEQUOIA_FORCE" != "1" ]; then
    CAL_OUT=$(cd "$PROJ" && timeout 150 $VENV_PY -m sequoia_x.trading_calendar --recent-closed 2>/dev/null | tail -1)
    IS_TRADING_TODAY=$(printf '%s' "$CAL_OUT" | sed -n 's/.*IS_TRADING_TODAY=\([^ ]*\).*/\1/p')
    RECENT_CLOSED=$(printf '%s' "$CAL_OUT" | sed -n 's/.*RECENT_CLOSED=\([^ ]*\).*/\1/p')
    if [ -z "$RECENT_CLOSED" ] || [ "$RECENT_CLOSED" = "UNKNOWN" ]; then
        # 日历不可用 → 回退旧推算（仅跳周末，不识别节假日；纯兜底）
        IS_TRADING_TODAY="UNKNOWN"
        RECENT_CLOSED=$(TZ=Asia/Shanghai $VENV_PY -c "
from datetime import date, datetime, timedelta
now = datetime.now()
d = now.date()
if d.weekday() >= 5 or (d.weekday() < 5 and now.hour < 15):  # 周末 或 工作日未收盘
    d -= timedelta(days=1)
while d.weekday() >= 5:  # 再回退周末
    d -= timedelta(days=1)
print(d.isoformat())
")
        echo "（注：交易日历不可用，回退周末推算 recent_closed=$RECENT_CLOSED）" >&2
    fi

    # ① 非交易日：数据已是最新交易日 → NO_TRADING_DAY；若数据落后（上次失败）→ 继续补跑
    #    （避免非交易日一律短路而丢失补跑机会；手动重跑同日报告请用 SEQUOIA_FORCE=1）
    if [ "$IS_TRADING_TODAY" = "0" ]; then
        if [ -n "$RECENT_CLOSED" ] && [[ "$DATA_DATE" < "$RECENT_CLOSED" ]]; then
            echo "（注：$TODAY_CN 非交易日，但库内数据日 $DATA_DATE < 最近交易日 $RECENT_CLOSED → 执行补跑）" >&2
        else
            echo "NO_TRADING_DAY data_date=$DATA_DATE recent_closed=$RECENT_CLOSED（日历判定 $TODAY_CN 非交易日，数据已是最新交易日）"
            exit 0
        fi
    fi

    # ② 交易日但当日数据未发布 → 探针轮询等待（跳过同步时不等待，避免手动验证被拖住）
    if [ "$SEQUOIA_SKIP_SYNC" != "1" ] && [ -n "$RECENT_CLOSED" ] && [[ "$DATA_DATE" < "$RECENT_CLOSED" ]]; then
        PROBE_WAIT="${SEQUOIA_PROBE_WAIT:-600}"
        PROBE_MAX="${SEQUOIA_PROBE_MAX:-8}"
        echo "（数据日 $DATA_DATE < 应达 $RECENT_CLOSED，先探针等待 baostock 发布…）" >&2
        PROBED=0
        PUB="PUBLISHED=0"
        while [ "$PROBED" -lt "$PROBE_MAX" ]; do
            PUB=$(cd "$PROJ" && timeout 150 $VENV_PY -m sequoia_x.trading_calendar --probe "$RECENT_CLOSED" 2>/dev/null | tail -1)
            [ "$PUB" = "PUBLISHED=1" ] && break
            # 探针本身不可用（网络/服务异常）→ 不阻塞，交给同步与后续校验兜底
            [ "$PUB" = "PUBLISHED=UNKNOWN" ] && break
            PROBED=$((PROBED + 1))
            [ "$PROBED" -lt "$PROBE_MAX" ] && sleep "$PROBE_WAIT"
        done
        if [ "$PUB" = "PUBLISHED=0" ]; then
            echo "DATA_NOT_READY data_date=$DATA_DATE expected=$RECENT_CLOSED waited=$PROBED 轮"
            echo "（交易日 $RECENT_CLOSED 的日 K 未发布：已轮询 ${PROBED} 轮仍无数据。"
            echo "  ⚠️ 这不是非交易日 —— 请勿回复「今日非交易日」，应说明数据延迟并建议稍后查看。）"
            exit 0
        fi
        [ "$PUB" = "PUBLISHED=UNKNOWN" ] && echo "（注：探针不可用，直接继续同步）" >&2
    fi
fi

# ─── 2. 增量同步（单进程串行——baostock 并发触发风控 2026-09-04；全市场 5215 只
#    实测 ~0.19s/只 ≈ 16 分钟，2026-09-04 实际 ~0.23s/只 ≈ 20 分钟；
#    ⚠️ 2026-09-15 实测 0.56s/只 ≈ 48.5 分钟（晚间 baostock 明显变慢）。
#    engine 是"全部拉完才一次性落库"，超时=0 行写入，故超时放宽到 3600s。
#    超时不算致命错误，继续尝试生成报告 —— 数据日正确性由下方二次校验把关。
SYNC_CODE=0
if [ "$SEQUOIA_SKIP_SYNC" = "1" ]; then
    echo "（注：SEQUOIA_SKIP_SYNC=1 跳过增量同步）" >&2
else
    SYNC_OUT=$(cd "$PROJ" && timeout 3600 $VENV_PY main.py 2>&1)
    SYNC_CODE=$?
    if [ $SYNC_CODE -ne 0 ] && [ $SYNC_CODE -ne 124 ]; then
        echo "SEQUOIA_ERROR: 同步失败 (exit=$SYNC_CODE)"
        echo "$SYNC_OUT" | tail -15
        exit 0
    fi
    [ $SYNC_CODE -eq 124 ] && echo "（注：增量同步超时 exit=124，继续用现有数据生成）" >&2
fi

# ─── 3. 同步后二次校验：数据日仍未达 → DATA_NOT_READY（而非 NO_TRADING_DAY）
DATA_DATE=$(DB_MAX_DATE)
if [ "$SEQUOIA_FORCE" != "1" ] && [ -n "$RECENT_CLOSED" ] && [[ "$DATA_DATE" < "$RECENT_CLOSED" ]]; then
    echo "DATA_NOT_READY data_date=$DATA_DATE expected=$RECENT_CLOSED waited=同步后校验"
    echo "（交易日 $RECENT_CLOSED 已收盘，但同步后库内仍无该日数据：疑似 baostock 延迟或同步异常。"
    echo "  ⚠️ 这不是非交易日；请说明数据延迟、建议稍后重跑。）"
    exit 0
fi

# ─── 4. 更新回测参考缓存（滚动 1 年窗口随数据日推进；失败不阻塞日报，沿用旧缓存）
(cd "$PROJ" && timeout 240 $VENV_PY -m sequoia_x.backtest \
    --period 1y --json-out data/backtest_1y.json 2>&1) \
    | grep -vE "INFO|login|logout" >/dev/null
BT_CODE=$?
[ $BT_CODE -ne 0 ] && echo "（注：回测缓存刷新失败 exit=$BT_CODE，沿用旧缓存）" >&2

# ─── 5. 生成日报 JSON（选股 + 回测参考缓存）
REPORT_JSON="$DATA_DIR/daily_report.json"
gen_report() {
    (cd "$PROJ" && timeout 600 $VENV_PY -m sequoia_x.report \
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

# ─── 6. 方案 A 跟踪更新（2026-09-04 接入日报 cron）：注册新批次 → pending 批买入 →
#     逐日市值更新。依赖刚生成的 daily_report.json + 库内最新数据。
#     失败不致命（不影响日报本体）。
TRACK_OUT=$(cd "$PROJ" && timeout 300 $VENV_PY -m sequoia_x.tracker update 2>&1)
TRACK_CODE=$?
if [ $TRACK_CODE -eq 0 ]; then
    echo ""
    echo "----- 方案A 跟踪更新 -----"
    echo "$TRACK_OUT" | grep -vE "INFO \| akquant"
else
    echo "（注：跟踪更新失败 exit=$TRACK_CODE，不影响日报）" >&2
fi

# ─── 7. 结构化摘要（策略计数 + 共振数 + 🚪 离场提示）
#     2026-09-20 起离场提示**直接进入本摘要**：此前只出现在上一步 tracker 的长输出里，
#     而 cron prompt 又未要求输出该段，导致该功能上线后从未真正送达用户（自检发现）。
$VENV_PY -c "
import json, sys
sys.path.insert(0, '$PROJ')
d = json.load(open('$REPORT_JSON', encoding='utf-8'))
bt = d.get('backtest', {})
lines = []
lines.append(f\"数据日: {d['date']}\")
lines.append(f\"共振票: {len(d['cross_hits'])} 只\")
for s in d['strategies']:
    lines.append(f\"  {s['name']}: 滤后 {s['count_total']} / 区间内 {s['count_in_range']} / TOP{min(10, len(s['top']))}\")
if bt:
    lines.append(f\"回测参考: {bt['range']}（JSON 内含 5/10/20 日胜率）\")

# 🚪 离场提示（日报必须单列小节，勿省略）
try:
    from sequoia_x import tracker as tk
    state = tk._load_state()
    names = tk._load_names()
    rows = tk.exit_signals(state)
    trig = [r for r in rows if r['triggers']]
    n_time = sum(1 for r in trig if 'time' in r['triggers'])
    n_ch = sum(1 for r in trig if 'chandelier' in r['triggers'])
    lines.append('')
    lines.append(f\"🚪 离场提示: 持仓 {len(rows)} 只（按个股合并）｜建议离场 {len(trig)} 只（到期 {n_time}｜吊灯 {n_ch}）\")
    _decl = tk._trail_off_declared()
    # 注意：本块整体是 shell 双引号字符串 → 代码里出现裸 " 会被 shell 吃掉（曾致 SyntaxError）
    _decl_txt = ''
    if _decl:
        _parts = []
        for _n, _v in _decl.items():
            _parts.append(_n + '→' + '/'.join(_v) + ' 不提示吊灯')
        _decl_txt = '；移动止损按来源策略启停：' + '、'.join(_parts)
    lines.append(f\"   口径：持有满该股定档持有期（按信号来源策略；老批次 {tk.EXIT_HOLD_DAYS} 日）或 收盘 < 峰值−{tk.EXIT_CHANDELIER_K}×ATR14{_decl_txt}\")
    # 移动止损按「来源策略声明」启停（与 tracker.report 同口径，勿只改一处）
    _off = [r for r in rows if not r.get('trail_on', True)]
    if _off:
        _rg = next((r.get('regime') for r in _off if r.get('regime')), '?')
        lines.append(f\"   ⚠️ 当前状态 {_rg}：本次有 {len(_off)}/{len(rows)} 只（来源策略已声明关闭移动止损，如 RPS 突破）只提示到期、不提示吊灯破位（2026-09-22 退出规则评估口径）\")
    if trig:
        for r in trig:
            hd = r['hold_days']
            tags = '+'.join({'time': f'到期{hd}日', 'chandelier': '吊灯'}.get(t, t) for t in r['triggers'])
            lines.append(f\"   {r['code']} {names.get(r['code'], '?')}  {r['ret_pct']:+.2f}%  持{r['bars']}日  {tags}\")
    else:
        lines.append('   （今日无触发）')
    hold = [r for r in rows if not r['triggers']]
    if hold:
        worst = sorted(hold, key=lambda r: r['ret_pct'])[:3]
        txt = '｜'.join(f\"{r['code']}{names.get(r['code'], '?')} {r['ret_pct']:+.2f}%\" for r in worst)
        lines.append(f\"   ⏸ 继续持有 {len(hold)} 只，浮亏最大: {txt}\")
    soon = [r for r in rows if not r['triggers'] and 1 <= r['hold_days'] - r['bars'] <= 2]
    if soon:
        txt = '｜'.join(
            f\"{r['code']}{names.get(r['code'], '?')}还需{r['hold_days'] - r['bars']}日\"
            for r in sorted(soon, key=lambda x: x['bars'], reverse=True))
        lines.append(f\"   ⏳ 即将到期 {len(soon)} 只: {txt}\")
except Exception as e:
    lines.append(f\"🚪 离场提示: 计算失败（{type(e).__name__}: {e}）\")

print(chr(10).join(lines))
"
echo ""
echo "完整结构化结果: $REPORT_JSON"
echo "（请读该 JSON 组织日报：TOP10 名单、共振票点评、回测参考、🚪 离场提示必带。免责声明必带。）"
