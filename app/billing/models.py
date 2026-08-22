from __future__ import annotations
from datetime import datetime, timezone
from sqlalchemy import (
    Column, Integer, String, Boolean, DateTime,
    Numeric, ForeignKey, CheckConstraint, UniqueConstraint, Text, BigInteger
)
from sqlalchemy.orm import relationship
from database import Base


class Warehouse(Base):
    __tablename__ = "Warehouses"
    __table_args__ = {"schema": "twam", "extend_existing": True}

    WarehouseId   = Column(Integer, primary_key=True, index=True)
    WarehouseName = Column(String(120), nullable=False)
    Address       = Column(Text)
    ContactPhone  = Column(String(20))
    IsActive      = Column(Boolean, default=True, nullable=False)
    LoginEmail    = Column(String(255), nullable=True)
    LoginPassword = Column(String(255), nullable=True)
    CreatedDate   = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    ModifiedDate  = Column(DateTime(timezone=True), onupdate=lambda: datetime.now(timezone.utc))

    inventory = relationship("Inventory", back_populates="warehouse")
    people = relationship("People", back_populates="warehouse")
    
class Inventory(Base):
    __tablename__ = "Inventory"
    __table_args__ = (
        CheckConstraint(
            "(\"StoreId\" IS NOT NULL AND \"WarehouseId\" IS NULL)"
            " OR "
            "(\"StoreId\" IS NULL AND \"WarehouseId\" IS NOT NULL)",
            name="chk_inventory_location",
        ),
        UniqueConstraint("StockId", "StoreId",     name="uq_inventory_stock_store"),
        UniqueConstraint("StockId", "WarehouseId", name="uq_inventory_stock_warehouse"),
        {"schema": "twam", "extend_existing": True},
    )

    InventoryId            = Column(Integer, primary_key=True, index=True)
    StockId                = Column(String(60), nullable=False, index=True)
    StoreId                = Column(Integer, ForeignKey("twam.Stores.StoreId"),         nullable=True, index=True)
    WarehouseId            = Column(Integer, ForeignKey("twam.Warehouses.WarehouseId"), nullable=True, index=True)
    ProductVariantDetailId = Column(Integer, ForeignKey("twam.ProductVariantDetail.ProductVariantDetailId"), nullable=True, index=True)  # ← new
    Quantity               = Column(Integer, nullable=False, default=0)
    ReservedQty            = Column(Integer, nullable=False, default=0)
    CreatedDate            = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    ModifiedDate           = Column(DateTime(timezone=True), onupdate=lambda: datetime.now(timezone.utc))

    store                  = relationship("Stores",               back_populates="inventory", foreign_keys=[StoreId])
    warehouse              = relationship("Warehouse",            back_populates="inventory", foreign_keys=[WarehouseId])
    product_variant_detail = relationship("ProductVariantDetail", foreign_keys=[ProductVariantDetailId])  # ← new

    @property
    def available(self) -> int:
        return max(0, (self.Quantity or 0) - (self.ReservedQty or 0))
    

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
    Channel    = Column(String(20), nullable=True,  default="POS")
    StoreId     = Column(Integer,    nullable=True)
    WarehouseId = Column(Integer,    nullable=True)

 
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
 
 
