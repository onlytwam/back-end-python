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
    taxHSNCodeId:    Optional[int]  = None
    # Fabric is now a product-level field — FK → mdm.Fabric.FabricId
    fabricId:        Optional[int]  = None
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
    taxHSNCodeId:    Optional[int]  = None
    fabricId:        Optional[int]  = None
    personalId:      Optional[int]  = None
    userProfileId:   Optional[str]  = None
    state:           Optional[str]  = None
    tag:             Optional[str]  = None
    modifiedBy:      Optional[str]  = None


# ── Response ──────────────────────────────────────────────────────────────────


class ProductImageResponse(BaseModel):
    productImageId: Optional[int] = None
    fileName:       Optional[str] = None
    filePath:       Optional[str] = None
    fileType:       Optional[str] = None
    fileData:       Optional[str] = None

    class Config:
        from_attributes = True


class ProductVariantSummary(BaseModel):
    productVariantId:   int
    productId:          int
    variantName:        Optional[str] = None
    variantDescription: Optional[str] = None
    # productCode removed — it belongs to the Product, not the Variant
    color:              Optional[str] = None
    colorName:          Optional[str] = None
    colorFamilyId:      Optional[int] = None
    colorFamily:        Optional[str] = None
    colorFamilyHex:     Optional[str] = None
    colorHex:           Optional[str] = None
    isBestSeller:       Optional[bool] = None
    isReturnAvailable:  Optional[bool] = None
    isCupSize:          Optional[bool] = None
    isActive:           Optional[bool] = True
    deletedInd:         Optional[bool] = False
    createdDate:        Optional[datetime] = None
    modifiedDate:       Optional[datetime] = None
    images:             Optional[List[ProductImageResponse]] = []

    class Config:
        from_attributes = True


class ProductResponse(BaseModel):
    productId:        int
    productCode:      Optional[str]  = None
    name:             Optional[str]  = None
    description:      Optional[str]  = None
    categoryId:       Optional[int]  = None
    childCategoryId:  Optional[int]  = None
    brandId:          Optional[int]  = None
    taxHSNCodeId:     Optional[int]  = None   # FK → mdm.TaxHSNCode
    taxHSNCode:       Optional[str]  = None   # resolved HSN code string
    taxHSNDescription: Optional[str] = None   # resolved HSN description
    totalGST:         Optional[float]= None   # resolved GST %
    # Fabric — product-level, FK → mdm.Fabric
    fabricId:         Optional[int]  = None
    fabricName:       Optional[str]  = None   # resolved fabric name (joined)
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
    categoryIsActive:    Optional[bool] = None
    categoryDeletedInd:  Optional[bool] = None
    # First image URL (variant-level, falls back to product-level)
    imageUrl:            Optional[str]  = None
    # Variant collection for admin cards/user-style swatches
    allVariants:         Optional[List[ProductVariantSummary]] = []

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
