"""Download the configured model's tokenizer.json (never committed).

    uv run python scripts/fetch_tokenizer.py            # Qwen/Qwen3.5-9B -> .cache/tokenizer.json
    uv run python scripts/fetch_tokenizer.py --repo X --out path

Then set TOKENIZER_PATH in .env to the printed path."""
import argparse
from pathlib import Path
from urllib.request import urlopen

parser = argparse.ArgumentParser()
parser.add_argument("--repo", default="Qwen/Qwen3.5-9B")
parser.add_argument("--out", default=".cache/tokenizer.json")
args = parser.parse_args()
target = Path(args.out).resolve()
target.parent.mkdir(parents=True, exist_ok=True)
with urlopen(f"https://huggingface.co/{args.repo}/resolve/main/tokenizer.json", timeout=60) as response:
    target.write_bytes(response.read())
print(target)
