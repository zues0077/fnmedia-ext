"""自定义虚拟库：state/custom_cats.json 增改删 + 动态分类池。

2026-11-13 新增能力：内置 10 库不够用时，用户在管理页手动加库
（如 ⚽体育 / VR影视），分类规则里就能把 LPL / VR视角 归进去。
固定库的 guid / 顺序 / 契约一个都不许变。
"""

import json

import pytest

from proxy import catalog


@pytest.fixture()
def cats_file(tmp_path, monkeypatch):
    f = tmp_path / "custom_cats.json"
    monkeypatch.setattr(catalog, "_CUSTOM_CATS_FILE", f)
    return f


def test_create_rename_delete_roundtrip(cats_file):
    ok, msg, cid = catalog.set_custom_cat("⚽体育", "Mix")
    assert ok and cid, msg
    assert [c["id"] for c in catalog.custom_cats()] == [cid]
    raw = json.loads(cats_file.read_text(encoding="utf-8"))
    assert raw["cats"][0]["title"] == "⚽体育"

    # 改名：id 不变（guid 稳定，客户端缓存不丢）
    ok, msg, _ = catalog.set_custom_cat("⚽体育赛事", "TV", cat_id=cid)
    assert ok, msg
    cats = catalog.custom_cats()
    assert cats[0]["id"] == cid and cats[0]["title"] == "⚽体育赛事"
    assert cats[0]["mdb"] == "TV"

    assert catalog.del_custom_cat(cid) == (True, "已删除")
    assert catalog.custom_cats() == []


def test_fixed_cats_never_change(cats_file):
    fixed_cats = list(catalog.CATS)
    fixed_table = list(catalog.CATALOG_CATEGORIES)
    catalog.set_custom_cat("VR影视", "Movie")
    catalog.set_custom_cat("体育", "Mix")
    assert catalog.CATS == fixed_cats
    assert catalog.CATALOG_CATEGORIES == fixed_table
    for c in catalog.custom_cats():
        assert c["id"] and c["id"] == c["id"].lower()
        assert c["id"] not in catalog.CATS and c["id"] != catalog.ADULT_CAT


def test_iter_categories_and_resolvers(cats_file):
    catalog.set_custom_cat("VR影视", "Movie")
    cats = [c for c, _t, _m in catalog.iter_categories()]
    assert cats == catalog.CATS + ["vr"]
    assert catalog.catalog_title("vr") == "VR影视"
    assert catalog.catalog_mdb("vr") == "Movie"
    # 固定库与福利库的标题/归属不受影响
    assert catalog.catalog_title("movie") == "🎞海量电影"
    assert catalog.catalog_title(catalog.ADULT_CAT) == "🔞福利"
    assert catalog.catalog_mdb(catalog.ADULT_CAT) == "Mix"


def test_all_cats_feeds_build_pool_and_rule_validation(cats_file):
    assert catalog.all_cats() == catalog.CATS
    catalog.set_custom_cat("体育", "Mix")
    cid = catalog.custom_cats()[0]["id"]
    assert catalog.all_cats() == catalog.CATS + [cid]
    # 规则可以指向自定义库
    ok, msg = catalog.set_rule("LPL", cid, True)
    assert ok, msg
    # 非法 cat 仍被拒绝
    assert not catalog.set_rule("x", "not_a_cat", True)[0]


def test_id_collision_with_fixed_and_adult(cats_file):
    ok, _msg, cid = catalog.set_custom_cat("Movie", "Mix")
    assert ok and cid not in catalog.CATS and cid != catalog.ADULT_CAT


def test_validation_errors(cats_file):
    assert not catalog.set_custom_cat("", "Mix")[0]
    assert not catalog.set_custom_cat("x" * 21, "Mix")[0]
    assert not catalog.set_custom_cat("库名", "Book")[0]
    catalog.set_custom_cat("体育", "Mix")
    assert not catalog.set_custom_cat("体育", "Mix")[0]  # 同名拒绝
    assert not catalog.set_custom_cat("别的", "Mix", cat_id="nope")[0]
    assert not catalog.del_custom_cat("nope")[0]


def test_broken_file_falls_back_empty(cats_file):
    cats_file.write_text("not json", encoding="utf-8")
    assert catalog.custom_cats() == []
    assert catalog.all_cats() == catalog.CATS
