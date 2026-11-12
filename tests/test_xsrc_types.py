"""源真实类型判定 / 排序保底 / 巡检分派 / 独立库适配（2026-11-12 第 7 批）。

背景（246 实测，用户要求"别把非 WASM 标成 WASM"+"启用却搜不到"）：
- 管理页用 ``FEAT_DDUO && src.xsrc`` 一刀切，把 8 个 drpy + 92 个 spider +
  小雅网盘全标成 WASM；
- 巡检把 ``catvod://xiaoya`` 当苹果 CMS 探测 → 判「地址非法」→ 一启用就被停用；
- 排序截断 top_n=40 后 head 全是 cms，92 spider + 8 drpy 永远没人搜。
"""
import asyncio

import pytest

import catalog
import config
import healthcheck
import providers


def _src(**kw):
    base = {"id": "x", "name": "X", "api": "https://x.test/at/json",
            "enabled": True}
    base.update(kw)
    return base


# ---------------------------------------------------------------- 类型判定
def test_source_type_mapping():
    assert config.source_type(_src()) == "cms"
    assert config.source_type(_src(xsrc=True)) == "dduo"
    assert config.source_type(_src(xsrc=True, catvod=True)) == "catvod"
    assert config.source_type(_src(xsrc=True, drpy=True)) == "drpy"
    assert config.source_type(_src(xsrc=True, spider_src=True)) == "spider"
    assert config.source_type(_src(xsrc=True, guard=True)) == "guard"
    assert config.source_type(_src(kind="t4")) == "t4"
    assert config.source_type(_src(kind="live")) == "live"
    assert config.source_type(None) == "cms"


# ---------------------------------------------------------------- 排序保底
def test_pick_head_reserves_each_type():
    ranked = ([_src(id="cms%d" % i) for i in range(40)]
              + [_src(id="sp%d" % i, xsrc=True, spider_src=True)
                 for i in range(10)]
              + [_src(id="dr%d" % i, xsrc=True, drpy=True) for i in range(5)])
    head, tail = config.pick_head(ranked, 40)
    assert len(head) == 40
    ids = {s["id"] for s in head}
    # 每种类型保底 3 个，不再被 40 个 cms 全挤出去
    assert {"sp0", "sp1", "sp2"} <= ids
    assert {"dr0", "dr1", "dr2"} <= ids
    # head 内部顺序仍是原字典序（cms 排前面），不引入权重
    assert head[0]["id"] == "cms0"
    assert len(head) + len(tail) == len(ranked)


def test_pick_head_without_limit_returns_all():
    ranked = [_src(id="a"), _src(id="b")]
    head, tail = config.pick_head(ranked, 0)
    assert head == ranked and tail == []


def test_pick_head_budget_smaller_than_reserve():
    ranked = ([_src(id="c%d" % i) for i in range(3)]
              + [_src(id="sp%d" % i, xsrc=True, spider_src=True)
                 for i in range(5)])
    head, tail = config.pick_head(ranked, 2)
    assert len(head) == 2 and len(tail) == len(ranked) - 2


# ---------------------------------------------------------------- 巡检分派
def test_probe_any_source_uses_catvod_adapter(monkeypatch):
    """小雅网盘必须走 catvod 适配口，不能再被苹果 CMS 判「地址非法」。"""
    async def boom(*a, **k):          # 不该走 CMS 探测
        raise AssertionError("catvod 源不该走苹果 CMS 探测")

    seen = {}

    async def fake(source, kw, timeout):
        seen["api"] = source["api"]
        return providers._quality("ok", count=2)

    monkeypatch.setattr(providers, "probe_source_quality", boom)
    monkeypatch.setattr(providers, "_probe_catvod_source", fake, raising=False)
    r = asyncio.run(providers.probe_any_source(
        _src(api="catvod://xiaoya", xsrc=True, catvod=True), keyword="庆余年"))
    assert r["state"] == "ok" and r["ok"] is True
    assert seen["api"] == "catvod://xiaoya"


def test_probe_any_source_missing_runtime_is_unsupported(monkeypatch):
    """公开构建里没有 drpy 适配函数：判 unsupported（不计失败），而不是 invalid。"""
    monkeypatch.delattr(providers, "_probe_drpy_source", raising=False)
    r = asyncio.run(providers.probe_any_source(
        _src(api="drpy://a.js", xsrc=True, drpy=True)))
    assert r["state"] == "unsupported" and r["ok"] is False


def test_healthcheck_routes_by_type(monkeypatch):
    seen = {}

    async def fake(source, *, keyword="", timeout=25.0):
        seen["id"] = source["id"]
        return providers._quality("ok")

    monkeypatch.setattr(providers, "probe_any_source", fake)
    asyncio.run(healthcheck._probe(
        _src(id="xiaoya", api="catvod://xiaoya", xsrc=True, catvod=True),
        5.0, "庆余年"))
    assert seen["id"] == "xiaoya"


# ---------------------------------------------------------------- 独立库适配
def test_catalog_class_list_uses_drpy_adapter(monkeypatch):
    async def fake(source):
        return [{"type_id": "1", "type_name": "电影"}]

    monkeypatch.setattr(providers, "_xsrc_classes_drpy", fake, raising=False)
    got = asyncio.run(catalog._fetch_class_list(
        None, _src(api="drpy://a.js", xsrc=True, drpy=True)))
    assert got and got[0]["type_name"] == "电影"


def test_catalog_map_uses_drpy_item():
    """完整构建：drpy 列表项走自己的映射（有伪剧集，能判「可播」）。

    公开构建剥离了 drpy 适配，_map_vod_any 会退化成 CMS 映射 → 这里跳过。
    """
    if getattr(providers, "_xsrc_map_drpy", None) is None:
        pytest.skip("公开构建不含 drpy 适配")
    t = catalog._map_vod_any({"vod_id": "9", "vod_name": "测试片"},
                             _src(xsrc=True, drpy=True))
    assert t is not None and t.title == "测试片" and t.sources


def test_catalog_map_keeps_cms_path():
    t = catalog._map_vod_any(
        {"vod_id": "1", "vod_name": "CMS片", "vod_play_from": "l1",
         "vod_play_url": "第1集$https://a.test/1.m3u8"}, _src())
    assert t is not None and t.title == "CMS片"
