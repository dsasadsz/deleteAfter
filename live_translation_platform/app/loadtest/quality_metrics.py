from __future__ import annotations

import re
from collections import Counter
from typing import Any


PROGRAMMING_TERMS = {
    "api",
    "c#",
    "css",
    "docker",
    "git",
    "html",
    "javascript",
    "js",
    "kubernetes",
    "python",
    "redis",
    "sql",
    "typescript",
    "ts",
    "websocket",
    "websockets",
}

_WORD_RE = re.compile(r"[\w#+.-]+", re.UNICODE)
_CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]")
_HAN_RE = re.compile(r"[\u3400-\u9fff]")
_LATIN_RE = re.compile(r"[A-Za-z]")


def stt_quality_report(reference_text: str, recognized_text: str, segments: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    reference_words = _words(reference_text)
    recognized_words = _words(recognized_text)
    missing_words = _counter_diff(reference_words, recognized_words)
    extra_words = _counter_diff(recognized_words, reference_words)
    return {
        "reference_text": reference_text,
        "recognized_text": recognized_text,
        "wer": round(_levenshtein(reference_words, recognized_words) / max(1, len(reference_words)), 4),
        "cer": round(_levenshtein(list(reference_text), list(recognized_text)) / max(1, len(reference_text)), 4),
        "missing_words": missing_words,
        "extra_words": extra_words,
        "side_by_side_diff": _side_by_side_diff(reference_words, recognized_words),
        "segments": segments or [],
    }


def translation_quality_report(target_language: str, output_text: str, reference_text: str | None = None) -> dict[str, Any]:
    return {
        "target_language": target_language,
        "output_text": output_text,
        "reference_text": reference_text,
        "checks": {
            "repetition": _repetition_check(output_text),
            "code_mixing": _code_mixing_check(target_language, output_text),
            "wrong_script": _wrong_script_check(target_language, output_text),
            "length_explosion": _length_explosion_check(output_text, reference_text),
        },
        "manual_score": None,
        "notes": None,
    }


def _words(value: str) -> list[str]:
    return [match.group(0).casefold() for match in _WORD_RE.finditer(value or "")]


def _counter_diff(left: list[str], right: list[str]) -> list[str]:
    remaining = Counter(left) - Counter(right)
    result: list[str] = []
    for word in left:
        if remaining[word] > 0:
            result.append(word)
            remaining[word] -= 1
    return result


def _levenshtein(left: list[Any], right: list[Any]) -> int:
    if not left:
        return len(right)
    if not right:
        return len(left)
    previous = list(range(len(right) + 1))
    for left_index, left_item in enumerate(left, start=1):
        current = [left_index]
        for right_index, right_item in enumerate(right, start=1):
            substitution = previous[right_index - 1] + (0 if left_item == right_item else 1)
            insertion = current[right_index - 1] + 1
            deletion = previous[right_index] + 1
            current.append(min(substitution, insertion, deletion))
        previous = current
    return previous[-1]


def _side_by_side_diff(reference_words: list[str], recognized_words: list[str]) -> list[dict[str, str]]:
    max_len = max(len(reference_words), len(recognized_words))
    rows = []
    for index in range(max_len):
        expected = reference_words[index] if index < len(reference_words) else ""
        actual = recognized_words[index] if index < len(recognized_words) else ""
        status = "same" if expected == actual else "different"
        rows.append({"reference": expected, "recognized": actual, "status": status})
    return rows


def _repetition_check(text: str) -> dict[str, Any]:
    words = _words(text)
    if not words:
        return {"status": "pass", "matches": []}
    matches: list[str] = []
    streak_word = None
    streak = 0
    for word in words:
        if word == streak_word:
            streak += 1
        else:
            streak_word = word
            streak = 1
        if streak >= 3 and word not in matches:
            matches.append(word)
    for word, count in Counter(words).items():
        if count >= 4 and word not in matches:
            matches.append(word)
    return {"status": "warn" if matches else "pass", "matches": matches}


def _code_mixing_check(target_language: str, text: str) -> dict[str, Any]:
    words = _words(text)
    suspect = []
    for word in words:
        normalized = word.casefold()
        if normalized in PROGRAMMING_TERMS:
            continue
        if target_language in {"kk", "ru"}:
            if _LATIN_RE.search(word) and not _CYRILLIC_RE.search(word):
                suspect.append(word)
        elif target_language == "zh-Hans":
            if (_LATIN_RE.search(word) or _CYRILLIC_RE.search(word)) and normalized not in PROGRAMMING_TERMS:
                suspect.append(word)
    return {"status": "warn" if suspect else "pass", "terms": suspect}


def _wrong_script_check(target_language: str, text: str) -> dict[str, Any]:
    if not text.strip():
        return {"status": "warn", "message": "empty output"}
    if target_language == "zh-Hans" and not _HAN_RE.search(text):
        return {"status": "warn", "message": "expected Han script"}
    if target_language == "kk" and not (_CYRILLIC_RE.search(text) or _LATIN_RE.search(text)):
        return {"status": "warn", "message": "expected Kazakh Cyrillic or Latin text"}
    return {"status": "pass", "message": ""}


def _length_explosion_check(output_text: str, reference_text: str | None) -> dict[str, Any]:
    if not reference_text:
        return {"status": "pass", "ratio": None}
    ratio = len(output_text) / max(1, len(reference_text))
    return {"status": "warn" if ratio > 3.0 else "pass", "ratio": round(ratio, 3)}
