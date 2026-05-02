# repository.py
import importlib
import traceback
from typing import Optional
from sqlalchemy.orm import Session
from sqlalchemy import BigInteger, Column, MetaData, String, Table, text

# ── stdlib json (avoids poetry.json shadow) ───────────────────────────────────
_stdlib_json = importlib.import_module("json")
json_loads   = _stdlib_json.loads
json_dumps   = _stdlib_json.dumps

# ── your app imports ──────────────────────────────────────────────────────────
from app.category.models import Category
from app.products.models import Products
from app.productvariantdetail.models import ProductVariantDetail
from app.report.schemas import StockLevelFilters, StockLevelItem, StockLevelReportResponse

def _paginate(rows: list, page_index, page_size) -> dict:
    total = len(rows)
    if page_index and page_size:
        start = (page_index - 1) * page_size
        rows = rows[start: start + page_size]
    return {"count": total, "list": rows, "parameters": None}


def _safe(db: Session, sql: str, params: dict = {}):
    try:
        rows = db.execute(text(sql), params).fetchall()
        return [dict(r._mapping) for r in rows]
    except Exception as e:
        print(f"[Report] Query error: {e}")
        return []


def get_order_report(db: Session, filters, page_index, page_size):
    rows = _safe(db, """
        SELECT 
            o."OrderId",
            o."OrderNumber",
            o."CreatedDate" AS "OrderDate",
            o."PaymentMethod",
            o."State",
            -- Concatenate all product names
            STRING_AGG(p."Name", ', ') AS "ProductName",
            -- Sum quantities across all items
            SUM(oi."Quantity") AS "Quantity",
            -- Total amount per order
            SUM(oi."Quantity" * pvd."FinalPrice") AS "TotalAmount",
            -- Window functions on aggregated data
            COUNT(o."OrderId") OVER() AS "TotalOrders",
            SUM(SUM(oi."Quantity" * pvd."FinalPrice")) OVER() AS "TotalRevenue"
        FROM twam."Orders" o
        LEFT JOIN twam."OrderItems" oi 
            ON oi."OrderId" = o."OrderId" 
            AND oi."DeletedInd" = false
        LEFT JOIN twam."ProductVariantDetail" pvd 
            ON pvd."ProductVariantDetailId" = oi."ProductVariantDetailId"
        LEFT JOIN twam."ProductVariants" pv 
            ON pv."ProductVariantId" = pvd."ProductVariantId"
        LEFT JOIN twam."Products" p 
            ON p."ProductId" = pv."ProductId"
        WHERE o."DeletedInd" = false
        GROUP BY 
            o."OrderId",
            o."OrderNumber",
            o."CreatedDate",
            o."PaymentMethod",
            o."State"
        ORDER BY o."CreatedDate" DESC
    """)
    print("Total rows returned:", len(rows))
    print("First row:", rows[0] if rows else "EMPTY")
    return _paginate(rows, page_index, page_size)


# repository
# repository.py — remove ALL inline -- comments from SQL

def get_products_category_report(db, type_val, id_val, from_date, to_date, filters, page_index, page_size):

    if not id_val:
        return _paginate([], page_index, page_size)

    date_filter = ""
    if from_date and to_date:
        date_filter = f'AND o."OrderDate" BETWEEN \'{from_date}\' AND \'{to_date}\''

    # ── Common SELECT columns ─────────────────────────────────────────────────
    SELECT = """
                COALESCE(COUNT(DISTINCT oi."OrderId"), 0)                                AS "totalOrders",
                COALESCE(SUM(oi."Quantity"), 0)                                           AS "unitsSold",
                COALESCE(SUM(pvd."FinalPrice"                     * oi."Quantity"), 0)   AS "grossRevenue",
                COALESCE(SUM((pvd."FinalPrice" - pvd."TaxAmount") * oi."Quantity"), 0)   AS "taxableValue",
                COALESCE(SUM((pvd."CGST"       + pvd."SGST")      * oi."Quantity"), 0)   AS "gstAmount"
    """

    # ── Common JOINs to OrderItems / Orders ──────────────────────────────────
    ORDER_JOINS = f"""
            LEFT JOIN twam."OrderItems" oi
                ON oi."ProductVariantDetailId" = pvd."ProductVariantDetailId"
                AND oi."DeletedInd" = false
            LEFT JOIN twam."Orders" o
                ON o."OrderId" = oi."OrderId"
                AND o."DeletedInd" = false
                {date_filter}
    """

    if type_val == "C":
        sql = f"""
            SELECT
                c."Name" AS "name",
                {SELECT}
            FROM twam."Category" c
            LEFT JOIN twam."Products" p
                ON p."CategoryId" = c."CategoryId"
                AND p."DeletedInd" = false
            LEFT JOIN twam."ProductVariants" pv
                ON pv."ProductId" = p."ProductId"
                AND pv."DeletedInd" = false
            LEFT JOIN twam."ProductVariantDetail" pvd
                ON pvd."ProductVariantId" = pv."ProductVariantId"
            {ORDER_JOINS}
            WHERE c."DeletedInd" = false
              AND c."CategoryId" = {id_val}
            GROUP BY c."Name"
        """

    elif type_val == "B":
        sql = f"""
            SELECT
                b."brandName" AS "name",
                {SELECT}
            FROM mdm."Brand" b
            LEFT JOIN twam."Products" p
                ON p."BrandId" = b."brandId"
                AND p."DeletedInd" = false
            LEFT JOIN twam."ProductVariants" pv
                ON pv."ProductId" = p."ProductId"
                AND pv."DeletedInd" = false
            LEFT JOIN twam."ProductVariantDetail" pvd
                ON pvd."ProductVariantId" = pv."ProductVariantId"
            {ORDER_JOINS}
            WHERE b."brandId" = {id_val}
            GROUP BY b."brandName"
        """

    else:  # type_val == "P" — Product
        sql = f"""
            SELECT
                p."Name" AS "name",
                {SELECT}
            FROM twam."Products" p
            LEFT JOIN twam."ProductVariants" pv
                ON pv."ProductId" = p."ProductId"
                AND pv."DeletedInd" = false
            LEFT JOIN twam."ProductVariantDetail" pvd
                ON pvd."ProductVariantId" = pv."ProductVariantId"
            {ORDER_JOINS}
            WHERE p."DeletedInd" = false
              AND p."ProductId" = {id_val}
            GROUP BY p."Name"
        """

    try:
        result = db.execute(text(sql))
        rows = [
            {
                "name":         r["name"],
                "type":         type_val,
                "totalOrders":  int(r["totalOrders"]   or 0),
                "unitsSold":    int(r["unitsSold"]      or 0),
                "grossRevenue": float(r["grossRevenue"] or 0),
                "taxableValue": float(r["taxableValue"] or 0),
                "gstAmount":    float(r["gstAmount"]    or 0),
            }
            for r in result.mappings().all()
        ]
        print(f">>> ROWS: {len(rows)} | first: {rows[0] if rows else 'empty'}")
        return _paginate(rows, page_index, page_size)

    except Exception as e:
        db.rollback()
        return _paginate([], page_index, page_size)
    
