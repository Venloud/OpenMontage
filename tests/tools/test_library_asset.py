"""library_asset tool, offline with a throwaway library. Skipped without vandam_assets."""

import pytest

pytest.importorskip("vandam_assets")
from vandam_assets import Library  # noqa: E402

from tools.base_tool import ToolStatus  # noqa: E402
from tools.graphics.library_asset import LibraryAsset  # noqa: E402


@pytest.fixture
def lib(tmp_path, monkeypatch):
    monkeypatch.setenv("VANDAM_ASSET_LIBRARY", str(tmp_path / "lib"))
    return Library(tmp_path / "lib")


def src(tmp_path, name, data):
    p = tmp_path / name
    p.write_bytes(data)
    return p


def test_off_without_env(monkeypatch):
    monkeypatch.delenv("VANDAM_ASSET_LIBRARY", raising=False)
    assert LibraryAsset().get_status() == ToolStatus.UNAVAILABLE
    assert not LibraryAsset().execute({"action": "find", "query": "x"}).success


def test_save_then_find(tmp_path, lib, monkeypatch):
    monkeypatch.setenv("VANDAM_ASSET_COOLDOWN", "0")
    tool = LibraryAsset()
    assert tool.get_status() == ToolStatus.AVAILABLE
    saved = tool.execute({"action": "save", "file": str(src(tmp_path, "a.jpg", b"img")), "video_id": "v1",
                          "record": {"provider": "openverse", "source_url": "https://ov/1", "license": "CC BY 4.0",
                                     "creator": "Ann", "attribution": "Photo by Ann, CC BY 4.0",
                                     "tags": ["lighthouse", "storm"]}})
    assert saved.success and saved.data["rights_status"] == "verified"
    out = tmp_path / "out" / "shot.jpg"
    got = tool.execute({"action": "find", "query": "lighthouse in a storm", "output_path": str(out), "video_id": "v9"})
    assert got.success and out.read_bytes() == b"img"
    assert got.data["attribution"] == "Photo by Ann, CC BY 4.0" and got.data["attribution_required"]


def test_unknown_licence_never_found(tmp_path, lib):
    lib.add(src(tmp_path, "b.jpg", b"x"), {"kind": "image", "provider": "web", "source_url": "https://w/1",
                                          "license": "CC BY-SA 4.0", "tags": ["lighthouse"]}, "openmontage", "v1")
    assert not LibraryAsset().execute({"action": "find", "query": "lighthouse"}).success
