from dataclasses import dataclass, field

from pydantic import BaseModel, Field


@dataclass(frozen=True)
class GlossaryTermData:
    id: str
    source: str
    canonical: str
    aliases: list[str] = field(default_factory=list)
    translations: dict[str, str] = field(default_factory=dict)
    case_sensitive: bool = False
    match_type: str = "phrase"
    priority: int = 0
    enabled: bool = True


@dataclass(frozen=True)
class NormalizationResult:
    original_text: str
    normalized_text: str
    changes: list[dict]


@dataclass(frozen=True)
class PostprocessResult:
    translations: dict[str, str]
    changes: list[dict]


class GlossaryCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str = ""
    domain: str = ""
    source_language: str = "ru-RU"
    target_languages: list[str] = Field(default_factory=lambda: ["kk", "uz", "zh-Hans"])
    is_default: bool = False


class GlossaryUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = None
    domain: str | None = None
    source_language: str | None = None
    target_languages: list[str] | None = None
    is_default: bool | None = None


class GlossaryTermCreate(BaseModel):
    source: str = Field(min_length=1)
    canonical: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)
    translations: dict[str, str] = Field(default_factory=dict)
    case_sensitive: bool = False
    match_type: str = Field(default="phrase", pattern="^(exact|phrase|regex)$")
    priority: int = 0
    enabled: bool = True


class GlossaryTermUpdate(BaseModel):
    source: str | None = None
    canonical: str | None = None
    aliases: list[str] | None = None
    translations: dict[str, str] | None = None
    case_sensitive: bool | None = None
    match_type: str | None = Field(default=None, pattern="^(exact|phrase|regex)$")
    priority: int | None = None
    enabled: bool | None = None


class LessonGlossarySelection(BaseModel):
    glossary_id: str | None = None
    enabled: bool = True
