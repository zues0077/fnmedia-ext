#!/usr/bin/env python3
"""GitHub 加速镜像池（A2，2026-11-12）。

为什么需要：
  国内 NAS 直连 raw.githubusercontent.com / github.com 常 NXDOMAIN 或被墙，
  拉订阅配置、spider jar、DRPY 脚本都会失败。加速镜像（URL 前缀重写，走国内
  CDN）是零成本的解法，比走翻墙代理更合适——代理由此只用于镜像也救不了的场景。

设计参照 lx-hunter（playbook P-0915），四条关键规则：
  1. 14 个种子候选，**探测通了才入池**（不通的不占位，避免每次试到死镜像）
  2. 入池后按**实测响应时间**升序排列，快的优先
  3. **连续失败 3 次自动出池**（enabled=False），临时抖动不会立即剔除
  4. 支持手动移除 / 启停（enabled=False 但仍保留记录，便于观察）

语义澄清：
  - **直连不在池内**。调用方永远先试原 URL，池只作兜底。
  - 池只对 GitHub 域名生效；非 GitHub 原样返回，不做任何改写。

此模块无专有逻辑，可进入公开构建。
"""
from __future__ import annotations

import time

import httpx

UA = "Mozilla/5.0 (Linux; Android 11) AppleWebKit/537.36"

# 连续失败多少次自动出池（对齐 lx-hunter）
FAIL_THRESHOLD = 3

# 探测用的固定小文件：GitHub 官方仓库里长期存在、体积约 1.5KB
PROBE_RAW = "https://raw.githubusercontent.com/github/gitignore/main/Python.gitignore"
PROBE_TIMEOUT = 10.0

# 14 个种子。kind 决定 URL 拼法：
#   prefix  → https://{host}/https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{rest}
#   raw     → https://{host}/{owner}/{repo}/{branch}/{rest}
#   jsdelivr→ https://{host}/gh/{owner}/{repo}@{branch}/{rest}
SEEDS: list[dict] = [
    {"host": "ghproxy.net", "kind": "prefix"},
    {"host": "gh-proxy.com", "kind": "prefix"},
    {"host": "gh-proxy.org", "kind": "prefix"},
    {"host": "ghfast.top", "kind": "prefix"},
    {"host": "ghps.cc", "kind": "prefix"},
    {"host": "gh.llkk.cc", "kind": "prefix"},
    {"host": "ghproxy.cc", "kind": "prefix"},
    {"host": "gh-proxy.net", "kind": "prefix"},
    {"host": "github.moeyy.xyz", "kind": "prefix"},
    {"host": "gh.api.99988866.xyz", "kind": "prefix"},
    {"host": "gh-proxy.pages.dev", "kind": "prefix"},
    {"host": "gh.acg.uy", "kind": "prefix"},
    {"host": "raw.gitmirror.com", "kind": "raw"},
    {"host": "cdn.jsdelivr.net", "kind": "jsdelivr"},
]

_SEED_BY_HOST = {s["host"]: s["kind"] for s in SEEDS}


def _cfg():
    try:
        import config
        return config.config
    except Exception:  # noqa: BLE001
        return None


def pool() -> list[dict]:
    """当前镜像池（按 ms 升序，仅 enabled）。池空时回退到内置种子顺序。"""
    c = _cfg()
    raw: list = []
    if c is not None:
        try:
            raw = c.get().get("github_mirror_pool") or []
        except Exception:  # noqa: BLE001
            raw = []
    if not raw:
        # 尚未探测过：用种子顺序兜底，保证首次可用（不阻塞）
        return [{"host": s["host"], "kind": s["kind"], "ms": 0,
                 "fails": 0, "enabled": True} for s in SEEDS]
    out = [dict(x) for x in raw if isinstance(x, dict) and x.get("enabled", True)]
    out.sort(key=lambda x: (x.get("ms") or 10 ** 9))
    return out


def full_pool() -> list[dict]:
    """完整池（含已停用），供管理页展示。池空时返回种子。"""
    c = _cfg()
    raw: list = []
    if c is not None:
        try:
            raw = c.get().get("github_mirror_pool") or []
        except Exception:  # noqa: BLE001
            raw = []
    if not raw:
        return [{"host": s["host"], "kind": s["kind"], "ms": 0,
                 "fails": 0, "enabled": True} for s in SEEDS]
    out = [dict(x) for x in raw if isinstance(x, dict)]
    out.sort(key=lambda x: (not x.get("enabled", True), x.get("ms") or 10 ** 9))
    return out


def _save(items: list[dict]) -> None:
    c = _cfg()
    if c is None:
        return
    try:
        c.save({"github_mirror_pool": items,
                "github_mirror_last_scan": int(time.time())})
    except Exception:  # noqa: BLE001
        pass


