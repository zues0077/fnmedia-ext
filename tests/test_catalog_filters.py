"""海量四库筛选：类型（CMS 分类名）与地区（资源站 area）。

飞牛筛选条 = /tag/list 给维度 + /tag/genres、/tag/iso3166 给按钮文案 +
item/list 收到 tags 后按维度过滤。加固专区已按这套跑通，这里测海量四库的移植。
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import app as appmod


def _no_stale():
    """兼容旧测试：新实现直接读内存路由表，不再关心 stale 标记。"""
    return patch.object(appmod.catalog, "routes_stale", return_value=False)


def _routes(names, cat="movie"):
    """构造内存路由表替身：_cat_class_names 直接读 catalog._routes（不加锁）。"""
    import catalog as catalogmod
    key = appmod.catalog._scope_key(catalogmod.AGG_SCOPE, cat)
    return patch.object(appmod.catalog, "_routes",
                        {key: [{"type_name": n} for n in names]})


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


class CatGenreMapTests(_FacetIsolated):
    def setUp(self):
        super().setUp()
        appmod._CAT_GENRE_CACHE.clear()
        self.addCleanup(appmod._CAT_GENRE_CACHE.clear)
    def test_ids_are_stable_and_in_reserved_range(self):
        with _no_seed(), _routes(["动作片", "喜剧片", "科幻片"]):
            m1 = appmod._cat_genre_map("movie")
            m2 = appmod._cat_genre_map("movie")
        assert m1 == m2, (m1, m2)
        assert set(m1) == {"动作", "喜剧", "科幻"}, m1   # 「片」后缀统一去掉
        for gid in m1.values():
            assert appmod._CAT_GENRE_ID_BASE <= gid <= appmod._CAT_GENRE_ID_MAX

    def test_single_class_keeps_same_id(self):
        """分类名不变时 id 不随其它分类增删漂移。"""
        with _no_seed(), _routes(["动作片"]):
            one = appmod._cat_genre_map("movie")
        with _no_seed(), _routes(["动作片", "喜剧片"]):
            two = appmod._cat_genre_map("movie")
        assert one["动作"] == two["动作"]

    def test_name_roundtrip(self):
        with _no_seed(), _routes(["动作片"]):
            gid = appmod._cat_genre_map("movie")["动作"]
            assert appmod._cat_genre_name("movie", gid) == "动作"
            assert appmod._cat_genre_name("movie", str(gid)) == "动作"
        assert appmod._cat_genre_name("movie", None) is None
        assert appmod._cat_genre_name("movie", "not-a-number") is None


class CatFacetPayloadTests(_FacetIsolated):
    def setUp(self):
        super().setUp()
        appmod._CAT_GENRE_CACHE.clear()
        self.addCleanup(appmod._CAT_GENRE_CACHE.clear)
    def test_payload_shape(self):
        with _no_seed(), _routes(["动作片", "喜剧片"]), \
                patch.object(appmod, "_cat_area_options",
                             return_value=["中国大陆", "美国"]), \
                patch.object(appmod, "_cat_decade_options",
                             return_value=["2026", "2025"]):
            p = appmod._cat_tag_filter_payload("movie")
        assert len(p["genres"]) == 2
        assert all(isinstance(g, int) for g in p["genres"])
        assert p["locate"] == ["中国大陆", "美国"]
        # 分辨率/动态范围/音频：源站无条目级数据，给静态枚举让按钮可点
        # （用户 2026-11-15 明确接受"点了可能空"）
        assert p["resolutions"] == ["4k", "1080P", "720P", "480P"]
        assert p["color_range"] == ["HDR10", "HLG", "SDR"]
        assert p["audio_type"] == ["杜比环绕", "杜比全景声", "DTS", "立体声"]
        assert p["decades"] == ["2026", "2025"]

    def test_area_options_dedup_and_cap(self):
        briefs = [{"area": "大陆"}, {"area": "大陆"}, {"area": "美国"},
                  {"area": "英国,美国"}, {"area": "泰国 / 中国大陆"},
                  {"area": "其它"}, {"area": ""}, {}]
        with patch.object(appmod.catalog, "cached_dense", return_value=briefs):
            # 大陆→中国大陆、逗号/斜杠拼接要拆开、其它要丢弃
            assert appmod._cat_area_options("movie") == [
                "中国大陆", "美国", "英国", "泰国"]


class TagScalarTests(unittest.TestCase):
    def test_scalar_and_list_forms(self):
        assert appmod._tag_scalar({"genres": 700001}, "genres") == 700001
        assert appmod._tag_scalar({"genres": [700001]}, "genres") == 700001
        assert appmod._tag_scalar({"genres": [700001, 700002]}, "genres") == 700001
        assert appmod._tag_scalar({"genres": -1}, "genres") is None
        assert appmod._tag_scalar({"genres": ""}, "genres") is None
        assert appmod._tag_scalar(None, "genres") is None
        # 兼容旧字段名
        assert appmod._tag_scalar({"genre": 700009}, "genres", "genre") == 700009


# ---------- 地区归一 / 分类名清洗 / 年代桶 / 动漫标签兜底 ----------

class AreaNormalizeTests(unittest.TestCase):
    def test_alias_and_split(self):
        assert appmod._split_areas("大陆") == ["中国大陆"]
        assert appmod._split_areas("香港") == ["中国香港"]
        assert appmod._split_areas("英国,美国") == ["英国", "美国"]
        assert appmod._split_areas("克罗地亚 / 波兰 / 加拿大 / 波黑") == [
            "克罗地亚", "波兰", "加拿大", "波黑"]
        assert appmod._split_areas("泰国 / 中国大陆") == ["泰国", "中国大陆"]
        assert appmod._split_areas("其它") == []
        assert appmod._split_areas("") == []
        assert appmod._split_areas(None) == []


class ClassNameTests(unittest.TestCase):
    def test_strip_pian_suffix(self):
        assert appmod._clean_class_name("动作片") == "动作"
        assert appmod._clean_class_name("喜剧片") == "喜剧"
        # 「纪录片」不能被砍成「纪录」
        assert appmod._clean_class_name("纪录片") == "纪录片"
        assert appmod._clean_class_name("") == ""


class DecadeTests(unittest.TestCase):
    def test_single_years_then_buckets(self):
        years = [str(y) for y in range(2026, 2000, -1)]
        out = appmod._decades_of(years)
        assert out[:3] == ["2026", "2025", "2024"]
        # 第 13 个之后的年份折成十年段
        assert any(x.endswith("年代") for x in out), out

    def test_year_bucket(self):
        assert appmod._year_bucket("2026") == "2026"
        assert appmod._year_bucket("") == ""
        assert appmod._year_bucket(None) == ""


class ChartTagFallbackTests(_FacetIsolated):
    """动漫新番只有 tags=['日本动画']、没有 subtitle——要能反推出「日本 / 动画」。"""

    def test_anime_tags_fallback(self):
        appmod._CHART_FACET_CACHE.clear()
        items = [{"guid": "a1", "title": "某番", "subtitle": "", "year": "",
                  "tags": ["日本动画"], "cat": "anime", "cover": "x", "rating": 8}]
        with _no_seed(), patch.object(appmod.chart_cache, "items", return_value=items):
            f = appmod._chart_facets("anime")
        assert list(f["genres"]) == ["动画"], f
        assert f["areas"] == ["日本"], f
        appmod._CHART_FACET_CACHE.clear()


class LibGenreSeedTests(unittest.TestCase):
    def test_builtin_libs_have_standard_vocab(self):
        movie = appmod._lib_genre_seed("movie")
        self.assertGreaterEqual(len(movie), 20)
        self.assertIn("剧情", movie)
        self.assertIn("纪录片", movie)
        tv = appmod._lib_genre_seed("tv")
        self.assertIn("古装", tv)          # 剧集特有
        variety = appmod._lib_genre_seed("variety")
        self.assertEqual(variety[:2], ["真人秀", "脱口秀"])   # 综艺特有
        self.assertTrue(appmod._lib_genre_seed("documentary"))
        self.assertTrue(appmod._lib_genre_seed("uhd"))

    def test_custom_cats_fall_back_to_generic(self):
        """自定义分类（lib/lib2…）没有专属词表时退回通用题材，不能空着。"""
        for cat in ("lib", "lib2", "lib3", "lib4", "lib5", "lib6", "custom_x"):
            got = appmod._lib_genre_seed(cat)
            self.assertTrue(got, cat)
            self.assertIn("剧情", got, cat)
            self.assertIn("古装", got, cat)

    def test_seed_decades_shape(self):
        ds = appmod._seed_decades()
        self.assertEqual(ds[0], str(appmod.time.localtime().tm_year))
        self.assertTrue(any(d.endswith("年代") for d in ds), ds)


class AdultLibFacetTests(_FacetIsolated):
    """福利库：不预置题材，只吃资源站自动入账（用户 2026-11-15 要求）。

    福利库本来就靠后台「扩展资源库」开关才出现；开启后筛选项跟着源返回的分类
    走即可，不必事先铺一套电影/剧集题材（会冒出「古装/家庭/情色」这类无关按钮）。
    """

    def setUp(self):
        super().setUp()
        import catalog as catalogmod
        self.adult = catalogmod.ADULT_CAT
        appmod._CAT_GENRE_CACHE.clear()
        self.addCleanup(appmod._CAT_GENRE_CACHE.clear)
        # 一次性清理是进程级开关，测试之间要还原
        self._flag = appmod._ADULT_SEED_PRUNED
        appmod._ADULT_SEED_PRUNED = False
        self.addCleanup(setattr, appmod, "_ADULT_SEED_PRUNED", self._flag)

    def _empty_routes(self):
        return patch.object(appmod.catalog, "_routes", {}), \
            patch.object(appmod.catalog, "cached_dense", return_value=[])

    def test_no_standard_vocab(self):
        self.assertEqual(appmod._lib_genre_seed(self.adult), [])
        # 其它库照旧
        self.assertTrue(appmod._lib_genre_seed("movie"))
        self.assertTrue(appmod._lib_genre_seed("lib4"))

    def test_auto_merge_only_real_classes(self):
        appmod._merge_catalog_facets(self.adult, [
            {"type_name": "国产精品", "area": "", "year": ""},
            {"type_name": "欧美精品", "area": "美国", "year": "2024"}])
        got = appmod.facets_store.ordered(f"catalog:{self.adult}", "genres")
        self.assertEqual(got, ["国产精品", "欧美精品"])
        self.assertNotIn("剧情", got)

    def test_legacy_generic_vocab_removed_once(self):
        st = appmod.facets_store
        # 旧版本留下的痕迹：通用兜底词和资源站真实分类混在一起
        st.merge(f"catalog:{self.adult}", "genres",
                 ["剧情", "喜剧", "古装", "国产精品", "家庭"])
        p1, p2 = self._empty_routes()
        with p1, p2:
            got = appmod._cat_class_names(self.adult)
        self.assertEqual(got, ["国产精品"])       # 通用词剔除，真实分类保留
        # 清理只做一次：再调不应把词带回来，也不改变已有内容
        p3, p4 = self._empty_routes()
        with p3, p4:
            self.assertEqual(appmod._cat_class_names(self.adult), ["国产精品"])

    def test_prune_leaves_other_libs_alone(self):
        st = appmod.facets_store
        st.merge("catalog:movie", "genres", ["剧情", "古装"])
        appmod._prune_adult_seed()
        self.assertEqual(st.ordered("catalog:movie", "genres"), ["剧情", "古装"])

    def test_real_class_named_like_generic_keeps_position(self):
        """「爱情」「家庭」既是通用兜底词、也可能是源里真有的分类，不能删。"""
        st = appmod.facets_store
        st.merge(f"catalog:{self.adult}", "genres",
                 ["国产精品", "爱情", "剧情", "家庭"])
        with _routes(["爱情", "家庭"], cat=self.adult):
            got = appmod._cat_class_names(self.adult)
        self.assertEqual(got, ["国产精品", "爱情", "家庭"])   # 只删了「剧情」


class CustomCatDecadeTests(unittest.TestCase):
    """自定义分类也要有年代档（用户 2026-11-15：自定义分类也铺）。"""

    def test_custom_cat_gets_seed_decades(self):
        from facets import FacetStore
        import tempfile
        d = Path(tempfile.mkdtemp(prefix="facet-custom-"))
        p = patch.object(appmod, "facets_store", FacetStore(d / "facets.json", use_default=False))
        p.start()
        self.addCleanup(p.stop)
        with patch.object(appmod.catalog, "cached_dense", return_value=[]):
            self.assertEqual(appmod._cat_decade_options("lib4"),
                             appmod._seed_decades())
            self.assertTrue(appmod._cat_decade_options("movie"))


class AutoMergeTests(_FacetIsolated):
    """源一返回数据就要自己进词表——不能等用户再点一次筛选。"""

    def test_listing_merges_without_opening_filter(self):
        briefs = [{"type_name": "动作片", "area": "英国,美国", "year": "2024"},
                  {"type_name": "科幻片", "area": "大陆", "year": ""}]
        with patch.object(appmod, "_lib_genre_seed", return_value=[]):
            appmod._merge_catalog_facets("movie", briefs)
        st = appmod.facets_store
        self.assertEqual(st.ordered("catalog:movie", "genres"), ["动作", "科幻"])
        self.assertEqual(st.ordered("catalog:movie", "areas"), ["英国", "美国", "中国大陆"])
        self.assertIn("2024", st.ordered("catalog:movie", "decades"))

    def test_seed_stays_in_front_of_new_data(self):
        """标准词表必须排在真实分类前面，否则筛选条顺序会被数据打乱。"""
        appmod._merge_catalog_facets(
            "movie", [{"type_name": "冷门题材", "area": "", "year": ""}])
        got = appmod.facets_store.ordered("catalog:movie", "genres")
        self.assertEqual(got[0], "剧情")          # 标准词第一个
        self.assertIn("冷门题材", got)            # 真实分类追加在后
        self.assertGreater(got.index("剧情"), -1)
        self.assertGreater(got.index("冷门题材"), got.index("纪录片"))

    def test_empty_briefs_is_noop(self):
        appmod._merge_catalog_facets("movie", [])
        self.assertEqual(appmod.facets_store.ordered("catalog:movie", "genres"), [])
