# ── Response: product lookup ──────────────────────────────────
from datetime import datetime
from typing import Optional,List,Any
from pydantic import BaseModel,Field

from sqlalchemy.orm import Session, relationship, declarative_base


class ProductLookupOut(BaseModel):
    stockId:           str
    productName:       str
    size:              str
    color:             str
    price:             float
    hsnCode:           str
    gstPercent:        float
    availableQuantity: int
    mrpPrice:          float
 
    class Config:
        from_attributes = True
 
 
# ── Request: create order ─────────────────────────────────────
class OrderItemIn(BaseModel):
    stockId:     str
    productName: str
    size:        str
    color:       str
    hsnCode:     str
    gstPercent:  float
    price:       float
    quantity:    int   = Field(..., ge=1)
    subtotal:    float
    gstAmount:   float
    cgst:        float
    sgst:        float
    igst:        float
    lineTotal:   float
 
 
class CreateOrderIn(BaseModel):
    customerName:   Optional[str] = None
    customerPhone:  Optional[str] = None
    gstMode:        str = "intrastate"   # intrastate | interstate
    paymentMethod:  str = "cash"
    items:          List[OrderItemIn] = Field(..., min_length=1)
    subtotal:       float
    gstTotal:       float
    cgstTotal:      float
    sgstTotal:      float
    igstTotal:      float
    discount:       float = 0
    grandTotal:     float
    receivedAmount: float = 0
    balance:        float = 0
    createdBy:      Optional[str] = None
 
 
# ── Response: create order ────────────────────────────────────
class CreateOrderOut(BaseModel):
    orderId:   int
    invoiceNo: str
    message:   str
 
 
# ── Response: order list ──────────────────────────────────────
class OrderListItem(BaseModel):
    orderId:      int
    invoiceNo:    str
    customerName: Optional[str]
    grandTotal:   float
    paymentMethod: str
    createdDate:  datetime
 
    class Config:
        from_attributes = True
 
 
# ── Response: order detail ────────────────────────────────────
class OrderItemOut(BaseModel):
    stockId:    str
    productName: str
    size:       str
    color:      str
    hsnCode:    str
    gstPercent: float
    price:      float
    quantity:   int
    lineTotal:  float
 
    class Config:
        from_attributes = True
 
 
class OrderDetailOut(BaseModel):
    orderId:       int
    invoiceNo:     str
    customerName:  Optional[str]
    customerPhone: Optional[str]
    gstMode:       str
    paymentMethod: str
    subtotal:      float
    cgstTotal:     float
    sgstTotal:     float
    igstTotal:     float
    gstTotal:      float
    discount:      float
    grandTotal:    float
    receivedAmount: float
    balance:       float
    createdDate:   datetime
    items:         List[OrderItemOut]
 
    class Config:
        from_attributes = True
 
 