def get_sales_by_region(db: Session, filters, page_index, page_size):
    rows = _safe(db, """
        SELECT 
            COALESCE(o."State", 'Unknown')          AS "Region",
            COUNT(DISTINCT o."OrderId")              AS "TotalOrders",
            COUNT(DISTINCT o."UserProfileId")        AS "Customers",
            COALESCE(SUM(oi."Quantity"), 0)                                           AS "unitsSold",
            COALESCE(SUM(o."TotalAmount"), 0)        AS "GrossRevenue",
            COALESCE(SUM(o."SubTotal"),    0)        AS "TaxableValue",
            COALESCE(SUM(o."TaxAmount"),   0)        AS "GSTAmount"
        FROM twam."Orders" o
        LEFT JOIN twam."OrderItems" oi ON oi."OrderId" = o."OrderId" AND oi."DeletedInd" = false
        WHERE o."DeletedInd" = false
        GROUP BY o."State"
        ORDER BY SUM(o."TotalAmount") DESC NULLS LAST
    """)
    return _paginate(rows, page_index, page_size)


def get_top_low_selling(db: Session, filters, page_index, page_size):
    rows = _safe(db, """
        SELECT 
            p."Name"                                            AS "ProductName",
            pv."ProductVariantId",
            COALESCE(SUM(oi."Quantity"), 0)                    AS "UnitsSold",
            COUNT(DISTINCT oi."OrderId")                       AS "TotalOrders",
            COALESCE(SUM(oi."Quantity" * pvd."FinalPrice"), 0) AS "GrossRevenue",
            c."Name"                                           AS "Category",
            b."brandName"                                      AS "Brand"
        FROM twam."Products" p
        LEFT JOIN twam."ProductVariants" pv 
            ON pv."ProductId" = p."ProductId" 
            AND pv."DeletedInd" = false
        LEFT JOIN twam."ProductVariantDetail" pvd 
            ON pvd."ProductVariantId" = pv."ProductVariantId"
            AND pvd."DeletedInd" = false
        LEFT JOIN twam."OrderItems" oi 
            ON oi."ProductVariantDetailId" = pvd."ProductVariantDetailId" 
            AND oi."DeletedInd" = false
        LEFT JOIN twam."Category" c 
            ON c."CategoryId" = p."CategoryId"
            AND c."DeletedInd" = false
        LEFT JOIN mdm."Brand" b 
            ON b."brandId" = p."BrandId"
            AND b."deletedInd" = false
        WHERE p."DeletedInd" = false
        GROUP BY p."Name", pv."ProductVariantId", c."Name", b."brandName"
        ORDER BY SUM(oi."Quantity") DESC NULLS LAST
    """)
    return _paginate(rows, page_index, page_size)
def get_sales_by_platform(db: Session, filters, page_index, page_size):
    rows = _safe(db, """
        SELECT o."OrderSource" AS "Platform",
               COUNT(o."OrderId") AS "TotalOrders",
               SUM(o."TotalAmount") AS "TotalRevenue"
        FROM twam."Orders" o
        WHERE o."DeletedInd"=false
        GROUP BY o."OrderSource" ORDER BY "TotalRevenue" DESC NULLS LAST
    """)
    return _paginate(rows, page_index, page_size)


def get_coupon_usage_report(db: Session, filters, page_index, page_size):
    rows = _safe(db, """
        SELECT c."CouponCode", c."DiscountType", c."DiscountValue",
               COUNT(o."OrderId") AS "TimesUsed",
               SUM(o."DiscountAmount") AS "TotalDiscount"
        FROM twam."Coupons" c
        LEFT JOIN twam."Orders" o ON o."CouponCode" = c."CouponCode" AND o."DeletedInd"=false
        WHERE c."DeletedInd"=false
        GROUP BY c."CouponCode", c."DiscountType", c."DiscountValue"
        ORDER BY "TimesUsed" DESC NULLS LAST
    """)
    return _paginate(rows, page_index, page_size)


def get_invoice_report(db: Session, filters, page_index, page_size):
    rows = _safe(db, """
        SELECT i."InvoiceId", i."InvoiceNumber", i."InvoiceDate",
               o."OrderId", o."TotalAmount", o."State" AS "OrderState",
               i."TaxAmount", i."TotalAmountWithTax"
        FROM twam."Invoice" i
        LEFT JOIN twam."Orders" o ON o."OrderId" = i."OrderId"
        WHERE i."DeletedInd"=false
        ORDER BY i."InvoiceDate" DESC
    """)
    return _paginate(rows, page_index, page_size)


def get_gst_report(db: Session, filters, page_index, page_size):
    rows = _safe(db, """
        SELECT o."OrderId", o."CreatedDate" AS "OrderDate",
               p."Name" AS "ProductName", oi."Quantity",
               pvd."FinalPrice" AS "UnitPrice",
               t."GSTRate", t."HSNCode",
               (pvd."FinalPrice" * t."GSTRate" / 100) AS "GSTAmount",
               (pvd."FinalPrice" + pvd."FinalPrice" * t."GSTRate" / 100) AS "PriceWithGST"
        FROM twam."Orders" o
        LEFT JOIN twam."OrderItems" oi ON oi."OrderId" = o."OrderId" AND oi."DeletedInd"=false
        LEFT JOIN twam."ProductVariantDetail" pvd ON pvd."ProductVariantDetailId" = oi."ProductVariantDetailId"
        LEFT JOIN twam."ProductVariants" pv ON pv."ProductVariantId" = pvd."ProductVariantId"
        LEFT JOIN twam."Products" p ON p."ProductId" = pv."ProductId"
        LEFT JOIN mdm."TaxHSNCode" t ON t."TaxHSNCodeId" = p."TaxHSNCodeId"
        WHERE o."DeletedInd"=false
        ORDER BY o."CreatedDate" DESC
    """)
    return _paginate(rows, page_index, page_size)