def build_url(kind: str, host: str, owner: str, repo: str,
              branch: str, rest: str) -> str:
    """按镜像类型拼出取文件的 URL。"""
    if kind == "raw":
        return f"https://{host}/{owner}/{repo}/{branch}/{rest}"
    if kind == "jsdelivr":
        return f"https://{host}/gh/{owner}/{repo}@{branch}/{rest}"
    raw = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{rest}"
    return f"https://{host}/{raw}"


def candidates(url: str, gh_hosts) -> list[str]:
    """GitHub 原始域名资源 → [原URL, 池内镜像（按速度）]；非 GitHub 只返回原 URL。

    直连（原 URL）永远排第一，池只作兜底。
    """
    out = [url]
    try:
        from urllib.parse import urlparse
        p = urlparse(url)
        host = (p.netloc or "").lower()
        parts = [seg for seg in p.path.split("/") if seg]
        if host in gh_hosts and len(parts) >= 4:
            owner, repo, branch = parts[0], parts[1], parts[2]
            rest = "/".join(parts[3:])
            for m in pool():
                u = build_url(m.get("kind") or "prefix", m["host"],
                              owner, repo, branch, rest)
                if u not in out:
                    out.append(u)
    except Exception:  # noqa: BLE001
        pass
    return out


def host_of(url: str) -> str:
    try:
        from urllib.parse import urlparse
        return (urlparse(url).netloc or "").lower()
    except Exception:  # noqa: BLE001
        return ""


def record_success(url: str) -> None:
    """命中成功：清零失败计数；若之前失败到临界值，顺手恢复启用。"""
    host = host_of(url)
    if host not in _SEED_BY_HOST:
        return
    items = full_pool()
    changed = False
    for it in items:
        if it.get("host") == host:
            if it.get("fails"):
                it["fails"] = 0
                changed = True
            if not it.get("enabled", True):
                it["enabled"] = True
                changed = True
    if changed:
        _save(items)


def record_failure(url: str) -> None:
    """失败一次：累计到 FAIL_THRESHOLD 就出池（enabled=False）。"""
    host = host_of(url)
    if host not in _SEED_BY_HOST:
        return
    items = full_pool()
    for it in items:
        if it.get("host") == host:
            n = int(it.get("fails") or 0) + 1
            it["fails"] = n
            if n >= FAIL_THRESHOLD:
                it["enabled"] = False
            _save(items)
            return


async def probe_one(host: str, kind: str, timeout: float = PROBE_TIMEOUT) -> tuple[bool, int]:
    """探测单个镜像：返回 (是否可用, 毫秒)。"""
    url = build_url(kind, host, "github", "gitignore", "main", "Python.gitignore")
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(trust_env=False, verify=False,
                                     follow_redirects=True,
                                     timeout=httpx.Timeout(timeout, connect=timeout)) as cli:
            r = await cli.get(url, headers={"User-Agent": UA})
            ms = int((time.monotonic() - t0) * 1000)
            # 通了但返回 4xx/5xx 不算可用（404 常意味着镜像只代理部分路径）
            if r.status_code == 200 and r.content:
                return True, ms
            return False, ms
    except Exception:  # noqa: BLE001
        return False, int((time.monotonic() - t0) * 1000)


async def discover(concurrency: int = 6, timeout: float = PROBE_TIMEOUT) -> dict:
    """并发探测全部种子，**只把通的放进池**，按 ms 升序。

    返回 {"ok": n, "dead": [...], "pool": [...]}。
    """
    import asyncio

    sem = asyncio.Semaphore(concurrency)

    async def one(seed):
        async with sem:
            ok, ms = await probe_one(seed["host"], seed["kind"], timeout)
            return seed, ok, ms

    results = await asyncio.gather(*[one(s) for s in SEEDS])
    alive, dead = [], []
    for seed, ok, ms in results:
        if ok:
            alive.append({"host": seed["host"], "kind": seed["kind"],
                          "ms": ms, "fails": 0, "enabled": True})
        else:
            dead.append(seed["host"])
    alive.sort(key=lambda x: x["ms"])
    _save(alive)
    return {"ok": len(alive), "dead": dead, "pool": alive}


def set_enabled(host: str, enabled: bool) -> bool:
    items = full_pool()
    hit = False
    for it in items:
        if it.get("host") == host:
            it["enabled"] = bool(enabled)
            if enabled:
                it["fails"] = 0
            hit = True
    if hit:
        _save(items)
    return hit


def remove(host: str) -> bool:
    items = [it for it in full_pool() if it.get("host") != host]
    if len(items) == len(full_pool()):
        return False
    _save(items)
    return True
