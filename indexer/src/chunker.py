"""Document chunking with overlap and section awareness."""

import re
from typing import Any

from .models import Chunk, Document

# Pre-compile regex patterns for performance (avoid re-compilation on every call)
HEADING_PATTERN = re.compile(r"^(#{1,6})\s+(.+?)$", re.MULTILINE)
PARAGRAPH_SPLIT_PATTERN = re.compile(r"\n\s*\n")
TOKEN_PATTERN = re.compile(r"[\u4e00-\u9fff]|[a-zA-Z]+|\S")


def chunk_document(
    document: Document,
    target_chunk_size: int = 1000,
    max_chunk_size: int = 2500,
    min_chunk_size: int = 200,
    overlap_size: int = 150,
) -> list[Chunk]:
    """Chunk a document into smaller pieces with overlap.

    Args:
        document: Document to chunk
        target_chunk_size: Target chunk size in words
        max_chunk_size: Maximum allowed chunk size in words
        min_chunk_size: Minimum chunk size in words
        overlap_size: Number of words to overlap between chunks

    Returns:
        List of Chunk objects
    """
    sections = _split_into_sections(document.text)

    all_chunks = []
    chunk_index = 0
    previous_overlap = ""

    for section in sections:
        section_chunks = _chunk_section(
            section,
            document.doc_id,
            previous_overlap,
            target_chunk_size,
            max_chunk_size,
            overlap_size,
        )

        for chunk_data in section_chunks:
            # Filter out chunks that are too short
            word_count = count_words(chunk_data["text"])
            if word_count >= min_chunk_size:
                chunk = Chunk(
                    # doc_id is a URI ending in .md; the slug is its id-safe form.
                    id=f"{document.slug}_chunk_{chunk_index:03d}",
                    doc_id=document.doc_id,
                    chunk_index=chunk_index,
                    heading=chunk_data["heading"],
                    text=chunk_data["text"],
                    word_count=word_count,
                )
                all_chunks.append(chunk)
                chunk_index += 1

        # Extract overlap from last chunk for next section
        if section_chunks:
            last_chunk_text = section_chunks[-1]["text"]
            previous_overlap = _extract_last_words(last_chunk_text, overlap_size)

    return all_chunks


def count_words(text: str) -> int:
    """Count words in text (works for both English and Chinese).

    Args:
        text: Text content

    Returns:
        Approximate word count
    """
    # Count Chinese characters
    chinese_chars = len(re.findall(r"[\u4e00-\u9fff]", text))

    # Count English words
    english_words = len(re.findall(r"[a-zA-Z]+", text))

    return chinese_chars + english_words


def _split_into_sections(text: str) -> list[dict[str, Any]]:
    """Split text into sections based on Markdown headings.

    Args:
        text: Markdown text

    Returns:
        List of section dictionaries with keys: heading, level, content, breadcrumb
    """
    headings = _extract_headings(text)

    if not headings:
        # No headings found, treat entire text as one section
        return [{"heading": None, "level": 0, "content": text, "breadcrumb": []}]

    sections = []
    breadcrumb_stack = []

    for i, (pos, level, heading_text) in enumerate(headings):
        # Determine section content
        start_pos = pos
        end_pos = headings[i + 1][0] if i + 1 < len(headings) else len(text)
        content = text[start_pos:end_pos].strip()

        # Update breadcrumb stack
        while breadcrumb_stack and breadcrumb_stack[-1][0] >= level:
            breadcrumb_stack.pop()

        # Add current heading to breadcrumb
        breadcrumb_stack.append((level, heading_text))

        # Create breadcrumb list
        breadcrumb = [h[1] for h in breadcrumb_stack]

        sections.append(
            {
                "heading": heading_text,
                "level": level,
                "content": content,
                "breadcrumb": breadcrumb.copy(),
            }
        )

    return sections


def _extract_headings(text: str) -> list[tuple[int, int, str]]:
    """Extract Markdown headings from text.

    Args:
        text: Markdown text

    Returns:
        List of (position, level, heading_text) tuples
    """
    headings = []

    for match in HEADING_PATTERN.finditer(text):
        level = len(match.group(1))  # Number of # characters
        heading_text = match.group(2).strip()
        position = match.start()
        headings.append((position, level, heading_text))

    return headings


