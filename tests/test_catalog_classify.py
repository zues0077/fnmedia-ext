"""分类归并：纪录片 / 4K / 短剧 / 动漫 / 综艺 / 电影 / 剧集。"""
import catalog


def test_documentary_and_typo():
    assert catalog.classify("纪录片") == "documentary"
    # 错别字“记录片”同样归入纪录片（README 本次更新）。
    assert catalog.classify("记录片") == "documentary"
    assert catalog.classify("纪录") == "documentary"


def test_uhd_keywords():
    assert catalog.classify("4K电影") == "uhd"
    assert catalog.classify("杜比视界") == "uhd"
    assert catalog.classify("蓝光") == "uhd"


def test_shortdrama_anime_variety():
    assert catalog.classify("热门短剧") == "shortdrama"
    assert catalog.classify("微短剧") == "shortdrama"
    assert catalog.classify("动漫") == "anime"
    assert catalog.classify("综艺") == "variety"


def test_movie_tv_and_excluded():
    assert catalog.classify("电影") == "movie"
    assert catalog.classify("动作片") == "movie"
    assert catalog.classify("国产剧") == "tv"
    # 脏类 / 噪音不进任何库。
    assert catalog.classify("影视解说") is None
    assert catalog.classify("预告") is None
    assert catalog.classify("") is None
    assert catalog.classify("未知分类xyz") is None


def test_redline_never_classified():
    # 硬红线：任何开关都不放行。
    assert catalog.is_redline("萝莉") is True
    assert catalog.is_redline("换脸") is True
    assert catalog.classify_source_class("萝莉", adult_allowed=True) is None
    assert catalog.classify_source_class("换脸视频", adult_allowed=True) is None


def test_adult_cat_needs_ext_switch():
    # 扩展库默认关闭：未归入常规四类的成人向分类也不收编。
    assert catalog._welfare_on() is False
    assert catalog.classify_source_class("伦理", adult_allowed=True) is None


def _welfare_on(monkeypatch):
    monkeypatch.setattr(catalog, "_welfare_on", lambda: True)


def test_overseas_place_names_are_tv(monkeypatch):
    """源站把港台剧按地名拆开写：「香港剧/韩国剧」必须归海外剧集，不能当垃圾丢。

    2026-11-12 实测：暴风资源的 49 个分类里，这 5 个之前 classify 全部返回 None，
    结果整类被丢弃（连不开福利库时也丢）。
    """
    _welfare_on(monkeypatch)
    for name in ("香港剧", "台湾剧", "韩国剧", "日本剧", "泰国剧"):
        assert catalog.classify(name) == "overseas", name


def test_adult_mark_never_swallows_sports(monkeypatch):
    """打了 18 也不能把体育倒进福利库。

    打 18 后的兜底是「classify 认不出 → 进福利库」，太宽：暴风资源实测 18 个
    「打 18 才进」的分类里，17 个是足球/篮球/网球/斯诺克这类误伤。
    """
    _welfare_on(monkeypatch)
    for name in ("足球", "篮球", "网球", "斯诺克", "少儿", "儿童", "相声"):
        assert catalog.classify_source_class(name, adult_allowed=True) is None, name
    # 常规类型走各自分类，不会被 18 拉进福利库。
    assert catalog.classify_source_class("综艺", True) == "variety"
    assert catalog.classify_source_class("纪录片", True) == "documentary"


def test_adult_mark_still_catches_vague_names(monkeypatch):
    """成人站那些完全不沾关键词的分类名，仍靠源级 18 兜底收编。

    规则库点名了一批高频名字（视频一区/两性课堂…），这些不打 18 也进福利库；
    但源站还会冒出没被点名的杂项（策划/专题/某某频道），只能靠 18 兜底——
    纯词表路线在这类源上会丢 62%（实测 95→36），18 标记的意义就在这。
    """
    _welfare_on(monkeypatch)
    # 规则点名的：不打 18 也归福利库（一份规则管所有源）
    assert catalog.classify_source_class("视频一区", adult_allowed=False) == catalog.ADULT_CAT
    # 规则没点名的杂项：不打 18 丢弃，打 18 才兜进福利库
    assert catalog.classify_source_class("策划专题", adult_allowed=False) is None
    assert catalog.classify_source_class("策划专题", adult_allowed=True) == catalog.ADULT_CAT
    assert catalog.classify_source_class("国产视频", adult_allowed=True) == catalog.ADULT_CAT


def test_adult_words_cover_lunatv_list(monkeypatch):
    """词表参照 LunaTV `src/lib/yellow.ts`：这些不打 18 也该认出来。"""
    _welfare_on(monkeypatch)
    for name in ("理论片", "港台三级", "无码", "黑料吃瓜", "国产传媒", "伦理片"):
        assert catalog.classify_source_class(name, adult_allowed=False) == catalog.ADULT_CAT, name
    # 「理论片」先被 _EXCLUDE_KW 从常规库踢掉，再进福利库，不能落进电影库。
    assert catalog.classify("理论片") is None


def test_child_class_inherits_parent(monkeypatch):
    """短剧子分类（重生民国/都市脑洞）光看名字判不出类型，必须继承父类。

    实测暴风资源：这 8 个分类不打 18 时被整个丢弃（短剧库永远空），
    打了 18 又被 adult 兜底吞进福利库。继承父类「短剧大全」后两条都解决。
    """
    import asyncio

    cls = [
        {"type_id": 1, "type_pid": 0, "type_name": "短剧大全"},
        {"type_id": 2, "type_pid": 1, "type_name": "重生民国"},
        {"type_id": 3, "type_pid": 1, "type_name": "都市脑洞"},
        {"type_id": 4, "type_pid": 0, "type_name": "体育赛事"},
        {"type_id": 5, "type_pid": 4, "type_name": "足球"},
        {"type_id": 6, "type_pid": 0, "type_name": "电影片"},
        {"type_id": 7, "type_pid": 6, "type_name": "理论片"},
    ]

    async def fake_fetch(*_a, **_k):
        return cls

    monkeypatch.setattr(catalog, "_fetch_class_list", fake_fetch)
    monkeypatch.setattr(catalog, "_welfare_on", lambda: True)

    cat = catalog.Catalog()
    asyncio.run(cat._load_source_classes(None, {"id": "t", "adult": True}))
    got = {x["type_name"]: x["cat"] for x in cat._classes["t"]}

    assert got["重生民国"] == "shortdrama"
    assert got["都市脑洞"] == "shortdrama"
    # 黑名单不继承父类：体育赛事下的足球继续被丢弃。
    assert "足球" not in got
    # 父类自身不是叶子，不单独建路由。
    assert "短剧大全" not in got and "电影片" not in got
    # 子分类自己认得出时以自己为准：理论片进福利库，不跟父类进电影库。
    assert got["理论片"] == catalog.ADULT_CAT
