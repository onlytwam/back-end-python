from pydantic import BaseModel
from typing import Optional, List, Any
from datetime import datetime
# from __future__ import annotations
from decimal import Decimal
from typing import List, Optional
from pydantic import BaseModel, field_validator

from typing import List, Optional, Any
class ReportListResponse(BaseModel):
    count: int
    list: List[Any]
    parameters: Optional[Any] = None


class ProductCategoryReportItem(BaseModel):
    id: Optional[int] = None
    name: str
    type: Optional[str] = None
    totalOrders: int = 0
    unitsSold: int = 0
    grossRevenue: float = 0.0
    taxableValue: float = 0.0
    gstAmount: float = 0.0




class StockLevelFilters(BaseModel):
    """All optional filter fields parsed from the Filters query param (JSON)."""
    search: Optional[str] = None          # fuzzy match on ProductName / StockId
    categoryId: Optional[int] = None
    isLowStock: Optional[bool] = None
    isOutOfStock: Optional[bool] = None
    minStock: Optional[int] = None
    maxStock: Optional[int] = None


class StockLevelItem(BaseModel):
    ProductVariantDetailId: int
    StockId:                Optional[str]   = None
    ProductName:            Optional[str]   = None
    VariantName:            Optional[str]   = None
    CategoryName:           Optional[str]   = None
    StockQuantity:          Optional[int]   = None
    AvailableQuantity:      Optional[int]   = None
    ProcessedQuantity:      Optional[int]   = None
    AmendmentQuantity:      Optional[int]   = None
    MRPPrice:               Optional[float] = None
    FinalPrice:             Optional[float] = None
    DiscountPercent:        Optional[int]   = None
    IsLowStock:             Optional[bool]  = None
    IsOutOfStock:           Optional[bool]  = None

    # ── coerce Decimal/str numbers coming from DB ──────────────────────────
    @field_validator(
        "StockQuantity", "AvailableQuantity", "ProcessedQuantity",
        "AmendmentQuantity", "DiscountPercent",
        mode="before"
    )
    @classmethod
    def coerce_int(cls, v: Any) -> Optional[int]:
        if v is None: return None
        try: return int(v)
        except: return None

    @field_validator("MRPPrice", "FinalPrice", mode="before")
    @classmethod
    def coerce_float(cls, v: Any) -> Optional[float]:
        if v is None: return None
        try: return float(v)
        except: return None

    model_config = {"from_attributes": True}


class StockLevelReportResponse(BaseModel):
    TotalCount: int
    Page:       int
    PageSize:   int
    Data:       List[StockLevelItem]

class InventoryAgingFilters(BaseModel):
    search:     Optional[str] = None
    categoryId: Optional[int] = None
    agingBucket: Optional[str] = None  # "0-30" | "31-90" | "91-180" | "180+"


class InventoryAgingItem(BaseModel):
    ProductVariantDetailId: int
    StockId:        Optional[str]   = None
    ProductName:    Optional[str]   = None
    VariantName:    Optional[str]   = None
    CategoryName:   Optional[str]   = None
    ReceivedDate:   Optional[str]   = None
    DaysInStock:    Optional[int]   = None
    AgingBucket:    Optional[str]   = None
    Quantity:       Optional[int]   = None
    StockQuantity:  Optional[int]   = None
    MRPPrice:       Optional[float] = None
    FinalPrice:     Optional[float] = None

    @field_validator("DaysInStock", "Quantity", "StockQuantity", mode="before")
    @classmethod
    def coerce_int(cls, v: Any) -> Optional[int]:
        if v is None: return None
        try: return int(v)
        except: return None

    @field_validator("MRPPrice", "FinalPrice", mode="before")
    @classmethod
    def coerce_float(cls, v: Any) -> Optional[float]:
        if v is None: return None
        try: return float(v)
        except: return None

    model_config = {"from_attributes": True}


class InventoryAgingReportResponse(BaseModel):
    TotalCount: int
    Page:       int
    PageSize:   int
    Data:       List[InventoryAgingItem]