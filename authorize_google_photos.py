from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/photoslibrary.readonly"]


def main() -> None:
    root = Path(__file__).resolve().parent
    credentials_path = root / "credentials.json"
    token_path = root / "token_photos.json"

    if not credentials_path.exists():
        raise SystemExit("credentials.json not found in project root.")

    flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)
    creds = flow.run_local_server(port=0)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    print(f"Saved Photos token to {token_path}")


if __name__ == "__main__":
    main()

