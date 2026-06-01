import re

from app.glossary.schemas import GlossaryTermData, PostprocessResult


class TranslationPostProcessor:
    def postprocess(
        self,
        original_text: str,
        translations: dict[str, str],
        glossary_terms: list[GlossaryTermData],
    ) -> PostprocessResult:
        if not translations:
            return PostprocessResult(translations=translations, changes=[])

        output = dict(translations)
        changes: list[dict] = []
        active_terms = [term for term in glossary_terms if term.enabled and _term_present(original_text, term)]
        for language, translated in list(output.items()):
            value = translated
            for term in sorted(active_terms, key=lambda item: -item.priority):
                expected = term.translations.get(language) or term.canonical
                if not expected or _contains(value, expected):
                    continue
                updated = _replace_known_surface(value, term, expected)
                if updated == value:
                    updated = f"{value} {expected}".strip()
                output[language] = updated
                changes.append(
                    {
                        "language": language,
                        "from": value,
                        "to": updated,
                        "term_id": term.id,
                        "canonical": term.canonical,
                    }
                )
                value = updated
        return PostprocessResult(translations=output, changes=changes)


def _term_present(text: str, term: GlossaryTermData) -> bool:
    if _contains(text, term.canonical):
        return True
    values = [term.source, *term.aliases]
    return any(_contains(text, value) for value in values if value)


def _contains(text: str, value: str) -> bool:
    if not value:
        return False
    return re.search(_contains_pattern(value), text, flags=re.IGNORECASE) is not None


def _replace_known_surface(text: str, term: GlossaryTermData, expected: str) -> str:
    for surface in [term.source, term.canonical, *term.aliases]:
        if not surface:
            continue
        updated = re.sub(_surface_pattern(surface), expected, text, flags=re.IGNORECASE)
        if updated != text:
            return updated
    return text


def _surface_pattern(surface: str) -> str:
    escaped = re.escape(surface)
    if re.match(r"^\w", surface, flags=re.UNICODE):
        escaped = rf"(?<!\w){escaped}"
    if re.search(r"\w$", surface, flags=re.UNICODE):
        escaped = rf"{escaped}(?!\w)"
    return escaped


def _contains_pattern(surface: str) -> str:
    pattern = _surface_pattern(surface)
    if re.fullmatch(r"[А-Яа-яЁёӘәІіҢңҒғҚқҰұҮүӨөҺһ\s-]+", surface):
        suffix = r"[А-Яа-яЁёӘәІіҢңҒғҚқҰұҮүӨөҺһ]{0,8}"
        pattern = pattern.replace(r"(?!\w)", rf"{suffix}(?!\w)")
    return pattern
