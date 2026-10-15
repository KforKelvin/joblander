"""后台任务的协作式停止：网页任务托盘点「停止」→ 置位；长任务在安全点自己看一眼、收尾退出。

不强杀线程——停在两条岗位之间，照常记账（见过的照记、已提的照留），下一轮不重复提。
标志按线程绑定：web 的 start_task 在任务线程里 bind；夜扫等不经网页的调用没绑，永远不停。
"""

from __future__ import annotations

import threading

_local = threading.local()


def bind(event: threading.Event | None) -> None:
    _local.event = event


def requested() -> bool:
    ev = getattr(_local, "event", None)
    return bool(ev is not None and ev.is_set())
