"""搜索合并键 `_merge_key`：同一部剧在不同源的写法差异要能并成一条。

背景（2026-11-14 实测）：`_norm_title` 只去标点和年份，源站塞进片名的
「第46集 / 全40集 / 完结 / 粤语 / 4K」全留在键里，导致同一部剧被拆成
多条搜索结果，各占一张卡，也就没有多线路可换。

用户拍板的取舍：
- 季号参与合并（第一季 ≠ 第二季）；
- 语言版（粤语/国语）并入同一部，作为可播线路；
- 无季标注保持 None，不猜成第一季；
- 「之XX」衍生名与蹭名短剧不合并。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import app as appmod


def test_episode_and_finish_marks_are_stripped():
    base = appmod._merge_key("庆余年 第一季")
    for raw in ("庆余年 第一季 ·第46集", "庆余年 第一季 全46集",
                "庆余年 第一季 更新至46集", "庆余年 第一季·已完结",
                "庆余年 第一季 第1-46集"):
        assert appmod._merge_key(raw) == base, raw


def test_season_notation_is_normalized():
    s2 = appmod._merge_key("庆余年 第二季")
    assert appmod._merge_key("庆余年 第2季") == s2
    assert appmod._merge_key("庆余年 S02") == s2
    assert appmod._merge_key("庆余年2") == s2
    # 季号写在括号里也不能被当成备注删掉（否则会退化成“无季”而与第一季混并）
    assert appmod._merge_key("庆余年（第二季）") == s2
    assert appmod._merge_key("庆余年【第二季】") == s2


def test_different_seasons_never_merge():
    assert appmod._merge_key("庆余年 第一季") != appmod._merge_key("庆余年 第二季")


def test_language_variant_merges_into_same_show():
    """粤语/国语是同一部剧的另一条线路，并进来（用户 2026-11-14 定）。"""
    assert appmod._merge_key("庆余年第二季粤语") == appmod._merge_key("庆余年 第二季")
    assert appmod._merge_key("庆余年 第一季 国语") == appmod._merge_key("庆余年 第一季")


def test_no_season_mark_is_not_guessed_as_first():
    """没有季标注时不猜成第一季，否则两季会被混成一条。"""
    assert appmod._merge_key("庆余年") == ("庆余年", None)
    assert appmod._merge_key("庆余年") != appmod._merge_key("庆余年 第一季")


def test_spinoff_and_clickbait_titles_stay_separate():
    base = appmod._merge_key("庆余年")
    for other in ("庆余年之少年风流", "庆余年之风起沧州",
                  "庆余年，全世界都以为我是废物太子"):
        assert appmod._merge_key(other)[0] != base[0], other


def test_quality_and_bracket_notes_are_stripped():
    base = appmod._merge_key("流浪地球2")
    for raw in ("流浪地球2 4K", "流浪地球2 HDR", "流浪地球2 蓝光",
                "【直连】流浪地球2", "流浪地球2[高清]", "流浪地球2 HD"):
        assert appmod._merge_key(raw) == base, raw


def test_special_edition_wording_is_normalized():
    """特别篇/特别版/特辑 是同一部特辑的不同写法，应能合并。"""
    a = appmod._merge_key("庆余年第一季特别篇 ·已完结")
    assert appmod._merge_key("庆余年第一季 特别版 ·第25集") == a
    assert appmod._merge_key("庆余年第一季 特辑") == a


def test_bracket_alias_is_dropped_but_season_kept():
    """括号里是别名（同一个片的另一种叫法）要丢掉才能合并；是季号才保留。"""
    a = appmod._merge_key("庆余年，全世界都以为我是废物太子 ·已完结")
    b = appmod._merge_key(
        "庆余年，全世界都以为我是废物太子（庆余年之大凉太子爷） ·第61-81集完结")
    assert a == b
    assert appmod._merge_key("庆余年（第二季）")[1] == 2


def test_norm_title_kept_for_chart_matching():
    """榜单匹配仍用 _norm_title，行为不能被动过（回归保护）。"""
    assert appmod._norm_title("庆余年 第一季 ·第46集") == "庆余年第一季第46集"
    assert appmod._norm_title("流浪地球2 2023") == "流浪地球2"


# ---------- 分组层：年份噪声不能把同一部剧拆开 ----------

import types

import providers


def _mk(name: str, year: str, provider: str, line: str = "m3u8"):
    ep = types.SimpleNamespace(title="正片", url="https://cdn.test/v.m3u8",
                               direct=True, resolve=False)
    return providers.OnlineTitle(
        provider=provider, vod_id=provider, title=name, type_name="电影",
        year=year, area="", updated="", remarks="", score="", poster="",
        overview="", actor="", director="", sources={line: [ep]})


def _groups(titles, keyword="庆余年"):
    return appmod._merge_online_titles(list(titles), [], keyword, 20)


def test_singleton_years_are_treated_as_noise():
    """每个年份都只有孤例时并成一条（实测特别版 2019/2024 各一源）。"""
    out = _groups([
        _mk("庆余年第一季 特别版 ·第25集", "2019", "s1"),
        _mk("庆余年第一季特别版", "2024", "s2"),
    ])
    assert len(out) == 1, out
    assert len(out[0]["lines"]) == 2


def test_remake_gap_over_ten_years_stays_split():
    """年份差 >10 年（射雕 1983/2017）是真不同版本，必须拆开。"""
    out = _groups([
        _mk("射雕英雄传", "1983", "a1"), _mk("射雕英雄传", "1983", "a2"),
        _mk("射雕英雄传", "2017", "b1"), _mk("射雕英雄传", "2017", "b2"),
    ])
    assert len(out) == 2, [g["title"] for g in out]


def test_small_year_gap_is_noise_and_merges():
    """年份差 ≤10 年（2019/2024）属源站写混，合并成一条。"""
    out = _groups([
        _mk("庆余年第一季 特别版", "2019", "s1"),
        _mk("庆余年第一季特别版", "2024", "s2"),
    ])
    assert len(out) == 1, [g["title"] for g in out]
    assert len(out[0]["lines"]) == 2


def test_prefix_alias_merges_into_one():
    """「甄嬛传」被「后宫·甄嬛传」包含（差 ≤4 字）→ 合成一条。"""
    out = _groups([
        _mk("甄嬛传", "2011", "s1"),
        _mk("后宫·甄嬛传 ·第76集", "2011", "s2"),
    ], keyword="甄嬛传")
    assert len(out) == 1, [g["title"] for g in out]
    assert len(out[0]["lines"]) == 2


def test_spinoff_with_zhi_is_never_absorbed():
    """带「之」的衍生剧不能被前缀别名吞掉。"""
    out = _groups([
        _mk("庆余年 第一季", "2019", "s1"),
        _mk("庆余年之少年风流", "2024", "s2"),
    ])
    assert len(out) == 2, [g["title"] for g in out]


def test_seasons_and_specials_stay_separate():
    out = _groups([
        _mk("庆余年 第一季 ·第46集", "2019", "s1"),
        _mk("庆余年 第二季 ·第36集完结", "2024", "s2"),
        _mk("庆余年第一季特别篇", "2019", "s3"),
    ])
    assert len(out) == 3, [g["title"] for g in out]
