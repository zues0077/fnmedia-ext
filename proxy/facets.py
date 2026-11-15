"""虚拟库筛选条目的本地词表（state/facets.json）。

为什么需要这份文件
------------------
筛选条的类型/国家地区/年份，原本都是「每次从当前数据现算」：

- 榜单四库：解析豆瓣 card_subtitle；
- 海量四库：CMS 分类名 + 已浏览条目的 area/year（纯内存缓存，重启即空）。

后果是筛选项会「缩水」：榜单刷新一轮、某个类型这期没命中就消失；海量库重启后
地区/年代直接变空；新源加进来出现的新分类也要等下一次浏览才出现。

用户要求（2026-11-15）：筛选项要像「归类」一样固定在本机——已经在列的条目
位置不动，以后新源带来新条目只是**追加**，重启、换榜单都不丢。

设计
----
- 每个库一套键：``chart:movie`` / ``catalog:movie`` …，互不干扰；
- **只存名字**，id 仍由 crc32 现算，保证跨版本、跨设备稳定；
- **只增不减**：已存在的保持原顺序，新名字追加到末尾；
- 每类有上限，超了就不再追加（长期跑也不会涨成几千项）；
- 文件坏了不致命：解析失败就按空库启动，并在下次写入时修好。
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

_STATE_DIR = Path(__file__).resolve().parent.parent / "state"
_FACET_FILE = _STATE_DIR / "facets.json"
# 随包内置的默认词表（proxy/assets 会打进 fpk）：新装/换机即有一整套筛选项，
# 不必从零累积。用户本地 state/facets.json 优先，升级不覆盖。
_DEFAULT_FILE = Path(__file__).resolve().parent / "assets" / "facets.default.json"
_FACET_VERSION = 1

# 每类上限：类型最多 240（海量四库跨 184 个源，去重后几百个很常见）、
# 地区 80、年份 40。到顶后只保留已有条目，不再追加。
_LIMITS = {"genres": 240, "areas": 80, "decades": 40}


class FacetStore:
    """按库累积筛选条目，落盘 state/facets.json（只增不减）。"""

    def __init__(self, path: Path | None = None, use_default: bool = True) -> None:
        self._path = Path(path) if path else _FACET_FILE
        # 测试/特殊场景可关掉「内置默认词表」，只吃本地文件
        self._use_default = bool(use_default)
        self._lock = threading.RLock()
        self._libs: dict[str, dict[str, list[str]]] = {}
        self._updated = 0
        self.load()

    # ---------------------------------------------------------------- 读写
    def load(self) -> bool:
        """优先读本地 state/facets.json，没有就退回随包内置默认项。

        返回是否读到了「本地」文件：False = 当前用的是内置默认，
        等首次 merge 有新条目时才落盘到 state（避免一启动就写文件）。
        """
        if self._read(self._path):
            return True
        if self._use_default:
            self._read(_DEFAULT_FILE)  # 内置默认：只进内存，不写盘
        return False

    def _read(self, path: Path) -> bool:
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return False
        except Exception:  # noqa: BLE001 — 坏文件按空库启动，下次写入自动修好
            return False
        if not isinstance(data, dict) or data.get("v") != _FACET_VERSION:
            return False
        libs = data.get("libs")
        if not isinstance(libs, dict):
            return False
        clean: dict[str, dict[str, list[str]]] = {}
        for key, kinds in libs.items():
            if not isinstance(kinds, dict):
                continue
            bucket: dict[str, list[str]] = {}
            for kind in _LIMITS:
                vals = kinds.get(kind) or []
                got: list[str] = []
                if isinstance(vals, list):
                    for v in vals:
                        s = str(v or "").strip()
                        if s and s not in got:
                            got.append(s)
                bucket[kind] = got
            clean[str(key)] = bucket
        with self._lock:
            self._libs = clean
            try:
                self._updated = int(data.get("updated") or 0)
            except (TypeError, ValueError):
                self._updated = 0
        return True

    def _flush(self) -> None:
        """原子写盘：先写 .tmp 再 os.replace（与 classify_rules.json 一致）。"""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"v": _FACET_VERSION, "updated": int(time.time()),
                       "libs": self._libs}
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                           encoding="utf-8")
            os.replace(tmp, self._path)
            self._updated = int(time.time())
        except Exception:  # noqa: BLE001 — 写盘失败不影响本次响应
            pass

    # ---------------------------------------------------------------- 查询
    def ordered(self, key: str, kind: str) -> list[str]:
        with self._lock:
            return list((self._libs.get(key) or {}).get(kind) or [])

    def snapshot(self) -> dict[str, dict[str, list[str]]]:
        with self._lock:
            return {k: {kk: list(vv) for kk, vv in v.items()}
                    for k, v in self._libs.items()}

    # ---------------------------------------------------------------- 写入
    def merge(self, key: str, kind: str, names) -> list[str]:
        """把这一轮看到的条目并进本地库，返回合并后的完整顺序。

        - 已有的保持原位（这是「固定在本地」的关键：按钮顺序不随数据抖动）；
        - 新名字按出现顺序追加到末尾；
        - 到上限后不再追加。
        """
        if kind not in _LIMITS:
            return []
        with self._lock:
            bucket = self._libs.setdefault(
                key, {k: [] for k in _LIMITS})
            cur = bucket.setdefault(kind, [])
            changed = False
            limit = _LIMITS[kind]
            for raw in names or []:
                s = str(raw or "").strip()
                if not s or s in cur:
                    continue
                if len(cur) >= limit:
                    break
                cur.append(s)
                changed = True
            if changed:
                self._flush()
            return list(cur)

    def prune(self, key: str, kind: str, names) -> list[str]:
        """从词表里剔除指定名字，返回剩下的顺序。

        只用于「预置词不再适用」的一次性迁移（如福利库不再铺通用题材）：
        词表整体是「只增不减」的，历史写进去的预置词不主动删就永远占着位。
        删除只按名字精确匹配，其余条目顺序原样保留；真删掉了才写盘。
        """
        if kind not in _LIMITS:
            return []
        drop = {str(n or "").strip() for n in names or []}
        drop.discard("")
        with self._lock:
            bucket = self._libs.get(key)
            if not bucket or not drop:
                return list((bucket or {}).get(kind) or [])
            cur = bucket.get(kind) or []
            kept = [v for v in cur if v not in drop]
            if len(kept) != len(cur):
                bucket[kind] = kept
                self._flush()
            return list(kept)


facets_store = FacetStore()
