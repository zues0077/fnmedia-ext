#!/usr/bin/env python3
"""定期体检：主动探测所有资源站，连续 3 次失效则停用（内置源）或删除（订阅/自定义源）。

设计要点：
- 主动探测（复用 providers.probe_source_alive 的多关键词检测），与被动的
  sourcehealth 运行时画像互补：后者只管排序降权，这里才做「停用/删除」动作。
- 连续失效计数持久化到 state/source_probe.json，重启不丢。
- 内置源（builtin=True）：失效 3 次 → 停用（永不删，可手动恢复）；
  订阅/自定义源（builtin=False）：失效 3 次 → 删除。
- 停用源也参与巡检；系统停用（sys_disabled）的源复活后自动启用，
  用户手动停用（enabled=false 无 sys_disabled）的永不自动启用。
- 直播源（live=True）用 m3u8 可达性探测（区别于 CMS 关键词探测）。

此模块无专有逻辑，可进入公开构建（未启用体检时循环空转）。
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from pathlib import Path

try:
    import config as _config
except Exception:  # pragma: no cover
    _config = None

# 默认体检周期（秒）= 6 小时；连续失效阈值 = 3 次。
PERIOD = 6 * 3600
FAIL_THRESHOLD = 3
BOOT_DELAY = 60

_STATE_PATH = Path(__file__).resolve().parent.parent / "state" / "source_probe.json"
_LOCK = threading.RLock()


def _load_state() -> dict:
    try:
        j = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
        return j if isinstance(j, dict) else {}
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    try:
        _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _STATE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        tmp.replace(_STATE_PATH)
    except Exception:  # noqa: BLE001
        pass


_STATE: dict = _load_state()


def _fail_count(sid: str) -> int:
    with _LOCK:
        return int((_STATE.get(sid) or {}).get("fails", 0))


def _set_fail(sid: str, count: int) -> None:
    with _LOCK:
        entry = dict(_STATE.get(sid) or {})
        entry["fails"] = count
        entry["last_probe"] = time.time()
        _STATE[sid] = entry
        _save_state(_STATE)


def probe_state() -> dict:
    with _LOCK:
        return dict(_STATE)


async def _probe_cms(source: dict) -> bool:
    import providers
    try:
        r = await asyncio.wait_for(
            providers.probe_source_alive(source, timeout=8.0), timeout=12)
        return bool(r.get("alive"))
    except Exception:  # noqa: BLE001
        return False


async def _probe_live(source: dict) -> bool:
    """直播源：探测 m3u8 地址可达且返回有效的 m3u8 文本。"""
    import subfetch
    url = str(source.get("api") or source.get("url") or "").strip()
    if not url:
        return False
    try:
        blob, _ = subfetch.fetch_first([url], timeout=12.0)
        if not blob:
            return False
        text = blob.decode("utf-8", "ignore")
        return ("#EXTM3U" in text) or ("#EXT-X-" in text)
    except Exception:  # noqa: BLE001
        return False


async def probe_one(source: dict) -> bool:
    """探测单个源是否可用（直播源走 m3u8 探测，其余走 CMS 关键词探测）。"""
    if source.get("live") or source.get("kind") == "live":
        return await _probe_live(source)
    return await _probe_cms(source)


async def scan_once() -> dict:
    """体检一轮：遍历所有源（含停用），处理连续失效。返回汇总。"""
    cfg = _config.config if _config else None
    if cfg is None:
        return {"checked": 0, "disabled": [], "removed": [], "revived": []}

    if not bool(cfg.get().get("source_health_enabled", True)):
        return {"checked": 0, "disabled": [], "removed": [], "revived": []}

    sources = cfg.all_sources()
    # 只体检「可探测」的源：CMS 与直播；跳过加固/爬虫/DRPY（它们有各自的运行时健康）
    probes = [s for s in sources
              if s.get("api") and not s.get("guard") and not s.get("spider_src")
              and not s.get("drpy")]

    disabled: list[str] = []
    removed: list[str] = []
    revived: list[str] = []
    checked = 0

    # 分批并发探测，避免一次打爆
    sem = asyncio.Semaphore(8)

    async def _do(s: dict):
        nonlocal checked
        async with sem:
            ok = await probe_one(s)
        checked += 1
        sid = s.get("id", "")
        builtin = bool(s.get("builtin"))
        sys_disabled = bool(s.get("sys_disabled"))

        if ok:
            # 复活：清计数；系统停用的自动启用
            if _fail_count(sid):
                _set_fail(sid, 0)
            if sys_disabled and not s.get("enabled"):
                cfg.sys_enable_source(sid)
                revived.append(s.get("name", sid))
            return

        # 失效：计数 +1
        cnt = _fail_count(sid) + 1
        _set_fail(sid, cnt)
        if cnt < FAIL_THRESHOLD:
            return
        # 达到阈值：内置停用，其余删除
        if builtin:
            if not s.get("enabled") and not sys_disabled:
                # 用户手动停用的内置源，不强制系统停用
                return
            cfg.sys_disable_source(sid)
            disabled.append(s.get("name", sid))
        else:
            cfg.sys_remove_source(sid)
            removed.append(s.get("name", sid))

    await asyncio.gather(*[_do(s) for s in probes])
    return {"checked": checked, "disabled": disabled, "removed": removed,
            "revived": revived}


async def periodic_loop(stop_event: asyncio.Event) -> None:
    """后台体检循环：启动延迟首检，之后每 PERIOD 秒一轮。"""
    try:
        await asyncio.wait_for(asyncio.shield(stop_event.wait()),
                               timeout=BOOT_DELAY)
        return
    except asyncio.TimeoutError:
        pass
    while not stop_event.is_set():
        try:
            await scan_once()
        except Exception:  # noqa: BLE001
            pass
        try:
            await asyncio.wait_for(asyncio.shield(stop_event.wait()),
                                   timeout=PERIOD)
        except asyncio.TimeoutError:
            continue
