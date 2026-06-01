from app.export.schemas import TranscriptExport
from app.export.text_selection import segment_text


class MarkdownExporter:
    def export(self, transcript: TranscriptExport, lang: str = "all", normalized: bool = True) -> str:
        lines = [
            f"# {transcript.title}",
            "",
            "## Lesson Metadata",
            "",
            f"- lesson_id: `{transcript.lesson_id}`",
            f"- mode: `{transcript.mode}`",
            f"- STT provider: `{transcript.providers.get('stt')}`",
            f"- translator: `{transcript.providers.get('translator')}`",
            f"- glossary: `{transcript.glossary.get('glossary_id') or 'none'}`",
            "",
            "## Latency Summary",
            "",
            f"- average STT: {transcript.metrics.get('latency', {}).get('avg_stt', 0)} ms",
            f"- average translation: {transcript.metrics.get('latency', {}).get('avg_translation', 0)} ms",
            f"- average total: {transcript.metrics.get('latency', {}).get('avg_total', 0)} ms",
            "",
            "## Transcript",
            "",
        ]
        for segment in transcript.segments:
            lines.append(f"### {segment.start_time} - {segment.end_time}")
            if lang == "all":
                lines.append(f"- RU raw: {segment.original_text_raw}")
                lines.append(f"- RU normalized: {segment.original_text_normalized}")
                for language in transcript.target_languages:
                    lines.append(f"- {language}: {segment.translations.get(language) or 'Translation unavailable'}")
            else:
                lines.append(segment_text(segment, lang, normalized))
            if segment.glossary.get("normalization_changes") or segment.glossary.get("postprocess_changes"):
                lines.append("")
                lines.append("Glossary corrections:")
                for change in [*segment.glossary.get("normalization_changes", []), *segment.glossary.get("postprocess_changes", [])]:
                    lines.append(f"- {change.get('from', '')} -> {change.get('to', change.get('canonical', ''))}")
            lines.append("")
        return "\n".join(lines)
