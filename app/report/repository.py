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
    channel = (filters or {}).get("channel")

    channel_filter = 'AND bo."Channel" = :channel' if channel else ''
    params = {"channel": channel} if channel else {}

    rows = _safe(db, f"""
        SELECT 
            o."OrderId",
            o."OrderNumber",
            o."CreatedDate"                         AS "OrderDate",
            o."PaymentMethod",
            o."State",

            STRING_AGG(DISTINCT p."Name", ', ')     AS "ProductName",
            SUM(oi."Quantity")                      AS "Quantity",
            SUM(oi."Quantity" * pvd."FinalPrice")   AS "TotalAmount",

            bo."InvoiceNo",
            bo."CustomerName",
            bo."CustomerPhone",
            bo."Channel",
            bo."GSTMode",
            bo."GrandTotal",
            bo."Discount",
            bo."ReceivedAmount",
            bo."Balance",
            bo."CGSTTotal",
            bo."SGSTTotal",
            bo."IGSTTotal",
            bo."GSTTotal",

            COUNT(o."OrderId") OVER()               AS "TotalOrders",
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

        LEFT JOIN twam."BillingOrders" bo
            ON bo."OrderId" = o."OrderId"
        LEFT JOIN twam."BillingOrderItems" boi
            ON boi."OrderId" = o."OrderId"

        WHERE o."DeletedInd" = false
        {channel_filter}

        GROUP BY 
            o."OrderId",
            o."OrderNumber",
            o."CreatedDate",
            o."PaymentMethod",
            o."State",
            bo."InvoiceNo",
            bo."CustomerName",
            bo."CustomerPhone",
            bo."Channel",
            bo."GSTMode",
            bo."GrandTotal",
            bo."Discount",
            bo."ReceivedAmount",
            bo."Balance",
            bo."CGSTTotal",
            bo."SGSTTotal",
            bo."IGSTTotal",
            bo."GSTTotal"

        ORDER BY o."CreatedDate" DESC
    """, params)

    return _paginate(rows, page_index, page_size)


def get_order_report_channels(db: Session):
    rows = _safe(db, """
        SELECT DISTINCT "Channel"
        FROM twam."BillingOrders"
        WHERE "Channel" IS NOT NULL
        ORDER BY "Channel"
    """)
    print("rows:", rows)
    print("type:", type(rows))
    if not rows:
        return []
    print("first row:", rows[0])
    print("first row type:", type(rows[0]))
    return [r["Channel"] for r in rows]


def get_available_order_dates(db: Session, channel: str = None) -> list[str]:
    where = ['o."DeletedInd" = false']
    if channel:
        where.append(f"o.\"Channel\" ILIKE '{channel}'")

    rows = _safe(db, f"""
        SELECT DISTINCT
            TO_CHAR(o."OrderDate", 'YYYY-MM-DD') AS "date"
        FROM twam."Orders" o
        WHERE {" AND ".join(where)}
        ORDER BY "date" ASC
    """)
    return [r["date"] for r in rows if r.get("date")]


