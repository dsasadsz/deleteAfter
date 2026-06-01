import html

from app.export.schemas import LessonNotesResult, TranscriptExport
from app.export.text_selection import segment_text


class NotesGenerator:
    def generate(
        self,
        transcript: TranscriptExport,
        language: str = "ru",
        mode: str = "simple",
        include_glossary_terms: bool = True,
    ) -> LessonNotesResult:
        if mode != "simple":
            raise NotImplementedError("LLM notes generation is not implemented yet.")
        key_points = [segment_text(segment, language, normalized=True) for segment in transcript.segments[:6]]
        glossary_terms = _glossary_terms(transcript) if include_glossary_terms else []
        lines = [
            f"# {transcript.title} Notes",
            "",
            "## Key Points",
            "",
            *[f"- {point}" for point in key_points if point],
            "",
            "## Glossary Terms Used",
            "",
            *([f"- {term}" for term in glossary_terms] if glossary_terms else ["- No glossary corrections recorded."]),
            "",
            "## Questions / Tasks",
            "",
            "- Review the main concepts from the lesson.",
            "- Write one practical example using the lesson terminology.",
            "",
            "## Timeline Summary",
            "",
            *[f"- {segment.start_time}: {segment_text(segment, language, normalized=True)}" for segment in transcript.segments[:8]],
            "",
        ]
        markdown = "\n".join(lines)
        html_content = _markdownish_to_html(markdown)
        return LessonNotesResult(
            lesson_id=transcript.lesson_id,
            language=language,
            mode=mode,
            content_markdown=markdown,
            content_html=html_content,
            metadata={"segments_used": len(transcript.segments), "glossary_terms_used": glossary_terms},
        )


def _glossary_terms(transcript: TranscriptExport) -> list[str]:
    terms = []
    for segment in transcript.segments:
        for change in segment.glossary.get("normalization_changes", []):
            value = change.get("to")
            if value and value not in terms:
                terms.append(value)
        for change in segment.glossary.get("postprocess_changes", []):
            value = change.get("canonical")
            if value and value not in terms:
                terms.append(value)
    return terms


def _markdownish_to_html(markdown: str) -> str:
    body = []
    for line in markdown.splitlines():
        if line.startswith("# "):
            body.append(f"<h1>{html.escape(line[2:])}</h1>")
        elif line.startswith("## "):
            body.append(f"<h2>{html.escape(line[3:])}</h2>")
        elif line.startswith("- "):
            body.append(f"<p>&bull; {html.escape(line[2:])}</p>")
        elif line:
            body.append(f"<p>{html.escape(line)}</p>")
    return "<!doctype html><html><body>" + "".join(body) + "</body></html>"
