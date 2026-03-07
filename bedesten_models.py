"""
Pydantic models for bedesten.adalet.gov.tr Mevzuat API.
"""
from enum import Enum
from pydantic import BaseModel, Field
from typing import List, Optional, Any


class MevzuatTurEnum(str, Enum):
    """Legislation types supported by the bedesten API."""
    KANUN = "KANUN"
    CB_KARARNAME = "CB_KARARNAME"
    YONETMELIK = "YONETMELIK"  # Bakanlar Kurulu yönetmelikleri
    CB_YONETMELIK = "CB_YONETMELIK"
    CB_KARAR = "CB_KARAR"
    CB_GENELGE = "CB_GENELGE"
    KHK = "KHK"
    TUZUK = "TUZUK"
    KKY = "KKY"  # Kurum ve Kuruluş yönetmelikleri
    UY = "UY"  # Üniversite yönetmelikleri
    TEBLIGLER = "TEBLIGLER"
    MULGA = "MULGA"  # Mülga kanunlar


class BedMevzuatTurInfo(BaseModel):
    """Legislation type info embedded in search results."""
    id: int
    name: str
    description: Optional[str] = None

    model_config = {"populate_by_name": True}


class BedMevzuatDocument(BaseModel):
    """A legislation document from search results."""
    mevzuat_id: str = Field(..., alias="mevzuatId")
    mevzuat_no: Any = Field(..., alias="mevzuatNo")  # can be int or str
    mevzuat_adi: str = Field(..., alias="mevzuatAdi")
    mevzuat_tur: Optional[Any] = Field(None, alias="mevzuatTur")
    mevzuat_tertip: Optional[Any] = Field(None, alias="mevzuatTertip")
    gerekce_id: Optional[str] = Field(None, alias="gerekceId")
    ekler: Optional[List[str]] = None
    resmi_gazete_tarihi: Optional[str] = Field(None, alias="resmiGazeteTarihi")
    resmi_gazete_sayisi: Optional[str] = Field(None, alias="resmiGazeteSayisi")
    url: Optional[str] = None
    mukerrer: Optional[str] = None

    model_config = {"populate_by_name": True}


class BedSearchResult(BaseModel):
    """Search results from bedesten API."""
    documents: List[BedMevzuatDocument] = []
    total_results: int = 0
    start: int = 0
    query_used: str = ""
    error_message: Optional[str] = None


class BedMaddeNode(BaseModel):
    """A node in the article tree (table of contents)."""
    madde_id: Optional[Any] = Field(None, alias="maddeId")
    madde_no: Optional[Any] = Field(None, alias="maddeNo")
    title: Optional[str] = None
    description: Optional[str] = None
    madde_baslik: Optional[str] = Field(None, alias="maddeBaslik")
    gerekce_id: Optional[Any] = Field(None, alias="gerekceId")
    children: List["BedMaddeNode"] = []

    model_config = {"populate_by_name": True}


class BedDocumentContent(BaseModel):
    """Document content from getDocumentContent endpoint."""
    content: str = ""  # decoded HTML/text
    mime_type: Optional[str] = None
    error_message: Optional[str] = None


class BedGerekceContent(BaseModel):
    """Law rationale (gerekçe) content."""
    gerekce_id: Optional[str] = None
    mevzuat_id: Optional[str] = None
    content: str = ""  # decoded HTML/text
    mime_type: Optional[str] = None
    error_message: Optional[str] = None
