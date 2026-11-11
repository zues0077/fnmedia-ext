"""关于页品牌：维护者与更新源必须是本 fork 的仓库（zues0077/fnmedia-ext）。

上游 yujian0815/fnmedia-ext 的品牌断言原为 funwukong，本仓库二次开发后改成自己；
关于页仍保留对上游 funwukong 的致谢（fork 合规），但更新源/维护者必须指向自己。
"""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "proxy"))

import updater

ROOT = os.path.join(os.path.dirname(__file__), "..")


def test_updater_repo_is_own():
    assert updater.REPO_SLUG == "zues0077/fnmedia-ext"
    assert updater.API_BASE.startswith(
        "https://api.github.com/repos/zues0077/fnmedia-ext/releases")


def test_manifest_maintainer_is_own():
    manifest = open(os.path.join(ROOT, "packaging", "fpk", "manifest.in"),
                    encoding="utf-8").read()
    assert "maintainer            = zues0077" in manifest
    assert "https://github.com/zues0077/fnmedia-ext" in manifest
    assert "yujian0815" not in manifest


def test_about_page_names_own_author():
    ui = open(os.path.join(ROOT, "proxy", "admin_ui.py"),
              encoding="utf-8").read()
    start = ui.find('id="page-about"')
    end = ui.find('</section>', start)
    about = ui[start:end]
    assert "zues0077" in about
    assert "https://github.com/zues0077/fnmedia-ext" in about
    assert "yujian0815" not in about
    # 保留对上游的署名（Apache-2.0 fork 合规）
    assert "funwukong" in about
