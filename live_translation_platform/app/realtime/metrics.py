from datetime import datetime


def milliseconds_between(start: datetime, end: datetime) -> int:
    return max(0, int((end - start).total_seconds() * 1000))


def isoformat_z(value: datetime) -> str:
    return f"{value.isoformat(timespec='milliseconds')}Z"

