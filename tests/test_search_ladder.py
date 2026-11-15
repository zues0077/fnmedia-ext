"""搜索阶梯（relaxation ladder）、别名回灌、覆盖率细排。

背景（2026-11-15 调研）：苹果 CMS 的 `wd` 是 `LIKE '%关键词%'`（整串包含，
不是分词），所以长片名常被源站直接返回空。业界的做法是「逐级放宽查询、
命中即停」（Algolia removeWordsIfNoResults / Bloomreach 的 relaxation
ladder），而不是「一次把所有派生词并发发完」——后者白吃预算。

本文件覆盖：
- `_ladder_queries`：派生词在前、别名在后，去重、不重复原词；
- `_expand_briefs`：第一级就够数时不再发第二级（命中即停）；
- `_merge_online_titles`：被折叠进来的其它写法记进 brief["aliases"]；
- `_kw_coverage`：同档相关度内的 2-gram 覆盖率细排。
"""
import asyncio
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import app as appmod


def _mk(name: str, year: str, provider: str, line: str = "m3u8"):
    ep = types.SimpleNamespace(title="正片", url="https://cdn.test/v.m3u8",
                               direct=True, resolve=False)
    return appmod.providers.OnlineTitle(
        provider=provider, vod_id=provider, title=name, type_name="电视剧",
        year=year, area="", updated="", remarks="", score="", poster="",
        overview="", actor="", director="", sources={line: [ep]})


def _ep(url="https://cdn.test/v.m3u8"):
    return {"title": "正片", "url": url, "direct": True, "resolve": False}


def _card(title, provider):
    return {
        "title": title,
        "guid": appmod.merged_guid(appmod._merge_key(title)),
        "lines": {f"{provider}::m3u8": [_ep()]},
        "aliases": [],
    }


class LadderQueryTests(unittest.TestCase):
    def test_variants_first_then_aliases(self):
        briefs = [_card("给阿嬷的情书", "s1")]
        briefs[0]["aliases"] = ["情书阿嬷"]
        steps = appmod._ladder_queries(
            "给阿嫲的一封情书", briefs, appmod._merge_key("给阿嫲的一封情书")[0])
        assert steps, steps
        # 派生词（去掉「给/一封」并做字形归一）排在别名前面
        assert steps[0] == "阿嬷的情书", steps
        assert "情书阿嬷" in steps, steps
        assert steps.index("情书阿嬷") > 0, steps
        # 原词不进阶梯，且没有重复项
        assert "给阿嫲的一封情书" not in steps
        assert len(steps) == len(set(steps))

    def test_aliases_only_come_from_related_cards(self):
        unrelated = _card("沙丘2", "s9")
        unrelated["aliases"] = ["沙丘"]
        steps = appmod._ladder_queries(
            "给阿嫲的一封情书", [unrelated],
            appmod._merge_key("给阿嫲的一封情书")[0])
        assert "沙丘" not in steps, steps


class LadderStopTests(unittest.IsolatedAsyncioTestCase):
    async def test_stops_after_first_step_when_enough(self):
        first = [_card("给阿嬷的情书", "s1")]
        extra = [_card("阿嬷的情书", "s2"),
                 _card("阿嬷情书", "s3"),
                 _card("给阿嬷情书", "s4")]
        calls = []

        async def fake_agg(kw, **_kw):
            calls.append(kw)
            return list(extra)

        with patch.object(appmod, "_aggregate_briefs", new=fake_agg), \
                patch.object(appmod.store, "remember_merged"):
            out = await appmod._expand_briefs(
                "给阿嫲的一封情书", first, fast_only=False)
        assert len(calls) == 1, calls          # 命中即停，不再发第二级
        assert len(out) >= 3, out

    async def test_keeps_going_when_first_step_gains_nothing(self):
        first = [_card("给阿嬷的情书", "s1")]
        calls = []

        async def fake_agg(kw, **_kw):
            calls.append(kw)
            return []                          # 这一级没新东西

        with patch.object(appmod, "_aggregate_briefs", new=fake_agg), \
                patch.object(appmod.store, "remember_merged"):
            out = await appmod._expand_briefs(
                "给阿嫲的一封情书", first, fast_only=False)
        assert len(calls) >= 1
        assert len(out) == 1, out


class AliasCaptureTests(unittest.TestCase):
    def test_folded_title_becomes_alias(self):
        """「甄嬛传」被折进「后宫·甄嬛传」后，前者要记成别名供下次补搜。"""
        titles = [_mk("甄嬛传", "2011", "s1"), _mk("后宫·甄嬛传", "2011", "s2")]
        out = appmod._merge_online_titles(titles, [], "甄嬛传", 20)
        assert len(out) == 1, [b["title"] for b in out]
        assert "甄嬛传" in (out[0].get("aliases") or []), out[0].get("aliases")


class CoverageTests(unittest.TestCase):
    def test_coverage_counts_query_bigrams(self):
        full = appmod._kw_coverage("给阿嬷的情书", "阿嬷的情书")
        half = appmod._kw_coverage("情书来自五年后", "给阿嬷的情书")
        none = appmod._kw_coverage("沙丘2", "给阿嬷的情书")
        assert full == 100, full
        assert 0 < half < full, half
        assert none == 0, none


class LadderTimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_step_cannot_exceed_budget(self):
        """共享聚合任务在飞时 _aggregate_briefs 会忽略自己的 timeout，
        实测能把一次搜索拖到 62 秒；阶梯必须用 wait_for 硬砍。"""
        first = [_card("给阿嬷的情书", "s1")]
        calls = []

        async def slow_agg(kw, **_kw):
            calls.append(kw)
            await asyncio.sleep(3)      # 模拟慢源（真实是 60 秒超时）
            return []

        import time as _t
        with patch.object(appmod, "_aggregate_briefs", new=slow_agg), \
                patch.object(appmod.store, "remember_merged"), \
                patch.object(appmod, "_LADDER_STEP_TIMEOUT", 0.2), \
                patch.object(appmod, "_LADDER_BUDGET", 2.0):
            t0 = _t.monotonic()
            out = await appmod._expand_briefs("给阿嫲的一封情书", first,
                                              fast_only=False)
            cost = _t.monotonic() - t0
        assert cost < 2.5, cost               # 没有被 3 秒的慢任务拖住
        assert calls, calls                   # 阶梯确实尝试过
        assert len(out) == 1
