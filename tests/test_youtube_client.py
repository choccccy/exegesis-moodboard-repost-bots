"""Tests for bot.youtube.YouTubeClient - mocked google api client wiring."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from bot.youtube import YouTubeClient


def _make_client():
    """Build a YouTubeClient with Credentials and build patched at their
    lookup sites (both are imported inside __init__)."""
    with patch("google.oauth2.credentials.Credentials") as creds_cls, \
         patch("googleapiclient.discovery.build") as build_fn:
        client = YouTubeClient("cid", "csecret", "rtok")
    return client, creds_cls, build_fn


def test_constructor_builds_credentials_and_service():
    client, creds_cls, build_fn = _make_client()

    creds_cls.assert_called_once_with(
        token=None,
        refresh_token="rtok",
        token_uri="https://oauth2.googleapis.com/token",
        client_id="cid",
        client_secret="csecret",
        scopes=["https://www.googleapis.com/auth/youtube"],
    )
    build_fn.assert_called_once_with("youtube", "v3", credentials=creds_cls.return_value)
    assert client._svc is build_fn.return_value


def test_add_to_playlist_inserts_and_returns_item_id():
    client, _, _ = _make_client()
    insert = client._svc.playlistItems.return_value.insert
    insert.return_value.execute.return_value = {"id": "playlist-item-42"}

    result = client.add_to_playlist("PL123", "vidABC")

    insert.assert_called_once_with(
        part="snippet",
        body={
            "snippet": {
                "playlistId": "PL123",
                "resourceId": {"kind": "youtube#video", "videoId": "vidABC"},
            }
        },
    )
    insert.return_value.execute.assert_called_once_with()
    assert result == "playlist-item-42"


def test_remove_from_playlist_deletes_by_item_id():
    client, _, _ = _make_client()
    delete = client._svc.playlistItems.return_value.delete

    client.remove_from_playlist("item-99")

    delete.assert_called_once_with(id="item-99")
    delete.return_value.execute.assert_called_once_with()


def test_add_to_playlist_propagates_api_error():
    client, _, _ = _make_client()
    insert = client._svc.playlistItems.return_value.insert
    insert.return_value.execute.side_effect = RuntimeError("quota exceeded")

    try:
        client.add_to_playlist("PL123", "vidABC")
    except RuntimeError as exc:
        assert "quota exceeded" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected RuntimeError to propagate")
