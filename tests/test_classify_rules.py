"""本地分类规则库：一份规则，所有源共用。

覆盖：默认规则命中、规则优先于内置关键词表、关键词取最长、明确丢弃不被
18 兜底捞回、用户覆盖/删除/落盘/损坏容错、未归类池汇总。
"""
import json

import pytest

import catalog


@pytest.fixture(autouse=True)
def _clean_rule_cache():
    """每个用例前后都清缓存：_RULES_FILE 被 monkeypatch 换过，缓存必须重读。"""
    catalog.reload_rules()
    yield
    catalog.reload_rules()


def _use_tmp_rules(monkeypatch, tmp_path):
    f = tmp_path / "classify_rules.json"
    monkeypatch.setattr(catalog, "_RULES_FILE", f)
    catalog.reload_rules()
    return f


def _welfare_on(monkeypatch, on=True):
    monkeypatch.setattr(catalog, "_welfare_on", lambda: on)


def test_default_rules_cover_real_unclassified_names():
    """默认规则必须覆盖 246 实测出来的高频未归类名（1926 条里 509 条认不出）。"""
    cases = {
        "古装仙侠": "shortdrama", "反转爽剧": "shortdrama", "重生民国": "shortdrama",
        "都市脑洞": "shortdrama",
        "足球": None, "篮球": None, "体育赛事": None, "斯诺克": None,
        "预告片": None, "影视解说": None, "资讯": None,
        "内地剧": "tv", "大陆剧": "tv",
        "港澳剧": "overseas", "港剧": "overseas", "Netflix自制剧": "overseas",
        "视频一区": "adult", "两性课堂": "adult",
    }
    for name, want in cases.items():
        hit, cat = catalog.rule_hit(name)
        assert hit, "默认规则未覆盖：%s" % name
        assert cat == want, "%s 期望 %s 实际 %s" % (name, want, cat)


def test_rule_beats_builtin_classifier(monkeypatch):
    """规则优先于内置关键词表：「成人动漫」归福利，不被「动漫」抢成 anime。"""
    _welfare_on(monkeypatch)
    assert catalog.classify("动漫") == "anime"
    # 内置表里 _EXCLUDE_KW 有「成人」，直接把这名字当噪音丢了
    assert catalog.classify("成人动漫") is None
    assert catalog.classify_source_class("成人动漫", False) == catalog.ADULT_CAT


def test_drop_rule_not_rescued_by_adult_mark(monkeypatch):
    """规则写「丢弃」后，源打了 18 也不能把它兜底捞进福利库。"""
    _welfare_on(monkeypatch)
    assert catalog.classify_source_class("足球", adult_allowed=True) is None
    # 对照组：规则没点名的杂项，打 18 仍会进福利库（成人站靠这条兜底）
    assert catalog.classify_source_class("某某频道", adult_allowed=True) == catalog.ADULT_CAT


def test_user_rule_overrides_default(monkeypatch, tmp_path):
    f = _use_tmp_rules(monkeypatch, tmp_path)
    _welfare_on(monkeypatch)
    assert catalog.classify_source_class("足球", False) is None

    ok, _ = catalog.set_rule("足球", "variety")
    assert ok and f.exists()
    assert catalog.classify_source_class("足球", False) == "variety"

    ok, _ = catalog.del_rule("足球")
    assert ok
    # 删除自定义规则 → 回落到默认的「丢弃」
    assert catalog.rule_hit("足球") == (True, None)


def test_exact_beats_keyword(monkeypatch, tmp_path):
    _use_tmp_rules(monkeypatch, tmp_path)
    assert catalog.set_rule("爽剧", "shortdrama", exact=False)[0]
    assert catalog.set_rule("战神爽剧", "variety")[0]
    # 精确名优先，关键词再长也让位
    assert catalog.rule_hit("战神爽剧")[1] == "variety"
    # 只沾关键词的仍按关键词走
    assert catalog.rule_hit("古装爽剧")[1] == "shortdrama"


def test_set_rule_rejects_unknown_cat(monkeypatch, tmp_path):
    _use_tmp_rules(monkeypatch, tmp_path)
    ok, msg = catalog.set_rule("某分类", "not_a_cat")
    assert not ok and "未知分类" in msg
    ok, msg = catalog.set_rule("", "tv")
    assert not ok


def test_user_rules_survive_reload(monkeypatch, tmp_path):
    f = _use_tmp_rules(monkeypatch, tmp_path)
    assert catalog.set_rule("古装仙侠", "variety")[0]
    catalog.reload_rules()                     # 模拟进程重启后重读
    assert catalog.rule_hit("古装仙侠")[1] == "variety"
    data = json.loads(f.read_text(encoding="utf-8"))
    assert data["v"] == catalog.RULES_VERSION
    assert any(r["k"] == "古装仙侠" for r in data["rules"])


def test_corrupt_rule_file_falls_back_to_default(monkeypatch, tmp_path):
    f = tmp_path / "bad.json"
    f.write_text("{ this is not json", encoding="utf-8")
    monkeypatch.setattr(catalog, "_RULES_FILE", f)
    catalog.reload_rules()
    # 读不了就当没有用户规则，默认规则照常工作
    assert catalog.rule_hit("足球") == (True, None)


def test_rules_snapshot_marks_override(monkeypatch, tmp_path):
    _use_tmp_rules(monkeypatch, tmp_path)
    catalog.set_rule("足球", "variety")
    catalog.set_rule("自定义分类", "movie")
    snap = catalog.rules_snapshot()
    by_key = {u["k"]: u for u in snap["user"]}
    assert by_key["足球"]["overrides_default"] is True
    assert by_key["自定义分类"]["overrides_default"] is False
    assert snap["exact"] > 0


def test_unclassified_pool_summary():
    cat = catalog.Catalog()
    cat._unclassified = {"s1": ["足球", "甲"], "s2": ["足球"]}
    out = cat.unclassified()
    assert out["total"] == 3 and out["names"] == 2
    top = out["items"][0]
    assert top["name"] == "足球" and top["sources"] == 2
