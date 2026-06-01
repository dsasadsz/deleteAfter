from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def main() -> int:
    args = parse_args()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        "-m",
        "ctranslate2.converters.transformers",
        "--model",
        args.model,
        "--output_dir",
        str(output),
        "--quantization",
        args.quantization,
    ]
    if args.copy_files:
        command.append("--copy_files")
        command.extend(args.copy_files)
    print("Running CTranslate2 conversion.")
    print("Note: model input may require internet unless it is a local Hugging Face model path or already cached.")
    print(" ".join(command))
    return subprocess.call(command)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Convert M2M100 to a local CTranslate2 model directory.")
    parser.add_argument(
        "--model",
        default="facebook/m2m100_418M",
        help="Hugging Face model id or local HF model directory. Remote ids may download during conversion.",
    )
    parser.add_argument("--output", required=True, help="Output CT2 model directory.")
    parser.add_argument("--quantization", default="int8", choices=["int8", "int8_float16", "float16", "float32"])
    parser.add_argument(
        "--copy-files",
        nargs="+",
        default=["sentencepiece.bpe.model", "vocab.json", "tokenizer_config.json", "special_tokens_map.json"],
        help="Tokenizer files to copy into the CT2 directory when present.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(main())