def get_net_vs_revenue_report(db: Session, filters, page_index, page_size):
    where_clauses = ['p."DeletedInd" = false']
    params = {}

    if filters:
        if filters.get("categoryId"):
            where_clauses.append('p."CategoryId" = :categoryId')
            params["categoryId"] = int(filters["categoryId"])
        if filters.get("brandId"):
            where_clauses.append('p."BrandId" = :brandId')
            params["brandId"] = int(filters["brandId"])
        if filters.get("fromDate") and filters.get("toDate"):
            where_clauses.append('o."CreatedDate" BETWEEN :fromDate AND :toDate')
            params["fromDate"] = filters["fromDate"]
            params["toDate"]   = filters["toDate"]

    where_sql = " AND ".join(where_clauses)

    rows = _safe(db, f"""
        SELECT
            p."Name"                                                    AS "ProductName",
            COALESCE(c."Name",        'Unknown')                       AS "Category",
            COALESCE(b."brandName",   'Unknown')                       AS "Brand",
            COALESCE(SUM(oi."Quantity"), 0)                            AS "UnitsSold",
            COUNT(DISTINCT o."OrderId")                                 AS "TotalOrders",
            COALESCE(SUM(oi."Quantity" * pvd."FinalPrice"), 0)         AS "GrossRevenue",
            COALESCE(
                SUM(oi."Quantity" * pvd."FinalPrice") -
                COALESCE(SUM(o."CouponAmount"), 0), 0
            )                                                           AS "NetRevenue",
            COALESCE(SUM(o."TaxAmount"),    0)                         AS "TaxAmount",
            COALESCE(SUM(o."CouponAmount"), 0)                         AS "Discount"
        FROM twam."Products" p
        LEFT JOIN twam."ProductVariants" pv
            ON pv."ProductId"    = p."ProductId"
           AND pv."DeletedInd"   = false
        LEFT JOIN twam."ProductVariantDetail" pvd
            ON pvd."ProductVariantId" = pv."ProductVariantId"
           AND pvd."DeletedInd"       = false
        LEFT JOIN twam."OrderItems" oi
            ON oi."ProductVariantDetailId" = pvd."ProductVariantDetailId"
           AND oi."DeletedInd"             = false
        LEFT JOIN twam."Orders" o
            ON o."OrderId"     = oi."OrderId"
           AND o."DeletedInd"  = false
        LEFT JOIN twam."Category" c
            ON c."CategoryId"  = p."CategoryId"
           AND c."DeletedInd"  = false
        LEFT JOIN mdm."Brand" b
            ON b."brandId"     = p."BrandId"
           AND b."deletedInd"  = false
        WHERE {where_sql}
        GROUP BY p."Name", c."Name", b."brandName"
        ORDER BY SUM(oi."Quantity" * pvd."FinalPrice") DESC NULLS LAST
    """, params)
    return _paginate(rows, page_index, page_size)


def get_order_summary(db: Session, filters, page_index, page_size):
    rows = _safe(db, """
        SELECT o."State" AS "OrderStatus",
               COUNT(o."OrderId") AS "TotalOrders",
               SUM(o."TotalAmount") AS "TotalRevenue",
               AVG(o."TotalAmount") AS "AvgOrderValue"
        FROM twam."Orders" o
        WHERE o."DeletedInd"=false
        GROUP BY o."State" ORDER BY "TotalOrders" DESC
    """)
    return _paginate(rows, page_index, page_size)


def get_order_shipping_status(db: Session, filters, page_index, page_size):
    rows = _safe(db, """
        SELECT o."OrderId", o."CreatedDate", o."State" AS "OrderState",
               s."TrackingNumber", s."ShipmentStatus", s."CourierName",
               s."EstimatedDeliveryDate"
        FROM twam."Orders" o
        LEFT JOIN shipment."Shipment" s ON s."OrderId" = o."OrderId" AND s."DeletedInd"=false
        WHERE o."DeletedInd"=false ORDER BY o."CreatedDate" DESC
    """)
    return _paginate(rows, page_index, page_size)


def get_average_order_value(db: Session, filters, page_index, page_size):
    rows = _safe(db, """
        SELECT DATE_TRUNC('month', o."CreatedDate") AS "Month",
               COUNT(o."OrderId") AS "OrderCount",
               AVG(o."TotalAmount") AS "AvgOrderValue",
               SUM(o."TotalAmount") AS "TotalRevenue"
        FROM twam."Orders" o
        WHERE o."DeletedInd"=false
        GROUP BY DATE_TRUNC('month', o."CreatedDate")
        ORDER BY "Month" DESC
    """)
    return _paginate(rows, page_index, page_size)


def get_order_fulfillment(db: Session, filters, page_index, page_size):
    rows = _safe(db, """
        SELECT o."OrderId", o."CreatedDate",
               o."State" AS "OrderState",
               s."ShipmentStatus",
               EXTRACT(EPOCH FROM (s."CreatedDate" - o."CreatedDate"))/3600 AS "FulfillmentHours"
        FROM twam."Orders" o
        LEFT JOIN shipment."Shipment" s ON s."OrderId" = o."OrderId" AND s."DeletedInd"=false
        WHERE o."DeletedInd"=false ORDER BY o."CreatedDate" DESC
    """)
    return _paginate(rows, page_index, page_size)


def get_stock_back_order(db: Session, filters, page_index, page_size):
    rows = _safe(db, """
        SELECT p."Name" AS "ProductName", pvd."ProductVariantDetailId",
               pvd."StockQuantity", COUNT(oi."OrderItemId") AS "PendingOrders"
        FROM twam."ProductVariantDetail" pvd
        LEFT JOIN twam."ProductVariants" pv ON pv."ProductVariantId" = pvd."ProductVariantId"
        LEFT JOIN twam."Products" p ON p."ProductId" = pv."ProductId"
        LEFT JOIN twam."OrderItems" oi ON oi."ProductVariantDetailId" = pvd."ProductVariantDetailId"
            AND oi."DeletedInd"=false
        WHERE pvd."StockQuantity" = 0 AND pvd."DeletedInd"=false
        GROUP BY p."Name", pvd."ProductVariantDetailId", pvd."StockQuantity"
    """)
    return _paginate(rows, page_index, page_size)


