# C# Integration Sample

This sample shows how an ASP.NET Core backend can call the Python translation service.

Environment:

```powershell
$env:TRANSLATION_SERVICE_URL="http://127.0.0.1:8000"
$env:TRANSLATION_SERVICE_KEY="dev-key-1"
```

Expected flow:

1. Create a lesson with an external C# lesson ID.
2. Store the returned Python `lesson_id` in the C# database.
3. Create student and teacher tokens through the v1 integration endpoints.
4. Give browser pages only short-lived scoped token URLs, not the integration key.
5. Teacher page uses the teacher token response for microphone audio ingest, diagnostics, and question moderation.
6. Student page uses the student token response for Zoom embed config, captions, TTS, text questions, and voice questions.
7. Fetch transcript/export/cost after the lesson.

The sample uses `mode=mock` so it can run without Zoom credentials. For real Zoom lessons, set `mode=zoom` and `create_zoom_meeting=true`.

The C# backend authenticates service-to-service calls with `X-Integration-Key`. Student and teacher browsers should use the signed scoped URLs returned by `/student-token` and `/teacher-token`.
