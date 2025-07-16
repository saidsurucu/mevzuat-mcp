# mevzuat_mcp_server.py
"""
Main FastMCP server file for the Adalet Bakanlığı Mevzuat service.
This file defines the tools exposed to the LLM and orchestrates calls
to the MevzuatApiClient.
"""
import asyncio
import logging
import os
import json
from pydantic import Field
from typing import Optional, List, Dict, Any, Union

LOG_DIRECTORY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
if not os.path.exists(LOG_DIRECTORY):
    os.makedirs(LOG_DIRECTORY)
LOG_FILE_PATH = os.path.join(LOG_DIRECTORY, "mevzuat_mcp_server.log")
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(threadName)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE_PATH, mode='a', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from mevzuat_client import MevzuatApiClient
from mevzuat_models import (
    MevzuatSearchRequest, MevzuatSearchResult,
    MevzuatTurEnum, SortFieldEnum, SortDirectionEnum,
    MevzuatArticleNode, MevzuatArticleContent
)

app = FastMCP(
    name="MevzuatGovTrMCP",
    instructions="MCP server for Adalet Bakanlığı Mevzuat Bilgi Sistemi. Allows detailed searching of Turkish legislation and retrieving the content of specific articles.",
    dependencies=["httpx", "beautifulsoup4", "lxml", "markitdown", "pypdf"]
)

mevzuat_client = MevzuatApiClient()

