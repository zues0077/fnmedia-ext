"""内容红线关键词：内置兜底 + 后台可增删改 + 空表合法。

背景：main 上曾把 `is_redline` / `_REDLINE_KW` 整段删掉，但 7 处调用还在
（建库归类 5 处、搜索合并 2 处），建库直接 NameError、搜索抛 AttributeError。
本文件锁住三件事：
1. 出厂的「萝莉/童贞/换脸」必须命中（判定不能因为配置损坏而全线放行）；
2. 词表以配置为准，后台增删即时生效，空表＝不拦截（用户主动清空是合法状态）；
3. 配置读不到时退回_内置表兜底，而不是当成空表。

只测内存与 tmp_path，不碰真实 state/ 与网络。
"""
import config as config_mod
import catalog


class FakeCfg:
    """替身：只提供 redline_keywords()，避免读取真实配置文件。"""

    def __init__(self, kws):
        self._kws = tuple(kws)

    def redline_keywords(self):
        return self._kws


def _state(monkeypatch, tmp_path):
    state = tmp_path / "state" / "config.json"
    monkeypatch.setattr(config_mod, "_STATE", state)
    return state


# ---------- is_redline：判定层 ----------

def test_builtin_keywords_hit(monkeypatch):
    monkeypatch.setattr(config_mod, "config", FakeCfg(["萝莉", "童贞", "换脸"]))
    assert catalog.is_redline("萝莉") is True
    assert catalog.is_redline("换脸") is True
    assert catalog.is_redline("童贞") is True
    # 备注也要拦截（标题干净、备注命中同样丢弃）
    assert catalog.is_redline("普通标题", "换脸合成") is True
    # 正常内容不误伤
    assert catalog.is_redline("庆余年", "全40集") is False
    assert catalog.is_redline("") is False


def test_user_keywords_extend_filter(monkeypatch):
    monkeypatch.setattr(config_mod, "config", FakeCfg(["萝莉", "自造词"]))
    assert catalog.is_redline("自造词") is True
    # 用户词表覆盖后，不在表里的内置词不再拦截（后台可删改的语义）
    assert catalog.is_redline("换脸") is False


def test_empty_list_means_no_filter(monkeypatch):
    """空表是合法状态（用户清空），不能悄悄退回内置表。"""
    monkeypatch.setattr(config_mod, "config", FakeCfg([]))
    assert catalog.is_redline("萝莉") is False


def test_config_failure_falls_back_to_builtin(monkeypatch):
    """配置读失败时用内置表兜底：宁可多拦，也不因异常全线放行。"""

    class Broken:
        def redline_keywords(self):
            raise RuntimeError("config broken")

    monkeypatch.setattr(config_mod, "config", Broken())
    assert catalog.is_redline("萝莉") is True
    assert catalog.is_redline("庆余年") is False


# ---------- 配置层：默认值 / 保存 / 恢复 / 缓存 ----------

def test_defaults_contain_builtin_words(monkeypatch, tmp_path):
    _state(monkeypatch, tmp_path)
    cfg = config_mod.Config()
    assert cfg.get()["redline_keywords"] == list(config_mod.REDLINE_KEYWORDS)
    assert cfg.redline_keywords() == tuple(config_mod.REDLINE_KEYWORDS)


def test_set_normalizes_and_persists(monkeypatch, tmp_path):
    _state(monkeypatch, tmp_path)
    cfg = config_mod.Config()
    kws = cfg.set_redline_keywords("萝莉，换脸\n自造词  自造词,,")
    assert kws == ["萝莉", "换脸", "自造词"]
    # 落盘后再读（新实例模拟重启）仍是这份词表
    again = config_mod.Config()
    assert again.get()["redline_keywords"] == kws
    assert again.redline_keywords() == ("萝莉", "换脸", "自造词")


def test_cache_invalidated_after_change(monkeypatch, tmp_path):
    _state(monkeypatch, tmp_path)
    cfg = config_mod.Config()
    assert "萝莉" in cfg.redline_keywords()          # 预热缓存
    cfg.set_redline_keywords([])
    assert cfg.redline_keywords() == ()              # 改完立即生效，不能吃旧缓存


def test_reset_restores_builtin(monkeypatch, tmp_path):
    _state(monkeypatch, tmp_path)
    cfg = config_mod.Config()
    cfg.set_redline_keywords(["只剩一个"])
    assert cfg.redline_keywords() == ("只剩一个",)
    kws = cfg.reset_redline_keywords()
    assert kws == list(config_mod.REDLINE_KEYWORDS)
    assert cfg.redline_keywords() == tuple(config_mod.REDLINE_KEYWORDS)


def test_bad_type_falls_back_to_builtin(monkeypatch, tmp_path):
    """配置里被手改成字符串/数字时，不要把词表读成逐字拆开。"""
    _state(monkeypatch, tmp_path)
    cfg = config_mod.Config()
    cfg._cfg["redline_keywords"] = "萝莉"     # 手改坏了
    cfg._redline_cache = None
    assert cfg.redline_keywords() == tuple(config_mod.REDLINE_KEYWORDS)
