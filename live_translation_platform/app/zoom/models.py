from dataclasses import dataclass


@dataclass(frozen=True)
class ZoomMeeting:
    meeting_id: str
    meeting_uuid: str
    join_url: str
    start_url: str
    topic: str
    created_at: str | None = None
    password: str = ""
