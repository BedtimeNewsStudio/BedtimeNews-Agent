"""Data models for document processing."""

from dataclasses import dataclass


@dataclass
class Document:
    """Represents a loaded Markdown document.

    Attributes:
        id: Unique identifier for the document
        file_path: Path to the source file
        doc_id: Document URI, e.g. "ShuiQianXiaoXi/0501-0600/0588.md"
        slug: doc_id without its .md suffix and with "/" replaced by "_",
            used to build chunk ids
        text: Cleaned 正文 text of the document
    """

    id: str
    file_path: str
    doc_id: str
    slug: str
    text: str


@dataclass
class Chunk:
    """Represents a chunk of text from a document.

    Attributes:
        id: Unique identifier for the chunk
        doc_id: ID of the parent document
        chunk_index: 0-based index of this chunk within the document
        text: The chunk's text content
        word_count: Number of words in this chunk
        heading: Section heading for this chunk
    """

    id: str
    doc_id: str
    chunk_index: int
    text: str
    word_count: int = 0
    heading: str | None = None
