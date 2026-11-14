"""搜索扩写与近似片名归并（2026-11-14 用户实机问题驱动）。

背景：搜「给阿嫲的一封情书」只命中 2 个站（3 条线路），搜「阿嬷的情书」
能命中 5 个站。原因不是合并丢源，而是**搜索词原样发给各资源站**：
子串匹配的站对长词返回空，只有分词匹配的站能中。

两条互补的修法（均无新依赖）：
1. `_query_variants` + `_expand_briefs`：首轮结果太少时，用派生短词再聚合一轮；
2. `_near_same_title`：同季内片名高度相似（包含/2-gram）视为同一部，归到更长的名字。
"""
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import app as appmod
import providers


def _mk(name: str, year: str, provider: str, line: str = "m3u8"):
    ep = types.SimpleNamespace(title="正片", url="https://cdn.test/v.m3u8",
                               direct=True, resolve=False)
    return providers.OnlineTitle(
        provider=provider, vod_id=provider, title=name, type_name="电影",
        year=year, area="", updated="", remarks="", score="", poster="",
        overview="", actor="", director="", sources={line: [ep]})



# ---------- 查询扩写 ----------

def test_query_variants_strip_leading_and_quantifier():
    # 字形归一后的查询排在最前（嫲→嬷），因为源站数据常用另一种写法
    got = appmod._query_variants("给阿嫲的一封情书", limit=3)
    assert got[:2] == ["阿嬷的情书", "阿嬷情书"], got
    assert "阿嫲的情书" in got
    assert appmod._query_variants("一封情书") == ["情书"]


def test_query_variants_keep_short_queries_untouched():
    # 没有可剥的修饰词时不派生，避免无意义地多打一轮源站
    assert appmod._query_variants("流浪地球2") == []
    assert appmod._query_variants("") == []


# ---------- 近似片名 ----------

def test_near_same_title_merges_aliases():
    assert appmod._near_same_title("甄嬛传", "后宫甄嬛传")
    assert appmod._near_same_title("给阿嬷的情书", "给阿嬷情书")


def test_near_same_title_keeps_different_works_apart():
    assert not appmod._near_same_title("庆余年", "庆余生")
    assert not appmod._near_same_title("庆余年", "庆余年之少年风流")
    assert not appmod._near_same_title("给阿嬷的情书", "给阿嬷的梦中情人")
    # 短名不参与包含合并，否则「情书」会被任何带这两个字的片名吞并
    assert not appmod._near_same_title("情书", "给阿嬷的情书")


# ---------- 结果并集 ----------

def test_merge_brief_lists_adds_lines_and_drops_unrelated():
    primary = [{"title": "给阿嬷的情书", "guid": "g1",
                "lines": {"a::x": [{"direct": True}]}}]
    extra = [{"title": "给阿嬷的情书-正片", "guid": "g2",
              "lines": {"b::y": [{"direct": True}]}},
             {"title": "阿嬷的梦中情人", "guid": "g3",
              "lines": {"c::z": [{"direct": True}]}}]
    out = appmod._merge_brief_lists(primary, extra, "给阿嫲的一封情书")
    assert len(out) == 1, [b["title"] for b in out]
    assert set(out[0]["lines"]) == {"a::x", "b::y"}
    assert out[0]["guid"] == "g1"      # 主结果身份不变


def test_merge_brief_lists_keeps_relevant_new_title():
    primary = []
    extra = [{"title": "阿嫲的情书", "guid": "g2",
              "lines": {"b::y": [{"direct": True}]}}]
    out = appmod._merge_brief_lists(primary, extra, "给阿嫲的一封情书")
    assert [b["guid"] for b in out] == ["g2"]


# ---------- 归并层：近似片名在分组时真的合并 ----------

def test_grouping_merges_near_identical_titles():
    out = appmod._merge_online_titles(
        [_mk("给阿嬷的情书", "2024", "s1"), _mk("给阿嬷情书", "2024", "s2")],
        [], "给阿嫲的一封情书", 20)
    assert len(out) == 1, [b["title"] for b in out]
    assert len(out[0]["lines"]) == 2
