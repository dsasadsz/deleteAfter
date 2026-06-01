from uuid import uuid4

from app.zoom.models import ZoomMeeting


class MockZoomClient:
    async def create_meeting(self, title: str) -> ZoomMeeting:
        meeting_id = str(900000000 + int(uuid4().int % 99999999))
        meeting_uuid = f"mock-{uuid4()}"
        return ZoomMeeting(
            meeting_id=meeting_id,
            meeting_uuid=meeting_uuid,
            join_url=f"https://zoom.example/mock/join/{meeting_id}",
            start_url=f"https://zoom.example/mock/start/{meeting_id}",
            topic=title,
            created_at=None,
            password="",
        )