def get_payment_method_usage(db: Session, filters, page_index, page_size):
    rows = _safe(db, """
        SELECT o."PaymentMethod",
               COUNT(o."OrderId") AS "TotalOrders",
               SUM(o."TotalAmount") AS "TotalRevenue",
               ROUND(COUNT(o."OrderId") * 100.0 / SUM(COUNT(o."OrderId")) OVER(), 2) AS "UsagePercent"
        FROM twam."Orders" o
        WHERE o."DeletedInd"=false AND o."PaymentMethod" IS NOT NULL
        GROUP BY o."PaymentMethod" ORDER BY "TotalOrders" DESC
    """)
    return _paginate(rows, page_index, page_size)


def get_return_rate_reason(db: Session, filters, page_index, page_size):
    rows = _safe(db, """
        SELECT ori."ReturnReason", COUNT(ori."ReturnInfoId") AS "TotalReturns",
               SUM(o."TotalAmount") AS "ReturnValue"
        FROM twam."OrderReturnInfo" ori
        LEFT JOIN twam."Orders" o ON o."OrderId" = ori."OrderId"
        WHERE ori."DeletedInd"=false
        GROUP BY ori."ReturnReason" ORDER BY "TotalReturns" DESC
    """)
    return _paginate(rows, page_index, page_size)


def get_failed_payment_report(db: Session, filters, page_index, page_size):
    rows = _safe(db, """
        SELECT o."OrderId", o."CreatedDate", o."TotalAmount",
               o."PaymentMethod", o."PaymentStatus", o."State"
        FROM twam."Orders" o
        WHERE o."DeletedInd"=false
          AND o."PaymentStatus" IN ('Failed', 'Pending', 'Cancelled')
        ORDER BY o."CreatedDate" DESC
    """)
    return _paginate(rows, page_index, page_size)


def get_tax_by_region(db: Session, filters, page_index, page_size):
    rows = _safe(db, """
        SELECT a."State" AS "Region",
               SUM(i."TaxAmount") AS "TotalTax",
               COUNT(DISTINCT i."InvoiceId") AS "InvoiceCount"
        FROM twam."Invoice" i
        LEFT JOIN twam."Orders" o ON o."OrderId" = i."OrderId"
        LEFT JOIN twam."Address" a ON a."AddressId" = o."ShippingAddressId"
        WHERE i."DeletedInd"=false
        GROUP BY a."State" ORDER BY "TotalTax" DESC NULLS LAST
    """)
    return _paginate(rows, page_index, page_size)


# def get_stock_level_report(db: Session, filters, page_index, page_size):
#     rows = _safe(db, """
#         SELECT p."Name" AS "ProductName", pv."VariantName",
#                pvd."ProductVariantDetailId", pvd."StockQuantity",
#                pvd."FinalPrice", pvd."MRPPrice"
#         FROM twam."ProductVariantDetail" pvd
#         LEFT JOIN twam."ProductVariants" pv ON pv."ProductVariantId" = pvd."ProductVariantId"
#         LEFT JOIN twam."Products" p ON p."ProductId" = pv."ProductId"
#         WHERE pvd."DeletedInd"=false
#         ORDER BY pvd."StockQuantity" ASC
#     """)
#     return _paginate(rows, page_index, page_size)


def get_inventory_aging_report(db: Session, filters, page_index, page_size):
    rows = _safe(db, """
        SELECT p."Name" AS "ProductName", pvd."ProductVariantDetailId",
               pvd."StockQuantity", pvd."CreatedDate" AS "StockAddedDate",
               EXTRACT(DAY FROM NOW() - pvd."CreatedDate") AS "AgeDays"
        FROM twam."ProductVariantDetail" pvd
        LEFT JOIN twam."ProductVariants" pv ON pv."ProductVariantId" = pvd."ProductVariantId"
        LEFT JOIN twam."Products" p ON p."ProductId" = pv."ProductId"
        WHERE pvd."DeletedInd"=false AND pvd."StockQuantity" > 0
        ORDER BY "AgeDays" DESC
    """)
    return _paginate(rows, page_index, page_size)


# def get_fast_slow_moving_inventory(db: Session, filters, page_index, page_size):
#     rows = _safe(db, """
#         SELECT p."Name" AS "ProductName", pvd."ProductVariantDetailId",
#                COALESCE(SUM(oi."Quantity"), 0) AS "TotalSold",
#                pvd."StockQuantity" AS "CurrentStock",
#                CASE WHEN COALESCE(SUM(oi."Quantity"), 0) > 50 THEN 'Fast'
#                     WHEN COALESCE(SUM(oi."Quantity"), 0) > 10 THEN 'Medium'
#                     ELSE 'Slow' END AS "MovementCategory"
#         FROM twam."ProductVariantDetail" pvd
#         LEFT JOIN twam."ProductVariants" pv ON pv."ProductVariantId" = pvd."ProductVariantId"
#         LEFT JOIN twam."Products" p ON p."ProductId" = pv."ProductId"
#         LEFT JOIN twam."OrderItems" oi ON oi."ProductVariantDetailId" = pvd."ProductVariantDetailId"
#             AND oi."DeletedInd"=false
#         WHERE pvd."DeletedInd"=false
#         GROUP BY p."Name", pvd."ProductVariantDetailId", pvd."StockQuantity"
#         ORDER BY "TotalSold" DESC
#     """)
#     return _paginate(rows, page_index, page_size)


def get_product_return_rate(db: Session, filters, page_index, page_size):
    rows = _safe(db, """
        SELECT p."Name" AS "ProductName",
               COUNT(DISTINCT oi."OrderItemId") AS "TotalSold",
               COUNT(DISTINCT ori."ReturnInfoId") AS "TotalReturned",
               ROUND(COUNT(DISTINCT ori."ReturnInfoId") * 100.0 /
                     NULLIF(COUNT(DISTINCT oi."OrderItemId"), 0), 2) AS "ReturnRate"
        FROM twam."Products" p
        LEFT JOIN twam."ProductVariants" pv ON pv."ProductId" = p."ProductId"
        LEFT JOIN twam."ProductVariantDetail" pvd ON pvd."ProductVariantId" = pv."ProductVariantId"
        LEFT JOIN twam."OrderItems" oi ON oi."ProductVariantDetailId" = pvd."ProductVariantDetailId" AND oi."DeletedInd"=false
        LEFT JOIN twam."OrderReturnInfo" ori ON ori."OrderId" = oi."OrderId" AND ori."DeletedInd"=false
        WHERE p."DeletedInd"=false
        GROUP BY p."Name" ORDER BY "ReturnRate" DESC NULLS LAST
    """)
    return _paginate(rows, page_index, page_size)


