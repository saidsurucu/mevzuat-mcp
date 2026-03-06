# mevzuat_mcp_server_new.py
"""
FastMCP server for mevzuat.gov.tr (direct API).
Supports searching and PDF content extraction for Kanun (laws).
"""
import logging
from pydantic import Field
from typing import Optional

from fastmcp import FastMCP

from mevzuat_client import MevzuatApiClientNew
from mevzuat_models import (
    MevzuatSearchRequestNew,
    MevzuatSearchResultNew,
    MevzuatArticleContent
)
from article_search import search_articles_by_keyword, ArticleSearchResult, format_search_results, _matches_query

# Semantic search (optional, requires OPENROUTER_API_KEY)
from semantic_search.embedder import is_openrouter_available
SEMANTIC_SEARCH_AVAILABLE = is_openrouter_available()
if SEMANTIC_SEARCH_AVAILABLE:
    from semantic_search import OpenRouterEmbedder, VectorStore, MevzuatProcessor, EmbeddingCache
    _embedder = OpenRouterEmbedder()
    _processor = MevzuatProcessor()
    _embedding_cache = EmbeddingCache(ttl=3600)

# Simple logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

app = FastMCP(
    name="MevzuatGovTrMCP",
    instructions="MCP server for mevzuat.gov.tr - Turkish legislation search and content retrieval. "
    "Supports 9 legislation types (21 tools): "
    "Kanun (laws), KHK (decree laws), Tüzük (statutes), Kurum Yönetmeliği (institutional regulations), "
    "Tebliğ (communiqués), CB Kararnamesi (presidential decrees), CB Kararı (presidential decisions), "
    "CB Yönetmeliği (presidential regulations), CB Genelgesi (presidential circulars). "
    "Each type has search and search_within tools. All 9 search_within tools support both keyword (Boolean AND/OR/NOT) "
    "and semantic search (natural language, requires OPENROUTER_API_KEY). Set semantic=True for semantic search. "
    "IMPORTANT: Search is keyword-based (not by law number) - use descriptive terms like "
    "'katma değer vergisi' instead of '3065', 'gümrük kanunu' instead of '4458'. "
    "Bakanlar Kurulu Kararı (BKK) is not a separate type - search under CB Kararı or Kanun."
)

# Initialize client with caching enabled (1 hour TTL by default)
# Mistral API key will be loaded from environment variable MISTRAL_API_KEY
mevzuat_client = MevzuatApiClientNew(cache_ttl=3600, enable_cache=True)


# ============================================================================
# Shared semantic search helper
# ============================================================================

async def _semantic_search_within(
    mevzuat_no: str,
    query: str,
    mevzuat_tur: int,
    mevzuat_tertip: str = "5",
    max_results: int = 10,
    threshold: float = 0.3,
    resmi_gazete_tarihi: Optional[str] = None,
) -> str:
    """Shared helper for semantic search within any legislation type."""
    # 1. Get content (already cached by mevzuat_client)
    content_result = await mevzuat_client.get_content(
        mevzuat_no=mevzuat_no,
        mevzuat_tur=mevzuat_tur,
        mevzuat_tertip=mevzuat_tertip,
        resmi_gazete_tarihi=resmi_gazete_tarihi,
    )

    if content_result.error_message:
        return f"Error fetching content: {content_result.error_message}"

    if not content_result.markdown_content:
        return f"Error: No content found for mevzuat {mevzuat_no}"

    content = content_result.markdown_content

    # 2. Check embedding cache
    cached = _embedding_cache.get(mevzuat_tur, mevzuat_tertip, mevzuat_no, content)
    if cached:
        vector_store, chunks = cached
    else:
        # 3. Process into chunks
        chunks = _processor.process_legislation(content, mevzuat_no, mevzuat_tur)
        if not chunks:
            return f"Error: Could not split content into searchable segments for mevzuat {mevzuat_no}"

        # 4. Encode documents
        texts = [c.text for c in chunks]
        titles = [c.title for c in chunks]
        embeddings = _embedder.encode_documents(texts, titles)

        # 5. Build vector store
        vector_store = VectorStore(dimension=_embedder.dimension)
        vector_store.add_documents(
            ids=[c.chunk_id for c in chunks],
            texts=texts,
            embeddings=embeddings,
            metadata=[c.metadata for c in chunks],
        )

        # 6. Cache
        _embedding_cache.put(mevzuat_tur, mevzuat_tertip, mevzuat_no, content, vector_store, chunks)

    # 7. Search
    query_embedding = _embedder.encode_query(query)
    results = vector_store.search(query_embedding, top_k=max_results, threshold=threshold)

    if not results:
        return f"No semantically similar content found for '{query}' in mevzuat {mevzuat_no}"

    # 8. Format results
    # Determine method description
    chunk_type = chunks[0].metadata.get('type', 'chunk') if chunks else 'chunk'
    method = "Article-based semantic search" if chunk_type == 'article' else "Chunk-based semantic search"

    output = []
    output.append("Semantic Search Results")
    output.append(f"Query: \"{query}\"")
    output.append(f"Legislation: {mevzuat_no} (type: {mevzuat_tur})")
    output.append(f"Method: {method} | Results: {len(results)}")
    output.append("")

    for doc, score in results:
        if chunk_type == 'article':
            madde_no = doc.metadata.get('madde_no', '?')
            madde_title = doc.metadata.get('madde_title', '')
            output.append(f"=== MADDE {madde_no} === (Similarity: {score:.2f})")
            if madde_title:
                output.append(f"Title: {madde_title}")
        else:
            chunk_idx = doc.metadata.get('chunk_index', 0)
            total = doc.metadata.get('total_chunks', 0)
            output.append(f"=== Chunk {chunk_idx + 1}/{total} === (Similarity: {score:.2f})")

        output.append("")
        output.append(doc.text)
        output.append("")

    return "\n".join(output)


async def _keyword_search_chunks(
    content: str,
    keyword: str,
    mevzuat_no: str,
    mevzuat_tur: int,
    case_sensitive: bool = False,
    max_results: int = 25,
) -> str:
    """Keyword search for chunk-based content (no article structure)."""
    # Try article split first for Teblig
    if mevzuat_tur == 9:
        from article_search import split_into_articles as _split
        articles = _split(content)
        if articles:
            matches = search_articles_by_keyword(content, keyword, case_sensitive, max_results)
            if matches:
                result = ArticleSearchResult(
                    mevzuat_no=mevzuat_no, mevzuat_tur=mevzuat_tur,
                    keyword=keyword, total_matches=len(matches), matching_articles=matches
                )
                return format_search_results(result)

    # Chunk-based keyword search
    from semantic_search.processor import MevzuatProcessor as _MevzuatProcessor
    processor = _processor if SEMANTIC_SEARCH_AVAILABLE else _MevzuatProcessor()
    chunks = processor.process_legislation(content, mevzuat_no, mevzuat_tur)

    if not chunks:
        return f"Error: Could not split content into searchable segments for mevzuat {mevzuat_no}"

    scored_chunks = []
    for chunk in chunks:
        matches, score = _matches_query(chunk.text, keyword, case_sensitive)
        if matches and score > 0:
            scored_chunks.append((chunk, score))

    scored_chunks.sort(key=lambda x: x[1], reverse=True)
    scored_chunks = scored_chunks[:max_results]

    if not scored_chunks:
        return f"No matches found for '{keyword}' in mevzuat {mevzuat_no}"

    output = []
    output.append(f"Keyword: '{keyword}'")
    output.append(f"Total matching segments: {len(scored_chunks)}")
    output.append("")

    for chunk, score in scored_chunks:
        chunk_type = chunk.metadata.get('type', 'chunk')
        if chunk_type == 'article':
            madde_no = chunk.metadata.get('madde_no', '?')
            output.append(f"=== MADDE {madde_no} ===")
        else:
            chunk_idx = chunk.metadata.get('chunk_index', 0)
            total = chunk.metadata.get('total_chunks', 0)
            output.append(f"=== Chunk {chunk_idx + 1}/{total} ===")
        output.append(f"Matches: {score}")
        output.append("")
        output.append("Full content:")
        output.append(chunk.text)
        output.append("")

    return "\n".join(output)


