from pydantic import BaseModel
from typing import Optional, List, Any
from datetime import datetime


# ── Create ────────────────────────────────────────────────────────────────────

class ProductCreate(BaseModel):
    productCode:     Optional[str]  = None
    name:            Optional[str]  = None
    description:     Optional[str]  = None
    categoryId:      Optional[int]  = None
    childCategoryId: Optional[int]  = None
    brandId:         Optional[int]  = None
    personalId:      Optional[int]  = None
    userProfileId:   Optional[str]  = None
    state:           Optional[str]  = None
    tag:             Optional[str]  = None
    createdBy:       Optional[str]  = None
    modifiedBy:      Optional[str]  = None


# ── Update ────────────────────────────────────────────────────────────────────

class ProductUpdate(BaseModel):
    productCode:     Optional[str]  = None
    name:            Optional[str]  = None
    description:     Optional[str]  = None
    categoryId:      Optional[int]  = None
    childCategoryId: Optional[int]  = None
    brandId:         Optional[int]  = None
    personalId:      Optional[int]  = None
    userProfileId:   Optional[str]  = None
    state:           Optional[str]  = None
    tag:             Optional[str]  = None
    modifiedBy:      Optional[str]  = None


# ── Response ──────────────────────────────────────────────────────────────────

class ProductResponse(BaseModel):
    productId:        int
    productCode:      Optional[str]  = None
    name:             Optional[str]  = None
    description:      Optional[str]  = None
    categoryId:       Optional[int]  = None
    childCategoryId:  Optional[int]  = None
    brandId:          Optional[int]  = None
    personalId:       Optional[int]  = None
    userProfileId:    Optional[str]  = None
    tag:              Optional[str]  = None
    state:            Optional[str]  = None
    createdBy:        Optional[str]  = None
    modifiedBy:       Optional[str]  = None
    createdDate:      Optional[datetime] = None
    modifiedDate:     Optional[datetime] = None
    # Own active/deleted status
    isActive:         Optional[bool] = True
    deletedInd:       Optional[bool] = False
    # Joined fields from Brand — required by admin UI to show Active/Inactive status
    brandName:        Optional[str]  = None
    brandIsActive:    Optional[bool] = None   # False = brand is hidden from public
    brandDeletedInd:  Optional[bool] = None   # True  = brand has been soft-deleted
    # Joined fields from Category
    categoryName:        Optional[str]  = None
    categoryIsActive:    Optional[bool] = None   # False = category is inactive or deleted
    categoryDeletedInd:  Optional[bool] = None   # True  = category has been soft-deleted
    # First image URL (variant-level, falls back to product-level)
    imageUrl:            Optional[str]  = None

    class Config:
        from_attributes = True


class ProductListResponse(BaseModel):
    count:      int
    list:       List[ProductResponse]
    parameters: Optional[Any] = None


# ── Validation ────────────────────────────────────────────────────────────────

class ProductValidationRequest(BaseModel):
    productId: int
    state:     Optional[str] = None


class ProductValidationResponse(BaseModel):
    isValid: bool