def _chunk_section(
    section: dict[str, Any],
    doc_id: str,
    previous_overlap: str,
    target_chunk_size: int,
    max_chunk_size: int,
    overlap_size: int,
) -> list[dict[str, Any]]:
    """Chunk a single section into smaller pieces if needed.

    Args:
        section: Section dictionary from _split_into_sections
        doc_id: Document ID
        previous_overlap: Overlap text from previous chunk
        target_chunk_size: Target size in words
        max_chunk_size: Maximum size in words
        overlap_size: Overlap size in words

    Returns:
        List of chunk dictionaries
    """
    content = section["content"]
    word_count = count_words(content)

    chunks = []
    overlap_text = previous_overlap

    # If section is small enough, return as single chunk
    if word_count <= max_chunk_size:
        chunk_text = (
            (overlap_text + "\n\n" + content).strip() if overlap_text else content
        )
        chunks.append(
            {
                "text": chunk_text,
                "heading": section["heading"],
                "breadcrumb": section["breadcrumb"],
                "doc_id": doc_id,
            }
        )
        return chunks

    # Section is too large, split by paragraphs
    paragraphs = _split_by_paragraphs(content)

    current_chunk = []
    current_size = 0

    # Start first chunk with overlap from previous section
    if overlap_text:
        current_chunk.append(overlap_text)
        current_size = count_words(overlap_text)

    for para in paragraphs:
        para_size = count_words(para)

        # If adding paragraph exceeds target, flush current chunk
        if current_size + para_size > target_chunk_size and current_chunk:
            chunk_text = "\n\n".join(current_chunk)
            chunks.append(
                {
                    "text": chunk_text,
                    "heading": section["heading"],
                    "breadcrumb": section["breadcrumb"],
                    "doc_id": doc_id,
                }
            )
            # Extract overlap for next chunk
            overlap_text = _extract_last_words(chunk_text, overlap_size)
            current_chunk = [overlap_text, para] if overlap_text else [para]
            current_size = (
                count_words(overlap_text) + para_size if overlap_text else para_size
            )
        else:
            # Add paragraph to current chunk
            current_chunk.append(para)
            current_size += para_size

    # Flush remaining chunk
    if current_chunk:
        chunks.append(
            {
                "text": "\n\n".join(current_chunk),
                "heading": section["heading"],
                "breadcrumb": section["breadcrumb"],
                "doc_id": doc_id,
            }
        )

    return chunks


def _split_by_paragraphs(text: str) -> list[str]:
    """Split text into paragraphs based on blank lines.

    Args:
        text: Text content

    Returns:
        List of paragraphs
    """
    paragraphs = PARAGRAPH_SPLIT_PATTERN.split(text)
    return [p.strip() for p in paragraphs if p.strip()]


def _extract_last_words(text: str, num_words: int) -> str:
    """Extract the last N words from text for overlap.

    Args:
        text: Text content
        num_words: Number of words to extract

    Returns:
        Last N words from the text
    """
    if num_words <= 0:
        return ""

    # Find all Chinese characters and English words
    tokens = []
    for match in TOKEN_PATTERN.finditer(text):
        token = match.group()
        if re.match(r"[\u4e00-\u9fff]", token) or re.match(r"[a-zA-Z]+", token):
            tokens.append(token)

    if len(tokens) <= num_words:
        return text

    # Simple approach: take last N% of characters as approximation
    char_ratio = num_words / len(tokens) if tokens else 0
    start_pos = int(len(text) * (1 - char_ratio))

    # Find a good break point (sentence or paragraph)
    search_text = text[max(0, start_pos - 50) : start_pos + 50]
    break_chars = ["。", "！", "？", "；", ".", "!", "?", ";", "\n\n"]

    best_pos = start_pos
    for char in break_chars:
        pos = search_text.rfind(char)
        if pos != -1:
            best_pos = max(0, start_pos - 50) + pos + 1
            break

    return text[best_pos:].strip()
