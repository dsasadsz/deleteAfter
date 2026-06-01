from app.export.schemas import TranscriptExport
from app.export.text_selection import segment_text


class VTTExporter:
    def export(self, transcript: TranscriptExport, lang: str = "ru", normalized: bool = True) -> str:
        lines = ["WEBVTT", ""]
        for segment in transcript.segments:
            lines.append(f"{segment.start_time} --> {segment.end_time}")
            lines.append(segment_text(segment, lang, normalized))
            lines.append("")
        return "\n".join(lines)
