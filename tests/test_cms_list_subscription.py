"""苹果CMS 接口清单（顶层数组）当作订阅导入：格式识别与归一化。

对应真实场景：https://www.ziyuanzu.com/download/ziyuanzu-cms-interfaces.json
形如 ``[{"name": "360资源站", "api": "https://.../api.php/provide/vod/",
"apiType": "json"}, ...]`` —— 没有 TVBox 的 sites 字段。

2026-11-13 实测：这类清单过去被 parse_config_text 判成「不是订阅」，用户把
URL 加成订阅后每轮拉取 sites=0，静默空转。本测试锁住三件事：
  1. 数组清单能被识别并转成 sites；
  2. apiType=xml 走 type=0，其余 type=1；
  3. 不合格的数组（非 dict / 非 http api / 空）仍然不被误判。
"""
import json

import tvbox


def _parse(text: str):
    return tvbox.parse_config_text(text)


def test_cms_interface_list_is_recognized():
    raw = json.dumps([
        {"name": "360资源站", "api": "https://360zy.com/api.php/provide/vod/",
         "apiType": "json", "uptime": "99.999%", "speed": "fast"},
        {"name": "鸭鸭资源站", "api": "https://cj.yayazy.net/api.php/provide/vod/",
         "apiType": "json"},
        {"name": "光速资源站", "api": "https://api.guangsuapi.com/api.php/provide/vod/"},
    ], ensure_ascii=False)
    cfg = _parse(raw)
    assert isinstance(cfg, dict), "顶层数组应被包装成配置 dict"
    sites = tvbox.extract_sites(cfg)
    assert len(sites) == 3
    assert [s["name"] for s in sites] == ["360资源站", "鸭鸭资源站", "光速资源站"]
    assert all(s["type"] == 1 for s in sites)
    assert sites[0]["api"].startswith("https://360zy.com")


def test_api_type_xml_maps_to_type_zero():
    raw = json.dumps([
        {"name": "xml站", "api": "https://x.example.com/api.php/provide/vod/xml/",
         "apiType": "xml"},
        {"name": "json站", "api": "https://j.example.com/api.php/provide/vod/",
         "apiType": "JSON"},
    ], ensure_ascii=False)
    sites = tvbox.extract_sites(_parse(raw))
    assert sites[0]["type"] == 0
    assert sites[1]["type"] == 1


def test_missing_name_falls_back_to_host():
    raw = json.dumps([{"api": "https://no-name.example.com/api.php/provide/vod/"}])
    sites = tvbox.extract_sites(_parse(raw))
    assert len(sites) == 1
    assert sites[0]["name"] == "no-name.example.com"


def test_invalid_entries_are_skipped():
    raw = json.dumps([
        {"name": "没 api"},
        {"name": "相对路径", "api": "./api.php/provide/vod/"},
        {"name": "ftp", "api": "ftp://x/api.php"},
        "不是对象",
        {"name": "好的", "api": "https://ok.example.com/api.php/provide/vod/"},
    ], ensure_ascii=False)
    sites = tvbox.extract_sites(_parse(raw))
    assert [s["name"] for s in sites] == ["好的"]


def test_all_invalid_list_is_still_not_a_subscription():
    raw = json.dumps([{"name": "没 api"}, {"x": 1}], ensure_ascii=False)
    assert _parse(raw) is None


def test_empty_array_is_not_a_subscription():
    assert _parse("[]") is None


def test_jsonc_comment_array_is_recognized():
    raw = """[
      // 这是接口清单
      {"name": "带注释", "api": "https://c.example.com/api.php/provide/vod/"}
    ]"""
    sites = tvbox.extract_sites(_parse(raw))
    assert len(sites) == 1
    assert sites[0]["name"] == "带注释"


def test_normal_dict_subscription_unchanged():
    raw = json.dumps({"sites": [
        {"key": "a", "name": "普通", "type": 1,
         "api": "https://n.example.com/api.php/provide/vod/"},
        {"key": "b", "name": "爬虫", "type": 3, "api": "csp_Test",
         "ext": "https://j/x.jar"},
    ]}, ensure_ascii=False)
    sites = tvbox.extract_sites(_parse(raw))
    assert [s["key"] for s in sites] == ["a", "b"]
    assert sites[1]["api"] == "csp_Test"
