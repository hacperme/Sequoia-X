"""baostock query 看门狗（2026-09-23 增强）的回归测试。

背景（真实故障）：baostock 的收包循环 `util/socketutil.py send_msg` 以结束标记
`<![CDATA[]]>` 判定读完；服务端不回该标记时**永久阻塞在 recv**，socket 超时拦不住
（超时只在两次 recv 之间生效，循环本身不退出）→ 2026-09-22 21:01 cron 与 09-23 手动
运行各挂死一次（Worker CPU≈0、无 socket 读、日志停在 login success!、wrapper 白等
3600s）。这里用「会睡死的 query」替身复现，断言看门狗能把它打断并走重试。
"""

import sys
import time
import types

import pytest

from sequoia_x.data import engine


class _StubRS:
    """baostock 结果集替身。"""

    def __init__(self, rows, error_code="0"):
        self._rows = list(rows)
        self.error_code = error_code
        self.error_msg = ""

    def next(self) -> bool:
        return bool(self._rows)

    def get_row_data(self) -> list:
        return self._rows.pop(0)


class _StubBS:
    """baostock 替身：可注入 login/query/logout 行为并计数。"""

    def __init__(self, query_impl):
        self.calls = {"login": 0, "logout": 0, "query": 0}
        self._query_impl = query_impl

    def login(self):
        self.calls["login"] += 1
        return types.SimpleNamespace(error_code="0", error_msg="")

    def logout(self):
        self.calls["logout"] += 1
        return types.SimpleNamespace(error_code="0", error_msg="")

    def query_history_k_data_plus(self, *args, **kwargs):
        self.calls["query"] += 1
        return self._query_impl(*args, **kwargs)


@pytest.fixture
def install_stub(monkeypatch):
    """把 baostock 换成替身，并把看门狗压到 1s 让测试够快。"""

    def _install(query_impl):
        stub = _StubBS(query_impl)
        monkeypatch.setitem(sys.modules, "baostock", stub)
        monkeypatch.setitem(
            sys.modules,
            "baostock.common",
            types.SimpleNamespace(
                context=types.SimpleNamespace(default_socket=None)
            ),
        )
        monkeypatch.setenv("SEQUOIA_QUERY_TIMEOUT", "1")
        return stub

    return _install


def _one_task(symbol="sh.600000"):
    return [(symbol, symbol, "2026-09-22", "2026-09-23")]


def test_watchdog_breaks_hung_query_and_skips(install_stub):
    """挂死的 query 必须被看门狗打断 → 3 次重试后跳过，绝不永久阻塞。"""

    def hang(*args, **kwargs):
        time.sleep(60)  # 模拟「recv 永久阻塞」

    stub = install_stub(hang)
    t0 = time.time()
    out = engine._bs_fetch_batch(_one_task())
    elapsed = time.time() - t0

    assert out == []
    assert elapsed < 30, f"看门狗未生效，耗时 {elapsed:.1f}s"
    assert stub.calls["query"] == 3, "应重试 3 次"
    assert stub.calls["login"] >= 3, "每次失败都应重连"


def test_happy_path_row_passthrough(install_stub):
    """正常路径不受重构影响：行数据原样带 symbol 前缀返回。"""

    def one_row(*args, **kwargs):
        return _StubRS([["2026-09-22", "1", "2", "0.5", "1.5", "1000", "2000"]])

    install_stub(one_row)
    out = engine._bs_fetch_batch(_one_task())

    assert out == [
        ["sh.600000", "2026-09-22", "1", "2", "0.5", "1.5", "1000", "2000"]
    ]


def test_error_code_triggers_retry(install_stub):
    """结果集 error_code != 0 走重试分支（而非静默当空数据）。"""

    def bad(*args, **kwargs):
        return _StubRS([], error_code="10001")

    stub = install_stub(bad)
    out = engine._bs_fetch_batch(_one_task())

    assert out == []
    assert stub.calls["query"] == 3


def test_periodic_reconnect(install_stub, monkeypatch):
    """每 N 只主动重连一次（对齐 backfill 的成熟模式）。"""

    def empty(*args, **kwargs):
        return _StubRS([])

    stub = install_stub(empty)
    monkeypatch.setenv("SEQUOIA_RECONNECT_EVERY", "2")
    tasks = [
        (f"sh.60000{i}", f"sh.60000{i}", "2026-09-22", "2026-09-23")
        for i in range(4)
    ]
    engine._bs_fetch_batch(tasks)

    # 第 3 只前触发 1 次主动重连 → login = 1(初始) + 1(重连)
    assert stub.calls["login"] == 2
    assert stub.calls["logout"] >= 1
