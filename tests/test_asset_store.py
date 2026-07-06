"""Tests for attachment storage helpers (bot.asset_store.store)."""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest

from bot.asset_store import store
from bot.asset_store.store import StorageFullError


def _usage(free_bytes: int):
    return SimpleNamespace(total=0, used=0, free=free_bytes)


def test_has_free_space_true():
    with patch("bot.asset_store.store.shutil.disk_usage", return_value=_usage(600 * 1024 * 1024)):
        assert store.has_free_space("/data", 500) is True


def test_has_free_space_false():
    with patch("bot.asset_store.store.shutil.disk_usage", return_value=_usage(100 * 1024 * 1024)):
        assert store.has_free_space("/data", 500) is False


def test_has_free_space_exact_boundary_counts_as_free():
    with patch("bot.asset_store.store.shutil.disk_usage", return_value=_usage(500 * 1024 * 1024)):
        assert store.has_free_space("/data", 500) is True


def test_has_free_space_oserror_returns_false():
    with patch("bot.asset_store.store.shutil.disk_usage", side_effect=OSError("boom")):
        assert store.has_free_space("/nope", 1) is False


def test_submission_dir_creates_nested_dir(tmp_path):
    path = store.submission_dir(str(tmp_path), 3, 42)
    assert path == os.path.join(str(tmp_path), "3", "42")
    assert os.path.isdir(path)


def test_submission_dir_is_idempotent(tmp_path):
    first = store.submission_dir(str(tmp_path), 3, 42)
    second = store.submission_dir(str(tmp_path), 3, 42)
    assert first == second
    assert os.path.isdir(second)


def test_remove_submission_dir_removes(tmp_path):
    path = store.submission_dir(str(tmp_path), 1, 2)
    (tmp_path / "1" / "2" / "file.png").write_bytes(b"x")
    store.remove_submission_dir(str(tmp_path), 1, 2)
    assert not os.path.exists(path)


def test_remove_submission_dir_tolerates_missing(tmp_path):
    store.remove_submission_dir(str(tmp_path), 9, 9)  # must not raise


def test_safe_filename_strips_traversal_and_slashes():
    assert store._safe_filename("../../evil.png") == "evil.png"
    assert store._safe_filename("dir/sub/file.png") == "file.png"


def test_safe_filename_removes_odd_characters():
    assert store._safe_filename("we?ird*na:me.png") == "weirdname.png"


def test_safe_filename_empty_and_all_stripped_fall_back():
    assert store._safe_filename("") == "attachment"
    assert store._safe_filename("???") == "attachment"
    assert store._safe_filename("   ") == "attachment"


def _ok_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, content=b"payload-bytes")


async def test_download_attachment_writes_file_atomically(tmp_path):
    client = httpx.AsyncClient(transport=httpx.MockTransport(_ok_handler))
    with patch("bot.asset_store.store.has_free_space", return_value=True):
        try:
            path = await store.download_attachment(
                url="https://cdn.example/x.png",
                dest_dir=str(tmp_path),
                filename="x.png",
                data_dir=str(tmp_path),
                min_free_mb=1,
                client=client,
            )
        finally:
            await client.aclose()
    assert path == os.path.join(str(tmp_path), "x.png")
    with open(path, "rb") as fh:
        assert fh.read() == b"payload-bytes"
    assert not os.path.exists(path + ".part")


async def test_download_attachment_sanitises_filename(tmp_path):
    client = httpx.AsyncClient(transport=httpx.MockTransport(_ok_handler))
    with patch("bot.asset_store.store.has_free_space", return_value=True):
        try:
            path = await store.download_attachment(
                url="https://cdn.example/x.png",
                dest_dir=str(tmp_path),
                filename="../../esc?ape.png",
                data_dir=str(tmp_path),
                min_free_mb=1,
                client=client,
            )
        finally:
            await client.aclose()
    assert path == os.path.join(str(tmp_path), "escape.png")
    assert os.path.exists(path)


async def test_download_attachment_404_propagates_and_leaves_no_file(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with patch("bot.asset_store.store.has_free_space", return_value=True):
        try:
            with pytest.raises(httpx.HTTPStatusError):
                await store.download_attachment(
                    url="https://cdn.example/gone.png",
                    dest_dir=str(tmp_path),
                    filename="gone.png",
                    data_dir=str(tmp_path),
                    min_free_mb=1,
                    client=client,
                )
        finally:
            await client.aclose()
    assert os.listdir(tmp_path) == []


async def test_download_attachment_raises_when_storage_full(tmp_path):
    with patch("bot.asset_store.store.has_free_space", return_value=False):
        with pytest.raises(StorageFullError):
            await store.download_attachment(
                url="https://cdn.example/x.png",
                dest_dir=str(tmp_path),
                filename="x.png",
                data_dir=str(tmp_path),
                min_free_mb=500,
            )


async def test_download_attachment_owns_and_closes_its_client(tmp_path):
    real_client = httpx.AsyncClient
    created: list[httpx.AsyncClient] = []

    def factory(**kwargs):
        client = real_client(transport=httpx.MockTransport(_ok_handler))
        created.append(client)
        return client

    with (
        patch("bot.asset_store.store.has_free_space", return_value=True),
        patch("bot.asset_store.store.httpx.AsyncClient", side_effect=factory),
    ):
        path = await store.download_attachment(
            url="https://cdn.example/x.png",
            dest_dir=str(tmp_path),
            filename="x.png",
            data_dir=str(tmp_path),
            min_free_mb=1,
        )
    assert len(created) == 1
    assert created[0].is_closed
    with open(path, "rb") as fh:
        assert fh.read() == b"payload-bytes"
