"""Turn a keyword query into the units that search matches and ranks.

A unit is one query word, or a phrase of touching words where at least one of
each touching pair is a fragment: a single character that is rarely a word on
its own. Names the dictionary does not know split into such pieces; searched
separately, a lone 斯 would match hundreds of unrelated transcripts, so the
pieces must appear next to each other in the body. Common single characters
(不, 生, 说) are words in their own right and never join a phrase; otherwise
年轻人不愿意生孩子 would become one phrase.

A unit can be written several ways: search_aliases.txt lists equivalent names
(特朗普 / 川普). Each spelling is a variant with its own words; the unit matches
where any variant does.

Core units decide whether a transcript is relevant at all. Question words such
as 问题 or 影响 and lone single characters only add to the score of transcripts
that a core unit already matched.
"""

from dataclasses import dataclass
from pathlib import Path

from .search_text import (
    MAX_TERM_LENGTH,
    normalize,
    query_tokens,
    term_weight,
    word_frequency,
)

# Dictionary frequency from which a single character counts as a word rather
# than a piece of a name: 不 360331, 生 22579; 美 16809, 斯 4120, 弘 163.
WORD_CHARACTER_FREQUENCY = 20000
ALIASES_FILE = Path(__file__).with_name("search_aliases.txt")
# Markers of a question rather than a name or topic lookup.
QUESTION_MARKERS = (
    "为什么", "为何", "怎么", "怎样", "如何", "哪些", "哪个", "是否", "什么", "多少",
    "能不能", "会不会", "有没有", "是不是", "吗", "呢", "?", "？",
)  # fmt: skip


def is_fragment(word: str) -> bool:
    return len(word) == 1 and word_frequency(word) < WORD_CHARACTER_FREQUENCY


@dataclass(frozen=True)
class Variant:
    text: str
    # (indexed word, offset of that word within text)
    parts: tuple[tuple[str, int], ...]

    @property
    def anchor(self) -> tuple[str, int]:
        """The longest part: usually the rarest word of the variant."""
        return max(self.parts, key=lambda part: len(part[0]))


@dataclass(frozen=True)
class QueryUnit:
    term: str
    variants: tuple[Variant, ...]
    weight: float
    core: bool

    @property
    def parts(self) -> tuple[tuple[str, int], ...]:
        return self.variants[0].parts

    @property
    def anchor(self) -> tuple[str, int]:
        return self.variants[0].anchor


def _load_aliases(path: Path = ALIASES_FILE) -> dict[str, tuple[str, ...]]:
    groups: dict[str, tuple[str, ...]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.startswith("#"):
            names = tuple(normalize(name) for name in line.split())
            for name in names:
                groups[name] = names
    return groups


ALIASES = _load_aliases()


def _group(tokens: list[tuple[str, int, int]]) -> list[list[tuple[str, int, int]]]:
    groups: list[list[tuple[str, int, int]]] = []
    for token in tokens:
        last = groups[-1][-1] if groups else None
        if (
            last is not None
            and token[1] == last[2]
            and (is_fragment(token[0]) or is_fragment(last[0]))
            and token[2] - groups[-1][0][1] <= MAX_TERM_LENGTH
        ):
            groups[-1].append(token)
        else:
            groups.append([token])
    return groups


def _variant(group: list[tuple[str, int, int]]) -> Variant:
    start = group[0][1]
    # Offsets come from the normalized text, so the variant text is the exact
    # span its words cover there.
    return Variant(
        text="".join(word for word, _, _ in group),
        parts=tuple((word, s - start) for word, s, _ in group),
    )


def _alias_variant(name: str) -> Variant | None:
    """A name from the alias table as one variant: its words must touch."""
    tokens = query_tokens(name)
    if not tokens or any(
        a[2] != b[1] for a, b in zip(tokens, tokens[1:], strict=False)
    ):
        return None
    return _variant(tokens)


def _unit(variant: Variant) -> QueryUnit:
    term = variant.text
    variants = [variant]
    for name in ALIASES.get(term, ()):
        alias = _alias_variant(name) if name != term else None
        if alias and alias not in variants:
            variants.append(alias)
    if len(variant.parts) > 1:
        weight, core = 1.0, True
    else:
        weight = term_weight(term)
        core = len(term) > 1 and weight == 1.0
    return QueryUnit(term=term, variants=tuple(variants), weight=weight, core=core)


def _merge_aliases(groups):
    """Join touching groups that together spell an alias name (国内 + 生产总值)."""
    merged = []
    i = 0
    while i < len(groups):
        for j in range(min(len(groups), i + 4), i + 1, -1):
            run = groups[i:j]
            touching = all(
                a[-1][2] == b[0][1] for a, b in zip(run, run[1:], strict=False)
            )
            text = "".join(word for group in run for word, _, _ in group)
            if touching and text in ALIASES:
                merged.append([token for group in run for token in group])
                i = j
                break
        else:
            merged.append(groups[i])
            i += 1
    return merged


def query_units(query: str) -> list[QueryUnit]:
    units: dict[str, QueryUnit] = {}
    for group in _merge_aliases(_group(query_tokens(query))):
        unit = _unit(_variant(group))
        units.setdefault(unit.term, unit)
    return list(units.values())


def split_phrases(units: list[QueryUnit]) -> list[QueryUnit]:
    """The fallback when a phrase occurs nowhere: search its words separately.

    Fragments are dropped. A misspelled name such as 阿尔特弥斯 would otherwise
    match every transcript containing 阿 and 尔; no result is more useful.
    """
    split: dict[str, QueryUnit] = {}
    for unit in units:
        if len(unit.parts) == 1:
            split.setdefault(unit.term, unit)
            continue
        for word, _ in unit.parts:
            if not is_fragment(word):
                split.setdefault(word, _unit(Variant(word, ((word, 0),))))
    return list(split.values())


def is_question(query: str, units: list[QueryUnit]) -> bool:
    """A question rather than a name or topic: worth a semantic search too."""
    if any(marker in query for marker in QUESTION_MARKERS):
        return True
    return any(not unit.core and len(unit.term) > 1 for unit in units)


def sql_inputs(units: list[QueryUnit]) -> dict:
    """Parallel arrays for the ranking SQL: units, their variants, and parts."""
    variants = [
        (unit_ordinal, variant)
        for unit_ordinal, unit in enumerate(units, start=1)
        for variant in unit.variants
    ]
    parts = [
        (variant_ordinal, word, offset)
        for variant_ordinal, (_, variant) in enumerate(variants, start=1)
        for word, offset in variant.parts
    ]
    return {
        "terms": [u.term for u in units],
        "weights": [u.weight for u in units],
        "cores": [u.core for u in units],
        "variant_units": [unit for unit, _ in variants],
        "variant_texts": [v.text for _, v in variants],
        "variant_sizes": [len(v.parts) for _, v in variants],
        "variant_anchors": [v.anchor[0] for _, v in variants],
        "variant_anchor_offsets": [v.anchor[1] for _, v in variants],
        "part_variants": [row[0] for row in parts],
        "part_words": [row[1] for row in parts],
        "part_offsets": [row[2] for row in parts],
        "words": sorted({row[1] for row in parts}),
    }
