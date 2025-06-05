# mevzuat_models.py
"""
Pydantic models for the Adalet Bakanlığı Mevzuat MCP server.
Defines data structures for search requests, search results, and document content.
"""

from pydantic import BaseModel, Field, HttpUrl
from typing import List, Optional, Dict, Any
from enum import Enum
import datetime

class MevzuatTurEnum(str, Enum):
    """Enum for legislation types available in the search."""
    KANUN = "KANUN"
    CB_KARARNAME = "CB_KARARNAME"
    YONETMELIK = "YONETMELIK"
    CB_YONETMELIK = "CB_YONETMELIK"
    CB_KARAR = "CB_KARAR"
    CB_GENELGE = "CB_GENELGE"
    KHK = "KHK"
    TUZUK = "TUZUK"
    KKY = "KKY"
    UY = "UY"
    TEBLIGLER = "TEBLIGLER"
    MULGA = "MULGA"

class SortFieldEnum(str, Enum):
    """Enum for sorting fields."""
    RESMI_GAZETE_TARIHI = "RESMI_GAZETE_TARIHI"
    KAYIT_TARIHI = "KAYIT_TARIHI"
    MEVZUAT_NUMARASI = "MEVZUAT_NUMARASI"

class SortDirectionEnum(str, Enum):
    """Enum for sort direction."""
    DESC = "desc"
    ASC = "asc"

class MevzuatSearchRequest(BaseModel):
    """Request model for searching legislation documents. Used by the client."""
    mevzuat_adi: Optional[str] = Field(None, description="The name of the legislation or a keyword to search for. For an exact phrase search, enclose the term in double quotes. E.g., 'ticaret' or '\"türk ceza kanunu\"'.")
    phrase: Optional[str] = Field(None, description="Search for this term in the FULL TEXT of the legislation. For an exact phrase search, enclose the term in double quotes.")
    mevzuat_no: Optional[str] = Field(None, description="The specific number of the legislation.")
    resmi_gazete_sayisi: Optional[str] = Field(None, description="The issue number of the Official Gazette.")
    mevzuat_tur_list: List[MevzuatTurEnum] = Field(
        default_factory=lambda: [tur for tur in MevzuatTurEnum],
        
        description="Filter by legislation type. IMPORTANT: Values must be one of the exact strings from the enum, using English characters only (e.g., 'YONETMELIK', not 'YÖNETMELİK'). Possible values: KANUN, CB_KARARNAME, YONETMELIK, CB_YONETMELIK, CB_KARAR, CB_GENELGE, KHK, TUZUK, KKY, UY, TEBLIGLER, MULGA. Defaults to all types."
    )
    page_number: int = Field(1, ge=1, description="The page number of the search results.")
    page_size: int = Field(10, ge=1, le=50, description="Number of results per page.")
    sort_field: SortFieldEnum = Field(
        SortFieldEnum.RESMI_GAZETE_TARIHI,
        description="Field to sort the results by. Possible values: RESMI_GAZETE_TARIHI, KAYIT_TARIHI, MEVZUAT_NUMARASI."
    )
    sort_direction: SortDirectionEnum = Field(
        SortDirectionEnum.DESC,
        description="Sorting direction. Possible values: DESC (descending, newest to oldest), ASC (ascending, oldest to newest)."
    )
    
class MevzuatTur(BaseModel):
    """Model for the legislation type object in search results."""
    id: int
    name: str
    description: str

class MevzuatDocument(BaseModel):
    """Model for a single legislation document found in search results."""
    mevzuat_id: str = Field(..., alias="mevzuatId")
    mevzuat_no: Optional[int] = Field(None, alias="mevzuatNo")
    mevzuat_adi: str = Field(..., alias="mevzuatAdi")
    mevzuat_tur: MevzuatTur = Field(..., alias="mevzuatTur")
    resmi_gazete_tarihi: Optional[datetime.datetime] = Field(None, alias="resmiGazeteTarihi")
    resmi_gazete_sayisi: Optional[str] = Field(None, alias="resmiGazeteSayisi")
    url: Optional[str] = None

class MevzuatSearchResult(BaseModel):
    """Model for the overall search result from the legislation API."""
    documents: List[MevzuatDocument]
    total_results: int
    current_page: int
    page_size: int
    total_pages: int
    query_used: Dict[str, Any]
    error_message: Optional[str] = None

class MevzuatArticleNode(BaseModel):
    """Recursive model for an article/section in the legislation's table of contents tree."""
    madde_id: str = Field(..., alias="maddeId")
    madde_no: Optional[int] = Field(None, alias="maddeNo")
    title: str
    description: Optional[str] = None
    children: List['MevzuatArticleNode'] = []
    mevzuat_id: str = Field(..., alias="mevzuatId")

MevzuatArticleNode.model_rebuild()

class MevzuatArticleContent(BaseModel):
    """Model for the content of a single legislation article."""
    madde_id: str
    mevzuat_id: str
    markdown_content: str
    error_message: Optional[str] = None