@app.tool()
async def search_kanun(
    aranacak_ifade: str = Field(
        ...,
        description='Search query with optional Boolean operators: simple word (yatırımcı), AND (yatırımcı AND tazmin), OR (vergi OR ücret), NOT (yatırımcı NOT kurum), + for required (+term), grouping with (), exact phrase with quotes ("mali sıkıntı")'
    ),
    tam_cumle: bool = Field(
        False,
        description="Exact phrase match (true) or any word match (false, default). Set to true when searching for exact phrases like 'mali sıkıntı'."
    ),
    baslangic_tarihi: Optional[str] = Field(
        None,
        description="Start date for filtering results (format: DD.MM.YYYY, e.g., '01.01.2020')"
    ),
    bitis_tarihi: Optional[str] = Field(
        None,
        description="End date for filtering results (format: DD.MM.YYYY, e.g., '31.12.2024')"
    ),
    page_number: int = Field(
        1,
        ge=1,
        description="Page number for pagination (starts at 1)"
    ),
    aranacak_yer: int = Field(
        3,
        ge=1,
        le=3,
        description="Where to search: 1=Title only, 2=Content only, 3=Both title and content (default)"
    ),
    page_size: int = Field(
        25,
        ge=1,
        le=100,
        description="Number of results per page (1-100)"
    )
) -> MevzuatSearchResultNew:
    """
    Search for Turkish laws (Kanun) in both titles and content on mevzuat.gov.tr.

    IMPORTANT: Search is keyword-based, NOT by law number. Use descriptive Turkish terms.
    - WRONG: "3065" or "6362" (numbers won't find laws reliably)
    - RIGHT: "katma değer vergisi" (finds KDV Kanunu No. 3065)
    - RIGHT: "sermaye piyasası" (finds Sermaye Piyasası Kanunu No. 6362)
    - RIGHT: "gümrük kanunu" (finds Gümrük Kanunu No. 4458)
    - RIGHT: "gelir vergisi" (finds Gelir Vergisi Kanunu No. 193)
    - RIGHT: "ceza muhakemesi" (finds CMK No. 5271)
    - RIGHT: "vergi usul" (finds VUK No. 213)

    Use 'search_within_kanun' to search within a specific law's articles after finding its number.

    Query Syntax:
    - Simple keyword: yatırımcı
    - Boolean AND: yatırımcı AND tazmin (both terms required)
    - Boolean OR: yatırımcı OR müşteri (at least one term)
    - Boolean NOT: yatırımcı NOT kurum (first yes, second no)
    - Required term: +yatırımcı +tazmin (similar to AND)
    - Grouping: (yatırımcı OR müşteri) AND tazmin
    - Exact phrase: "mali sıkıntı" (or use tam_cumle=true)

    Returns: Law number, title, acceptance date, Official Gazette date and issue number.
    """
    search_req = MevzuatSearchRequestNew(
        mevzuat_tur="Kanun",
        aranacak_ifade=aranacak_ifade,
        aranacak_yer=aranacak_yer,
        tam_cumle=tam_cumle,
        mevzuat_no=None,
        baslangic_tarihi=baslangic_tarihi,
        bitis_tarihi=bitis_tarihi,
        page_number=page_number,
        page_size=page_size
    )

    log_params = search_req.model_dump(exclude_defaults=True)
    logger.info(f"Tool 'search_kanun' called with parameters: {log_params}")

    try:
        result = await mevzuat_client.search_documents(search_req)

        if not result.documents and not result.error_message:
            result.error_message = "No legislation found matching the specified criteria."

        return result

    except Exception as e:
        logger.exception("Error in tool 'search_mevzuat'")
        return MevzuatSearchResultNew(
            documents=[],
            total_results=0,
            current_page=page_number,
            page_size=page_size,
            total_pages=0,
            query_used=log_params,
            error_message=f"An unexpected error occurred: {str(e)}"
        )


@app.tool()
async def search_within_kanun(
    mevzuat_no: str = Field(
        ...,
        description="The legislation number to search within (e.g., '6362', '5237')"
    ),
    keyword: str = Field(
        ...,
        description='Search query. For keyword mode: supports AND/OR/NOT operators (uppercase). For semantic mode: use natural language.'
    ),
    mevzuat_tertip: str = Field(
        "5",
        description="Legislation series from search results (e.g., '3', '5')"
    ),
    case_sensitive: bool = Field(
        False,
        description="Whether to match case when searching (default: False). Only used in keyword mode."
    ),
    max_results: int = Field(
        25,
        ge=1,
        le=50,
        description="Maximum number of matching articles to return (1-50, default: 25)"
    ),
    semantic: bool = Field(
        False,
        description="True: semantic search (natural language query, requires OPENROUTER_API_KEY). False: keyword search (Boolean operators AND/OR/NOT)."
    )
) -> str:
    """
    Search within a specific law's articles using keyword or semantic search.

    Modes:
    - semantic=False (default): Keyword search with Boolean operators (AND/OR/NOT, uppercase required)
    - semantic=True: Natural language semantic search using AI embeddings (requires OPENROUTER_API_KEY)

    Keyword examples: "yatırımcı AND tazmin", '"mali sıkıntı"', "vergi OR ücret"
    Semantic examples: "yatırımcının zararının tazmini", "sermaye piyasası düzenlemeleri"
    """
    logger.info(f"Tool 'search_within_kanun' called: {mevzuat_no}, keyword: '{keyword}', semantic: {semantic}")

    try:
        if semantic:
            if not SEMANTIC_SEARCH_AVAILABLE:
                return "Error: Semantic search requires OPENROUTER_API_KEY environment variable."
            return await _semantic_search_within(
                mevzuat_no=mevzuat_no, query=keyword, mevzuat_tur=1,
                mevzuat_tertip=mevzuat_tertip, max_results=max_results
            )

        # Keyword search
        content_result = await mevzuat_client.get_content(
            mevzuat_no=mevzuat_no, mevzuat_tur=1, mevzuat_tertip=mevzuat_tertip
        )
        if content_result.error_message:
            return f"Error fetching legislation content: {content_result.error_message}"

        matches = search_articles_by_keyword(
            markdown_content=content_result.markdown_content,
            keyword=keyword, case_sensitive=case_sensitive, max_results=max_results
        )
        result = ArticleSearchResult(
            mevzuat_no=mevzuat_no, mevzuat_tur=1, keyword=keyword,
            total_matches=len(matches), matching_articles=matches
        )
        if len(matches) == 0:
            return f"No articles found containing '{keyword}' in Kanun {mevzuat_no}"
        return format_search_results(result)

    except Exception as e:
        logger.exception(f"Error in tool 'search_within_kanun' for {mevzuat_no}")
        return f"An unexpected error occurred: {str(e)}"


@app.tool()
async def search_teblig(
    aranacak_ifade: str = Field(
        ...,
        description='Search query with optional Boolean operators: simple word (vergi), AND (vergi AND muafiyet), OR (muafiyet OR istisna), NOT (vergi NOT gelir), + for required (+term), grouping with (), exact phrase with quotes ("katma değer vergisi")'
    ),
    tam_cumle: bool = Field(
        False,
        description="Exact phrase match (true) or any word match (false, default). Set to true when searching for exact phrases."
    ),
    baslangic_tarihi: Optional[str] = Field(
        None,
        description="Start year for filtering results (format: YYYY, e.g., '2020')"
    ),
    bitis_tarihi: Optional[str] = Field(
        None,
        description="End year for filtering results (format: YYYY, e.g., '2024')"
    ),
    page_number: int = Field(
        1,
        ge=1,
        description="Page number for pagination (starts at 1)"
    ),
    aranacak_yer: int = Field(
        3,
        ge=1,
        le=3,
        description="Where to search: 1=Title only, 2=Content only, 3=Both title and content (default)"
    ),
    page_size: int = Field(
        25,
        ge=1,
        le=100,
        description="Number of results per page (1-100)"
    )
) -> MevzuatSearchResultNew:
    """
    Search for Turkish communiqués (Tebliğ) in both titles and content on mevzuat.gov.tr.

    IMPORTANT: Search is keyword-based, NOT by number. Use descriptive Turkish terms.
    Communiqués are regulatory documents issued by various government institutions.

    Query Syntax: Simple keyword, AND, OR, NOT, +required, (grouping), "exact phrase"

    Example queries:
    - "katma değer vergisi" - Find VAT-related communiqués
    - "muafiyet OR istisna" - Communiqués about exemptions
    - "gümrük" - Customs-related communiqués
    - "ithalat" or "ihracat" - Import/export communiqués

    Returns: Communiqué number, title, publication date, Official Gazette info.
    """
    search_req = MevzuatSearchRequestNew(
        mevzuat_tur="Tebliğ",
        aranacak_ifade=aranacak_ifade,
        aranacak_yer=aranacak_yer,
        tam_cumle=tam_cumle,
        mevzuat_no=None,
        baslangic_tarihi=baslangic_tarihi,
        bitis_tarihi=bitis_tarihi,
        page_number=page_number,
        page_size=page_size
    )

    log_params = search_req.model_dump(exclude_defaults=True)
    logger.info(f"Tool 'search_teblig' called with parameters: {log_params}")

    try:
        result = await mevzuat_client.search_documents(search_req)

        if not result.documents and not result.error_message:
            result.error_message = "No communiqués found matching the specified criteria."

        return result

    except Exception as e:
        logger.exception("Error in tool 'search_teblig'")
        return MevzuatSearchResultNew(
            documents=[],
            total_results=0,
            current_page=page_number,
            page_size=page_size,
            total_pages=0,
            query_used=log_params,
            error_message=f"An unexpected error occurred: {str(e)}"
        )


