from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]


def _load_calendar_credentials(token_path: Path) -> Credentials:
    if not token_path.exists():
        raise FileNotFoundError(
            f"Calendar token not found at {token_path}. "
            "Run authorize_google_calendar.py first."
        )
    creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token_path.write_text(creds.to_json(), encoding="utf-8")
    if not creds.valid:
        raise RuntimeError("Calendar token is invalid or expired. Re-run authorize_google_calendar.py.")
    return creds


def calendar_ids_from_env() -> List[str]:
    raw = os.getenv("GOOGLE_CALENDAR_IDS", "primary").strip()
    if not raw:
        return ["primary"]
    return [x.strip() for x in raw.split(",") if x.strip()]


def _event_start_utc(ev: Dict[str, Any]) -> datetime:
    start = ev.get("start") or {}
    if isinstance(start.get("dateTime"), str):
        return datetime.fromisoformat(start["dateTime"].replace("Z", "+00:00"))
    if isinstance(start.get("date"), str):
        return datetime.fromisoformat(start["date"] + "T00:00:00+00:00")
    return datetime.min.replace(tzinfo=timezone.utc)


def _fmt_event_line(ev: Dict[str, Any]) -> str:
    dt = _event_start_utc(ev)
    if dt == datetime.min.replace(tzinfo=timezone.utc):
        when = "?"
    else:
        local = dt.astimezone()
        when = local.strftime("%a %b %d · %H:%M")
    summary = str(ev.get("summary") or "(no title)").strip() or "(no title)"
    loc = str(ev.get("location") or "").strip()
    line = f"{when}  {summary}"
    if loc:
        line = f"{line}  @ {loc}"
    return line


def fetch_upcoming_events(
    token_path: Path,
    *,
    horizon_days: int | None = None,
    max_total: int | None = None,
    max_per_calendar: int = 24,
) -> List[Dict[str, Any]]:
    """
    Return upcoming single-instance events from the given calendars, sorted by start time.

    Each item is the Calendar API event dict (includes summary, start, location, …).
    """
    if horizon_days is None:
        horizon_days = max(1, min(60, int(os.getenv("GOOGLE_CALENDAR_HORIZON_DAYS", "21"))))
    if max_total is None:
        max_total = max(1, min(40, int(os.getenv("GOOGLE_CALENDAR_MAX_EVENTS", "12"))))

    creds = _load_calendar_credentials(token_path)
    service = build("calendar", "v3", credentials=creds, static_discovery=False)
    now = datetime.now(timezone.utc)
    time_min = now.isoformat()
    time_max = (now + timedelta(days=horizon_days)).isoformat()

    merged: List[Dict[str, Any]] = []
    for cid in calendar_ids_from_env():
        try:
            resp = (
                service.events()
                .list(
                    calendarId=cid,
                    timeMin=time_min,
                    timeMax=time_max,
                    maxResults=max_per_calendar,
                    singleEvents=True,
                    orderBy="startTime",
                )
                .execute()
            )
        except Exception:
            continue
        for ev in resp.get("items") or []:
            if not isinstance(ev, dict):
                continue
            merged.append(ev)

    merged.sort(key=_event_start_utc)
    return merged[:max_total]


def format_events_for_map_overlay(events: List[Dict[str, Any]], max_lines: int = 9) -> List[str]:
    """Short text lines for the map overlay (first line is a header)."""
    if not events:
        return []
    lines = ["Upcoming"]
    for ev in events[:max_lines]:
        lines.append(_fmt_event_line(ev))
    return lines