def get_stock_reorder_alerts(db: Session, filters, page_index, page_size):
    rows = _safe(db, """
        SELECT p."Name" AS "ProductName", pvd."ProductVariantDetailId",
               pvd."StockQuantity",
               pvd."ReorderLevel",
               CASE WHEN pvd."StockQuantity" <= pvd."ReorderLevel" THEN 'REORDER NOW'
                    ELSE 'OK' END AS "AlertStatus"
        FROM twam."ProductVariantDetail" pvd
        LEFT JOIN twam."ProductVariants" pv ON pv."ProductVariantId" = pvd."ProductVariantId"
        LEFT JOIN twam."Products" p ON p."ProductId" = pv."ProductId"
        WHERE pvd."DeletedInd"=false
          AND pvd."ReorderLevel" IS NOT NULL
          AND pvd."StockQuantity" <= pvd."ReorderLevel"
        ORDER BY pvd."StockQuantity" ASC
    """)
    return _paginate(rows, page_index, page_size)


def get_customer_acquisition_report(db: Session, filters, page_index, page_size):
    filters    = filters or {}
    group_by   = filters.get("group_by", "month")
    start_date = filters.get("start_date") or None
    end_date   = filters.get("end_date")   or None

    trunc_map = {"day": "day", "week": "week", "month": "month", "year": "year"}
    trunc = trunc_map.get(group_by, "month")

    start_expr = ":start_date ::date" if start_date else "(NOW() - INTERVAL '1 year')::date"
    end_expr   = ":end_date ::date"   if end_date   else "NOW()::date"

    params: dict = {}
    if start_date: params["start_date"] = start_date
    if end_date:   params["end_date"]   = end_date

    rows = _safe(db, f"""
        SELECT
            DATE_TRUNC('{trunc}', u."CreatedDate")                          AS "Period",
            COUNT(u."UserId")                                                AS "NewCustomers",
            COUNT(u."UserId") FILTER (WHERE u."EmailConfirmed" = true)      AS "VerifiedCustomers",
            SUM(COUNT(u."UserId")) OVER ()                                   AS "TotalNewCustomers"
        FROM auth."Users" u
        WHERE u."DeletedInd" = false
          AND u."CreatedDate" >= {start_expr}
          AND u."CreatedDate" <= {end_expr}
          AND u."RoleId" = 2
        GROUP BY DATE_TRUNC('{trunc}', u."CreatedDate")
        ORDER BY "Period" DESC
    """, params)

    return _paginate(rows, page_index, page_size)
 
 
def get_repeat_vs_new_customers(db: Session, filters, page_index, page_size):
    """
    Splits customers in a date window into new (first ever order) vs repeat.
    Filters accepted: start_date, end_date
    """
    start_date = (filters or {}).get("start_date", (
        "NOW() - INTERVAL '90 days'"
    ))
    end_date = (filters or {}).get("end_date", "NOW()")
 
    rows = _safe(db, """
        WITH period_orders AS (
            SELECT
                o."UserId",
                MIN(o."CreatedDate") AS "FirstInPeriod"
            FROM twam."Orders" o
            WHERE o."DeletedInd" = false
              AND o."CreatedDate" >= :start_date ::date
              AND o."CreatedDate" <= :end_date   ::date
            GROUP BY o."UserId"
        ),
        all_time_first AS (
            SELECT
                o."UserId",
                MIN(o."CreatedDate") AS "FirstEver"
            FROM twam."Orders" o
            WHERE o."DeletedInd" = false
            GROUP BY o."UserId"
        ),
        classified AS (
            SELECT
                p."UserId",
                CASE
                    WHEN a."FirstEver" >= :start_date ::date THEN 'New'
                    ELSE 'Repeat'
                END AS "CustomerType"
            FROM period_orders p
            JOIN all_time_first a ON a."UserId" = p."UserId"
        )
        SELECT
            "CustomerType",
            COUNT("UserId")                              AS "CustomerCount",
            ROUND(COUNT("UserId") * 100.0 / SUM(COUNT("UserId")) OVER (), 2) AS "Percentage",
            SUM(COUNT("UserId")) OVER ()                 AS "TotalCustomers"
        FROM classified
        GROUP BY "CustomerType"
        ORDER BY "CustomerCount" DESC
    """, {"start_date": start_date, "end_date": end_date})
 
    return _paginate(rows, page_index, page_size)
 
 
def get_customer_lifetime_value(db: Session, filters, page_index, page_size):
    """
    CLV per customer: total revenue, avg order value, order count, days active.
    Filters accepted: min_orders (default 1), top_n (default 20)
    """
    min_orders = int((filters or {}).get("min_orders", 1))
    top_n = int((filters or {}).get("top_n", 20))
 
    rows = _safe(db, """
        SELECT
            u."UserId",
            u."Name"                                          AS "CustomerName",
            u."Email",
            u."MobileNumber"                                  AS "Phone",
            COUNT(o."OrderId")                                AS "OrderCount",
            SUM(o."TotalAmount")                              AS "TotalRevenue",
            ROUND(AVG(o."TotalAmount"), 2)                    AS "AvgOrderValue",
            MIN(o."CreatedDate")::date                        AS "FirstOrderDate",
            MAX(o."CreatedDate")::date                        AS "LastOrderDate",
            EXTRACT(DAY FROM NOW() - MIN(o."CreatedDate"))    AS "DaysAsCustomer"
        FROM auth."Users" u
        JOIN twam."Orders" o ON o."UserId" = u."UserId" AND o."DeletedInd" = false
        WHERE u."DeletedInd" = false
          AND o."State" NOT IN ('Cancelled', 'Returned')
        GROUP BY u."UserId", u."Name", u."Email", u."MobileNumber"
        HAVING COUNT(o."OrderId") >= :min_orders
        ORDER BY "TotalRevenue" DESC
        LIMIT :top_n
    """, {"min_orders": min_orders, "top_n": top_n})
 
    return _paginate(rows, page_index, page_size)
 
 