@app.tool()
async def get_teblig_content(
    mevzuat_no: str = Field(
        ...,
        description="The communiqué number from search results (e.g., '42331')"
    ),
    mevzuat_tertip: str = Field(
        "5",
        description="Communiqué series from search results (e.g., '5')"
    )
) -> MevzuatArticleContent:
    """
    Retrieve the full content of a Turkish communiqué (Tebliğ) in Markdown format.

    This tool fetches the complete text of a communiqué identified by its number.
    Use 'search_teblig' first to find the communiqué number and series.

    Returns:
    - Full communiqué content formatted as Markdown
    - Ready for analysis, summarization, or question answering

    Example usage:
    1. Search for communiqués: search_teblig(aranacak_ifade="katma değer vergisi")
    2. Get full content: get_teblig_content(mevzuat_no="42331", mevzuat_tertip="5")
    """
    logger.info(f"Tool 'get_teblig_content' called: {mevzuat_no}, tertip: {mevzuat_tertip}")

    try:
        result = await mevzuat_client.get_content(
            mevzuat_no=mevzuat_no,
            mevzuat_tur=9,  # Tebliğ
            mevzuat_tertip=mevzuat_tertip
        )

        if result.error_message:
            logger.warning(f"Error fetching communiqué content: {result.error_message}")

        return result

    except Exception as e:
        logger.exception(f"Error in tool 'get_teblig_content' for {mevzuat_no}")
        return MevzuatArticleContent(
            madde_id=mevzuat_no,
            mevzuat_id=mevzuat_no,
            markdown_content="",
            error_message=f"An unexpected error occurred: {str(e)}"
        )


@app.tool()
async def search_cbk(
    aranacak_ifade: str = Field(
        ...,
        description='Search query with optional Boolean operators: simple word (organize), AND (organize AND suç), OR (suç OR ceza), NOT (organize NOT terör), + for required (+term), grouping with (), exact phrase with quotes ("organize suç")'
    ),
    tam_cumle: bool = Field(
        False,
        description="Exact phrase match (true) or any word match (false, default). Set to true when searching for exact phrases."
    ),
    baslangic_tarihi: Optional[str] = Field(
        None,
        description="Start year for filtering results (format: YYYY, e.g., '2018')"
    ),
    bitis_tarihi: Optional[str] = Field(
        None,
        description="End year for filtering results (format: YYYY, e.g., '2024')"
    ),
    page_number: int = Field(
        1,
        ge=1,
        description="Page number for pagination (starts at 1)"
    ),
    aranacak_yer: int = Field(
        3,
        ge=1,
        le=3,
        description="Where to search: 1=Title only, 2=Content only, 3=Both title and content (default)"
    ),
    page_size: int = Field(
        25,
        ge=1,
        le=100,
        description="Number of results per page (1-100)"
    )
) -> MevzuatSearchResultNew:
    """
    Search for Turkish Presidential Decrees (Cumhurbaşkanlığı Kararnamesi) in both titles and content.

    IMPORTANT: Search is keyword-based, NOT by decree number. Use descriptive Turkish terms.
    Presidential Decrees are executive orders issued by the President of Turkey (post-2017).

    Query Syntax: Simple keyword, AND, OR, NOT, +required, (grouping), "exact phrase"

    Example queries:
    - "organize suç" - Find decrees about organized crime
    - "kamu OR devlet" - Decrees about public or state matters
    - "bakanlık AND teşkilat" - Ministry organization decrees

    Returns: Decree number, title, publication date, Official Gazette info.
    """
    search_req = MevzuatSearchRequestNew(
        mevzuat_tur="Cumhurbaşkanlığı Kararnamesi",
        aranacak_ifade=aranacak_ifade,
        aranacak_yer=aranacak_yer,
        tam_cumle=tam_cumle,
        mevzuat_no=None,
        baslangic_tarihi=baslangic_tarihi,
        bitis_tarihi=bitis_tarihi,
        page_number=page_number,
        page_size=page_size
    )

    log_params = search_req.model_dump(exclude_defaults=True)
    logger.info(f"Tool 'search_cbk' called with parameters: {log_params}")

    try:
        result = await mevzuat_client.search_documents(search_req)

        if not result.documents and not result.error_message:
            result.error_message = "No Presidential Decrees found matching the specified criteria."

        return result

    except Exception as e:
        logger.exception("Error in tool 'search_cbk'")
        return MevzuatSearchResultNew(
            documents=[],
            total_results=0,
            current_page=page_number,
            page_size=page_size,
            total_pages=0,
            query_used=log_params,
            error_message=f"An unexpected error occurred: {str(e)}"
        )


@app.tool()
async def search_within_cbk(
    mevzuat_no: str = Field(
        ...,
        description="The Presidential Decree number to search within (e.g., '1', '32')"
    ),
    keyword: str = Field(
        ...,
        description='Search query. For keyword mode: supports AND/OR/NOT operators (uppercase). For semantic mode: use natural language.'
    ),
    mevzuat_tertip: str = Field(
        "5",
        description="Decree series from search results (e.g., '5')"
    ),
    case_sensitive: bool = Field(
        False,
        description="Whether to match case when searching (default: False). Only used in keyword mode."
    ),
    max_results: int = Field(
        25,
        ge=1,
        le=50,
        description="Maximum number of matching articles to return (1-50, default: 25)"
    ),
    semantic: bool = Field(
        False,
        description="True: semantic search (natural language query, requires OPENROUTER_API_KEY). False: keyword search (Boolean operators AND/OR/NOT)."
    )
) -> str:
    """
    Search within a specific Presidential Decree's articles using keyword or semantic search.

    Modes:
    - semantic=False (default): Keyword search with Boolean operators (AND/OR/NOT, uppercase required)
    - semantic=True: Natural language semantic search using AI embeddings (requires OPENROUTER_API_KEY)

    Keyword examples: "organize AND suç", '"organize suç"', "devlet OR kamu"
    Semantic examples: "organize suç örgütleri ile mücadele", "bakanlık teşkilat yapısı"
    """
    logger.info(f"Tool 'search_within_cbk' called: {mevzuat_no}, keyword: '{keyword}', semantic: {semantic}")

    try:
        if semantic:
            if not SEMANTIC_SEARCH_AVAILABLE:
                return "Error: Semantic search requires OPENROUTER_API_KEY environment variable."
            return await _semantic_search_within(
                mevzuat_no=mevzuat_no, query=keyword, mevzuat_tur=19,
                mevzuat_tertip=mevzuat_tertip, max_results=max_results
            )

        content_result = await mevzuat_client.get_content(
            mevzuat_no=mevzuat_no, mevzuat_tur=19, mevzuat_tertip=mevzuat_tertip
        )
        if content_result.error_message:
            return f"Error fetching decree content: {content_result.error_message}"

        matches = search_articles_by_keyword(
            markdown_content=content_result.markdown_content,
            keyword=keyword, case_sensitive=case_sensitive, max_results=max_results
        )
        result = ArticleSearchResult(
            mevzuat_no=mevzuat_no, mevzuat_tur=19, keyword=keyword,
            total_matches=len(matches), matching_articles=matches
        )
        if len(matches) == 0:
            return f"No articles found containing '{keyword}' in CBK {mevzuat_no}"
        return format_search_results(result)

    except Exception as e:
        logger.exception(f"Error in tool 'search_within_cbk' for {mevzuat_no}")
        return f"An unexpected error occurred: {str(e)}"


