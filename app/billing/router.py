from __future__ import annotations
 
from datetime import datetime, timezone
from decimal import Decimal
from typing import List
from fastapi import APIRouter, Depends, HTTPException, Query, Path
from .schemas import ProductLookupOut, CreateOrderIn, CreateOrderOut, OrderListItem, OrderDetailOut, OrderItemOut
from .models import BillingOrder, BillingOrderItem
from app.productvariantdetail.models import ProductVariantDetail
from app.productvariant.models import ProductVariant
from app.products.models import Products
from app.taxhsncode.models import TaxHSNCode
from sqlalchemy.orm import Session
from database import get_db
from sqlalchemy import (func, text)


router = APIRouter(
    prefix="/api/Billing",
    tags=["Billing / POS"],
)
 
LOW_STOCK_THRESHOLD = 5
 
 
# ── Helpers ───────────────────────────────────────────────────
 
def _generate_invoice_no(db: Session) -> str:
    """INV-YYYYMMDD-NNNN, sequence resets daily."""
    today     = datetime.now(timezone.utc).date()
    today_str = today.strftime("%Y%m%d")
    prefix    = f"INV-{today_str}-"
    count     = (
        db.query(func.count(BillingOrder.OrderId))
        .filter(BillingOrder.InvoiceNo.like(f"{prefix}%"))
        .scalar()
    ) or 0
    return f"{prefix}{(count + 1):04d}"
 
 
def _build_size_label(pvd: ProductVariantDetail) -> str:
    """
    Size is stored as a BigInt FK / raw value.
    If you have a Sizes table, join it above and return the label.
    For now: return the numeric value as a string.
    """
    if pvd.cupSizeId:
        return str(pvd.cupSizeId)
    if pvd.sizeId:
        return str(pvd.sizeId)
    return "—"
 
 
# =============================================================
#  ENDPOINT 1 — Product lookup by StockId
# =============================================================
 
@router.get(
    "/product/{stock_id}",
    response_model=ProductLookupOut,
    summary="Scan / type a StockId to fetch product details for the POS cart",
)
def get_product_by_stock(
    stock_id: str = Path(..., min_length=1, description="StockId from ProductVariantDetail"),
    db: Session = Depends(get_db),
):
    # Sanitise — trim + uppercase (matches how StockIds are stored)
    cleaned = stock_id.strip().upper()
    if not cleaned:
        raise HTTPException(status_code=400, detail="Stock ID cannot be empty.")
 
    # ── JOIN: ProductVariantDetail → ProductVariants → Products → TaxHSNCodes ──
    row = (
        db.query(ProductVariantDetail, ProductVariant, Products, TaxHSNCode)
        .join(ProductVariant,
              ProductVariantDetail.productVariantId == ProductVariant.productVariantId)
        .join(Products,
              ProductVariantDetail.productId == Products.productId)
        .outerjoin(TaxHSNCode,
                   ProductVariant.taxHSNCodeId == TaxHSNCode.taxHSNCodeId)
        .filter(
            ProductVariantDetail.stockId == cleaned,
            ProductVariantDetail.deletedInd.isnot(True),
            Products.deletedInd.isnot(True),
        )
        .first()
    )
 
    if not row:
        raise HTTPException(
            status_code=404,
            detail=f"No product found for Stock ID '{cleaned}'. Please verify and try again.",
        )
 
    pvd, pv, p, hsn = row
    available = pvd.availableQuantity or 0
 
    if available <= 0:
        raise HTTPException(
            status_code=409,
            detail=f"'{p.Name}' ({cleaned}) is currently out of stock.",
        )
 
    return ProductLookupOut(
        stockId           = pvd.stockId,
        productName       = p.name or "—",
        size              = _build_size_label(pvd),
        color             = pv.color or "—",
        price             = float(pvd.finalPrice or 0),
        mrpPrice          = float(pvd.mrpPrice or 0),
        hsnCode           = (hsn.hsnCode if hsn else "") or "",
        gstPercent        = float(hsn.gstPercent if hsn else 0),
        availableQuantity = available,
    )
 
 
# =============================================================
#  ENDPOINT 2 — Create order  (save + deduct stock)
# =============================================================
 
