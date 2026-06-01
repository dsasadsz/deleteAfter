from pydantic import BaseModel


class LessonLatencyAverage(BaseModel):
    lesson_id: str
    stt: float
    translation: float
    total: float

