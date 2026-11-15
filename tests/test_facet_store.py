"""本地筛选词表（state/facets.json）：只增不减、顺序固定、可跨重启。

用户要求（2026-11-15）：筛选项要和「归类」一样固定在本机——已经在列的条目
位置不动，新源带来的新条目只追加；重启、换榜单都不能把筛选项弄丢。
"""
import sys
import json
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

from facets import FacetStore


def _store():
    d = Path(tempfile.mkdtemp(prefix="facet-store-"))
    # 这些用例只测 merge/落盘语义，关掉内置默认词表，断言才不会被默认项污染
    return FacetStore(d / "facets.json", use_default=False), d / "facets.json"


class MergeTests(unittest.TestCase):
    def test_append_only_and_order_stable(self):
        st, _f = _store()
        assert st.merge("chart:movie", "genres", ["剧情", "爱情"]) == ["剧情", "爱情"]
        # 第二轮只出现「爱情」「喜剧」：已在列的保持原位，新的追加到末尾
        assert st.merge("chart:movie", "genres", ["爱情", "喜剧"]) == [
            "剧情", "爱情", "喜剧"]
        # 第三轮一个都没有：绝不删已有条目
        assert st.merge("chart:movie", "genres", []) == ["剧情", "爱情", "喜剧"]

    def test_dedupe_and_blank(self):
        st, _f = _store()
        st.merge("catalog:tv", "areas", ["日本", "日本", "", None, " 韩国 "])
        assert st.ordered("catalog:tv", "areas") == ["日本", "韩国"]

    def test_libs_are_independent(self):
        st, _f = _store()
        st.merge("chart:movie", "genres", ["剧情"])
        st.merge("chart:tv", "genres", ["古装"])
        assert st.ordered("chart:movie", "genres") == ["剧情"]
        assert st.ordered("chart:tv", "genres") == ["古装"]

    def test_cap_keeps_old_and_drops_new(self):
        st, _f = _store()
        st.merge("catalog:movie", "decades", [str(y) for y in range(2000, 2030)])
        first = st.ordered("catalog:movie", "decades")
        st.merge("catalog:movie", "decades", ["1900"])
        after = st.ordered("catalog:movie", "decades")
        assert after[:len(first)] == first      # 老的没被动
        assert "1900" not in after[:40] or len(after) <= 40

    def test_unknown_kind_is_ignored(self):
        st, _f = _store()
        assert st.merge("chart:movie", "whatever", ["x"]) == []


class PruneTests(unittest.TestCase):
    """prune 只给「预置词不再适用」的一次性迁移用，删完仍保持剩余顺序。"""

    def test_removes_only_named_entries(self):
        st, _f = _store()
        st.merge("catalog:adult", "genres", ["剧情", "国产精品", "古装"])
        assert st.prune("catalog:adult", "genres", ["剧情", "古装"]) == ["国产精品"]
        assert st.ordered("catalog:adult", "genres") == ["国产精品"]

    def test_noop_does_not_touch_file(self):
        st, f = _store()
        st.merge("catalog:movie", "genres", ["剧情"])
        before = f.read_text(encoding="utf-8")
        assert st.prune("catalog:movie", "genres", ["不存在"]) == ["剧情"]
        assert f.read_text(encoding="utf-8") == before

    def test_missing_lib_is_safe(self):
        st, _f = _store()
        assert st.prune("catalog:nope", "genres", ["x"]) == []
        assert st.prune("catalog:nope", "whatever", ["x"]) == []

    def test_prune_is_persisted(self):
        st, f = _store()
        st.merge("catalog:adult", "genres", ["剧情", "国产精品"])
        st.prune("catalog:adult", "genres", ["剧情"])
        again = FacetStore(f, use_default=False)
        assert again.ordered("catalog:adult", "genres") == ["国产精品"]


class PersistenceTests(unittest.TestCase):
    def test_survives_reload(self):
        st, f = _store()
        st.merge("chart:variety", "genres", ["真人秀", "脱口秀"])
        st.merge("chart:variety", "decades", ["2026"])
        # 模拟重启：新实例从同一文件读
        st2 = FacetStore(f, use_default=False)
        assert st2.ordered("chart:variety", "genres") == ["真人秀", "脱口秀"]
        assert st2.ordered("chart:variety", "decades") == ["2026"]

    def test_corrupt_file_starts_empty(self):
        d = Path(tempfile.mkdtemp(prefix="facet-bad-"))
        f = d / "facets.json"
        f.write_text("{not json", encoding="utf-8")
        st = FacetStore(f, use_default=False)   # 不抛异常
        assert st.ordered("chart:movie", "genres") == []
        st.merge("chart:movie", "genres", ["剧情"])   # 写入自动修好文件
        assert FacetStore(f, use_default=False).ordered(
            "chart:movie", "genres") == ["剧情"]

    def test_version_mismatch_is_dropped(self):
        d = Path(tempfile.mkdtemp(prefix="facet-ver-"))
        f = d / "facets.json"
        f.write_text('{"v": 999, "libs": {"chart:movie": {"genres": ["剧情"]}}}',
                     encoding="utf-8")
        assert FacetStore(f, use_default=False).ordered(
            "chart:movie", "genres") == []


if __name__ == "__main__":
    unittest.main()


class DefaultVocabTests(unittest.TestCase):
    """随包内置默认词表：新装/换机即有一整套筛选项，不必从零累积。"""

    def _pair(self):
        d = Path(tempfile.mkdtemp(prefix="facet-default-"))
        return d / "facets.json", d

    def test_new_install_uses_bundled_default_without_writing(self):
        state, d = self._pair()
        st = FacetStore(state)          # 无 state 文件 → 读 proxy/assets/facets.default.json
        snap = st.snapshot()
        self.assertTrue(snap, "内置默认词表应被加载")
        self.assertIn("chart:movie", snap)
        self.assertGreaterEqual(len(snap["chart:movie"]["genres"]), 20)
        # 只进内存，不因为「读默认」就写出 state 文件
        self.assertFalse(state.exists())

    def test_local_state_wins_over_default(self):
        state, d = self._pair()
        state.write_text(json.dumps({"v": 1, "libs": {
            "chart:movie": {"genres": ["只有这个"], "areas": [], "decades": []}}}),
            encoding="utf-8")
        st = FacetStore(state)
        self.assertEqual(st.ordered("chart:movie", "genres"), ["只有这个"])
        self.assertEqual(st.ordered("chart:tv", "genres"), [])   # 不回退混入默认

    def test_first_merge_persists_default_plus_new(self):
        state, d = self._pair()
        st = FacetStore(state)
        before = len(st.ordered("chart:movie", "genres"))
        st.merge("chart:movie", "genres", ["源里新来的题材"])
        self.assertTrue(state.exists())
        again = FacetStore(state)
        got = again.ordered("chart:movie", "genres")
        self.assertEqual(len(got), before + 1)
        self.assertEqual(got[-1], "源里新来的题材")
