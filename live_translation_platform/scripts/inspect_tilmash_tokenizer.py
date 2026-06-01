from __future__ import annotations

import re
import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.config import get_settings
from app.translation.local_engines.model_loader import (
    _token_id_for_language_code,
    resolve_tilmash_language_code,
    tilmash_language_diagnostics,
)


TERMS = ("kaz", "kk", "rus", "uz", "uzn", "uzb", "tur", "eng")
REQUIRED_CODES = ("rus_Cyrl", "kaz_Cyrl", "tur_Latn", "eng_Latn", "uzn_Latn", "uzb_Latn")
LANG_CODE_RE = re.compile(r"^[a-z]{3}_[A-Z][a-z]{3}$")


def main() -> int:
    try:
        from transformers import AutoTokenizer
    except Exception as exc:
        print(f"ERROR: transformers is required: {exc.__class__.__name__}", file=sys.stderr)
        return 1

    settings = get_settings()
    tokenizer_path = settings.tilmash_tokenizer_path or settings.tilmash_model_path
    if not tokenizer_path:
        print("ERROR: TILMASH_TOKENIZER_PATH or TILMASH_MODEL_PATH is required.", file=sys.stderr)
        return 2

    tokenizer = AutoTokenizer.from_pretrained(tokenizer_path, local_files_only=True)
    diagnostics = tilmash_language_diagnostics(tokenizer)
    model_config = _model_config(Path(settings.tilmash_model_path or tokenizer_path))

    print(f"tokenizer_path=<configured>")
    print(f"tokenizer_class={tokenizer.__class__.__name__}")
    print(f"src_lang={getattr(tokenizer, 'src_lang', None)!r}")
    print(f"tgt_lang={getattr(tokenizer, 'tgt_lang', None)!r}")
    print(f"model_config_forced_bos_token_id={model_config.get('forced_bos_token_id')}")
    print(f"model_config_decoder_start_token_id={model_config.get('decoder_start_token_id')}")
    print(f"bos={getattr(tokenizer, 'bos_token', None)!r} id={getattr(tokenizer, 'bos_token_id', None)!r}")
    print(f"eos={getattr(tokenizer, 'eos_token', None)!r} id={getattr(tokenizer, 'eos_token_id', None)!r}")
    print(f"pad={getattr(tokenizer, 'pad_token', None)!r} id={getattr(tokenizer, 'pad_token_id', None)!r}")
    print()

    print("required_language_codes:")
    for code in REQUIRED_CODES:
        token_id = _token_id_for_language_code(tokenizer, code)
        print(f"  {code}: exists={token_id is not None} id={token_id}")
    print()

    print("project_language_resolution:")
    for project_language in ("ru", "ru-RU", "kk", "kk-KZ", "en", "tr", "uz"):
        try:
            resolved = resolve_tilmash_language_code(project_language, tokenizer=tokenizer)
            token_id = _token_id_for_language_code(tokenizer, resolved)
            print(f"  {project_language}: {resolved} id={token_id}")
        except Exception as exc:
            print(f"  {project_language}: unsupported ({exc})")
    print()

    print("candidate_language_tokens:")
    for token in _candidate_language_tokens(tokenizer):
        print(f"  {token}: id={_token_id_for_language_code(tokenizer, token)}")
    print()
    print(f"unsupported_project_languages={diagnostics.get('unsupported_project_languages', [])}")
    return 0


def _candidate_language_tokens(tokenizer) -> list[str]:
    candidates: set[str] = set()
    for attr in ("lang_code_to_id", "fairseq_tokens_to_ids"):
        mapping = getattr(tokenizer, attr, None)
        if isinstance(mapping, dict):
            candidates.update(str(key) for key in mapping)
    candidates.update(str(item) for item in (getattr(tokenizer, "additional_special_tokens", None) or []))
    try:
        for token in tokenizer.get_vocab().keys():
            token = str(token)
            if LANG_CODE_RE.match(token) and any(term in token.lower() for term in TERMS):
                candidates.add(token)
    except Exception:
        pass
    candidates.update(REQUIRED_CODES)
    return sorted(token for token in candidates if any(term in token.lower() for term in TERMS))


def _model_config(model_path: Path) -> dict:
    config_path = model_path / "config.json"
    try:
        return json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


if __name__ == "__main__":
    raise SystemExit(main())
