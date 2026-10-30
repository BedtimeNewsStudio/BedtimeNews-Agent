"""Identical lexical analysis for the separately built Agent and indexer images.

Keep both copies, and both copies of search_words.txt, byte-identical; a
repository contract test checks this. Pinning jieba, the domain vocabulary, the
discovered word list, normalization and stopwords is part of SEARCH_VERSION.
"""

import hashlib
import logging
import re
import unicodedata
from pathlib import Path

import jieba

SEARCH_VERSION = 3
MAX_TERMS = 64
MAX_TERM_LENGTH = 64
_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
STOPWORDS = frozenset(
    "的 了 着 过 地 得 是 在 和 与 及 及其 或 而 但 也 都 就 又 并 并且 对 于 从 向 为 为了 "
    "把 被 让 给 有 会 能 可以 可能 一个 一些 这 那 这些 那些 我 我们 你 你们 他 他们 它 它们 "
    "什么 为什么 怎么 如何 哪些 哪个 是否 吗 呢 啊 吧 以及 其中 之 所 其 者".split()
)
# Keep question/analysis vocabulary searchable, but reduce its query weight.
_INTENT_TERMS = frozenset(
    "问题 现状 情况 成因 原因 分析 介绍 影响 发展 形成 带来 相关 目前 现在 进行".split()
)
_DOMAIN_WORDS = (
    "城投",
    "城投债",
    "地方债",
    "恒大",
    "碧桂园",
    "正威",
    "城商行",
    "房地产税",
    "人工智能",
    "大模型",
    "生成式",
    "高铁",
    "降准",
    "烂尾楼",
    "棚改",
)
WORDS_FILE = Path(__file__).with_name("search_words.txt")
jieba.setLogLevel(logging.WARNING)


def load_words(path: Path = WORDS_FILE) -> list[tuple[str, int]]:
    """Words discovered in the corpus: `word frequency` lines, `#` comments."""
    words = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.startswith("#"):
            word, freq = line.split()
            words.append((word, int(freq)))
    return words


def new_tokenizer(words: list[tuple[str, int]]) -> jieba.Tokenizer:
    """The fixed packaged dictionary plus extra words, never jieba's global one."""
    tokenizer = jieba.Tokenizer()
    for word, freq in words:
        tokenizer.add_word(word, freq=freq)
    return tokenizer


BASE_WORDS = [(word, 10000) for word in _DOMAIN_WORDS]
_tokenizer = new_tokenizer(BASE_WORDS + load_words())


def normalize(text: str) -> str:
    return unicodedata.normalize("NFC", text).translate(_ASCII_LOWER)


def lexeme(term: str) -> str:
    """A fixed-size SQL-safe lexeme; raw terms are verified in token_data too."""
    return "w" + hashlib.sha256(term.encode("utf-8")).hexdigest()


def analyze(text: str, *, search_mode: bool = True) -> dict[str, list[int]]:
    """Return complete token occurrence starts in normalized Unicode characters.

    Search segmentation includes dictionary subwords of compounds. Deduplicate
    identical word/offset pairs, but retain different words at the same offset.
    Offsets remain complete even beyond PostgreSQL tsvector's positional limits.
    """
    positions: dict[str, set[int]] = {}
    for term, start, _end in _tokenizer.tokenize(
        normalize(text), mode="search" if search_mode else "default", HMM=False
    ):
        if term not in STOPWORDS and any(c.isalnum() for c in term):
            positions.setdefault(term, set()).add(start)
    return {term: sorted(offsets) for term, offsets in positions.items()}


def query_terms(query: str) -> list[str]:
    # Precise query segmentation keeps a compound as one query concept.
    return list(analyze(query, search_mode=False))


def word_frequency(word: str) -> int:
    """Frequency of a word in the packaged jieba dictionary, 0 when unknown."""
    _tokenizer.check_initialized()
    return _tokenizer.FREQ.get(word, 0)


def query_tokens(query: str) -> list[tuple[str, int, int]]:
    """Searchable query words in order, with offsets in the normalized query.

    Query-side only: the index never calls this, so it is not part of
    SEARCH_VERSION. It lets the Agent rejoin words split out of one name.
    """
    return [
        (term, start, end)
        for term, start, end in _tokenizer.tokenize(
            normalize(query), mode="default", HMM=False
        )
        if term not in STOPWORDS and any(c.isalnum() for c in term)
    ]


def term_weight(term: str) -> float:
    return 0.15 if term in _INTENT_TERMS else 1.0


def proximity_patterns(terms: list[str]) -> list[str]:
    """A bounded proximity bonus, never a requirement for candidate recall."""
    return [
        re.escape(a) + r"[^\n]{0,12}" + re.escape(b)
        for a, b in zip(terms, terms[1:], strict=False)
    ]