@app.tool()
async def search_mevzuat(
    # mevzuat_adi: Optional[str] = Field(None, description="Search in legislation titles/names only. Cannot be used together with 'phrase' parameter. For exact phrase search, enclose in double quotes."),
    phrase: Optional[str] = Field(None, description="Turkish full-text search phrase. Supports multiple search operators:\\n\\nBoolean operators: AND, OR, NOT (space between words = AND logic)\\nRequired/prohibited terms: +required -prohibited\\nExact phrases: \\\"exact phrase\\\"\\nProximity search: \\\"word1 word2\\\"~5\\nWildcard search: word* or w?rd\\nFuzzy search: word~ or word~0.8\\nTerm boosting: important^2\\nRegex patterns: /[a-z]+/ with full regex syntax\\n\\nExamples:\\n- Basic: mahkeme\\n- Space = AND: mahkeme karar (finds both)\\n- Boolean: mahkeme AND karar\\n- Required: +mahkeme -eski\\n- Fuzzy: mahkeme~\\n- Wildcard: mah*\\n- Regex: /(mahkeme|karar)/"),
    mevzuat_no: Optional[str] = Field(None, description="The specific number of the legislation, e.g., '5237' for the Turkish Penal Code."),
    resmi_gazete_sayisi: Optional[str] = Field(None, description="The issue number of the Official Gazette where the legislation was published."),
    # AÇIKLAMA GÜNCELLENDİ
    mevzuat_turleri: Optional[Union[List[MevzuatTurEnum], str]] = Field(None, description="Filter by legislation types. A JSON-formatted string of this list is also acceptable."),
    page_number: int = Field(1, ge=1, description="Page number for pagination."),
    page_size: int = Field(5, ge=1, le=10, description="Number of results to return per page."),
    # AÇIKLAMA GÜNCELLENDİ
    sort_field: SortFieldEnum = Field("RESMI_GAZETE_TARIHI", description="Field to sort results by."),
    # AÇIKLAMA GÜNCELLENDİ
    sort_direction: SortDirectionEnum = Field("desc", description="Sorting direction.")
) -> MevzuatSearchResult:
    """
    Searches for Turkish legislation on mevzuat.gov.tr.
    Use 'phrase' for full-text content search with various operators and patterns.
    """
    if not phrase and not mevzuat_no:
        raise ToolError("You must provide at least one of the following search criteria: 'phrase' or 'mevzuat_no'.")

    # Convert boolean operators to Solr syntax
    def convert_boolean_operators(phrase_text: str) -> str:
        if not phrase_text:
            return phrase_text
        
        import re
        text = phrase_text
        
        # Convert AND to space (implicit AND works)
        text = re.sub(r'\s+AND\s+', ' ', text)
        
        # Convert NOT to - (this works!)
        text = re.sub(r'\s+NOT\s+', ' -', text)
        
        # Convert OR chains to regex
        def replace_or_chain(text):
            while 'OR' in text:
                # Match quoted terms separated by OR
                match = re.search(r'"([^"]+)"\s+OR\s+"([^"]+)"(?:\s+OR\s+"([^"]+)")*', text)
                if match:
                    # Extract all quoted terms in the OR chain
                    full_match = match.group(0)
                    terms = re.findall(r'"([^"]+)"', full_match)
                    # Convert to regex alternation
                    regex_pattern = f"/({'|'.join(terms)})/"
                    text = text.replace(full_match, regex_pattern, 1)
                else:
                    # Handle simple word OR word
                    simple_or = re.search(r'(\w+)\s+OR\s+(\w+)', text)
                    if simple_or:
                        word1, word2 = simple_or.groups()
                        regex_pattern = f"/({word1}|{word2})/"
                        text = text.replace(simple_or.group(0), regex_pattern, 1)
                    else:
                        break
            return text
        
        text = replace_or_chain(text)
        
        return text
    
    # Convert query to proximity search for fallback
    def convert_to_proximity(phrase_text: str) -> str:
        if not phrase_text:
            return phrase_text
            
        import re
        text = phrase_text
        
        # Skip if already contains proximity operators
        if '~' in text and '"' in text:
            return text
            
        # Skip if it's a regex pattern
        if text.startswith('/') and text.endswith('/'):
            return text
            
        # Skip if it contains complex operators (+, -, quotes, etc.)
        if any(op in text for op in ['+', '-', '"', '*', '?', '^']):
            return text
            
        # Convert space-separated words to proximity search
        # Split by spaces and boolean operators
        words = re.split(r'\s+(?:AND|OR|NOT)\s+|\s+', text)
        clean_words = [word.strip() for word in words if word.strip() and word not in ['AND', 'OR', 'NOT']]
        
        if len(clean_words) >= 2:
            # For any multiple words, try adjacent pairs with proximity 5
            # Use the pair that is most likely to match
            pairs = []
            for i in range(len(clean_words) - 1):
                pairs.append(f'"{clean_words[i]} {clean_words[i+1]}"~10')
            
            # Try the last pair first (often more specific)
            return pairs[-1] if pairs else text
        else:
            # Single word, just return as is
            return text
    
    # Process phrase - only convert OR to regex, other operators work natively
    processed_phrase = convert_boolean_operators(phrase) if phrase else phrase

    processed_turler = mevzuat_turleri
    if isinstance(mevzuat_turleri, str):
        try:
            parsed_list = json.loads(mevzuat_turleri)
            if isinstance(parsed_list, list):
                processed_turler = parsed_list
            else:
                raise ToolError(f"mevzuat_turleri was provided as a string, but it's not a JSON list. Value: {mevzuat_turleri}")
        except json.JSONDecodeError:
            raise ToolError(f"mevzuat_turleri was provided as a string, but it is not valid JSON. Value: {mevzuat_turleri}")

    search_req = MevzuatSearchRequest(
        # mevzuat_adi=mevzuat_adi,
        phrase=processed_phrase,
        mevzuat_no=mevzuat_no,
        resmi_gazete_sayisi=resmi_gazete_sayisi,
        mevzuat_tur_list=processed_turler if processed_turler is not None else ["KANUN", "CB_KARARNAME", "YONETMELIK", "CB_YONETMELIK", "CB_KARAR", "CB_GENELGE", "KHK", "TUZUK", "KKY", "UY", "TEBLIGLER", "MULGA"],
        page_number=page_number,
        page_size=page_size,
        sort_field=sort_field,
        sort_direction=sort_direction
    )
    
    log_params = search_req.model_dump(exclude_defaults=True)
    logger.info(f"Tool 'search_mevzuat' called with parameters: {log_params}")
    
    try:
        # First attempt: original query
        result = await mevzuat_client.search_documents(search_req)
        
        # Smart proximity fallback: if no results and we have a phrase
        if result.total_results == 0 and processed_phrase and not result.error_message:
            logger.info("No results found, attempting proximity fallback")
            
            # Try all proximity pairs until we find results
            import re
            words = re.split(r'\s+(?:AND|OR|NOT)\s+|\s+', processed_phrase)
            clean_words = [word.strip() for word in words if word.strip() and word not in ['AND', 'OR', 'NOT']]
            
            if len(clean_words) >= 2:
                # Generate all adjacent pairs
                pairs = []
                for i in range(len(clean_words) - 1):
                    pairs.append(f'"{clean_words[i]} {clean_words[i+1]}"~10')
                
                # Try each pair until we find results
                for pair_query in pairs:
                    logger.info(f"Trying proximity pair: {pair_query}")
                    
                    proximity_req = MevzuatSearchRequest(
                        phrase=pair_query,
                        mevzuat_no=mevzuat_no,
                        resmi_gazete_sayisi=resmi_gazete_sayisi,
                        mevzuat_tur_list=processed_turler if processed_turler is not None else ["KANUN", "CB_KARARNAME", "YONETMELIK", "CB_YONETMELIK", "CB_KARAR", "CB_GENELGE", "KHK", "TUZUK", "KKY", "UY", "TEBLIGLER", "MULGA"],
                        page_number=page_number,
                        page_size=page_size,
                        sort_field=sort_field,
                        sort_direction=sort_direction
                    )
                    
                    proximity_result = await mevzuat_client.search_documents(proximity_req)
                    if proximity_result.total_results > 0:
                        logger.info(f"Proximity fallback successful with '{pair_query}': {proximity_result.total_results} results")
                        return proximity_result
        
        # Return original result if no fallback was needed or fallback didn't help
        if not result.documents and not result.error_message:
            result.error_message = "No legislation found matching the specified criteria."
        return result
        
    except Exception as e:
        logger.exception("Error in tool 'search_mevzuat'.")
        return MevzuatSearchResult(
            documents=[], total_results=0, current_page=page_number, page_size=page_size, 
            total_pages=0, query_used=log_params, 
            error_message=f"An unexpected error occurred in the tool: {str(e)}"
        )

