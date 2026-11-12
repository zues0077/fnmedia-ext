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


# >>> PRIVATE:DRPY
def _drpy_module():
    """延迟取 drpy 模块：公开构建剥离本块后取不到，按「无 JS 运行时」跳过。"""
    import drpy
    return drpy
# <<< PRIVATE:DRPY

# >>> PRIVATE:SPIDER
def _spider_module():
    """延迟取 spiders 模块：公开构建剥离本块后取不到，csp 视为不可导入。"""
    import spiders
    return spiders
# <<< PRIVATE:SPIDER

# 订阅解析缓存：只服务「预览勾选 → 用户确认 → 正式导入」这一对调用，
# 避免同一个订阅在几秒内被下载/解密两遍。正式刷新（未预览）从不读缓存，
# 保证用户点「立即更新」拿到的永远是站上最新的一份。
_PARSE_TTL = 600.0
_PARSE_CACHE: dict[str, tuple[float, tuple]] = {}


def _cache_key(sub: dict) -> str:
    return "%s|%s|%s" % (sub.get("id") or "", sub.get("url") or "",
                         sub.get("builtin") or "")


def _cache_put(sub: dict, val: tuple) -> None:
    now = time.time()
    for k in [k for k, v in _PARSE_CACHE.items() if now - v[0] >= _PARSE_TTL]:
        _PARSE_CACHE.pop(k, None)
    _PARSE_CACHE[_cache_key(sub)] = (now, val)
    while len(_PARSE_CACHE) > 4:
        _PARSE_CACHE.pop(min(_PARSE_CACHE, key=lambda k: _PARSE_CACHE[k][0]), None)


def _cache_take(sub: dict):
    hit = _PARSE_CACHE.pop(_cache_key(sub), None)
    if hit and (time.time() - hit[0]) < _PARSE_TTL:
        return hit[1]
    return None


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
        # 2026-11-13：同一订阅会偶发「解析结果为空」（镜像/网络抖动，实测
        # hafrey1 第一次 0 条、隔几秒第二次 69 条）。重试一次再判失败。
        await asyncio.sleep(1.5)
        sites, _subs, spider, base, _rules = await tvboxmod.load_subscription_full(url=url)
    if not sites:
        raise RuntimeError("订阅解析结果为空（已重试 1 次）")
    return sites, spider, base, url


def _preview_report(sub: dict, raw_sites: list, groups: dict) -> dict:
    """预览报告：只把「这个订阅里有什么」列出来，一个字节都不写。

    主要给 csp 勾选清单用（一个订阅能带几十上百个 csp 源，全量导入要下 jar、
    每个源占真机运行时）。cms/drpy/t4/live 只报数量，用户不需要挑。
    """
    csp_items = list(groups.get("csp") or [])
    loader = globals().get("_spider_module")
    rows: list[dict] = []
    skipped_build = loader is None
    if loader is not None:
        try:
            rows = loader().candidates(csp_items)
        except Exception as exc:  # noqa: BLE001
            return {"code": 1, "sub_id": sub.get("id"),
                    "msg": f"生成爬虫清单失败：{type(exc).__name__}"}
    return {
        "code": 0, "sub_id": sub.get("id"), "preview": True,
        "scanned": len(raw_sites),
        "counts": {"cms": len(groups.get("cms") or []),
                   "csp": len(csp_items),
                   "drpy": len(groups.get("drpy") or []),
                   "t4": len(groups.get("t4") or []),
                   "live": len(groups.get("live") or []),
                   "guard": len(groups.get("guard") or [])},
        "csp": rows,
        "csp_skipped_build": skipped_build,
        "csp_keys": sub.get("csp_keys"),
    }


