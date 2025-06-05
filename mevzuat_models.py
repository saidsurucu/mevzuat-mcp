# mevzuat_models.py
"""
Pydantic models for the Adalet Bakanlığı Mevzuat MCP server.
Defines data structures for search requests, search results, and document content.
"""

from pydantic import BaseModel, Field, HttpUrl, field_validator
from typing import List, Optional, Dict, Any
from enum import Enum
import datetime
import json

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
    mevzuat_no: Optional[str] = Field(None, description="The specific number of the legislation.")
    resmi_gazete_sayisi: Optional[str] = Field(None, description="The issue number of the Official Gazette.")
    mevzuat_tur_list: List[MevzuatTurEnum] = Field(
        default_factory=lambda: [tur for tur in MevzuatTurEnum],
        description="Filter by legislation type. Possible values: KANUN (Law - Kanun), CB_KARARNAME (Presidential Decree - Cumhurbaşkanlığı Kararnamesi), YONETMELIK (Regulation - Yönetmelik), CB_YONETMELIK (Presidential Regulation - Cumhurbaşkanlığı Yönetmeliği), CB_KARAR (Presidential Decision - Cumhurbaşkanlığı Kararı), CB_GENELGE (Presidential Circular - Cumhurbaşkanlığı Genelgesi), KHK (Decree Law - Kanun Hükmünde Kararname), TUZUK (Statute/Bylaw - Tüzük), KKY (Institutional and Organizational Regulations - Kurum ve Kuruluş Yönetmelikleri), UY (Procedures and Regulations - Usul ve Yönetmelikler), TEBLIGLER (Communiqué - Tebliğler), MULGA (Repealed - Mülga). Defaults to all types."
    )
    search_in_title: bool = Field(default=False, description="When true, searches only within the legislation title.")
    exact_phrase: bool = Field(default=False, description="When true, searches for the exact phrase.")
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
    id: int
    name: str
    description: str

class MevzuatDocument(BaseModel):
    mevzuat_id: str = Field(..., alias="mevzuatId")
    mevzuat_no: Optional[int] = Field(None, alias="mevzuatNo")
    mevzuat_adi: str = Field(..., alias="mevzuatAdi")
    mevzuat_tur: MevzuatTur = Field(..., alias="mevzuatTur")
    resmi_gazete_tarihi: Optional[datetime.datetime] = Field(None, alias="resmiGazeteTarihi")
    resmi_gazete_sayisi: Optional[str] = Field(None, alias="resmiGazeteSayisi")
    url: Optional[str] = None

class MevzuatSearchResult(BaseModel):
    documents: List[MevzuatDocument]
    total_results: int
    current_page: int
    page_size: int
    total_pages: int
    query_used: Dict[str, Any]
    error_message: Optional[str] = None

class MevzuatArticleNode(BaseModel):
    madde_id: str = Field(..., alias="maddeId")
    madde_no: Optional[int] = Field(None, alias="maddeNo")
    title: str
    description: Optional[str] = None
    children: List['MevzuatArticleNode'] = []
    mevzuat_id: str = Field(..., alias="mevzuatId")

MevzuatArticleNode.model_rebuild()

class MevzuatArticleContent(BaseModel):
    madde_id: str
    mevzuat_id: str
    markdown_content: str
    error_message: Optional[str] = None

class MevzuatSearchToolArgs(BaseModel):
    """Pydantic model for the arguments of the 'search_mevzuat' tool."""
    mevzuat_adi: Optional[str] = Field(None, description="The name of the legislation or a keyword to search for. For an exact phrase search, enclose the term in double quotes. E.g., 'ticaret' or '\"türk ceza kanunu\"'.")
    mevzuat_no: Optional[str] = Field(None, description="The specific number of the legislation, e.g., '5237' for the Turkish Penal Code.")
    resmi_gazete_sayisi: Optional[str] = Field(None, description="The issue number of the Official Gazette where the legislation was published.")
    search_in_title: bool = Field(False, description="Set to true to search only within the legislation title, not the full text.")
    mevzuat_turleri: Optional[List[MevzuatTurEnum]] = Field(
        None,
        description="Filter by legislation type. Possible values: KANUN (Law - Kanun), CB_KARARNAME (Presidential Decree - Cumhurbaşkanlığı Kararnamesi), YONETMELIK (Regulation - Yönetmelik), CB_YONETMELIK (Presidential Regulation - Cumhurbaşkanlığı Yönetmeliği), CB_KARAR (Presidential Decision - Cumhurbaşkanlığı Kararı), CB_GENELGE (Presidential Circular - Cumhurbaşkanlığı Genelgesi), KHK (Decree Law - Kanun Hükmünde Kararname), TUZUK (Statute/Bylaw - Tüzük), KKY (Institutional and Organizational Regulations - Kurum ve Kuruluş Yönetmelikleri), UY (Procedures and Regulations - Usul ve Yönetmelikler), TEBLIGLER (Communiqué - Tebliğler), MULGA (Repealed - Mülga). If not provided, searches all types."
    )
    page_number: int = Field(1, ge=1, description="Page number for pagination.")
    page_size: int = Field(10, ge=1, le=50, description="Number of results to return per page.")
    sort_field: SortFieldEnum = Field(
        SortFieldEnum.RESMI_GAZETE_TARIHI,
        description="Field to sort results by. Possible values: RESMI_GAZETE_TARIHI, KAYIT_TARIHI, MEVZUAT_NUMARASI."
    )
    sort_direction: SortDirectionEnum = Field(
        SortDirectionEnum.DESC,
        description="Sorting direction. Possible values: DESC (descending, newest to oldest), ASC (ascending, oldest to newest)."
    )

    @field_validator("mevzuat_turleri", mode='before')
    @classmethod
    def parse_json_string(cls, v: Any) -> Any:
        """Tries to parse a string value into a JSON list."""
        if isinstance(v, str):
            try:
                return json.loads(v)
            except json.JSONDecodeError:
                raise ValueError(f"'{v}' is not a valid list format.")
        return v