# semantic_search/processor.py

import logging
import re
import hashlib
from typing import List, Dict, Any
from dataclasses import dataclass

from article_search import split_into_articles

logger = logging.getLogger(__name__)


@dataclass
class DocumentChunk:
    """Represents a chunk of a document."""
    chunk_id: str
    text: str
    title: str
    metadata: Dict[str, Any]


# Legislation types that use article-based splitting
ARTICLE_BASED_TYPES = {1, 2, 4, 7, 19, 21}  # Kanun, Tuzuk, KHK, Kurum Yonetmeligi, CBK, CB Yonetmeligi
# Legislation types that use chunk-based splitting
CHUNK_BASED_TYPES = {20, 22}  # CB Karari, CB Genelgesi
# Teblig (9) tries article first, falls back to chunk


class MevzuatProcessor:
    """Processes legislation for semantic search with dual strategy."""

    def __init__(self, chunk_size: int = 1500, chunk_overlap: int = 300, min_chunk_size: int = 100):
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.min_chunk_size = min_chunk_size

    def process_legislation(self, markdown_content: str, mevzuat_no: str, mevzuat_tur: int) -> List[DocumentChunk]:
        """
        Process legislation content into chunks for embedding.

        Article-based (tur 1,2,4,7,19,21): split_into_articles() -> each article = one document
        Chunk-based (tur 20,22): overlapping chunks
        Teblig (9): try article split first, if no articles -> chunk fallback
        """
        if not markdown_content or len(markdown_content.strip()) < self.min_chunk_size:
            logger.warning(f"Content too short for {mevzuat_no}")
            return []

        doc_id = f"mevzuat_{mevzuat_tur}_{mevzuat_no}"

        if mevzuat_tur in ARTICLE_BASED_TYPES:
            return self._process_articles(markdown_content, doc_id, mevzuat_no, mevzuat_tur)
        elif mevzuat_tur in CHUNK_BASED_TYPES:
            return self._process_chunks(markdown_content, doc_id, mevzuat_no, mevzuat_tur)
        elif mevzuat_tur == 9:  # Teblig: try articles first
            chunks = self._process_articles(markdown_content, doc_id, mevzuat_no, mevzuat_tur)
            if chunks:
                return chunks
            logger.info(f"No articles found in Teblig {mevzuat_no}, falling back to chunk-based")
            return self._process_chunks(markdown_content, doc_id, mevzuat_no, mevzuat_tur)
        else:
            # Unknown type, default to chunk-based
            return self._process_chunks(markdown_content, doc_id, mevzuat_no, mevzuat_tur)

    def _process_articles(self, markdown_content: str, doc_id: str, mevzuat_no: str, mevzuat_tur: int) -> List[DocumentChunk]:
        """Split content into articles and create DocumentChunks."""
        articles = split_into_articles(markdown_content)
        chunks = []

        for article in articles:
            madde_no = article['madde_no']
            chunk_id = f"{doc_id}_madde_{madde_no}"
            title = f"Madde {madde_no}"
            if article['madde_title']:
                title = f"Madde {madde_no} - {article['madde_title']}"

            chunks.append(DocumentChunk(
                chunk_id=chunk_id,
                text=article['madde_content'],
                title=title,
                metadata={
                    'mevzuat_no': mevzuat_no,
                    'mevzuat_tur': mevzuat_tur,
                    'madde_no': madde_no,
                    'madde_title': article['madde_title'],
                    'type': 'article',
                }
            ))

        logger.info(f"Processed {len(chunks)} articles from {doc_id}")
        return chunks

    def _process_chunks(self, markdown_content: str, doc_id: str, mevzuat_no: str, mevzuat_tur: int) -> List[DocumentChunk]:
        """Split content into overlapping chunks."""
        text_chunks = self._create_chunks(markdown_content)
        chunks = []

        for i, chunk_text in enumerate(text_chunks):
            chunk_hash = hashlib.md5(f"{doc_id}_c{i}".encode()).hexdigest()[:8]
            chunk_id = f"{doc_id}_chunk_{i}_{chunk_hash}"

            chunks.append(DocumentChunk(
                chunk_id=chunk_id,
                text=chunk_text,
                title=f"Chunk {i+1}/{len(text_chunks)}",
                metadata={
                    'mevzuat_no': mevzuat_no,
                    'mevzuat_tur': mevzuat_tur,
                    'chunk_index': i,
                    'total_chunks': len(text_chunks),
                    'type': 'chunk',
                }
            ))

        logger.info(f"Processed {len(chunks)} chunks from {doc_id}")
        return chunks

    def _create_chunks(self, text: str) -> List[str]:
        """Create overlapping chunks from text using sentence boundaries."""
        sentences = self._split_sentences(text)
        chunks = []
        current_chunk = []
        current_size = 0

        for sentence in sentences:
            sentence_size = len(sentence)

            if current_size + sentence_size > self.chunk_size and current_chunk:
                chunk_text = ' '.join(current_chunk)
                chunks.append(chunk_text)

                # Create overlap
                overlap_size = 0
                overlap_sentences = []
                for sent in reversed(current_chunk):
                    overlap_size += len(sent)
                    overlap_sentences.insert(0, sent)
                    if overlap_size >= self.chunk_overlap:
                        break

                current_chunk = overlap_sentences
                current_size = sum(len(s) for s in current_chunk)

            current_chunk.append(sentence)
            current_size += sentence_size

        if current_chunk:
            chunk_text = ' '.join(current_chunk)
            if len(chunk_text) >= self.min_chunk_size:
                chunks.append(chunk_text)

        return chunks

    def _split_sentences(self, text: str) -> List[str]:
        """Split text into sentences, preserving Turkish abbreviations."""
        abbreviations = ['Dr', 'Prof', 'Av', 'Md', 'Yrd', 'Doç', 'No', 'S', 'vs', 'vb', 'bkz']

        temp_text = text
        replacements = {}
        for i, abbr in enumerate(abbreviations):
            placeholder = f"__ABBR{i}__"
            temp_text = temp_text.replace(f"{abbr}.", placeholder)
            replacements[placeholder] = f"{abbr}."

        sentence_endings = re.compile(r'[.!?]+')
        sentences = sentence_endings.split(temp_text)

        cleaned_sentences = []
        for sentence in sentences:
            for placeholder, original in replacements.items():
                sentence = sentence.replace(placeholder, original)
            sentence = sentence.strip()
            if sentence and len(sentence) > 10:
                cleaned_sentences.append(sentence)

        return cleaned_sentences
