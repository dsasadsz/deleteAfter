from app.export.schemas import TranscriptExportSegment


def segment_text(segment: TranscriptExportSegment, lang: str, normalized: bool = True) -> str:
    if lang in {"ru", "ru-RU", "original"}:
        return segment.original_text_normalized if normalized else segment.original_text_raw
    return segment.translations.get(lang) or "Translation unavailable"
