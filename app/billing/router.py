from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Path
from sqlalchemy.orm import Session
from sqlalchemy import func

from database import get_db
from app.saleschannel.models              import SalesChannel
from app.billing.models                   import Inventory, Warehouse, BillingOrder, BillingOrderItem
from app.productvariantdetail.models      import ProductVariantDetail
from app.productvariant.models            import ProductVariant
from app.products.models                  import Products
from app.taxhsncode.models                import TaxHSNCode
from app.stores.models                    import Stores
from app.auth.dependencies                import get_current_user          # ← only this
from app.auth.schemas                     import CurrentUser               # ← ADD THIS
from .schemas                             import (
    InventoryItem, ProductLookupOut, CreateBillIn, CreateBillOut,
    WarehouseOut, StoreOut,                                                # ← ADD StoreOut
)

router = APIRouter(prefix="/api/v2/Billing", tags=["Billing v2 / Multi-Channel"])

LOW_STOCK_THRESHOLD = 5
SUPER_ADMIN_ROLE_ID = 1


# ── Helpers ───────────────────────────────────────────────────────────────────

def _generate_invoice_no(db: Session) -> str:
    today_str = datetime.now(timezone.utc).date().strftime("%Y%m%d")
    prefix    = f"INV-{today_str}-"
    count     = (
        db.query(func.count(BillingOrder.OrderId))
        .filter(BillingOrder.InvoiceNo.like(f"{prefix}%"))
        .scalar()
    ) or 0
    return f"{prefix}{(count + 1):04d}"


def _get_inventory_row(
    db: Session,
    stock_id: str,
    *,
    store_id:     Optional[int] = None,
    warehouse_id: Optional[int] = None,
    lock: bool = False,
) -> Optional[Inventory]:
    q = db.query(Inventory).filter(Inventory.StockId == stock_id)
    if store_id is not None:
        q = q.filter(Inventory.StoreId == store_id)
    elif warehouse_id is not None:
        q = q.filter(Inventory.WarehouseId == warehouse_id)
    if lock:
        q = q.with_for_update()
    return q.first()


def _resolve_location_name(inv: Inventory, db: Session) -> tuple[str, str]:
    if inv.StoreId:
        store = db.query(Stores).filter(Stores.StoreId == inv.StoreId).first()
        return "store", (store.StoreName if store else f"Store {inv.StoreId}")
    wh = db.query(Warehouse).filter(Warehouse.WarehouseId == inv.WarehouseId).first()
    return "warehouse", (wh.WarehouseName if wh else f"Warehouse {inv.WarehouseId}")


# ── ENDPOINT 0a — List warehouses ────────────────────────────────────────────

@router.get(
    "/warehouses",
    response_model=List[WarehouseOut],
    summary="List all active warehouses",
)
def list_warehouses(
    db:           Session     = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),   # ← CurrentUser
):
    rows = (
        db.query(Warehouse)
        .filter(Warehouse.IsActive == True)
        .order_by(Warehouse.WarehouseName)
        .all()
    )
    return [
        WarehouseOut(
            warehouseId   = wh.WarehouseId,
            warehouseName = wh.WarehouseName,
            address       = wh.Address,
            contactPhone  = wh.ContactPhone,
            isActive      = wh.IsActive,
        )
        for wh in rows
    ]


# ── ENDPOINT 0b — List stores (MISSING — frontend needs this) ────────────────

@router.get("/stores", response_model=List[StoreOut])
def list_stores(
    db:           Session     = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    rows = db.query(Stores).filter(Stores.isActive == True).order_by(Stores.storeName).all()
    return [
        StoreOut(
            storeId   = s.storeId,
            storeName = s.storeName or "",
            address   = s.address  or "",
            phone     = s.phone    or "",
            city      = s.city     or "",
            state     = s.state    or "",
            pinCode   = s.pinCode  or "",
        )
        for s in rows
    ] 
# ── ENDPOINT 1  — Store inventory list ───────────────────────────────────────

@router.get(
    "/inventory",
    response_model=List[InventoryItem],
    summary="Return inventory for the logged-in user's store",
)
def list_inventory(
    db:           Session     = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),   # ← CurrentUser
):
    q = db.query(Inventory)

    if current_user.role_id == SUPER_ADMIN_ROLE_ID and current_user.store_id is None:
        q = q.filter(Inventory.StoreId.isnot(None))
    else:
        q = q.filter(Inventory.StoreId == current_user.store_id)

    rows = q.order_by(Inventory.StockId).all()

    result = []
    for inv in rows:
        loc_type, loc_name = _resolve_location_name(inv, db)
        result.append(InventoryItem(
            inventoryId  = inv.InventoryId,
            stockId      = inv.StockId,
            storeId      = inv.StoreId,
            warehouseId  = inv.WarehouseId,
            locationName = loc_name,
            locationType = loc_type,
            quantity     = inv.Quantity,
            available    = inv.available,
        ))
    return result