def get_customer_segmentation(db: Session, filters, page_index, page_size):
    """
    Returns three segmentation breakdowns in one call:
      by_location, by_spending_tier, by_frequency.
    The 'segment_by' filter controls which view: location | spending | frequency
    Filters accepted: segment_by (default: location)
    """
    segment_by = (filters or {}).get("segment_by", "location")
 
    if segment_by == "spending":
        rows = _safe(db, """
            SELECT
                CASE
                    WHEN SUM(o."TotalAmount") >= 10000 THEN 'High (≥10k)'
                    WHEN SUM(o."TotalAmount") >= 2000  THEN 'Mid (2k–10k)'
                    ELSE 'Low (<2k)'
                END                        AS "SpendingTier",
                COUNT(DISTINCT o."UserId") AS "CustomerCount"
            FROM twam."Orders" o
            WHERE o."DeletedInd" = false
              AND o."State" NOT IN ('Cancelled', 'Returned')
            GROUP BY o."UserId"
            -- Wrap in outer query to group the tiers
        """)
        # Reaggregate tiers in Python since SQL can't GROUP BY a CASE on aggregated subquery easily
        from collections import defaultdict
        tier_map = defaultdict(int)
        for r in rows:
            tier_map[r["SpendingTier"]] += 1
        rows = [{"SpendingTier": t, "CustomerCount": c} for t, c in tier_map.items()]
 
    elif segment_by == "frequency":
        rows = _safe(db, """
            SELECT
                CASE
                    WHEN "OrderCount" >= 10 THEN 'Frequent (10+)'
                    WHEN "OrderCount" >= 3  THEN 'Occasional (3–9)'
                    ELSE 'One-time (1–2)'
                END                AS "FrequencySegment",
                COUNT("UserId")    AS "CustomerCount"
            FROM (
                SELECT o."UserId", COUNT(o."OrderId") AS "OrderCount"
                FROM twam."Orders" o
                WHERE o."DeletedInd" = false
                  AND o."State" NOT IN ('Cancelled', 'Returned')
                GROUP BY o."UserId"
            ) sub
            GROUP BY "FrequencySegment"
            ORDER BY "CustomerCount" DESC
        """)
    else:  # location (default)
        rows = _safe(db, """
            SELECT
                COALESCE(a."State", 'Unknown')     AS "Location",
                COUNT(DISTINCT o."UserId")          AS "CustomerCount",
                SUM(o."TotalAmount")                AS "TotalRevenue",
                COUNT(o."OrderId")                  AS "TotalOrders"
            FROM twam."Orders" o
            LEFT JOIN twam."Address" a ON a."AddressId" = o."ShippingAddressId"
            WHERE o."DeletedInd" = false
              AND o."State" NOT IN ('Cancelled', 'Returned')
            GROUP BY a."State"
            ORDER BY "CustomerCount" DESC
            LIMIT 20
        """)
 
    return _paginate(rows, page_index, page_size)
 
 
def get_customer_churn_rate(db: Session, filters, page_index, page_size):
    """
    Churn = customers who ordered in the analysis window but whose
    last order is older than `inactive_days`.
    Filters accepted: inactive_days (default 90), period_months (default 12)
    """
    inactive_days = int((filters or {}).get("inactive_days", 90))
    period_months = int((filters or {}).get("period_months", 12))
 
    rows = _safe(db, """
        WITH active_customers AS (
            SELECT DISTINCT o."UserId"
            FROM twam."Orders" o
            WHERE o."DeletedInd" = false
              AND o."CreatedDate" >= NOW() - (:period_months || ' months')::INTERVAL
        ),
        last_order AS (
            SELECT
                o."UserId",
                MAX(o."CreatedDate") AS "LastOrderDate"
            FROM twam."Orders" o
            WHERE o."DeletedInd" = false
              AND o."UserId" IN (SELECT "UserId" FROM active_customers)
            GROUP BY o."UserId"
        )
        SELECT
            EXTRACT(YEAR  FROM "LastOrderDate")::int    AS "Year",
            EXTRACT(MONTH FROM "LastOrderDate")::int    AS "Month",
            COUNT("UserId")                              AS "ChurnedCustomers",
            (SELECT COUNT(*) FROM active_customers)      AS "TotalActiveCustomers",
            ROUND(
                COUNT("UserId") * 100.0 /
                NULLIF((SELECT COUNT(*) FROM active_customers), 0), 2
            )                                            AS "ChurnRatePct"
        FROM last_order
        WHERE "LastOrderDate" < NOW() - (:inactive_days || ' days')::INTERVAL
        GROUP BY "Year", "Month"
        ORDER BY "Year" DESC, "Month" DESC
    """, {"inactive_days": inactive_days, "period_months": period_months})
 
    return _paginate(rows, page_index, page_size)
 
 
def get_top_customers_by_revenue(db: Session, filters, page_index, page_size):
    """
    Revenue leaderboard with rank, revenue share %, and last order date.
    Filters accepted: start_date, end_date, top_n (default 10)
    """
    start_date = (filters or {}).get("start_date", "NOW() - INTERVAL '1 year'")
    end_date = (filters or {}).get("end_date", "NOW()")
    top_n = int((filters or {}).get("top_n", 10))
 
    rows = _safe(db, """
        SELECT
            RANK() OVER (ORDER BY SUM(o."TotalAmount") DESC) AS "Rank",
            u."UserId",
            u."Name"                                          AS "CustomerName",
            u."Email",
            u."MobileNumber"                                  AS "Phone",
            COUNT(o."OrderId")                                AS "OrderCount",
            SUM(o."TotalAmount")                              AS "TotalRevenue",
            ROUND(
                SUM(o."TotalAmount") * 100.0 /
                NULLIF(SUM(SUM(o."TotalAmount")) OVER (), 0), 2
            )                                                 AS "RevenueSharePct",
            MAX(o."CreatedDate")::date                        AS "LastOrderDate",
            SUM(SUM(o."TotalAmount")) OVER ()                 AS "GrandTotalRevenue"
        FROM auth."Users" u
        JOIN twam."Orders" o ON o."UserId" = u."UserId" AND o."DeletedInd" = false
        WHERE u."DeletedInd" = false
          AND o."State" NOT IN ('Cancelled', 'Returned')
          AND o."CreatedDate" >= :start_date ::date
          AND o."CreatedDate" <= :end_date   ::date
        GROUP BY u."UserId", u."Name", u."Email", u."MobileNumber"
        ORDER BY "TotalRevenue" DESC
        LIMIT :top_n
    """, {"start_date": start_date, "end_date": end_date, "top_n": top_n})
 
    return _paginate(rows, page_index, page_size)