@app.tool()
async def search_cbyonetmelik(
    aranacak_ifade: Optional[str] = Field(
        None,
        description='Search query with optional Boolean operators: simple word (yatırımcı), AND (yatırımcı AND tazmin), OR (vergi OR ücret), NOT (yatırımcı NOT kurum), + for required (+term), grouping with (), exact phrase with quotes ("mali sıkıntı"). Leave empty to list all regulations.'
    ),
    tam_cumle: bool = Field(
        False,
        description="Exact phrase match (true) or any word match (false, default). Set to true when searching for exact phrases."
    ),
    baslangic_tarihi: Optional[str] = Field(
        None,
        description="Start year for filtering results (format: YYYY, e.g., '2018')"
    ),
    bitis_tarihi: Optional[str] = Field(
        None,
        description="End year for filtering results (format: YYYY, e.g., '2024')"
    ),
    page_number: int = Field(
        1,
        ge=1,
        description="Page number (1-indexed)"
    ),
    aranacak_yer: int = Field(
        3,
        ge=1,
        le=3,
        description="Where to search: 1=Title only, 2=Content only, 3=Both title and content (default)"
    ),
    page_size: int = Field(
        25,
        ge=1,
        le=100,
        description="Number of results per page (max 100)"
    )
) -> MevzuatSearchResultNew:
    """
    Search for Turkish Presidential Regulations (Cumhurbaşkanlığı Yönetmeliği / CB Yönetmeliği) in both titles and content.

    IMPORTANT: Search is keyword-based, NOT by number. Use descriptive Turkish terms.
    These are regulations issued directly by the Presidency. For institutional regulations (Kurum Yönetmeliği),
    use 'search_kurum_yonetmelik' instead.

    Query Syntax: Simple keyword, AND, OR, NOT, +required, (grouping), "exact phrase"

    Example queries:
    - "ihale" - Procurement regulations
    - "taşınır AND mal" - Movable property regulations
    - "kamu ihale" - Public procurement regulations
    - Leave empty to list all, use date filters for period

    Returns: Regulation number, title, publication date, Official Gazette info.
    """
    logger.info(f"Tool 'search_cbyonetmelik' called with query: {aranacak_ifade}")

    try:
        search_req = MevzuatSearchRequestNew(
            mevzuat_tur="CB Yönetmeliği",
            aranacak_ifade=aranacak_ifade or "",
            aranacak_yer=aranacak_yer,
            tam_cumle=tam_cumle,
            mevzuat_no=None,
            baslangic_tarihi=baslangic_tarihi,
            bitis_tarihi=bitis_tarihi,
            page_number=page_number,
            page_size=page_size
        )

        result = await mevzuat_client.search_documents(search_req)
        logger.info(f"Search completed: {result.total_results} total results")
        return result

    except Exception as e:
        logger.exception("Error in tool 'search_cbyonetmelik'")
        return MevzuatSearchResultNew(
            documents=[],
            total_results=0,
            current_page=page_number,
            page_size=page_size,
            total_pages=0,
            query_used={"aranacak_ifade": aranacak_ifade},
            error_message=f"An unexpected error occurred: {str(e)}"
        )


@app.tool()
async def search_within_cbyonetmelik(
    mevzuat_no: str = Field(
        ...,
        description="The Presidential Regulation number to search within (e.g., '10453', '9014')"
    ),
    keyword: str = Field(
        ...,
        description='Search query. For keyword mode: supports AND/OR/NOT operators (uppercase). For semantic mode: use natural language.'
    ),
    mevzuat_tertip: str = Field(
        "5",
        description="Regulation series from search results (typically '5')"
    ),
    case_sensitive: bool = Field(
        False,
        description="Whether to match case (false = case-insensitive, default). Only used in keyword mode."
    ),
    max_results: int = Field(
        25,
        ge=1,
        le=50,
        description="Maximum number of matching articles to return (1-50)"
    ),
    semantic: bool = Field(
        False,
        description="True: semantic search (natural language query, requires OPENROUTER_API_KEY). False: keyword search (Boolean operators AND/OR/NOT)."
    )
) -> str:
    """
    Search within a specific Presidential Regulation's articles using keyword or semantic search.

    Modes:
    - semantic=False (default): Keyword search with Boolean operators (AND/OR/NOT, uppercase required)
    - semantic=True: Natural language semantic search using AI embeddings (requires OPENROUTER_API_KEY)

    Keyword examples: "taşınır AND mal", '"ihale kanunu"', "kamu OR devlet"
    Semantic examples: "taşınır mal yönetimi ve zimmet işlemleri", "kamu ihale süreçleri"
    """
    logger.info(f"Tool 'search_within_cbyonetmelik' called: {mevzuat_no}, keyword: '{keyword}', semantic: {semantic}")

    try:
        if semantic:
            if not SEMANTIC_SEARCH_AVAILABLE:
                return "Error: Semantic search requires OPENROUTER_API_KEY environment variable."
            return await _semantic_search_within(
                mevzuat_no=mevzuat_no, query=keyword, mevzuat_tur=21,
                mevzuat_tertip=mevzuat_tertip, max_results=max_results
            )

        content_result = await mevzuat_client.get_content(
            mevzuat_no=mevzuat_no, mevzuat_tur=21, mevzuat_tertip=mevzuat_tertip
        )
        if content_result.error_message:
            return f"Error: {content_result.error_message}"
        if not content_result.markdown_content:
            return f"Error: No content found for regulation {mevzuat_no}"

        matches = search_articles_by_keyword(
            markdown_content=content_result.markdown_content,
            keyword=keyword, case_sensitive=case_sensitive, max_results=max_results
        )
        if not matches:
            return f"No articles found matching '{keyword}' in regulation {mevzuat_no}"

        result = ArticleSearchResult(
            mevzuat_no=mevzuat_no, mevzuat_tur=21, keyword=keyword,
            total_matches=len(matches), matching_articles=matches
        )
        return format_search_results(result)

    except Exception as e:
        logger.exception(f"Error in tool 'search_within_cbyonetmelik' for regulation {mevzuat_no}")
        return f"Error: An unexpected error occurred: {str(e)}"


@app.tool()
async def search_cbbaskankarar(
    aranacak_ifade: Optional[str] = Field(
        None,
        description='Search query with optional Boolean operators: simple word (organize), AND (organize AND suç), OR (suç OR ceza), NOT (organize NOT terör), + for required (+term), grouping with (), exact phrase with quotes ("organize suç"). Leave empty to list all decrees.'
    ),
    tam_cumle: bool = Field(
        False,
        description="Exact phrase match (true) or any word match (false, default). Set to true when searching for exact phrases."
    ),
    baslangic_tarihi: Optional[str] = Field(
        None,
        description="Start year for filtering results (format: YYYY, e.g., '2018')"
    ),
    bitis_tarihi: Optional[str] = Field(
        None,
        description="End year for filtering results (format: YYYY, e.g., '2024')"
    ),
    page_number: int = Field(
        1,
        ge=1,
        description="Page number for pagination (starts at 1)"
    ),
    aranacak_yer: int = Field(
        3,
        ge=1,
        le=3,
        description="Where to search: 1=Title only, 2=Content only, 3=Both title and content (default)"
    ),
    page_size: int = Field(
        25,
        ge=1,
        le=100,
        description="Number of results per page (1-100)"
    )
) -> MevzuatSearchResultNew:
    """
    Search for Turkish Presidential Decisions (Cumhurbaşkanı Kararı) in both titles and content.

    IMPORTANT: Search is keyword-based, NOT by decision number. Use descriptive Turkish terms.
    Presidential Decisions are executive decisions (different from Presidential Decrees/Kararnamesi).
    Note: Bakanlar Kurulu Kararı (BKK) is NOT a separate type - older BKKs may appear here or in Kanun.

    Query Syntax: Simple keyword, AND, OR, NOT, +required, (grouping), "exact phrase"

    Example queries:
    - "atama" - Find decisions about appointments
    - "ihracat AND rejim" - Export regime decisions
    - "vergi" or "gümrük" - Tax or customs decisions
    - Leave empty with dates to list all decisions from a period

    Returns: Decision number, title, publication date, Official Gazette info. PDF format only.
    """
    search_req = MevzuatSearchRequestNew(
        mevzuat_tur="Cumhurbaşkanı Kararı",
        aranacak_ifade=aranacak_ifade or "",
        aranacak_yer=aranacak_yer,
        tam_cumle=tam_cumle,
        mevzuat_no=None,
        baslangic_tarihi=baslangic_tarihi,
        bitis_tarihi=bitis_tarihi,
        page_number=page_number,
        page_size=page_size
    )

    log_params = search_req.model_dump(exclude_defaults=True)
    logger.info(f"Tool 'search_cbbaskankarar' called with parameters: {log_params}")

    try:
        result = await mevzuat_client.search_documents(search_req)

        if not result.documents and not result.error_message:
            result.error_message = "No Presidential Decisions found matching the specified criteria."

        return result

    except Exception as e:
        logger.exception("Error in tool 'search_cbbaskankarar'")
        return MevzuatSearchResultNew(
            documents=[],
            total_results=0,
            current_page=page_number,
            page_size=page_size,
            total_pages=0,
            query_used=log_params,
            error_message=f"An unexpected error occurred: {str(e)}"
        )


