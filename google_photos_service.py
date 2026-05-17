from __future__ import annotations

from pathlib import Path
from typing import Dict, Iterator

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/photoslibrary.readonly"]


def _load_photos_credentials(token_path: Path) -> Credentials:
    if not token_path.exists():
        raise FileNotFoundError(
            f"Photos token not found at {token_path}. "
            "Run authorize_google_photos.py first."
        )
    creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token_path.write_text(creds.to_json(), encoding="utf-8")
    if not creds.valid:
        raise RuntimeError("Photos token is invalid or expired. Re-run authorize_google_photos.py.")
    return creds


def get_media_item(token_path: Path, media_item_id: str) -> Dict:
    """Full mediaItem resource (search results are sometimes missing fields like location)."""
    creds = _load_photos_credentials(token_path)
    service = build("photoslibrary", "v1", credentials=creds, static_discovery=False)
    return service.mediaItems().get(mediaItemId=media_item_id).execute()


def iter_album_media_items(token_path: Path, album_id: str) -> Iterator[Dict]:
    """Yield mediaItems from a Google Photos album via the Photos Library API."""
    creds = _load_photos_credentials(token_path)
    service = build("photoslibrary", "v1", credentials=creds, static_discovery=False)

    page_token = None
    while True:
        body: Dict = {"albumId": album_id, "pageSize": 100}
        if page_token:
            body["pageToken"] = page_token
        resp = service.mediaItems().search(body=body).execute()
        for item in resp.get("mediaItems", []) or []:
            yield item
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