# ===================new reports above===================

def _parse_filters(raw) -> dict:
    if not raw:
        return {}
    try:
        if isinstance(raw, dict):
            return raw
        return json_loads(raw)   # ← use the alias, NOT json.loads
    except ValueError:
        return {}


def get_stock_level_report(
    db: Session,
    filters,
    page_index: Optional[int] = 1,
    page_size: Optional[int] = 20,
):
    # ── Parse filters ─────────────────────────────────────────────────────────
    f = _parse_filters(filters)
    print(f"[StockLevel] Parsed filters: {f}")

    # ── Build WHERE clauses ───────────────────────────────────────────────────
    where_clauses = ['pvd."DeletedInd" = false']
    params = {}

    if f.get("search"):
        where_clauses.append(
            '(p."Name" ILIKE :search OR CAST(pvd."StockId" AS TEXT) ILIKE :search)'
        )
        params["search"] = f"%{f['search'].strip()}%"

    if f.get("categoryId") is not None:
        try:
            params["categoryId"] = int(f["categoryId"])
            where_clauses.append('p."CategoryId" = :categoryId')
        except (ValueError, TypeError) as e:
            print(f"[StockLevel] Invalid categoryId: {e}")

    if f.get("isLowStock") is not None:
        val = f["isLowStock"]
        if isinstance(val, str):
            val = val.lower() == "true"
        params["isLowStock"] = bool(val)
        where_clauses.append('pvd."IsLowStock" = :isLowStock')

    if f.get("isOutOfStock") is not None:
        val = f["isOutOfStock"]
        if isinstance(val, str):
            val = val.lower() == "true"
        params["isOutOfStock"] = bool(val)
        where_clauses.append('pvd."IsOutOfStock" = :isOutOfStock')

    if f.get("minStock") is not None:
        try:
            params["minStock"] = int(f["minStock"])
            where_clauses.append('pvd."StockQuantity" >= :minStock')
        except (ValueError, TypeError) as e:
            print(f"[StockLevel] Invalid minStock: {e}")

    if f.get("maxStock") is not None:
        try:
            params["maxStock"] = int(f["maxStock"])
            where_clauses.append('pvd."StockQuantity" <= :maxStock')
        except (ValueError, TypeError) as e:
            print(f"[StockLevel] Invalid maxStock: {e}")

    where_sql = " AND ".join(where_clauses)

    base_from = f"""
        FROM twam."ProductVariantDetail" pvd
        LEFT JOIN twam."ProductVariants"  pv  ON pv."ProductVariantId"  = pvd."ProductVariantId"
        LEFT JOIN twam."Products"          p   ON p."ProductId"          = pv."ProductId"
        LEFT JOIN twam."Category"          c   ON c."CategoryId"         = p."CategoryId"
        WHERE {where_sql}
    """

    # ── COUNT ─────────────────────────────────────────────────────────────────
    count_params = dict(params)
    total = 0
    try:
        total = db.execute(
            text(f'SELECT COUNT(*) {base_from}'), count_params
        ).scalar() or 0
        print(f"[StockLevel] Total count: {total}")
    except Exception as e:
        print(f"[StockLevel] COUNT query error: {e}")
        traceback.print_exc()
        db.rollback()
        return {
            "TotalCount": 0,
            "Page":       page_index or 1,
            "PageSize":   page_size or 20,
            "Data":       [],
        }

    # ── PAGINATION ────────────────────────────────────────────────────────────
    page_index = max(int(page_index or 1), 1)
    page_size  = max(int(page_size  or 20), 1)
    params["limit"]  = page_size
    params["offset"] = (page_index - 1) * page_size

    # ── DATA QUERY ────────────────────────────────────────────────────────────
    rows = []
    try:
        result = db.execute(
            text(f"""
                SELECT
                    pvd."ProductVariantDetailId",
                    pvd."StockId",
                    pvd."StockQuantity",
                    pvd."AvailableQuantity",
                    pvd."ProcessedQuantity",
                    pvd."AmendmentQuantity",
                    pvd."MRPPrice",
                    pvd."FinalPrice",
                    pvd."DiscountPercent",
                    pvd."IsLowStock",
                    pvd."IsOutOfStock",
                    p."Name"         AS "ProductName",
                    pv."VariantName" AS "VariantName",
                    c."Name"         AS "CategoryName"
                {base_from}
                ORDER BY pvd."StockQuantity" ASC NULLS LAST
                LIMIT :limit OFFSET :offset
            """),
            params,
        )
        rows = [dict(r._mapping) for r in result]
        print(f"[StockLevel] Rows fetched: {len(rows)}")
    except Exception as e:
        print(f"[StockLevel] DATA query error: {e}")
        traceback.print_exc()
        db.rollback()
        return {
            "TotalCount": total,
            "Page":       page_index,
            "PageSize":   page_size,
            "Data":       [],
        }

    return {
        "TotalCount": total,
        "Page":       page_index,
        "PageSize":   page_size,
        "Data":       rows,
    }
    
    # -----------------------------
