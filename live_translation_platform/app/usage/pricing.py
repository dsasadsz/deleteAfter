from datetime import datetime


PLACEHOLDER_NOTE = "Placeholder pricing. Update before financial use."


def default_pricing_rows() -> list[dict]:
    now = datetime.utcnow()
    return [
        _row("stt", "mock", "audio_minute", 0.0, now),
        _row("stt", "elevenlabs", "audio_minute", 0.0, now),
        _row("stt", "azure", "audio_minute", 0.0, now),
        _row("stt", "cartesia", "audio_minute", 0.0, now),
        _row("translation", "mock", "million_characters", 0.0, now),
        _row("translation", "azure", "million_characters", 0.0, now),
        _row("translation", "azure_translator", "million_characters", 0.0, now),
    ]


def _row(provider_type: str, provider_name: str, unit: str, price: float, effective_from: datetime) -> dict:
    return {
        "provider_type": provider_type,
        "provider_name": provider_name,
        "unit": unit,
        "price_per_unit": price,
        "currency": "USD",
        "effective_from": effective_from,
        "source_note": PLACEHOLDER_NOTE,
        "enabled": True,
    }
