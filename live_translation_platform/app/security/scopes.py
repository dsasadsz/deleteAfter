CAPTIONS_READ = "captions:read"
AUDIO_WRITE = "audio:write"
DIAGNOSTICS_READ = "diagnostics:read"
ZOOM_EMBED = "zoom:embed"
QUESTION_WRITE = "question:write"
QUESTION_READ = "question:read"
QUESTION_MODERATE = "question:moderate"
TTS_PLAY = "tts:play"

STUDENT_TOKEN_SCOPES = {CAPTIONS_READ, ZOOM_EMBED, QUESTION_WRITE, QUESTION_READ, TTS_PLAY}
TEACHER_TOKEN_SCOPES = {AUDIO_WRITE, DIAGNOSTICS_READ, CAPTIONS_READ, QUESTION_READ, QUESTION_MODERATE}
ALL_TOKEN_SCOPES = STUDENT_TOKEN_SCOPES | TEACHER_TOKEN_SCOPES


def validate_requested_scopes(requested: list[str] | None, *, allowed: set[str], defaults: list[str]) -> list[str]:
    scopes = requested if requested is not None else defaults
    normalized = [scope.strip() for scope in scopes if scope and scope.strip()]
    disallowed = sorted(set(normalized) - allowed)
    if disallowed:
        raise ValueError(f"Scopes are not allowed: {', '.join(disallowed)}")
    return list(dict.fromkeys(normalized))
