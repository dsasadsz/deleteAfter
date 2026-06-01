from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.config import get_settings
from app.translation.local_engines.model_loader import (
    TilmashBackendConfig,
    TransformersTilmashBackend,
    _forced_bos_token_id,
    resolve_tilmash_language_code,
)


DEFAULT_SENTENCE = "Привет, класс! Откройте редактор кода и запустите программу."


async def main() -> int:
    args = parse_args()
    settings = get_settings()
    backend = TransformersTilmashBackend(
        TilmashBackendConfig(
            model_path=settings.tilmash_model_path,
            tokenizer_path=settings.tilmash_tokenizer_path,
            device=settings.tilmash_device,
            dtype=settings.tilmash_dtype,
            max_batch_size=1,
        )
    )
    print("loading Tilmash from <configured> ...")
    await backend.load()
    targets = [item.strip() for item in args.targets.split(",") if item.strip()]

    for target in targets:
        try:
            resolved = resolve_tilmash_language_code(target, tokenizer=backend.tokenizer)
            forced_bos = _forced_bos_token_id(backend.tokenizer, target)
        except Exception as exc:
            print(f"{target}: unsupported ({exc})")
            continue
        started_at = perf_counter()
        try:
            output = (await backend.translate_batch([args.text], "ru-RU", target))[0]
        except Exception as exc:
            print(f"{target}: {resolved} forced_bos_token_id={forced_bos} ERROR {exc.__class__.__name__}: {exc}")
            continue
        latency_ms = (perf_counter() - started_at) * 1000
        print(f"{target}: {resolved} forced_bos_token_id={forced_bos} latency_ms={latency_ms:.2f}")
        print(f"  {output}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Debug local Tilmash target language routing.")
    parser.add_argument("--text", default=DEFAULT_SENTENCE)
    parser.add_argument("--targets", default="kk,ru,en,tr,uz")
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
