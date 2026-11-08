"""Offline rights/acquisition contracts. HTTP and decoding mocked explicitly."""
import importlib.util
import json
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

ROOT = Path(__file__).resolve()
MODULE = next(p for parent in ROOT.parents for p in
              [parent / "tools/openverse_assets.py", parent / "pipeline/openverse_assets.py",
               parent / "kindled_iron/openverse_assets.py"] if p.exists())
spec = importlib.util.spec_from_file_location("ov_assets_contract", MODULE)
ov = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ov)


def hit(**kw):
    return {"id": "a", "title": "Old drawing", "creator": "Artist", "license": "cc0",
            "license_version": "1.0", "license_url": "https://creativecommons.org/publicdomain/zero/1.0/",
            "url": "https://cdn.example/drawing", "foreign_landing_url": "https://source.example/a", **kw}


def reply(body=None, status=200, content=b"media"):
    r = Mock(status_code=status, headers={}, content=content,
             text='https://creativecommons.org/publicdomain/zero/1.0/')
    r.json.return_value = body or {"results": [hit()]}
    r.iter_content.return_value = [content]
    return r


@pytest.fixture(autouse=True)
def cache(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENVERSE_CACHE_DIR", str(tmp_path))


@pytest.mark.parametrize("change", [{"license": "by-nc"}, {"license_url": "https://evil.example/cc0"},
                                    {"foreign_landing_url": ""}, {"mature": True}, {"sensitive": True}])
def test_reject_bad_rights(change):
    with pytest.raises(ValueError):
        ov.rights(hit(**change))


def test_search_deduplicates_and_recovers_corrupt_cache(tmp_path):
    with patch.object(ov.requests, "get", return_value=reply({"results": [hit(), hit()]})) as get:
        assert len(ov.search("images", "old drawing")) == 1
        assert len(ov.search("images", "old drawing")) == 1
        assert get.call_count == 1
        next(tmp_path.glob("*.json")).write_text("broken")
        assert len(ov.search("images", "old drawing")) == 1
        assert get.call_count == 2


def test_429_prevents_repeated_api_calls():
    with patch.object(ov.requests, "get", return_value=reply(status=429)) as get:
        with pytest.raises(RuntimeError, match="rate limit"):
            ov.search("audio", "wind")
        with pytest.raises(RuntimeError, match="cooldown"):
            ov.search("images", "mountains")
        assert get.call_count == 1


def test_source_check_and_hash_dedup(tmp_path):
    with patch.object(ov.requests, "get", side_effect=[reply(), reply()]) as get, \
            patch.object(ov, "_decode") as decode:
        path, record = ov.acquire(hit(), "images")
        assert record["source_verified"] and record["sha256"] and record["attribution"]
        assert path.read_bytes() == b"media"
        assert ov.acquire(hit(), "images")[0] == path
        assert get.call_count == 2 and decode.call_count == 2
        assert json.loads(next((tmp_path / "assets").glob("*.license.json")).read_text())["license"] == "cc0"


def test_unverified_original_source_not_downloaded():
    page = reply()
    page.text = "No license statement"
    with patch.object(ov.requests, "get", return_value=page) as get:
        with pytest.raises(ValueError, match="Original source"):
            ov.acquire(hit(), "audio")
        assert get.call_count == 1


def test_bad_decode_leaves_no_usable_asset(tmp_path):
    with patch.object(ov.requests, "get", side_effect=[reply(), reply()]), \
            patch.object(ov, "_decode", side_effect=ValueError("bad stream")):
        with pytest.raises(ValueError):
            ov.acquire(hit(), "images")
    assert not list((tmp_path / "assets").iterdir())


def test_candidate_failure_tries_next():
    with patch.object(ov, "search", return_value=[hit(), hit(id="b")]), \
            patch.object(ov, "acquire", side_effect=[ValueError("bad"), (Path("ok"), {})]):
        assert ov.first("audio", "wind")[0] == Path("ok")


def test_not_a_video_provider():
    with pytest.raises(ValueError, match="not video"):
        ov.search("video", "ocean")


@pytest.mark.parametrize("bad", [[], {"results": "bad"}, {"results": [None, 7]}])
def test_valid_json_corrupt_cache_recovers(tmp_path, bad):
    with patch.object(ov.requests, "get", return_value=reply()):
        ov.search("images", "cache shape")
    next(tmp_path.glob("*.json")).write_text(json.dumps(bad))
    with patch.object(ov.requests, "get", return_value=reply()) as get:
        assert len(ov.search("images", "cache shape")) == 1
        assert get.call_count == 1


def test_corrupt_asset_sidecar_reacquires(tmp_path):
    with patch.object(ov.requests, "get", side_effect=[reply(), reply()]), patch.object(ov, "_decode"):
        path, _ = ov.acquire(hit(), "images")
    next(path.parent.glob("*.license.json")).write_text("[]")
    with patch.object(ov.requests, "get", side_effect=[reply(), reply()]) as get, patch.object(ov, "_decode"):
        assert ov.acquire(hit(), "images")[1]["source_verified"]
        assert get.call_count == 2


def test_license_version_must_match_url():
    with pytest.raises(ValueError):
        ov.rights(hit(license="by", license_version="4.0", license_url="https://creativecommons.org/licenses/by/3.0/"))
