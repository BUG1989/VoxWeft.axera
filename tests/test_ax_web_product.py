from pathlib import Path
import asyncio

from voxweft.runtime import server


def test_demo_intro_prefers_requested_language():
    payload = asyncio.run(server.demo_intro("zh"))
    assert payload["lang"] == "zh"
    assert any("VoxWeft" in line for line in payload["lines"])


def test_demo_intro_falls_back_for_missing_russian_intro():
    payload = asyncio.run(server.demo_intro("ru"))
    assert payload["lang"] == "ru"
    assert payload["lines"]


def test_ax_demo_intro_mentions_local_ax8860(monkeypatch):
    monkeypatch.setattr(server, "AX_BACKEND", True)
    payload = asyncio.run(server.demo_intro("zh"))
    assert any("AX8860" in line for line in payload["lines"])
    assert all("Apple Silicon" not in line for line in payload["lines"])


def test_ax_web_contains_product_controls():
    html = (Path(__file__).parents[1] / "voxweft/runtime/ax_web.html").read_text(encoding="utf-8")
    for marker in (
        "单页操作台", "当前模型", "板端资源", "译员音色", "播放演示文本",
        "导出", "自动重连", "指标口径说明", "本轮指标与电平",
        "data-help=\"metrics\"", "nodeAsr", "micSupported",
        "浏览器禁用麦克风", "navigator.mediaDevices",
    ):
        assert marker in html
    assert "cmm_total_mb.toFixed(1)} MB" in html
    assert "cmm_total_mb.toFixed(1)} GB" not in html
