#!/usr/bin/env python3
"""订阅源定时拉取调度：多订阅 URL，各自按 update_interval_h 到期拉取，只增不减。

设计要点：
- 只增不减：拉取后仅把「库中尚不存在」的新源加入（复用 config.add_source 的去重），
  已存在的源不重复导入、不覆盖用户的启用/改名/改址。
- 每个订阅独立间隔（默认 24h），到点才拉；拉取失败记录 fail_count，不影响下次。
- 内置订阅（fantaiying / cat_lite）与 URL 订阅走同一套调度。
- 后台循环由 app.py 的 lifespan 注册；缓存刷新通过回调（_on_changed）注入，
  避免与 app 模块循环导入。

此模块无专有逻辑，可进入公开构建（未配置订阅时循环空转）。
"""
from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable

import httpx

try:
    import config as _config
except Exception:  # pragma: no cover
    _config = None

try:
    import features
except Exception:  # pragma: no cover
    features = None

# 后台循环：启动后延迟首检 + 每轮扫描间隔（秒）。
BOOT_DELAY = 30
SCAN_PERIOD = 300  # 每 5 分钟扫一次「是否有订阅到期」

# 单次订阅拉取的探测并发上限。
_PROBE_CONCURRENCY = 8

# 刷新回调（由 app 注入）：新增源后清搜索缓存 / 刷新聚合路由。
_on_changed: Callable[[], Awaitable[None]] | None = None


def set_change_hook(fn: Callable[[], Awaitable[None]] | None) -> None:
    """注入「源有新增」回调（app.lifespan 里注册）。"""
    global _on_changed
    _on_changed = fn


async def _notify_changed() -> None:
    if _on_changed is not None:
        try:
            await _on_changed()
        except Exception:  # noqa: BLE001
            pass


def _builtin_url(sub: dict) -> str:
    """内置订阅的 url 形如 builtin:fantaiying / builtin:cat_lite。"""
    url = str(sub.get("url") or "")
    if url.startswith("builtin:"):
        return url[len("builtin:"):].strip()
    return ""


async def _load_sites(sub: dict):
    """拉取订阅并返回 (raw_sites, spider, base, used_url)。失败抛异常。"""
    import tvbox as tvboxmod
    url = str(sub.get("url") or "")
    builtin = _builtin_url(sub)
    if builtin == "fantaiying":
        sites, spider, base, used = await tvboxmod.load_fantaiying()
        return sites, spider, base, used
    if builtin in ("cat_lite", "catlite", "mao_lite"):
        sites, spider, base, used = tvboxmod.load_cat_lite_sync()
        return sites, spider, base, used
    if not url or not url.startswith(("http://", "https://")):
        raise RuntimeError("订阅无有效地址")
    sites, _subs, spider, base, _rules = await tvboxmod.load_subscription_full(url=url)
    if not sites:
        raise RuntimeError("订阅解析结果为空")
    return sites, spider, base, url


async def refresh_subscription(sub_id: str) -> dict:
    """拉取单个订阅并「只增不减」导入。返回报告 dict。

    - 标准苹果 CMS：探测通过且库中不存在才加入（带 sub_id 归属）。
    - m3u8 直播源：交给 livesrc 导入。
    - 加固/爬虫/DRPY 源：完整构建才处理（复用 config 底表同步），公开构建跳过。
    """
    cfg = _config.config if _config else None
    if cfg is None:
        return {"code": 1, "msg": "config 未就绪", "sub_id": sub_id}
    subs = cfg.subscriptions()
    sub = next((s for s in subs if s.get("id") == sub_id), None)
    if not sub:
        return {"code": 1, "msg": "订阅不存在", "sub_id": sub_id}

    import tvbox as tvboxmod
    try:
        raw_sites, spider, base, _used = await _load_sites(sub)
    except Exception as exc:  # noqa: BLE001
        cfg.mark_subscription_update(sub_id, False, str(exc))
        return {"code": 1, "msg": f"拉取失败：{type(exc).__name__}",
                "sub_id": sub_id}

    groups = tvboxmod.classify_subscription(raw_sites, spider, base)
    cms_cands = groups.get("cms", [])
    live_items = groups.get("live", [])

    # ---------- 苹果 CMS：探测 + 只增不减 ----------
    import providers
    existing = {s.get("api", "").rstrip("/").lower()
                for s in cfg.all_sources()}
    added_names: list[str] = []
    dup = 0
    failed = 0
    sem = asyncio.Semaphore(_PROBE_CONCURRENCY)

    async def _probe(c):
        async with sem:
            try:
                pr = await asyncio.wait_for(
                    providers.probe_source(c["api"], timeout=8.0), timeout=10)
            except Exception:
                pr = {"ok": False}
            return c, pr

    checked = await asyncio.gather(*[_probe(c) for c in cms_cands[:80]])
    for c, pr in checked:
        key = c["api"].rstrip("/").lower()
        if key in existing:
            dup += 1
            continue
        if not pr.get("ok"):
            failed += 1
            continue
        ok, _sid = cfg.add_source(c["name"], c["api"], True, False,
                                  sub_id=sub_id)
        if ok:
            added_names.append(c["name"])
            existing.add(key)
        else:
            dup += 1
        if len(added_names) >= 60:
            break

    # ---------- m3u8 直播源 ----------
    live_report = None
    if live_items:
        try:
            live_report = cfg.import_live_sources(live_items, sub_id=sub_id)
        except Exception as exc:  # noqa: BLE001
            live_report = {"error": f"直播源写入失败：{type(exc).__name__}"}

    changed = bool(added_names) or (live_report and live_report.get("added"))
    if changed:
        await _notify_changed()

    cfg.mark_subscription_update(sub_id, True)
    return {
        "code": 0, "sub_id": sub_id,
        "scanned": len(raw_sites),
        "cms_added": len(added_names),
        "added_names": added_names,
        "cms_dup": dup,
        "cms_failed": failed,
        "live": live_report,
    }


async def scan_once() -> dict:
    """扫描一次：拉取所有到期的订阅，返回汇总。"""
    cfg = _config.config if _config else None
    if cfg is None:
        return {"ran": 0, "results": []}
    now = time.time()
    ran = 0
    results: list[dict] = []
    for sub in cfg.subscriptions():
        if not sub.get("enabled"):
            continue
        interval = int(sub.get("update_interval_h") or 24) * 3600
        last = float(sub.get("last_update") or 0)
        if last and (now - last) < interval:
            continue
        try:
            r = await refresh_subscription(sub["id"])
        except Exception as exc:  # noqa: BLE001
            r = {"code": 1, "msg": f"异常：{type(exc).__name__}", "sub_id": sub["id"]}
        results.append(r)
        ran += 1
    return {"ran": ran, "results": results}


async def periodic_loop(stop_event: asyncio.Event) -> None:
    """后台循环：启动延迟首检，之后每 SCAN_PERIOD 秒扫描一次到期订阅。"""
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
                                   timeout=SCAN_PERIOD)
        except asyncio.TimeoutError:
            continue