@app.tool()
async def get_cbbaskankarar_content(
    mevzuat_no: str = Field(
        ...,
        description="The Presidential Decision number from search results (e.g., '10452')"
    ),
    mevzuat_tertip: str = Field(
        "5",
        description="Decision series from search results (e.g., '5')"
    )
) -> MevzuatArticleContent:
    """
    Retrieve the full content of a Turkish Presidential Decision (Cumhurbaşkanı Kararı) in Markdown format.

    This tool fetches the PDF document and converts it to Markdown.
    Presidential Decisions are available only as PDF files.
    Use 'search_cbbaskankarar' first to find the decision number and series.

    Returns:
    - Full decision content formatted as Markdown (converted from PDF)
    - Ready for analysis, summarization, or question answering

    Example usage:
    1. Search for decisions: search_cbbaskankarar(baslangic_tarihi="2023", bitis_tarihi="2024")
    2. Get full content: get_cbbaskankarar_content(mevzuat_no="10452", mevzuat_tertip="5")
    """
    logger.info(f"Tool 'get_cbbaskankarar_content' called: {mevzuat_no}, tertip: {mevzuat_tertip}")

    try:
        result = await mevzuat_client.get_content(
            mevzuat_no=mevzuat_no,
            mevzuat_tur=20,  # Cumhurbaşkanı Kararı
            mevzuat_tertip=mevzuat_tertip
        )

        if result.error_message:
            logger.warning(f"Error fetching decision content: {result.error_message}")

        return result

    except Exception as e:
        logger.exception(f"Error in tool 'get_cbbaskankarar_content' for {mevzuat_no}")
        return MevzuatArticleContent(
            madde_id=mevzuat_no,
            mevzuat_id=mevzuat_no,
            markdown_content="",
            error_message=f"An unexpected error occurred: {str(e)}"
        )


@app.tool()
async def search_cbgenelge(
    aranacak_ifade: Optional[str] = Field(
        None,
        description='Search query with optional Boolean operators: simple word (organize), AND (organize AND suç), OR (suç OR ceza), NOT (organize NOT terör), + for required (+term), grouping with (), exact phrase with quotes ("organize suç"). Leave empty to list all circulars.'
    ),
    tam_cumle: bool = Field(
        False,
        description="Exact phrase match (true) or any word match (false, default). Set to true when searching for exact phrases."
    ),
    baslangic_tarihi: Optional[str] = Field(
        None,
        description="Start year for filtering results (format: YYYY, e.g., '2018')"
    ),
    bitis_tarihi: Optional[str] = Field(
        None,
        description="End year for filtering results (format: YYYY, e.g., '2024')"
    ),
    page_number: int = Field(
        1,
        ge=1,
        description="Page number (1-indexed)"
    ),
    aranacak_yer: int = Field(
        3,
        ge=1,
        le=3,
        description="Where to search: 1=Title only, 2=Content only, 3=Both title and content (default)"
    ),
    page_size: int = Field(
        25,
        ge=1,
        le=100,
        description="Number of results per page (max 100)"
    )
) -> MevzuatSearchResultNew:
    """
    Search for Turkish Presidential Circulars (Cumhurbaşkanlığı Genelgesi / CB Genelgesi) in titles and content.

    IMPORTANT: Search is keyword-based, NOT by circular number. Use descriptive Turkish terms.
    Use 'get_cbgenelge_content' with mevzuat_no and resmi_gazete_tarihi to retrieve full PDF content.

    Query Syntax: Simple keyword, AND, OR, NOT, +required, (grouping), "exact phrase"

    Example queries:
    - "koordinasyon" - Coordination circulars
    - Leave empty with dates to list all circulars from a period

    Returns: Circular number, title, publication date, Official Gazette info. PDF format only.
    """
    logger.info(f"Tool 'search_cbgenelge' called with query: {aranacak_ifade}")

    try:
        search_req = MevzuatSearchRequestNew(
            mevzuat_tur="CB Genelgesi",
            aranacak_ifade=aranacak_ifade or "",
            aranacak_yer=aranacak_yer,
            tam_cumle=tam_cumle,
            mevzuat_no=None,
            baslangic_tarihi=baslangic_tarihi,
            bitis_tarihi=bitis_tarihi,
            page_number=page_number,
            page_size=page_size
        )

        result = await mevzuat_client.search_documents(search_req)
        logger.info(f"Search completed: {result.total_results} total results")
        return result

    except Exception as e:
        logger.exception("Error in tool 'search_cbgenelge'")
        return MevzuatSearchResultNew(
            documents=[],
            total_results=0,
            current_page=page_number,
            page_size=page_size,
            total_pages=0,
            query_used={"aranacak_ifade": aranacak_ifade},
            error_message=f"An unexpected error occurred: {str(e)}"
        )


@app.tool()
async def get_cbgenelge_content(
    mevzuat_no: str = Field(
        ...,
        description="The Presidential Circular number from search results (e.g., '16', '15')"
    ),
    resmi_gazete_tarihi: str = Field(
        ...,
        description="Official Gazette date from search results in DD/MM/YYYY format (e.g., '20/09/2025')"
    ),
    mevzuat_tertip: str = Field(
        "5",
        description="Circular series from search results (e.g., '5')"
    )
) -> MevzuatArticleContent:
    """
    Retrieve the full content of a Turkish Presidential Circular (Cumhurbaşkanlığı Genelgesi) in Markdown format.

    This tool fetches the PDF document and converts it to Markdown.
    Presidential Circulars are available only as PDF files.
    Use 'search_cbgenelge' first to find the circular number and Official Gazette date.

    IMPORTANT: You must provide the 'resmi_gazete_tarihi' (Official Gazette date) from the search results.
    This is required to construct the correct PDF URL.

    Returns:
    - Full circular content formatted as Markdown (converted from PDF)
    - Ready for analysis, summarization, or question answering

    Example usage:
    1. Search for circulars: search_cbgenelge(baslangic_tarihi="2025")
    2. Get full content: get_cbgenelge_content(mevzuat_no="16", resmi_gazete_tarihi="20/09/2025", mevzuat_tertip="5")
    """
    logger.info(f"Tool 'get_cbgenelge_content' called: {mevzuat_no}, RG date: {resmi_gazete_tarihi}, tertip: {mevzuat_tertip}")

    try:
        result = await mevzuat_client.get_content(
            mevzuat_no=mevzuat_no,
            mevzuat_tur=22,  # Cumhurbaşkanlığı Genelgesi
            mevzuat_tertip=mevzuat_tertip,
            resmi_gazete_tarihi=resmi_gazete_tarihi
        )

        if result.error_message:
            logger.warning(f"Error fetching circular content: {result.error_message}")

        return result

    except Exception as e:
        logger.exception(f"Error in tool 'get_cbgenelge_content' for {mevzuat_no}")
        return MevzuatArticleContent(
            madde_id=mevzuat_no,
            mevzuat_id=mevzuat_no,
            markdown_content="",
            error_message=f"An unexpected error occurred: {str(e)}"
        )


# ============================================================================
# KHK (Kanun Hükmünde Kararname) Tools
# ============================================================================

@app.tool()
async def search_khk(
    aranacak_ifade: Optional[str] = Field(
        None,
        description='Search query with Boolean operators and wildcards. Examples: "değişiklik" (simple), "sağlık AND düzenleme" (AND), "bakanlık OR kurum" (OR), "kanun NOT yürürlük" (NOT), "değişiklik*" (wildcard), "güvenlik sistemi" (exact phrase with quotes). Leave empty to list all KHKs in date range.'
    ),
    tam_cumle: bool = Field(
        False,
        description="If True, searches for exact phrase match. If False (default), searches for any word match with Boolean operators."
    ),
    baslangic_tarihi: Optional[str] = Field(
        None,
        description="Start year for filtering results (format: YYYY, e.g., '2010'). Use with bitis_tarihi to define a date range."
    ),
    bitis_tarihi: Optional[str] = Field(
        None,
        description="End year for filtering results (format: YYYY, e.g., '2018'). Use with baslangic_tarihi to define a date range."
    ),
    page_number: int = Field(
        1,
        ge=1,
        description="Page number of results to retrieve (starts from 1)"
    ),
    aranacak_yer: int = Field(
        3,
        ge=1,
        le=3,
        description="Where to search: 1=Title only, 2=Content only, 3=Both title and content (default)"
    ),
    page_size: int = Field(
        25,
        ge=1,
        le=100,
        description="Number of results per page (1-100, default: 25)"
    )
) -> MevzuatSearchResultNew:
    """
    Search for Turkish Decree Laws (Kanun Hükmünde Kararname / KHK) in titles and content.

    IMPORTANT: Search is keyword-based, NOT by KHK number. Use descriptive Turkish terms.
    KHKs were abolished after the 2017 constitutional referendum (last issued 2018).
    Previously enacted KHKs remain in force unless repealed.

    Query Syntax: Simple keyword, AND, OR, NOT, +required, (grouping), "exact phrase"

    Example queries:
    - "sağlık AND düzenleme" - Health-related KHKs
    - "anayasa" - Constitutional KHKs
    - Leave empty with dates (e.g., 2010-2018) to list all KHKs from a period

    Returns: KHK number, title, dates, Official Gazette info.
    """
    logger.info(f"Tool 'search_khk' called: '{aranacak_ifade}', dates: {baslangic_tarihi}-{bitis_tarihi}")

    try:
        search_req = MevzuatSearchRequestNew(
            mevzuat_tur="KHK",
            aranacak_ifade=aranacak_ifade or "",
            aranacak_yer=aranacak_yer,
            tam_cumle=tam_cumle,
            mevzuat_no=None,
            baslangic_tarihi=baslangic_tarihi,
            bitis_tarihi=bitis_tarihi,
            page_number=page_number,
            page_size=page_size
        )

        result = await mevzuat_client.search_documents(search_req)
        logger.info(f"Found {result.total_results} KHKs")
        return result

    except Exception as e:
        logger.exception("Error in tool 'search_khk'")
        return MevzuatSearchResultNew(
            documents=[],
            total_results=0,
            current_page=page_number,
            page_size=page_size,
            total_pages=0,
            query_used={"error": str(e)},
            error_message=f"An unexpected error occurred: {str(e)}"
        )


