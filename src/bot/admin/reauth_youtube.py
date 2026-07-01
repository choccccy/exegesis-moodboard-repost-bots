"""Run the YouTube OAuth flow to obtain a new refresh token.

Usage (from project root with credentials in environment):

    op run --env-file=op.env -- uv run python -m bot.admin.reauth_youtube

Opens a browser for the Google consent screen, then prints the new refresh
token. Copy it into 1Password and redeploy.
"""

from __future__ import annotations

import os
import sys


def main() -> None:
    client_id = os.environ.get("YOUTUBE_CLIENT_ID")
    client_secret = os.environ.get("YOUTUBE_CLIENT_SECRET")

    if not client_id or not client_secret:
        print("ERROR: YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET must be set in the environment.")
        print("Run with: op run --env-file=op.env -- uv run python -m bot.admin.reauth_youtube")
        sys.exit(1)

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print("ERROR: google-auth-oauthlib is not installed.")
        print("Install dev dependencies: uv sync --extra dev")
        sys.exit(1)

    client_config = {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["urn:ietf:wg:oauth:2.0:oob", "http://localhost"],
        }
    }

    flow = InstalledAppFlow.from_client_config(
        client_config,
        scopes=["https://www.googleapis.com/auth/youtube"],
    )

    print("Opening browser for Google sign-in...")
    print("(If no browser opens, copy the URL printed below and open it manually.)")
    print()

    creds = flow.run_local_server(port=0, open_browser=True)

    print()
    print("=" * 60)
    print("NEW REFRESH TOKEN:")
    print()
    print(creds.refresh_token)
    print()
    print("=" * 60)
    print("Store this in 1Password as YOUTUBE_REFRESH_TOKEN, then redeploy.")


if __name__ == "__main__":
    main()
