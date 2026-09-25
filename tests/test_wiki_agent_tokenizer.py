import json
from hashlib import sha256
from pathlib import Path

from aidd_chat.adapters.tokenizer import HfTokenizer

FIXTURES = Path(__file__).parent / "fixtures"


def test_python_counts_match_vectors():
    tokenizer = HfTokenizer(FIXTURES / "tiny-tokenizer.json")
    for vector in json.loads((FIXTURES / "token_vectors.json").read_text(encoding="utf-8")):
        assert tokenizer.count(vector["text"]) == vector["count"]
    assert tokenizer.sha256 == sha256((FIXTURES / "tiny-tokenizer.json").read_bytes()).hexdigest()
