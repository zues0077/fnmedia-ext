"""榜单四库（热门电影/剧集/综艺/动漫）的筛选：类型 + 国家/地区。

数据来源是豆瓣卡片的 card_subtitle（"2026 / 韩国 / 剧情 爱情 / 导演 / 演员"）：
第 2 段地区、第 3 段题材。这不是猜的，是 2026-11-15 在 246 上从 chart_cache
取真实条目确认过的格式。

分辨率/动态范围/音频规格不提供：源站 CMS 接口与豆瓣都不返回，逐条探测不现实，
填静态枚举只会让用户点了筛出 0 条。
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import app as appmod
import charts


def _item(title, sub, guid):
    return {"guid": guid, "title": title, "subtitle": sub, "cat": "movie",
            "cover": "https://img.test/a.jpg", "rating": 8.6, "year": "2026",
            "tags": [], "desc": ""}


ITEMS = [
    _item("可能的爱", "2026 / 韩国 / 剧情 爱情 / 李沧东 / 全度妍 薛景求", "g1"),
    _item("某片", "2026 / 西班牙 法国 / 剧情 同性 历史 / x / y", "g2"),
    _item("某综艺", "2026 / 中国大陆 / 真人秀 / z / w", "g3"),
]


class ParseSubtitleTests(unittest.TestCase):
    def test_areas_and_genres(self):
        a, g = charts.parse_subtitle(
            "2026 / 韩国 / 剧情 爱情 / 李沧东 / 全度妍 薛景求")
        self.assertEqual(a, ["韩国"])
        self.assertEqual(g, ["剧情", "爱情"])

    def test_multi_area(self):
        a, _g = charts.parse_subtitle("2026 / 西班牙 法国 / 剧情 同性 / x / y")
        self.assertEqual(a, ["西班牙", "法国"])

    def test_missing_segments_is_empty(self):
        self.assertEqual(charts.parse_subtitle(""), ([], []))
        self.assertEqual(charts.parse_subtitle("2026 / 韩国"), (["韩国"], []))


def _no_seed():
    """关掉豆瓣标准词表，专注测「数据驱动」那部分（否则断言要连标准词一起数）。"""
    return patch.object(appmod, "_lib_genre_seed", return_value=[])


class _FacetIsolated(unittest.TestCase):
    """本地词表落到临时目录，避免测试把 state/facets.json 写脏（会跨用例串味）。"""

    def setUp(self):
        super().setUp()
        import tempfile
        from facets import FacetStore
        d = Path(tempfile.mkdtemp(prefix="facets-"))
        p = patch.object(appmod, "facets_store", FacetStore(d / "facets.json", use_default=False))
        p.start()
        self.addCleanup(p.stop)


class ChartFacetTests(_FacetIsolated):
    def setUp(self):
        super().setUp()
        appmod._CHART_FACET_CACHE.clear()
        self.addCleanup(appmod._CHART_FACET_CACHE.clear)

    def _facets(self):
        with _no_seed(), patch.object(appmod.chart_cache, "items", return_value=ITEMS):
            return appmod._chart_facets("movie")

    def test_genres_and_areas_collected(self):
        f = self._facets()
        self.assertEqual(set(f["genres"]), {"剧情", "爱情", "同性", "历史", "真人秀"})
        self.assertEqual(set(f["areas"]), {"韩国", "西班牙", "法国", "中国大陆"})

    def test_ids_are_stable_and_in_own_range(self):
        f1 = self._facets()
        appmod._CHART_FACET_CACHE.clear()
        f2 = self._facets()
        self.assertEqual(f1["genres"], f2["genres"])
        for gid in f1["genres"].values():
            self.assertTrue(appmod._CHART_GENRE_ID_BASE <= gid <= appmod._CHART_GENRE_ID_MAX, gid)

    def test_payload_leaves_undetectable_dims_empty(self):
        with _no_seed(), patch.object(appmod.chart_cache, "items", return_value=ITEMS):
            p = appmod._chart_tag_filter_payload("movie")
        self.assertTrue(p["genres"] and p["locate"])
        # 分辨率/动态范围/音频：源站没有条目级数据，给静态枚举让按钮可点
        # （用户 2026-11-15 明确接受"哪怕点了可能空"）
        self.assertEqual(p["resolutions"], ["4k", "1080P", "720P", "480P"])
        self.assertEqual(p["color_range"], ["HDR10", "HLG", "SDR"])
        self.assertEqual(p["audio_type"], ["杜比环绕", "杜比全景声", "DTS", "立体声"])
        # 年代 = 豆瓣式标准年代档（最近 8 年 + 十年段）+ 条目真实年份
        self.assertIn("2026", p["decades"])
        self.assertTrue(any(d.endswith("年代") for d in p["decades"]), p["decades"])

    def test_genre_id_maps_back_to_name(self):
        with _no_seed(), patch.object(appmod.chart_cache, "items", return_value=ITEMS):
            f = appmod._chart_facets("movie")
            gid = f["genres"]["剧情"]
            self.assertEqual(appmod._chart_genre_name("movie", gid), "剧情")
            self.assertIsNone(appmod._chart_genre_name("movie", 123))


class ChartFilterTests(_FacetIsolated):
    def setUp(self):
        super().setUp()
        appmod._CHART_FACET_CACHE.clear()
        self.addCleanup(appmod._CHART_FACET_CACHE.clear)

    def _rows(self, tags):
        body = {"tags": tags, "page": 1, "page_size": 22}
        with patch.object(appmod.chart_cache, "items", return_value=ITEMS), \
                patch.object(appmod, "CHART_LIB_GUIDS", {"movie": "libguid"}), \
                patch.object(appmod, "_apply_marks", lambda r: r):
            appmod.CHART_LIB_GUIDS.update({"movie": "libguid"})
            return appmod._chart_rows_all(body, "movie")

    def test_filter_by_genre(self):
        with _no_seed(), patch.object(appmod.chart_cache, "items", return_value=ITEMS):
            gid = appmod._chart_facets("movie")["genres"]["真人秀"]
        rows = self._rows({"genres": gid})
        self.assertEqual(len(rows), 1, [r.get("title") for r in rows])

    def test_filter_by_area(self):
        rows = self._rows({"locate": "西班牙"})
        self.assertEqual(len(rows), 1, [r.get("title") for r in rows])

    def test_no_filter_returns_all(self):
        rows = self._rows({})
        self.assertEqual(len(rows), len(ITEMS))


if __name__ == "__main__":
    unittest.main()
