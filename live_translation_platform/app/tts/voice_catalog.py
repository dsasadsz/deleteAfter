SUPPORTED_TTS_LANGUAGE_ORDER = ("kk", "uz", "zh-Hans", "ru")
LANGUAGE_TO_LOCALE = {"kk": "kk-KZ", "uz": "uz-UZ", "zh-Hans": "zh-CN", "ru": "ru-RU"}
LOCALE_TO_LANGUAGE = {locale.lower(): language for language, locale in LANGUAGE_TO_LOCALE.items()}

_AZURE_GENDER_HINTS = {
    "aigul": "female",
    "madina": "female",
    "xiaoxiao": "female",
    "svetlana": "female",
    "daulet": "male",
    "sardor": "male",
    "yunxi": "male",
    "dmitry": "male",
}


def empty_voice_catalog() -> dict[str, list[dict]]:
    return {language: [] for language in SUPPORTED_TTS_LANGUAGE_ORDER}


def split_voice_ids(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def azure_voice(
    voice_id: str,
    language: str,
    gender: str | None = None,
    name: str | None = None,
    locale: str | None = None,
    display_name: str | None = None,
    local_name: str | None = None,
) -> dict:
    selected_locale = locale or LANGUAGE_TO_LOCALE.get(language, "")
    selected_name = name or display_name or azure_voice_name(voice_id)
    voice = {
        "id": voice_id,
        "name": selected_name,
        "short_name": voice_id,
        "display_name": display_name or selected_name,
        "gender": (gender or azure_voice_gender(voice_id)).lower(),
        "provider": "azure",
        "language": language,
        "locale": selected_locale,
        "experimental": False,
    }
    if local_name:
        voice["local_name"] = local_name
    return voice


def generic_voice(
    voice_id: str,
    name: str,
    gender: str,
    provider: str,
    language: str,
    locale: str | None = None,
    experimental: bool = False,
    display_name: str | None = None,
) -> dict:
    return {
        "id": voice_id,
        "name": name,
        "display_name": display_name or name,
        "gender": (gender or "unknown").lower(),
        "provider": provider,
        "language": language,
        **({"locale": locale} if locale else {}),
        "experimental": experimental,
    }


def language_from_locale(locale: str | None) -> str | None:
    if not locale:
        return None
    normalized = locale.strip().lower().replace("_", "-")
    if normalized in LOCALE_TO_LANGUAGE:
        return LOCALE_TO_LANGUAGE[normalized]
    if normalized.startswith("zh"):
        return "zh-Hans"
    primary = normalized.split("-", 1)[0]
    if primary in {"kk", "uz", "ru"}:
        return primary
    return None


def azure_voice_name(voice_id: str) -> str:
    parts = voice_id.split("-")
    if len(parts) >= 3:
        name = parts[-1]
    else:
        name = voice_id
    if name.endswith("Neural"):
        name = name[: -len("Neural")]
    return name or voice_id


def azure_voice_gender(voice_id: str) -> str:
    normalized = voice_id.lower()
    for hint, gender in _AZURE_GENDER_HINTS.items():
        if hint in normalized:
            return gender
    return "unknown"


def dedupe_voices(voices: list[dict]) -> list[dict]:
    result = []
    seen = set()
    for voice in voices:
        voice_id = voice.get("id")
        if not voice_id or voice_id in seen:
            continue
        seen.add(voice_id)
        result.append(voice)
    return result