def get_inventory_aging_report(
    db: Session,
    filters,
    page_index: Optional[int] = 1,
    page_size:  Optional[int] = 20,
):
    # ── Parse filters ────────────────────────────────────────────────────────
    f = _parse_filters(filters)   # reuse your existing _parse_filters()
    print(f"[AgingReport] Parsed filters: {f}")

    # ── WHERE clauses ────────────────────────────────────────────────────────
    where_clauses = [
        'pvd."DeletedInd" = false',
        'ia."DeletedInd"  = false',
    ]
    params = {}

    if f.get("search"):
        where_clauses.append(
            '(p."Name" ILIKE :search OR CAST(pvd."StockId" AS TEXT) ILIKE :search)'
        )
        params["search"] = f"%{f['search'].strip()}%"

    if f.get("categoryId") is not None:
        try:
            params["categoryId"] = int(f["categoryId"])
            where_clauses.append('p."CategoryId" = :categoryId')
        except (ValueError, TypeError):
            pass

    # Age bucket filter — translated into a day-range WHERE clause
    bucket = f.get("agingBucket")
    if bucket == "0-30":
        where_clauses.append(
            "DATE_PART('day', NOW() - ia.\"ReceivedDate\") BETWEEN 0 AND 30"
        )
    elif bucket == "31-90":
        where_clauses.append(
            "DATE_PART('day', NOW() - ia.\"ReceivedDate\") BETWEEN 31 AND 90"
        )
    elif bucket == "91-180":
        where_clauses.append(
            "DATE_PART('day', NOW() - ia.\"ReceivedDate\") BETWEEN 91 AND 180"
        )
    elif bucket == "180+":
        where_clauses.append(
            "DATE_PART('day', NOW() - ia.\"ReceivedDate\") > 180"
        )

    where_sql = " AND ".join(where_clauses)

    base_from = f"""
        FROM twam."InventoryAging"         ia
        JOIN twam."ProductVariantDetail"   pvd ON pvd."ProductVariantDetailId" = ia."ProductVariantDetailId"
        JOIN twam."ProductVariants"        pv  ON pv."ProductVariantId"        = pvd."ProductVariantId"
        JOIN twam."Products"               p   ON p."ProductId"                = pv."ProductId"
        LEFT JOIN twam."Category"          c   ON c."CategoryId"               = p."CategoryId"
        WHERE {where_sql}
    """

    # ── COUNT ────────────────────────────────────────────────────────────────
    count_params = dict(params)
    total = 0
    try:
        total = db.execute(
            text(f"SELECT COUNT(*) {base_from}"), count_params
        ).scalar() or 0
        print(f"[AgingReport] Total: {total}")
    except Exception as e:
        print(f"[AgingReport] COUNT error: {e}")
        traceback.print_exc()
        db.rollback()
        return {"TotalCount": 0, "Page": page_index or 1, "PageSize": page_size or 20, "Data": []}

    # ── PAGINATION ───────────────────────────────────────────────────────────
    page_index = max(int(page_index or 1), 1)
    page_size  = max(int(page_size  or 20), 1)
    params["limit"]  = page_size
    params["offset"] = (page_index - 1) * page_size

    # ── DATA QUERY ───────────────────────────────────────────────────────────
    rows = []
    try:
        result = db.execute(text(f"""
            SELECT
                pvd."ProductVariantDetailId",
                pvd."StockId",
                pvd."StockQuantity",
                pvd."MRPPrice",
                pvd."FinalPrice",
                p."Name"          AS "ProductName",
                pv."VariantName"  AS "VariantName",
                c."Name"          AS "CategoryName",
                ia."ReceivedDate",
                ia."Quantity",
                CAST(DATE_PART('day', NOW() - ia."ReceivedDate") AS INT) AS "DaysInStock",
                CASE
                    WHEN DATE_PART('day', NOW() - ia."ReceivedDate") BETWEEN 0  AND 30  THEN '0-30'
                    WHEN DATE_PART('day', NOW() - ia."ReceivedDate") BETWEEN 31 AND 90  THEN '31-90'
                    WHEN DATE_PART('day', NOW() - ia."ReceivedDate") BETWEEN 91 AND 180 THEN '91-180'
                    ELSE '180+'
                END AS "AgingBucket"
            {base_from}
            ORDER BY "DaysInStock" DESC NULLS LAST
            LIMIT :limit OFFSET :offset
        """), params)

        rows = [dict(r._mapping) for r in result]
        # Serialize ReceivedDate to string so Pydantic doesn't choke
        for row in rows:
            if row.get("ReceivedDate") is not None:
                row["ReceivedDate"] = str(row["ReceivedDate"])

        print(f"[AgingReport] Rows: {len(rows)}")
    except Exception as e:
        print(f"[AgingReport] DATA error: {e}")
        traceback.print_exc()
        db.rollback()
        return {"TotalCount": total, "Page": page_index, "PageSize": page_size, "Data": []}

    return {
        "TotalCount": total,
        "Page":       page_index,
        "PageSize":   page_size,
        "Data":       rows,
    }
def get_fast_slow_moving_inventory(db, filters, page_index, page_size):
    f = _parse_filters(filters)

    where = ['pvd."DeletedInd" = false']
    params = {}

    # 🔍 Search
    if f.get("search"):
        where.append('(p."Name" ILIKE :search OR pvd."StockId" ILIKE :search)')
        params["search"] = f"%{f['search']}%"

    # 📂 Category filter
    if f.get("categoryId"):
        where.append('p."CategoryId" = :categoryId')
        params["categoryId"] = f["categoryId"]

    where_sql = " AND ".join(where)

    query = f"""
        SELECT 
            p."Name" AS "ProductName",
            pvd."ProductVariantDetailId",
            COALESCE(SUM(oi."Quantity"), 0) AS "TotalSold",
            pvd."StockQuantity" AS "CurrentStock",

            CASE 
                WHEN COALESCE(SUM(oi."Quantity"), 0) >= 50 THEN 'Fast'
                WHEN COALESCE(SUM(oi."Quantity"), 0) >= 20 THEN 'Medium'
                ELSE 'Slow'
            END AS "MovementCategory"

        FROM twam."ProductVariantDetail" pvd
        LEFT JOIN twam."ProductVariants" pv 
            ON pv."ProductVariantId" = pvd."ProductVariantId"
        LEFT JOIN twam."Products" p 
            ON p."ProductId" = pv."ProductId"
        LEFT JOIN twam."OrderItems" oi 
            ON oi."ProductVariantDetailId" = pvd."ProductVariantDetailId"
            AND oi."DeletedInd" = false

        WHERE {where_sql}

        GROUP BY p."Name", pvd."ProductVariantDetailId", pvd."StockQuantity"
        ORDER BY "TotalSold" DESC
        LIMIT :limit OFFSET :offset
    """

    params["limit"] = page_size
    params["offset"] = (page_index - 1) * page_size

    result = db.execute(text(query), params)
    rows = [dict(r._mapping) for r in result]

    return {
        "TotalCount": len(rows),
        "Page": page_index,
        "PageSize": page_size,
        "Data": rows
    }