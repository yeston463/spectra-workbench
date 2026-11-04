"""瓦片/窗口缓存与视图代际（P3-03）。

要点：
1. 缓存有**字节预算**并按 LRU 淘汰；预算来自配置，不硬占固定数 GB。
2. 键里包含源指纹、波段、窗口、输出尺寸与概览层，避免跨视图/跨图层串味。
3. 每个视图有代际号；过期响应直接丢弃，不覆盖新视图。
4. 只缓存**原始窗口字节**，不缓存拉伸结果——显示参数变化不需要重新读盘。
"""

from __future__ import annotations

import os
from collections import OrderedDict
from dataclasses import dataclass, field

DEFAULT_BUDGET_MIB = 256
MAX_BUDGET_MIB = 512


def default_budget_bytes() -> int:
    """默认预算：可用内存的 10%，上限 512 MiB，可用 SPECTRA_TILE_CACHE_MIB 覆盖。"""
    override = os.environ.get("SPECTRA_TILE_CACHE_MIB")
    if override:
        try:
            return max(1, int(float(override))) * 1024 * 1024
        except ValueError:
            pass
    avail = None
    try:
        import ctypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        st = MEMORYSTATUSEX()
        st.dwLength = ctypes.sizeof(st)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            avail = int(st.ullAvailPhys)
    except Exception:
        avail = None
    if not avail:
        return DEFAULT_BUDGET_MIB * 1024 * 1024
    want = int(avail * 0.10)
    return max(16 * 1024 * 1024, min(want, MAX_BUDGET_MIB * 1024 * 1024))


@dataclass(frozen=True)
class TileKey:
    source: str  # 路径 + 指纹，不含可变显示参数
    bands: tuple[int, ...]
    x_off: int
    y_off: int
    win_w: int
    win_h: int
    out_w: int
    out_h: int
    overview: int


@dataclass
class CacheStats:
    hits: int = 0
    misses: int = 0
    evictions: int = 0
    bytes: int = 0
    entries: int = 0
    budget_bytes: int = 0

    @property
    def hit_ratio(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0


class TileCache:
    """LRU 字节预算缓存。超预算时按最近最少使用逐出，直到装得下。"""

    def __init__(self, budget_bytes: int | None = None) -> None:
        self.budget_bytes = int(budget_bytes) if budget_bytes else default_budget_bytes()
        self._data: OrderedDict[TileKey, bytes] = OrderedDict()
        self._sizes: dict[TileKey, int] = {}
        self.hits = 0
        self.misses = 0
        self.evictions = 0

    def set_budget(self, budget_bytes: int) -> None:
        if budget_bytes <= 0:
            raise ValueError("缓存预算必须为正数")
        self.budget_bytes = int(budget_bytes)
        self._trim()

    def get(self, key: TileKey) -> bytes | None:
        payload = self._data.get(key)
        if payload is None:
            self.misses += 1
            return None
        self.hits += 1
        self._data.move_to_end(key)
        return payload

    def put(self, key: TileKey, payload: bytes) -> None:
        size = len(payload)
        if size > self.budget_bytes:
            # 单条超过总预算：不缓存，也不清空已有内容
            self._trim()
            return
        if key in self._data:
            self._data[key] = payload
            self._sizes[key] = size
            self._data.move_to_end(key)
        else:
            self._data[key] = payload
            self._sizes[key] = size
        self._trim()

    def _trim(self) -> None:
        total = sum(self._sizes.values())
        while total > self.budget_bytes and self._data:
            oldest, _ = self._data.popitem(last=False)
            total -= self._sizes.pop(oldest, 0)
            self.evictions += 1

    def invalidate_source(self, source: str) -> int:
        """源变化（如重新定位文件、任务覆写成果）时丢弃该源全部缓存。"""
        doomed = [
            k for k in self._data
            if k.source == source or k.source.startswith(f"{source}|")
        ]
        for k in doomed:
            self._sizes.pop(k, None)
            self._data.pop(k, None)
        return len(doomed)

    def clear(self) -> None:
        self._data.clear()
        self._sizes.clear()

    @property
    def stats(self) -> CacheStats:
        return CacheStats(
            hits=self.hits,
            misses=self.misses,
            evictions=self.evictions,
            bytes=sum(self._sizes.values()),
            entries=len(self._data),
            budget_bytes=self.budget_bytes,
        )


@dataclass
class ViewGeneration:
    """视图代际：每次视图/图层/波段变化递增，过期响应据此丢弃。"""

    generation: int = 0
    layer_id: str | None = None
    pending_newer: bool = False
    _lock_note: str = field(default="single-threaded Qt main thread", repr=False)

    def bump(self, layer_id: str | None = None) -> int:
        self.generation += 1
        if layer_id is not None:
            self.layer_id = layer_id
        return self.generation

    def is_current(self, generation: int, layer_id: str | None) -> bool:
        return generation == self.generation and (
            layer_id == self.layer_id or (layer_id is None and self.layer_id is None)
        )