@app.tool()
async def search_within_khk(
    mevzuat_no: str = Field(
        ...,
        description="The KHK number to search within (e.g., '703', '700', '665')"
    ),
    keyword: str = Field(
        ...,
        description='Search query. For keyword mode: supports AND/OR/NOT operators (uppercase). For semantic mode: use natural language.'
    ),
    mevzuat_tertip: str = Field(
        "5",
        description="KHK series from search results (e.g., '5')"
    ),
    case_sensitive: bool = Field(
        False,
        description="Whether to match case when searching (default: False). Only used in keyword mode."
    ),
    max_results: int = Field(
        25,
        ge=1,
        le=50,
        description="Maximum number of matching articles to return (1-50, default: 25)"
    ),
    semantic: bool = Field(
        False,
        description="True: semantic search (natural language query, requires OPENROUTER_API_KEY). False: keyword search (Boolean operators AND/OR/NOT)."
    )
) -> str:
    """
    Search within a specific Decree Law's (KHK) articles using keyword or semantic search.

    Modes:
    - semantic=False (default): Keyword search with Boolean operators (AND/OR/NOT, uppercase required)
    - semantic=True: Natural language semantic search using AI embeddings (requires OPENROUTER_API_KEY)

    Keyword examples: "kanun AND değişiklik", '"kanun hükmünde"', "bakanlık OR kurum"
    Semantic examples: "sağlık alanında yapılan düzenlemeler", "anayasa değişikliği"
    """
    logger.info(f"Tool 'search_within_khk' called: {mevzuat_no}, keyword: '{keyword}', semantic: {semantic}")

    try:
        if semantic:
            if not SEMANTIC_SEARCH_AVAILABLE:
                return "Error: Semantic search requires OPENROUTER_API_KEY environment variable."
            return await _semantic_search_within(
                mevzuat_no=mevzuat_no, query=keyword, mevzuat_tur=4,
                mevzuat_tertip=mevzuat_tertip, max_results=max_results
            )

        content_result = await mevzuat_client.get_content(
            mevzuat_no=mevzuat_no, mevzuat_tur=4, mevzuat_tertip=mevzuat_tertip
        )
        if content_result.error_message:
            return f"Error fetching KHK content: {content_result.error_message}"

        matches = search_articles_by_keyword(
            markdown_content=content_result.markdown_content,
            keyword=keyword, case_sensitive=case_sensitive, max_results=max_results
        )
        result = ArticleSearchResult(
            mevzuat_no=mevzuat_no, mevzuat_tur=4, keyword=keyword,
            total_matches=len(matches), matching_articles=matches
        )
        if len(matches) == 0:
            return f"No articles found containing '{keyword}' in KHK {mevzuat_no}"
        return format_search_results(result)

    except Exception as e:
        logger.exception(f"Error in tool 'search_within_khk' for {mevzuat_no}")
        return f"An unexpected error occurred while searching KHK {mevzuat_no}: {str(e)}"


# ============================================================================
# Tüzük (Statute/Regulation) Tools
# ============================================================================

@app.tool()
async def search_tuzuk(
    aranacak_ifade: Optional[str] = Field(
        None,
        description='Search query with Boolean operators and wildcards. Examples: "tapu" (simple), "sicil AND kayıt" (AND), "tescil OR ilan" (OR), "vakıf NOT kurul" (NOT), "tescil*" (wildcard), "medeni kanun" (exact phrase with quotes). Leave empty to list all statutes in date range.'
    ),
    tam_cumle: bool = Field(
        False,
        description="If True, searches for exact phrase match. If False (default), searches for any word match with Boolean operators."
    ),
    baslangic_tarihi: Optional[str] = Field(
        None,
        description="Start year for filtering results (format: YYYY, e.g., '2008'). Use with bitis_tarihi to define a date range."
    ),
    bitis_tarihi: Optional[str] = Field(
        None,
        description="End year for filtering results (format: YYYY, e.g., '2013'). Use with baslangic_tarihi to define a date range."
    ),
    page_number: int = Field(
        1,
        ge=1,
        description="Page number of results to retrieve (starts from 1)"
    ),
    aranacak_yer: int = Field(
        3,
        ge=1,
        le=3,
        description="Where to search: 1=Title only, 2=Content only, 3=Both title and content (default)"
    ),
    page_size: int = Field(
        25,
        ge=1,
        le=100,
        description="Number of results per page (1-100, default: 25)"
    )
) -> MevzuatSearchResultNew:
    """
    Search for Turkish Statutes/Regulations (Tüzük) in titles and content.

    IMPORTANT: Search is keyword-based, NOT by statute number. Use descriptive Turkish terms.
    Tüzük are regulatory statutes that implement and detail the provisions of laws.

    Query Syntax: Simple keyword, AND, OR, NOT, +required, (grouping), "exact phrase"

    Example queries:
    - "tapu" - Land registry related statutes
    - "vakıf AND tescil" - Foundation registration statutes
    - Leave empty with dates to list all statutes from a period

    Returns: Statute number, title, dates, Official Gazette info.
    """
    logger.info(f"Tool 'search_tuzuk' called: '{aranacak_ifade}', dates: {baslangic_tarihi}-{bitis_tarihi}")

    try:
        search_req = MevzuatSearchRequestNew(
            mevzuat_tur="Tuzuk",
            aranacak_ifade=aranacak_ifade or "",
            aranacak_yer=aranacak_yer,
            tam_cumle=tam_cumle,
            mevzuat_no=None,
            baslangic_tarihi=baslangic_tarihi,
            bitis_tarihi=bitis_tarihi,
            page_number=page_number,
            page_size=page_size
        )

        result = await mevzuat_client.search_documents(search_req)
        logger.info(f"Found {result.total_results} statutes")
        return result

    except Exception as e:
        logger.exception("Error in tool 'search_tuzuk'")
        return MevzuatSearchResultNew(
            documents=[],
            total_results=0,
            current_page=page_number,
            page_size=page_size,
            total_pages=0,
            query_used={"error": str(e)},
            error_message=f"An unexpected error occurred: {str(e)}"
        )


@app.tool()
async def search_within_tuzuk(
    mevzuat_no: str = Field(
        ...,
        description="The statute number to search within (e.g., '20135150', '20134513', '200814001')"
    ),
    keyword: str = Field(
        ...,
        description='Search query. For keyword mode: supports AND/OR/NOT operators (uppercase). For semantic mode: use natural language.'
    ),
    mevzuat_tertip: str = Field(
        "5",
        description="Statute series from search results (e.g., '5')"
    ),
    case_sensitive: bool = Field(
        False,
        description="Whether to match case when searching (default: False). Only used in keyword mode."
    ),
    max_results: int = Field(
        25,
        ge=1,
        le=50,
        description="Maximum number of matching articles to return (1-50, default: 25)"
    ),
    semantic: bool = Field(
        False,
        description="True: semantic search (natural language query, requires OPENROUTER_API_KEY). False: keyword search (Boolean operators AND/OR/NOT)."
    )
) -> str:
    """
    Search within a specific Statute's (Tüzük) articles using keyword or semantic search.

    Modes:
    - semantic=False (default): Keyword search with Boolean operators (AND/OR/NOT, uppercase required)
    - semantic=True: Natural language semantic search using AI embeddings (requires OPENROUTER_API_KEY)

    Keyword examples: "tapu AND sicil", '"sicil kayıt"', "tescil OR ilan"
    Semantic examples: "tapu sicil kayıt işlemleri", "vakıf tescil süreci"
    """
    logger.info(f"Tool 'search_within_tuzuk' called: {mevzuat_no}, keyword: '{keyword}', semantic: {semantic}")

    try:
        if semantic:
            if not SEMANTIC_SEARCH_AVAILABLE:
                return "Error: Semantic search requires OPENROUTER_API_KEY environment variable."
            return await _semantic_search_within(
                mevzuat_no=mevzuat_no, query=keyword, mevzuat_tur=2,
                mevzuat_tertip=mevzuat_tertip, max_results=max_results
            )

        content_result = await mevzuat_client.get_content(
            mevzuat_no=mevzuat_no, mevzuat_tur=2, mevzuat_tertip=mevzuat_tertip
        )
        if content_result.error_message:
            return f"Error fetching statute content: {content_result.error_message}"

        matches = search_articles_by_keyword(
            markdown_content=content_result.markdown_content,
            keyword=keyword, case_sensitive=case_sensitive, max_results=max_results
        )
        result = ArticleSearchResult(
            mevzuat_no=mevzuat_no, mevzuat_tur=2, keyword=keyword,
            total_matches=len(matches), matching_articles=matches
        )
        if len(matches) == 0:
            return f"No articles found containing '{keyword}' in Tüzük {mevzuat_no}"
        return format_search_results(result)

    except Exception as e:
        logger.exception(f"Error in tool 'search_within_tuzuk' for {mevzuat_no}")
        return f"An unexpected error occurred while searching Tüzük {mevzuat_no}: {str(e)}"