# ── ENDPOINT 2  — Product scan ────────────────────────────────────────────────
@router.get("/product/{stock_id}", response_model=ProductLookupOut)
def get_product_by_stock(
    stock_id:     str           = Path(..., min_length=1),
    channel:      str           = Query("POS"),
    store_id:     Optional[int] = Query(None),
    warehouse_id: Optional[int] = Query(None),
    db:           Session       = Depends(get_db),
    current_user: CurrentUser   = Depends(get_current_user),
):
    cleaned = stock_id.strip().upper()

    #  Auto-resolve from JWT if frontend didn't send them
    if store_id is None and warehouse_id is None:
        store_id     = current_user.store_id
        warehouse_id = getattr(current_user, "warehouse_id", None)

    if store_id is None and warehouse_id is None:
        raise HTTPException(400, detail="Either store_id or warehouse_id is required.")
    

    row = (
        db.query(ProductVariantDetail, ProductVariant, Products, TaxHSNCode)
        .join(ProductVariant,  ProductVariantDetail.productVariantId == ProductVariant.productVariantId)
        .join(Products,        ProductVariantDetail.productId        == Products.productId)
        .outerjoin(TaxHSNCode, ProductVariant.taxHSNCodeId           == TaxHSNCode.taxHSNCodeId)
        .filter(
            ProductVariantDetail.stockId == cleaned,
            ProductVariantDetail.deletedInd.isnot(True),
            Products.deletedInd.isnot(True),
        )
        .first()
    )
    if not row:
        raise HTTPException(404, detail=f"No product found for Stock ID '{cleaned}'.")

    pvd, pv, p, hsn = row

    # ── Stock check: ONLY check the selected location ─────────────────────────
    if store_id:
        # Store billing — check ONLY this store's inventory
        inv = _get_inventory_row(db, cleaned, store_id=store_id)
        if not inv or inv.available <= 0:
            raise HTTPException(
                409,
                detail=f"'{p.name}' ({cleaned}) is not available at this store."
            )
        available = inv.available

    elif warehouse_id:
        # Warehouse billing — check ONLY this warehouse's inventory
        inv = _get_inventory_row(db, cleaned, warehouse_id=warehouse_id)
        if not inv or inv.available <= 0:
            raise HTTPException(
                409,
                detail=f"'{p.name}' ({cleaned}) is not available in this warehouse."
            )
        available = inv.available

    else:
        raise HTTPException(400, detail="Either store_id or warehouse_id is required.")

    return ProductLookupOut(
        stockId           = pvd.stockId,
        productName       = p.name or "—",
        size              = str(pvd.cupSizeId or pvd.sizeId or "—"),
        color             = pv.color or "—",
        price             = float(pvd.finalPrice or 0),
        mrpPrice          = float(pvd.mrpPrice or 0),
        hsnCode           = (hsn.hsnCode if hsn else "") or "",
        gstPercent        = float(hsn.totalGST if hsn and hasattr(hsn, "totalGST") and hsn.totalGST else 0),
        availableQuantity = available,
    )


# ── ENDPOINT 3  — Create billing order ───────────────────────────────────────

