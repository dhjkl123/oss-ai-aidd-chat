"""Builds tests/fixtures/tiny-tokenizer.json: a small byte-level BPE (qwen's model
family) so CI proves the Python and Node tokenizer libraries count identically
without downloading the 12 MB production tokenizer."""
import json
from pathlib import Path

from tokenizers import Tokenizer, decoders, models, pre_tokenizers, trainers

ROOT = Path(__file__).resolve().parents[1]
CORPUS = [
    "BMAD와 Superpowers의 워크플로 차이는 무엇인가요?",
    "Wiki에서 근거를 찾지 못했어요. Wiki 보충 대상이에요.",
    "The agent reads index.md first, then comparisons and queries.",
    "문서 읽는 중: BMAD vs Superpowers",
] * 50
VECTORS = [
    "", "a", "안녕하세요", "BMAD와 Superpowers", "Hello, wiki!", "😀 emoji ❤️",
    "줄\n바꿈\t탭", "{\"role\":\"user\",\"text\":\"질문\"}", "가" * 300,
]

tokenizer = Tokenizer(models.BPE())
tokenizer.pre_tokenizer = pre_tokenizers.ByteLevel(add_prefix_space=False)
tokenizer.decoder = decoders.ByteLevel()
trainer = trainers.BpeTrainer(vocab_size=400, initial_alphabet=pre_tokenizers.ByteLevel.alphabet())
tokenizer.train_from_iterator(CORPUS, trainer)
target = ROOT / "tests" / "fixtures" / "tiny-tokenizer.json"
target.parent.mkdir(parents=True, exist_ok=True)
tokenizer.save(str(target))
loaded = Tokenizer.from_file(str(target))
vectors = [{"text": text, "count": len(loaded.encode(text, add_special_tokens=False).ids)} for text in VECTORS]
(ROOT / "tests" / "fixtures" / "token_vectors.json").write_text(
    json.dumps(vectors, ensure_ascii=False, indent=1), encoding="utf-8")
