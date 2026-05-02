from __future__ import annotations

from typing import List, Optional
from pydantic import BaseModel, Field, field_validator


# ─────────────────────────────────────────────
# Shared
# ─────────────────────────────────────────────

class FileUploadModel(BaseModel):
    fileUploadId: Optional[int]  = None
    fileName:     Optional[str]  = None
    filePath:     Optional[str]  = None
    fileData:     Optional[str]  = None
    fileType:     Optional[str]  = None


class PaginationParams(BaseModel):
    index: int = Field(1,  ge=1)
    size:  int = Field(20, ge=1, le=100)


# ─────────────────────────────────────────────
# Brand
# ─────────────────────────────────────────────

class BrandBase(BaseModel):
    brandName:        str            = Field(..., min_length=1, max_length=255)
    brandDescription: Optional[str]  = Field(None, max_length=2000)
    userProfileId:    Optional[str]  = None
    state:            Optional[str]  = None
    isActive:         bool           = True

    @field_validator("brandName", mode="before")
    @classmethod
    def strip_brand_name(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("brandName must not be empty")
        return v.strip()

    @field_validator("brandDescription", mode="before")
    @classmethod
    def strip_description(cls, v: Optional[str]) -> Optional[str]:
        return v.strip() if v and v.strip() else None


class BrandCreate(BrandBase):
    brandLogoFileModel:  Optional[FileUploadModel] = None
    brandImageFileModel: Optional[FileUploadModel] = None


class BrandUpdate(BaseModel):
    """All fields optional for PATCH-style updates."""
    brandName:        Optional[str]  = Field(None, min_length=1, max_length=255)
    brandDescription: Optional[str]  = Field(None, max_length=2000)
    userProfileId:    Optional[str]  = None
    state:            Optional[str]  = None
    isActive:         Optional[bool] = None
    brandLogoFileModel:  Optional[FileUploadModel] = None
    brandImageFileModel: Optional[FileUploadModel] = None

    @field_validator("brandName", mode="before")
    @classmethod
    def strip_brand_name(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        if not v.strip():
            raise ValueError("brandName must not be empty")
        return v.strip()

    @field_validator("brandDescription", mode="before")
    @classmethod
    def strip_description(cls, v: Optional[str]) -> Optional[str]:
        return v.strip() if v and v.strip() else None


class BrandResponse(BaseModel):
    brandId:          int
    brandName:        str
    brandDescription: Optional[str]  = None
    brandLogo:        Optional[str]  = None
    brandImage:       Optional[str]  = None
    userProfileId:    Optional[str]  = None
    state:            Optional[str]  = None
    isActive:         bool           = True

    model_config = {"from_attributes": True}


class BrandListResponse(BaseModel):
    count:      int
    list:       List[BrandResponse]
    parameters: Optional[dict] = None