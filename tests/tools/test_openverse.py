"""Offline tests for openverse_image / openverse_audio (mocked HTTP, no network)."""

from __future__ import annotations

from unittest.mock import patch

import pytest
import requests

from tools import openverse_common as ov
from tools.audio.openverse_audio import OpenverseAudio
from tools.base_tool import ToolStatus
from tools.graphics.openverse_image import OpenverseImage


class Reply:
    def __init__(self, status=200, body=None, headers=None, content=b"x"):
        self.status_code = status
        self._body = body if body is not None else {}
        self.headers = headers or {}
        self.content = content
        self.text = "https://creativecommons.org/licenses/by/4.0/ https://creativecommons.org/publicdomain/zero/1.0/"

    def iter_content(self, size):
        return [self.content]

    def close(self):
        pass

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


def hit(i, lic, **kw):
    return {"id": f"id{i}", "title": f"item {i}", "url": f"https://cdn.example/{i}", "filetype": "jpg",
            "foreign_landing_url": f"https://src.example/{i}", "creator": f"maker{i}", "license": lic,
            "license_version": "4.0", "license_url": "https://creativecommons.org/" + ({"cc0":"publicdomain/zero/1.0/", "pdm":"publicdomain/mark/1.0/", "by":"licenses/by/4.0/"}.get(lic, "licenses/by-nc/4.0/")),
            "attribution": f'"item {i}" by maker{i}', "source": "flickr", "duration": 3000, **kw}


BODY = {"results": [hit(1, "by"), hit(2, "by-nc"), hit(3, "by-sa"), hit(4, "cc0"),
                    hit(5, "pdm", mature=True), hit(6, "by-nd")]}


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENVERSE_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv(ov.ENABLE_ENV, "1")
    monkeypatch.setattr("tools.openverse_assets._decode", lambda *a: None)
    return tmp_path


def fake_get(search_reply, media_type="image/jpeg"):
    def get(url, **kw):
        if url.startswith(ov.API):
            return search_reply
        return Reply(headers={"Content-Type": media_type}, content=b"data")
    return get


def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv(ov.ENABLE_ENV)
    assert OpenverseImage().get_status() == ToolStatus.UNAVAILABLE
    assert OpenverseAudio().get_status() == ToolStatus.UNAVAILABLE
    with patch("requests.get") as get:
        assert not OpenverseImage().execute({"query": "x"}).success
        get.assert_not_called()


def test_license_filter_rights_and_download(env):
    with patch("requests.get", side_effect=fake_get(Reply(body=BODY))) as get:
        res = OpenverseImage().execute({"query": "old lighthouse", "output_path": str(env / "a.jpg")})
        assert get.call_args_list[0].kwargs["params"]["license"] == "by,cc0,pdm"
    assert res.success, res.error
    assert [c["openverse_id"] for c in res.data["candidates"]] == ["id1", "id4"]
    d = res.data
    assert d["license"] == "CC BY 4.0" and d["attribution_required"] and d["commercial_use"]
    for k in ("creator", "source_url", "license_url", "attribution"):
        assert d[k]
    assert (env / "a.jpg").read_bytes() == b"data"


def test_dedup_cache_and_audio(env):
    with patch("requests.get", side_effect=fake_get(Reply(body=BODY), "audio/mpeg")):
        res = OpenverseAudio().execute({"query": "rain", "exclude_ids": ["id1"], "output_path": str(env / "r.mp3")})
    assert res.success and res.data["openverse_id"] == "id4" and res.data["license"] == "CC0 4.0"
    with patch("requests.get", side_effect=fake_get(Reply(500), "audio/mpeg")) as get:
        again = OpenverseAudio().execute({"query": "rain", "output_path": str(env / "r2.mp3")})
        assert again.success, "second search should come from the cache"
        assert all(not c.args[0].startswith(ov.API) for c in get.call_args_list)


def test_rate_limit_invalid_response_and_wrong_content(env):
    with patch("requests.get", return_value=Reply(429, headers={"Retry-After": "30"})):
        res = OpenverseImage().execute({"query": "limited"})
    assert not res.success and "rate_limit" in res.error


def test_invalid_response(env):
    with patch("requests.get", return_value=Reply(body={"detail": "oops"})):
        assert "no results list" in OpenverseImage().execute({"query": "broken"}).error


def test_wrong_content_type(env):
    with patch("requests.get", side_effect=fake_get(Reply(body=BODY), "text/html")):
        res = OpenverseImage().execute({"query": "html page", "output_path": str(env / "h.jpg")})
    assert not res.success and "not image" in res.error and not (env / "h.jpg").exists()


def test_no_allowed_results(env):
    with patch("requests.get", return_value=Reply(body={"results": [hit(9, "by-nc")]})):
        res = OpenverseImage().execute({"query": "nothing usable"})
    assert not res.success and "No openly licensed image" in res.error
