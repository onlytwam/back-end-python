from pydantic import BaseModel, model_validator
from typing import Optional, List, Any


# ── Inline slab sub-schema (used inside Create/Update/Response) ────────────────

class SlabData(BaseModel):
    minPrice:    float = 0
    maxPrice:    Optional[float] = None   # NULL = no upper limit
    cgst:        float
    sgst:        float
    totalGST:    Optional[float] = None   # auto-derived as cgst+sgst if omitted
    description: Optional[str]  = None

    @model_validator(mode="after")
    def fill_total(self) -> "SlabData":
        if self.totalGST is None:
            self.totalGST = round(self.cgst + self.sgst, 6)
        return self


# ── Create ─────────────────────────────────────────────────────────────────────

class TaxHSNCodeCreate(BaseModel):
    hsnCode:        str
    hsnDescription: str
    slab1:          SlabData
    slab2:          Optional[SlabData] = None
    slab3:          Optional[SlabData] = None
    userProfileId:  Optional[str]  = None
    state:          Optional[str]  = None
    isActive:       bool = True


# ── Update ─────────────────────────────────────────────────────────────────────

class TaxHSNCodeUpdate(BaseModel):
    taxHSNCodeId:   int
    hsnCode:        str
    hsnDescription: str
    slab1:          SlabData
    slab2:          Optional[SlabData] = None
    slab3:          Optional[SlabData] = None
    userProfileId:  Optional[str]  = None
    state:          Optional[str]  = None
    isActive:       bool = True


# ── Response ───────────────────────────────────────────────────────────────────

class SlabDataResponse(BaseModel):
    minPrice:    float
    maxPrice:    Optional[float] = None
    cgst:        float
    sgst:        float
    totalGST:    float
    description: Optional[str]  = None

    class Config:
        from_attributes = True


class TaxHSNCodeResponse(BaseModel):
    taxHSNCodeId:   int
    hsnCode:        Optional[str] = None
    hsnDescription: Optional[str] = None
    slab1:          Optional[SlabDataResponse] = None
    slab2:          Optional[SlabDataResponse] = None
    slab3:          Optional[SlabDataResponse] = None
    userProfileId:  Optional[str] = None
    state:          Optional[str] = None
    isActive:       bool

    class Config:
        from_attributes = True


class TaxHSNCodeListResponse(BaseModel):
    count:      int
    list:       List[TaxHSNCodeResponse]
    parameters: Optional[Any] = None


# ── Tax computation result (returned to callers / compute endpoint) ────────────

class TaxComputeResult(BaseModel):
    taxHSNCodeId: Optional[int]  = None
    hsnCode:      Optional[str]  = None
    cgstRate:     float = 0      # e.g. 2.5
    sgstRate:     float = 0      # e.g. 2.5
    totalGSTRate: float = 0      # e.g. 5.0
    cgstAmount:   float = 0      # cgstRate% of finalPrice
    sgstAmount:   float = 0      # sgstRate% of finalPrice
    taxAmount:    float = 0      # cgstAmount + sgstAmount
    slabApplied:  Optional[str]  = None   # description of matched slab