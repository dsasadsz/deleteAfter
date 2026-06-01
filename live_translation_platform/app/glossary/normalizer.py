import re
from dataclasses import dataclass

from app.glossary.schemas import GlossaryTermData, NormalizationResult


@dataclass(frozen=True)
class _Match:
    start: int
    end: int
    replacement: str
    matched: str
    term: GlossaryTermData
    match_type: str


class TranscriptNormalizer:
    def normalize(self, text: str, glossary_terms: list[GlossaryTermData]) -> NormalizationResult:
        if not text:
            return NormalizationResult(original_text=text, normalized_text=text, changes=[])
        candidates = self._matches(text, glossary_terms)
        selected = self._select_non_overlapping(candidates)
        if not selected:
            return NormalizationResult(original_text=text, normalized_text=text, changes=[])

        output = []
        cursor = 0
        changes = []
        for match in sorted(selected, key=lambda item: item.start):
            output.append(text[cursor : match.start])
            output.append(match.replacement)
            cursor = match.end
            changes.append(
                {
                    "from": match.matched,
                    "to": match.replacement,
                    "term_id": match.term.id,
                    "match_type": match.match_type,
                }
            )
        output.append(text[cursor:])
        return NormalizationResult(original_text=text, normalized_text="".join(output), changes=changes)

    def _matches(self, text: str, terms: list[GlossaryTermData]) -> list[_Match]:
        matches = []
        for term in terms:
            if not term.enabled:
                continue
            flags = 0 if term.case_sensitive else re.IGNORECASE
            if term.match_type == "regex":
                patterns = [term.source]
            else:
                values = [term.source, *term.aliases]
                patterns = [_term_pattern(value, term.match_type) for value in values if value]
            for pattern in patterns:
                try:
                    iterator = re.finditer(pattern, text, flags=flags)
                except re.error:
                    continue
                for found in iterator:
                    matches.append(
                        _Match(
                            start=found.start(),
                            end=found.end(),
                            replacement=term.canonical,
                            matched=found.group(0),
                            term=term,
                            match_type=term.match_type,
                        )
                    )
        return matches

    def _select_non_overlapping(self, matches: list[_Match]) -> list[_Match]:
        selected: list[_Match] = []
        occupied: list[range] = []
        ordered = sorted(matches, key=lambda item: (-item.term.priority, -(item.end - item.start), item.start))
        for match in ordered:
            current = range(match.start, match.end)
            if any(_overlaps(current, previous) for previous in occupied):
                continue
            selected.append(match)
            occupied.append(current)
        return selected


def _term_pattern(value: str, match_type: str) -> str:
    escaped = re.escape(value)
    if match_type == "exact":
        return rf"(?<!\w){escaped}(?!\w)"
    return rf"(?<!\w){escaped}(?!\w)"


def _overlaps(left: range, right: range) -> bool:
    return left.start < right.stop and right.start < left.stop
