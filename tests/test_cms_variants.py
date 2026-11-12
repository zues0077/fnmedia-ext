"""非标准路径的苹果CMS 站：识别、解析、相对地址补全、成人源判定。

2026-11-13 实测：246 上 3 份订阅里 16 个真苹果CMS 站被
`_CMS_PATH = /provide/vod` 这条窄规则判成「非标准苹果CMS接口」直接丢弃
（/inc/apijson.php、/api/json.php、/api/xml.php、/apple_m3u8.php、
/feifei、/cjapi/mc/vod/json.html）。修复口径：
  1) type 0/1 的 http(s) 站点一律作为 CMS 候选，真实可用性交给探测；
  2) 运行时按内容解析（JSON 优先、失败回退 XML），现有 JSON 源行为不变；
  3) 成功判定放宽到「无 code 但有 list/class」。
"""

import json

import pytest

from proxy import catalog, providers, tvbox

CMS_VARIANTS = [
    ("1", "https://api.yzzy-api.com/inc/apijson.php"),
    ("1", "https://www.msnii.com/api/json.php"),
    ("1", "https://www.pgxdy.com/api/json.php"),
    ("1", "https://www.kxgav.com/api/json.php"),
    ("1", "https://dadiapi.com/feifei"),
    ("1", "https://caiji.semaozy.net/inc/apijson_vod.php"),
    ("1", "https://523zyw.me/inc/apijson_vod.php"),
    ("0", "https://www.xrbsp.com/api/xml.php"),
    ("0", "https://www.gdlsp.com/api/xml.php"),
    ("0", "http://www.afasu.com/api/xml.php"),
    ("0", "https://dadiapi.com/apple_m3u8.php"),
    ("1", "https://taopianapi.com/cjapi/mc/vod/json.html"),
]


@pytest.mark.parametrize("t,api", CMS_VARIANTS)
def test_classify_accepts_nonstandard_cms_paths(t, api):
    st, norm = tvbox.classify_site({"name": "测试站", "type": t, "api": api})
    assert norm is not None, api
    assert st in ("add_json", "add_xml")
    assert norm["api"], api


def test_classify_still_rejects_pan_and_live():
    assert tvbox.classify_site(
        {"name": "网盘", "type": "1", "api": "https://pan.quark.cn/s/x"}) == ("skip", None)
    assert tvbox.classify_site(
        {"name": "某某直播", "type": "1", "api": "https://a.com/api.php/provide/vod"})[0] == "skip"


def test_classify_keeps_standard_rewrite():
    st, norm = tvbox.classify_site(
        {"name": "标准站", "type": "0", "api": "https://a.com/api.php/provide/vod"})
    assert norm["api"] == "https://a.com/api.php/provide/vod/at/json"
    # 已知 json 路径不重写（否则会破坏 /inc/apijson.php 这类真站）
    st2, n2 = tvbox.classify_site(
        {"name": "j", "type": "1", "api": "https://a.com/inc/apijson.php"})
    assert n2["api"] == "https://a.com/inc/apijson.php"


def test_abs_api_joins_relative_only():
    base = "https://raw.githubusercontent.com/cluntop/tvbox/refs/heads/main/fun.js"
    assert tvbox._abs_api("./js/ss/x.js", base).endswith("/refs/heads/main/js/ss/x.js")
    assert tvbox._abs_api("/api.php/provide/vod", "https://a.com/x.json") == \
        "https://a.com/api.php/provide/vod"
    # 类名/脚本名不能当相对路径
    assert tvbox._abs_api("csp_XBPQ", base) == "csp_XBPQ"
    assert tvbox._abs_api("https://a.com/x.js", base) == "https://a.com/x.js"


def test_parse_cms_payload_json_first_then_xml():
    body = json.dumps({"code": 1, "list": [{"vod_name": "片"}]})
    assert providers.parse_cms_payload(body, "https://a.com/api/json.php")["code"] == 1
    xml = ('<?xml version="1.0" encoding="utf-8"?><rss><list>'
           '<video><vod_name>片名</vod_name><vod_id>1</vod_id></video>'
           '</list></rss>')
    d = providers.parse_cms_payload(xml, "https://a.com/api/xml.php")
    assert isinstance(d, dict) and d.get("list")
    # /at/xml 老路径仍按 XML 解析
    assert providers.parse_cms_payload(xml, "https://a.com/api.php/provide/vod/at/xml")
    assert providers.parse_cms_payload("<html>not cms</html>", "https://a.com/x") is None


def test_cms_ok_tolerates_missing_code():
    assert providers._cms_ok({"code": 1, "list": []})
    assert providers._cms_ok({"list": [{"vod_name": "x"}]})          # 淘片式
    assert providers._cms_ok({"class": [{"type_id": 1}]})            # 纯分类接口
    assert not providers._cms_ok({"code": 1002, "list": []})         # 业务拒绝仍是失败
    assert not providers._cms_ok({"list": []})
    assert not providers._cms_ok(None)


@pytest.mark.parametrize("name,expect", [
    ("🔞白嫖资源", True),
    ("🔞-黄AVZY", True),
    ("91porn", True),
    ("麻豆传媒", True),
    ("🎬优质资源", False),
    ("🤖AI漫剧", False),
    ("足球直播", False),          # 体育站不能被误标
    ("", False),
])
def test_looks_adult_source_conservative(name, expect):
    assert catalog.looks_adult_source(name) is expect


def test_bare_play_url_without_name_is_kept():
    """vod_play_url 只有裸地址、没有「名称$地址」结构时不能丢。

    2026-11-13 实测 msnii / xrbsp / gdlsp 一族：vod_play_from=ckplayer，
    vod_play_url = https://videoXX.xxx.com/.../index.m3u8（无 $），
    旧逻辑因缺 $ 丢掉整条 → 搜得到但永远没有播放地址。
    """
    out = providers._parse_play(
        "ckplayer", "",
        "https://video1.example.com/video/20230704/abc/index.m3u8", "https://x/")
    assert list(out) == ["ckplayer"]
    ep = out["ckplayer"][0]
    assert ep.url.endswith("index.m3u8") and ep.direct is True

    # 多集裸地址按 # 分开，正常编号
    out2 = providers._parse_play(
        "ckplayer", "",
        "https://a.com/1/index.m3u8#https://a.com/2/index.m3u8", "https://x/")
    assert len(out2["ckplayer"]) == 2
    # 既有「名称$地址」写法不受影响
    out3 = providers._parse_play(
        "线路1", "", "第1集$https://a.com/1.m3u8#第2集$https://a.com/2.m3u8", "https://x/")
    assert [e.title for e in out3["线路1"]] == ["第1集", "第2集"]


class _DummyClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def test_looks_adult_api_by_content(monkeypatch):
    """名字判不出时按内容判：搜「无码/麻豆」，成人站 20 条、正常站 0~3 条。"""
    import asyncio as _asyncio

    async def fake_search(client, source, kw):
        n = 20 if kw == "无码" else 0
        return [object()] * n

    monkeypatch.setattr(providers, "search_source", fake_search)
    monkeypatch.setattr(providers.netutil, "async_client",
                        lambda timeout=None: _DummyClient())
    assert _asyncio.run(providers.looks_adult_api("https://a.com/api.php")) is True

    async def few(client, source, kw):
        return [object()] * 3          # 量子资源这类正常站的实际命中

    monkeypatch.setattr(providers, "search_source", few)
    assert _asyncio.run(providers.looks_adult_api("https://a.com/api.php")) is False
    assert _asyncio.run(providers.looks_adult_api("csp_XBPQ")) is False
