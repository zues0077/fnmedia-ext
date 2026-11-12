"""海量四库路由落盘：重启后秒开、删源/停用源不复活、清缓存连盘一起清。

背景（2026-11-12 实测）：catalog 纯内存，进程重启后必须重新拉 82 个源分类表
并逐类探测，线上实测 94 秒、独立复跑 90.3 秒，这段时间四库全空。落盘后启动
直接读上次结论，后台再按 CLASS_REFRESH 刷新覆盖。

【可整块删除】本文件只测 OPTIONAL:DISK CACHE 这一层。若决定改用更轻的浏览
方案（如 LunaTV 那种豆瓣榜单 + 点开再搜源），把 proxy/catalog.py 里所有
OPTIONAL:DISK CACHE 标记块与本文件一起删掉即可，不影响建库/选源逻辑。
"""
import json

import config as configmod
import catalog as catalogmod
from catalog import Catalog

SCOPE = catalogmod.AGG_SCOPE


def _use_tmp_disk(monkeypatch, tmp_path):
    f = tmp_path / "catalog_routes.json"
    monkeypatch.setattr(catalogmod, "_DISK_FILE", f)
    return f


def _stub_sources(monkeypatch, all_ids, enabled_ids=None):
    enabled_ids = list(all_ids) if enabled_ids is None else list(enabled_ids)
    cfg = configmod.config
    monkeypatch.setattr(cfg, "all_sources",
                        lambda *a, **k: [{"id": i, "api": "http://x/api.php/provide/vod"}
                                         for i in all_ids],
                        raising=False)
    monkeypatch.setattr(cfg, "enabled_sources",
                        lambda *a, **k: [{"id": i, "api": "http://x/api.php/provide/vod"}
                                         for i in enabled_ids],
                        raising=False)


def _seeded():
    cat = Catalog()
    cat._classes = {
        "s1": [{"type_id": 6, "type_name": "动作片", "type_pid": "1", "cat": "movie"}],
        "s2": [{"type_id": 8, "type_name": "国产剧", "type_pid": "2", "cat": "tv"}],
    }
    cat._routes = {
        cat._scope_key(SCOPE, "movie"): [
            {"source_id": "s1", "type_id": 6, "type_name": "动作片", "pagecount": 250}],
        cat._scope_key(SCOPE, "tv"): [
            {"source_id": "s2", "type_id": 8, "type_name": "国产剧", "pagecount": 88}],
        cat._scope_key("s2", "tv"): [
            {"source_id": "s2", "type_id": 8, "type_name": "国产剧", "pagecount": 88}],
    }
    cat._updated = 1794480771.0
    return cat


def test_save_then_load_restores_everything(monkeypatch, tmp_path):
    _use_tmp_disk(monkeypatch, tmp_path)
    _stub_sources(monkeypatch, ["s1", "s2"])
    seed = _seeded()
    assert seed.save_disk() is True

    fresh = Catalog()
    assert fresh.load_disk() is True
    assert fresh._classes == seed._classes
    assert fresh._routes == seed._routes
    # 恢复的时间戳要保留，routes_stale 才能按真实年龄判断
    assert fresh._updated == seed._updated
    # 恢复后 status() 立刻非空，前端不用等一轮建库
    st = fresh.status()
    assert st["categories"]["movie"]["routes"] == 1
    assert st["categories"]["movie"]["source"] == "s1"
    assert st["categories"]["tv"]["pagecount"] == 88


def test_load_missing_file_returns_false(monkeypatch, tmp_path):
    _use_tmp_disk(monkeypatch, tmp_path)
    _stub_sources(monkeypatch, ["s1"])
    assert Catalog().load_disk() is False


def test_load_rejects_bad_version(monkeypatch, tmp_path):
    f = _use_tmp_disk(monkeypatch, tmp_path)
    _stub_sources(monkeypatch, ["s1"])
    f.write_text(json.dumps({"v": 999, "classes": {}, "routes": {}}), encoding="utf-8")
    assert Catalog().load_disk() is False


def test_load_drops_source_removed_from_config(monkeypatch, tmp_path):
    """配置里删掉的源不能靠旧表复活（否则会指向不存在的源）。"""
    _use_tmp_disk(monkeypatch, tmp_path)
    _stub_sources(monkeypatch, ["s1"], ["s1"])
    _seeded().save_disk()

    fresh = Catalog()
    assert fresh.load_disk() is True
    assert "s2" not in fresh._classes
    assert fresh._scope_key(SCOPE, "tv") not in fresh._routes
    assert fresh._scope_key("s2", "tv") not in fresh._routes


def test_load_drops_disabled_source(monkeypatch, tmp_path):
    """停用的源同样不能借旧表回到库里。"""
    _use_tmp_disk(monkeypatch, tmp_path)
    # s2 还在配置里，但被停用
    _stub_sources(monkeypatch, ["s1", "s2"], ["s1"])
    _seeded().save_disk()

    fresh = Catalog()
    assert fresh.load_disk() is True
    assert fresh._scope_key(SCOPE, "tv") not in fresh._routes
    assert fresh._scope_key("s2", "tv") not in fresh._routes
    assert fresh._scope_key(SCOPE, "movie") in fresh._routes


def test_save_is_atomic_and_leaves_no_tmp(monkeypatch, tmp_path):
    f = _use_tmp_disk(monkeypatch, tmp_path)
    _stub_sources(monkeypatch, ["s1", "s2"])
    assert _seeded().save_disk() is True
    assert f.exists()
    assert not (tmp_path / "catalog_routes.json.tmp").exists()
    data = json.loads(f.read_text(encoding="utf-8"))
    assert data["v"] == catalogmod.DISK_VERSION
    assert data["classes"] and data["routes"]


def test_save_does_not_raise_when_dir_unwritable(monkeypatch, tmp_path):
    """写盘失败必须只记日志，绝不能把 refresh_routes 带崩。"""
    f = tmp_path / "nope" / "catalog_routes.json"
    monkeypatch.setattr(catalogmod, "_DISK_FILE", f)
    # 用一个文件占住父路径，mkdir 必然失败
    blocker = tmp_path / "nope"
    blocker.write_text("x", encoding="utf-8")
    assert _seeded().save_disk() is False


def test_reset_removes_disk_file(monkeypatch, tmp_path):
    """管理页「清缓存」要连盘一起清，否则下次重启又把旧表读回来。"""
    f = _use_tmp_disk(monkeypatch, tmp_path)
    _stub_sources(monkeypatch, ["s1", "s2"])
    cat = _seeded()
    cat.save_disk()
    assert f.exists()

    cat.reset()
    assert not f.exists()
    assert cat._routes == {} and cat._classes == {}