@router.post("/orders", response_model=CreateBillOut, status_code=201)
def create_order(
    payload:      CreateBillIn,
    db:           Session     = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    if not payload.items:
        raise HTTPException(400, detail="Order must contain at least one item.")

    is_super_admin = current_user.role_id == SUPER_ADMIN_ROLE_ID

    if payload.storeId:
        # Store billing (POS)
        effective_store_id     = payload.storeId
        effective_warehouse_id = None
        location_filter        = {"store_id": effective_store_id}
        order_kwargs           = {"StoreId": effective_store_id, "WarehouseId": None}

    elif payload.warehouseId:
        # Warehouse billing (Amazon / Flipkart / Meesho)
        effective_store_id     = None
        effective_warehouse_id = payload.warehouseId
        location_filter        = {"warehouse_id": effective_warehouse_id}
        order_kwargs           = {"StoreId": None, "WarehouseId": effective_warehouse_id}

    else:
        # Store manager — use their JWT store automatically
        if not current_user.store_id:
            raise HTTPException(400, detail="No store or warehouse selected.")
        effective_store_id     = current_user.store_id
        effective_warehouse_id = None
        location_filter        = {"store_id": effective_store_id}
        order_kwargs           = {"StoreId": effective_store_id, "WarehouseId": None}


    try:
        invoice_no = _generate_invoice_no(db)

        order = BillingOrder(
            InvoiceNo      = invoice_no,
            CustomerName   = payload.customerName,
            CustomerPhone  = payload.customerPhone,
            GSTMode        = payload.gstMode,
            PaymentMethod  = payload.paymentMethod,
            Channel        = payload.channel,
            Subtotal       = Decimal(str(payload.subtotal)),
            CGSTTotal      = Decimal(str(payload.cgstTotal)),
            SGSTTotal      = Decimal(str(payload.sgstTotal)),
            IGSTTotal      = Decimal(str(payload.igstTotal)),
            GSTTotal       = Decimal(str(payload.gstTotal)),
            Discount       = Decimal(str(payload.discount)),
            GrandTotal     = Decimal(str(payload.grandTotal)),
            ReceivedAmount = Decimal(str(payload.receivedAmount)),
            Balance        = Decimal(str(payload.balance)),
            CreatedBy      = payload.createdBy or current_user.user_id,  # ← user_id not sub
            **order_kwargs,
        )
        db.add(order)
        db.flush()

        for item in payload.items:
            cleaned_id = item.stockId.strip().upper()
            inv = _get_inventory_row(db, cleaned_id, **location_filter, lock=True)

            if not inv:
                loc_label = (
                        f"Store {effective_store_id}" if effective_store_id
                        else f"Warehouse {effective_warehouse_id}"
                    )
                raise HTTPException(404, detail=f"'{cleaned_id}' not found in {loc_label}.")

            if inv.available < item.quantity:
                raise HTTPException(
                    409,
                    detail=(
                        f"Insufficient stock for '{item.productName}' ({cleaned_id}). "
                        f"Available: {inv.available}, Requested: {item.quantity}."
                    ),
                )

            inv.Quantity    -= item.quantity
            inv.ModifiedDate = datetime.now(timezone.utc)

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

        db.commit()
        return CreateBillOut(
            orderId   = order.OrderId,
            invoiceNo = invoice_no,
            message   = f"Order {invoice_no} created via {payload.channel}. {len(payload.items)} item(s) processed.",
        )

    except HTTPException:
        db.rollback()
        raise
    except Exception as exc:
        db.rollback()
        raise HTTPException(500, detail=f"Order creation failed: {str(exc)}") from exc


# ── ENDPOINT 4  — Warehouse inventory list ───────────────────────────────────

@router.get(
    "/inventory/warehouse/{warehouse_id}",
    response_model=List[InventoryItem],
    summary="List inventory for a specific warehouse",
)
def list_warehouse_inventory(
    warehouse_id: int         = Path(..., ge=1),
    db:           Session     = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),   # ← CurrentUser
):
    wh = db.query(Warehouse).filter(Warehouse.WarehouseId == warehouse_id).first()
    if not wh:
        raise HTTPException(404, detail=f"Warehouse {warehouse_id} not found.")

    rows = (
        db.query(Inventory)
        .filter(Inventory.WarehouseId == warehouse_id)
        .order_by(Inventory.StockId)
        .all()
    )
    return [
        InventoryItem(
            inventoryId  = inv.InventoryId,
            stockId      = inv.StockId,
            storeId      = None,
            warehouseId  = inv.WarehouseId,
            locationName = wh.WarehouseName,
            locationType = "warehouse",
            quantity     = inv.Quantity,
            available    = inv.available,
        )
        for inv in rows
    ]