"""探针：模拟「Qt 对象已销毁但 worker 读取线程仍在派发信号」的场景。

这是 0xC0000409 (STATUS_STACK_BUFFER_OVERRUN) 的候选路径：
QtWorkerClient 是 QObject，parent=MainWindow；MainWindow 销毁后其 C++ 侧失效，
而 worker 的 stdout 读取线程（守护线程）仍会回调 _on_worker_envelope -> emit。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath("."))
sys.path.insert(0, os.path.abspath("src"))
os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtWidgets import QApplication  # noqa: E402

from spectra_workbench.protocol.messages import Envelope, Kind  # noqa: E402
from spectra_workbench.scheduler.client import QtWorkerClient  # noqa: E402

app = QApplication.instance() or QApplication([])
client = QtWorkerClient(provider_id="gdal", project_root=os.path.abspath("."))
print("client created, closed =", client._closed)

env = Envelope(
    kind=Kind.ready, message_id="m1", worker_id="w1", generation=0, payload={"role": "gdal", "runtime_id": "rt-1"}
)

print("--- 关闭前派发（应正常） ---")
client._on_worker_envelope(env)
print("OK, 未抛异常")

print("--- 关闭后派发（应被丢弃，不得 emit） ---")
client.stop(timeout=1.0)
print("closed =", client._closed)
try:
    client._on_worker_envelope(env)
    print("OK: 关闭后 envelope 被安全丢弃，未 emit 到已销毁对象")
except Exception as exc:
    print(f"FAIL: 关闭后仍抛异常: {type(exc).__name__} {exc}")
    sys.exit(1)
