from app.export.schemas import TranscriptExport
from app.export.text_selection import segment_text


class SRTExporter:
    def export(self, transcript: TranscriptExport, lang: str = "ru", normalized: bool = True) -> str:
        blocks = []
        for index, segment in enumerate(transcript.segments, start=1):
            blocks.append(
                "\n".join(
                    [
                        str(index),
                        f"{_srt_time(segment.start_time)} --> {_srt_time(segment.end_time)}",
                        segment_text(segment, lang, normalized),
                    ]
                )
            )
        return "\n\n".join(blocks) + ("\n" if blocks else "")


def _srt_time(value: str) -> str:
    return value.replace(".", ",")
