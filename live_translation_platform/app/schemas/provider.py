from pydantic import BaseModel


class ProviderList(BaseModel):
    stt: list[str]
    translation: list[str]


class ProviderReadiness(BaseModel):
    ready: bool
    missing: list[str] = []


class ProviderStatus(BaseModel):
    stt: dict[str, ProviderReadiness]
    translation: dict[str, ProviderReadiness]
