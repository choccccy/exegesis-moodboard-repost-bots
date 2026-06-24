"""Thin synchronous wrapper around the YouTube Data API v3 for playlist writes.

Requires OAuth2 credentials (refresh token) - a plain API key is read-only and
cannot insert playlist items. The client is initialized once at startup and
passed into the Discord bot; None if credentials are not configured.

Call add_to_playlist via run_in_executor since it is blocking I/O.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


class YouTubeClient:
    _SCOPE = "https://www.googleapis.com/auth/youtube"
    _TOKEN_URI = "https://oauth2.googleapis.com/token"

    def __init__(self, client_id: str, client_secret: str, refresh_token: str) -> None:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        creds = Credentials(
            token=None,
            refresh_token=refresh_token,
            token_uri=self._TOKEN_URI,
            client_id=client_id,
            client_secret=client_secret,
            scopes=[self._SCOPE],
        )
        self._svc = build("youtube", "v3", credentials=creds)

    def remove_from_playlist(self, item_id: str) -> None:
        """Remove a playlist item by its item ID (not the video ID)."""
        self._svc.playlistItems().delete(id=item_id).execute()

    def add_to_playlist(self, playlist_id: str, video_id: str) -> str:
        """Insert video into playlist. Returns the playlist item ID.

        Raises googleapiclient.errors.HttpError on API failure (duplicate,
        quota exceeded, auth error, etc.).
        """
        resp = self._svc.playlistItems().insert(
            part="snippet",
            body={
                "snippet": {
                    "playlistId": playlist_id,
                    "resourceId": {"kind": "youtube#video", "videoId": video_id},
                }
            },
        ).execute()
        return resp["id"]
