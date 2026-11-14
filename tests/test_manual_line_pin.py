"""A2 手动换源：只有用户手动选线才固定，自动路径仍竞速/自动切换。

语义（2026-11-14）：
- 自动起播：play_link = preset.m3u8?index=N（**不带 line**）→ preset 枚举全部线路，
  首个变体带 auto=1 且不带 line → main 走竞速选最快；其余变体带 line+pin=1，
  用户从播放器「线路」菜单切到哪条就固定哪条。
- 手动固定：pin=1 时 `_ensure_playable(strict=True)` 只认这一条，探测失败也不
  换源，由上层明确报错。
- 线路卡（用户点选某线路）：play_link 直接带 line+pin=1。

沿用本仓库测试风格（unittest，不依赖 pytest-asyncio），只测进程内逻辑，不联网。
"""
import sys
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import AsyncMock, patch

from starlette.requests import Request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import app as appmod


def _t(line: str, url: str = "https://cdn.test/v.m3u8", idx: int = 0) -> dict:
    return {"line": line, "index": idx, "url": url, "direct": True,
            "online_guid": "og", "title": "测试片"}


def _req(qs: bytes = b"") -> Request:
    return Request({"type": "http", "method": "GET",
                    "path": "/v/media/online/mg/preset.m3u8",
                    "query_string": qs, "headers": [], "app": appmod.app})


def _variants(master: str) -> list[dict]:
    """从 master m3u8 里抽出每个变体的 query（按出现顺序）。"""
    out = []
    for line in master.splitlines():
        if "main.m3u8?" in line:
            q = line.split("main.m3u8?", 1)[1]
            out.append(urllib.parse.parse_qs(q))
    return out


async def _preset(qs: bytes, targets: list[dict], picked: dict | None = None,
                  fastest: bool = True) -> str:
    with patch.object(appmod, "_await_pending_chart",
                      AsyncMock(return_value=None)), \
            patch.object(appmod, "_pick_episode",
                         return_value=picked or targets[0]), \
            patch.object(appmod, "_dodge_known_bad",
                         side_effect=lambda p, r: p), \
            patch.object(appmod, "_online_target_viewable", return_value=True), \
            patch.object(appmod.store, "target_lines", return_value=targets), \
            patch.object(appmod, "_fastest_on", return_value=fastest):
        resp = await appmod.hls_preset("mg", _req(qs))
    return resp.body.decode("utf-8")


class AutoPresetTest(unittest.IsolatedAsyncioTestCase):
    async def test_auto_play_lists_all_lines_and_only_first_races(self):
        """不带 line 起播：枚举全部线路，首变体竞速（auto=1 且无 line），其余 pin。"""
        master = await _preset(b"index=0", [_t("a::x"), _t("b::y")])
        vs = _variants(master)
        self.assertEqual(len(vs), 2, master)
        # 首变体：自动起播 → auto=1 且不带 line（否则 main 不会竞速）
        self.assertEqual(vs[0].get("auto"), ["1"])
        self.assertNotIn("line", vs[0])
        # 第二个变体：用户手动从菜单选它 → 带 line + pin，固定不换源
        self.assertEqual(vs[1].get("line"), ["b::y"])
        self.assertEqual(vs[1].get("pin"), ["1"])
        self.assertNotIn("auto", vs[1])

    async def test_line_card_preset_pins_the_chosen_line(self):
        """线路卡（带 line+pin）：只输出该线路一个变体，且不竞速。"""
        master = await _preset(b"line=a::x&pin=1&index=0",
                               [_t("a::x"), _t("b::y")], picked=_t("a::x"))
        vs = _variants(master)
        self.assertEqual(len(vs), 1, master)
        self.assertEqual(vs[0].get("line"), ["a::x"])
        self.assertEqual(vs[0].get("pin"), ["1"])
        self.assertNotIn("auto", vs[0])

    async def test_fastest_off_does_not_pin_menu_variants(self):
        """竞速关闭时，菜单变体不加 pin，保持原有故障切换行为。"""
        master = await _preset(b"index=0", [_t("a::x"), _t("b::y")], fastest=False)
        vs = _variants(master)
        self.assertEqual(len(vs), 2)
        self.assertTrue(all("pin" not in v for v in vs))


class StrictEnsureTest(unittest.IsolatedAsyncioTestCase):
    async def test_strict_mode_never_switches_to_another_source(self):
        """pin=1（用户手动选线）：探测失败也不找替代线。"""
        with patch.object(appmod, "_autofail_on", return_value=True), \
                patch.object(appmod, "_line_is_bad", return_value=False), \
                patch.object(appmod, "_probe_target",
                             AsyncMock(return_value=False)), \
                patch.object(appmod, "_mark_line_bad") as bad, \
                patch.object(appmod, "_find_playable",
                             AsyncMock(return_value=_t("b::y"))) as find:
            out = await appmod._ensure_playable(_t("a::x"), _req(), strict=True)
        self.assertEqual(out["line"], "a::x")   # 仍是用户选的那条，绝不偷换
        self.assertEqual(find.call_count, 0)
        self.assertEqual(bad.call_count, 1)     # 只记坏线，供后续提示

    async def test_non_strict_mode_keeps_auto_failover(self):
        """自动路径（无 pin）：探测失败仍自动换到可用线路。"""
        with patch.object(appmod, "_autofail_on", return_value=True), \
                patch.object(appmod, "_line_is_bad", return_value=False), \
                patch.object(appmod, "_probe_target",
                             AsyncMock(return_value=False)), \
                patch.object(appmod, "_mark_line_bad"), \
                patch.object(appmod, "_find_playable",
                             AsyncMock(return_value=_t("b::y"))):
            out = await appmod._ensure_playable(_t("a::x"), _req())
        self.assertEqual(out["line"], "b::y")


if __name__ == "__main__":
    unittest.main()
