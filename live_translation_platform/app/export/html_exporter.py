import html

from app.export.schemas import TranscriptExport
from app.export.text_selection import segment_text


class HTMLExporter:
    def export(self, transcript: TranscriptExport, lang: str = "all", normalized: bool = True) -> str:
        rows = []
        for segment in transcript.segments:
            if lang == "all":
                text = "<br>".join(
                    [
                        f"<strong>RU raw:</strong> {html.escape(segment.original_text_raw)}",
                        f"<strong>RU normalized:</strong> {html.escape(segment.original_text_normalized)}",
                        *[
                            f"<strong>{html.escape(language)}:</strong> {html.escape(segment.translations.get(language) or 'Translation unavailable')}"
                            for language in transcript.target_languages
                        ],
                    ]
                )
            else:
                text = html.escape(segment_text(segment, lang, normalized))
            rows.append(
                f"<tr><td>{html.escape(segment.start_time)}</td><td>{html.escape(segment.end_time)}</td><td>{text}</td></tr>"
            )
        return f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <title>{html.escape(transcript.title)} transcript</title>
  <style>
    body {{ font-family: system-ui, sans-serif; margin: 32px; color: #1f2937; }}
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{ border-bottom: 1px solid #d8dee9; padding: 8px; text-align: left; vertical-align: top; }}
    .muted {{ color: #64748b; }}
  </style>
</head>
<body>
  <h1>{html.escape(transcript.title)}</h1>
  <p class="muted">{html.escape(transcript.lesson_id)} / {html.escape(transcript.providers.get('stt', ''))} + {html.escape(transcript.providers.get('translator', ''))}</p>
  <table>
    <thead><tr><th>Start</th><th>End</th><th>Text</th></tr></thead>
    <tbody>{''.join(rows)}</tbody>
  </table>
</body>
</html>"""
