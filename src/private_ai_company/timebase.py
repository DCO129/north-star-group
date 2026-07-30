'''Authoritative Beijing-time utilities.'''

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

AUTHORITATIVE_TIMEZONE = 'Asia/Shanghai'
try:
    BEIJING_TZ = ZoneInfo(AUTHORITATIVE_TIMEZONE)
except ZoneInfoNotFoundError:
    BEIJING_TZ = timezone(timedelta(hours=8), name=AUTHORITATIVE_TIMEZONE)


def authoritative_now() -> datetime:
    return datetime.now(BEIJING_TZ)


def normalize_to_authoritative(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError('Timestamp must include an explicit timezone offset.')
    return value.astimezone(BEIJING_TZ)


def authoritative_timestamp(value: datetime | None = None) -> str:
    current = authoritative_now() if value is None else normalize_to_authoritative(value)
    return current.isoformat(timespec='seconds')
