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
    mevzuat_adi: Optional[str] = Field(None, description="The name of the legislation or a keyword to search for. For an exact phrase search, enclose the term in double quotes."),
    phrase: Optional[str] = Field(None, description="Search for this term in the FULL TEXT of the legislation. For an exact phrase search, enclose the term in double quotes."),
    mevzuat_no: Optional[str] = Field(None, description="The specific number of the legislation, e.g., '5237' for the Turkish Penal Code."),
    resmi_gazete_sayisi: Optional[str] = Field(None, description="The issue number of the Official Gazette where the legislation was published."),
    # AÇIKLAMA GÜNCELLENDİ
    mevzuat_turleri: Optional[Union[List[MevzuatTurEnum], str]] = Field(None, description="Filter by legislation types. IMPORTANT: Provide a list of exact enum values. Possible values: KANUN (Law - Kanun), CB_KARARNAME (Presidential Decree - Cumhurbaşkanlığı Kararnamesi), YONETMELIK (Regulation - Yönetmelik), CB_YONETMELIK (Presidential Regulation - Cumhurbaşkanlığı Yönetmeliği), CB_KARAR (Presidential Decision - Cumhurbaşkanlığı Kararı), CB_GENELGE (Presidential Circular - Cumhurbaşkanlığı Genelgesi), KHK (Decree Law - Kanun Hükmünde Kararname), TUZUK (Statute/Bylaw - Tüzük), KKY (Institutional and Organizational Regulations - Kurum ve Kuruluş Yönetmelikleri), UY (Procedures and Regulations - Usul ve Yönetmelikler), TEBLIGLER (Communiqué - Tebliğler), MULGA (Repealed - Mülga). A JSON-formatted string of this list is also acceptable."),
    page_number: int = Field(1, ge=1, description="Page number for pagination."),
    page_size: int = Field(10, ge=1, le=50, description="Number of results to return per page."),
    # AÇIKLAMA GÜNCELLENDİ
    sort_field: SortFieldEnum = Field(SortFieldEnum.RESMI_GAZETE_TARIHI, description="Field to sort results by. Possible values: RESMI_GAZETE_TARIHI (Official Gazette Date - Resmi Gazete Tarihi), KAYIT_TARIHI (Registration Date - Kayıt Tarihi), MEVZUAT_NUMARASI (Legislation Number - Mevzuat Numarası)."),
    # AÇIKLAMA GÜNCELLENDİ
    sort_direction: SortDirectionEnum = Field(SortDirectionEnum.DESC, description="Sorting direction. Possible values: DESC (descending, newest to oldest - Azalan, yeniden eskiye), ASC (ascending, oldest to newest - Artan, eskiden yeniye).")
) -> MevzuatSearchResult:
    """
    Searches for Turkish legislation on mevzuat.gov.tr.
    Use 'mevzuat_adi' for title-only search and 'phrase' for full-text search.
    """
    if not mevzuat_adi and not phrase and not mevzuat_no:
        raise ToolError("You must provide at least one of the following search criteria: 'mevzuat_adi', 'phrase', or 'mevzuat_no'.")

    if mevzuat_adi and phrase:
        raise ToolError("You cannot search by title ('mevzuat_adi') and full text ('phrase') at the same time. Please provide only one of them.")

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
        mevzuat_adi=mevzuat_adi,
        phrase=phrase,
        mevzuat_no=mevzuat_no,
        resmi_gazete_sayisi=resmi_gazete_sayisi,
        mevzuat_tur_list=processed_turler if processed_turler is not None else [tur for tur in MevzuatTurEnum],
        page_number=page_number,
        page_size=page_size,
        sort_field=sort_field,
        sort_direction=sort_direction
    )
    
    log_params = search_req.model_dump(exclude_defaults=True)
    logger.info(f"Tool 'search_mevzuat' called with parameters: {log_params}")
    
    try:
        result = await mevzuat_client.search_documents(search_req)
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
    """
    logger.info(f"Tool 'get_mevzuat_article_tree' called for mevzuat_id: {mevzuat_id}")
    try:
        return await mevzuat_client.get_article_tree(mevzuat_id)
    except Exception as e:
        logger.exception(f"Error in tool 'get_mevzuat_article_tree' for id {mevzuat_id}.")
        raise ToolError(f"Failed to retrieve article tree: {str(e)}")

@app.tool()
async def get_mevzuat_article_content(mevzuat_id: str = Field(..., description="The ID of the legislation, obtained from 'search_mevzuat' results."), madde_id: str = Field(..., description="The ID of the specific article (madde), obtained from the 'get_mevzuat_article_tree' tool. E.g., '2596801'.")) -> MevzuatArticleContent:
    """
    Retrieves the full text content of a single article of a legislation and provides it as clean Markdown text.
    """
    logger.info(f"Tool 'get_mevzuat_article_content' called for madde_id: {madde_id}")
    try:
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