# ============================================================================
# Kurum ve Kuruluş Yönetmeliği Tools
# ============================================================================

@app.tool()
async def search_kurum_yonetmelik(
    aranacak_ifade: Optional[str] = Field(
        None,
        description='Search query with Boolean operators and wildcards. Examples: "nükleer" (simple), "ihracat AND kontrol" (AND), "denetim OR teftiş" (OR), "mali NOT ceza" (NOT), "kontrol*" (wildcard), "ithalat ihracat" (exact phrase with quotes). Leave empty to list all regulations in date range.'
    ),
    tam_cumle: bool = Field(
        False,
        description="If True, searches for exact phrase match. If False (default), searches for any word match with Boolean operators."
    ),
    baslangic_tarihi: Optional[str] = Field(
        None,
        description="Start year for filtering results (format: YYYY, e.g., '2020'). Use with bitis_tarihi to define a date range."
    ),
    bitis_tarihi: Optional[str] = Field(
        None,
        description="End year for filtering results (format: YYYY, e.g., '2025'). Use with baslangic_tarihi to define a date range."
    ),
    page_number: int = Field(
        1,
        ge=1,
        description="Page number of results to retrieve (starts from 1)"
    ),
    aranacak_yer: int = Field(
        3,
        ge=1,
        le=3,
        description="Where to search: 1=Title only, 2=Content only, 3=Both title and content (default)"
    ),
    page_size: int = Field(
        25,
        ge=1,
        le=100,
        description="Number of results per page (1-100, default: 25)"
    )
) -> MevzuatSearchResultNew:
    """
    Search for Institutional/Organizational Regulations (Kurum ve Kuruluş Yönetmeliği) in titles and content.

    IMPORTANT: Search is keyword-based, NOT by regulation number. Use descriptive Turkish terms.
    These are regulations issued by governmental institutions (ministries, agencies, boards).
    This is the largest dataset with 8686+ regulations. Use for: Gümrük Yönetmeliği, İthalat/İhracat
    Yönetmeliği, and similar institutional regulations.

    Query Syntax: Simple keyword, AND, OR, NOT, +required, (grouping), "exact phrase"

    Example queries:
    - "gümrük" - Customs regulations (e.g., Gümrük Yönetmeliği)
    - "ithalat" or "ihracat" - Import/export regulations
    - "nükleer" - Nuclear regulations
    - "adalet AND akademi" - Justice academy regulations
    - Leave empty with dates to list all regulations from a period

    Returns: Regulation number, title, dates, Official Gazette info.
    """
    logger.info(f"Tool 'search_kurum_yonetmelik' called: '{aranacak_ifade}', dates: {baslangic_tarihi}-{bitis_tarihi}")

    try:
        search_req = MevzuatSearchRequestNew(
            mevzuat_tur="Kurum Yönetmeliği",
            aranacak_ifade=aranacak_ifade or "",
            aranacak_yer=aranacak_yer,
            tam_cumle=tam_cumle,
            mevzuat_no=None,
            baslangic_tarihi=baslangic_tarihi,
            bitis_tarihi=bitis_tarihi,
            page_number=page_number,
            page_size=page_size
        )

        result = await mevzuat_client.search_documents(search_req)
        logger.info(f"Found {result.total_results} institutional regulations")
        return result

    except Exception as e:
        logger.exception("Error in tool 'search_kurum_yonetmelik'")
        return MevzuatSearchResultNew(
            documents=[],
            total_results=0,
            current_page=page_number,
            page_size=page_size,
            total_pages=0,
            query_used={"error": str(e)},
            error_message=f"An unexpected error occurred: {str(e)}"
        )


@app.tool()
async def search_within_kurum_yonetmelik(
    mevzuat_no: str = Field(
        ...,
        description="The regulation number to search within (e.g., '42641', '42638', '42613')"
    ),
    keyword: str = Field(
        ...,
        description='Search query. For keyword mode: supports AND/OR/NOT operators (uppercase). For semantic mode: use natural language.'
    ),
    mevzuat_tertip: str = Field(
        "5",
        description="Regulation series from search results (e.g., '5')"
    ),
    case_sensitive: bool = Field(
        False,
        description="Whether to match case when searching (default: False). Only used in keyword mode."
    ),
    max_results: int = Field(
        25,
        ge=1,
        le=50,
        description="Maximum number of matching articles to return (1-50, default: 25)"
    ),
    semantic: bool = Field(
        False,
        description="True: semantic search (natural language query, requires OPENROUTER_API_KEY). False: keyword search (Boolean operators AND/OR/NOT)."
    )
) -> str:
    """
    Search within a specific Institutional Regulation's articles using keyword or semantic search.

    Modes:
    - semantic=False (default): Keyword search with Boolean operators (AND/OR/NOT, uppercase required)
    - semantic=True: Natural language semantic search using AI embeddings (requires OPENROUTER_API_KEY)

    Keyword examples: "nükleer AND ihracat", '"ihracat kontrol"', "denetim OR teftiş"
    Semantic examples: "nükleer madde ihracat kontrol düzenlemeleri", "disiplin cezaları"
    """
    logger.info(f"Tool 'search_within_kurum_yonetmelik' called: {mevzuat_no}, keyword: '{keyword}', semantic: {semantic}")

    try:
        if semantic:
            if not SEMANTIC_SEARCH_AVAILABLE:
                return "Error: Semantic search requires OPENROUTER_API_KEY environment variable."
            return await _semantic_search_within(
                mevzuat_no=mevzuat_no, query=keyword, mevzuat_tur=7,
                mevzuat_tertip=mevzuat_tertip, max_results=max_results
            )

        content_result = await mevzuat_client.get_content(
            mevzuat_no=mevzuat_no, mevzuat_tur=7, mevzuat_tertip=mevzuat_tertip
        )
        if content_result.error_message:
            return f"Error fetching regulation content: {content_result.error_message}"

        matches = search_articles_by_keyword(
            markdown_content=content_result.markdown_content,
            keyword=keyword, case_sensitive=case_sensitive, max_results=max_results
        )
        result = ArticleSearchResult(
            mevzuat_no=mevzuat_no, mevzuat_tur=7, keyword=keyword,
            total_matches=len(matches), matching_articles=matches
        )
        if len(matches) == 0:
            return f"No articles found containing '{keyword}' in Kurum Yönetmeliği {mevzuat_no}"
        return format_search_results(result)

    except Exception as e:
        logger.exception(f"Error in tool 'search_within_kurum_yonetmelik' for {mevzuat_no}")
        return f"An unexpected error occurred while searching Kurum Yönetmeliği {mevzuat_no}: {str(e)}"


# ============================================================================
# New search_within tools (Tebliğ, CB Kararı, CB Genelgesi)
# ============================================================================