async def refresh_subscription(sub_id: str, *, preview: bool = False,
                               csp_keys: list[str] | None = None) -> dict:
    """拉取单个订阅并「只增不减」导入。返回报告 dict。

    - 标准苹果 CMS：探测通过且库中不存在才加入（带 sub_id 归属）。
    - m3u8 直播源：交给 livesrc 导入。
    - 加固/爬虫/DRPY 源：完整构建才处理（复用 config 底表同步），公开构建跳过。
    - preview=True：只解析并返回候选清单（含 csp 每项的 风险/缺 jar/已在库），
      不写任何配置，也不更新 last_update。
    - csp_keys：本次要导入的 csp key。None=用订阅里存下的勾选（订阅也没存过
      就一个都不导）；[]=明确一个都不导。
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
        cached = _cache_take(sub) if not preview else None
        if cached is not None:
            raw_sites, spider, base, _used = cached
        else:
            raw_sites, spider, base, _used = await _load_sites(sub)
            if preview:
                _cache_put(sub, (raw_sites, spider, base, _used))
    except Exception as exc:  # noqa: BLE001
        if preview:
            return {"code": 1, "sub_id": sub_id,
                    "msg": f"拉取/解析订阅失败：{type(exc).__name__}"}
        cfg.mark_subscription_update(sub_id, False, str(exc))
        return {"code": 1, "msg": f"拉取失败：{type(exc).__name__}",
                "sub_id": sub_id}

    groups = tvboxmod.classify_subscription(raw_sites, spider, base)
    if preview:
        return _preview_report(sub, raw_sites, groups)

    cms_cands = groups.get("cms", [])
    live_items = groups.get("live", [])
    # 2026-11-12：此前只消费 cms/live，订阅里的 drpy 与 t4 全被静默丢弃
    # （实测 cluntop 87 源丢 1 drpy、niubihu1 135 源丢 5 drpy + 1 t4）。
    drpy_items = groups.get("drpy", [])
    t4_items = groups.get("t4", [])
    csp_items = groups.get("csp", [])

    # ---------- 苹果 CMS：探测 + 只增不减 ----------
    existing = {s.get("api", "").rstrip("/").lower()
                for s in cfg.all_sources()}
    added_names: list[str] = []
    dup = 0

    # A1（2026-11-12）：订阅导入【不再同步探测】。
    # 原实现用 8 秒超时逐源探测，会把 10~15 秒才响应的正常源误判为失效直接丢弃
    # （实测 niubihu1 的 21 个 CMS 只进 8 个，其中「❤百度」实际 5.4~13 秒可用却
    # 被 TimeoutError 杀掉）。现在全部入库并标 verified=False，由定时巡检
    # （/source/health-check，25 秒超时 + 多热门词）确认后更新状态；
    # 真正失效的源由巡检自动禁用，不再拖慢导入、不再误杀。
    standalone = bool(sub.get("standalone"))
    adult_auto: list[str] = []
    for c in cms_cands:
        key = c["api"].rstrip("/").lower()
        if key in existing:
            dup += 1
            continue
        ok, sid = cfg.add_source(c["name"], c["api"], True, standalone,
                                 sub_id=sub_id, verified=False)
        if ok:
            added_names.append(c["name"])
            existing.add(key)
            # 成人站自动打 18（2026-11-13）：进「🔞福利」库，不混进普通搜索。
            # 保守判定，导入后用户可随时取消。
            try:
                import catalog as _catalog
                if _catalog.looks_adult_source(c["name"]) and cfg.set_adult(sid, True)[0]:
                    adult_auto.append(c["name"])
            except Exception:  # noqa: BLE001
                pass
        else:
            dup += 1
        if len(added_names) >= 200:      # 上限从 60 放宽到 200
            break

    # ---------- m3u8 直播源 ----------
    live_report = None
    if live_items:
        try:
            live_report = cfg.import_live_sources(live_items, sub_id=sub_id)
        except Exception as exc:  # noqa: BLE001
            live_report = {"error": f"直播源写入失败：{type(exc).__name__}"}

    # ---------- T4 远程源（服务器端解析，可搜可播） ----------
    t4_report = None
    if t4_items:
        try:
            t4_report = cfg.import_t4_sources(t4_items)
        except Exception as exc:  # noqa: BLE001
            t4_report = {"error": f"T4 源写入失败：{type(exc).__name__}"}

    # ---------- 远程 drpyS（Node sidecar 跑规则脚本，仅完整构建） ----------
    drpy_report = None
    if drpy_items:
        loader = globals().get("_drpy_module")
        if loader is None:
            drpy_report = {"skipped_build": True}
        else:
            try:
                res = loader().import_remote(drpy_items, "smart")
                drpy_report = {"added": res.get("added", 0),
                               "updated": res.get("updated", 0)}
                cfg.load()          # 让新导入的源立刻出现在 all_sources()
            except Exception as exc:  # noqa: BLE001
                drpy_report = {"error": f"DRPY 写入失败：{type(exc).__name__}"}

    # ---------- csp 爬虫：只导入用户在订阅里勾选过的 ----------
    # 以前是「全导或全不导」的二选一，实际一个订阅能带 78~95 个 csp 源，
    # 全导会下满 jar 并把真机运行时打死；现在按订阅记录里的 csp_keys 精确导入。
    csp_report = None
    csp_pending = len(csp_items)
    if csp_items:
        loader = globals().get("_spider_module")
        if loader is None:
            csp_report = {"skipped_build": True, "pending": csp_pending}
        else:
            keys = csp_keys if csp_keys is not None else sub.get("csp_keys")
            if keys is None:
                csp_report = {"pending": csp_pending, "chosen": 0,
                              "hint": "尚未勾选 csp 源（在订阅卡片里点「选择 csp 源」）"}
            else:
                want = {str(k) for k in keys if str(k).strip()}
                chosen = [c for c in csp_items if str(c.get("key")) in want]
                if not chosen:
                    csp_report = {"pending": csp_pending, "chosen": 0}
                else:
                    try:
                        res = loader().import_sites(chosen, "smart")
                        csp_report = {"pending": csp_pending, "chosen": len(chosen),
                                      "added": res.get("added", 0),
                                      "updated": res.get("updated", 0),
                                      "items": res.get("items", [])}
                        cfg.load()      # 新导入的源立刻进 all_sources()
                    except Exception as exc:  # noqa: BLE001
                        csp_report = {"error": f"爬虫写入失败：{type(exc).__name__}"}

    changed = (bool(added_names)
               or (live_report and live_report.get("added"))
               or (t4_report and t4_report.get("added"))
               or (drpy_report and drpy_report.get("added"))
               or (csp_report and csp_report.get("added")))
    if changed:
        await _notify_changed()

    cfg.mark_subscription_update(sub_id, True)
    return {
        "code": 0, "sub_id": sub_id,
        "scanned": len(raw_sites),
        "cms_added": len(added_names),
        "added_names": added_names,
        "adult_auto": adult_auto,
        "cms_dup": dup,
        # A1：导入不再探测，全部入库待巡检（cms_added 即待验证数）
        "cms_pending": len(added_names),
        "cms_failed": 0,
        "live": live_report,
        "t4": t4_report,
        "drpy": drpy_report,
        # csp 爬虫按用户勾选导入；pending 是订阅里的候选总数
        "csp": csp_report,
        "csp_pending": csp_pending,
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