@router.post(
    "/orders",
    response_model=CreateOrderOut,
    status_code=201,
    summary="Save billing order and deduct stock quantities atomically",
)
def create_order(
    payload: CreateOrderIn,
    db: Session = Depends(get_db),
):
    try:
        invoice_no = _generate_invoice_no(db)
 
        # ── 1. Create order header ────────────────────────────
        order = BillingOrder(
            InvoiceNo      = invoice_no,
            CustomerName   = payload.customerName,
            CustomerPhone  = payload.customerPhone,
            GSTMode        = payload.gstMode,
            PaymentMethod  = payload.paymentMethod,
            Subtotal       = Decimal(str(payload.subtotal)),
            CGSTTotal      = Decimal(str(payload.cgstTotal)),
            SGSTTotal      = Decimal(str(payload.sgstTotal)),
            IGSTTotal      = Decimal(str(payload.igstTotal)),
            GSTTotal       = Decimal(str(payload.gstTotal)),
            Discount       = Decimal(str(payload.discount)),
            GrandTotal     = Decimal(str(payload.grandTotal)),
            ReceivedAmount = Decimal(str(payload.receivedAmount)),
            Balance        = Decimal(str(payload.balance)),
            CreatedBy      = payload.createdBy,
        )
        db.add(order)
        db.flush()   # get OrderId without committing
 
        # ── 2. Create line items + deduct stock ───────────────
        for item in payload.items:
            cleaned_id = item.stockId.strip().upper()
 
            # Lock the row to prevent concurrent oversell
            pvd: ProductVariantDetail | None = (
                db.query(ProductVariantDetail)
                .filter(
                    ProductVariantDetail.stockId == cleaned_id,
                    ProductVariantDetail.deletedInd.isnot(True),
                )
                .with_for_update()
                .first()
            )
 
            if not pvd:
                raise HTTPException(
                    status_code=404,
                    detail=f"Stock ID '{cleaned_id}' not found during order processing.",
                )
 
            available = pvd.availableQuantity or 0
            if available < item.quantity:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"Insufficient stock for '{item.productName}' ({cleaned_id}). "
                        f"Available: {available}, Requested: {item.quantity}. "
                        "Please update the cart and try again."
                    ),
                )
 
            # Deduct quantities
            new_available = available - item.quantity
            new_processed = (pvd.processedQuantity or 0) + item.quantity
 
            pvd.availableQuantity = new_available
            pvd.processedQuantity = new_processed
            pvd.isOutOfStock      = new_available <= 0
            pvd.isLowStock        = 0 < new_available <= LOW_STOCK_THRESHOLD
            pvd.modifiedDate      = datetime.now(timezone.utc)
 
            # Add line item
            db.add(BillingOrderItem(
                OrderId     = order.OrderId,
                StockId     = cleaned_id,
                ProductName = item.productName,
                Size        = item.size,
                Color       = item.color,
                HSNCode     = item.hsnCode,
                GSTPercent  = Decimal(str(item.gstPercent)),
                Price       = Decimal(str(item.price)),
                Quantity    = item.quantity,
                Subtotal    = Decimal(str(item.subtotal)),
                GSTAmount   = Decimal(str(item.gstAmount)),
                CGST        = Decimal(str(item.cgst)),
                SGST        = Decimal(str(item.sgst)),
                IGST        = Decimal(str(item.igst)),
                LineTotal   = Decimal(str(item.lineTotal)),
            ))
 
        # ── 3. Commit everything atomically ───────────────────
        db.commit()
 
        return CreateOrderOut(
            orderId   = order.OrderId,
            invoiceNo = invoice_no,
            message   = f"Order {invoice_no} saved. {len(payload.items)} item(s) processed.",
        )
 
    except HTTPException:
        db.rollback()
        raise
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Order creation failed: {str(exc)}") from exc
 
 
# =============================================================
#  ENDPOINT 3 — List orders (billing history)
# =============================================================
 
@router.get(
    "/orders",
    response_model=List[OrderListItem],
    summary="List all billing orders (most recent first)",
)
def list_orders(
    page:  int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    orders = (
        db.query(BillingOrder)
        .order_by(BillingOrder.CreatedDate.desc())
        .offset((page - 1) * limit)
        .limit(limit)
        .all()
    )
    return [
        OrderListItem(
            orderId      = o.OrderId,
            invoiceNo    = o.InvoiceNo,
            customerName = o.CustomerName,
            grandTotal   = float(o.GrandTotal),
            paymentMethod = o.PaymentMethod,
            createdDate  = o.CreatedDate,
        )
        for o in orders
    ]
 
 
# =============================================================
#  ENDPOINT 4 — Single order detail (for reprint)
# =============================================================
 
@router.get(
    "/orders/{order_id}",
    response_model=OrderDetailOut,
    summary="Fetch a single order with all line items (for reprint)",
)
def get_order(
    order_id: int = Path(..., ge=1),
    db: Session = Depends(get_db),
):
    order = db.query(BillingOrder).filter(BillingOrder.OrderId == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail=f"Order {order_id} not found.")
 
    return OrderDetailOut(
        orderId       = order.OrderId,
        invoiceNo     = order.InvoiceNo,
        customerName  = order.CustomerName,
        customerPhone = order.CustomerPhone,
        gstMode       = order.GSTMode,
        paymentMethod = order.PaymentMethod,
        subtotal      = float(order.Subtotal),
        cgstTotal     = float(order.CGSTTotal),
        sgstTotal     = float(order.SGSTTotal),
        igstTotal     = float(order.IGSTTotal),
        gstTotal      = float(order.GSTTotal),
        discount      = float(order.Discount),
        grandTotal    = float(order.GrandTotal),
        receivedAmount = float(order.ReceivedAmount),
        balance       = float(order.Balance),
        createdDate   = order.CreatedDate,
        items=[
            OrderItemOut(
                stockId     = i.StockId,
                productName = i.ProductName,
                size        = i.Size or "",
                color       = i.Color or "",
                hsnCode     = i.HSNCode or "",
                gstPercent  = float(i.GSTPercent),
                price       = float(i.Price),
                quantity    = i.Quantity,
                lineTotal   = float(i.LineTotal),
            )
            for i in order.items
        ],
    )
 
 

 




