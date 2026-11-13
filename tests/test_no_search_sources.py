"""「站点不支持搜索 → 不参与全局搜索」的判定与配置语义。

背景（2026-11-13）：牛牛资源（niuniuzy4）分类 59 个、12.5 万部，但搜索接口
返回 HTTP 200 + 纯文本「暂不支持搜索」。这类源以前会被巡检判成 state=ok、
hit=0，然后一直白占全局搜索的首轮名额（40 个）——搜不到任何东西。

本测试锁住三件事：
  1. 判定：正常 JSON → 支持；「暂不支持搜索」纯文本 / code=1002 → 不支持；
     未知错误 → 不判定（不能误伤能搜的源）；
  2. 配置语义：手动 no_search 优先于巡检自动判定，缺省参与搜索；
  3. 搜索取源里确实排除了它（不影响库）。
"""
import providers
import config as config_mod


# ---------------- 判定 ----------------

def test_json_with_list_means_supported():
    assert providers.judge_search_capability(
        body='{"code":1,"list":[]}', parsed={"code": 1, "list": []}) is True


def test_plain_text_unsupported_phrase_means_no_search():
    for body in ("暂不支持搜索", "抱歉，本接口不支持搜索", "搜索已关闭", "禁止搜索"):
        assert providers.judge_search_capability(body=body, parsed=None) is False


def test_business_code_1002_means_no_search():
    assert providers.judge_search_capability(
        body='{"code":1002,"msg":"禁止关键词搜索"}',
        parsed={"code": 1002, "msg": "禁止关键词搜索"}) is False


def test_unknown_error_is_not_judged():
    # HTML 错误页、未知业务码：一律 None，绝不能被当成「不支持搜索」而摘掉源
    assert providers.judge_search_capability(body="<html>502</html>", parsed=None) is None
    assert providers.judge_search_capability(body="", parsed=None) is None
    assert providers.judge_search_capability(
        body='{"code":-1,"msg":"参数错误"}', parsed={"code": -1, "msg": "参数错误"}) is None


def test_plain_text_with_normal_words_is_not_judged():
    assert providers.judge_search_capability(body="404 Not Found", parsed=None) is None


# ---------------- 配置语义 ----------------

def test_manual_flag_wins_over_auto():
    src = {"no_search": False, "quality": {"no_search": True}}
    assert config_mod.Config.source_no_search(src) is False
    src2 = {"no_search": True, "quality": {"no_search": False}}
    assert config_mod.Config.source_no_search(src2) is True


def test_auto_flag_used_when_manual_missing():
    assert config_mod.Config.source_no_search({"quality": {"no_search": True}}) is True
    assert config_mod.Config.source_no_search({"quality": {"no_search": False}}) is False


def test_default_is_searchable():
    # 老源没有任何 no_search 字段 → 必须继续参与搜索（行为不变）
    assert config_mod.Config.source_no_search({}) is False
    assert config_mod.Config.source_no_search({"quality": {}}) is False
    assert config_mod.Config.source_no_search({"quality": None}) is False
