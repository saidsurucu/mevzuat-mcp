# mevzuat_mcp_server_new.py
"""
FastMCP server for mevzuat.gov.tr (direct API).
Supports searching and PDF content extraction for Kanun (laws).
"""
import logging
from pydantic import Field
from typing import Optional

# Simple logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from mevzuat_client import MevzuatApiClientNew
from mevzuat_models import (
    MevzuatSearchRequestNew,
    MevzuatSearchResultNew,
    MevzuatTurLiteral,
    MevzuatArticleContent
)
from article_search import search_articles_by_keyword, ArticleSearchResult, format_search_results

app = FastMCP(
    name="MevzuatGovTrMCP",
    instructions="MCP server for mevzuat.gov.tr. Allows searching Turkish legislation (Kanun/laws, Tebliğ/communiqués, CB Kararnamesi/Presidential Decrees, CB Kararı/Presidential Decisions, CB Yönetmeliği/Presidential Regulations, CB Genelgesi/Presidential Circulars) and retrieving full content."
)

# Initialize client with caching enabled (1 hour TTL by default)
# Mistral API key will be loaded from environment variable MISTRAL_API_KEY
mevzuat_client = MevzuatApiClientNew(cache_ttl=3600, enable_cache=True)


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
    page_size: int = Field(
        25,
        ge=1,
        le=100,
        description="Number of results per page (1-100)"
    )
) -> MevzuatSearchResultNew:
    """
    Search for Turkish laws (Kanun) in both titles and content.

    This tool searches in law titles and full text content.
    Use 'search_within_kanun' to search within a specific law's articles and get article-level results.

    Query Syntax:
    - Simple keyword: yatırımcı
    - Boolean AND: yatırımcı AND tazmin (both terms)
    - Boolean OR: yatırımcı OR müşteri (at least one term)
    - Boolean NOT: yatırımcı NOT kurum (first yes, second no)
    - Required term: +yatırımcı +tazmin (similar to AND)
    - Grouping: (yatırımcı OR müşteri) AND tazmin
    - Exact phrase: "mali sıkıntı" (or use tam_cumle=true)

    Returns:
    - Law number, title, and acceptance date
    - Official Gazette publication date and issue number
    - URLs for viewing online and downloading PDF

    Example queries:
    - "sermaye piyasası" - Find Capital Markets Law
    - "vergi OR gelir" - Laws with tax or income in title/content
    - "ceza muhakemesi" - Find Criminal Procedure Law
    """
    search_req = MevzuatSearchRequestNew(
        mevzuat_tur="Kanun",
        aranacak_ifade=aranacak_ifade,
        aranacak_yer=1,  # 1=Title only
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
        description='Search query supporting advanced operators: simple keyword ("yatırımcı"), exact phrase ("mali sıkıntı"), AND/OR/NOT operators (yatırımcı AND tazmin, yatırımcı OR müşteri, yatırımcı NOT kurum). Operators must be uppercase.'
    ),
    mevzuat_tertip: str = Field(
        "5",
        description="Legislation series from search results (e.g., '3', '5')"
    ),
    case_sensitive: bool = Field(
        False,
        description="Whether to match case when searching (default: False)"
    ),
    max_results: int = Field(
        25,
        ge=1,
        le=50,
        description="Maximum number of matching articles to return (1-50, default: 25)"
    )
) -> str:
    """
    Search for a keyword within a specific legislation's articles with advanced query operators.

    This tool is optimized for large legislation (e.g., Sermaye Piyasası Kanunu with 142 articles).
    Instead of loading the entire legislation into context, it:
    1. Fetches the full content
    2. Splits it into individual articles (madde)
    3. Returns only the articles that match the search query
    4. Sorts results by relevance score (based on match count)

    Query Syntax (operators must be uppercase):
    - Simple keyword: yatırımcı
    - Exact phrase: "mali sıkıntı"
    - AND operator: yatırımcı AND tazmin (both terms must be present)
    - OR operator: yatırımcı OR müşteri (at least one term must be present)
    - NOT operator: yatırımcı NOT kurum (first term present, second must not be)
    - Combinations: "mali sıkıntı" AND yatırımcı NOT kurum

    Returns formatted text with:
    - Article number and title
    - Relevance score (higher = more matches)
    - Full article content for matching articles

    Example use cases:
    - Search for "yatırımcı" in Kanun 6362 (Capital Markets Law)
    - Search for "ceza AND temyiz" in Kanun 5237 (Turkish Penal Code)
    - Search for "vergi OR ücret" in tax-related legislation
    - Search for '"iş kazası" AND işveren NOT işçi' for specific labor law articles
    """
    logger.info(f"Tool 'search_within_kanun' called: {mevzuat_no}, keyword: '{keyword}'")

    try:
        # Get full content
        content_result = await mevzuat_client.get_content(
            mevzuat_no=mevzuat_no,
            mevzuat_tur=1,  # Kanun
            mevzuat_tertip=mevzuat_tertip
        )

        if content_result.error_message:
            return f"Error fetching legislation content: {content_result.error_message}"

        # Search within articles
        matches = search_articles_by_keyword(
            markdown_content=content_result.markdown_content,
            keyword=keyword,
            case_sensitive=case_sensitive,
            max_results=max_results
        )

        result = ArticleSearchResult(
            mevzuat_no=mevzuat_no,
            mevzuat_tur=1,
            keyword=keyword,
            total_matches=len(matches),
            matching_articles=matches
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
    page_size: int = Field(
        25,
        ge=1,
        le=100,
        description="Number of results per page (1-100)"
    )
) -> MevzuatSearchResultNew:
    """
    Search for Turkish communiqués (Tebliğ) in both titles and content.

    This tool searches in communiqué titles and full text content.
    Communiqués are regulatory documents issued by various government institutions.

    Query Syntax:
    - Simple keyword: vergi
    - Boolean AND: vergi AND muafiyet (both terms)
    - Boolean OR: muafiyet OR istisna (at least one term)
    - Boolean NOT: vergi NOT gelir (first yes, second no)
    - Required term: +vergi +muafiyet (similar to AND)
    - Grouping: (muafiyet OR istisna) AND vergi
    - Exact phrase: "katma değer vergisi" (or use tam_cumle=true)

    Returns:
    - Communiqué number, title, and publication date
    - Official Gazette publication date and issue number
    - URLs for viewing online

    Example queries:
    - "katma değer vergisi" - Find VAT-related communiqués
    - "muafiyet OR istisna" - Communiqués about exemptions
    - "vergi AND matrah" - Tax base related communiqués
    """
    search_req = MevzuatSearchRequestNew(
        mevzuat_tur="Tebliğ",
        aranacak_ifade=aranacak_ifade,
        aranacak_yer=1,  # Search in titles and content
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
    page_size: int = Field(
        25,
        ge=1,
        le=100,
        description="Number of results per page (1-100)"
    )
) -> MevzuatSearchResultNew:
    """
    Search for Turkish Presidential Decrees (Cumhurbaşkanlığı Kararnamesi) in both titles and content.

    This tool searches in Presidential Decree titles and full text content.
    Presidential Decrees are executive orders issued by the President of Turkey.

    Query Syntax:
    - Simple keyword: organize
    - Boolean AND: organize AND suç (both terms)
    - Boolean OR: suç OR ceza (at least one term)
    - Boolean NOT: organize NOT terör (first yes, second no)
    - Required term: +organize +suç (similar to AND)
    - Grouping: (organize OR terör) AND suç
    - Exact phrase: "organize suç" (or use tam_cumle=true)

    Returns:
    - Decree number, title, and publication date
    - Official Gazette publication date and issue number
    - URLs for viewing online

    Example queries:
    - "organize suç" - Find decrees about organized crime
    - "kamu OR devlet" - Decrees about public or state matters
    - "ceza AND infaz" - Decrees about criminal enforcement
    """
    search_req = MevzuatSearchRequestNew(
        mevzuat_tur="Cumhurbaşkanlığı Kararnamesi",
        aranacak_ifade=aranacak_ifade,
        aranacak_yer=1,  # Search in titles and content
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
        description='Search query supporting advanced operators: simple keyword ("organize"), exact phrase ("organize suç"), AND/OR/NOT operators (organize AND suç, suç OR ceza, organize NOT terör). Operators must be uppercase.'
    ),
    mevzuat_tertip: str = Field(
        "5",
        description="Decree series from search results (e.g., '5')"
    ),
    case_sensitive: bool = Field(
        False,
        description="Whether to match case when searching (default: False)"
    ),
    max_results: int = Field(
        25,
        ge=1,
        le=50,
        description="Maximum number of matching articles to return (1-50, default: 25)"
    )
) -> str:
    """
    Search for a keyword within a specific Presidential Decree's articles with advanced query operators.

    This tool is optimized for large Presidential Decrees.
    Instead of loading the entire decree into context, it:
    1. Fetches the full content
    2. Splits it into individual articles (madde)
    3. Returns only the articles that match the search query
    4. Sorts results by relevance score (based on match count)

    Query Syntax (operators must be uppercase):
    - Simple keyword: organize
    - Exact phrase: "organize suç"
    - AND operator: organize AND suç (both terms must be present)
    - OR operator: organize OR terör (at least one term must be present)
    - NOT operator: organize NOT terör (first term present, second must not be)
    - Combinations: "organize suç" AND ceza NOT terör

    Returns formatted text with:
    - Article number and title
    - Relevance score (higher = more matches)
    - Full article content for matching articles

    Example use cases:
    - Search for "organize" in CBK 1 (Judicial Reform)
    - Search for "suç AND ceza" in specific decree
    - Search for "devlet OR kamu" in administrative decrees
    """
    logger.info(f"Tool 'search_within_cbk' called: {mevzuat_no}, keyword: '{keyword}'")

    try:
        # Get full content
        content_result = await mevzuat_client.get_content(
            mevzuat_no=mevzuat_no,
            mevzuat_tur=19,  # Cumhurbaşkanlığı Kararnamesi
            mevzuat_tertip=mevzuat_tertip
        )

        if content_result.error_message:
            return f"Error fetching decree content: {content_result.error_message}"

        # Search within articles
        matches = search_articles_by_keyword(
            markdown_content=content_result.markdown_content,
            keyword=keyword,
            case_sensitive=case_sensitive,
            max_results=max_results
        )

        result = ArticleSearchResult(
            mevzuat_no=mevzuat_no,
            mevzuat_tur=19,
            keyword=keyword,
            total_matches=len(matches),
            matching_articles=matches
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
    page_size: int = Field(
        25,
        ge=1,
        le=100,
        description="Number of results per page (max 100)"
    )
) -> MevzuatSearchResultNew:
    """
    Search for Turkish Presidential Regulations (Cumhurbaşkanlığı Yönetmeliği / CB Yönetmeliği) in both titles and content.

    This tool searches in regulation titles and full text content.
    Use 'search_within_cbyonetmelik' to search within a specific regulation's articles and get article-level results.

    Boolean operators (must be uppercase):
    - AND: Both terms must be present (yatırımcı AND tazmin)
    - OR: At least one term must be present (vergi OR ücret)
    - NOT: Exclude term (yatırımcı NOT kurum)
    - Exact phrase: Use quotes ("mali sıkıntı")

    Returns:
    - List of matching regulations with metadata (number, title, publication date, Official Gazette info)
    - Total result count and pagination info
    - Use the 'mevzuat_no' and 'mevzuat_tertip' from results for content retrieval

    Example usage:
    - List all: search_cbyonetmelik()
    - Search: search_cbyonetmelik(aranacak_ifade="ihale")
    - Filter by year: search_cbyonetmelik(baslangic_tarihi="2023", bitis_tarihi="2024")
    """
    logger.info(f"Tool 'search_cbyonetmelik' called with query: {aranacak_ifade}")

    try:
        search_req = MevzuatSearchRequestNew(
            mevzuat_tur="CB Yönetmeliği",
            aranacak_ifade=aranacak_ifade or "",
            aranacak_yer=1,
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
        logger.exception(f"Error in tool 'search_cbyonetmelik'")
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
        description='Search query supporting advanced operators: simple word (yatırımcı), AND (yatırımcı AND tazmin), OR (vergi OR ücret), NOT (yatırımcı NOT kurum), exact phrase with quotes ("mali sıkıntı")'
    ),
    mevzuat_tertip: str = Field(
        "5",
        description="Regulation series from search results (typically '5')"
    ),
    case_sensitive: bool = Field(
        False,
        description="Whether to match case (false = case-insensitive, default)"
    ),
    max_results: int = Field(
        25,
        ge=1,
        le=50,
        description="Maximum number of matching articles to return (1-50)"
    )
) -> str:
    """
    Search for a keyword within a specific Presidential Regulation's articles with advanced query operators.

    This tool:
    1. Retrieves the full content of the specified regulation
    2. Splits it into individual articles (madde)
    3. Searches within each article using the keyword query
    4. Returns matching articles sorted by relevance

    Query syntax (operators must be uppercase):
    - Simple keyword: "yatırımcı"
    - Exact phrase: "mali sıkıntı"
    - AND operator: yatırımcı AND tazmin (both must be present)
    - OR operator: yatırımcı OR müşteri (at least one must be present)
    - NOT operator: yatırımcı NOT kurum (exclude term)
    - Combinations: "mali sıkıntı" AND yatırımcı NOT kurum

    Returns:
    - Full text of each matching article
    - Article number and title
    - Number of keyword occurrences
    - Results sorted by relevance (most matches first)

    Example usage:
    1. First search regulations: search_cbyonetmelik(aranacak_ifade="ihale")
    2. Then search within: search_within_cbyonetmelik(mevzuat_no="9014", keyword="taşınır mal")
    """
    logger.info(f"Tool 'search_within_cbyonetmelik' called: regulation {mevzuat_no}, keyword: {keyword}")

    try:
        # Get full regulation content
        content_result = await mevzuat_client.get_content(
            mevzuat_no=mevzuat_no,
            mevzuat_tur=21,  # CB Yönetmeliği
            mevzuat_tertip=mevzuat_tertip
        )

        if content_result.error_message:
            logger.warning(f"Error fetching regulation content: {content_result.error_message}")
            return f"Error: {content_result.error_message}"

        if not content_result.markdown_content:
            return f"Error: No content found for regulation {mevzuat_no}"

        # Search within articles
        matches = search_articles_by_keyword(
            markdown_content=content_result.markdown_content,
            keyword=keyword,
            case_sensitive=case_sensitive,
            max_results=max_results
        )

        if not matches:
            return f"No articles found matching '{keyword}' in regulation {mevzuat_no}"

        # Format and return results
        result = ArticleSearchResult(
            mevzuat_no=mevzuat_no,
            mevzuat_tur=21,
            keyword=keyword,
            total_matches=len(matches),
            matching_articles=matches
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
    page_size: int = Field(
        25,
        ge=1,
        le=100,
        description="Number of results per page (1-100)"
    )
) -> MevzuatSearchResultNew:
    """
    Search for Turkish Presidential Decisions (Cumhurbaşkanı Kararı) in both titles and content.

    This tool searches in Presidential Decision titles and full text content.
    Presidential Decisions are executive decisions issued by the President of Turkey (different from Presidential Decrees/Kararnamesi).

    Query Syntax:
    - Simple keyword: atama
    - Boolean AND: atama AND tayin (both terms)
    - Boolean OR: atama OR görevden (at least one term)
    - Boolean NOT: atama NOT görevden (first yes, second no)
    - Required term: +atama +tayin (similar to AND)
    - Grouping: (atama OR tayin) AND görev
    - Exact phrase: "görevden alma" (or use tam_cumle=true)
    - Empty search: List all decisions (use date filters)

    Returns:
    - Decision number, title, and publication date
    - Official Gazette publication date and issue number
    - URLs for viewing online (PDF format)

    Example queries:
    - "atama tayin" - Find decisions about appointments
    - Leave empty with dates to list all decisions from a period
    - "görevden AND alma" - Decisions about dismissals
    """
    search_req = MevzuatSearchRequestNew(
        mevzuat_tur="Cumhurbaşkanı Kararı",
        aranacak_ifade=aranacak_ifade or "",
        aranacak_yer=1,  # Search in titles and content
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
    page_size: int = Field(
        25,
        ge=1,
        le=100,
        description="Number of results per page (max 100)"
    )
) -> MevzuatSearchResultNew:
    """
    Search for Turkish Presidential Circulars (Cumhurbaşkanlığı Genelgesi / CB Genelgesi) in titles.

    This tool searches in circular titles (search is title-only by default for this document type).
    Use 'get_cbgenelge_content' to retrieve the full PDF content of a specific circular.

    Boolean operators (must be uppercase):
    - AND: Both terms must be present (organize AND suç)
    - OR: At least one term must be present (suç OR ceza)
    - NOT: Exclude term (organize NOT terör)
    - Exact phrase: Use quotes ("organize suç")

    Returns:
    - List of matching circulars with metadata (number, title, publication date, Official Gazette info)
    - Total result count and pagination info
    - Use the 'mevzuat_no' and 'resmi_gazete_tarihi' from results for content retrieval

    Note: Presidential Circulars are available as PDF files only.

    Example usage:
    - List all: search_cbgenelge()
    - Search: search_cbgenelge(aranacak_ifade="koordinasyon")
    - Filter by year: search_cbgenelge(baslangic_tarihi="2024", bitis_tarihi="2025")
    """
    logger.info(f"Tool 'search_cbgenelge' called with query: {aranacak_ifade}")

    try:
        search_req = MevzuatSearchRequestNew(
            mevzuat_tur="CB Genelgesi",
            aranacak_ifade=aranacak_ifade or "",
            aranacak_yer=1,
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
        logger.exception(f"Error in tool 'search_cbgenelge'")
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
    page_size: int = Field(
        25,
        ge=1,
        le=100,
        description="Number of results per page (1-100, default: 25)"
    )
) -> MevzuatSearchResultNew:
    """
    Search for Turkish Decree Laws (Kanun Hükmünde Kararname / KHK) by title.

    Note: KHKs were abolished after the 2017 constitutional referendum. The last KHKs were issued in 2018.
    However, previously enacted KHKs remain in force unless repealed.

    Query Syntax:
    - Simple keyword: değişiklik
    - Exact phrase: "sağlık düzenleme" (use quotes or set tam_cumle=True)
    - AND operator: sağlık AND düzenleme (both terms must be present)
    - OR operator: bakanlık OR kurum (at least one term must be present)
    - NOT operator: kanun NOT yürürlük (first term present, second must not be)
    - Wildcard: değişiklik* (matches değişiklikler, değişikliği, etc.)
    - Combinations: (sağlık OR eğitim) AND düzenleme NOT yürürlük

    Returns:
    - List of matching KHKs with numbers, titles, dates, and metadata
    - Pagination info and total result count
    - Each KHK includes: mevzuat_no, mev_adi, kabul_tarih, resmi_gazete_tarihi, etc.

    Example usage:
    - search_khk(aranacak_ifade="anayasa", baslangic_tarihi="2018") → Find constitutional KHKs from 2018
    - search_khk(baslangic_tarihi="2010", bitis_tarihi="2018") → List all KHKs from 2010-2018
    - search_khk(aranacak_ifade="sağlık AND düzenleme") → Find health-related KHKs
    """
    logger.info(f"Tool 'search_khk' called: '{aranacak_ifade}', dates: {baslangic_tarihi}-{bitis_tarihi}")

    try:
        search_req = MevzuatSearchRequestNew(
            mevzuat_tur="KHK",
            aranacak_ifade=aranacak_ifade or "",
            aranacak_yer=1,  # Title search
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
        description='Search query supporting advanced operators: simple keyword ("değişiklik"), exact phrase ("kanun hükmünde"), AND/OR/NOT operators (kanun AND değişiklik, madde OR fıkra, değişiklik NOT yürürlük). Operators must be uppercase.'
    ),
    mevzuat_tertip: str = Field(
        "5",
        description="KHK series from search results (e.g., '5')"
    ),
    case_sensitive: bool = Field(
        False,
        description="Whether to match case when searching (default: False)"
    ),
    max_results: int = Field(
        25,
        ge=1,
        le=50,
        description="Maximum number of matching articles to return (1-50, default: 25)"
    )
) -> str:
    """
    Search for a keyword within a specific Decree Law's (KHK) articles with advanced query operators.

    This tool is optimized for large KHKs.
    Instead of loading the entire decree law into context, it:
    1. Fetches the full content
    2. Splits it into individual articles (madde)
    3. Returns only the articles that match the search query
    4. Sorts results by relevance score (based on match count)

    Query Syntax (operators must be uppercase):
    - Simple keyword: değişiklik
    - Exact phrase: "kanun hükmünde"
    - AND operator: kanun AND değişiklik (both terms must be present)
    - OR operator: madde OR fıkra (at least one term must be present)
    - NOT operator: değişiklik NOT yürürlük (first term present, second must not be)
    - Combinations: "kanun hükmünde" AND değişiklik NOT yürürlük

    Returns formatted text with:
    - Article number and title
    - Relevance score (higher = more matches)
    - Full article content for matching articles

    Example use cases:
    - Search for "anayasa" in KHK 703 (Constitutional amendments)
    - Search for "sağlık AND düzenleme" in KHK 663 (Health regulations)
    - Search for "bakanlık OR kurum" in organizational KHKs
    """
    logger.info(f"Tool 'search_within_khk' called: {mevzuat_no}, keyword: '{keyword}'")

    try:
        # Get full content
        content_result = await mevzuat_client.get_content(
            mevzuat_no=mevzuat_no,
            mevzuat_tur=4,  # KHK
            mevzuat_tertip=mevzuat_tertip
        )

        if content_result.error_message:
            return f"Error fetching KHK content: {content_result.error_message}"

        # Search within articles
        matches = search_articles_by_keyword(
            markdown_content=content_result.markdown_content,
            keyword=keyword,
            case_sensitive=case_sensitive,
            max_results=max_results
        )

        result = ArticleSearchResult(
            mevzuat_no=mevzuat_no,
            mevzuat_tur=4,
            keyword=keyword,
            total_matches=len(matches),
            matching_articles=matches
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
    page_size: int = Field(
        25,
        ge=1,
        le=100,
        description="Number of results per page (1-100, default: 25)"
    )
) -> MevzuatSearchResultNew:
    """
    Search for Turkish Statutes/Regulations (Tüzük) by title.

    Tüzük are regulatory statutes that implement and detail the provisions of laws.
    They are issued by government agencies and regulatory bodies.

    Query Syntax:
    - Simple keyword: tapu
    - Exact phrase: "sicil kayıt" (use quotes or set tam_cumle=True)
    - AND operator: tapu AND sicil (both terms must be present)
    - OR operator: tescil OR ilan (at least one term must be present)
    - NOT operator: vakıf NOT kurul (first term present, second must not be)
    - Wildcard: tescil* (matches tescil, tescile, tescili, etc.)
    - Combinations: (tapu OR kadastro) AND sicil NOT iptal

    Returns:
    - List of matching statutes with numbers, titles, dates, and metadata
    - Pagination info and total result count
    - Each statute includes: mevzuat_no, mev_adi, kabul_tarih, resmi_gazete_tarihi, etc.

    Example usage:
    - search_tuzuk(aranacak_ifade="tapu") → Find land registry related statutes
    - search_tuzuk(baslangic_tarihi="2008", bitis_tarihi="2013") → List all statutes from 2008-2013
    - search_tuzuk(aranacak_ifade="vakıf AND tescil") → Find foundation registration statutes
    """
    logger.info(f"Tool 'search_tuzuk' called: '{aranacak_ifade}', dates: {baslangic_tarihi}-{bitis_tarihi}")

    try:
        search_req = MevzuatSearchRequestNew(
            mevzuat_tur="Tuzuk",
            aranacak_ifade=aranacak_ifade or "",
            aranacak_yer=1,  # Title search
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
        description='Search query supporting advanced operators: simple keyword ("kayıt"), exact phrase ("sicil kayıt"), AND/OR/NOT operators (tapu AND sicil, tescil OR ilan, kayıt NOT iptal). Operators must be uppercase.'
    ),
    mevzuat_tertip: str = Field(
        "5",
        description="Statute series from search results (e.g., '5')"
    ),
    case_sensitive: bool = Field(
        False,
        description="Whether to match case when searching (default: False)"
    ),
    max_results: int = Field(
        25,
        ge=1,
        le=50,
        description="Maximum number of matching articles to return (1-50, default: 25)"
    )
) -> str:
    """
    Search for a keyword within a specific Statute's (Tüzük) articles with advanced query operators.

    This tool is optimized for large statutes.
    Instead of loading the entire statute into context, it:
    1. Fetches the full content
    2. Splits it into individual articles (madde)
    3. Returns only the articles that match the search query
    4. Sorts results by relevance score (based on match count)

    Query Syntax (operators must be uppercase):
    - Simple keyword: kayıt
    - Exact phrase: "sicil kayıt"
    - AND operator: tapu AND sicil (both terms must be present)
    - OR operator: tescil OR ilan (at least one term must be present)
    - NOT operator: kayıt NOT iptal (first term present, second must not be)
    - Combinations: "sicil kayıt" AND tapu NOT iptal

    Returns formatted text with:
    - Article number and title
    - Relevance score (higher = more matches)
    - Full article content for matching articles

    Example use cases:
    - Search for "tapu" in Tapu Sicili Tüzüğü (20135150)
    - Search for "tescil AND ilan" in Vakıflar Tüzüğü (20134513)
    - Search for "kayıt OR sicil" in cadastral statutes
    """
    logger.info(f"Tool 'search_within_tuzuk' called: {mevzuat_no}, keyword: '{keyword}'")

    try:
        # Get full content
        content_result = await mevzuat_client.get_content(
            mevzuat_no=mevzuat_no,
            mevzuat_tur=2,  # Tüzük
            mevzuat_tertip=mevzuat_tertip
        )

        if content_result.error_message:
            return f"Error fetching statute content: {content_result.error_message}"

        # Search within articles
        matches = search_articles_by_keyword(
            markdown_content=content_result.markdown_content,
            keyword=keyword,
            case_sensitive=case_sensitive,
            max_results=max_results
        )

        result = ArticleSearchResult(
            mevzuat_no=mevzuat_no,
            mevzuat_tur=2,
            keyword=keyword,
            total_matches=len(matches),
            matching_articles=matches
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
    page_size: int = Field(
        25,
        ge=1,
        le=100,
        description="Number of results per page (1-100, default: 25)"
    )
) -> MevzuatSearchResultNew:
    """
    Search for Institutional and Organizational Regulations (Kurum ve Kuruluş Yönetmeliği) by title.

    These are regulations issued by governmental institutions and organizations to regulate
    their internal operations, procedures, and administrative matters.

    Note: This is the largest dataset with 8686+ regulations across all government institutions.

    Query Syntax:
    - Simple keyword: nükleer
    - Exact phrase: "ihracat kontrol" (use quotes or set tam_cumle=True)
    - AND operator: nükleer AND ihracat (both terms must be present)
    - OR operator: denetim OR teftiş (at least one term must be present)
    - NOT operator: mali NOT ceza (first term present, second must not be)
    - Wildcard: kontrol* (matches kontrol, kontrolü, kontrole, etc.)
    - Combinations: (nükleer OR kimyasal) AND ihracat NOT silah

    Returns:
    - List of matching regulations with numbers, titles, dates, and metadata
    - Pagination info and total result count
    - Each regulation includes: mevzuat_no, mev_adi, resmi_gazete_tarihi, etc.

    Example usage:
    - search_kurum_yonetmelik(aranacak_ifade="nükleer") → Find nuclear-related regulations
    - search_kurum_yonetmelik(baslangic_tarihi="2025") → List all regulations from 2025
    - search_kurum_yonetmelik(aranacak_ifade="adalet AND akademi") → Find justice academy regulations
    """
    logger.info(f"Tool 'search_kurum_yonetmelik' called: '{aranacak_ifade}', dates: {baslangic_tarihi}-{bitis_tarihi}")

    try:
        search_req = MevzuatSearchRequestNew(
            mevzuat_tur="Kurum Yönetmeliği",
            aranacak_ifade=aranacak_ifade or "",
            aranacak_yer=1,  # Title search
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
        description='Search query supporting advanced operators: simple keyword ("kontrol"), exact phrase ("ihracat kontrol"), AND/OR/NOT operators (nükleer AND ihracat, denetim OR teftiş, kontrol NOT iptal). Operators must be uppercase.'
    ),
    mevzuat_tertip: str = Field(
        "5",
        description="Regulation series from search results (e.g., '5')"
    ),
    case_sensitive: bool = Field(
        False,
        description="Whether to match case when searching (default: False)"
    ),
    max_results: int = Field(
        25,
        ge=1,
        le=50,
        description="Maximum number of matching articles to return (1-50, default: 25)"
    )
) -> str:
    """
    Search for a keyword within a specific Institutional Regulation's articles with advanced query operators.

    This tool is optimized for large regulations.
    Instead of loading the entire regulation into context, it:
    1. Fetches the full content
    2. Splits it into individual articles (madde)
    3. Returns only the articles that match the search query
    4. Sorts results by relevance score (based on match count)

    Query Syntax (operators must be uppercase):
    - Simple keyword: kontrol
    - Exact phrase: "ihracat kontrol"
    - AND operator: nükleer AND ihracat (both terms must be present)
    - OR operator: denetim OR teftiş (at least one term must be present)
    - NOT operator: kontrol NOT iptal (first term present, second must not be)
    - Combinations: "ihracat kontrol" AND nükleer NOT silah

    Returns formatted text with:
    - Article number and title
    - Relevance score (higher = more matches)
    - Full article content for matching articles

    Example use cases:
    - Search for "nükleer" in Nuclear Export Regulation (42641)
    - Search for "disiplin AND ceza" in disciplinary regulations
    - Search for "görev OR yetki" in organizational regulations
    """
    logger.info(f"Tool 'search_within_kurum_yonetmelik' called: {mevzuat_no}, keyword: '{keyword}'")

    try:
        # Get full content
        content_result = await mevzuat_client.get_content(
            mevzuat_no=mevzuat_no,
            mevzuat_tur=7,  # Kurum Yönetmeliği
            mevzuat_tertip=mevzuat_tertip
        )

        if content_result.error_message:
            return f"Error fetching regulation content: {content_result.error_message}"

        # Search within articles
        matches = search_articles_by_keyword(
            markdown_content=content_result.markdown_content,
            keyword=keyword,
            case_sensitive=case_sensitive,
            max_results=max_results
        )

        result = ArticleSearchResult(
            mevzuat_no=mevzuat_no,
            mevzuat_tur=7,
            keyword=keyword,
            total_matches=len(matches),
            matching_articles=matches
        )

        if len(matches) == 0:
            return f"No articles found containing '{keyword}' in Kurum Yönetmeliği {mevzuat_no}"

        return format_search_results(result)

    except Exception as e:
        logger.exception(f"Error in tool 'search_within_kurum_yonetmelik' for {mevzuat_no}")
        return f"An unexpected error occurred while searching Kurum Yönetmeliği {mevzuat_no}: {str(e)}"


def main():
    logger.info(f"Starting {app.name} server...")
    try:
        app.run()
    except KeyboardInterrupt:
        logger.info(f"{app.name} server shut down by user.")
    except Exception as e:
        logger.exception(f"{app.name} server crashed.")


if __name__ == "__main__":
    main()
