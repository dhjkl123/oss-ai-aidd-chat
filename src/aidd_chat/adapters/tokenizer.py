"""The binding's Tokenizer Authority: the model's own tokenizer.json, loaded by the
library family the sidecar uses too, so both processes count identical ids."""
from hashlib import sha256
from pathlib import Path

from tokenizers import Tokenizer


class HfTokenizer:
    def __init__(self, path: str | Path) -> None:
        raw = Path(path).read_bytes()
        self.sha256 = sha256(raw).hexdigest()
        self._tokenizer = Tokenizer.from_str(raw.decode("utf-8"))

    def count(self, text: str) -> int:
        return len(self._tokenizer.encode(text, add_special_tokens=False).ids)
