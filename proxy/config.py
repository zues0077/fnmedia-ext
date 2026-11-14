#!/usr/bin/env python3
"""fnmedia-ext 运行配置（可热加载）。state/config.json。"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from pathlib import Path

import features
from providers import DEFAULT_SOURCES

try:
    from fnlog import get_logger
except Exception:  # 独立运行 / 旧打包缺少 fnlog 时退回标准 logging
    import logging as _logging

    def get_logger(name):  # type: ignore
        return _logging.getLogger(f"fnmedia.{name}")

_log = get_logger("config")

_STATE = Path(__file__).resolve().parent.parent / "state" / "config.json"
_LOCK = threading.RLock()

# 「参数未提供」哨兵：区分「没传这个字段」（保持原值）与「显式传了空值」（清空）。
_UNSET = object()

# 配置版本号：升级时按版本做迁移（见 Config.load）。
# v2：多用户可见性默认改 fail-closed（acl_default_visible/acl_default_online
# 由 True 改 False；旧配置首次加载时一次性翻成 False，见 load() 的 v1→v2 迁移）。
# v3：多用户可见性默认改回 fail-open（两项由 False 改回 True；v2 存量配置
# 首次加载时一次性翻回 True，见 load() 的 v2→v3 迁移——v1 配置走完整链
# v1→v2→v3，最终仍为 True）。
CONFIG_VERSION = 3
# 每次保存前保留的旧配置备份份数。
BACKUP_KEEP = 5

# 慢线起播预算（秒）：每次读取时夹取 [0.8, 2.8]，非法值回落默认值。
MAIN_READY_BUDGET_DEFAULT = 2.4
MAIN_READY_BUDGET_MIN = 0.8
MAIN_READY_BUDGET_MAX = 2.8


def clamp_main_ready_budget(raw) -> float:
    """把任意输入归一化为慢线起播预算：非法/非有限值回落 2.4，否则夹取 [0.8, 2.8]。"""
    import math

    try:
        v = float(raw)
    except (TypeError, ValueError):
        return MAIN_READY_BUDGET_DEFAULT
    if not math.isfinite(v):
        return MAIN_READY_BUDGET_DEFAULT
    return min(max(v, MAIN_READY_BUDGET_MIN), MAIN_READY_BUDGET_MAX)


# 源真实类型（唯一判定口径）。管理页标签、巡检探测、独立库、排序保底都走这里，
# 不要再各写一套 flags 判断：2026-11-12 管理页用 `FEAT_DDUO && src.xsrc` 一刀切，
# 把 246 上 102 个非 WASM 源（8 drpy + 92 spider + 1 网盘 + dduo）全标成了 WASM。
SOURCE_TYPES = ("cms", "dduo", "catvod", "drpy", "spider", "guard", "t4", "live")


def source_type(s: dict) -> str:
    """按源自身标记判定真实类型，顺序即优先级（一个源只可能属于一类）。"""
    if not isinstance(s, dict):
        return "cms"
    if s.get("kind") == "t4" or s.get("t4"):
        return "t4"
    if s.get("kind") == "live" or s.get("live"):
        return "live"
    if s.get("drpy"):
        return "drpy"
    if s.get("spider_src"):
        return "spider"
    if s.get("guard"):
        return "guard"
    if s.get("catvod"):
        return "catvod"
    if s.get("xsrc"):
        return "dduo"
    return "cms"


# 排序截断时每种类型的保底名额。全局搜索只取前 N 个源（默认 40），纯按质量
# 排序会让 92 个 spider + 8 个 drpy 全部落在 40 名之后、永远没机会出结果
# （2026-11-12 实测 167 启用源 head(40) 全是 cms）。保底只保证「有机会被搜到」，
# 名额之外的排序仍是原字典序，不引入权重。
TYPE_RESERVE = 3


def pick_head(ranked: list[dict], top_n) -> tuple[list[dict], list[dict]]:
    """按类型保底截断排序结果，返回 (head, tail)。

    top_n <= 0 或大于等于全部源数时不截断。非 cms 类型各保底 TYPE_RESERVE 个
    （该类型不足则全取），剩余名额按原排序补满；head 内部顺序与原排序一致，
    所以「置顶/屏蔽/状态/延迟」的既有优先级完全不变。
    """
    try:
        n = int(top_n)
    except (TypeError, ValueError):
        n = 0
    if n <= 0 or n >= len(ranked):
        return list(ranked), []
    order = {id(s): i for i, s in enumerate(ranked)}

    reserved: list[dict] = []
    for t in SOURCE_TYPES:
        if t == "cms":
            continue
        group = [s for s in ranked if source_type(s) == t]
        reserved.extend(group[:TYPE_RESERVE])
    if len(reserved) > n:
        # 类型过多（少见）：保底名额本身超预算时，仍按原排序取前 n 个
        reserved.sort(key=lambda s: order[id(s)])
        reserved = reserved[:n]
    picked = {id(s) for s in reserved}
    head = list(reserved)
    for s in ranked:
        if len(head) >= n:
            break
        if id(s) not in picked:
            head.append(s)
            picked.add(id(s))
    head.sort(key=lambda s: order[id(s)])
    tail = [s for s in ranked if id(s) not in picked]
    return head, tail


def parse_config_version(raw):
    """解析存量 config_version：返回 int，无法确定时返回 None。

    接受 int、整数 float（如 2.0）、数字字符串（如 "2"、"2.0"）；
    非数字字符串（"abc"）、None、非整数 float（2.5）、bool、空串等一律返回 None，
    调用方不得将其误判为 v1（否则会重跑破坏性迁移，翻掉用户显式设的值）。
    与 p0p1-config（P1-11）同一函数、同一语义，合入时去重。
    """
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        return int(raw) if raw.is_integer() else None
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return None
        try:
            return int(s)
        except ValueError:
            pass
        try:
            f = float(s)
        except ValueError:
            return None
        return int(f) if f.is_integer() else None
    return None


def _backup_dir() -> Path:
    return _STATE.parent / "backups"


def _backup_config() -> Path | None:
    """把当前 config.json 复制一份带时间戳的备份，只留最近 N 份。

    由 _persist 在成功写入后调用，备份即“最近一次完好的配置”。"""
    try:
        if not _STATE.is_file():
            return None
        d = _backup_dir()
        d.mkdir(parents=True, exist_ok=True)
        import datetime

        ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        dst = d / f"config-{ts}.json"
        # 同一秒内多次保存不覆盖已有备份。
        i = 1
        while dst.exists():
            dst = d / f"config-{ts}-{i}.json"
            i += 1
        dst.write_bytes(_STATE.read_bytes())
        # 按修改时间删最旧：同秒多次备份会带 -1 后缀，按文件名排序会误删新备份。
        for old in sorted(d.glob("config-*.json"),
                          key=lambda b: (b.stat().st_mtime, b.name))[: -BACKUP_KEEP]:
            try:
                old.unlink()
            except OSError:
                pass
        return dst
    except Exception as e:
        _log.warning("config backup failed: %s: %s", type(e).__name__, e)
        return None


def _restore_from_backup() -> dict | None:
    """主配置损坏时按时间倒序尝试备份，返回第一个能解析的备份内容。"""
    try:
        # 按修改时间倒序：同秒多次备份会带 -1 后缀，按文件名倒序会把新备份排在旧备份之后。
        backups = sorted(_backup_dir().glob("config-*.json"),
                         key=lambda b: (b.stat().st_mtime, b.name), reverse=True)
    except Exception:
        return None
    for b in backups:
        try:
            j = json.loads(b.read_text(encoding="utf-8"))
            if isinstance(j, dict):
                _log.error("config corrupted, restored from backup: %s", b.name)
                return j
        except Exception:
            continue
    return None

# 噪音关键词：标题或备注命中则不注入（电影解说、预告、花絮等）。
# P2：AI漫剧 / 漫剧 / 动态漫画已是正式分类（catalog.ai_comic），不再当噪音过滤。
JUNK_KEYWORDS = [
    "电影解说", "影视解说", "解说", "预告片", "预告", "花絮", "抢先", "枪版",
    "彩蛋", "删减", "片场", "开机", "特辑",
]

# 内容红线关键词（出厂词表）：涉未成年人性化、非自愿（换脸等）的内容。
# 与噪音词不同，命中是**整条丢弃**（不进聚合库/独立库/搜索），不只影响展示。
# 后台「搜索设置 → 内容安全 · 红线关键词」可增删改，配置里保存**完整词表**，
# 因此删掉默认词同样是永久生效的；下面这份只用于首次初始化与配置损坏时兜底。
REDLINE_KEYWORDS = [
    "萝莉", "童贞", "换脸",
]

DEFAULTS = {
    "config_version": CONFIG_VERSION,
    "sources": [dict(s) for s in DEFAULT_SOURCES],
    # 被删除的内置源 id（自定义源删除时直接从 sources 移除）
    "removed_ids": [],
    # 订阅源管理：多个订阅 URL，各自可设更新间隔，后台定时拉取只增不减。
    # 每项：{id, name, url, builtin, update_interval_h, enabled, last_update,
    #       last_status, fail_count, last_error}
    "subscriptions": [],
    "max_per_keyword": 12,
    "filter_junk": True,
    "junk_keywords": JUNK_KEYWORDS,
    # 内容红线关键词（完整词表，后台可增删改）：标题/备注/分类名命中即整条丢弃。
    # 出厂值见 REDLINE_KEYWORDS；用户删光后为 []，即不再拦截。
    "redline_keywords": list(REDLINE_KEYWORDS),
    # 在线资源前排展示（在线片库更齐全，默认前排）
    "online_first": True,
    # 播放线路健康探测与自动换线：当前线路解析失败/文件不存在时，自动探测同集其他
    # 线路并切到首个可用线路；坏线短时记忆，起播自动跳过（关闭则保持纯手动选线）
    "line_autofail": True,
    # >>> PRIVATE:CATVOD
    # 网盘分类媒体库（完整版私有能力）：按网盘目录分类建立多张独立库卡片。
    "pan_lib_enabled": True,
    # <<< PRIVATE:CATVOD
    # 完全自动选最快线：起播时并行竞速探测前若干条候选线路，选首字节延迟最低的线；
    # 最快线短时记忆，同一集后续/重放稳定用它（手动在播放器切线仍被尊重）
    "line_fastest": True,
    # 自动选最快线时最多参与竞速的候选线路数（控制起播开销）
    "line_fastest_max": 6,
    # 竞速超时预算（秒）：超时未响应的线路本轮判慢，不阻塞起播
    "line_fastest_timeout": 4.0,
    # 默认线（高质量源）领先优势（秒）：它在该优势内返回就算赢，避免无谓抖动
    "line_fastest_prefer": 0.25,
    # 慢线起播预算（秒）：仅慢源（加固/爬虫/drpy/专有解码/小雅）main.m3u8
    # 首个请求的最长等待；每次取 main 时实时读取并夹取 [0.8, 2.8]，
    # 改值即时生效，无需重启。
    "main_ready_budget": MAIN_READY_BUDGET_DEFAULT,
    # —— 本片线路预测（linepredict）：浏览海报时对首屏影片的少数候选线做轻量探活，
    # 预测出本片最快线并记忆，点播放零竞速秒播；取代旧的全局源测速 speedbench ——
    # 是否在海报/搜索列表阶段对可见影片做轻量预热
    "linepredict_enabled": True,
    # 每张海报最多探测的候选线数（只探最可能起播的前几条，避免压源站）
    "linepredict_max_lines": 3,
    # 预热结果记忆时长（分钟）
    "linepredict_ttl_min": 30,
    # 启动预热方式：realtime=重启后后台逐部搜源绑定（方案A，默认）；
    # persist=热门片绑定结果落盘，重启直接恢复线路、少重新搜源（方案B）
    "prewarm_mode": "realtime",
    # 虚拟“热门榜单”四库媒体库（豆瓣真实周榜/标签榜）
    "hot_library_enabled": True,
    # 虚拟“海量分类”四库媒体库（苹果 CMS 分类深翻）。与热门榜单相互独立开关。
    "catalog_library_enabled": True,
    "t4_library_enabled": True,
    # 扩展（成人）资源库独立开关：开启后，已被源标记为「福利分类(adult)」的
    # 源才把 ADULT_CAT 纳入浏览。默认关闭：不建路由、不抓取、不显示。
    "welfare_library_enabled": False,
    # 是否把每个启用的 TVBox 加固源（csp_*Guard，如比特/糯米/花花）生成为一张
    # 独立“专区媒体库”卡片。默认关闭：加固源只作为播放时可切换的线路/搜索源，
    # 不在媒体库列表里产生空库。需要专区墙时可在管理页打开。
    "guard_zone_enabled": False,
    # 是否在飞牛原生媒体库注入单张「🛡加固影视」聚合库（源→分类片单，后台预取
    # 缓存、请求即时返回）。与 guard_zone_enabled（每源一张专区墙）相互独立。
    "guard_native_enabled": True,
    # 置顶的虚拟媒体库 guid 集合（打开=在飞牛影视媒体库列表置顶，关闭=普通顺序）。
    "pinned_libs": [],
    # 虚拟媒体库显隐/排序/子账号授权（上游 vlibs 设计，按稳定库键 key 配置，
    # 与 guid 解耦）。lib_hidden：管理员隐藏的库键；lib_order：虚拟库自定义
    # 顺序；lib_placement：front/back/after；lib_anchor：after 的锚点本地库
    # guid；lib_acl：{用户 guid: [允许的库键，含 "__online__" 标记]}；
    # acl_default_visible/acl_default_online：未配置账号的默认（True=可见，
    # fail-open 默认，上游原样；False=隐藏。识别不到身份的会话也按这两项
    # 处理，见 vlibs.account_can_see）。
    "lib_hidden": [],
    "lib_order": [],
    "lib_placement": "front",
    "lib_anchor": "",
    "lib_acl": {},
    "acl_default_visible": True,
    "acl_default_online": True,
    "hot_library_title": "🌐在线热门",
    "hot_limit": 300,          # 四库合计保留条数上限
    "hot_per_source": 120,    # 每个资源站最多抓取的可播条目
    "hot_pages_per_source": 6,  # 每个资源站翻“最近更新”的页数（每页约20条）
    "hot_refresh_interval": 6 * 3600,
    # —— 在线资源会话缓存 state/titles.json（issue #42：搜索越用越慢）——
    # 该文件每次合并搜索结果都会落盘；机械盘/ARM 盒子写几十 MB 要数秒，直接拖慢搜索。
    # 故同时给出「条数上限」和「字节上限」双重裁剪，并把落盘改为后台防抖异步写。
    "titles_cache_max_entries": 4000,   # 最多保留多少条标题（超出丢最旧）
    "titles_cache_max_mb": 4,          # 落盘文件 MB 硬上限（超出按最旧裁剪；0=不按字节限）
    "titles_flush_debounce": 5,        # 落盘防抖秒数：脏后最多等这么久合并写一次（0=立即）
    # 源质量优先级：source_id -> 权重（越高越优先）。未列出的源取内省默认（见
    # providers.source_priority）。热门榜单点击搜源时按 (优先级, 直链数) 排序。
    "source_priority": {},
    # 人工排序干预（替代已废弃的 100 级权重表，仅两个动作、互斥）：
    #   source_pinned  ：置顶，搜索结果永远排最前
    #   source_blocked ：屏蔽，搜索结果沉到最后（仍参与搜索，不删源）
    "source_pinned": [],
    "source_blocked": [],
    # 热门资源解析是否优先高质量源（关闭则只按可播直链数）
    "prefer_quality_source": True,
    # 自动优选（上游 v0.2.47）：在源质量权重基础上，叠加实测健康度（死亡/高失败率/
    # 慢的源排后），让能连通、延迟低的源默认排到更前。默认开启；关掉则只看质量权重。
    "auto_best_source": True,
    "admin_token": "",
    # 管理令牌元数据（admin_token 加固，见 proxy/admin_auth.py）：
    # created_at：令牌创建时间（epoch 秒）；老配置没有时升级时打戳为当前时间，
    # 年龄从升级日起算，老令牌本身继续可用。
    # previous/previous_expires_at：轮换下来的旧令牌及其宽限截止时间。
    # grace_hours：轮换后旧令牌仍可用的宽限（小时，默认 24）。
    # max_age_days：令牌最长年龄（天），0 = 不按年龄强制过期（默认，避免
    #   用户久未登录被锁死；如需强制轮换可改成如 90，过期后须重新生成）。
    # admin_auth_max_failures/lockout_seconds/window_seconds：登录限流阈值。
    "admin_token_created_at": 0,
    "admin_token_previous": "",
    "admin_token_previous_expires_at": 0,
    "admin_token_grace_hours": 24,
    "admin_token_max_age_days": 0,
    "admin_auth_max_failures": 5,
    "admin_auth_lockout_seconds": 900,
    "admin_auth_window_seconds": 900,
    # —— 统一网络代理（A1，2026-11-12）——
    # 一处填地址 + 四处用途开关 + 手动例外，替代原来分散的
    # subscription_proxy / source_proxy / update_proxy 三个输入框。
    # 老字段保留（只读兼容），启动时按 _legacy_proxy() 自动回退。
    "proxy_url": "",
    "proxy_use": {"subscription": True, "source": True,
                  "update": False, "media": False},
    # 手动例外（智能模式生效，最高优先级）：
    #   proxy_always_hosts：总是走代理（直连能通但慢/不稳的站）
    #   proxy_never_hosts ：永远直连（加代理反而不能播的站）
    "proxy_always_hosts": [],
    "proxy_never_hosts": [],
    # —— GitHub 加速镜像池（A2，2026-11-12）——
    # 照 lx-hunter P-0915 的做法：14 个种子探测通了才入池，按实测响应时间排序，
    # 连续失败 3 次自动出池（enabled=False），支持手动移除/启停。
    # 空列表 = 尚未探测过，此时按内置种子顺序兜底（不阻塞首次使用）。
    # 元素结构：{"host": str, "kind": "prefix|raw|jsdelivr", "ms": int,
    #            "fails": int, "enabled": bool}
    "github_mirror_pool": [],
    "github_mirror_last_scan": 0,
    # 仅用于「拉取订阅资源」的可选 HTTP(S) 代理（订阅配置 / spider jar / DRPY 脚本）。
    # 【已并入 proxy_url + proxy_use.subscription；仅为老配置兼容保留】
    "subscription_proxy": "",
    # 仅用于「访问资源站取源」（聚合搜索 / 分类深翻 / 测速探测 / CMS 详情）的可选
    # HTTP(S) 代理。【已并入 proxy_url + proxy_use.source；仅为老配置兼容保留】
    "source_proxy": "",
    # 资源站代理模式："off"=一律直连（忽略已配置代理）；"on"=所有取源都走代理；
    # "auto"=默认直连，仅当某个资源站直连发生连接级失败（重置/超时/DNS等）时，
    # 自动对该站切换到已配置代理重试，其余站点保持直连（推荐）。
    "source_proxy_mode": "auto",
    # 检查/下载插件更新安装包时可选的 HTTP(S) 代理（仅更新用途）。
    # 【已并入 proxy_url + proxy_use.update；仅为老配置兼容保留】
    "update_proxy": "",
    # —— 网盘过期转存清理（P1-8，与上游调用点默认值一致）——
    # 默认开启：转存只是可随时重新生成的缓存副本，过期不清理会无限占用
    # 用户网盘空间；TTL 6h：覆盖一次正常观影（点播时实时换链，清理只删
    # 云端副本、不碰进行中的播放会话），又不至于让缓存长期堆积。
    # 清理每小时跑一次（lifespan 内 _pan_cache_loop），开关关则不跑。
    "pan_cache_cleanup_enabled": True,
    "pan_cache_ttl_hours": 6,
    # m3u8 流层去广告总开关（TVBox/OK影视 Pro rules 引擎 + 通用启发式）。
    "ad_filter_enabled": True,
    # 播放时动态检测（无订阅 rules 时，按 DISCONTINUITY 分段+时长/CDN/位置识别广告段）。
    "ad_filter_dynamic": True,
    # 分片编码参数(SPS)指纹去广告：识别"重新编码拼接、无任何结构特征"的广告。
    # 只读分片头 4~32KB，按块时长加权投票 + 多重守卫，判定不确定时绝不删。
    "ad_filter_sps": True,
    # 红果短剧（HGDJ）伪媒体库：由本机 HGDJ 服务提供元数据/播放流，
    # 插件仅充当客户端，本机服务缺省监听 127.0.0.1:18098。
    "hgdj_enabled": False,
    "hgdj_url": "http://127.0.0.1:18098",
    # ---- P2 开关 ----
    # 追剧提醒：开启后，收藏的剧集数变多时在管理页 / _ext/follow 提醒。
    "follow_enabled": True,
    # 源健康自动降级：开启后，连续失败 5 次的源在 3 分钟冷却窗内被搜索跳过。
    "source_health_enabled": True,
    # 域名自愈：开启后，资源站主域名失效时按备用域名列表自动试下一个。
    # 每个源的备用域名：source_id -> [域名/URL/JSON {"host": "..."}]，默认空。
    "domain_heal_enabled": True,
    "source_domain_backups": {},
    # 元数据补全：开启后，详情缺失的年份/地区/简介/海报用同名其他源的条目补齐。
    # 不接任何外部元数据接口。
    "metadata_enabled": True,
    # 关联作品：开启后，详情页下方挂 recommendations 关联作品（同系列/同分类优先）。
    "related_enabled": True,

    # ---- 巡检（第 3 批，2026-11-12）----
    # 是否启用定期巡检。与 source_health_enabled 分开：后者只管「搜索结果降权」，
    # 本项只管「定时探测 + 停用/删除」，一个开关一种语义。
    "patrol_enabled": True,
    # 正常源巡检间隔（小时）。实测 30 源一轮 48~78 秒，6 小时足够。
    "patrol_interval_h": 6,
    # 已停用源巡检间隔（小时）。停用源要快速复活，默认 1 小时。
    "patrol_disabled_interval_h": 1,
    # 停用满多少天自动删除；0 表示永不删除（只停用）。
    "patrol_retain_days": 30,
    # 单次探测超时（秒）。实测 p90=17.5s、max=24.2s，故 25 秒。
    "patrol_timeout_s": 25,
    # 巡检并发。实测并发 2/4/8 的存活判定一致，4 兼顾速度与不惊动源站。
    "patrol_concurrency": 4,
    # 连续失败阈值（浮点比较用 int）。
    "patrol_fail_threshold": 3,
    # 首次失败到判定停用至少要跨越的小时数，防止瞬时抖动判死。
    "patrol_min_span_h": 12,

    # ---- 巡检结果回流搜索（第 3 批）----
    # 搜索只取按质量排序后的前 N 个源；0 = 不截断（只排序）。
    "search_top_n": 40,

    # 已删除源的墓碑：{id, name, api, reason, deleted_at, last_error, quality}
    # 用于「已移除」列表展示与单条恢复，避免删掉就再也找不回来。
    "removed_sources": [],
}

# 已废弃、需从旧配置里清除的键
_DEPRECATED = {"public_base", "strm_root", "library_visible_users", "t4_sources",
               "tmdb_api_key"}
_BUILTIN_IDS = {s["id"] for s in DEFAULT_SOURCES}


class Config:
    def __init__(self):
        self._cfg: dict = {}
        self._mtime = 0
        # 红线词表缓存（元组不可变，供 is_redline 热路径直接复用）；任何落盘/重载都置空
        self._redline_cache: tuple[str, ...] | None = None
        self.load()

    # >>> PRIVATE:GUARD
    def _sync_guard_defaults(self):
        """把 guard 模块的实时源（内置+用户导入）同步进 DEFAULT_SOURCES 底表。

        导入 TVBox 配置后调用，使新源无需重启即进入源列表/聚合；旧的导入源若
        已删除则从底表移除（用户配置中的 enabled 等仍按 saved_by_id 合并）。
        """
        if features.GUARD:
            try:
                import providers as _prov
                live = {d["id"]: d for d in _prov.guard_source_defs()}
                # 普通 csp_ 爬虫源（订阅导入，spider- 前缀）
                try:
                    import spiders as _sp
                    live.update({d["id"]: d for d in _sp.source_defs()})
                except Exception:
                    _sp = None
                # 移除已不存在的 guard/普通爬虫底表项（保留其它与内置项）
                for s in list(DEFAULT_SOURCES):
                    if (s.get("guard") or s.get("spider_src")) and \
                            s["id"] not in live and not s.get("builtin_locked"):
                        DEFAULT_SOURCES.remove(s)
                for d in live.values():
                    ex = next((s for s in DEFAULT_SOURCES if s["id"] == d["id"]), None)
                    if ex is None:
                        DEFAULT_SOURCES.append(dict(d))
                    elif d.get("guard"):
                        # 名称/guard_key 跟随导入定义更新
                        ex["name"] = d["name"]
                        ex["guard_key"] = d["guard_key"]
                    elif d.get("spider_src"):
                        ex["name"] = d["name"]
                        ex["spider_key"] = d.get("spider_key", "")
            except Exception:
                pass
        # <<< PRIVATE:GUARD

    def load(self):
        with _LOCK:
            # >>> PRIVATE:GUARD
            self._sync_guard_defaults()
            # <<< PRIVATE:GUARD
            self._restored_from_backup = False
            _read_missing = False
            try:
                j = json.loads(_STATE.read_text(encoding="utf-8"))
                if not isinstance(j, dict):
                    raise ValueError("config root is not an object")
            except FileNotFoundError:
                j = {}
                _read_missing = True
            except Exception as e:
                # 主配置损坏：先试备份，备份也坏才退回默认值，绝不静默丢配置。
                _log.error("config corrupted (%s: %s), trying backups",
                           type(e).__name__, e)
                j = _restore_from_backup() or {}
                self._restored_from_backup = bool(j)
                if not j:
                    _log.error("no usable config backup, falling back to defaults")
            cfg = json.loads(json.dumps(DEFAULTS))  # deep copy
            cfg.update(j)
            # 存量版本号：v1→v2→v3 迁移链的判定依据，须在下面 setdefault
            # 补当前版本之前读取。缺失该键的旧配置视为 v1；键存在但
            # 解析失败时（如 "2.0"、"abc"、None）不得误判为 v1——按
            # “无法确定版本”处理，跳过破坏性迁移（见下），只记 warning。
            if "config_version" not in j:
                _stored_version = 1
                _version_known = True
                _version_missing = True
            else:
                _stored_version = parse_config_version(j.get("config_version"))
                _version_known = _stored_version is not None
                _version_missing = False
            # 旧配置没有版本号时补当前版本，后续升级按版本号迁移。
            cfg.setdefault("config_version", CONFIG_VERSION)
            for k in list(cfg.keys()):
                if k in _DEPRECATED:
                    cfg.pop(k, None)
            removed = set(cfg.get("removed_ids", []))

            # 源列表：以内置为底（跳过被删除的），保留用户的启用/改名/改地址，
            # 再追加用户自定义源。
            saved = j.get("sources") if isinstance(j.get("sources"), list) else []
            saved_by_id = {s.get("id"): s for s in saved if isinstance(s, dict)}
            merged = []
            for s in DEFAULT_SOURCES:
                if s["id"] in removed:
                    continue
                cur = saved_by_id.get(s["id"], {})
                m = dict(s)
                # api 以代码为准（内置源地址随版本修复才能真正生效）；enabled/name
                # 保留本地。用户改不了内置源的 api（update_source 硬拒），所以这里
                # 用代码地址不会覆盖任何人的自定义。历史上确实存过旧 api 的，此次
                # 升级会一次性纠正到新地址。
                m.update({k: cur[k] for k in ("enabled", "name") if k in cur})
                # 巡检状态字段必须能从落盘配置恢复（否则重启后 sys_disabled /
                # disabled_at 丢失，内置源会被当成用户手动停用而永不复活）。
                for key in ("verified", "fail_count", "last_check", "last_ok",
                            "disabled_reason", "disabled_at", "sys_disabled",
                            "quality"):
                    if cur.get(key) is not None:
                        m[key] = cur[key]
                m["builtin"] = True
                m["standalone"] = bool(cur.get("standalone", False))
                m["loose_filter"] = bool(cur.get("loose_filter", False))
                # >>> PRIVATE:ADULT
                m["adult"] = bool(cur.get("adult", False)) if features.ADULT else False
                # <<< PRIVATE:ADULT
                # >>> PRIVATE:DDUO
                m["xsrc"] = bool(s.get("xsrc", False)) if features.DDUO else False
                # <<< PRIVATE:DDUO
                # >>> PRIVATE:CATVOD
                m["catvod"] = bool(s.get("catvod", False)) if features.CATVOD else False
                if m["catvod"]:
                    m["xsrc"] = True
                # <<< PRIVATE:CATVOD
                # >>> PRIVATE:DRPY
                m["drpy"] = bool(s.get("drpy", False)) if features.DRPY else False
                if m["drpy"]:
                    m["xsrc"] = True
                # <<< PRIVATE:DRPY
                # >>> PRIVATE:GUARD
                m["guard"] = bool(s.get("guard", False)) if features.GUARD else False
                m["guard_key"] = s.get("guard_key", "") if features.GUARD else ""
                m["spider_src"] = bool(s.get("spider_src", False)) if features.GUARD else False
                m["spider_key"] = s.get("spider_key", "") if features.GUARD else ""
                if m["guard"] or m["spider_src"]:
                    m["xsrc"] = True
                # <<< PRIVATE:GUARD
                merged.append(m)
            for sid, s in saved_by_id.items():
                if sid in _BUILTIN_IDS or sid in removed:
                    continue
                m = {k: s.get(k) for k in ("id", "name", "api", "enabled")}
                m["enabled"] = bool(s.get("enabled", True))
                m["standalone"] = bool(s.get("standalone", False))
                m["loose_filter"] = bool(s.get("loose_filter", False))
                # >>> PRIVATE:ADULT
                m["adult"] = bool(s.get("adult", False)) if features.ADULT else False
                # <<< PRIVATE:ADULT
                # >>> PRIVATE:DDUO
                m["xsrc"] = bool(s.get("xsrc", False)) if features.DDUO else False
                # <<< PRIVATE:DDUO
                # >>> PRIVATE:DRPY
                m["drpy"] = bool(s.get("drpy", False)) if features.DRPY else False
                # <<< PRIVATE:DRPY
                # >>> PRIVATE:GUARD
                m["guard"] = bool(s.get("guard", False)) if features.GUARD else False
                m["guard_key"] = s.get("guard_key", "") if features.GUARD else ""
                # <<< PRIVATE:GUARD
                m["builtin"] = bool(s.get("builtin", False))
                for key in ("kind", "site_key", "ext", "searchable", "filterable", "indexs", "method", "sub_id", "live"):
                    if key in s:
                        m[key] = s[key]
                # A1/A2：巡检状态字段必须持久化，否则重启即丢（曾踩坑：verified 写 False
                # 后重启被合并逻辑抹成 None）。这里显式登记，新增巡检字段请一并加进来。
                for key in ("verified", "fail_count", "last_check", "last_ok",
                            "disabled_reason", "disabled_at", "sys_disabled",
                            "quality"):
                    if s.get(key) is not None:
                        m[key] = s[key]
                merged.append(m)
            cfg["sources"] = merged
            cfg["removed_ids"] = sorted(removed)
            # 旧版本（单库/小数量）保存过的热门参数自动升级到深度爬取规模；
            # 仅在检测到旧小值时迁移一次，用户之后自定义的大值不会被覆盖。
            migrated = False
            _need_token_persist = False
            try:
                if int(cfg.get("hot_limit", 0)) <= 120 and int(DEFAULTS["hot_limit"]) > 120:
                    cfg["hot_limit"] = DEFAULTS["hot_limit"]; migrated = True
                if int(cfg.get("hot_per_source", 0)) <= 40:
                    cfg["hot_per_source"] = DEFAULTS["hot_per_source"]; migrated = True
                if not cfg.get("hot_pages_per_source"):
                    cfg["hot_pages_per_source"] = DEFAULTS["hot_pages_per_source"]; migrated = True
            except (TypeError, ValueError):
                pass
            # v1→v2 一次性迁移（decisions-2-failclosed）：多用户可见性默认改
            # fail-closed。旧配置两项默认 True，存量用户也须翻成 False；只在
            # 存量版本 < 2 时跑一次，之后立即由 v2→v3 接力（v1 配置走完整链，
            # 最终仍为 True）。
            # v2→v3 一次性迁移（fail-open 回滚）：多用户可见性默认改回
            # fail-open。v2 存量用户（含已在真机跑过 v1→v2、两项已落盘为
            # False 的）一次性翻回 True；只在存量版本 < 3 时跑一次，之后
            # 用户在管理页手动改回 False 会保留（版本号已是 3，不再迁移）。
            # 迁移跑与不跑都记一条日志；版本无法确定或高于当前版本时只警告，
            # 不重跑破坏性迁移（解析失败误判为 v1 会把用户显式设的值翻掉）。
            _acl_migrated = False
            _version_normalized = False
            if not _version_known:
                # 无法确定版本：跳过 v1→v2 / v2→v3 破坏性迁移，用户值原样保留；
                # 只把版本号归一化为当前版本（非破坏性），避免每次加载都重复警告。
                _log.warning(
                    "config_version unparsable (%r), skipping v1->v2/v2->v3 "
                    "migration; keeping user values and normalizing "
                    "version to %d", j.get("config_version"), CONFIG_VERSION)
                cfg["config_version"] = CONFIG_VERSION
                _version_normalized = True
                migrated = True
            elif _stored_version > CONFIG_VERSION:
                # 未来版本降级使用：不迁移、不降版本号，只警告（新版本可能
                # 已写入本版本不认识的键，静默改写会丢数据）。
                _log.warning(
                    "config_version %d is newer than supported %d; "
                    "skipping migration, keeping stored values as-is",
                    _stored_version, CONFIG_VERSION)
            else:
                _cur_version = _stored_version
                if _cur_version < 2:
                    cfg["acl_default_visible"] = False
                    cfg["acl_default_online"] = False
                    cfg["config_version"] = 2
                    _cur_version = 2
                    _acl_migrated = True
                    migrated = True
                    _log.info(
                        "config migration v1->v2: acl defaults flipped to "
                        "fail-closed (stored version %s%s)", _stored_version,
                        " (version key missing, treated as v1)"
                        if _version_missing else "")
                if _cur_version < 3:
                    cfg["acl_default_visible"] = True
                    cfg["acl_default_online"] = True
                    cfg["config_version"] = 3
                    _acl_migrated = True
                    migrated = True
                    _log.info(
                        "config migration v2->v3: acl defaults flipped back "
                        "to fail-open (stored version %s)", _stored_version)
                else:
                    _log.info(
                        "config migration skipped: stored version %d is "
                        "current, no migration needed", _stored_version)
            if migrated:
                _mig_keys = ("hot_limit", "hot_per_source", "hot_pages_per_source")
                if _acl_migrated:
                    _mig_keys += ("acl_default_visible", "acl_default_online",
                                  "config_version")
                if _version_normalized:
                    _mig_keys += ("config_version",)
                j.update({k: cfg[k] for k in _mig_keys})
                try:
                    # 走 _persist：先备份、再写临时文件 + 原子 rename。
                    # cfg 已含合并后的 sources 与迁移后的 hot_* 值，直接持久化它。
                    self._persist(cfg)
                except Exception as e:
                    _log.warning("config migration persist failed: %s: %s",
                                 type(e).__name__, e)
            if not cfg.get("admin_token"):
                import secrets
                import time as _time
                # 新令牌 256 bit；创建时间一并落盘（否则重启后令牌会变）。
                cfg["admin_token"] = secrets.token_hex(32)
                cfg["admin_token_created_at"] = int(_time.time())
                _need_token_persist = True
            elif not cfg.get("admin_token_created_at"):
                # 老令牌（16 hex 字符）原样保留、继续可用，只补创建时间戳，
                # 年龄从本次升级日起算；max_age_days 默认 0，本就不按年龄过期。
                import time as _time
                cfg["admin_token_created_at"] = int(_time.time())
                _need_token_persist = True
            # 宽限已过的旧令牌不再保留（当前令牌不受影响，不会锁死用户）。
            _prev_exp = cfg.get("admin_token_previous_expires_at") or 0
            if cfg.get("admin_token_previous") and _prev_exp:
                import time as _time
                if int(_prev_exp) <= int(_time.time()):
                    cfg["admin_token_previous"] = ""
                    cfg["admin_token_previous_expires_at"] = 0
                    _need_token_persist = True
            # load() 里只在两种情况下落盘：文件本就不存在（首次生成令牌，
            # 否则重启后令牌会变），或刚从备份恢复（顺手修好已损坏的主配置）。
            # 其余情况（老令牌补打戳、过期旧令牌清理）只改内存，等下一次
            # save() 再落盘——避免覆盖用户手改后待修复的配置文件。
            if _need_token_persist and (_read_missing or self._restored_from_backup):
                try:
                    self._persist(cfg, backup=False)
                except Exception as e:
                    _log.warning("admin token persist failed: %s: %s",
                                 type(e).__name__, e)
            self._cfg = cfg
            self._redline_cache = None
            try:
                self._mtime = _STATE.stat().st_mtime
            except FileNotFoundError:
                self._mtime = 0
                # 首次安装（state/config.json 尚不存在）：把合并后的默认配置落盘一次。
                # 否则干净安装后 status/管理页等依赖“文件存在”的路径会异常（上游 v0.2.45，issue #39）。
                try:
                    self._persist(cfg)
                    self._mtime = _STATE.stat().st_mtime
                except Exception:
                    pass

    def maybe_reload(self):
        try:
            mt = _STATE.stat().st_mtime
        except FileNotFoundError:
            return
        if mt != self._mtime:
            self.load()

    def get(self):
        with _LOCK:
            return json.loads(json.dumps(self._cfg))

    def enabled_sources(self):
        self.maybe_reload()
        with _LOCK:
            return [dict(s) for s in self._cfg["sources"] if s.get("enabled")]

    # ---------------- 巡检质量排序（第 3 批） ----------------

    @staticmethod
    def _lat_bucket(ms) -> int:
        """延迟分档：<1s=0, <3s=1, <8s=2, >=8s=3，无数据=2（居中，给机会）。"""
        try:
            v = float(ms)
        except (TypeError, ValueError):
            return 2
        if v <= 0:
            return 2
        if v < 1000:
            return 0
        if v < 3000:
            return 1
        if v < 8000:
            return 2
        return 3

    @staticmethod
    def _state_bucket(state: str) -> int:
        """状态分档：ok=0, 未知=1, 限搜=2, 存疑=3。"""
        return {"ok": 0, "": 1, None: 1, "limited": 2, "suspect": 3,
                "unsupported": 2}.get(state, 1)

    @staticmethod
    def _rate_bucket(rate) -> int:
        """7 天可用率分档：>=0.9=0, >=0.7=1, >=0.5=2, 更低=3，无数据=1。"""
        try:
            v = float(rate)
        except (TypeError, ValueError):
            return 1
        if v >= 0.9:
            return 0
        if v >= 0.7:
            return 1
        if v >= 0.5:
            return 2
        return 3

    def rank_sources(self, sources: list[dict], limit: int | None = None) -> list[dict]:
        """按巡检质量排序（字典序，不做加权总分），可截断前 N 个。

        排序键从高到低：
          0 置顶    人工置顶永远最前
          1 屏蔽    人工屏蔽的沉底（不删除，也不进前 N）
          2 状态    ok > 未知 > 限搜 > 存疑（不删不弃，只影响先后）
          3 预算    3 秒内能返回的排前面（全局搜索总预算 5 秒，慢了等于没结果）
          4 命中    最近 5 轮巡检关键词命中累计（有片优先）
          5 直链    搜到的结果里有 http 直链（能播概率高）
          6 延迟    巡检实测延迟分档（<1s / 1~3s / 3~8s / >=8s）
          7 可用率  近 7 天巡检成功率分档
          8 人工    source_priority（人工片源质量，值大优先）
          9 id      稳定排序，避免刷新后顺序乱跳

        每一档都能指着巡检日志说清「它为什么排后面」，不引入权重表。
        """
        self.maybe_reload()
        with _LOCK:
            cfg = self._cfg
            pinned = {x for x in (cfg.get("source_pinned") or []) if isinstance(x, str)}
            blocked = {x for x in (cfg.get("source_blocked") or []) if isinstance(x, str)}
            prio = cfg.get("source_priority") or {}

        def key(s: dict):
            sid = str(s.get("id") or "")
            q = s.get("quality") if isinstance(s.get("quality"), dict) else {}
            try:
                hits = int(q.get("hits") or 0)
            except (TypeError, ValueError):
                hits = 0
            try:
                direct = int(q.get("direct") or 0)
            except (TypeError, ValueError):
                direct = 0
            try:
                lat = float(q.get("lat_ms") or 0)
            except (TypeError, ValueError):
                lat = 0.0
            # 「赶得上搜索预算」比「快多少」更重要：全局搜索总预算 5 秒，
            # 3 秒还没回的源基本进不了结果。所以先按这个二值分档，同档内
            # 再看有没有片、有没有直链、快不快。
            feasible = 0 if (lat <= 0 or lat < 3000) else 1
            try:
                p = int(prio.get(sid) or 0)
            except (TypeError, ValueError):
                p = 0
            return (
                0 if sid in pinned else 1,
                1 if sid in blocked else 0,
                self._state_bucket(q.get("state")),
                feasible,
                -hits,
                0 if direct > 0 else 1,
                self._lat_bucket(q.get("lat_ms")),
                self._rate_bucket(q.get("ok_rate")),
                -p,
                sid,
            )

        ordered = sorted(sources, key=key)
        if limit is not None:
            try:
                n = int(limit)
            except (TypeError, ValueError):
                n = 0
            if n > 0:
                ordered = ordered[:n]
        return ordered

    def all_sources(self):
        self.maybe_reload()
        with _LOCK:
            return [dict(s) for s in self._cfg["sources"]]

    def save(self, patch: dict):
        with _LOCK:
            cfg = self.get()
            for k, v in patch.items():
                if k in cfg:
                    cfg[k] = v
            # 直接改写令牌（非轮换路径）时同步刷新创建时间、清掉旧令牌：
            # 直接改写没有宽限期，优雅轮换请走 rotate_admin_token()。
            if "admin_token" in patch:
                new_tok = str(patch.get("admin_token") or "")
                if new_tok and new_tok != str(self._cfg.get("admin_token") or ""):
                    import time as _time
                    cfg["admin_token_created_at"] = int(_time.time())
                    cfg["admin_token_previous"] = ""
                    cfg["admin_token_previous_expires_at"] = 0
            self._persist(cfg)

    # ---- 管理令牌（admin_token 加固）----
    def admin_token_info(self, now=None):
        """令牌状态（掩码展示，不含原文），供管理页显示年龄/过期/宽限。"""
        import admin_auth
        with _LOCK:
            return admin_auth.token_info(self.get(), now)

    def verify_admin_token(self, candidate: str, now=None):
        """校验候选令牌。返回 ``(是否通过, 原因)``，见 admin_auth.verify_token。"""
        import admin_auth
        with _LOCK:
            return admin_auth.verify_token(self.get(), candidate, now)

    def rotate_admin_token(self, now=None):
        """轮换管理令牌并返回 ``(新令牌原文, 令牌状态)``。

        当前令牌移入 ``admin_token_previous``，在宽限期
        （``admin_token_grace_hours``，默认 24 小时）内仍可登录；新令牌为
        256 bit。调用方须把新令牌原文**只展示一次**给用户保存（管理页），
        之后任何接口都不再返回原文。"""
        import admin_auth
        import time as _time
        with _LOCK:
            cfg = self.get()
            now = int(_time.time()) if now is None else int(now)
            old = str(cfg.get("admin_token") or "")
            grace_hours = cfg.get("admin_token_grace_hours") or 0
            try:
                grace_hours = int(grace_hours)
            except (TypeError, ValueError):
                grace_hours = 0
            if old and grace_hours > 0:
                cfg["admin_token_previous"] = old
                cfg["admin_token_previous_expires_at"] = now + grace_hours * 3600
            else:
                cfg["admin_token_previous"] = ""
                cfg["admin_token_previous_expires_at"] = 0
            new_tok = admin_auth.new_token()
            cfg["admin_token"] = new_tok
            cfg["admin_token_created_at"] = now
            self._persist(cfg)
            return new_tok, admin_auth.token_info(cfg, now)

    def _persist(self, cfg: dict, backup: bool = True):
        _STATE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(_STATE)
        # 成功写入后再备份：备份 = 最近一次已知的完好配置，
        # 主配置日后被外部写坏时可直接恢复到它。load() 里首次生成令牌
        # 的落盘不备份（backup=False），否则一份默认配置的备份会挡住
        # 之后真正的用户配置备份。
        if backup:
            _backup_config()
        self._cfg = cfg
        self._redline_cache = None
        self._mtime = _STATE.stat().st_mtime

    @staticmethod
    def make_custom_id(api: str) -> str:
        return "custom_" + hashlib.md5(api.strip().encode()).hexdigest()[:10]

    def add_source(self, name: str, api: str, enabled: bool = True,
                   standalone: bool = False, sub_id: str = "",
                   verified: bool | None = None) -> tuple[bool, str]:
        with _LOCK:
            cfg = self.get()
            api = (api or "").strip()
            name = (name or "").strip()
            if not api.startswith(("http://", "https://")):
                return False, "API 地址需以 http(s):// 开头"
            # 入库归一（与 TVBox 导入路径 tvbox.classify_site 一致）：
            # 苹果 CMS 的 /at/xml 只作运行时兼容（providers.parse_cms_xml，
            # 供已入库的旧 /at/xml 源继续工作），新入库统一写成 /at/json，
            # 避免同一站点的 xml/json 两条记录并存。只改新入库这一条，
            # 已入库源的 api 不动。query（如采集站 token）原样保留。
            api = re.sub(r"/at/xml(?=/|$|\?)", "/at/json", api, flags=re.I)
            if not name:
                name = api.split("/")[2]
            sid = self.make_custom_id(api)
            norm = api.rstrip("/").lower()
            if any(s["id"] == sid or s.get("api", "").rstrip("/").lower() == norm
                   for s in cfg["sources"]):
                return False, "该资源站地址已存在"
            entry = {"id": sid, "name": name, "api": api,
                     "enabled": bool(enabled), "standalone": bool(standalone),
                     "builtin": False}
            # 待验证标记：订阅导入时不探测（探测交给定时巡检），先入库标 False，
            # 巡检确认可用后置 True。缺省不写该键，老源行为不变。
            if verified is not None:
                entry["verified"] = bool(verified)
            if sub_id:
                entry["sub_id"] = sub_id
            cfg["sources"].append(entry)
            self._persist(cfg)
            return True, sid

    # 移植上游 v0.2.69（CMS 源导入/导出：/v/api/ext/source/update 的后端）。
    def update_source(self, sid: str, name: str | None = None,
                      api: str | None = None) -> tuple[bool, str]:
        """编辑资源站的显示名称 / 订阅(API)链接。

        - 改名：任意源均可；空名回退为域名。
        - 改链接：仅自定义源（builtin=False）允许，内置源改链会破坏升级一致性，
          如需自定义请删除后按新地址添加。新地址须 http(s) 且不能与其它源重复。
        改 api 后该自定义源 id（由旧地址哈希生成）会一并重建为新地址的 id。"""
        with _LOCK:
            cfg = self.get()
            target = next((s for s in cfg["sources"] if s["id"] == sid), None)
            if not target:
                return False, "源不存在"
            if name is not None:
                nm = str(name).strip()[:40]
                if not nm:
                    api0 = str(target.get("api", ""))
                    nm = api0.split("/")[2] if "://" in api0 else str(target.get("name", "资源站"))
                target["name"] = nm
            if api is not None:
                newapi = str(api).strip()
                if not newapi.startswith(("http://", "https://")):
                    return False, "API 地址需以 http(s):// 开头"
                if target.get("builtin"):
                    return False, "内置源不支持改链接，请删除后以自定义源重新添加"
                norm = newapi.rstrip("/").lower()
                if any(s["id"] != sid and
                       s.get("api", "").rstrip("/").lower() == norm
                       for s in cfg["sources"]):
                    return False, "该资源站地址已存在"
                new_id = self.make_custom_id(newapi)
                if any(s["id"] == new_id for s in cfg["sources"] if s["id"] != sid):
                    return False, "该资源站地址已存在"
                target["api"] = newapi
                target["id"] = new_id
                # 同步迁移该源的独立库/优先级等按 id 记录的配置
                pr = cfg.get("source_priority") or {}
                if sid in pr:
                    pr[new_id] = pr.pop(sid)
                sid = new_id
            self._persist(cfg)
            return True, sid

    def source_priorities(self) -> dict:
        with _LOCK:
            return dict(self.get().get("source_priority") or {})

    def import_t4_sources(self, sources: list[dict]) -> dict:
        import t4
        with _LOCK:
            cfg = self.get()
            existing = {s["id"] for s in cfg["sources"]}
            added, duplicate = [], 0
            for source in sources:
                sid = t4.source_id(source["api"], source.get("ext"))
                if sid in existing:
                    duplicate += 1
                    continue
                cfg["sources"].append(dict(
                    source, id=sid, kind="t4", builtin=False, standalone=False))
                existing.add(sid)
                added.append(source["name"])
            if added:
                self._persist(cfg)
            return {"candidates": len(sources), "added": len(added),
                    "added_names": added, "duplicate": duplicate}

    def import_live_sources(self, sources: list[dict], sub_id: str = "") -> dict:
        """导入 m3u8 直播源（LunaTV lives / 资源站电视直播源）。

        live 源以 kind="live" + live=True 标记入库，与 CMS/T4 源区分；
        id 用「live_ + url md5」生成，避免与其它源 id 冲突。
        返回 {candidates, added, added_names, duplicate}。
        """
        with _LOCK:
            cfg = self.get()
            existing_ids = {s["id"] for s in cfg["sources"]}
            existing_urls = {str(s.get("api", "")).strip() for s in cfg["sources"]}
            added, duplicate = [], 0
            for it in sources:
                if not isinstance(it, dict):
                    continue
                url = str(it.get("url") or "").strip()
                name = str(it.get("name") or "").strip()
                if not url.startswith(("http://", "https://")):
                    continue
                sid = "live_" + hashlib.md5(url.encode()).hexdigest()[:10]
                if sid in existing_ids or url in existing_urls:
                    duplicate += 1
                    continue
                entry = {
                    "id": sid, "name": name or url.split("/")[2],
                    "api": url, "enabled": True,
                    "kind": "live", "live": True,
                    "builtin": False, "standalone": False,
                }
                if it.get("ua"):
                    entry["ua"] = str(it["ua"]).strip()
                if it.get("epg"):
                    entry["epg"] = str(it["epg"]).strip()
                if sub_id:
                    entry["sub_id"] = sub_id
                cfg["sources"].append(entry)
                existing_ids.add(sid)
                existing_urls.add(url)
                added.append(entry["name"])
            if added:
                self._persist(cfg)
            return {"candidates": len(sources), "added": len(added),
                    "added_names": added, "duplicate": duplicate}

    def live_sources(self) -> list[dict]:
        """当前库内所有 m3u8 直播源（live=True）。"""
        self.maybe_reload()
        with _LOCK:
            return [dict(s) for s in self._cfg["sources"]
                    if s.get("live") and s.get("enabled")]

    @staticmethod
    def source_no_search(s: dict) -> bool:
        """该源是否**不参与全局搜索**（不影响海量库/独立库）。

        两层：源记录顶层 `no_search`（用户手动开关，优先）；否则看巡检自动判定
        `quality.no_search`（站点明确返回「暂不支持搜索」时打上）。
        缺省 = 参与搜索，老源行为不变。
        """
        v = (s or {}).get("no_search")
        if v is not None:
            return bool(v)
        q = (s or {}).get("quality")
        return bool(q.get("no_search")) if isinstance(q, dict) else False

    def set_source_no_search(self, sid: str, on: bool | None
                             ) -> tuple[bool, str]:
        """手动开关「不参与搜索」。on=None 表示恢复自动判定（删掉手动值）。"""
        sid = (sid or "").strip()
        if not sid:
            return False, "缺少源 id"
        with _LOCK:
            cfg = self.get()
            target = next((s for s in cfg.get("sources") or []
                           if s.get("id") == sid), None)
            if target is None:
                return False, "源不存在"
            if on is None:
                target.pop("no_search", None)
            else:
                target["no_search"] = bool(on)
            self._persist(cfg)
        return True, "ok"

    def set_source_priority(self, mapping: dict) -> tuple[bool, str]:
        """整体保存源优先级（id -> 整数权重），忽略未知源与非法值。"""
        with _LOCK:
            cfg = self.get()
            valid = {s["id"] for s in cfg["sources"]}
            out = {}
            for k, v in (mapping or {}).items():
                if k in valid:
                    try:
                        out[k] = max(-100, min(100, int(v)))
                    except (TypeError, ValueError):
                        continue
            cfg["source_priority"] = out
            self._persist(cfg)
            return True, "ok"

    # ---------------- 人工排序干预：置顶 / 屏蔽（互斥，替代 100 级权重表） ----------------

    def pinned_ids(self) -> set:
        """置顶的源 id 集合。"""
        self.maybe_reload()
        with _LOCK:
            return {x for x in (self._cfg.get("source_pinned") or [])
                    if isinstance(x, str) and x}

    def blocked_ids(self) -> set:
        """被屏蔽（沉底）的源 id 集合。"""
        self.maybe_reload()
        with _LOCK:
            return {x for x in (self._cfg.get("source_blocked") or [])
                    if isinstance(x, str) and x}

    def set_source_pin(self, sid: str, on: bool) -> tuple[bool, str]:
        """置顶：搜索结果永远排最前。与「屏蔽」互斥（置顶会取消屏蔽）。"""
        sid = (sid or "").strip()
        if not sid:
            return False, "缺少源 id"
        with _LOCK:
            cfg = self.get()
            pin = [x for x in (cfg.get("source_pinned") or [])
                   if isinstance(x, str) and x]
            blk = [x for x in (cfg.get("source_blocked") or [])
                   if isinstance(x, str) and x]
            if on:
                if sid not in pin:
                    pin.append(sid)
                blk = [x for x in blk if x != sid]
            else:
                pin = [x for x in pin if x != sid]
            cfg["source_pinned"] = pin
            cfg["source_blocked"] = blk
            self._persist(cfg)
            return True, "ok"

    def set_source_block(self, sid: str, on: bool) -> tuple[bool, str]:
        """屏蔽：搜索结果沉到最后（仍参与搜索，不删源）。与「置顶」互斥。"""
        sid = (sid or "").strip()
        if not sid:
            return False, "缺少源 id"
        with _LOCK:
            cfg = self.get()
            pin = [x for x in (cfg.get("source_pinned") or [])
                   if isinstance(x, str) and x]
            blk = [x for x in (cfg.get("source_blocked") or [])
                   if isinstance(x, str) and x]
            if on:
                if sid not in blk:
                    blk.append(sid)
                pin = [x for x in pin if x != sid]
            else:
                blk = [x for x in blk if x != sid]
            cfg["source_pinned"] = pin
            cfg["source_blocked"] = blk
            self._persist(cfg)
            return True, "ok"

    # ---------------- 订阅源管理（多订阅 URL，各自定时更新，只增不减） ----------------

    @staticmethod
    def make_subscription_id(url: str) -> str:
        return "sub_" + hashlib.md5(url.strip().encode()).hexdigest()[:10]

    def subscriptions(self) -> list[dict]:
        self.maybe_reload()
        with _LOCK:
            subs = self._cfg.get("subscriptions") or []
            return [dict(s) for s in subs if isinstance(s, dict)]

    def add_subscription(self, name: str, url: str, update_interval_h: int = 24,
                         builtin: bool = False,
                         standalone: bool = False) -> tuple[bool, str]:
        """新增一个订阅源。url 为空（纯内置）或 http(s)。重名/重 URL 拒绝。

        standalone=True：该订阅导入的苹果CMS源同时作为独立媒体库（后续每次
        定时拉取也遵守此设置）。
        csp_keys：该订阅下用户勾选的 csp 爬虫 key 列表。None 表示「还没选过」
        （定时拉取因此不导入任何 csp）；[] 表示「明确一个都不要」。"""
        name = (name or "").strip()
        url = (url or "").strip()
        if url and not url.startswith(("http://", "https://", "builtin:")):
            return False, "订阅地址需以 http(s):// 开头"
        if not name:
            name = "订阅源"
        try:
            interval = int(update_interval_h)
        except (TypeError, ValueError):
            interval = 24
        interval = max(1, min(interval, 24 * 7))  # 1h ~ 7天
        with _LOCK:
            cfg = self.get()
            subs = cfg.get("subscriptions") or []
            sid = self.make_subscription_id(url) if url else "sub_" + hashlib.md5(
                name.encode()).hexdigest()[:10]
            if any(s.get("id") == sid for s in subs):
                return False, "该订阅已存在"
            if url and any((s.get("url") or "").strip() == url for s in subs):
                return False, "该订阅地址已存在"
            subs.append({
                "id": sid, "name": name, "url": url, "builtin": bool(builtin),
                "standalone": bool(standalone),
                "update_interval_h": interval, "enabled": True,
                "last_update": 0, "last_status": "", "fail_count": 0,
                "last_error": "",
                # None=未选过（定时拉取不导入 csp）；[]=明确全不选
                "csp_keys": None,
            })
            cfg["subscriptions"] = subs
            self._persist(cfg)
            return True, sid

    def update_subscription(self, sid: str, *, name: str | None = None,
                            url: str | None = None,
                            update_interval_h: int | None = None,
                            enabled: bool | None = None,
                            csp_keys=_UNSET) -> tuple[bool, str]:
        """编辑订阅源：改名/改址/改间隔/启停/勾选的 csp 源。

        csp_keys 用哨兵默认值区分「没传」（保持原值）与「显式传 []」（一个都不要）。
        """
        with _LOCK:
            cfg = self.get()
            subs = cfg.get("subscriptions") or []
            target = next((s for s in subs if s.get("id") == sid), None)
            if not target:
                return False, "订阅不存在"
            if name is not None:
                nm = str(name).strip()
                if nm:
                    target["name"] = nm
            if url is not None:
                u = str(url).strip()
                if u and not u.startswith(("http://", "https://", "builtin:")):
                    return False, "订阅地址需以 http(s):// 开头"
                if u and any((s.get("url") or "").strip() == u
                             for s in subs if s.get("id") != sid):
                    return False, "该订阅地址已存在"
                target["url"] = u
            if update_interval_h is not None:
                try:
                    interval = int(update_interval_h)
                except (TypeError, ValueError):
                    interval = 24
                target["update_interval_h"] = max(1, min(interval, 24 * 7))
            if enabled is not None:
                target["enabled"] = bool(enabled)
            if csp_keys is not _UNSET:
                target["csp_keys"] = (None if csp_keys is None else
                                      [str(k) for k in csp_keys
                                       if str(k).strip()])
            cfg["subscriptions"] = subs
            self._persist(cfg)
            return True, "ok"

    def remove_subscription(self, sid: str) -> tuple[bool, str]:
        """删除订阅源（不删除它已导入的源；只停止后续更新）。"""
        with _LOCK:
            cfg = self.get()
            subs = cfg.get("subscriptions") or []
            if not any(s.get("id") == sid for s in subs):
                return False, "订阅不存在"
            cfg["subscriptions"] = [s for s in subs if s.get("id") != sid]
            self._persist(cfg)
            return True, "ok"

    def purge_subscription_sources(self, sid: str) -> int:
        """清空某个订阅此前导入的全部源（仅按 sub_id 精确匹配，内置源永不涉及）。

        用于「改了订阅地址后重新导入」：旧地址的源已无意义，清掉避免新旧混杂。
        返回被删除的源数量。其他订阅/自定义源不受影响。
        """
        with _LOCK:
            cfg = self.get()
            keep, removed = [], 0
            for s in cfg["sources"]:
                if s.get("sub_id") == sid and not s.get("builtin"):
                    removed += 1
                    continue
                keep.append(s)
            if removed:
                cfg["sources"] = keep
                self._persist(cfg)
            return removed

    def subscription_source_count(self, sid: str) -> int:
        """统计某个订阅当前名下的源数量（按 sub_id）。"""
        self.maybe_reload()
        with _LOCK:
            return sum(1 for s in self._cfg["sources"]
                       if s.get("sub_id") == sid and not s.get("builtin"))

    def mark_subscription_update(self, sid: str, ok: bool, error: str = "") -> None:
        """记录一次订阅拉取结果（更新 last_update/last_status/fail_count）。"""
        import time as _time
        with _LOCK:
            cfg = self.get()
            subs = cfg.get("subscriptions") or []
            target = next((s for s in subs if s.get("id") == sid), None)
            if not target:
                return
            target["last_update"] = int(_time.time())
            if ok:
                target["last_status"] = "ok"
                target["fail_count"] = 0
                target["last_error"] = ""
            else:
                target["last_status"] = "error"
                target["fail_count"] = int(target.get("fail_count", 0)) + 1
                target["last_error"] = str(error)[:200]
            cfg["subscriptions"] = subs
            self._persist(cfg, backup=False)

    # ---------------- 统一网络代理（A1，2026-11-12） ----------------

    _PROXY_PURPOSES = ("subscription", "source", "update", "media")

    @staticmethod
    def _valid_proxy_url(p: str) -> tuple[bool, str]:
        from urllib.parse import urlparse
        if not p:
            return True, "ok"
        u = urlparse(p)
        if u.scheme not in ("http", "https", "socks5", "socks5h") or not u.hostname:
            return False, "代理地址格式应为 http://user:pass@host:port（支持 socks5）"
        return True, "ok"

    def _legacy_proxy(self, cfg: dict) -> str:
        """老配置迁移：按用途优先级取第一个非空旧字段。"""
        for k in ("source_proxy", "subscription_proxy", "update_proxy"):
            v = str(cfg.get(k) or "").strip()
            if v:
                return v
        return ""

    def proxy_url(self) -> str:
        """统一代理地址（空串=直连）。新字段为空时自动回退老字段，老用户升级即生效。"""
        try:
            self.maybe_reload()
            cfg = self.get()
            u = str(cfg.get("proxy_url") or "").strip()
            return u or self._legacy_proxy(cfg)
        except Exception:  # noqa: BLE001
            return ""

    def proxy_for(self, purpose: str) -> str:
        """按用途返回应使用的代理地址；该用途未开启或没配地址时返回空串（=直连）。

        purpose: subscription / source / update / media
        """
        if purpose not in self._PROXY_PURPOSES:
            return ""
        try:
            cfg = self.get()
            use = cfg.get("proxy_use")
            if not isinstance(use, dict) or not use:
                # 老配置没有 proxy_use：只按老字段精确回退，避免把订阅代理误用到搜索
                key = {"subscription": "subscription_proxy",
                       "source": "source_proxy",
                       "update": "update_proxy",
                       "media": None}[purpose]
                return str(cfg.get(key) or "").strip() if key else ""
            if not bool(use.get(purpose)):
                return ""
            return self.proxy_url()
        except Exception:  # noqa: BLE001
            return ""

    def proxy_hosts(self, kind: str) -> set:
        """手动例外域名集合。kind: always（总走代理）/ never（永不代理）。"""
        key = "proxy_always_hosts" if kind == "always" else "proxy_never_hosts"
        try:
            cfg = self.get()
            return {str(x).strip().lower() for x in (cfg.get(key) or [])
                    if str(x).strip()}
        except Exception:  # noqa: BLE001
            return set()

    def set_proxy(self, url: str | None = None, use: dict | None = None,
                  always: list | str | None = None,
                  never: list | str | None = None) -> tuple[bool, str]:
        """保存统一代理配置。各参数为 None 时保持原值。

        - url：代理地址（空串=直连）
        - use：{subscription,source,update,media} 勾选
        - always/never：手动例外域名（列表或换行分隔字符串）
        """
        def _hosts(v):
            if v is None:
                return None
            items = v if isinstance(v, list) else str(v).replace(",", "\n").splitlines()
            out = []
            for it in items:
                h = str(it).strip().lower()
                if h and h not in out:
                    out.append(h)
            return out

        with _LOCK:
            cfg = self.get()
            if url is not None:
                p = str(url).strip()
                ok, msg = self._valid_proxy_url(p)
                if not ok:
                    return False, msg
                cfg["proxy_url"] = p
                # 写了统一地址后清空老字段，避免两处不一致
                for k in ("source_proxy", "subscription_proxy", "update_proxy"):
                    cfg[k] = ""
            if isinstance(use, dict):
                cur = dict(cfg.get("proxy_use") or {})
                for k in self._PROXY_PURPOSES:
                    if k in use:
                        cur[k] = bool(use[k])
                cfg["proxy_use"] = cur
            a = _hosts(always)
            if a is not None:
                cfg["proxy_always_hosts"] = a
                # 同一个域名不能同时在两个列表：always 优先
                nv = [h for h in (cfg.get("proxy_never_hosts") or []) if h not in a]
                cfg["proxy_never_hosts"] = nv
            n = _hosts(never)
            if n is not None:
                cfg["proxy_never_hosts"] = n
                av = [h for h in (cfg.get("proxy_always_hosts") or []) if h not in n]
                cfg["proxy_always_hosts"] = av
            self._persist(cfg)
            return True, "ok"

    def set_subscription_proxy(self, proxy_url: str) -> tuple[bool, str]:
        """保存「订阅资源抓取」专用代理（空串=直连）。仅用于订阅配置/jar/DRPY脚本，
        不影响播放与搜索。仅接受 http(s)/socks5 URL 形态。"""
        from urllib.parse import urlparse
        p = (proxy_url or "").strip()
        if p:
            u = urlparse(p)
            if u.scheme not in ("http", "https", "socks5", "socks5h") or not u.hostname:
                return False, "代理地址格式应为 http://user:pass@host:port"
        with _LOCK:
            cfg = self.get()
            cfg["subscription_proxy"] = p
            self._persist(cfg)
            return True, "ok"

    def set_source_proxy(self, proxy_url: str) -> tuple[bool, str]:
        """保存「资源站取源」专用代理（空串=直连），issue #30。
        仅用于搜索/分类/探测/详情；不影响订阅抓取与媒体播放。"""
        from urllib.parse import urlparse
        p = (proxy_url or "").strip()
        if p:
            u = urlparse(p)
            if u.scheme not in ("http", "https", "socks5", "socks5h") or not u.hostname:
                return False, "代理地址格式应为 http://user:pass@host:port"
        with _LOCK:
            cfg = self.get()
            cfg["source_proxy"] = p
            self._persist(cfg)
            return True, "ok"

    def set_source_proxy_mode(self, mode: str) -> tuple[bool, str]:
        """设置资源站代理模式：off/on/auto。"""
        m = str(mode or "auto").strip().lower()
        if m not in ("off", "on", "auto"):
            return False, "模式须为 off/on/auto"
        with _LOCK:
            cfg = self.get()
            cfg["source_proxy_mode"] = m
            self._persist(cfg)
            return True, "ok"

    def source_proxy_mode(self) -> str:
        try:
            m = str(self.get().get("source_proxy_mode") or "auto").strip().lower()
            return m if m in ("off", "on", "auto") else "auto"
        except Exception:
            return "auto"

    def main_ready_budget(self) -> float:
        """慢线起播预算（秒）：每次实时读配置并夹取 [0.8, 2.8]，非法值回落 2.4。

        配置加载是 DEFAULTS 深拷贝 + saved update，旧配置没有该键时
        自动取默认值 2.4，无需迁移。
        """
        try:
            return clamp_main_ready_budget(self.get().get(
                "main_ready_budget", MAIN_READY_BUDGET_DEFAULT))
        except Exception:
            return MAIN_READY_BUDGET_DEFAULT

    def set_standalone(self, sid: str, on: bool) -> tuple[bool, str]:
        with _LOCK:
            cfg = self.get()
            target = next((s for s in cfg["sources"] if s["id"] == sid), None)
            if not target:
                return False, "源不存在"
            if target.get("kind") == "t4" and on:
                return False, "T4 来源统一显示在「T4 选片」，无需逐插件建立独立库"
            target["standalone"] = bool(on)
            self._persist(cfg)
            return True, "ok"

    def pinned_lib_guids(self) -> set:
        with _LOCK:
            try:
                return set(self.get().get("pinned_libs") or [])
            except TypeError:
                return set()

    def set_lib_pinned(self, guid: str, on: bool) -> tuple[bool, str]:
        """置顶/取消置顶某个虚拟媒体库（guid）。"""
        if not isinstance(guid, str) or not guid:
            return False, "guid 非法"
        with _LOCK:
            cfg = self.get()
            cur = [g for g in (cfg.get("pinned_libs") or []) if isinstance(g, str)]
            if on:
                if guid not in cur:
                    cur.append(guid)
            else:
                cur = [g for g in cur if g != guid]
            cfg["pinned_libs"] = cur
            self._persist(cfg)
            return True, "ok"

    def set_loose_filter(self, sid: str, on: bool) -> tuple[bool, str]:
        """放宽正片（标题/备注噪音）过滤；分类层面的红线不受影响。"""
        with _LOCK:
            cfg = self.get()
            target = next((s for s in cfg["sources"] if s["id"] == sid), None)
            if not target:
                return False, "源不存在"
            target["loose_filter"] = bool(on)
            self._persist(cfg)
            return True, "ok"

    # >>> PRIVATE:ADULT
    def set_adult(self, sid: str, on: bool) -> tuple[bool, str]:
        """允许该源独立库收录合规成年分类；硬红线（涉未成年/非自愿）不受影响。"""
        if not features.ADULT:
            return False, "当前构建不支持"
        with _LOCK:
            cfg = self.get()
            target = next((s for s in cfg["sources"] if s["id"] == sid), None)
            if not target:
                return False, "源不存在"
            target["adult"] = bool(on)
            self._persist(cfg)
            return True, "ok"
    # <<< PRIVATE:ADULT

    def remove_source(self, sid: str) -> tuple[bool, str]:
        with _LOCK:
            cfg = self.get()
            target = next((s for s in cfg["sources"] if s["id"] == sid), None)
            if not target:
                return False, "源不存在"
            # 手动删除也写墓碑：删错了能在「已移除」里单条恢复。
            self._add_tombstone(cfg, target, "手动删除")
            if target.get("builtin"):
                # 内置源不物理删除，标记移除（可在重置时恢复）
                if sid not in cfg["removed_ids"]:
                    cfg["removed_ids"].append(sid)
            cfg["sources"] = [s for s in cfg["sources"] if s["id"] != sid]
            self._persist(cfg)
            return True, "ok"

    def restore_builtin(self) -> tuple[bool, str]:
        with _LOCK:
            cfg = self.get()
            cfg["removed_ids"] = []
            self._persist(cfg)
            self.load()
            return True, "ok"

    # ---------------- 定期体检（失效三次停用/删除） ----------------

    def sys_disable_source(self, sid: str, reason: str = "") -> tuple[bool, str]:
        """系统体检停用源：置 enabled=False 并打 sys_disabled 标记 + disabled_at。

        与用户手动停用（enabled=False 无标记）区分，恢复时系统可自动启用。
        disabled_at 用于「停用满 N 天自动删除」的计算，缺失时按当前时间兜底。
        """
        with _LOCK:
            cfg = self.get()
            target = next((s for s in cfg["sources"] if s["id"] == sid), None)
            if not target:
                return False, "源不存在"
            target["enabled"] = False
            target["sys_disabled"] = True
            target["disabled_at"] = time.time()
            if reason:
                target["disabled_reason"] = reason
            self._persist(cfg)
            return True, "ok"

    def sys_enable_source(self, sid: str) -> tuple[bool, str]:
        """系统恢复源（体检发现复活）：清除 sys_disabled 标记并启用。
        仅对系统停用的源生效；用户手动停用的源（无 sys_disabled）不动。"""
        with _LOCK:
            cfg = self.get()
            target = next((s for s in cfg["sources"] if s["id"] == sid), None)
            if not target:
                return False, "源不存在"
            if not target.get("sys_disabled"):
                return False, "该源非系统停用"
            target["enabled"] = True
            target["sys_disabled"] = False
            self._persist(cfg)
            return True, "ok"

    def apply_quality_batch(self, mapping: dict) -> None:
        """把一轮巡检结果批量写回源条目（只落盘一次，避免逐源写配置风暴）。

        mapping: {sid: {"quality": {...}, "verified": bool|None,
                        "fail_count": int, "last_check": ts, "last_ok": ts|None}}
        """
        if not isinstance(mapping, dict) or not mapping:
            return
        with _LOCK:
            cfg = self.get()
            changed = False
            by_id = {s["id"]: s for s in cfg["sources"]}
            for sid, patch in mapping.items():
                target = by_id.get(sid)
                if not target or not isinstance(patch, dict):
                    continue
                for k, v in patch.items():
                    if v is None:
                        continue
                    target[k] = v
                changed = True
            if changed:
                self._persist(cfg)

    def patrol_settings(self) -> dict:
        """巡检参数（每次读取都取当前配置，改完立即生效）。"""
        cfg = self.get()

        def num(key, default, lo, hi, cast=int):
            try:
                v = cast(cfg.get(key, default))
            except (TypeError, ValueError):
                v = default
            return max(lo, min(hi, v))

        return {
            "enabled": bool(cfg.get("patrol_enabled", True)),
            "interval_h": num("patrol_interval_h", 6, 0.1, 168, float),
            "disabled_interval_h": num("patrol_disabled_interval_h", 1, 0.1, 168, float),
            "retain_days": num("patrol_retain_days", 30, 0, 3650),
            "timeout_s": num("patrol_timeout_s", 25, 3, 120, float),
            "concurrency": num("patrol_concurrency", 4, 1, 16),
            "fail_threshold": num("patrol_fail_threshold", 3, 1, 20),
            "min_span_h": num("patrol_min_span_h", 12, 0, 240, float),
            "top_n": num("search_top_n", 40, 0, 2000),
        }

    def sys_remove_source(self, sid: str, reason: str = "") -> tuple[bool, str]:
        """系统/手动删除源（内置源也允许，但先写墓碑，可单条恢复）。

        不同于旧实现（内置源硬拒）：删掉的内置源会进 removed_ids，下次加载不再
        合并回来；墓碑保留 name/api/原因/最后巡检数据，「已移除」列表里能查、
        能单条恢复。这样内置源既不会变成改不了也删不掉的僵尸，也不是不可逆。
        """
        with _LOCK:
            cfg = self.get()
            target = next((s for s in cfg["sources"] if s["id"] == sid), None)
            if not target:
                return False, "源不存在"
            self._add_tombstone(cfg, target, reason)
            if target.get("builtin") and sid not in cfg["removed_ids"]:
                cfg["removed_ids"].append(sid)
            cfg["sources"] = [s for s in cfg["sources"] if s["id"] != sid]
            self._persist(cfg)
            return True, "ok"

    def _add_tombstone(self, cfg: dict, target: dict, reason: str = "") -> None:
        """把源记录写进墓碑（按 id 去重，最多保留 500 条，新的在前）。"""
        tombstones = [t for t in (cfg.get("removed_sources") or [])
                      if isinstance(t, dict) and t.get("id") != target.get("id")]
        tombstones.insert(0, {
            "id": target.get("id"),
            "name": target.get("name"),
            "api": target.get("api"),
            "kind": target.get("kind") or ("live" if target.get("live") else ""),
            "builtin": bool(target.get("builtin")),
            "sub_id": target.get("sub_id") or "",
            "reason": reason or target.get("disabled_reason") or "",
            "deleted_at": time.time(),
            "last_ok": target.get("last_ok"),
            "last_check": target.get("last_check"),
            "quality": target.get("quality") or {},
        })
        cfg["removed_sources"] = tombstones[:500]

    def removed_sources(self) -> list[dict]:
        """墓碑列表（新的在前）。"""
        self.maybe_reload()
        with _LOCK:
            return [dict(t) for t in (self._cfg.get("removed_sources") or [])
                    if isinstance(t, dict)]

    def restore_removed(self, sid: str) -> tuple[bool, str]:
        """从墓碑恢复单个源。内置源摘掉 removed_ids 后由加载逻辑从代码合并回来；
        订阅/自定义源按墓碑记录原样插回（id 不变，置顶/优先级仍有效）。"""
        sid = (sid or "").strip()
        if not sid:
            return False, "缺少源 id"
        with _LOCK:
            cfg = self.get()
            tomb = next((t for t in (cfg.get("removed_sources") or [])
                         if isinstance(t, dict) and t.get("id") == sid), None)
            if not tomb:
                return False, "墓碑里没有这个源"
            cfg["removed_sources"] = [t for t in (cfg.get("removed_sources") or [])
                                      if not (isinstance(t, dict) and t.get("id") == sid)]
            if tomb.get("builtin"):
                cfg["removed_ids"] = [x for x in (cfg.get("removed_ids") or [])
                                      if x != sid]
                self._persist(cfg)
                self.load()
                return True, "ok"
            if any(s.get("id") == sid for s in cfg["sources"]):
                self._persist(cfg)
                return True, "already"
            cfg["sources"].append({
                "id": sid,
                "name": tomb.get("name") or sid,
                "api": tomb.get("api") or "",
                "enabled": True,
                "standalone": False,
                "builtin": False,
                "sub_id": tomb.get("sub_id") or "",
                "verified": None,
            })
            self._persist(cfg)
            return True, "ok"

    @staticmethod
    def normalize_keywords(raw) -> list[str]:
        """把逗号/换行/空白分隔的输入清洗成去重关键词列表。"""
        if isinstance(raw, str):
            parts = re.split(r"[,，、\n\r;；\s]+", raw)
        elif isinstance(raw, (list, tuple)):
            parts = []
            for item in raw:
                parts.extend(re.split(r"[,，、\n\r;；\s]+", str(item)))
        else:
            return []
        out, seen = [], set()
        for p in parts:
            k = p.strip()
            if k and k not in seen and len(k) <= 30:
                seen.add(k)
                out.append(k)
        return out

    def set_junk_keywords(self, raw) -> list[str]:
        kws = self.normalize_keywords(raw)
        self.save({"junk_keywords": kws})
        return kws

    def reset_junk_keywords(self) -> list[str]:
        self.save({"junk_keywords": list(JUNK_KEYWORDS)})
        return list(JUNK_KEYWORDS)

    # ---- 内容红线关键词（后台可增删改，命中即整条丢弃）----
    def set_redline_keywords(self, raw) -> list[str]:
        """保存完整红线词表（去重、去空、限长）；传空列表即不再拦截。"""
        kws = self.normalize_keywords(raw)
        self.save({"redline_keywords": kws})
        return kws

    def reset_redline_keywords(self) -> list[str]:
        self.save({"redline_keywords": list(REDLINE_KEYWORDS)})
        return list(REDLINE_KEYWORDS)

    def redline_keywords(self) -> tuple[str, ...]:
        """当前生效的红线词表。空元组代表用户已清空（不做拦截）。

        配置缺键/类型不对时退回出厂词表——宁可多拦，也不因配置损坏全线放行。
        结果按元组缓存，`load()`/`_persist()` 会清空缓存，改配置即时生效。
        """
        cached = self._redline_cache
        if cached is not None:
            return cached
        raw = self._cfg.get("redline_keywords")
        if not isinstance(raw, (list, tuple)):
            kws: tuple[str, ...] = tuple(REDLINE_KEYWORDS)
        else:
            kws = tuple(str(k).strip() for k in raw if str(k).strip())
        self._redline_cache = kws
        return kws

    def is_junk(self, title: str, remarks: str = "") -> bool:
        if not self._cfg.get("filter_junk", True):
            return False
        kws = self._cfg.get("junk_keywords", JUNK_KEYWORDS)
        hay = f"{title} {remarks}"
        return any(k and k in hay for k in kws)


config = Config()
