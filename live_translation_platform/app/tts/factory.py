from app.tts.base import TTSConfigurationError, TTSProvider


def create_tts_provider(name: str, **kwargs) -> TTSProvider:
    normalized = name.lower()
    if normalized == "mock":
        from app.tts.mock_tts import MockTTS

        return MockTTS()
    if normalized == "azure":
        from app.tts.azure_tts import AzureTTS

        return AzureTTS(**kwargs)
    if normalized == "elevenlabs":
        from app.tts.elevenlabs_tts import ElevenLabsTTS

        return ElevenLabsTTS(**kwargs)
    if normalized == "local":
        from app.tts.local_tts import LocalTTSProvider

        return LocalTTSProvider(**kwargs)
    raise TTSConfigurationError(f"Unknown TTS provider: {name}")
