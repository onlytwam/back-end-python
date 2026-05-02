# app/saleschannel/schemas.py

from pydantic import BaseModel, Field, field_validator
from typing import Optional
from datetime import datetime
from pydantic.alias_generators import to_camel



VALID_STOCK_SOURCES = {"store", "warehouse"}


class SalesChannelBase(BaseModel):
    channelName:   str            = Field(..., min_length=1, max_length=60)
    emoji:         Optional[str]  = None
    stockSource:   str            = Field(..., description="store or warehouse")
    commissionPct: float          = Field(0, ge=0, le=100)
    isActive:      bool           = True
    sortOrder:     int            = 0

    @field_validator("stockSource")
    @classmethod
    def validate_stock_source(cls, v: str) -> str:
        if v not in VALID_STOCK_SOURCES:
            raise ValueError(f"stockSource must be one of {VALID_STOCK_SOURCES}")
        return v


class SalesChannelCreateIn(SalesChannelBase):
    channelCode: str = Field(..., min_length=1, max_length=30)

    @field_validator("channelCode")
    @classmethod
    def uppercase_code(cls, v: str) -> str:
        return v.strip().upper()


class SalesChannelUpdateIn(BaseModel):
    """All fields optional — PATCH semantics."""
    channelName:   Optional[str]   = None
    emoji:         Optional[str]   = None
    stockSource:   Optional[str]   = None
    commissionPct: Optional[float] = Field(None, ge=0, le=100)
    isActive:      Optional[bool]  = None
    sortOrder:     Optional[int]   = None

    @field_validator("stockSource")
    @classmethod
    def validate_stock_source(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in VALID_STOCK_SOURCES:
            raise ValueError(f"stockSource must be one of {VALID_STOCK_SOURCES}")
        return v


class SalesChannelOut(BaseModel):
    channelId:     int
    channelCode:   str
    channelName:   str
    emoji:         Optional[str]
    stockSource:   str
    commissionPct: float
    isActive:      bool
    sortOrder:     int
    createdDate:   datetime
    modifiedDate:  Optional[datetime]

    model_config = {"from_attributes": True}




class SalesChannelResponse(BaseModel):
    channelId:     int      = Field(alias="ChannelId")
    channelCode:   str      = Field(alias="ChannelCode")
    channelName:   str      = Field(alias="ChannelName")
    emoji:         str | None = Field(default=None, alias="Emoji")
    stockSource:   str      = Field(alias="StockSource")
    commissionPct: float    = Field(alias="CommissionPct")
    isActive:      bool     = Field(alias="IsActive")
    sortOrder:     int      = Field(alias="SortOrder")
    createdDate:   datetime = Field(alias="CreatedDate")
    modifiedDate:  datetime | None = Field(default=None, alias="ModifiedDate")

    model_config = {
        "from_attributes": True,
        "populate_by_name": True,
    }


class SalesChannelCreate(BaseModel):
    channelCode: str
    channelName: str
    emoji: str
    stockSource: str
    commissionPct: float
    isActive: bool
    sortOrder: int