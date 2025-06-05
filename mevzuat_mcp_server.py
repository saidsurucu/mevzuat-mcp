# mevzuat_mcp_server.py

import asyncio
import logging
import os
from pydantic import Field
from typing import Optional, List, Dict, Any

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
    MevzuatArticleNode, MevzuatArticleContent,
    MevzuatSearchToolArgs  
)

app = FastMCP(
    name="MevzuatMCP",
    instructions="MCP server for Adalet Bakanlığı Mevzuat Bilgi Sistemi. Allows detailed searching of Turkish legislation and retrieving the content of specific articles.",
    dependencies=["httpx", "beautifulsoup4", "lxml", "markitdown", "pypdf"]
)

mevzuat_client = MevzuatApiClient()


@app.tool()
async def search_mevzuat(args: MevzuatSearchToolArgs) -> MevzuatSearchResult:
    """
    Searches for Turkish legislation (laws, regulations, etc.) on mevzuat.gov.tr.
    Returns a paginated list of found documents.
    Note: For an exact phrase search, enclose the term in double quotes within the 'mevzuat_adi' parameter (e.g., '"ticaret kanunu"').
    """

    if not args.mevzuat_adi and not args.mevzuat_no:
        raise ToolError("You must provide either a search term ('mevzuat_adi') or a legislation number ('mevzuat_no').")


    search_req = MevzuatSearchRequest(
        mevzuat_adi=args.mevzuat_adi,
        mevzuat_no=args.mevzuat_no,
        resmi_gazete_sayisi=args.resmi_gazete_sayisi,
        search_in_title=args.search_in_title,
        mevzuat_tur_list=args.mevzuat_turleri if args.mevzuat_turleri is not None else [tur for tur in MevzuatTurEnum],
        page_number=args.page_number,
        page_size=args.page_size,
        sort_field=args.sort_field,
        sort_direction=args.sort_direction
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
            documents=[], total_results=0, current_page=args.page_number, page_size=args.page_size, 
            total_pages=0, query_used=log_params, 
            error_message=f"An unexpected error occurred in the tool: {str(e)}"
        )


@app.tool()
async def get_mevzuat_article_tree(mevzuat_id: str = Field(..., description="The ID of the legislation, obtained from the 'search_mevzuat' tool. E.g., '343829'.")) -> List[MevzuatArticleNode]:
    logger.info(f"Tool 'get_mevzuat_article_tree' called for mevzuat_id: {mevzuat_id}")
    try:
        return await mevzuat_client.get_article_tree(mevzuat_id)
    except Exception as e:
        logger.exception(f"Error in tool 'get_mevzuat_article_tree' for id {mevzuat_id}.")
        raise ToolError(f"Failed to retrieve article tree: {str(e)}")

@app.tool()
async def get_mevzuat_article_content(mevzuat_id: str = Field(..., description="The ID of the legislation, obtained from 'search_mevzuat' results."), madde_id: str = Field(..., description="The ID of the specific article (madde), obtained from the 'get_mevzuat_article_tree' tool. E.g., '2596801'.")) -> MevzuatArticleContent:
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