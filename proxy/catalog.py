#!/usr/bin/env python3
"""海量分类片库（方案 C）：直接按资源站苹果 CMS 分类深翻。

与豆瓣“热榜”（charts.py，精选 ~160 部、点击才搜源）不同，这里面向“无限浏览”：
- 启动/定时拉取各资源站的分类表 ac=list，把所有子分类按名称归并为
  movie / tv / anime / variety 四个标准大类；
- 每个大类选一个“规范源”（可深翻、页数多、稳定），浏览时按页直连
  ac=detail&t=<子类id>&pg=N，每页 20 条且自带播放地址，点开即可播，无需再搜索；
- 页级短 TTL 内存缓存 + 单 flight；规范源某页失败自动回退到备选源；
- 条目 guid 绑定 (源, vod_id)，brief 入库后复用既有详情/选集/播放/续播链。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

import httpx

import netutil
import providers
from store import make_guid

_log = logging.getLogger(__name__)

PAGE_SIZE = 20
# 每个大类最多可浏览的页数（页数×20=条数）。250 页 ≈ 5000 部。
MAX_PAGES = 250
# P1-17：分类深翻有界并发度。一批最多同时在途这么多页；CMS 源多为
# 慢源/共享带宽，并发度取 4 既能把逐页等待折叠，又不至于冲垮源站。
DEEP_FETCH_CONCURRENCY = 4
CLASS_REFRESH = 6 * 3600
PAGE_TTL = 10 * 60
# 空页（多为慢源抖动超时）只短缓存，避免该页被长时间冻结
EMPTY_TTL = 15
HTTP_TIMEOUT = 12.0
# 非苹果 CMS 源（drpy / csp 爬虫）多数不回 pagecount，用这个估算值兜底：
# 翻到末页时返回空列表，_extend_dense 连续 3 个空页就停，不会真翻 30 页。
XSRC_DEFAULT_PAGES = 30
# 非 CMS 源的分类表要走 sidecar 再去源站首页，慢源首访常超 12 秒（实测
# csp 爬虫首拉 0.6~12s+）。建库是一次性动作，给宽一点的 25 秒，别让首访抖动
# 直接把独立库判成「没有分类」。
XSRC_CLASS_TIMEOUT = 25.0
# 建库阶段「每个源」的硬预算（秒）。auto 代理模式下直连失败会切代理重试一次，
# 单请求 HTTP_TIMEOUT=12 实际能跑到 24 秒；而每轮 gather 都在等最慢的那一个，
# 于是 27 个连不上的源把整轮拖成 90 秒（实测 A 段 24.8s / B1 35.4s / B2 30s，
# 而 40/82 个源 3 秒内就返回了）。建库是尽力而为的冷启动动作：超预算的源
# 记为「本次没有分类表」，等下一轮刷新或用户真正翻到它时再补，不让单点拖全局。
BUILD_BUDGET = 10.0
# >>> OPTIONAL:DISK CACHE
# 分类表 + 路由落盘：重启后先按上次结果出库（秒开），后台再按 CLASS_REFRESH 刷。
# 体积实测约 160 KB（82 源 / 1591 条分类 / 400 余条路由），同目录 titles.json 已 3.8 MB。
#
# 【这一层可整块摘除，不影响建库本身】
#   - 只关不删：DISK_CACHE 改 False，立刻回到「每次启动重新建库」的旧行为；
#   - 彻底删除：删掉所有 OPTIONAL:DISK CACHE 标记块 + state/catalog_routes.json
#     + tests/test_catalog_disk.py，删完顺带可删 json / os / Path 三个 import 与
#     _log（它们只服务这层）。refresh_routes 的探测与选源逻辑一行都没动，
#     摘掉缓存 = 只是回到「冷启动等一轮」，不会少任何一个库。
# 之所以做成可摘除的独立层：若以后改用更轻的浏览方案（例如像 LunaTV 那样分类
# 只用豆瓣榜单、点开再搜源），这层可连同建库整块删掉，不留半截持久化依赖。
DISK_CACHE = True
_DISK_FILE = Path(__file__).resolve().parent.parent / "state" / "catalog_routes.json"
DISK_VERSION = 1
# <<< OPTIONAL:DISK CACHE


def _src_kind(source: dict) -> str:
    """源真实类型（统一口径见 config.source_type）。"""
    try:
        import config as _cfg
        return _cfg.source_type(source)
    except Exception:  # noqa: BLE001
        return "cms"


def _xsrc_fn(kind: str, name: str):
    """取 providers 里某私有类型的独立库适配函数。

    适配函数写在 providers 的 PRIVATE 块里：公开构建剥离后这里取到 None，
    调用方按「该类型不支持」降级，不会因缺符号崩。
    """
    return getattr(providers, f"_xsrc_{name}_{kind}", None)


def _map_vod_any(v: dict, source: dict):
    """按源类型把列表项映射成 OnlineTitle（苹果 CMS / drpy / csp 各有各的字段）。"""
    kind = _src_kind(source)
    if kind in ("drpy", "spider"):
        fn = _xsrc_fn(kind, "map")
        if fn is not None:
            return fn(source, v)
        return None
    return providers._map_vod(v, source)

# 大类 -> 展示名 / MediaDB category
CATALOG_CATEGORIES = [
    ("movie", "🎞海量电影", "Movie"),
    ("tv", "📺海量剧集", "TV"),
    ("anime", "🌸海量动漫", "Mix"),
    ("variety", "🎤海量综艺", "Mix"),
    ("documentary", "🎞纪录片", "Movie"),
    ("uhd", "📽4K影视", "Movie"),
    ("shortdrama", "📱热门短剧", "TV"),
    # P2 新增：海外电视剧、AI漫剧，跟随海量主开关，不设独立开关。
    ("overseas", "🌍海外剧集", "TV"),
    ("ai_comic", "🤖AI漫剧", "Mix"),
    # P2 追加：TED学习，跟随海量主开关，不设独立开关。
    ("ted", "🎓TED学习", "Mix"),
]

# 跨源聚合的“海量四库”使用的保留作用域 id；单源独立库作用域为其 source id。
AGG_SCOPE = "__agg__"
CATS = [c for c, _, _ in CATALOG_CATEGORIES]

# 名称关键字（匹配 type_name 子串）。顺序：动漫/综艺优先于“电影/剧”判定。
_ANIME_KW = ("动漫", "动画", "番剧", "卡通")
_VARIETY_KW = ("综艺", "真人秀", "脱口秀", "娱乐")
_DOC_KW = ("纪录片", "纪录", "记录", "纪实")
_UHD_KW = ("4k", "杜比视界", "dolby", "uhd", "超高清", "蓝光", "bluray")
# 片名级 4K 判定（标题/备注里的画质信号）：比分类名多认 2160 与 HDR。
# 注意不含“超清”——太多源把所有片都标超清，放进来 4K 库就等于全库。
_UHD_TITLE_KW = _UHD_KW + ("2160", "hdr")


def is_uhd_title(title: str, remarks: str) -> bool:
    """片名/备注是否带 4K 画质信号（4K/2160/UHD/杜比/HDR/蓝光/超高清）。"""
    s = f"{title or ''} {remarks or ''}".lower()
    return any(k in s for k in _UHD_TITLE_KW)
_SHORT_KW = ("短剧", "微短剧", "剧场")
# P2 新增：AI漫剧（先于 _EXCLUDE_KW 判定，见 classify），海外剧（先于普通剧集判定）。
_AI_COMIC_KW = ("AI漫剧", "AI动漫", "AI漫画", "动态漫画", "漫剧")
# P2 追加：TED学习（大小写不敏感，用小写匹配，见 classify）。
_TED_KW = ("ted", "ted演讲", "tedx", "ted-ed", "ted演讲")
_OVERSEAS_KW = ("海外剧", "美剧", "英剧", "韩剧", "日剧", "泰剧",
                "港台剧", "欧美剧", "海外电视剧",
                # 源站常把「港台剧」拆成具体地名写，之前一个都认不出来，
                # 结果「香港剧/韩国剧」这类正常分类被当垃圾丢掉（见 2026-11-12 实测）。
                "香港剧", "台湾剧", "韩国剧", "日本剧", "泰国剧",
                "新加坡剧", "马来剧", "印度剧", "土耳其剧")
# 成人向分类名：显式导向 ADULT 福利分类，绝不落入常规电影/剧集类。
# 硬红线（_REDLINE_KW）在 is_redline 中先于本表拦截，任何开关都不放行。
# 词表参照 LunaTV src/lib/yellow.ts，并按国内源站实际分类名补了「理论/三级/
# 无码/有码/国产传媒/黑料吃瓜」等——这些名字不含「福利/伦理」二字，光靠
# 原表认不出来，只能靠源级 18 标记兜底（见 classify_source_class）。
# 英文词用小写写，匹配时对 n.lower() 做（Cosplay / SWAG 都能认）。
_ADULT_KW = ("福利", "倫理", "伦理", "情色", "午夜", "写真", "里番", "成人",
             "理论", "三级", "无码", "有码", "门事件", "国产传媒", "传媒",
             "网红主播", "黑丝", "制服", "色情", "同性片", "热舞",
             "自拍", "偷拍", "探花", "吃瓜", "黑料", "swag", "cosplay")
# 明确非成人：即使该源打了「福利分类」标记（adult=true），下面这些分类也
# 绝不进福利库。否则给一个综合站打上 18，会把体育和港台日韩剧全倒进福利库
# ——2026-11-12 实测暴风资源 49 个分类里，打 18 后 17 个是这种误伤。
_NON_ADULT_KW = (
    # 体育项目：源站直接用品类名，不含「体育」二字，逃得过 _EXCLUDE_KW
    "足球", "篮球", "网球", "排球", "乒乓", "羽毛球", "台球", "斯诺克",
    "棒球", "冰球", "高尔夫", "赛车", "拳击", "格斗", "ufc", "nba",
    "世界杯", "奥运", "亚运", "英超", "西甲", "意甲", "德甲", "中超",
    "电竞", "游戏解说",
    # 常规类型：正常应由 classify 收走，落到这里说明是没认出来的写法
    "综艺", "真人秀", "脱口秀", "动漫", "动画", "番剧", "卡通",
    "纪录片", "纪实", "短剧", "微短剧", "少儿", "儿童", "亲子",
    "戏曲", "相声", "小品", "曲艺", "公开课", "教程", "网课",
)
# 电影：电影/影片/各类型“XX片”且非剧非动漫；剧：电视/连续剧/美剧/国产剧…
_MOVIE_KW = ("电影", "影片")
_TV_KW = ("电视", "连续剧", "剧集", "美剧", "英剧", "韩剧", "日剧", "泰剧",
          "国产剧", "港台剧", "海外剧", "番剧")
# 明确排除的脏类（伦理/福利/资讯/解说/预告/体育/短剧等）
# 注意：这里必须列全「XX片」形态的成人分类——classify 末尾有
# 「以『片』结尾且 ≤4 字 → movie」的兜底，漏一个就会让成人分类漏进电影库
# （实测「伦理片/情色片/三级片/成人片」全被判成 movie）。
_EXCLUDE_KW = ("理论", "福利", "写真", "午夜", "伦理", "倫理", "情色",
               "成人", "三级", "同性",
               "解说", "预告",
               "花絮", "资讯", "新闻", "体育", "赛事", "漫剧", "AI",
               "演员", "音乐", "MV")

# 硬红线：任何开关都不能放开。涉未成年人性化、非自愿（换脸/迷奸/强奸/偷拍等）。
# 同时作用于“分类名”和条目标题/备注，命中即丢弃，聚合库、独立库、搜索一律不收录。
_REDLINE_KW = (
    # 未成年人
    "萝莉", "童贞",
    # 非自愿 / 性暴力
    "换脸"
)
# 即便开启福利分类，也仍按“非影视/中性噪音”排除的分类
_NEUTRAL_KW = ("解说", "预告", "花絮", "资讯", "新闻", "体育", "赛事",
               "演员", "音乐", "MV", "直播", "电台")

# 福利分类使用的内部 cat id（不进入 CATS，故永不进入跨源聚合的四个海量库）
ADULT_CAT = "adult"

try:
    import features
except Exception:  # 兜底：独立运行/剥离异常时按公开（关闭）处理
    features = None


def _welfare_on() -> bool:
    """扩展（成人）资源库独立开关（管理页可调，默认关）。

    与构建期 features.ADULT（是否编译进本包的能力）是两个维度：
    - features.ADULT=False：完整能力未编译，任何开关都不放行；
    - welfare_library_enabled=False：独立开关关闭，不建路由、不抓取、不显示。
    开启后还需该源显式勾选「福利分类」(source.adult=true) 才会实际建库。
    """
    if not getattr(features, "ADULT", False):
        return False
    try:
        from config import config as _cfg
        return bool(_cfg.get().get("welfare_library_enabled", False))
    except Exception:
        return False


# ==================== 本地分类规则库 ====================
# 一份规则，所有源共用：分类名 -> 标准库 id（cat=None 表示「确认丢弃」）。
#
# 为什么要这一层：源站分类名五花八门。2026-11-12 在 246 实测 50 个连通源、
# 1926 条分类，其中 509 条（26%）内置关键词表判不出来、被静默丢掉；未归类
# 的不同名字有 156 个。靠代码里加词永远追不上，而且每加一个都得改代码发版。
# 这里把「分类名 -> 库」外置成一份文件：
#   - 所有源共用同一套答案，同一个分类名在哪个源都归同一个库；
#   - 认不出的名字进「未归类池」，管理页可见，点一下归类就写回本文件，
#     下次所有源同时生效；
#   - 内置默认规则不写入文件，升级能带着新规则走，用户改过的条目以文件为准。
#
# 与内置关键词表的关系：规则优先级更高（除红线外），因为规则是「明确意志」；
# 规则没命中才轮到 classify() 的关键词猜。cat=None 与「没认出来」是两回事：
# 前者不再进未归类池，也不会被源级 18 兜底捞回福利库。
_RULES_FILE = Path(__file__).resolve().parent.parent / "state" / "classify_rules.json"
RULES_VERSION = 1

# 默认规则，全部取自 246 实测的未归类名，只收实际观测到的、跨源通用的叫法。
# 每条 (分类名, 目标库 或 None=丢弃, 是否精确匹配)。
_DEFAULT_RULES: tuple[tuple[str, str | None, bool], ...] = (
    # —— 体育：源站直接用品类名，不含「体育」二字，内置表认不出；没有对应媒体库 ——
    ("足球", None, True), ("篮球", None, True), ("网球", None, True),
    ("排球", None, True), ("乒乓球", None, True), ("羽毛球", None, True),
    ("台球", None, True), ("斯诺克", None, True), ("棒球", None, True),
    ("冰球", None, True), ("高尔夫", None, True), ("赛车", None, True),
    ("拳击", None, True), ("格斗", None, True), ("ufc", None, True),
    ("nba", None, True), ("cba", None, True), ("世界杯", None, True),
    ("奥运", None, True), ("亚运", None, True), ("英超", None, True),
    ("西甲", None, True), ("意甲", None, True), ("德甲", None, True),
    ("中超", None, True), ("体育赛事", None, True), ("其他赛事", None, True),
    ("体育", None, True), ("赛事", None, True), ("电竞", None, True),
    ("网游", None, True), ("单机", None, True), ("手游", None, True),
    ("游戏解说", None, True),
    # —— 噪点：栏目、公告、解说、预告，没有对应媒体库 ——
    ("预告片", None, True), ("预告", None, True), ("花絮", None, True),
    ("资讯", None, True), ("新闻资讯", None, True), ("公告", None, True),
    ("头条", None, True), ("影视解说", None, True), ("电影解说", None, True),
    ("剧情介绍", None, True), ("未分类", None, True), ("中文字幕", None, True),
    ("精品推荐", None, True), ("家庭篇", None, True), ("ai制作", None, True),
    ("演唱会", None, True), ("儿童儿歌", None, True), ("科普学习", None, True),
    # 涉暴力内容，任何库都不收（红线词表拦不到这一类名字）
    ("强奸乱伦", None, True),
    # —— 短剧子类：名字全是题材，光看名字判不出类型 ——
    ("古装仙侠", "shortdrama", True), ("反转爽剧", "shortdrama", True),
    ("现代都市", "shortdrama", True), ("脑洞悬疑", "shortdrama", True),
    ("重生民国", "shortdrama", True), ("穿越年代", "shortdrama", True),
    ("年代穿越", "shortdrama", True), ("女频恋爱", "shortdrama", True),
    ("言情总裁", "shortdrama", True), ("成长逆袭", "shortdrama", True),
    ("现代言情", "shortdrama", True), ("反转爽文", "shortdrama", True),
    ("女恋总裁", "shortdrama", True), ("闪婚离婚", "shortdrama", True),
    ("都市脑洞", "shortdrama", True),
    # 「爽剧」是短剧站最通用的后缀，放宽成关键词（如「战神爽剧」）
    ("爽剧", "shortdrama", False),
    # —— 剧集别名（内置表只认「国产剧」这种写法）——
    ("内地剧", "tv", True), ("大陆剧", "tv", True),
    ("音乐片", "movie", True),          # 被内置表「音乐」误伤，其实是电影类型
    # —— 海外剧集 ——
    ("港剧", "overseas", True), ("台剧", "overseas", True),
    ("港澳剧", "overseas", True), ("美国剧", "overseas", True),
    ("东南亚剧", "overseas", True),
    ("netflix", "overseas", False),     # Netflix 自制剧 / Netflix 电影
    # —— 成人站分类名：一个字都不沾「福利/伦理」，只能靠规则点名 ——
    ("视频一区", "adult", True), ("视频二区", "adult", True),
    ("视频三区", "adult", True), ("国产精品", "adult", True),
    ("欧美精品", "adult", True), ("日本精品", "adult", True),
    ("两性课堂", "adult", True), ("成人动漫", "adult", True),
    ("国产传媒", "adult", True), ("女同性恋", "adult", True),
    ("男同性恋", "adult", True),
)

_rules_lock = threading.RLock()
# (精确名 dict, 关键词按长度降序的元组)；None 表示尚未加载
_rules_cache: tuple[dict[str, str | None],
                    tuple[tuple[str, str | None], ...]] | None = None


def _read_rule_file() -> list[dict]:
    """只读用户规则（默认规则不落盘，随版本走）。"""
    try:
        data = json.loads(_RULES_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except Exception as exc:  # noqa: BLE001
        _log.warning("classify rules read failed: %s", exc)
        return []
    rules = data.get("rules") if isinstance(data, dict) else None
    return [r for r in (rules or []) if isinstance(r, dict)]


def _write_rule_file(rules: list[dict]) -> bool:
    try:
        _RULES_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _RULES_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(
            {"v": RULES_VERSION, "updated": int(time.time()), "rules": rules},
            ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, _RULES_FILE)
        return True
    except Exception as exc:  # noqa: BLE001
        _log.warning("classify rules write failed: %s", exc)
        return False


def _rule_index():
    """合并默认规则与用户规则，建索引。用户同 key 同 exact 覆盖默认。"""
    global _rules_cache
    with _rules_lock:
        if _rules_cache is not None:
            return _rules_cache
        exact: dict[str, str | None] = {}
        kw: dict[str, str | None] = {}
        for k, cat, is_exact in _DEFAULT_RULES:
            (exact if is_exact else kw)[k.lower()] = cat
        for r in _read_rule_file():
            k = str(r.get("k") or "").strip().lower()
            if not k:
                continue
            cat = r.get("cat")
            cat = str(cat) if cat is not None else None
            (exact if bool(r.get("exact", True)) else kw)[k] = cat
        _rules_cache = (exact, tuple(sorted(kw.items(), key=lambda kv: -len(kv[0]))))
        return _rules_cache


def rule_hit(name: str) -> tuple[bool, str | None]:
    """查规则。返回 (是否命中, 目标库)；命中且目标为 None = 确认丢弃。

    精确名优先于关键词；关键词取最长者（「成人动漫」不会被「动漫」抢走，
    虽然内置表里没有「动漫」这条规则，但用户可能自己加）。
    """
    n = (name or "").strip().lower()
    if not n:
        return (False, None)
    exact, kws = _rule_index()
    if n in exact:
        return (True, exact[n])
    for k, cat in kws:
        if k and k in n:
            return (True, cat)
    return (False, None)


def reload_rules() -> None:
    """规则改动后清缓存，下次建库即生效。"""
    global _rules_cache
    with _rules_lock:
        _rules_cache = None


def set_rule(name: str, cat: str | None, exact: bool = True) -> tuple[bool, str]:
    """新增/覆盖一条用户规则。cat=None 表示确认丢弃。"""
    k = (name or "").strip()
    if not k:
        return False, "分类名不能为空"
    if len(k) > 40:
        return False, "分类名过长"
    if cat is not None and cat not in all_cats() and cat != ADULT_CAT:
        return False, "未知分类：%s" % cat
    rules = [r for r in _read_rule_file()
             if not (str(r.get("k") or "").strip().lower() == k.lower()
                     and bool(r.get("exact", True)) == bool(exact))]
    rules.append({"k": k, "cat": cat, "exact": bool(exact)})
    if not _write_rule_file(rules):
        return False, "写入失败（检查 state 目录权限）"
    reload_rules()
    return True, "已保存"


def del_rule(name: str, exact: bool = True) -> tuple[bool, str]:
    """删除一条用户规则，回落到默认规则（若默认里也有同名条目）。"""
    k = (name or "").strip().lower()
    if not k:
        return False, "分类名不能为空"
    rules = _read_rule_file()
    kept = [r for r in rules
            if not (str(r.get("k") or "").strip().lower() == k
                    and bool(r.get("exact", True)) == bool(exact))]
    if len(kept) == len(rules):
        return False, "没有这条自定义规则（默认规则不可删除，可直接覆盖）"
    if not _write_rule_file(kept):
        return False, "写入失败（检查 state 目录权限）"
    reload_rules()
    return True, "已恢复默认"


# ---------- 自定义虚拟库（用户手动新增/改名/删除的分类） ----------
# 存 state/custom_cats.json：[{"id","title","mdb"}]。id 参与路由键与库 guid
# （md5("fnmedia-catalog-lib:<id>")），必须 [a-z0-9]；mdb 决定客户端按
# 电影/剧集/综合哪种翻页视图取片。内置 CATALOG_CATEGORIES 不动，自定义库
# 永远追加在后面，保证固定库的 guid/顺序/契约一个都不变。

_CUSTOM_MDB = ("Movie", "TV", "Mix")
_CUSTOM_CATS_FILE = (Path(__file__).resolve().parent.parent / "state"
                     / "custom_cats.json")


def _read_custom_cats() -> list[dict]:
    try:
        data = json.loads(_CUSTOM_CATS_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except Exception as exc:  # noqa: BLE001
        _log.warning("custom cats read failed: %s", exc)
        return []
    out = []
    for c in (data.get("cats") if isinstance(data, dict) else None) or []:
        if not isinstance(c, dict):
            continue
        cid = str(c.get("id") or "").strip()
        title = str(c.get("title") or "").strip()
        mdb = c.get("mdb") if c.get("mdb") in _CUSTOM_MDB else "Mix"
        if cid and title:
            out.append({"id": cid, "title": title, "mdb": mdb})
    return out


def _write_custom_cats(cats: list[dict]) -> bool:
    try:
        _CUSTOM_CATS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _CUSTOM_CATS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(
            {"v": 1, "updated": int(time.time()), "cats": cats},
            ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, _CUSTOM_CATS_FILE)
        return True
    except Exception as exc:  # noqa: BLE001
        _log.warning("custom cats write failed: %s", exc)
        return False


def custom_cats() -> list[dict]:
    """自定义库列表（管理页 CRUD 与分类规则下拉都用它）。"""
    return _read_custom_cats()


def all_cats() -> list[str]:
    """建库/路由用的完整分类池：固定 10 库 + 用户自定义库。"""
    return CATS + [c["id"] for c in _read_custom_cats()]


def iter_categories() -> list[tuple[str, str, str]]:
    """固定 + 自定义的完整分类表：(cat, 展示名, MediaDB category)。

    客户端建库、计数、预热等原来遍历 CATALOG_CATEGORIES 的地方一律换成
    本函数，自定义库才能和固定库走同一条链路。
    """
    return list(CATALOG_CATEGORIES) + [
        (c["id"], c["title"], c["mdb"]) for c in _read_custom_cats()]


def set_custom_cat(title: str, mdb: str = "Mix",
                   cat_id: str | None = None) -> tuple[bool, str, str | None]:
    """新增（cat_id=None）或改名/改归属。返回 (ok, msg, id)。"""
    title = (title or "").strip()
    if not title:
        return False, "库名不能为空", None
    if len(title) > 20:
        return False, "库名过长（≤20 字）", None
    if mdb not in _CUSTOM_MDB:
        return False, "内容归属必须是 Movie/TV/Mix", None
    cats = _read_custom_cats()
    if any(c["title"] == title for c in cats):
        return False, "已有同名库", None
    if cat_id:
        hit = [c for c in cats if c["id"] == cat_id]
        if not hit:
            return False, "库不存在", None
        hit[0]["title"] = title
        hit[0]["mdb"] = mdb
    else:
        base = re.sub(r"[^a-z0-9]", "", title.lower())[:16] or "lib"
        cid, n = base, 2
        exist = {c["id"] for c in cats} | set(CATS) | {ADULT_CAT}
        while cid in exist:
            cid = "%s%d" % (base, n)
            n += 1
        cats.append({"id": cid, "title": title, "mdb": mdb})
        cat_id = cid
    if not _write_custom_cats(cats):
        return False, "写入失败（检查 state 目录权限）", None
    return True, "已保存", cat_id


def del_custom_cat(cat_id: str) -> tuple[bool, str]:
    cats = _read_custom_cats()
    kept = [c for c in cats if c["id"] != cat_id]
    if len(kept) == len(cats):
        return False, "库不存在"
    if not _write_custom_cats(kept):
        return False, "写入失败（检查 state 目录权限）"
    return True, "已删除"


def rules_snapshot() -> dict:
    """给管理页：有效规则统计 + 用户自定义规则列表。"""
    exact, kws = _rule_index()
    defaults = {(k.lower(), e) for k, _c, e in _DEFAULT_RULES}
    users = []
    for r in _read_rule_file():
        k = str(r.get("k") or "").strip()
        if not k:
            continue
        e = bool(r.get("exact", True))
        users.append({"k": k, "cat": r.get("cat"), "exact": e,
                      "overrides_default": (k.lower(), e) in defaults})
    return {
        "file": str(_RULES_FILE),
        "file_exists": _RULES_FILE.exists(),
        "exact": len(exact),
        "keyword": len(kws),
        "default_total": len(_DEFAULT_RULES),
        "user": users,
    }


def is_redline(*texts: str) -> bool:
    for t in texts:
        if not t:
            continue
        for k in _REDLINE_KW:
            if k and k in t:
                return True
    return False


# 源名专用成人特征词（比分类词表更严：打错 18 会让该源从全局搜索里消失，
# 所以只放「基本不可能出现在正经站名里」的词）。
_ADULT_SRC_KW = ("🔞", "porn", "jav", "91", "avzy", "麻豆", "糖心", "海角",
                 "含羞草", "swag", "hanime", "女优", "无码", "福利")


def looks_adult_source(name: str) -> bool:
    """按源名判断是否成人源（订阅导入时自动标 18 用）。

    保守优先：adult=True 的源会被排除出全局搜索（app.search_all 的分层），
    误判会直接让一个正常源「搜不到」。所以只认强特征：
      1) 名字带 🔞；
      2) 命中 _ADULT_SRC_KW（porn/jav/91/麻豆/糖心/海角…）；
      3) 命中分类词表 _ADULT_KW 且不含 _NON_ADULT_KW（体育/综艺等反向词）。
    导入后用户可在资源站页随时取消 18。
    """
    n = (name or "").strip().lower()
    if not n:
        return False
    if "🔞" in n or any(k in n for k in _ADULT_SRC_KW):
        return True
    if any(k in n for k in _NON_ADULT_KW):
        return False
    return any(k in n for k in _ADULT_KW)


def catalog_title(cat: str) -> str:
    if cat == ADULT_CAT:
        return "🔞福利"
    for c in _read_custom_cats():
        if c["id"] == cat:
            return c["title"]
    for c, title, _ in CATALOG_CATEGORIES:
        if c == cat:
            return title
    return "🌐海量片库"


def catalog_mdb(cat: str) -> str:
    if cat == ADULT_CAT:
        return "Mix"
    for c in _read_custom_cats():
        if c["id"] == cat:
            return c["mdb"]
    for c, _, mdb in CATALOG_CATEGORIES:
        if c == cat:
            return mdb
    return "Mix"


def classify(name: str) -> str | None:
    n = (name or "").strip()
    nl = n.lower()
    if not n:
        return None
    # P2：AI漫剧先于脏类排除判定——“漫剧 / AI”曾在 _EXCLUDE_KW 里，
    # 现在是正式分类，不再当噪音丢掉。
    if any(k in n for k in _AI_COMIC_KW):
        return "ai_comic"
    # P2 追加：TED学习（小写匹配，TED / Ted / TEDx 都认）。
    if any(k in nl for k in _TED_KW):
        return "ted"
    if any(k in n for k in _EXCLUDE_KW):
        return None
    # P2：海外剧先于普通剧集判定——美剧/韩剧/日剧等不再混进“海量剧集”。
    if any(k in n for k in _OVERSEAS_KW):
        return "overseas"
    if any(k in n for k in _ANIME_KW):
        return "anime"
    if any(k in n for k in _VARIETY_KW):
        return "variety"
    if any(k in n for k in _SHORT_KW):
        return "shortdrama"
    if any(k in nl for k in _UHD_KW):
        return "uhd"
    if any(k in n for k in _DOC_KW):
        return "documentary"
    if any(k in n for k in _TV_KW):
        return "tv"
    if any(k in n for k in _MOVIE_KW):
        return "movie"
    # “动作片/喜剧片/剧情片/科幻片…”这类以“片”结尾的归入电影
    if n.endswith("片") and len(n) <= 4:
        return "movie"
    return None


def classify_source_class(name: str, adult_allowed: bool) -> str | None:
    """按源的扩展授权决定分类归属。

    - 命中硬红线（涉未成年/非自愿）一律返回 None，任何开关都不放行；
    - 本地分类规则库优先于内置关键词表：规则命中的名字直接照规则归属，
      命中「确认丢弃」(cat=None) 就丢弃，不再往下猜 —— 这是「一份规则管所有源」；
    - 规则没命中才走 classify 的关键词猜测；
    - 最后才用源级 18 授权把认不出的分类兜进福利库。
    """
    n = (name or "").strip()
    if not n or is_redline(n):
        return None
    hit, rcat = rule_hit(n)
    if hit:
        if rcat is None:
            return None
        if rcat == ADULT_CAT and not _welfare_on():
            return None
        return rcat
    cat = classify(n)
    if cat:
        return cat
    if not _welfare_on():
        return None
    nl = n.lower()
    if any(k in n for k in _NEUTRAL_KW):
        return None
    # 明确非成人的先挡掉：打了 18 也不能把体育/港台日韩剧倒进福利库。
    if any(k in n or k in nl for k in _NON_ADULT_KW):
        return None
    # 成人词表命中：独立开关打开即收编进 ADULT 池（无需逐源标记），
    # 这样扩展库卡"打开开关就有内容"；硬红线已在函数开头拦截。
    if any(k in n or k in nl for k in _ADULT_KW):
        return ADULT_CAT
    # 其余「名字完全不沾关键词」的分类（如「视频一区」「国产视频」「黑料吃瓜」），
    # 只有该源显式打了 18 才纳入——成人站全靠这一条兜底。
    if adult_allowed:
        return ADULT_CAT
    return None


async def _fetch_class_list(client: httpx.AsyncClient, source: dict) -> list[dict]:
    # 非苹果 CMS 源（drpy / csp 爬虫）没有 ac=list，走各自 sidecar 的 home()。
    kind = _src_kind(source)
    if kind in ("drpy", "spider"):
        fn = _xsrc_fn(kind, "classes")
        if fn is None:
            return []
        cls = await asyncio.wait_for(fn(source), timeout=XSRC_CLASS_TIMEOUT)
        return cls if isinstance(cls, list) else []
    base = source["api"].split("/at/json")[0].split("?")[0]
    r = await client.get(base, params={"ac": "list"},
                         headers={"User-Agent": "Mozilla/5.0"}, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    j = r.json()
    cls = j.get("class") if isinstance(j, dict) else None
    return cls if isinstance(cls, list) else []


async def _fetch_category_page(client: httpx.AsyncClient, source: dict,
                               type_id, page: int,
                               timeout: float = HTTP_TIMEOUT) -> tuple[list, int]:
    """返回 (本页 vod 列表, 该分类总页数)。"""
    # 非苹果 CMS 源：drpy / csp 爬虫的分类接口在各自 sidecar 里（同步调用放线程）。
    kind = _src_kind(source)
    if kind in ("drpy", "spider"):
        fn = _xsrc_fn(kind, "page")
        if fn is None:
            return [], 0
        try:
            lst, pc = await asyncio.wait_for(
                asyncio.to_thread(fn, source, type_id, page), timeout=timeout)
        except Exception:  # noqa: BLE001
            return [], 0
        return (lst or []), int(pc or 0) or XSRC_DEFAULT_PAGES
    base = source["api"].split("/at/json")[0].split("?")[0]
    # 注意：分类深翻不能带 h 参数（部分源加 h 后返回空/pagecount=0）。
    r = await client.get(base, params={"ac": "detail", "t": type_id, "pg": page},
                         headers={"User-Agent": "Mozilla/5.0"}, timeout=timeout)
    r.raise_for_status()
    j = r.json()
    if not isinstance(j, dict) or j.get("code") not in (1, "1"):
        return [], 0
    return (j.get("list") or []), int(j.get("pagecount") or 0)


class Catalog:
    def __init__(self):
        self._lock = threading.RLock()
        # source_id -> [ {type_id, type_name, type_pid, cat} ]
        self._classes: dict[str, list[dict]] = {}
        # "scope:cat" -> [ {source_id, type_id, type_name, pagecount?} ]（首选在最前）
        # scope 为 AGG_SCOPE（跨源聚合）或某个 source_id（单源独立媒体库）。
        self._routes: dict[str, list[dict]] = {}
        self._updated = 0.0
        self._refreshing = False
        # key = (scope, cat, page)
        self._page_cache: dict[tuple, tuple[float, list[dict]]] = {}
        self._inflight: dict[tuple, asyncio.Future] = {}
        # "scope:cat" -> 去重缓冲（资源站分页会重复个别 vod）
        self._seen_guids: dict[str, set[str]] = {}
        self._dense: dict[str, list[dict]] = {}
        self._scanned_page: dict[str, int] = {}
        self._dense_count: dict[str, int] = {}
        # (sid, cat) -> [备用叶子 route...]（首选分类探测失败时 rescue 得到）
        self._fallbacks: dict[tuple[str, str], list[dict]] = {}
        # source_id -> [分类名...]：既没命中规则、也没被内置表/18 兜底认出来的
        # 叶子分类。不参与建库，只在管理页「未归类池」里露出来，让用户一眼看到
        # 漏了什么、点一下归类就写回规则库（见 classify_rules.json）。
        self._unclassified: dict[str, list[str]] = {}
        # 本轮抓分类表失败的源：这些源保留上一轮结果不覆盖，管理页要如实标出来。
        # 不标的话，用户看到的「未归类 N 条」其实是「本轮连通的那批源」的快照，
        # 源一抖动数字就变，会被误判成规则失效（2026-11-12 实测：112 → 260）。
        self._fetch_failed: set[str] = set()

    # 移植上游 v0.2.54（缓存管理）：清空海量库分类路由/分页/聚合缓冲
    # （管理页一键清缓存），下次浏览海量库时重新探测分类并深翻。
    # fork 适配：上游无 _fallbacks 字段，这里一并清空，避免残留备用路由
    # 与已清空的路由表不一致。
    def reset(self) -> None:
        with self._lock:
            self._classes.clear()
            self._routes.clear()
            self._updated = 0.0
            self._page_cache.clear()
            self._inflight.clear()
            self._seen_guids.clear()
            self._dense.clear()
            self._scanned_page.clear()
            self._dense_count.clear()
            self._fallbacks.clear()
            self._unclassified.clear()
            self._fetch_failed.clear()
            # >>> OPTIONAL:DISK CACHE
            # 「清缓存」要连落盘一起清：否则下次重启又会把旧表读回来，
            # 用户会以为按钮没生效。清完由调用方重新建库并再次落盘。
            if DISK_CACHE:
                try:
                    _DISK_FILE.unlink()
                except FileNotFoundError:
                    pass
                except Exception as exc:  # noqa: BLE001
                    _log.warning("catalog disk remove failed: %s", exc)
            # <<< OPTIONAL:DISK CACHE

    # >>> OPTIONAL:DISK CACHE
    def load_disk(self) -> bool:
        """从磁盘恢复上次的分类表与路由（不联网）。返回是否恢复成功。

        冷启动专用：恢复后 status() / routes_for_scope() 立刻可用，用户不必
        陪着一轮建库；后台仍按 CLASS_REFRESH 周期刷新并覆盖本文件。
        配置里已删除的源在这里过滤掉，避免路由指向不存在的源。
        """
        if not DISK_CACHE:
            return False
        try:
            data = json.loads(_DISK_FILE.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return False
        except Exception as exc:  # noqa: BLE001
            _log.warning("catalog disk load failed: %s", exc)
            return False
        if not isinstance(data, dict) or data.get("v") != DISK_VERSION:
            return False
        classes = data.get("classes") or {}
        routes = data.get("routes") or {}
        if not isinstance(classes, dict) or not isinstance(routes, dict):
            return False

        try:
            import config as _config
            live = {str(s.get("id") or "") for s in _config.config.all_sources()}
            enabled = {str(s.get("id") or "")
                       for s in _config.config.enabled_sources()}
        except Exception:  # noqa: BLE001
            live, enabled = set(), set()
        cfg_ok = bool(live)

        cls_ok = {k: v for k, v in classes.items()
                  if isinstance(v, list) and (not cfg_ok or k in live)}
        # 路由键形如 "scope:cat"，scope 是 AGG_SCOPE 或某个源 id
        rt_ok: dict[str, list[dict]] = {}
        for k, v in routes.items():
            if not isinstance(v, list):
                continue
            scope = str(k).split(":", 1)[0]
            if cfg_ok and scope != AGG_SCOPE and scope not in enabled:
                continue
            # 停用的源不能靠旧表复活，按当前启用列表过滤
            keep = [r for r in v if isinstance(r, dict)
                    and (not cfg_ok or str(r.get("source_id") or "") in enabled)]
            if keep:
                rt_ok[k] = keep
        if not rt_ok:
            return False

        with self._lock:
            self._classes = cls_ok
            self._routes = rt_ok
            self._updated = float(data.get("updated") or data.get("saved_at") or 0)
            # 未归类池跟着落盘一起恢复：缓存路径不重建分类，内存里没有池子，
            # 管理页会显示「没有未归类」——跟实际情况不符。旧文件没这个字段时
            # 取空字典，向后兼容。
            unc = data.get("unclassified") or {}
            self._unclassified = {
                str(k): [str(n) for n in v] for k, v in unc.items()
                if isinstance(v, list) and (not cfg_ok or str(k) in live)}
        return True

    def save_disk(self) -> bool:
        """把分类表与路由原子写盘（先写 .tmp 再 rename），供下次启动秒开。

        落盘的是**上次的结论**而不是探测过程：重启后不做任何联网就能出库，
        后台刷新完成后自然覆盖。写失败只记日志，绝不影响当前进程运行。
        """
        if not DISK_CACHE:
            return False
        try:
            with self._lock:
                payload = {
                    "v": DISK_VERSION,
                    "saved_at": int(time.time()),
                    "updated": int(self._updated),
                    "classes": {k: v for k, v in self._classes.items() if v},
                    "routes": {k: v for k, v in self._routes.items() if v},
                    "unclassified": {k: v for k, v in self._unclassified.items() if v},
                }
            _DISK_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = _DISK_FILE.with_name(_DISK_FILE.name + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False),
                           encoding="utf-8")
            os.replace(tmp, _DISK_FILE)
            return True
        except Exception as exc:  # noqa: BLE001
            _log.warning("catalog disk save failed: %s", exc)
            return False
    # <<< OPTIONAL:DISK CACHE

    @staticmethod
    def _scope_key(scope: str, cat: str) -> str:
        return f"{scope}:{cat}"

    def standalone_scopes(self) -> list[str]:
        """当前需要独立成库的源 id（来源配置中的 standalone 标记）。"""
        try:
            from config import config
            return [s["id"] for s in config.all_sources() if s.get("standalone")]
        except Exception:
            return []

    # -------------------------------------------------- 分类表 / 路由
    async def refresh_routes(self, sources: list[dict]) -> dict[str, Any]:
        # T4 browsing is not CMS deep crawling; never pre-expand cloud plugins.
        sources = [s for s in sources if s.get("kind") not in ("t4", "live")]
        scopes = set(self.standalone_scopes())
        # drpy / csp 爬虫只服务「独立媒体库」：它们每页 20~40 条、靠 sidecar 串行，
        # 混进海量四库只会挤掉规范源。没人勾「库」时连分类表都不拉——每个源
        # 一次 home()，103 个源一起拉会把刷新拖到分钟级。
        sources = [s for s in sources
                   if _src_kind(s) not in ("drpy", "spider")
                   or str(s.get("id") or "") in scopes]
        # 非 CMS 源只拉分类表、**不做建库前的探测**：sidecar 是串行的，一个源
        # 5 个分类 × 每类最多 9 个叶子候选，能把整轮刷新（调用方给 90 秒）
        # 直接顶超时，结果所有库都建不出来。路由用分类表直接建（页数按上限
        # 估算），首屏翻页时按实际返回自然收敛。
        lazy_ids = {str(s.get("id") or "") for s in sources
                    if _src_kind(s) in ("drpy", "spider")}
        if self._refreshing:
            return self.status()
        self._refreshing = True
        try:
            async def _bounded(coro):
                """给单个源的建库动作封顶。超时 = 本次没有结果（不抛、不中断整轮）。

                auto 代理模式下直连失败会切代理重试，单请求 12 秒实际能跑到 24 秒；
                不封顶的话，一个连不上的源就把整轮 gather 拖满（实测 90 秒里有
                55 秒是这类源在等超时，而 40/82 个源 3 秒内就返回了）。
                """
                try:
                    return await asyncio.wait_for(coro, timeout=BUILD_BUDGET)
                except asyncio.TimeoutError:
                    return None
                except Exception:  # noqa: BLE001
                    return None

            # 上游同位置走 netutil.async_client（按代理模式直连/走代理/自动切代理），
            # 与本文件 _load_page 的两处用法一致。
            async with netutil.async_client(
                    timeout=HTTP_TIMEOUT) as client:
                await asyncio.gather(
                    *[_bounded(self._load_source_classes(client, s)) for s in sources],
                    return_exceptions=True)

            # 每个源每类先探测一个代表性叶子；首选超时/空时，回退探测该类下
            # 其它叶子，选能连通且页数最多的（部分私有源个别分类响应很慢）。
            # all_by_cat[(sid,cat)] = [cl,...]，首个为首选。
            all_by_cat: dict[tuple[str, str], list[dict]] = {}
            order: list[tuple[str, str]] = []
            for sid, classes in self._classes.items():
                if sid in lazy_ids or _src_kind(self._source_by_id(sid) or {}) in ("drpy", "spider"):
                    continue          # 非 CMS 源不预探（见上）
                per_cat: dict[str, list[dict]] = {}
                for cl in classes:
                    cat = cl.get("cat")
                    if cat:
                        per_cat.setdefault(cat, []).append(cl)
                for cat, cls in per_cat.items():
                    key = (sid, cat)
                    all_by_cat[key] = cls
                    order.append(key)
            probed: dict[tuple[str, str], dict] = {}

            async with netutil.async_client(
                    timeout=HTTP_TIMEOUT) as client:
                async def probe_one(sid, cat, cl):
                    """探一个代表叶子，返回 (状态, 路由)。

                    `ok` 有内容 / `empty` 能连通但该分类为空 / `fail` 连不上或超时。
                    区分 empty 与 fail 很关键：空页值得换个叶子再试，而连不上的源
                    换 8 个叶子依然连不上，只会白白再等一轮超时。
                    """
                    source = self._source_by_id(sid)
                    if not source:
                        return ("fail", None)
                    try:
                        _lst, pc = await asyncio.wait_for(
                            _fetch_category_page(client, source, cl["type_id"], 1),
                            timeout=BUILD_BUDGET)
                    except Exception:  # noqa: BLE001
                        return ("fail", None)
                    if not _lst:
                        return ("empty", None)
                    return ("ok", {"source_id": sid, "type_id": cl["type_id"],
                                   "type_name": cl["type_name"],
                                   "pagecount": int(pc or 0)})

                # 第一轮：每个 (源,类) 只探首选
                first = await asyncio.gather(
                    *[probe_one(sid, cat, all_by_cat[(sid, cat)][0])
                      for sid, cat in order],
                    return_exceptions=True)
                missing: list[tuple[str, str]] = []
                for (sid, cat), res in zip(order, first):
                    st, payload = res if isinstance(res, tuple) else ("fail", None)
                    if st == "ok" and isinstance(payload, dict):
                        probed[(cat, sid)] = payload
                    elif st == "empty":
                        # 只对「能连通但空」的换叶子；连接失败的直接放弃本轮
                        missing.append((sid, cat))

                # 第二轮：对首轮空页的(源,类)，并发尝试其余叶子，取页数最多者
                async def rescue(sid, cat):
                    cls = all_by_cat[(sid, cat)][1:9]  # 最多再试 8 个叶子
                    if not cls:
                        return
                    res = await asyncio.gather(
                        *[probe_one(sid, cat, cl) for cl in cls],
                        return_exceptions=True)
                    ok = [r[1] for r in res
                          if isinstance(r, tuple) and r[0] == "ok"
                          and isinstance(r[1], dict)]
                    if ok:
                        ok.sort(key=lambda r: -(r.get("pagecount") or 0))
                        probed[(cat, sid)] = ok[0]
                        # 记录备用叶子，供翻页超时/空页时同类并发竞速回退
                        self._fallbacks[(sid, cat)] = ok[1:7]
                await asyncio.gather(*[rescue(s, c) for s, c in missing],
                                     return_exceptions=True)

            def routes_for_scope(scope: str) -> dict[str, list[dict]]:
                out: dict[str, list[dict]] = {}
                # 福利分类由 welfare_library_enabled 独立开关控制：
                # 开关开 → 聚合与单源独立库都纳入 ADULT 池；开关关 → 一律不建。
                cats = all_cats() + ([ADULT_CAT] if _welfare_on() else [])
                for cat in cats:
                    if scope == AGG_SCOPE:
                        rs = [probed[(cat, sid)] for (c2, sid) in probed
                              if c2 == cat and self._agg_ok(sid)]
                        rs.sort(key=lambda r: -(r.get("pagecount") or 0))
                        # 聚合：首选最深的源，并附带其余可深翻源做回退
                        out[cat] = rs
                    else:
                        # 单源独立库：不依赖建库探测是否成功——直接纳入该类下
                        # 全部叶子（探测到页数的排前面，未探测到的用 MAX_PAGES
                        # 兜底），取页时并发抓取合并、天然容错慢/超时叶子。
                        by_id = {}
                        seed = []
                        hit = probed.get((cat, scope))
                        if hit:
                            seed.append(hit)
                        seed.extend(self._fallbacks.get((scope, cat), []))
                        for r in seed:
                            by_id[r["type_id"]] = r
                        for cl in self._classes.get(scope, []):
                            if cl.get("cat") == cat and cl["type_id"] not in by_id:
                                by_id[cl["type_id"]] = {
                                    "source_id": scope,
                                    "type_id": cl["type_id"],
                                    "type_name": cl["type_name"],
                                    "pagecount": MAX_PAGES}
                        rs = list(by_id.values())
                        rs.sort(key=lambda r: -(r.get("pagecount") or 0))
                        out[cat] = rs
                return out

            new_routes: dict[str, list[dict]] = {}
            agg = routes_for_scope(AGG_SCOPE)
            for cat, rs in agg.items():
                new_routes[self._scope_key(AGG_SCOPE, cat)] = rs
            for sid in self.standalone_scopes():
                for cat, rs in routes_for_scope(sid).items():
                    if rs:
                        new_routes[self._scope_key(sid, cat)] = rs
            with self._lock:
                # 清理已不再需要的作用域缓冲
                valid_keys = set(new_routes.keys())
                self._routes = new_routes
                for d in (self._seen_guids, self._dense, self._scanned_page):
                    for k in list(d.keys()):
                        if k not in valid_keys:
                            d.pop(k, None)
                self._updated = time.time()
            # >>> OPTIONAL:DISK CACHE
            # 这一轮的结论立刻落盘：下次进程重启直接用它出库，不再空等一轮建库。
            self.save_disk()
            # <<< OPTIONAL:DISK CACHE
            warm_scopes = [sid for sid in self.standalone_scopes()
                           if any(self.available(c, sid) for c in
                                  (all_cats() + ([ADULT_CAT] if _welfare_on() else [])))]
            for sid in warm_scopes:
                asyncio.create_task(self.warm_standalone(sid))
            # 4K 派生预热：源没有 4K 分类时，后台先翻电影页把 4K 片抄进
            # uhd 密集列表——库卡才有海报、首次点开才有内容；只在 uhd
            # 还是空的时候跑一次（密集列表在路由刷新时保留，不会反复跑）。
            if (not self._routes.get(self._scope_key(AGG_SCOPE, "uhd"))
                    and self._routes.get(self._scope_key(AGG_SCOPE, "movie"))
                    and not self._dense.get(self._scope_key(AGG_SCOPE, "uhd"))):
                asyncio.create_task(self.get_window("uhd", 0, 40))
        finally:
            self._refreshing = False
        return self.status()

    async def _load_source_classes(self, client: httpx.AsyncClient, source: dict):
        sid = str(source.get("id") or "")
        try:
            cls = await _fetch_class_list(client, source)
        except Exception as exc:  # noqa: BLE001
            # 抓不到 ≠ 该源没有分类。超时/HTTP 错误时保留上一轮结果，不覆盖：
            # 否则一轮抖动就会把已知分类表和未归类条目全清空，海量库路由忽多
            # 忽少、池子数字忽大忽小（2026-11-12 实测 112 → 260 的根因）。
            with self._lock:
                self._fetch_failed.add(sid)
            _log.info("class list fetch failed, keep last: %s (%s)", sid, exc)
            return
        with self._lock:
            self._fetch_failed.discard(sid)
        # 只取叶子子类：苹果CMS 的父类（电影片/连续剧…）不聚合子类、只有零星条目，
        # 真正可深翻的是动作片/国产剧这类子分类。
        pids_with_child = {str(x.get("type_pid") or "0") for x in cls
                           if str(x.get("type_pid") or "0") not in ("", "0")}
        adult_allowed = bool(source.get("adult"))
        # 父分类归属表：子分类名字认不出来时用它兜底继承。
        # 短剧站的子类叫「重生民国 / 都市脑洞 / 古装仙侠 / 闪婚离婚」，光看名字
        # 判不出类型（实测暴风资源 8 个这类分类）：不继承就会被「打 18」的兜底
        # 吞进福利库；不打 18 时更糟——整个被丢掉，短剧库永远是空的。
        parent_cat = {}
        for x in cls:
            try:
                # 父类一律按「不带 18 授权」判，免得父类自己靠 adult 兜底成
                # 福利库后，把子分类一起拖进去。
                parent_cat[str(x.get("type_id"))] = classify_source_class(
                    str(x.get("type_name") or ""), False)
            except Exception:  # noqa: BLE001
                continue
        norm = []
        missed: list[str] = []
        for x in cls:
            try:
                tid = x.get("type_id")
                if tid is None:
                    continue
                pid = str(x.get("type_pid") or "0")
                name = str(x.get("type_name") or "")
                is_leaf = pid != "0" or str(tid) not in pids_with_child
                # 规则命中「确认丢弃」就直接跳过：既不再继承父类，也不走 18
                # 兜底。否则给一个源打了 18，规则明明写了「足球丢弃」，还是会
                # 被兜底捞进福利库。
                hit, rcat = rule_hit(name)
                if hit and rcat is None:
                    continue
                # 判断顺序很关键：① 只看名字（含本地规则）→ ② 认不出就继承
                # 父类 → ③ 最后才用 18 授权兜底。反过来的话，18 兜底会把短剧
                # 子类全吞进福利库。
                cat = classify_source_class(name, False)
                if cat is None and not hit and pid != "0" \
                        and not any(k in name for k in _EXCLUDE_KW) \
                        and not any(k in name.lower() for k in _NON_ADULT_KW):
                    cat = parent_cat.get(pid)
                if cat is None and not hit:
                    cat = classify_source_class(name, adult_allowed)
                if not is_leaf:
                    continue
                if cat is None:
                    # 规则已命中但福利库关着，不算「没认出来」，别塞进池子让
                    # 用户以为规则没生效。
                    if not hit:
                        missed.append(name)
                    continue
                norm.append({"type_id": tid, "type_name": name,
                             "type_pid": pid, "cat": cat})
            except Exception:
                continue
        with self._lock:
            self._classes[source["id"]] = norm
            self._unclassified[source["id"]] = missed

    def _source_by_id(self, sid: str) -> dict | None:
        try:
            from config import config
            return next((s for s in config.all_sources() if s["id"] == sid), None)
        except Exception:
            return None

    def _agg_ok(self, sid: str) -> bool:
        """该源能否进跨源聚合的海量四库：只收苹果 CMS 深翻源。

        drpy / csp 爬虫只做独立库（页小、慢、靠 sidecar），混进四库会把
        规范源挤掉，也会让海量库分页忽多忽少。
        """
        src = self._source_by_id(sid)
        return bool(src) and _src_kind(src) not in ("drpy", "spider")

    def cat_for_source_type(self, source_id: str, type_id) -> str | None:
        """(source_id, type_id) → 该源分类路由的 cat；路由未加载/无匹配返回 None。

        供聚合搜索 brief 补 ``catalog_route`` 用：搜索返回的 vod 自带
        type_id，与建库时抓到的分类表（``_classes``）同一 id 空间；
        cat 由建库时的 ``classify_source_class`` 定，与分类库路由一致。
        """
        if not source_id or type_id is None or str(type_id) == "":
            return None
        tid = str(type_id)
        with self._lock:
            for cl in self._classes.get(source_id, []) or []:
                if str(cl.get("type_id")) == tid:
                    return cl.get("cat") or None
        return None

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "updated_at": int(self._updated),
                # 建库进行中：管理页保存规则后据此轮询，重建完成再刷池子，
                # 不再让用户误以为「点了保存没作用」（池子是上一轮的快照）。
                "refreshing": bool(self._refreshing),
                "unclassified": sum(len(v) for v in self._unclassified.values()),
                # 本轮抓分类表失败的源数：这些源沿用上一轮结果，池子数字不含
                # 它们的最新变化，管理页要据此提示「数字是快照不是全量」。
                "source_failed": len(self._fetch_failed),
                "categories": {
                    cat: {
                        "routes": len(self._routes.get(
                            self._scope_key(AGG_SCOPE, cat), [])),
                        "source": (self._routes.get(
                            self._scope_key(AGG_SCOPE, cat)) or [{}])[0].get("source_id", ""),
                        "type_name": (self._routes.get(
                            self._scope_key(AGG_SCOPE, cat)) or [{}])[0].get("type_name", ""),
                        "pagecount": min(int((self._routes.get(
                            self._scope_key(AGG_SCOPE, cat)) or [{}])[0]
                            .get("pagecount") or 0), MAX_PAGES),
                    } for cat, _, _ in iter_categories()},
            }

    def unclassified(self) -> dict[str, Any]:
        """未归类池：名字 + 涉及几个源 + 示例源 id，按涉及源数倒序。

        给管理页「分类规则」卡片用：一屏列出没认出来的分类名，用户选个库
        点保存 → 写进 classify_rules.json → 下次建库所有源同时生效。
        """
        with self._lock:
            buckets: dict[str, set[str]] = {}
            total = 0
            for sid, names in self._unclassified.items():
                for nm in names:
                    buckets.setdefault(nm, set()).add(sid)
                    total += 1
        items = [{"name": nm, "sources": len(sids), "sample": sorted(sids)[:3]}
                 for nm, sids in buckets.items()]
        items.sort(key=lambda x: (-x["sources"], x["name"]))
        with self._lock:
            failed = len(self._fetch_failed)
        return {"total": total, "names": len(items), "items": items, "failed": failed}

    def _uhd_fallback_routes(self, scope: str) -> list[dict]:
        """4K 回退路由：源没有“4K”分类名时，借电影分类的路由翻页，
        再由 _briefs_from_vods 按片名画质信号过滤出真 4K。"""
        with self._lock:
            return list(self._routes.get(self._scope_key(scope, "movie")) or [])

    def available(self, cat: str, scope: str = AGG_SCOPE) -> bool:
        with self._lock:
            if self._routes.get(self._scope_key(scope, cat)):
                return True
            # 4K：没有 4K 分类名、但有电影路由时也可派生；或已从浏览中抄到 4K 片。
            if cat == "uhd":
                if self._routes.get(self._scope_key(scope, "movie")):
                    return True
                return bool(self._dense.get(self._scope_key(scope, "uhd")))
            return False

    def pagecount(self, cat: str, scope: str = AGG_SCOPE) -> int:
        with self._lock:
            routes = self._routes.get(self._scope_key(scope, cat)) or []
            if not routes and cat == "uhd":
                routes = self._routes.get(self._scope_key(scope, "movie")) or []
            if not routes:
                return 0
            return min(int(routes[0].get("pagecount") or 0), MAX_PAGES)

    def dense_count(self, cat: str, scope: str = AGG_SCOPE) -> int:
        """已实际找到并去重的条目数（密集列表长度）。"""
        with self._lock:
            return len(self._dense.get(self._scope_key(scope, cat)) or [])

    def cached_dense(self, cat: str, limit: int = 40,
                     scope: str = AGG_SCOPE) -> list[dict]:
        """纯本地、零网络：取某大类已物化的去重密集列表前 limit 条。

        只返回已在内存里的 brief（浏览分类时由 _extend_dense 填入），
        未物化的分类返回 []，不触发任何网络拉取。供详情页关联作品冷启动
        兜底用：新装/未浏览时已浏览池为空，靠它补候选。
        """
        with self._lock:
            dense = self._dense.get(self._scope_key(scope, cat)) or []
            return list(dense[:max(0, int(limit))])

    def total(self, cat: str, scope: str = AGG_SCOPE) -> int:
        # 用规范源页数估算 total；最后一页条数未知，前端以空页自然停止。
        # 例外：4K 没有原生路由时是派生库，电影页数 × 20 是虚数——源备注里
        # 没有 4K 信号时库是空的，却报几十条，前端会一直翻空页转圈；
        # 只报实际找到的数，没找到就是 0。
        if cat == "uhd" and not self._routes.get(self._scope_key(scope, cat)):
            return self.dense_count(cat, scope)
        # 非 CMS 独立库（drpy / csp 爬虫）建库时不预探页数，路由里的页数只是
        # 上限估算，按它算 total 会虚高几千条：库里其实没内容，界面却显示
        # 20000 条然后一直翻空页。只报已实际翻到的条数。
        if scope != AGG_SCOPE:
            try:
                import config as _cfg
                if _cfg.source_type(self._source_by_id(scope) or {}) in ("drpy", "spider"):
                    return self.dense_count(cat, scope)
            except Exception:  # noqa: BLE001
                pass
        return self.pagecount(cat, scope) * PAGE_SIZE

    def routes_stale(self) -> bool:
        return (not self._updated) or (time.time() - self._updated) >= CLASS_REFRESH

    async def _fetch_pages(self, scope: str, cat: str,
                         pages: list[int]) -> list[tuple[int, list[dict]]]:
        """P1-17：有界并发取一批 CMS 页，返回 [(页号, rows)]，按页号升序。

        并发只发生在取页层：信号量把同时在途的 get_page 控制在
        DEEP_FETCH_CONCURRENCY 以内；get_page/_load_page 内部的
        3 轮重试与 (16, 10, 8) 三档超时回退原样不动。
        任一页失败（get_page 本身已吞异常返 []；此处再防非常规抛错）
        只记该页为空，不丢同批其它页；调用方按页号顺序合并，去重、
        空页容忍与游标语义与串行逐页完全一致。
        """
        sem = asyncio.Semaphore(DEEP_FETCH_CONCURRENCY)

        async def bound(pg: int) -> tuple[int, list[dict]]:
            async with sem:
                try:
                    rows = await self.get_page(cat, pg, scope)
                except Exception:
                    return pg, []
                return pg, rows if isinstance(rows, list) else []

        results = await asyncio.gather(
            *[bound(pg) for pg in pages], return_exceptions=True)
        out: list[tuple[int, list[dict]]] = []
        for pg, res in zip(pages, results):
            if isinstance(res, tuple) and len(res) == 2:
                out.append(res)
            else:
                out.append((pg, []))
        return out

    # -------------------------------------------------- 页级抓取
    async def _extend_dense(self, scope: str, cat: str, need_count: int) -> int:
        """顺序翻页直到去重后的累计条目数达到 need_count（或翻到该源末页）。

        P1-17：按 DEEP_FETCH_CONCURRENCY 分批并发取页，批内按页号顺序
        合并——每页的去重、游标前移、空页计数与串行版逐页体完全一致。
        达到 need / 空页到限时停下：批次里已取到但未处理到的页只留在
        _page_cache，不并入 dense、不前移游标，下次接着翻时从缓存取，
        不重复请求。"""
        sk = self._scope_key(scope, cat)
        pc = self.pagecount(cat, scope)
        seen = self._seen_guids.setdefault(sk, set())
        dense = self._dense.setdefault(sk, [])
        pg = self._scanned_page.get(sk, 0) + 1
        empty_streak = 0
        # 4K 回退是借电影页过滤，4K 片稀疏时连续多页都可能 0 新增，
        # 空页容忍放宽到 10 页，别翻两页就断。
        streak_limit = 10 if cat == "uhd" else 3
        while len(dense) < need_count and pg <= pc:
            batch = list(range(pg, min(pg + DEEP_FETCH_CONCURRENCY, pc + 1)))
            pages = await self._fetch_pages(scope, cat, batch)
            stopped = False
            for pg_i, rows in pages:
                added = 0
                for b in rows:
                    gid = b.get("guid")
                    if gid in seen:
                        continue
                    seen.add(gid); dense.append(b); added += 1
                if added:
                    # 只有真正取到新增才把游标前移，慢源抖动的空页下次可重试
                    self._scanned_page[sk] = pg_i
                    empty_streak = 0
                else:
                    # 连续多页空（超时/末页）才停止，容忍慢源的偶发抖动
                    empty_streak += 1
                    if empty_streak >= streak_limit:
                        self._scanned_page[sk] = pg_i
                        stopped = True
                        break
                pg = pg_i + 1
                if len(dense) >= need_count:
                    stopped = True
                    break
            if stopped:
                break
        self._dense_count[sk] = len(dense)
        return len(dense)

    async def get_window(self, cat: str, offset: int, limit: int,
                         scope: str = AGG_SCOPE) -> list[dict]:
        """按“去重后的全局偏移”取一窗 brief，自动顺序跨 CMS 页，密集无重复。"""
        offset = max(0, int(offset))
        limit = max(1, min(int(limit), 100))
        sk = self._scope_key(scope, cat)
        dense = self._dense.setdefault(sk, [])
        if len(dense) < offset + limit:
            await self._extend_dense(scope, cat, offset + limit)
        return dense[offset:offset + limit]

    async def get_page(self, cat: str, page: int, scope: str = AGG_SCOPE) -> list[dict]:
        """取某作用域某大类第 page 页（1 起）的可播放 brief 列表（已入库）。"""
        page = max(1, int(page))
        if page > MAX_PAGES:
            return []
        key = (scope, cat, page)
        with self._lock:
            hit = self._page_cache.get(key)
            if hit:
                ts, data, ttl = hit
                if time.time() - ts < ttl:
                    return data
                self._page_cache.pop(key, None)
            fut = self._inflight.get(key)
            if fut is not None and fut.get_loop() is not asyncio.get_running_loop():
                # 旧事件循环残留的 future（循环已关闭、拉取任务已取消）：
                # 丢弃重建，否则新循环里等一个永远不会完成的 future。
                fut = None
                self._inflight.pop(key, None)
        if fut is None:
            fut = asyncio.get_event_loop().create_future()
            with self._lock:
                self._inflight[key] = fut

            async def work():
                try:
                    briefs = await self._load_page(scope, cat, page)
                    with self._lock:
                        # 空结果只短缓存，避免慢源一次抖动把该页冻结 10 分钟
                        ttl = EMPTY_TTL if not briefs else PAGE_TTL
                        self._page_cache[key] = (time.time(), briefs, ttl)
                    if not fut.done():
                        fut.set_result(briefs)
                except asyncio.CancelledError:
                    # 拉取任务被取消：先给等它的人一个空结果再重抛，
                    # 别把 key 永远挂在 _inflight 让后来的人等死。
                    if not fut.done():
                        fut.set_result([])
                    raise
                except Exception:
                    if not fut.done():
                        fut.set_result([])
                finally:
                    with self._lock:
                        self._inflight.pop(key, None)

            asyncio.create_task(work())
        return await fut

    def cached_page_posters(self, cat: str, n: int = 8,
                            scope: str = AGG_SCOPE) -> list[str]:
        """纯本地、零网络：从已缓存的首页/密集列表取海报，供媒体库卡片秒回。"""
        out: list[str] = []
        with self._lock:
            # 优先已翻好的去重密集列表（最新），其次首页缓存
            dense = self._dense.get(self._scope_key(scope, cat)) or []
            pool = list(dense)
            hit = self._page_cache.get((scope, cat, 1))
            if hit and time.time() - hit[0] < hit[2]:
                for b in hit[1]:
                    if b not in pool:
                        pool.append(b)
            seen = set()
            for b in pool:
                p = b.get("poster") or ""
                if p and p not in seen:
                    seen.add(p)
                    out.append(p)
                    if len(out) >= n:
                        break
        return out

    def _briefs_from_vods(self, vods: list, source: dict, route: dict,
                          scope: str, cat: str) -> list[dict]:
        from config import config as _cfg
        briefs = []
        for v in vods:
            try:
                t = _map_vod_any(v, source)
            except Exception:
                continue
            if not t or not t.title:
                continue
            playable = [e for line in t.sources.values() for e in line
                        if e.direct or e.resolve]
            if not playable:
                continue
            # 硬红线（任何模式都不放行）：涉未成年/非自愿，标题或备注命中即丢。
            if is_redline(t.title, t.remarks):
                continue
            # 私有源放宽：单源作用域且该源开启 loose_filter 时，
            # 跳过标题/备注噪音过滤；硬红线与常规分类红线仍在。
            loose = (scope != AGG_SCOPE and bool(source.get("loose_filter")))
            if not loose and _cfg.is_junk(t.title, t.remarks):
                continue
            # 4K 库只收带画质信号的片（见 _load_page 的 uhd 回退路由）。
            if cat == "uhd" and not is_uhd_title(t.title, t.remarks):
                continue
            briefs.append({
                "guid": make_guid(t),
                "online_id": t.online_id, "title": t.title,
                "type_name": t.type_name, "year": t.year,
                "poster": t.poster, "overview": t.overview,
                "remarks": t.remarks, "score": t.score,
                "actor": t.actor, "director": t.director, "area": t.area,
                "lines": {name: [e.__dict__ for e in eps]
                          for name, eps in t.sources.items()},
                "catalog_cat": cat, "catalog_scope": scope,
                "catalog_source": route["source_id"],
                # P0-6：来源分类路由（与 catalog_* 同源，供 libfilter
                # 并集归属；聚合搜索 brief 由 app 层补同一形状字段）。
                "catalog_route": {"source": source["id"],
                                  "type_id": route["type_id"], "cat": cat},
            })
        # 派生 4K：浏览电影/剧集等分类时，带 4K 画质信号的片顺手抄一份进
        # 同作用域的 uhd 密集列表——源分类名里没有“4K”时 4K 库也能有内容。
        if cat != "uhd" and briefs:
            uhd_sk = self._scope_key(scope, "uhd")
            uhd_seen = self._seen_guids.setdefault(uhd_sk, set())
            uhd_dense = self._dense.setdefault(uhd_sk, [])
            for b in briefs:
                if b["guid"] in uhd_seen:
                    continue
                if not is_uhd_title(b["title"], b["remarks"]):
                    continue
                uhd_seen.add(b["guid"])
                uhd_dense.append({**b, "catalog_cat": "uhd",
                                  "catalog_route": {**b["catalog_route"],
                                                    "cat": "uhd"}})
            self._dense_count[uhd_sk] = len(uhd_dense)
        return briefs

    async def _load_page(self, scope: str, cat: str, page: int) -> list[dict]:
        from store import store
        with self._lock:
            routes = list(self._routes.get(self._scope_key(scope, cat)) or [])
        if not routes and cat == "uhd":
            # 源没有“4K”分类名：借电影分类翻页，片名画质信号过滤在
            # _briefs_from_vods 里做（cat 仍是 uhd，只留真 4K）。
            routes = self._uhd_fallback_routes(scope)
        if scope == AGG_SCOPE:
            routes = routes[:4]
        if not routes:
            return []

        async def fetch_one(client, route, timeout):
            source = self._source_by_id(route["source_id"])
            if not source:
                return None
            try:
                vods, pc = await _fetch_category_page(
                    client, source, route["type_id"], page, timeout=timeout)
            except Exception:
                return None
            if not vods:
                return None
            return source, route, vods

        # 单源独立库：把该类下全部叶子的同一页并发抓取后“合并去重”，
        # 而不是竞速只取一个——慢/不稳的私有源单页常超时，合并所有叶子
        # 才能保证条目完整。首轮给较长超时，全空再重试两轮。
        if scope != AGG_SCOPE and len(routes) > 1:
            sem = asyncio.Semaphore(10)

            async def once(timeout):
                # 上游 v1.0.0 做法：深翻经 netutil.async_client（按代理模式
                # 直连/走代理/自动切代理），与 providers.py 已有用法一致。
                # 证书校验开启（与 providers.py 的 CMS 抓取一致；用户源
                # API 与搜索/详情走同一批主机，不应在此处单独关校验）。
                async with netutil.async_client(
                        timeout=HTTP_TIMEOUT) as client:
                    async def bound(r):
                        async with sem:
                            return await fetch_one(client, r, timeout)
                    results = await asyncio.gather(
                        *[bound(r) for r in routes],
                        return_exceptions=True)
                merged: list[dict] = []
                seen_g: set[str] = set()
                for res in results:
                    if not isinstance(res, tuple):
                        continue
                    source, route, vods = res
                    for b in self._briefs_from_vods(vods, source, route, scope, cat):
                        g = b.get("guid")
                        if g and g not in seen_g:
                            seen_g.add(g)
                            merged.append(b)
                return merged

            for attempt, tmo in enumerate((16.0, 10.0, 8.0)):
                briefs = await once(tmo)
                if briefs:
                    return store.remember_merged(briefs)
            return []

        # 跨源聚合：串行回退（不同源结果应一致，并发会无谓翻倍请求）
        # 证书校验开启，理由同上（与 providers.py 的 CMS 抓取一致）。
        async with netutil.async_client(timeout=HTTP_TIMEOUT) as client:
            for route in routes:
                got = await fetch_one(client, route, HTTP_TIMEOUT)
                if not got:
                    continue
                source, _route, vods = got
                briefs = self._briefs_from_vods(vods, source, route, scope, cat)
                if briefs:
                    return store.remember_merged(briefs)
        return []

    async def warm_standalone(self, scope: str):
        """建库后后台预热独立库各分类首页，缓解慢源首屏空白。"""
        try:
            src = self._source_by_id(scope)
            pool = all_cats() + [ADULT_CAT] if (_welfare_on() and src and src.get("adult")) else all_cats()
            await asyncio.gather(
                *[self.get_page(c, 1, scope) for c in pool
                  if self.available(c, scope)],
                return_exceptions=True)
        except Exception:
            pass

    async def _source_cat_window(self, scope: str, cat: str,
                                 offset: int, limit: int) -> list[dict]:
        """单源某类窗口：每页 = 该类下全部叶子同页合并去重（首页约数百条）。

        顺序取合并大页直到攒够 offset+limit；容忍慢源偶发的整页空（重试到
        连续 3 个空页才认为到底），避免一次抖动就把分页永久截断。

        P1-17：按 DEEP_FETCH_CONCURRENCY 分批并发取页，批内按页号顺序
        合并；任一页失败只记该页为空，不丢同批其它页。
        """
        out: list[dict] = []
        seen: set[str] = set()
        pg = 1
        empty_streak = 0
        while len(out) < offset + limit and pg <= MAX_PAGES and empty_streak < 3:
            batch = list(range(pg, min(pg + DEEP_FETCH_CONCURRENCY,
                                       MAX_PAGES + 1)))
            pages = await self._fetch_pages(scope, cat, batch)
            stopped = False
            for pg_i, rows in pages:
                added = 0
                for b in rows:
                    g = b.get("guid")
                    if g and g not in seen:
                        seen.add(g); out.append(b); added += 1
                if added:
                    empty_streak = 0
                else:
                    empty_streak += 1
                pg = pg_i + 1
                if len(out) >= offset + limit or empty_streak >= 3:
                    stopped = True
                    break
            if stopped:
                break
        return out[offset:offset + limit]

    # --------------------------------------- 单源独立库：跨四类交错窗口
    async def standalone_window(self, scope: str, offset: int,
                                limit: int) -> tuple[list[dict], int]:
        """单源独立库：把该源的电影/剧/动漫/综艺按轮次交错成一条“最近更新”流。

        返回 (brief 列表, 估算 total)。四类各取 offset..offset+limit 的窗，
        再按排名轮次合并去重，最后按全局 offset 切片。
        """
        src = self._source_by_id(scope)
        pool = all_cats() + [ADULT_CAT] if (_welfare_on() and src and src.get("adult")) else all_cats()
        cats = [c for c in pool if self.available(c, scope)]
        if not cats:
            return [], 0
        end = offset + limit
        results = await asyncio.gather(
            *[self._source_cat_window(scope, c, 0, end) for c in cats],
            return_exceptions=True)
        per: list[list[dict]] = []
        total = 0
        for c, r in zip(cats, results):
            per.append(r if isinstance(r, list) else [])
            if isinstance(r, list):
                total += self.total(c, scope)
        merged: list[dict] = []
        seen: set[str] = set()
        maxlen = max((len(x) for x in per), default=0)
        # 轮次交错：第 i 轮依次取每类第 i 条，近似源站“最近更新”混合排序
        for i in range(maxlen):
            for arr in per:
                if i < len(arr):
                    b = arr[i]
                    g = b.get("guid")
                    if g and g not in seen:
                        seen.add(g)
                        merged.append(b)
        return merged[offset:offset + limit], total

    def standalone_available(self, scope: str) -> bool:
        src = self._source_by_id(scope)
        pool = all_cats() + [ADULT_CAT] if (_welfare_on() and src and src.get("adult")) else all_cats()
        return any(self.available(c, scope) for c in pool)


catalog = Catalog()
