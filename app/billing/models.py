from pydantic import BaseModel, Field
from sqlalchemy import (
    BigInteger, Boolean, Column, DateTime, Integer,
    Numeric, String, ForeignKey, Text, func, text
)
from sqlalchemy.orm import Session, relationship, declarative_base
from database import  Base          # ← your DB session & Base
from datetime import datetime, timezone 


class BillingOrder(Base):
    __tablename__ = "BillingOrders"
    __table_args__ = {"schema": "twam"}
 
    OrderId        = Column(BigInteger, primary_key=True, autoincrement=True)
    InvoiceNo      = Column(String(50), unique=True, nullable=False)
    CustomerName   = Column(String(200))
    CustomerPhone  = Column(String(20))
    GSTMode        = Column(String(20), default="intrastate")   # intrastate | interstate
    PaymentMethod  = Column(String(20), default="cash")         # cash | upi | card
    Subtotal       = Column(Numeric(22, 6), default=0)
    CGSTTotal      = Column(Numeric(22, 6), default=0)
    SGSTTotal      = Column(Numeric(22, 6), default=0)
    IGSTTotal      = Column(Numeric(22, 6), default=0)
    GSTTotal       = Column(Numeric(22, 6), default=0)
    Discount       = Column(Numeric(22, 6), default=0)
    GrandTotal     = Column(Numeric(22, 6), default=0)
    ReceivedAmount = Column(Numeric(22, 6), default=0)
    Balance        = Column(Numeric(22, 6), default=0)
    CreatedDate    = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    CreatedBy      = Column(String(100))
 
    items = relationship("BillingOrderItem", back_populates="order", cascade="all, delete-orphan")
 
 
class BillingOrderItem(Base):
    __tablename__ = "BillingOrderItems"
    __table_args__ = {"schema": "twam"}
 
    OrderItemId   = Column(BigInteger, primary_key=True, autoincrement=True)
    OrderId       = Column(BigInteger, ForeignKey("twam.BillingOrders.OrderId"), nullable=False)
    StockId       = Column(String(100), nullable=False)
    ProductName   = Column(String(255))
    Size          = Column(String(50))
    Color         = Column(String(100))
    HSNCode       = Column(String(30))
    GSTPercent    = Column(Numeric(5, 2))
    Price         = Column(Numeric(22, 6))
    Quantity      = Column(Integer)
    Subtotal      = Column(Numeric(22, 6))
    GSTAmount     = Column(Numeric(22, 6))
    CGST          = Column(Numeric(22, 6))
    SGST          = Column(Numeric(22, 6))
    IGST          = Column(Numeric(22, 6))
    LineTotal     = Column(Numeric(22, 6))
 
    order = relationship("BillingOrder", back_populates="items")
 
 