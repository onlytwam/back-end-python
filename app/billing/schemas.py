from pydantic import BaseModel, Field, field_validator
from typing import List, Optional
from datetime import datetime
from enum import Enum


# ── Auth ──────────────────────────────────────────────────────────────────────
class TokenPayload(BaseModel):
    sub:          str                  # = user_id
    role_id:      int           = 0
    store_id:     Optional[int] = None
    warehouse_id: Optional[int] = None


# ── Inventory ─────────────────────────────────────────────────────────────────
class InventoryItem(BaseModel):
    inventoryId:  int
    stockId:      str
    storeId:      Optional[int]
    warehouseId:  Optional[int]
    locationName: str
    locationType: str
    quantity:     int
    available:    int
    productName:  Optional[str] = None
    productCode:  Optional[str] = None
    colorName:    Optional[str] = None
    sizeName:     Optional[str] = None

    model_config = {"from_attributes": True}

class AssignStockIn(BaseModel):
    stockId: str
    storeId: Optional[int] = None
    warehouseId: Optional[int] = None
    quantity: int



# ── Product lookup ────────────────────────────────────────────────────────────
class ProductLookupOut(BaseModel):
    stockId:           str
    productName:       str
    size:              str
    color:             str
    price:             float
    mrpPrice:          float
    hsnCode:           str
    gstPercent:        float
    availableQuantity: int


# ── Billing line item ─────────────────────────────────────────────────────────
class BillLineIn(BaseModel):
    stockId:     str
    productName: str
    size:        str   = ""
    color:       str   = ""
    hsnCode:     str   = ""
    gstPercent:  float
    price:       float
    quantity:    int   = Field(..., ge=1)
    subtotal:    float
    gstAmount:   float
    cgst:        float
    sgst:        float
    igst:        float
    lineTotal:   float


# ── Warehouse response ────────────────────────────────────────────────────────
class WarehouseOut(BaseModel):
    warehouseId:   int
    warehouseName: str
    address:       Optional[str]
    contactPhone:  Optional[str]
    isActive:      bool
    loginEmail:    Optional[str] = None
    loginPassword: Optional[str] = None

    model_config = {"from_attributes": True}

class WarehouseCreate(BaseModel):
    warehouseName: str
    address:       Optional[str] = None
    contactPhone:  Optional[str] = None
    isActive:      bool = True
    loginEmail:    Optional[str] = None
    loginPassword: Optional[str] = None
    loginFirstName: Optional[str] = None
    loginLastName: Optional[str] = None
    loginPhone:    Optional[str] = None


# ── Create bill ───────────────────────────────────────────────────────────────
class CreateBillIn(BaseModel):
    customerName:   Optional[str] = None
    customerPhone:  Optional[str] = None
    gstMode:        str           = "intrastate"
    paymentMethod:  str           = "cash"
    channel:        str                          # matches SalesChannel.ChannelCode in DB
    storeId:        Optional[int] = None         # super admin supplies this for POS orders
    warehouseId:    Optional[int] = None         # required for online channel orders
    items:          List[BillLineIn]
    subtotal:       float
    cgstTotal:      float
    sgstTotal:      float
    igstTotal:      float
    gstTotal:       float
    discount:       float = 0
    grandTotal:     float
    receivedAmount: float = 0
    balance:        float = 0
    createdBy:      Optional[str] = None

    # NOTE: warehouse validation is done in the endpoint (needs DB channel lookup),
    # not here, because channel is a free string matched against the SalesChannel table.


class CreateBillOut(BaseModel):
    orderId:   int
    invoiceNo: str
    message:   str


class StoreOut(BaseModel):
    storeId:   int
    storeName: str
    address:   str = ""
    phone:     str = ""
    city:      str = ""
    state:     str = ""
    pinCode:   str = ""

    model_config = {"from_attributes": True}
class WarehouseUpdate(BaseModel):
    warehouseName: Optional[str] = None
    address:       Optional[str] = None
    isActive:      Optional[bool] = None
    loginEmail:    Optional[str] = None
    loginPassword: Optional[str] = None

