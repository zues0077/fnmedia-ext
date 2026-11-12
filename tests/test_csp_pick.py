"""csp 勾选清单：preview 只读、按 csp_keys 精确导入、订阅记录记住勾选。

对应用户诉求「csp 爬虫要能挑，而不是全导或全不导」。之前一个订阅能带 78~95
个 csp 源，全导要下满 jar 并把真机运行时打死；现在先预览清单、用户勾选后把
key 存进订阅记录，之后每次自动更新都按这份勾选走。

全程不联网：_load_sites / classify_subscription / spiders 全被替换成假实现。
"""
import asyncio
import sys

import config as config_mod
import subsched

CSP_ITEMS = [
    {"key": "k1", "name": "影视一", "jar": "http://j/a.jar"},
    {"key": "k2", "name": "影视二", "jar": "http://j/a.jar"},
]


class FakeCfg:
    """只记录副作用的 config 替身。"""

    def __init__(self, sub):
        self._sub = dict(sub)
        self.added: list[tuple] = []
        self.marked: list[tuple] = []
        self.loads = 0

    def subscriptions(self):
        return [dict(self._sub)]

    def all_sources(self):
        return []

    def add_source(self, name, api, enabled, standalone, sub_id=None,
                   verified=True):
        self.added.append((name, api))
        return True, "id-" + name

    def import_live_sources(self, items, sub_id=None):
        return {"added": 0}

    def import_t4_sources(self, items):
        return {"added": 0}

    def mark_subscription_update(self, sid, ok, err=""):
        self.marked.append((sid, ok, err))

    def load(self):
        self.loads += 1


class FakeSpider:
    """spiders 模块替身：记录真正被导入的 key。"""

    def __init__(self):
        self.calls: list[list[str]] = []

    def candidates(self, items):
        return [{"key": it["key"], "name": it["name"], "risk": False,
                 "has_jar": True, "jar_url": "", "exists": False,
                 "enabled": False} for it in items]

    def import_sites(self, items, mode="smart"):
        self.calls.append([it["key"] for it in items])
        return {"added": len(items), "updated": 0,
                "items": [{"key": i["key"], "name": i["name"], "enabled": True,
                           "risk": False, "has_jar": True} for i in items]}


def _setup(monkeypatch, csp_keys=None, csp_items=None):
    sub = {"id": "sub_1", "name": "测试订阅", "url": "http://x/sub",
           "enabled": True, "csp_keys": csp_keys}
    fake = FakeCfg(sub)
    spider = FakeSpider()
    monkeypatch.setattr(subsched, "_config",
                        type("M", (), {"config": fake}))

    async def _load(_sub):
        return (["raw"], "", "", "http://x/sub")

    monkeypatch.setattr(subsched, "_load_sites", _load)
    # 解析缓存不参与单测（避免跨用例串味）
    monkeypatch.setattr(subsched, "_cache_take", lambda _s: None)
    monkeypatch.setattr(subsched, "_cache_put", lambda _s, _v: None)
    monkeypatch.setattr(subsched, "_spider_module", lambda: spider, raising=False)

    items = list(CSP_ITEMS if csp_items is None else csp_items)

    class FakeTvbox:
        @staticmethod
        def classify_subscription(raw, spider_, base):
            return {"cms": [], "live": [], "drpy": [], "t4": [],
                    "csp": items, "guard": [], "skipped": []}

    monkeypatch.setitem(sys.modules, "tvbox", FakeTvbox)
    return fake, spider


def test_preview_lists_candidates_without_writing(monkeypatch):
    fake, spider = _setup(monkeypatch)
    r = asyncio.run(subsched.refresh_subscription("sub_1", preview=True))
    assert r["code"] == 0 and r["preview"] is True
    assert [x["key"] for x in r["csp"]] == ["k1", "k2"]
    assert r["counts"]["csp"] == 2
    # 预览必须零副作用：不导源、不导 csp、不更新 last_update
    assert fake.added == []
    assert fake.marked == []
    assert spider.calls == []


def test_no_selection_imports_no_csp(monkeypatch):
    fake, spider = _setup(monkeypatch, csp_keys=None)
    r = asyncio.run(subsched.refresh_subscription("sub_1"))
    assert spider.calls == []
    assert r["csp"]["chosen"] == 0
    assert r["csp_pending"] == 2
    assert fake.marked and fake.marked[0][1] is True


def test_explicit_keys_import_only_those(monkeypatch):
    _fake, spider = _setup(monkeypatch, csp_keys=None)
    r = asyncio.run(subsched.refresh_subscription("sub_1", csp_keys=["k2"]))
    assert spider.calls == [["k2"]]
    assert r["csp"]["added"] == 1


def test_stored_keys_are_reused(monkeypatch):
    _fake, spider = _setup(monkeypatch, csp_keys=["k1"])
    asyncio.run(subsched.refresh_subscription("sub_1"))
    assert spider.calls == [["k1"]]


def test_explicit_empty_list_overrides_stored(monkeypatch):
    _fake, spider = _setup(monkeypatch, csp_keys=["k1"])
    asyncio.run(subsched.refresh_subscription("sub_1", csp_keys=[]))
    assert spider.calls == []


def test_public_build_preview_reports_skipped(monkeypatch):
    _fake, _spider = _setup(monkeypatch)
    monkeypatch.delattr(subsched, "_spider_module", raising=False)
    r = asyncio.run(subsched.refresh_subscription("sub_1", preview=True))
    assert r["csp_skipped_build"] is True
    assert r["csp"] == []


# ---------------- config 侧：勾选结果要能存住 ----------------

def _patch_state(monkeypatch, tmp_path):
    state = tmp_path / "state" / "config.json"
    monkeypatch.setattr(config_mod, "_STATE", state)
    return state


def test_csp_keys_sentinel_semantics(monkeypatch, tmp_path):
    _patch_state(monkeypatch, tmp_path)
    c = config_mod.Config()
    ok, sid = c.add_subscription("订阅A", "http://a/sub")
    assert ok
    # 新建订阅默认「还没选过」，定时拉取不会导入任何 csp
    assert c.subscriptions()[0]["csp_keys"] is None

    # 显式空列表 = 明确一个都不要，要能存住
    c.update_subscription(sid, csp_keys=[])
    assert c.subscriptions()[0]["csp_keys"] == []
    # 不传则保持原值（不能因为改了名称就清掉勾选）
    c.update_subscription(sid, name="订阅A2")
    assert c.subscriptions()[0]["csp_keys"] == []
    # 传了就覆盖
    c.update_subscription(sid, csp_keys=["k1", "k2"])
    assert c.subscriptions()[0]["csp_keys"] == ["k1", "k2"]
    # 空字符串 key 被过滤掉，避免写入脏数据
    c.update_subscription(sid, csp_keys=["k1", "", " "])
    assert c.subscriptions()[0]["csp_keys"] == ["k1"]