def get_products_category_report(db, type_val, id_val, from_date, to_date, channel, page_index, page_size):

    if not id_val:
        return _paginate([], page_index, page_size)

    date_where    = ""
    channel_where = ""

    if from_date and to_date:
        date_where = f'AND o."CreatedDate" BETWEEN \'{from_date}\' AND \'{to_date}\''

    if channel:
        channel_where = f"AND o.\"Platform\" ILIKE '%{channel}%'"

    SELECT = """
                COALESCE(COUNT(DISTINCT o."OrderId"),                                         0) AS "totalOrders",
                COALESCE(SUM(oi."Quantity"),                                                   0) AS "unitsSold",
                COALESCE(SUM(oi."Price" * oi."Quantity"),                                      0) AS "grossRevenue",
                COALESCE(SUM((oi."Price" - COALESCE(oi."TaxAmount",0)) * oi."Quantity"),       0) AS "taxableValue",
                COALESCE(SUM(COALESCE(oi."CGST",0) * oi."Quantity"),                           0) AS "cgst",
                COALESCE(SUM(COALESCE(oi."SGST",0) * oi."Quantity"),                           0) AS "sgst",
                COALESCE(SUM((COALESCE(oi."CGST",0)+COALESCE(oi."SGST",0)) * oi."Quantity"),   0) AS "gstAmount",
                COALESCE(SUM(o."TotalAmount"),                                                 0) AS "grandTotal",
                COALESCE(SUM(COALESCE(o."CouponAmount",0)),                                    0) AS "discount",
                COALESCE(SUM(o."TotalAmount"),                                                 0) AS "receivedAmount"
    """

    ORDER_JOINS = f"""
            LEFT JOIN twam."OrderItems" oi
                ON oi."ProductId" = p."ProductId"
                AND oi."DeletedInd" = false
            LEFT JOIN twam."Orders" o
                ON o."OrderId" = oi."OrderId"
                AND o."DeletedInd" = false
                AND o."State" NOT IN (
                    'AwaitingPayment', 'Payment Pending', 'Payment Failed',
                    'PaymentFailed', 'Refunded', 'Cancelled'
                )
                {date_where}
                {channel_where}
    """

    if type_val == "C":
        sql = f"""
            SELECT
                c."Name"  AS "name",
                p."Name"  AS "productName",
                {SELECT}
            FROM twam."Category" c
            LEFT JOIN twam."Products" p
                ON p."CategoryId" = c."CategoryId"
                AND p."DeletedInd" = false
            {ORDER_JOINS}
            WHERE c."DeletedInd" = false
              AND c."CategoryId" = {id_val}
            GROUP BY c."Name", p."Name"
            ORDER BY "unitsSold" DESC NULLS LAST
        """

    elif type_val == "B":
        sql = f"""
            SELECT
                b."brandName" AS "name",
                p."Name"      AS "productName",
                {SELECT}
            FROM mdm."Brand" b
            LEFT JOIN twam."Products" p
                ON p."BrandId" = b."brandId"
                AND p."DeletedInd" = false
            {ORDER_JOINS}
            WHERE b."brandId" = {id_val}
            GROUP BY b."brandName", p."Name"
            ORDER BY "unitsSold" DESC NULLS LAST
        """

    else:
        sql = f"""
            SELECT
                p."Name" AS "name",
                p."Name" AS "productName",
                {SELECT}
            FROM twam."Products" p
            {ORDER_JOINS}
            WHERE p."DeletedInd" = false
              AND p."ProductId" = {id_val}
            GROUP BY p."Name"
            ORDER BY "unitsSold" DESC NULLS LAST
        """

    try:
        result = db.execute(text(sql))
        rows = [
            {
                "name":           r["name"],
                "productName":    r["productName"],
                "type":           type_val,
                "totalOrders":    int(r["totalOrders"]     or 0),
                "unitsSold":      int(r["unitsSold"]        or 0),
                "grossRevenue":   float(r["grossRevenue"]   or 0),
                "taxableValue":   float(r["taxableValue"]   or 0),
                "cgst":           float(r["cgst"]           or 0),
                "sgst":           float(r["sgst"]           or 0),
                "gstAmount":      float(r["gstAmount"]      or 0),
                "grandTotal":     float(r["grandTotal"]     or 0),
                "discount":       float(r["discount"]       or 0),
                "receivedAmount": float(r["receivedAmount"] or 0),
            }
            for r in result.mappings().all()
        ]
        return _paginate(rows, page_index, page_size)

    except Exception as e:
        db.rollback()
        return _paginate([], page_index, page_size)