@app.tool()
async def get_mevzuat_article_tree(mevzuat_id: str = Field(..., description="The ID of the legislation, obtained from the 'search_mevzuat' tool. E.g., '343829'.")) -> List[MevzuatArticleNode]:
    """
    Retrieves the table of contents (article tree) for a specific legislation.
    This shows the chapters, sections, and articles in a hierarchical structure.
    If the tree is empty, it means the document doesn't have a hierarchical structure - you can use the mevzuat_id directly as the madde_id for get_mevzuat_article_content.
    """
    logger.info(f"Tool 'get_mevzuat_article_tree' called for mevzuat_id: {mevzuat_id}")
    try:
        article_tree = await mevzuat_client.get_article_tree(mevzuat_id)
        if not article_tree:
            logger.info(f"Article tree is empty for mevzuat_id {mevzuat_id}. Document may not have hierarchical structure.")
        return article_tree
    except Exception as e:
        logger.exception(f"Error in tool 'get_mevzuat_article_tree' for id {mevzuat_id}.")
        raise ToolError(f"Failed to retrieve article tree: {str(e)}")

@app.tool()
async def get_mevzuat_article_content(mevzuat_id: str = Field(..., description="The ID of the legislation, obtained from 'search_mevzuat' results."), madde_id: str = Field(..., description="The ID of the specific article (madde), obtained from the 'get_mevzuat_article_tree' tool. If article tree is empty, use the mevzuat_id as madde_id to get the full document content.")) -> MevzuatArticleContent:
    """
    Retrieves the full text content of a single article of a legislation and provides it as clean Markdown text.
    If the article tree is empty (no hierarchical structure), use the mevzuat_id as the madde_id parameter to get the full document content.
    """
    logger.info(f"Tool 'get_mevzuat_article_content' called for madde_id: {madde_id}")
    try:
        # If madde_id equals mevzuat_id, try to get full document content
        if madde_id == mevzuat_id:
            return await mevzuat_client.get_full_document_content(mevzuat_id)
        else:
            return await mevzuat_client.get_article_content(madde_id, mevzuat_id)
    except Exception as e:
        logger.exception(f"Error in tool 'get_mevzuat_article_content' for id {madde_id}.")
        return MevzuatArticleContent(
            madde_id=madde_id, mevzuat_id=mevzuat_id,
            markdown_content="", error_message=f"An unexpected error occurred: {str(e)}"
        )


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