# mevzuat_models_new.py
"""
Pydantic models for mevzuat.gov.tr MCP server.
Defines data structures for search requests and results from the new API.
"""

from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any, Literal

# Mevzuat types from mevzuat.gov.tr
MevzuatTurLiteral = Literal[
    "Kanun",
    "KHK",
    "Tuzuk",
    "Kurum Yönetmeliği",
    "Cumhurbaşkanlığı Kararnamesi",
    "Cumhurbaşkanı Kararı",
    "CB Yönetmeliği",
    "CB Genelgesi",
    "Tebliğ",
    "Diğer"
]

# Search location options
AranacakYerLiteral = Literal[
    "1",  # Başlık
    "2",  # Madde Başlığı
    "3",  # Tüm Metin
]


class MevzuatSearchRequestNew(BaseModel):
    """Request model for searching legislation on mevzuat.gov.tr"""

    mevzuat_tur: MevzuatTurLiteral = Field(
        "Kanun",
        description="Type of legislation. Currently only 'Kanun' (laws) are fully supported for content extraction."
    )

    aranacak_ifade: Optional[str] = Field(
        None,
        description="Search term or phrase to look for in legislation"
    )

    aranacak_yer: int = Field(
        3,
        ge=1,
        le=3,
        description="Where to search: 1=Title only, 2=Article titles, 3=Full text (default)"
    )

    tam_cumle: bool = Field(
        False,
        description="Exact phrase match (true) or any word match (false, default)"
    )

    mevzuat_no: Optional[str] = Field(
        None,
        description="Specific legislation number to search for"
    )

    baslangic_tarihi: Optional[str] = Field(
        None,
        description="Start date for filtering (format: DD.MM.YYYY)"
    )

    bitis_tarihi: Optional[str] = Field(
        None,
        description="End date for filtering (format: DD.MM.YYYY)"
    )

    page_number: int = Field(
        1,
        ge=1,
        description="Page number of results"
    )

    page_size: int = Field(
        10,
        ge=1,
        le=100,
        description="Number of results per page"
    )


class MevzuatDocumentNew(BaseModel):
    """Model for a single legislation document from mevzuat.gov.tr"""

    mevzuat_no: str = Field(..., description="Legislation number")
    mev_adi: str = Field(..., description="Legislation title/name")
    kabul_tarih: Optional[str] = Field(None, description="Acceptance date")
    resmi_gazete_tarihi: Optional[str] = Field(None, description="Official Gazette publication date")
    resmi_gazete_sayisi: Optional[str] = Field(None, description="Official Gazette issue number")
    mevzuat_tertip: str = Field(..., description="Legislation series/order")
    mevzuat_tur: int = Field(..., description="Legislation type code")
    url: str = Field(..., description="Relative URL to view the legislation")

    def get_pdf_url(self) -> str:
        """Generate PDF download URL for this legislation (only works for Kanun)."""
        return f"https://www.mevzuat.gov.tr/MevzuatMetin/{self.mevzuat_tur}.{self.mevzuat_tertip}.{self.mevzuat_no}.pdf"

    def get_web_url(self) -> str:
        """Generate web page URL for this legislation."""
        return f"https://www.mevzuat.gov.tr/{self.url}"


class MevzuatSearchResultNew(BaseModel):
    """Model for search results from mevzuat.gov.tr"""

    documents: List[MevzuatDocumentNew]
    total_results: int
    current_page: int
    page_size: int
    total_pages: int
    query_used: Dict[str, Any]
    error_message: Optional[str] = None


class MevzuatArticleContent(BaseModel):
    """Model for the content of legislation (reused from old models)."""
    madde_id: str
    mevzuat_id: str
    markdown_content: str
    error_message: Optional[str] = None
