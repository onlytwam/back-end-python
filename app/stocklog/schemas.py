from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime


class StockLogCreate(BaseModel):
    productVariantDetailId: int
    productId:              Optional[int]  = None
    productVariantId:       Optional[int]  = None
    stockId:                Optional[str]  = None
    changeType:             str
    previousValue:          Optional[int]  = None
    newValue:               Optional[int]  = None
    totalStock:             Optional[int]  = None
    processedQuantity:      Optional[int]  = None
    returnedQuantity:       Optional[int]  = None
    availableQuantity:      Optional[int]  = None
    note:                   Optional[str]  = None
    changedBy:              Optional[str]  = None


class StockLogResponse(BaseModel):
    stockLogId:             int
    productVariantDetailId: int
    productId:              Optional[int]   = None
    productVariantId:       Optional[int]   = None
    stockId:                Optional[str]   = None
    changeType:             str
    previousValue:          Optional[int]   = None
    newValue:               Optional[int]   = None
    totalStock:             Optional[int]   = None
    processedQuantity:      Optional[int]   = None
    returnedQuantity:       Optional[int]   = None
    availableQuantity:      Optional[int]   = None
    note:                   Optional[str]   = None
    changedBy:              Optional[str]   = None
    createdDate:            Optional[datetime] = None

    class Config:
        from_attributes = True


class StockUpdateRequest(BaseModel):
    stockQuantity:     Optional[int] = None
    processedQuantity: Optional[int] = None
    returnedQuantity:  Optional[int] = None
    note:              Optional[str] = None
    changedBy:         Optional[str] = None


class StockSummaryItem(BaseModel):
    productVariantDetailId: int
    productId:              int
    productVariantId:       int
    productCode:            Optional[str]   = None
    productName:            Optional[str]   = None
    variantName:            Optional[str]   = None
    color:                  Optional[str]   = None
    sizeLabel:              Optional[str]   = None
    cupSizeLabel:           Optional[str]   = None
    stockId:                Optional[str]   = None
    totalStock:             Optional[int]   = None
    processedQuantity:      Optional[int]   = None
    returnedQuantity:       Optional[int]   = None
    availableQuantity:      Optional[int]   = None
    isLowStock:             Optional[bool]  = None
    isOutOfStock:           Optional[bool]  = None
    image:                  Optional[str]   = None