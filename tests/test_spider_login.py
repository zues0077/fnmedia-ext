"""csp 登录态：识别「需登录」而不是「源死了」，并能把 ext 存住。

诉求：csp 里有相当一部分（网盘/付费/厂牌站）必须带用户自己的 Cookie 才能取内容。
这类源既不该显示成「已失效」，更不能被「删除勾选失效源」一键删掉——它只是缺
一份凭据。我们不做「后台替用户登录」，只做识别 + 让用户把 Cookie 填进 ext。
"""
# >>> PRIVATE:SPIDER
# 本文件只测私有爬虫运行时（公开构建里 spiders.py 被剥离），整段随构建剥离。
import json

import spiders


def test_login_hint_matches_real_messages():
    for m in ("请先登录", "未登录", "Login required", "401 Unauthorized",
              "403 Forbidden", "需要授权", "cookie 失效", "该片为VIP会员专享",
              "userid 不能为空", "captcha verify"):
        assert spiders.login_hint(m) is True, m


def test_login_hint_does_not_match_plain_network_errors():
    for m in ("UnknownHostException", "timed out", "connect refused",
              "协议不支持", "空列表", ""):
        assert spiders.login_hint(m) is False, m


def test_login_verdict_shape():
    assert spiders._login_verdict(RuntimeError("请先登录")) == {
        "verdict": "needs_login"}
    assert spiders._login_verdict(RuntimeError("timeout")) == {}


def test_needs_login_is_not_a_dead_verdict():
    # 关键：体检结果里 needs_login 不能进「建议删除」集合，否则用户点一次
    # 「删除勾选失效源」就会把只是缺 Cookie 的源一起删掉。
    assert "needs_login" not in spiders.DEAD_VERDICTS
    assert spiders.VERDICT_LABEL["needs_login"]


def _boom_factory(msg):
    def _boom(*_a, **_k):
        raise RuntimeError(msg)
    return _boom


def test_diagnose_empty_maps_login_error_to_needs_login(monkeypatch):
    monkeypatch.setattr(spiders, "site", lambda k: {"key": k, "name": k})
    for fn in ("home", "search", "category"):
        monkeypatch.setattr(spiders, fn, _boom_factory("请先登录后再搜索"))
    r = spiders.diagnose_empty("Czsapp", "庆余年")
    assert r["verdict"] == "needs_login", r
    assert "登录" in r["reason"]


def test_diagnose_empty_still_marks_network_failure_dead(monkeypatch):
    # 回归守卫：加了 needs_login 之后，普通网络故障仍要判 dead_runtime
    monkeypatch.setattr(spiders, "site", lambda k: {"key": k, "name": k})
    for fn in ("home", "search", "category"):
        monkeypatch.setattr(spiders, fn,
                            _boom_factory("UnknownHostException: hhh.tv"))
    r = spiders.diagnose_empty("Czsapp", "庆余年")
    assert r["verdict"] == "dead_runtime", r


def test_set_ext_persists_and_reloads(monkeypatch, tmp_path):
    sites_file = tmp_path / "spider_sites.json"
    sites_file.write_text(json.dumps({"sites": [
        {"key": "Czsapp", "name": "厂长", "ext": "", "enabled": True},
    ]}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(spiders, "_SITES_FILE", sites_file)
    monkeypatch.setattr(spiders, "_ASSET", tmp_path)
    monkeypatch.setattr(spiders, "_sites", None)

    assert spiders.set_ext("Czsapp", "uid=42; token=abc") is True
    # 落盘
    saved = json.loads(sites_file.read_text(encoding="utf-8"))["sites"]
    assert saved[0]["ext"] == "uid=42; token=abc"
    # 内存表也刷新了（_call 会立刻用到）
    assert spiders.site("Czsapp")["ext"] == "uid=42; token=abc"
    # 留空 = 清空
    spiders.set_ext("Czsapp", "")
    assert spiders.site("Czsapp")["ext"] == ""
    # 未知 key 返回 False，不新建记录
    assert spiders.set_ext("nope", "x") is False


def test_status_exposes_login_fields(monkeypatch, tmp_path):
    sites_file = tmp_path / "spider_sites.json"
    sites_file.write_text(json.dumps({"sites": [
        {"key": "Czsapp", "name": "厂长", "ext": "uid=1", "enabled": True,
         "last_verdict": "needs_login", "last_ok": False,
         "last_error": "请先登录", "last_probe": 100},
    ]}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(spiders, "_SITES_FILE", sites_file)
    monkeypatch.setattr(spiders, "_ASSET", tmp_path)
    monkeypatch.setattr(spiders, "_sites", None)
    monkeypatch.setattr(spiders, "available", lambda: False)

    row = spiders.status()["sites"][0]
    assert row["ext"] == "uid=1"
    assert row["last_verdict"] == "needs_login"
    assert row["last_error"] == "请先登录"

# <<< PRIVATE:SPIDER
