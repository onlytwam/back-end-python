from datetime import datetime
from typing import Optional, List, Any
from pydantic import BaseModel, Field


class ProductColorCreate(BaseModel):
    colorFamilyName: str = Field(..., min_length=1)
    hexCode: str = Field(..., min_length=1)
    description: Optional[str] = None
    orderNo: Optional[int] = None
    state: Optional[str] = None
    isActive: bool = True
    createdBy: Optional[str] = None
    organizationId: Optional[int] = None


class ProductColorUpdate(BaseModel):
    colorFamilyName: Optional[str] = None
    hexCode: Optional[str] = None
    description: Optional[str] = None
    orderNo: Optional[int] = None
    state: Optional[str] = None
    isActive: Optional[bool] = None
    modifiedBy: Optional[str] = None
    organizationId: Optional[int] = None


class ProductColorResponse(BaseModel):
    productColorId: int
    colorFamilyId: int
    colorFamilyName: str
    name: str
    hexCode: str
    hex: str
    description: Optional[str] = None
    orderNo: Optional[int] = None
    state: Optional[str] = None
    isActive: bool = True
    createdDate: Optional[datetime] = None
    modifiedDate: Optional[datetime] = None
    deletedInd: bool = False
    createdBy: Optional[str] = None
    modifiedBy: Optional[str] = None
    organizationId: Optional[int] = None


class ProductColorListResponse(BaseModel):
    count: int
    list: List[ProductColorResponse]
    parameters: Optional[Any] = None