def get_sales_by_region(db: Session, filters, page_index, page_size):
    if filters is None:
        filters = {}

    where_clauses = ['o."DeletedInd" = false']

    from_date = filters.get("fromDate")
    to_date   = filters.get("toDate")
    city      = filters.get("city")
    state     = filters.get("state")

    if from_date:
        where_clauses.append(f"o.\"OrderDate\" >= '{from_date}'")
    if to_date:
        where_clauses.append(f"o.\"OrderDate\" <= '{to_date} 23:59:59'")
    if city:
        where_clauses.append(f"a.\"City\" ILIKE '{city}'")
    if state:
        where_clauses.append(f"a.\"StateId\" = {state}")

    where_sql = " AND ".join(where_clauses)

    rows = _safe(db, f"""
        SELECT
            COALESCE(a."City", 'Unknown')               AS "Region",
            COUNT(DISTINCT o."OrderId")                  AS "TotalOrders",
            COUNT(DISTINCT o."UserProfileId")            AS "Customers",
            COALESCE(SUM(oi."Quantity"), 0)              AS "unitsSold",
            COALESCE(SUM(o."TotalAmount"), 0)            AS "GrossRevenue",
            COALESCE(SUM(o."SubTotal"),    0)            AS "TaxableValue",
            COALESCE(SUM(o."TaxAmount"),   0)            AS "GSTAmount"
        FROM twam."Orders" o
        LEFT JOIN twam."Address" a
            ON  a."UserProfileId" = o."UserProfileId"
            AND a."IsDefault"     = true
            AND a."DeletedInd"    = false
        LEFT JOIN twam."OrderItems" oi
            ON  oi."OrderId"    = o."OrderId"
            AND oi."DeletedInd" = false
        WHERE {where_sql}
        GROUP BY a."City"
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
    if filters is None:
        filters = {}

    from_date   = filters.get("fromDate")
    to_date     = filters.get("toDate")
    org_id      = filters.get("organizationId")
    report_type = filters.get("type", "all")

    def date_join_clause(alias: str) -> str:
        parts = []
        if from_date:
            parts.append(f'{alias}."UsedAt" >= \'{from_date}\'')
        if to_date:
            parts.append(f'{alias}."UsedAt" <= \'{to_date} 23:59:59\'')
        return (" AND " + " AND ".join(parts)) if parts else ""

    def order_date_clause(alias: str) -> str:
        parts = []
        if from_date:
            parts.append(f'{alias}."OrderDate" >= \'{from_date}\'')
        if to_date:
            parts.append(f'{alias}."OrderDate" <= \'{to_date} 23:59:59\'')
        return (" AND " + " AND ".join(parts)) if parts else ""

    def org_clause(alias: str) -> str:
        return f'AND {alias}."OrganizationId" = {org_id}' if org_id else ''

    # ── PART 1: Digital Coupons ───────────────────────────────────────────────
    coupon_sql = f"""
        SELECT
            'coupon'::text                                  AS "ReportType",
            c."CouponCode"::text                            AS "Code",
            c."CouponName"::text                            AS "Name",
            c."Description"::text                           AS "Description",
            c."Discount"::numeric                           AS "DiscountValue",
            NULL::text                                      AS "GiftType",
            COALESCE(c."CouponType", c."State")::text       AS "SubType",
            c."StartDate"::text                             AS "StartDate",
            c."EndDate"::text                               AS "EndDate",
            c."IsActive"::boolean                           AS "IsActive",
            COUNT(DISTINCT cu."UsageId")::bigint            AS "TimesUsed",
            COALESCE(SUM(o."CouponAmount"), 0)::numeric     AS "TotalDiscount",
            COALESCE(SUM(o."TotalAmount"),  0)::numeric     AS "TotalRevenue",
            NULL::text                                      AS "SKU",
            NULL::text                                      AS "HSNCode",
            NULL::numeric                                   AS "Weight"
        FROM coupons."Coupons" c
        LEFT JOIN coupons."CouponUsage" cu
            ON  cu."CouponId" = c."CouponId"
            {date_join_clause('cu')}
        LEFT JOIN twam."Orders" o
            ON  o."OrderId"    = cu."OrderId"
            AND o."DeletedInd" = false
        WHERE c."DeletedInd" = false
          {org_clause('c')}
        GROUP BY
            c."CouponId", c."CouponCode", c."CouponName", c."Description",
            c."Discount", c."CouponType", c."State",
            c."StartDate", c."EndDate", c."IsActive"
    """

    # ── PART 2: Physical Coupons ──────────────────────────────────────────────
    _pc_from_clause = f"AND pc.\"UsedAt\" >= '{from_date}'" if from_date else ""
    _pc_to_clause   = f"AND pc.\"UsedAt\" <= '{to_date} 23:59:59'" if to_date else ""

    physical_coupon_sql = f"""
        SELECT
            'physical_coupon'::text                              AS "ReportType",
            pc."CouponCode"::text                                AS "Code",
            pc."Description"::text                               AS "Name",
            pc."Description"::text                               AS "Description",
            pc."Discount"::numeric                               AS "DiscountValue",
            NULL::text                                           AS "GiftType",
            'physical_coupon'::text                              AS "SubType",
            NULL::text                                           AS "StartDate",
            NULL::text                                           AS "EndDate",
            CASE WHEN pc."IsUsed" THEN false ELSE true END       AS "IsActive",
            SUM(CASE WHEN pc."IsUsed" THEN 1 ELSE 0 END)::bigint AS "TimesUsed",
            COALESCE(
                SUM(CASE WHEN pc."IsUsed" THEN pc."Discount" ELSE 0 END), 0
            )::numeric                                           AS "TotalDiscount",
            COALESCE(SUM(o."TotalAmount"), 0)::numeric           AS "TotalRevenue",
            NULL::text                                           AS "SKU",
            NULL::text                                           AS "HSNCode",
            NULL::numeric                                        AS "Weight"
        FROM coupons."PhysicalCoupons" pc
        LEFT JOIN twam."Orders" o
            ON  o."OrderId"    = pc."OrderId"
            AND o."DeletedInd" = false
            {order_date_clause('o')}
        WHERE 1=1
          {org_clause('pc')}
          {_pc_from_clause}
          {_pc_to_clause}
        GROUP BY
            pc."CouponCode", pc."Description", pc."Discount", pc."IsUsed"
    """

    # ── PART 3: Surprise Gifts ────────────────────────────────────────────────
    gift_sql = f"""
        SELECT
            'surprise_gift'::text                                    AS "ReportType",
            r."SKU"::text                                            AS "Code",
            r."GiftName"::text                                       AS "Name",
            r."GiftDescription"::text                                AS "Description",
            0::numeric                                               AS "DiscountValue",
            r."GiftType"::text                                       AS "GiftType",
            r."GiftType"::text                                       AS "SubType",
            NULL::text                                               AS "StartDate",
            NULL::text                                               AS "EndDate",
            r."IsActive"::boolean                                    AS "IsActive",
            COUNT(DISTINCT osg."OrderSurpriseGiftId")::bigint        AS "TimesUsed",
            0::numeric                                               AS "TotalDiscount",
            COALESCE(SUM(o."TotalAmount"), 0)::numeric               AS "TotalRevenue",
            r."SKU"::text                                            AS "SKU",
            r."HSNCode"::text                                        AS "HSNCode",
            r."Weight"::numeric                                      AS "Weight"
        FROM twam."SurpriseGiftRule" r
        LEFT JOIN twam."OrderSurpriseGift" osg
            ON  osg."GiftRuleId" = r."GiftRuleId"
            AND osg."DeletedInd" = false
        LEFT JOIN twam."Orders" o
            ON  o."OrderId"    = osg."OrderId"
            AND o."DeletedInd" = false
            {order_date_clause('o')}
        WHERE r."DeletedInd" = false
          {org_clause('r')}
        GROUP BY
            r."GiftRuleId", r."SKU", r."GiftName", r."GiftDescription",
            r."GiftType", r."IsActive", r."HSNCode", r."Weight"
    """

    if report_type == "coupon":
        final_sql = f"""
            SELECT * FROM ({coupon_sql})          coupon_data
            UNION ALL
            SELECT * FROM ({physical_coupon_sql}) phys_data
            ORDER BY "TimesUsed" DESC NULLS LAST
        """
    elif report_type == "gift":
        final_sql = f"""
            SELECT * FROM ({gift_sql}) gift_data
            ORDER BY "TimesUsed" DESC NULLS LAST
        """
    else:
        final_sql = f"""
            SELECT * FROM ({coupon_sql})          coupon_data
            UNION ALL
            SELECT * FROM ({physical_coupon_sql}) phys_data
            UNION ALL
            SELECT * FROM ({gift_sql})            gift_data
            ORDER BY "TimesUsed" DESC NULLS LAST
        """

    rows = _safe(db, final_sql)
    return _paginate(rows, page_index, page_size)


def get_gst_report(db: Session, filters, page_index, page_size):
    rows = _safe(db, """
        SELECT o."OrderId", o."CreatedDate" AS "OrderDate",
               p."Name" AS "ProductName", oi."Quantity",
               pvd."FinalPrice" AS "UnitPrice",
               (COALESCE(pvd."CGST", 0) + COALESCE(pvd."SGST", 0)) AS "GSTRate",
               t."HSNCode",
               (pvd."FinalPrice" * (COALESCE(pvd."CGST", 0) + COALESCE(pvd."SGST", 0)) / 100) AS "GSTAmount",
               (pvd."FinalPrice" + pvd."FinalPrice" * (COALESCE(pvd."CGST", 0) + COALESCE(pvd."SGST", 0)) / 100) AS "PriceWithGST"
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
            where_clauses.append('pvd."Date" BETWEEN :fromDate AND :toDate')
            params["fromDate"] = filters["fromDate"]
            params["toDate"]   = filters["toDate"]

    where_sql = " AND ".join(where_clauses)

    rows = _safe(db, f"""
        SELECT
            p."ProductId"                                AS "ProductId",
            pvd."StockId"                                AS "StockId",
            p."Name"                                     AS "ProductName",
            COALESCE(c."Name",      'Unknown')           AS "Category",
            COALESCE(b."brandName", 'Unknown')           AS "Brand",

            COALESCE(SUM(pvd."StockQuantity"),     0)    AS "TotalStock",
            COALESCE(SUM(pvd."ProcessedQuantity"), 0)    AS "UnitsSold",
            COALESCE(SUM(pvd."AvailableQuantity"), 0)    AS "RemainingStock",

            COALESCE(
                SUM(
                    COALESCE(pvd."StockQuantity", pvd."AvailableQuantity", 0)
                    * COALESCE(pvd."MRPPrice", 0)
                ), 0
            )                                            AS "GrossRevenue",

            COALESCE(
                SUM(
                    COALESCE(pvd."StockQuantity", pvd."AvailableQuantity", 0) *
                    GREATEST(
                        COALESCE(pvd."MRPPrice", 0) - COALESCE(pvd."FinalPrice", 0), 0
                    )
                ), 0
            )                                            AS "Discount",

            COALESCE(
                SUM(
                    COALESCE(pvd."StockQuantity", pvd."AvailableQuantity", 0) *
                    CASE
                        WHEN COALESCE(pvd."CGST", 0) + COALESCE(pvd."SGST", 0) > 0
                        THEN COALESCE(pvd."FinalPrice", 0) -
                             (COALESCE(pvd."FinalPrice", 0) /
                              (1 + (COALESCE(pvd."CGST", 0) + COALESCE(pvd."SGST", 0)) / 100.0))
                        ELSE 0
                    END
                ), 0
            )                                            AS "TaxAmount",

            COALESCE(
                SUM(
                    COALESCE(pvd."AvailableQuantity", 0) *
                    CASE
                        WHEN COALESCE(pvd."CGST", 0) + COALESCE(pvd."SGST", 0) > 0
                        THEN COALESCE(pvd."FinalPrice", 0) /
                             (1 + (COALESCE(pvd."CGST", 0) + COALESCE(pvd."SGST", 0)) / 100.0)
                        ELSE COALESCE(pvd."FinalPrice", 0)
                    END
                ), 0
            )                                            AS "NetRevenue"

        FROM twam."Products" p

        JOIN twam."ProductVariantDetail" pvd
            ON pvd."ProductId"  = p."ProductId"
           AND pvd."DeletedInd" = false

        LEFT JOIN twam."Category" c
            ON c."CategoryId" = p."CategoryId"
           AND c."DeletedInd" = false

        LEFT JOIN mdm."Brand" b
            ON b."brandId"    = p."BrandId"
           AND b."deletedInd" = false

        WHERE {where_sql}

        GROUP BY p."ProductId", pvd."StockId", p."Name", c."Name", b."brandName"
        ORDER BY
            SUM(
                COALESCE(pvd."StockQuantity", pvd."AvailableQuantity", 0)
                * COALESCE(pvd."MRPPrice", 0)
            ) DESC NULLS LAST
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
        SELECT o."OrderId",
               o."CreatedDate",
               o."State"        AS "OrderState",
               s."TrackingId"   AS "TrackingNumber",
               s."State"        AS "ShipmentStatus",
               s."PaymentMode",
               s."TotalAmount"  AS "ShipmentAmount",
               s."Quantity",
               s."InvoiceNumber",
               s."InvoiceDate"
        FROM twam."Orders" o
        LEFT JOIN shipment."Shipment" s
               ON s."OrderId" = o."OrderId"
              AND s."DeletedInd" = false
        WHERE o."DeletedInd" = false
        ORDER BY o."CreatedDate" DESC
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


def get_return_rate_reason(db, filters, page_index, page_size):
    sql = """
        SELECT ori."Reason" AS "ReturnReason", 
               COUNT(*) AS "TotalReturns",
               SUM(o."TotalAmount") AS "ReturnValue"
        FROM twam."OrderReturnInfo" ori
        LEFT JOIN twam."Orders" o ON o."OrderId" = ori."OrderId"
        WHERE ori."DeletedInd" = false
        GROUP BY ori."Reason" 
        ORDER BY "TotalReturns" DESC
    """
    rows = db.execute(text(sql)).mappings().all()
    rows = [dict(r) for r in rows]
    return _paginate(rows, page_index, page_size)


def get_failed_payment_report(db: Session, filters, page_index, page_size):
    rows = _safe(db, """
        SELECT 
            o."OrderId",
            o."CreatedDate",
            o."TotalAmount",
            o."PaymentMethod",
            CASE r."PaymentStatus"
                WHEN 0 THEN 'Failed'
                WHEN 1 THEN 'Approved'
                WHEN 2 THEN 'Pending'
                ELSE 'Unknown'
            END AS "PaymentStatus",
            o."State"
        FROM twam."Orders" o
        INNER JOIN payment."OrderRefund" r ON r."OrderId" = o."OrderId"
        WHERE o."DeletedInd" = false
          AND r."DeletedInd" = false
          AND r."PaymentStatus" IN (0, 2)
        ORDER BY o."CreatedDate" DESC
    """)
    return _paginate(rows, page_index, page_size)


def get_tax_by_region(db: Session, filters, page_index, page_size):
    rows = _safe(db, """
        SELECT 
            o."State" AS "Region",
            SUM(o."TaxAmount") AS "TotalTax",
            COUNT(DISTINCT i."InvoiceId") AS "InvoiceCount"
        FROM twam."Orders" o
        LEFT JOIN twam."Invoice" i ON i."OrderId" = o."OrderId"
        WHERE o."DeletedInd" = false
        GROUP BY o."State" 
        ORDER BY "TotalTax" DESC NULLS LAST
    """)
    return _paginate(rows, page_index, page_size)


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
    group_by = (filters or {}).get("group_by", "month")

    trunc_map = {
        "day":   "day",
        "week":  "week",
        "month": "month",
        "year":  "year",
    }
    trunc = trunc_map.get(group_by, "month")

    rows = _safe(db, f"""
        WITH period_customers AS (
            SELECT
                DATE_TRUNC('{trunc}', u."CreatedDate")  AS "Period",
                u."Id"                                   AS "UserId",
                u."EmailConfirmed"                       AS "IsVerified"
            FROM auth."IdentityUser" u
            WHERE u."DeletedInd" = false
              AND u."UserRoleId" = 2
        )
        SELECT
            "Period",
            COUNT("UserId")                          AS "NewCustomers",
            COUNT("UserId") FILTER (WHERE "IsVerified" = true) AS "VerifiedCustomers",
            SUM(COUNT("UserId")) OVER ()              AS "TotalNewCustomers"
        FROM period_customers
        GROUP BY "Period"
        ORDER BY "Period" DESC
    """)

    return _paginate(rows, page_index, page_size)


def get_repeat_vs_new_customers(db: Session, filters, page_index, page_size):
    filters    = filters or {}
    start_date = filters.get("start_date") or None
    end_date   = filters.get("end_date")   or None

    date_filter = ""
    params: dict = {}
    if start_date:
        date_filter += " AND o.\"OrderDate\" >= :start_date ::date"
        params["start_date"] = start_date
    if end_date:
        date_filter += " AND o.\"OrderDate\" <= :end_date ::date"
        params["end_date"] = end_date

    rows = _safe(db, f"""
        WITH customer_orders AS (
            SELECT
                o."UserProfileId",
                COUNT(o."OrderId") AS "OrderCount"
            FROM twam."Orders" o
            WHERE o."DeletedInd" = false
              AND o."State" NOT IN ('Cancelled', 'Returned')
              {date_filter}
            GROUP BY o."UserProfileId"
        ),
        classified AS (
            SELECT
                CASE WHEN "OrderCount" = 1 THEN 'New' ELSE 'Repeat' END AS "CustomerType",
                COUNT(*) AS "CustomerCount"
            FROM customer_orders
            GROUP BY 1
        )
        SELECT
            "CustomerType",
            "CustomerCount",
            ROUND("CustomerCount" * 100.0 / NULLIF(SUM("CustomerCount") OVER (), 0), 2) AS "Percentage",
            SUM("CustomerCount") OVER () AS "TotalCustomers"
        FROM classified
        ORDER BY "CustomerType"
    """, params)

    return _paginate(rows, page_index, page_size)


def get_customer_lifetime_value(db: Session, filters, page_index, page_size):
    min_orders = int((filters or {}).get("min_orders", 1))
    top_n      = int((filters or {}).get("top_n", 20))

    rows = _safe(db, """
        SELECT
            u."Id"                                                                 AS "UserId",
            TRIM(COALESCE(u."FirstName", '') || ' ' || COALESCE(u."LastName", '')) AS "CustomerName",
            u."Email",
            u."PhoneNumber"                                                        AS "Phone",
            COUNT(o."OrderId")                                                     AS "OrderCount",
            SUM(o."TotalAmount")                                                   AS "TotalRevenue",
            ROUND(AVG(o."TotalAmount"), 2)                                         AS "AvgOrderValue",
            MIN(o."OrderDate")::date                                               AS "FirstOrderDate",
            MAX(o."OrderDate")::date                                               AS "LastOrderDate",
            EXTRACT(DAY FROM NOW() - MIN(o."OrderDate"))                           AS "DaysAsCustomer"
        FROM auth."IdentityUser" u
        JOIN twam."Orders" o ON o."UserProfileId" = u."Id" AND o."DeletedInd" = false
        WHERE u."DeletedInd" = false
          AND u."UserRoleId" = 2
          AND o."State" NOT IN ('Cancelled', 'Returned')
        GROUP BY u."Id", u."FirstName", u."LastName", u."Email", u."PhoneNumber"
        HAVING COUNT(o."OrderId") >= :min_orders
        ORDER BY "TotalRevenue" DESC
        LIMIT :top_n
    """, {"min_orders": min_orders, "top_n": top_n})

    return _paginate(rows, page_index, page_size)


def get_customer_segmentation(db: Session, filters, page_index, page_size):
    segment_by = (filters or {}).get("segment_by", "location")

    if segment_by == "spending":
        rows = _safe(db, """
            SELECT
                CASE
                    WHEN total >= 10000 THEN 'High (≥10k)'
                    WHEN total >= 2000  THEN 'Mid (2k–10k)'
                    ELSE 'Low (<2k)'
                END                AS "SpendingTier",
                COUNT(*)           AS "CustomerCount"
            FROM (
                SELECT o."UserProfileId", SUM(o."TotalAmount") AS total
                FROM twam."Orders" o
                WHERE o."DeletedInd" = false
                  AND o."State" NOT IN ('Cancelled', 'Returned')
                GROUP BY o."UserProfileId"
            ) sub
            GROUP BY 1
            ORDER BY "CustomerCount" DESC
        """)

    elif segment_by == "frequency":
        rows = _safe(db, """
            SELECT
                CASE
                    WHEN "OrderCount" >= 10 THEN 'Frequent (10+)'
                    WHEN "OrderCount" >= 3  THEN 'Occasional (3–9)'
                    ELSE 'One-time (1–2)'
                END                        AS "FrequencySegment",
                COUNT("UserProfileId")     AS "CustomerCount"
            FROM (
                SELECT o."UserProfileId", COUNT(o."OrderId") AS "OrderCount"
                FROM twam."Orders" o
                WHERE o."DeletedInd" = false
                  AND o."State" NOT IN ('Cancelled', 'Returned')
                GROUP BY o."UserProfileId"
            ) sub
            GROUP BY "FrequencySegment"
            ORDER BY "CustomerCount" DESC
        """)

    else:
        rows = _safe(db, """
            SELECT
                COALESCE(o."State", 'Unknown')          AS "Location",
                COUNT(DISTINCT o."UserProfileId")        AS "CustomerCount",
                SUM(o."TotalAmount")                     AS "TotalRevenue",
                COUNT(o."OrderId")                       AS "TotalOrders"
            FROM twam."Orders" o
            WHERE o."DeletedInd" = false
              AND o."State" NOT IN ('Cancelled', 'Returned')
            GROUP BY o."State"
            ORDER BY "CustomerCount" DESC
            LIMIT 20
        """)

    return _paginate(rows, page_index, page_size)


def get_customer_churn_rate(db: Session, filters, page_index, page_size):
    inactive_days = int((filters or {}).get("inactive_days", 90))
    period_months = int((filters or {}).get("period_months", 12))

    query = """
        WITH customer_last_order AS (
            SELECT
                iu."Id" AS "UserId",
                iu."FirstName",
                iu."LastName",
                MAX(o."OrderDate") AS "LastOrderDate",
                COUNT(o."OrderId") AS "TotalOrders",
                COALESCE(SUM(o."TotalAmount"), 0) AS "TotalSpent"
            FROM auth."IdentityUser" iu
            LEFT JOIN twam."Orders" o
                ON iu."Id" = o."UserProfileId"
                AND o."DeletedInd" = false
            WHERE iu."DeletedInd" = false
            GROUP BY
                iu."Id",
                iu."FirstName",
                iu."LastName"
        )

        SELECT
            "UserId",
            CONCAT(
                COALESCE("FirstName", ''),
                ' ',
                COALESCE("LastName", '')
            ) AS "CustomerName",

            "LastOrderDate",

            EXTRACT(DAY FROM (NOW() - "LastOrderDate"))::INT
                AS "InactiveDays",

            "TotalOrders",

            "TotalSpent",

            CASE
                WHEN "LastOrderDate" IS NULL THEN 'Churned'
                WHEN "LastOrderDate" < NOW() - (:inactive_days || ' days')::INTERVAL
                    THEN 'Churned'
                ELSE 'Active'
            END AS "CustomerStatus"

        FROM customer_last_order

        WHERE
            (
                "LastOrderDate" IS NULL
                OR
                "LastOrderDate" < NOW() - (:inactive_days || ' days')::INTERVAL
            )

        ORDER BY "LastOrderDate" ASC NULLS FIRST
    """

    rows = _safe(
        db,
        query,
        {
            "inactive_days": inactive_days,
            "period_months": period_months
        }
    )

    return _paginate(rows, page_index, page_size)


def get_top_customers_by_revenue(db: Session, filters, page_index, page_size):
    filters    = filters or {}
    start_date = filters.get("start_date") or None
    end_date   = filters.get("end_date")   or None
    top_n      = int(filters.get("top_n", 10))

    start_expr = ":start_date ::date" if start_date else "(NOW() - INTERVAL '1 year')::date"
    end_expr   = ":end_date ::date"   if end_date   else "NOW()::date"

    params: dict = {"top_n": top_n}
    if start_date: params["start_date"] = start_date
    if end_date:   params["end_date"]   = end_date

    rows = _safe(db, f"""
        SELECT
            RANK() OVER (ORDER BY SUM(o."TotalAmount") DESC)                       AS "Rank",
            u."Id"                                                                  AS "UserId",
            TRIM(COALESCE(u."FirstName", '') || ' ' || COALESCE(u."LastName", '')) AS "CustomerName",
            u."Email",
            u."PhoneNumber"                                                         AS "Phone",
            COUNT(o."OrderId")                                                      AS "OrderCount",
            SUM(o."TotalAmount")                                                    AS "TotalRevenue",
            ROUND(
                SUM(o."TotalAmount") * 100.0 /
                NULLIF(SUM(SUM(o."TotalAmount")) OVER (), 0), 2
            )                                                                       AS "RevenueSharePct",
            MAX(o."OrderDate")::date                                                AS "LastOrderDate",
            SUM(SUM(o."TotalAmount")) OVER ()                                       AS "GrandTotalRevenue"
        FROM auth."IdentityUser" u
        JOIN twam."Orders" o ON o."UserProfileId" = u."Id" AND o."DeletedInd" = false
        WHERE u."DeletedInd" = false
          AND u."UserRoleId" = 2
          AND o."State" NOT IN ('Cancelled', 'Returned')
          AND o."OrderDate" >= {start_expr}
          AND o."OrderDate" <= {end_expr}
        GROUP BY u."Id", u."FirstName", u."LastName", u."Email", u."PhoneNumber"
        ORDER BY "TotalRevenue" DESC
        LIMIT :top_n
    """, params)

    return _paginate(rows, page_index, page_size)


# ===================new reports above===================

def _parse_filters(raw) -> dict:
    if not raw:
        return {}
    try:
        if isinstance(raw, dict):
            return raw
        return json_loads(raw)
    except ValueError:
        return {}


def get_stock_level_report(
    db: Session,
    filters,
    page_index: Optional[int] = 1,
    page_size: Optional[int] = 20,
):
    f = _parse_filters(filters)

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
        except (ValueError, TypeError):
            pass

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
        except (ValueError, TypeError):
            pass

    if f.get("maxStock") is not None:
        try:
            params["maxStock"] = int(f["maxStock"])
            where_clauses.append('pvd."StockQuantity" <= :maxStock')
        except (ValueError, TypeError):
            pass

    where_sql = " AND ".join(where_clauses)

    base_from = f"""
        FROM twam."ProductVariantDetail" pvd
        LEFT JOIN twam."ProductVariants"  pv  ON pv."ProductVariantId"  = pvd."ProductVariantId"
        LEFT JOIN twam."Products"          p   ON p."ProductId"          = pv."ProductId"
        LEFT JOIN twam."Category"          c   ON c."CategoryId"         = p."CategoryId"
        WHERE {where_sql}
    """

    count_params = dict(params)
    total = 0
    try:
        total = db.execute(
            text(f'SELECT COUNT(*) {base_from}'), count_params
        ).scalar() or 0
    except Exception as e:
        traceback.print_exc()
        db.rollback()
        return {
            "TotalCount": 0,
            "Page":       page_index or 1,
            "PageSize":   page_size or 20,
            "Data":       [],
        }

    page_index = max(int(page_index or 1), 1)
    page_size  = max(int(page_size  or 20), 1)
    params["limit"]  = page_size
    params["offset"] = (page_index - 1) * page_size

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


def get_inventory_aging_report(
    db: Session,
    filters,
    page_index: Optional[int] = 1,
    page_size:  Optional[int] = 20,
):
    f = _parse_filters(filters)

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

    count_params = dict(params)
    total = 0
    try:
        total = db.execute(
            text(f"SELECT COUNT(*) {base_from}"), count_params
        ).scalar() or 0
    except Exception as e:
        traceback.print_exc()
        db.rollback()
        return {"TotalCount": 0, "Page": page_index or 1, "PageSize": page_size or 20, "Data": []}

    page_index = max(int(page_index or 1), 1)
    page_size  = max(int(page_size  or 20), 1)
    params["limit"]  = page_size
    params["offset"] = (page_index - 1) * page_size

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
        for row in rows:
            if row.get("ReceivedDate") is not None:
                row["ReceivedDate"] = str(row["ReceivedDate"])

    except Exception as e:
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

    if f.get("search"):
        where.append('(p."Name" ILIKE :search OR pvd."StockId" ILIKE :search)')
        params["search"] = f"%{f['search']}%"

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

    params["limit"]  = page_size
    params["offset"] = (page_index - 1) * page_size

    result = db.execute(text(query), params)
    rows = [dict(r._mapping) for r in result]

    return {
        "TotalCount": len(rows),
        "Page":       page_index,
        "PageSize":   page_size,
        "Data":       rows,
    }