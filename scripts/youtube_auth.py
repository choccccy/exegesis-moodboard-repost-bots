"""One-time OAuth2 flow to obtain a YouTube API refresh token.

Run locally (not in the container):
    uv run --extra dev python scripts/youtube_auth.py

Prerequisites: create a Google Cloud project, enable YouTube Data API v3, and
create OAuth 2.0 credentials of type "Desktop app". Paste the client ID and
secret when prompted. The script opens a browser for consent and then prints
the three values to save in 1Password as "YouTube OAuth2".
"""

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/youtube"]

client_id = input("Client ID: ").strip()
client_secret = input("Client secret: ").strip()

flow = InstalledAppFlow.from_client_config(
    {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    },
    scopes=SCOPES,
)
creds = flow.run_local_server(port=0)

print("\nSave these to 1Password as 'YouTube OAuth2':")
print(f"client_id={client_id}")
print(f"client_secret={client_secret}")
print(f"refresh_token={creds.refresh_token}")
