# FramePi

Fullscreen Raspberry Pi photo gallery synced from a public/shared Google Photos link, including local image metadata (EXIF when available) and **video** files when the album serves them.

## Features

- Pulls images and videos from a shared Google Photos album URL (detected from download bytes and `Content-Type`).
- Stores media locally in `data/photos` for smooth playback.
- Shows metadata such as creation date, camera model, ISO, shutter speed, and resolution if present in image EXIF.
- **On this day:** press **t** in the viewer (or **On this day** on the phone remote) to show only photos taken on today’s month and day in **earlier years** (local timezone); press again for the full library.
- Runs a slideshow in a browser (`http://<pi-ip>:8080`).

## 2) Project setup on Raspberry Pi

```bash
cd ~/FramePi
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
# Create `.env` in this folder (see variables below).
```

Then edit `.env`:

```env
GOOGLE_PHOTOS_SHARED_ALBUM_URL=https://photos.app.goo.gl/SZ2HK3EHBfAWo22D9
SYNC_INTERVAL_SECONDS=900
PORT=8080
```

## 3) First run (sync + initial gallery)

```bash
source .venv/bin/activate
python app.py
```

After that, open:

- `http://localhost:8080` on the Pi
- or `http://<pi-local-ip>:8080` from another device

## Google Calendar on the map

Upcoming events appear in a **panel at the bottom** of the map view (↑) when `token_calendar.json` exists.

```bash
source .venv/bin/activate
python authorize_google_calendar.py
```

Optional `.env` (defaults shown):

- `GOOGLE_CALENDAR_IDS=primary` — comma-separated calendar IDs
- `GOOGLE_CALENDAR_HORIZON_DAYS=21` — how far ahead to query
- `GOOGLE_CALENDAR_MAX_EVENTS=12` — max events listed
- `FRAMEPI_CALENDAR_REFRESH_SEC=300` — refetch interval while on the map
- `FRAMEPI_MAP_CALENDAR=0` — hide the panel

## 4) Run automatically on boot (systemd)

Create `/etc/systemd/system/framepi.service`:

```ini
[Unit]
Description=FramePi Google Photos Gallery
After=network-online.target
Wants=network-online.target

[Service]
User=pi
WorkingDirectory=/home/pi/FramePi
Environment=PYTHONUNBUFFERED=1
ExecStart=/home/pi/FramePi/.venv/bin/python /home/pi/FramePi/app.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Enable:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now framepi
sudo systemctl status framepi
```

## Notes

- Shared-album sync uses page scraping and may break if Google changes page structure.
- Videos are included when the album serves downloadable video URLs.
- With ``DELETE_REMOVED_FROM_FRAME`` enabled, files removed from the album can be deleted from ``data/photos`` (see env docs in code).
# FramePi