@app.tool()
async def search_within_teblig(
    mevzuat_no: str = Field(
        ...,
        description="The communiqué number to search within (e.g., '42331')"
    ),
    keyword: str = Field(
        ...,
        description='Search query. For keyword mode: supports AND/OR/NOT operators (uppercase). For semantic mode: use natural language.'
    ),
    mevzuat_tertip: str = Field(
        "5",
        description="Communiqué series from search results (e.g., '5')"
    ),
    case_sensitive: bool = Field(
        False,
        description="Whether to match case when searching (default: False). Only used in keyword mode."
    ),
    max_results: int = Field(
        25,
        ge=1,
        le=50,
        description="Maximum number of matching segments to return (1-50, default: 25)"
    ),
    semantic: bool = Field(
        False,
        description="True: semantic search (natural language query, requires OPENROUTER_API_KEY). False: keyword search (Boolean operators AND/OR/NOT)."
    )
) -> str:
    """
    Search within a specific communiqué's (Tebliğ) content using keyword or semantic search.

    Tries article-based splitting first; if no articles found, falls back to chunk-based search.

    Modes:
    - semantic=False (default): Keyword search with Boolean operators (AND/OR/NOT, uppercase required)
    - semantic=True: Natural language semantic search using AI embeddings (requires OPENROUTER_API_KEY)

    Keyword examples: "vergi AND muafiyet", '"katma değer"', "istisna OR muafiyet"
    Semantic examples: "vergi muafiyeti koşulları", "KDV iade işlemleri"
    """
    logger.info(f"Tool 'search_within_teblig' called: {mevzuat_no}, keyword: '{keyword}', semantic: {semantic}")

    try:
        if semantic:
            if not SEMANTIC_SEARCH_AVAILABLE:
                return "Error: Semantic search requires OPENROUTER_API_KEY environment variable."
            return await _semantic_search_within(
                mevzuat_no=mevzuat_no, query=keyword, mevzuat_tur=9,
                mevzuat_tertip=mevzuat_tertip, max_results=max_results
            )

        # Keyword search
        content_result = await mevzuat_client.get_content(
            mevzuat_no=mevzuat_no, mevzuat_tur=9, mevzuat_tertip=mevzuat_tertip
        )
        if content_result.error_message:
            return f"Error fetching communiqué content: {content_result.error_message}"
        if not content_result.markdown_content:
            return f"Error: No content found for Tebliğ {mevzuat_no}"

        # Try article-based search first
        matches = search_articles_by_keyword(
            markdown_content=content_result.markdown_content,
            keyword=keyword, case_sensitive=case_sensitive, max_results=max_results
        )
        if matches:
            result = ArticleSearchResult(
                mevzuat_no=mevzuat_no, mevzuat_tur=9, keyword=keyword,
                total_matches=len(matches), matching_articles=matches
            )
            return format_search_results(result)

        # Fallback to chunk-based keyword search
        return await _keyword_search_chunks(
            content=content_result.markdown_content, keyword=keyword,
            mevzuat_no=mevzuat_no, mevzuat_tur=9,
            case_sensitive=case_sensitive, max_results=max_results
        )

    except Exception as e:
        logger.exception(f"Error in tool 'search_within_teblig' for {mevzuat_no}")
        return f"An unexpected error occurred: {str(e)}"


@app.tool()
async def search_within_cbbaskankarar(
    mevzuat_no: str = Field(
        ...,
        description="The Presidential Decision number to search within (e.g., '1733', '10452')"
    ),
    keyword: str = Field(
        ...,
        description='Search query. For keyword mode: supports AND/OR/NOT operators (uppercase). For semantic mode: use natural language.'
    ),
    mevzuat_tertip: str = Field(
        "5",
        description="Decision series from search results (e.g., '5')"
    ),
    case_sensitive: bool = Field(
        False,
        description="Whether to match case when searching (default: False). Only used in keyword mode."
    ),
    max_results: int = Field(
        25,
        ge=1,
        le=50,
        description="Maximum number of matching segments to return (1-50, default: 25)"
    ),
    semantic: bool = Field(
        False,
        description="True: semantic search (natural language query, requires OPENROUTER_API_KEY). False: keyword search (Boolean operators AND/OR/NOT)."
    )
) -> str:
    """
    Search within a specific Presidential Decision's (CB Kararı) content using keyword or semantic search.

    Presidential Decisions are PDF-based and use chunk-based splitting (no article structure).

    Modes:
    - semantic=False (default): Keyword search with Boolean operators (AND/OR/NOT, uppercase required)
    - semantic=True: Natural language semantic search using AI embeddings (requires OPENROUTER_API_KEY)

    Keyword examples: "atama AND görev", '"ihracat rejimi"', "vergi OR gümrük"
    Semantic examples: "kamu personeli atama kararları", "ihracat rejimi düzenlemeleri"
    """
    logger.info(f"Tool 'search_within_cbbaskankarar' called: {mevzuat_no}, keyword: '{keyword}', semantic: {semantic}")

    try:
        if semantic:
            if not SEMANTIC_SEARCH_AVAILABLE:
                return "Error: Semantic search requires OPENROUTER_API_KEY environment variable."
            return await _semantic_search_within(
                mevzuat_no=mevzuat_no, query=keyword, mevzuat_tur=20,
                mevzuat_tertip=mevzuat_tertip, max_results=max_results
            )

        # Keyword search (chunk-based)
        content_result = await mevzuat_client.get_content(
            mevzuat_no=mevzuat_no, mevzuat_tur=20, mevzuat_tertip=mevzuat_tertip
        )
        if content_result.error_message:
            return f"Error fetching decision content: {content_result.error_message}"
        if not content_result.markdown_content:
            return f"Error: No content found for CB Kararı {mevzuat_no}"

        return await _keyword_search_chunks(
            content=content_result.markdown_content, keyword=keyword,
            mevzuat_no=mevzuat_no, mevzuat_tur=20,
            case_sensitive=case_sensitive, max_results=max_results
        )

    except Exception as e:
        logger.exception(f"Error in tool 'search_within_cbbaskankarar' for {mevzuat_no}")
        return f"An unexpected error occurred: {str(e)}"


@app.tool()
async def search_within_cbgenelge(
    mevzuat_no: str = Field(
        ...,
        description="The Presidential Circular number to search within (e.g., '16', '15')"
    ),
    keyword: str = Field(
        ...,
        description='Search query. For keyword mode: supports AND/OR/NOT operators (uppercase). For semantic mode: use natural language.'
    ),
    resmi_gazete_tarihi: str = Field(
        ...,
        description="Official Gazette date from search results in DD/MM/YYYY format (e.g., '20/09/2025') - REQUIRED for PDF retrieval"
    ),
    mevzuat_tertip: str = Field(
        "5",
        description="Circular series from search results (e.g., '5')"
    ),
    case_sensitive: bool = Field(
        False,
        description="Whether to match case when searching (default: False). Only used in keyword mode."
    ),
    max_results: int = Field(
        25,
        ge=1,
        le=50,
        description="Maximum number of matching segments to return (1-50, default: 25)"
    ),
    semantic: bool = Field(
        False,
        description="True: semantic search (natural language query, requires OPENROUTER_API_KEY). False: keyword search (Boolean operators AND/OR/NOT)."
    )
) -> str:
    """
    Search within a specific Presidential Circular's (CB Genelgesi) content using keyword or semantic search.

    Presidential Circulars are PDF-based and use chunk-based splitting (no article structure).
    IMPORTANT: resmi_gazete_tarihi is required (from search_cbgenelge results).

    Modes:
    - semantic=False (default): Keyword search with Boolean operators (AND/OR/NOT, uppercase required)
    - semantic=True: Natural language semantic search using AI embeddings (requires OPENROUTER_API_KEY)

    Keyword examples: "koordinasyon AND toplantı", '"kamu yönetimi"'
    Semantic examples: "bakanlıklar arası koordinasyon düzeni", "tasarruf tedbirleri"
    """
    logger.info(f"Tool 'search_within_cbgenelge' called: {mevzuat_no}, keyword: '{keyword}', semantic: {semantic}")

    try:
        if semantic:
            if not SEMANTIC_SEARCH_AVAILABLE:
                return "Error: Semantic search requires OPENROUTER_API_KEY environment variable."
            return await _semantic_search_within(
                mevzuat_no=mevzuat_no, query=keyword, mevzuat_tur=22,
                mevzuat_tertip=mevzuat_tertip, max_results=max_results,
                resmi_gazete_tarihi=resmi_gazete_tarihi
            )

        # Keyword search (chunk-based)
        content_result = await mevzuat_client.get_content(
            mevzuat_no=mevzuat_no, mevzuat_tur=22, mevzuat_tertip=mevzuat_tertip,
            resmi_gazete_tarihi=resmi_gazete_tarihi
        )
        if content_result.error_message:
            return f"Error fetching circular content: {content_result.error_message}"
        if not content_result.markdown_content:
            return f"Error: No content found for CB Genelgesi {mevzuat_no}"

        return await _keyword_search_chunks(
            content=content_result.markdown_content, keyword=keyword,
            mevzuat_no=mevzuat_no, mevzuat_tur=22,
            case_sensitive=case_sensitive, max_results=max_results
        )

    except Exception as e:
        logger.exception(f"Error in tool 'search_within_cbgenelge' for {mevzuat_no}")
        return f"An unexpected error occurred: {str(e)}"


def main():
    logger.info(f"Starting {app.name} server...")
    try:
        app.run()
    except KeyboardInterrupt:
        logger.info(f"{app.name} server shut down by user.")
    except Exception:
        logger.exception(f"{app.name} server crashed.")


if __name__ == "__main__":
    main()
