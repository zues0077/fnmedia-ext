#!/usr/bin/env python3
"""定期巡检：主动探测所有资源站，按「多轮 + 跨时间 + 错误分类」判失效。

设计（2026-11-12 第 3 批重做，依据实机实测）：

1. **探测接口用最轻的 ac=list&pg=1**，不用「5 个热门词串行搜索」。
   实测 30 源：5 词搜索 78 秒 / 19 个可用；ac=list 48 秒 / 22 个可用。
   搜索路径还会把「禁止关键词搜索」的源（code=1002）直接判死，而它其实活着。

2. **单次结果不作为判死依据**。LunaTV 实机同一批 79 源间隔 3 秒跑两次，
   8 个源状态翻转（10%），4 个在 valid/invalid 之间来回跳。所以要求：
   连续失败 ≥ N 次 **且** 首次失败距今 ≥ M 小时 **且** 最近一次仍是失败。

3. **只有连接级失败计数**。超时/连接重置/DNS/TLS/5xx/404 才算；HTTP 403、
   返回非 JSON（接口改版）、code≠1（禁搜/受限）都不计，避免误杀。

4. **成功一次只减 1，不清零**。旧实现清零会让成功率 40% 的半死源永远判不死，
   长期霸占搜索前排却经常给不出结果。

5. **停用 ≠ 删除，但停用满 retention 天后自动删除**（内置源同样允许，先写墓碑，
   可在「已移除」里单条恢复）。删除原因写进巡检日志。

6. **停用源按更短周期复检**（默认 1 小时），复活即自动启用——停用是冷藏不是封存。

7. 状态落 state/source_probe.json，巡检明细落 state/patrol_log.jsonl（可查原因）。
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path

try:
    import config as _config
except Exception:  # pragma: no cover
    _config = None

# 兜底默认值：真实值每次循环从 config.patrol_settings() 读取（改完立即生效）。
DEFAULT_INTERVAL_H = 6
DEFAULT_DISABLED_INTERVAL_H = 1
DEFAULT_RETAIN_DAYS = 30
FAIL_THRESHOLD = 3
MIN_SPAN_H = 12
BOOT_DELAY = 60
TICK = 60
LOG_KEEP = 5000

_STATE_DIR = Path(__file__).resolve().parent.parent / "state"
_STATE_PATH = _STATE_DIR / "source_probe.json"
_LOG_PATH = _STATE_DIR / "patrol_log.jsonl"
_LOCK = threading.RLock()


def _load_state() -> dict:
    try:
        j = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
        return j if isinstance(j, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _save_state() -> None:
    try:
        _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _STATE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(_STATE, ensure_ascii=False), encoding="utf-8")
        tmp.replace(_STATE_PATH)
    except Exception:  # noqa: BLE001
        pass


_STATE: dict = _load_state()
# 巡检互斥：启动首检（BOOT_DELAY 后）与用户手动「立即巡检一轮」可能撞车，
# 两轮并发会把同一批源打两遍、日志重复、延迟被自己干扰。
_RUNNING = False


def _entry(sid: str) -> dict:
    with _LOCK:
        e = _STATE.get(sid)
        if not isinstance(e, dict):
            e = {}
            _STATE[sid] = e
        return e


def _settings() -> dict:
    try:
        return _config.config.patrol_settings()
    except Exception:  # noqa: BLE001
        return {"enabled": True, "interval_h": DEFAULT_INTERVAL_H,
                "disabled_interval_h": DEFAULT_DISABLED_INTERVAL_H,
                "retain_days": DEFAULT_RETAIN_DAYS, "timeout_s": 25.0,
                "concurrency": 4, "fail_threshold": FAIL_THRESHOLD,
                "min_span_h": MIN_SPAN_H, "top_n": 40}


def ok_rate(sid: str, days: float = 7.0) -> float | None:
    """近 N 天巡检成功率；样本不足返回 None。"""
    e = _STATE.get(sid) or {}
    recent = e.get("recent") or []
    if not recent:
        return None
    since = time.time() - days * 86400
    vals = [v for x in recent if isinstance(x, list) and len(x) >= 2
            and isinstance(x[0], (int, float)) and x[0] >= since
            for v in (x[1],)]
    if not vals:
        return None
    return sum(1 for v in vals if v) / len(vals)


def _push_recent(e: dict, ok: bool) -> None:
    recent = e.get("recent")
    if not isinstance(recent, list):
        recent = []
    recent.append([time.time(), 1 if ok else 0])
    cutoff = time.time() - 8 * 86400
    recent = [x for x in recent if isinstance(x, list) and x and x[0] >= cutoff][-200:]
    e["recent"] = recent


def probe_state() -> dict:
    with _LOCK:
        return {k: dict(v) for k, v in _STATE.items() if isinstance(v, dict)}


def _log_rows(rows: list[dict]) -> None:
    if not rows:
        return
    try:
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _LOG_PATH.open("a", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        return
    try:
        if _LOG_PATH.stat().st_size > 2 * 1024 * 1024:
            lines = _LOG_PATH.read_text(encoding="utf-8").splitlines()[-LOG_KEEP:]
            _LOG_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def read_log(limit: int = 200) -> list[dict]:
    """读最近 limit 条巡检日志（新的在前）。"""
    try:
        lines = _LOG_PATH.read_text(encoding="utf-8").splitlines()[-limit:]
    except Exception:  # noqa: BLE001
        return []
    out = []
    for line in reversed(lines):
        try:
            out.append(json.loads(line))
        except Exception:  # noqa: BLE001
            continue
    return out


async def _probe_live(source: dict, timeout: float) -> dict:
    """直播源：拉 m3u8 文本判可达。"""
    import subfetch
    url = str(source.get("api") or source.get("url") or "").strip()
    t0 = time.monotonic()
    if not url:
        return {"state": "invalid", "ok": False, "lat_ms": 0,
                "list_count": 0, "hit": 0, "error": "地址为空"}
    try:
        blob, _ = subfetch.fetch_first([url], timeout=timeout)
        lat = int((time.monotonic() - t0) * 1000)
        if not blob:
            return {"state": "fail", "ok": False, "lat_ms": lat,
                    "list_count": 0, "hit": 0, "error": "拉取失败"}
        text = blob.decode("utf-8", "ignore")
        alive = ("#EXTM3U" in text) or ("#EXT-X-" in text)
        return {"state": "ok" if alive else "unsupported", "ok": alive,
                "lat_ms": lat, "list_count": 0, "hit": 0,
                "error": "" if alive else "非 m3u8 内容"}
    except Exception as exc:  # noqa: BLE001
        return {"state": "fail", "ok": False,
                "lat_ms": int((time.monotonic() - t0) * 1000),
                "list_count": 0, "hit": 0, "error": type(exc).__name__}


async def _probe_t4(source: dict, timeout: float) -> dict:
    """T4（真机）源：走 t4.probe，结果只有成功/失败两态。"""
    import providers
    t0 = time.monotonic()
    try:
        r = await providers.probe_source(source.get("api") or "", timeout=timeout,
                                         source=source)
        ok = bool(r.get("ok"))
        return {"state": "ok" if ok else "fail", "ok": ok,
                "lat_ms": int(r.get("elapsed_ms") or (time.monotonic() - t0) * 1000),
                "list_count": int(r.get("count") or 0), "hit": 1 if ok else 0,
                "error": "" if ok else str(r.get("error") or "")}
    except Exception as exc:  # noqa: BLE001
        return {"state": "fail", "ok": False,
                "lat_ms": int((time.monotonic() - t0) * 1000),
                "list_count": 0, "hit": 0, "error": type(exc).__name__}


def _pick_keyword(round_no: int) -> str:
    """每轮轮换一个热门词，避免长期只用同一部片判断「有没有片源」。"""
    try:
        from providers import PROBE_HOT_KEYWORDS
        kws = list(PROBE_HOT_KEYWORDS) or ["庆余年"]
    except Exception:  # noqa: BLE001
        kws = ["庆余年"]
    return kws[round_no % len(kws)]


async def _probe(source: dict, timeout: float, keyword: str) -> dict:
    if source.get("live") or source.get("kind") == "live":
        return await _probe_live(source, timeout)
    if source.get("kind") == "t4":
        return await _probe_t4(source, timeout)
    import providers
    # 按真实类型分派：drpy/spider/catvod/dduo 各有自己的轻量探测口，
    # 不能再一律打苹果 CMS 的 ac=list（会把 catvod:// 判成「地址非法」误杀）。
    return await providers.probe_any_source(
        source, keyword=keyword, timeout=timeout)


def _label(res: dict) -> str:
    return {"ok": "正常", "limited": "限搜", "unsupported": "接口异常",
            "blocked": "需验证", "fail": "失败", "invalid": "地址非法"}.get(
                res.get("state") or "", "未知")


async def _scan_group(sources: list[dict], settings: dict, *, group: str,
                      round_no: int) -> dict:
    """探测一组源并落判定。group: active（启用）/ disabled（系统停用）。"""
    cfg = _config.config
    keyword = _pick_keyword(round_no)
    timeout = float(settings["timeout_s"])
    threshold = int(settings["fail_threshold"])
    min_span = float(settings["min_span_h"]) * 3600
    sem = asyncio.Semaphore(int(settings["concurrency"]))

    disabled_now: list[str] = []
    revived: list[str] = []
    deleted: list[str] = []
    quality_patch: dict = {}
    log_rows: list[dict] = []
    checked = 0

    async def _do(s: dict):
        nonlocal checked
        sid = str(s.get("id") or "")
        if not sid:
            return
        async with sem:
            try:
                res = await asyncio.wait_for(_probe(s, timeout, keyword),
                                             timeout=timeout + 5)
            except asyncio.TimeoutError:
                res = {"state": "fail", "ok": False, "lat_ms": int(timeout * 1000),
                       "list_count": 0, "hit": 0, "error": "外层超时"}
            except Exception as exc:  # noqa: BLE001
                res = {"state": "fail", "ok": False, "lat_ms": 0,
                       "list_count": 0, "hit": 0, "error": type(exc).__name__}
        checked += 1

        now = time.time()
        state = res.get("state") or "fail"
        e = _entry(sid)
        e["last_probe"] = now
        e["last_state"] = state
        e["last_error"] = res.get("error") or ""
        e["lat_ms"] = int(res.get("lat_ms") or 0)
        e["list_count"] = int(res.get("list_count") or 0)
        e["hit"] = int(res.get("hit") or 0)
        e["direct"] = int(res.get("direct") or 0)
        # 延迟用最近 5 次成功探测的中位数：单次抖动太大（实测同一源 1s/10.9s），
        # 直接拿最后一次排序会让源每次巡检后忽前忽后。
        hist = e.get("lat_hist")
        if not isinstance(hist, list):
            hist = []
        if e["lat_ms"] > 0 and state in ("ok", "limited"):
            hist = (hist + [e["lat_ms"]])[-5:]
        e["lat_hist"] = hist
        lat_med = 0
        if hist:
            srt = sorted(hist)
            lat_med = srt[len(srt) // 2]

        countable = state in ("fail", "invalid")   # 只有这两类计失败
        if countable:
            e["fails"] = int(e.get("fails") or 0) + 1
            if not e.get("first_fail_ts"):
                e["first_fail_ts"] = now
        else:
            # 成功（或「活着但受限」）：失败计数只减 1，不清零
            e["fails"] = max(0, int(e.get("fails") or 0) - 1)
            if e["fails"] == 0:
                e["first_fail_ts"] = None
            if state == "ok":
                e["last_ok"] = now
        _push_recent(e, state in ("ok", "limited"))
        fails = int(e.get("fails") or 0)

        action = "none"
        note = ""

        # 复活：系统停用的源探测到「活着」就自动启用
        if (state in ("ok", "limited") and not s.get("enabled")
                and s.get("sys_disabled")):
            cfg.sys_enable_source(sid)
            revived.append(s.get("name") or sid)
            action = "enable"
            note = "探测恢复，自动启用"

        # 判死：三类条件同时满足才停用
        elif countable and s.get("enabled"):
            span = now - float(e.get("first_fail_ts") or now)
            if state == "invalid":
                cfg.sys_disable_source(sid, f"地址非法（{res.get('error')}）")
                disabled_now.append(s.get("name") or sid)
                action = "disable"
                note = "地址非法，直接停用"
            elif fails >= threshold and span >= min_span:
                reason = (f"连续失败 {fails} 次且跨 {span / 3600:.1f} 小时，"
                          f"最后一次：{res.get('error') or _label(res)}")
                cfg.sys_disable_source(sid, reason)
                disabled_now.append(s.get("name") or sid)
                action = "disable"
                note = reason

        # 命中用最近 5 轮累计（每轮换一个热门词）。单轮只看一个词，某站恰好
        # 没收这部片就被判「没片」太武断；累计 5 轮能反映真实片源覆盖度。
        hh = e.get("hit_hist")
        if not isinstance(hh, list):
            hh = []
        hh = (hh + [e["hit"]])[-5:]
        e["hit_hist"] = hh
        hit_sum = sum(1 for x in hh if x)

        quality_patch[sid] = {
            "quality": {
                "state": state,
                "lat_ms": lat_med or e["lat_ms"],
                "lat_last": e["lat_ms"],
                "hits": hit_sum,
                "hit": e["hit"],
                "direct": e["direct"],
                "list_count": e["list_count"],
                "ok_rate": ok_rate(sid),
                "fails": fails,
                "checked_at": now,
                "error": e["last_error"],
            },
            "fail_count": fails,
            "last_check": now,
            "last_ok": e.get("last_ok"),
            "verified": True if state == "ok" else (
                False if countable else s.get("verified")),
        }
        log_rows.append({
            "ts": int(now), "group": group, "id": sid,
            "name": s.get("name") or sid,
            "state": state, "label": _label(res), "ok": state in ("ok", "limited"),
            "ms": e["lat_ms"], "list_count": e["list_count"], "hit": e["hit"],
            "error": e["last_error"], "fails": fails,
            "first_fail_h": round((now - float(e.get("first_fail_ts") or now)) / 3600, 1)
            if e.get("first_fail_ts") else 0,
            "action": action, "note": note, "keyword": keyword,
        })

    await asyncio.gather(*[_do(s) for s in sources])

    # 批量写回质量（只落盘一次，避免逐源写配置风暴）
    try:
        cfg.apply_quality_batch(quality_patch)
    except Exception:  # noqa: BLE001
        pass

    # 停用满 retention 天 → 删除（内置源同样允许，先写墓碑）
    retain_days = int(settings["retain_days"])
    if retain_days > 0:
        now = time.time()
        for s in cfg.all_sources():
            if s.get("enabled") or not s.get("sys_disabled"):
                continue
            ts = s.get("disabled_at")
            if not ts:
                # 老数据没有 disabled_at：补一次时间戳，从今天开始计时
                try:
                    cfg.sys_disable_source(str(s.get("id") or ""),
                                           s.get("disabled_reason") or "历史停用")
                except Exception:  # noqa: BLE001
                    pass
                continue
            if now - float(ts) < retain_days * 86400:
                continue
            sid = str(s.get("id") or "")
            reason = (f"停用满 {retain_days} 天自动删除（停用原因："
                      f"{s.get('disabled_reason') or '未知'}）")
            ok, _ = cfg.sys_remove_source(sid, reason)
            if ok:
                deleted.append(s.get("name") or sid)
                with _LOCK:
                    _STATE.pop(sid, None)
                log_rows.append({
                    "ts": int(now), "group": "cleanup", "id": sid,
                    "name": s.get("name") or sid, "state": "deleted",
                    "label": "已删除", "ok": False, "ms": 0, "list_count": 0,
                    "hit": 0, "error": "", "fails": 0, "first_fail_h": 0,
                    "action": "delete", "note": reason, "keyword": "",
                })

    with _LOCK:
        _save_state()
    _log_rows(log_rows)
    return {"checked": checked, "disabled": disabled_now, "revived": revived,
            "deleted": deleted}


async def scan_once(*, force: bool = False) -> dict:
    """跑一轮巡检（防止并发：启动首检与手动触发撞上时，只跑一轮）。"""
    global _RUNNING
    with _LOCK:
        if _RUNNING:
            return {"checked": 0, "disabled": [], "revived": [], "deleted": [],
                    "skipped": "上一轮巡检仍在进行"}
        _RUNNING = True
    try:
        return await _scan_once_inner(force=force)
    finally:
        with _LOCK:
            _RUNNING = False


async def _scan_once_inner(*, force: bool = False) -> dict:
    """跑一轮巡检（默认按各自周期判断是否到点，force=True 立刻全测）。"""
    if _config is None:
        return {"checked": 0, "disabled": [], "revived": [], "deleted": [],
                "skipped": "no config"}
    cfg = _config.config
    settings = _settings()
    if not settings.get("enabled"):
        return {"checked": 0, "disabled": [], "revived": [], "deleted": [],
                "skipped": "巡检已关闭"}

    sources = cfg.all_sources()
    # 只巡检「可独立探测」的源：CMS/T4/直播/DRPY/爬虫/小雅/dduo 都有对应的轻量
    # 探测口（见 providers.probe_any_source）。加固源（redroid 真机）有各自的
    # 运行时健康与专题页，仍然排除，避免把设备占用当源质量。
    probes = [s for s in sources if s.get("api") and not s.get("guard")]
    active = [s for s in probes if s.get("enabled")]
    relay = [s for s in probes if not s.get("enabled") and s.get("sys_disabled")]

    now = time.time()
    meta = _STATE.get("_meta") if isinstance(_STATE.get("_meta"), dict) else {}
    _STATE["_meta"] = meta
    round_no = int(meta.get("round") or 0) + 1
    meta["round"] = round_no

    due_active = force or (now - float(meta.get("active_run") or 0)
                           >= float(settings["interval_h"]) * 3600)
    due_relay = force or (now - float(meta.get("disabled_run") or 0)
                          >= float(settings["disabled_interval_h"]) * 3600)

    summary = {"checked": 0, "disabled": [], "revived": [], "deleted": [],
               "active_due": due_active, "disabled_due": due_relay,
               "sources": len(probes), "round": round_no}
    if due_active:
        r = await _scan_group(active, settings, group="active", round_no=round_no)
        meta["active_run"] = time.time()
        summary["checked"] += r["checked"]
        summary["disabled"] += r["disabled"]
        summary["revived"] += r["revived"]
        summary["deleted"] += r["deleted"]
    if due_relay and relay:
        r = await _scan_group(relay, settings, group="disabled", round_no=round_no)
        meta["disabled_run"] = time.time()
        summary["checked"] += r["checked"]
        summary["revived"] += r["revived"]
        summary["deleted"] += r["deleted"]
    elif due_relay:
        meta["disabled_run"] = time.time()
    meta["last_run"] = time.time()
    meta["last_summary"] = {k: summary[k] for k in
                            ("checked", "disabled", "revived", "deleted")}
    with _LOCK:
        _save_state()
    return summary


async def periodic_loop(stop_event: asyncio.Event) -> None:
    """后台巡检循环：启动延迟首检，之后每 60 秒检查一次是否到点。

    用「每分钟 tick + 各自周期判断」而不是「睡 6 小时」，是为了让
    patrol_interval_h / patrol_disabled_interval_h 改完立刻生效。
    """
    try:
        await asyncio.wait_for(asyncio.shield(stop_event.wait()), timeout=BOOT_DELAY)
        return
    except asyncio.TimeoutError:
        pass
    while not stop_event.is_set():
        try:
            await scan_once()
        except Exception:  # noqa: BLE001
            pass
        try:
            await asyncio.wait_for(asyncio.shield(stop_event.wait()), timeout=TICK)
        except asyncio.TimeoutError:
            continue
