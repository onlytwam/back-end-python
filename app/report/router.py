import json
import re
import traceback
from typing import Optional
from fastapi import APIRouter, Depends, Query, Request
# from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from urllib3 import _DEFAULT_POOL
from database import get_db
from app.report import schemas, repository
from app.auth.dependencies import get_current_user, require_roles, Roles, CurrentUser
from fastapi.responses import JSONResponse
router = APIRouter(
    dependencies=[Depends(get_current_user)],prefix="/api/Report", tags=["Report"])

def _paging(Page_Index, Page_Size, Filters):
    if not Filters:
        return None, Page_Index, Page_Size
    try:
        parsed = json.loads(Filters)
    except (json.JSONDecodeError, ValueError):
        parsed = {}
        for part in Filters.split(","):
            if ":" in part:
                k, v = part.split(":", 1)
                parsed[k.strip()] = v.strip()
    return parsed, Page_Index, Page_Size
@router.get("/OrderReport/Channels")
def order_report_channels(db: Session = Depends(get_db)):
    channels = repository.get_order_report_channels(db)
    return JSONResponse(content=channels)
@router.get("/OrderReport/AvailableDates")
def get_available_dates(channel: str = None, db: Session = Depends(get_db)):
    return repository.get_available_order_dates(db, channel)

@router.get("/OrderList", response_model=schemas.ReportListResponse)
def order_list(Filters: Optional[str]=Query(None,alias="Filters"),
    Page_Index: Optional[int]=Query(None,alias="Page.Index",ge=1),
    Page_Size: Optional[int]=Query(None,alias="Page.Size",ge=1), db: Session=Depends(get_db)):
    return repository.get_order_report(db, *_paging(Page_Index, Page_Size, Filters))

# router.py

@router.get("/ProductsCategoryReport", response_model=schemas.ReportListResponse)
def products_category_report(
    request:    Request,
    Page_Index: Optional[int] = Query(None, alias="Page.Index", ge=1),
    Page_Size:  Optional[int] = Query(None, alias="Page.Size",  ge=1),
    db:         Session = Depends(get_db)
):
    params      = dict(request.query_params)
    filters_map = {}
    for key, val in params.items():
        m = re.match(r"filters\[(\d+)\]\.(\w+)", key)
        if m:
            idx, field = m.group(1), m.group(2)
            filters_map.setdefault(idx, {})[field] = val

    parsed    = { f["property"]: f.get("value", "") for f in filters_map.values() if "property" in f }
    type_val  = parsed.get("Type",     "P")
    id_val    = parsed.get("Id",       "")
    from_date = parsed.get("FromDate", None)
    to_date   = parsed.get("ToDate",   None)

    return repository.get_products_category_report(
        db, type_val, id_val, from_date, to_date, None, Page_Index, Page_Size
    )

# ✅ NEW — reads all params directly from query string
@router.get("/SalesByRegionReport", response_model=schemas.ReportListResponse)
def sales_by_region(
    state:      Optional[str] = Query(None),
    city:       Optional[str] = Query(None),
    fromDate:   Optional[str] = Query(None),
    toDate:     Optional[str] = Query(None),
    Page_Index: Optional[int] = Query(None, alias="Page.Index", ge=1),
    Page_Size:  Optional[int] = Query(None, alias="Page.Size",  ge=1),
    db: Session = Depends(get_db),
):
    # Build filters dict from direct query params
    filters: dict = {}
    if state:    filters["state"]    = state      # ← "Telangana"
    if city:     filters["city"]     = city
    if fromDate: filters["fromDate"] = fromDate   # ← "2026-03-01"
    if toDate:   filters["toDate"]   = toDate

    page_index = Page_Index or 1
    page_size  = Page_Size  or 100

    return repository.get_sales_by_region(db, filters, page_index, page_size)

@router.get("/TopLowSellingReport", response_model=schemas.ReportListResponse)
def top_low_selling(Filters: Optional[str]=Query(None,alias="Filters"),
    Page_Index: Optional[int]=Query(None,alias="Page.Index",ge=1),
    Page_Size: Optional[int]=Query(None,alias="Page.Size",ge=1), db: Session=Depends(get_db)):
    return repository.get_top_low_selling(db, *_paging(Page_Index, Page_Size, Filters))

#ProductsCategoryReport APi router
@router.get("/ProductsCategoryReport", response_model=schemas.ReportListResponse)
def products_category_report(
    request:    Request,
    # ── accept both lowercase (frontend) and uppercase (legacy) ──────────────
    page_index: Optional[int] = Query(None, alias="page.index", ge=1),
    page_size:  Optional[int] = Query(None, alias="page.size",  ge=1),
    Page_Index: Optional[int] = Query(None, alias="Page.Index", ge=1),
    Page_Size:  Optional[int] = Query(None, alias="Page.Size",  ge=1),
    db:         Session = Depends(get_db)
):
    params      = dict(request.query_params)
    filters_map = {}
    for key, val in params.items():
        # ── case-insensitive match for filters[N].property/value/comparison ──
        m = re.match(r"filters\[(\d+)\]\.(property|value|comparison)", key, re.IGNORECASE)
        if m:
            idx   = m.group(1)
            field = m.group(2).lower()   # normalize to lowercase
            filters_map.setdefault(idx, {})[field] = val

    parsed    = { f["property"]: f.get("value", "") for f in filters_map.values() if "property" in f }
    type_val  = parsed.get("Type",     "P")
    id_val    = parsed.get("Id",       "")
    from_date = parsed.get("FromDate", None)
    to_date   = parsed.get("ToDate",   None)

    # ── use whichever page params arrived ────────────────────────────────────
    pi = page_index or Page_Index
    ps = page_size  or Page_Size

    # # print(f">>> type={type_val} id={id_val} from={from_date} to={to_date} page={pi}/{ps}")

    return repository.get_products_category_report(
        db, type_val, id_val, from_date, to_date, None, pi, ps
    )

@router.get("/SalesbyPlatform", response_model=schemas.ReportListResponse)
def sales_by_platform(Filters: Optional[str]=Query(None,alias="Filters"),
    Page_Index: Optional[int]=Query(None,alias="Page.Index",ge=1),
    Page_Size: Optional[int]=Query(None,alias="Page.Size",ge=1), db: Session=Depends(get_db)):
    return repository.get_sales_by_platform(db, *_paging(Page_Index, Page_Size, Filters))

@router.get("/CouponUsageReport", response_model=schemas.ReportListResponse)
def coupon_usage(
    fromDate:       Optional[str] = Query(None),
    toDate:         Optional[str] = Query(None),
    type:           Optional[str] = Query("all"),
    organizationId: Optional[int] = Query(None),
    Page_Index:     Optional[int] = Query(None, alias="Page.Index", ge=1),
    Page_Size:      Optional[int] = Query(None, alias="Page.Size",  ge=1),
    Filters:        Optional[str] = Query(None, alias="Filters"),
    db: Session = Depends(get_db)
):
    try:
        filters = {
            "fromDate":       fromDate,
            "toDate":         toDate,
            "type":           type or "all",
            "organizationId": str(organizationId) if organizationId else None,
        }
        # ✅ Pass page_index and page_size directly — no _paging unpacking
        return repository.get_coupon_usage_report(
            db,
            filters,
            Page_Index or 1,
            Page_Size  or 20,
        )
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise

@router.get("/InvoiceReport", response_model=schemas.ReportListResponse)
def invoice_report(Filters: Optional[str]=Query(None,alias="Filters"),
    Page_Index: Optional[int]=Query(None,alias="Page.Index",ge=1),
    Page_Size: Optional[int]=Query(None,alias="Page.Size",ge=1), db: Session=Depends(get_db)):
    return repository.get_invoice_report(db, *_paging(Page_Index, Page_Size, Filters))

@router.get("/GSTReportList", response_model=schemas.ReportListResponse)
def gst_report(Filters: Optional[str]=Query(None,alias="Filters"),
    Page_Index: Optional[int]=Query(None,alias="Page.Index",ge=1),
    Page_Size: Optional[int]=Query(None,alias="Page.Size",ge=1), db: Session=Depends(get_db)):
    return repository.get_gst_report(db, *_paging(Page_Index, Page_Size, Filters))

@router.get("/NetVsRevenueReport", response_model=schemas.ReportListResponse)
def net_vs_revenue_report(
    categoryId: Optional[str] = Query(None, alias="categoryId"),
    brandId:    Optional[str] = Query(None, alias="brandId"),
    fromDate:   Optional[str] = Query(None, alias="fromDate"),
    toDate:     Optional[str] = Query(None, alias="toDate"),
    Page_Index: Optional[int] = Query(None, alias="Page.Index", ge=1),
    Page_Size:  Optional[int] = Query(None, alias="Page.Size",  ge=1),
    db: Session = Depends(get_db)
):
    filters = {}
    if categoryId: filters["categoryId"] = categoryId
    if brandId:    filters["brandId"]    = brandId
    if fromDate:   filters["fromDate"]   = fromDate
    if toDate:     filters["toDate"]     = toDate

    _, pi, ps = _paging(Page_Index, Page_Size, None)
    return repository.get_net_vs_revenue_report(db, filters, pi, ps)

@router.get("/OrderSummary", response_model=schemas.ReportListResponse)
def order_summary(Filters: Optional[str]=Query(None,alias="Filters"),
    Page_Index: Optional[int]=Query(None,alias="Page.Index",ge=1),
    Page_Size: Optional[int]=Query(None,alias="Page.Size",ge=1), db: Session=Depends(get_db)):
    return repository.get_order_summary(db, *_paging(Page_Index, Page_Size, Filters))

@router.get("/OrderShippingStatus", response_model=schemas.ReportListResponse)
def order_shipping_status(Filters: Optional[str]=Query(None,alias="Filters"),
    Page_Index: Optional[int]=Query(None,alias="Page.Index",ge=1),
    Page_Size: Optional[int]=Query(None,alias="Page.Size",ge=1), db: Session=Depends(get_db)):
    return repository.get_order_shipping_status(db, *_paging(Page_Index, Page_Size, Filters))

@router.get("/AverageOrderValue", response_model=schemas.ReportListResponse)
def average_order_value(Filters: Optional[str]=Query(None,alias="Filters"),
    Page_Index: Optional[int]=Query(None,alias="Page.Index",ge=1),
    Page_Size: Optional[int]=Query(None,alias="Page.Size",ge=1), db: Session=Depends(get_db)):
    return repository.get_average_order_value(db, *_paging(Page_Index, Page_Size, Filters))

@router.get("/OrderFulfillment", response_model=schemas.ReportListResponse)
def order_fulfillment(Filters: Optional[str]=Query(None,alias="Filters"),
    Page_Index: Optional[int]=Query(None,alias="Page.Index",ge=1),
    Page_Size: Optional[int]=Query(None,alias="Page.Size",ge=1), db: Session=Depends(get_db)):
    return repository.get_order_fulfillment(db, *_paging(Page_Index, Page_Size, Filters))

@router.get("/StockBackOrder", response_model=schemas.ReportListResponse)
def stock_back_order(Filters: Optional[str]=Query(None,alias="Filters"),
    Page_Index: Optional[int]=Query(None,alias="Page.Index",ge=1),
    Page_Size: Optional[int]=Query(None,alias="Page.Size",ge=1), db: Session=Depends(get_db)):
    return repository.get_stock_back_order(db, *_paging(Page_Index, Page_Size, Filters))

@router.get("/PaymentMethodUsage", response_model=schemas.ReportListResponse)
def payment_method_usage(Filters: Optional[str]=Query(None,alias="Filters"),
    Page_Index: Optional[int]=Query(None,alias="Page.Index",ge=1),
    Page_Size: Optional[int]=Query(None,alias="Page.Size",ge=1), db: Session=Depends(get_db)):
    return repository.get_payment_method_usage(db, *_paging(Page_Index, Page_Size, Filters))

@router.get("/ReturnRateReason", response_model=schemas.ReportListResponse)
def return_rate_reason(Filters: Optional[str]=Query(None,alias="Filters"),
    Page_Index: Optional[int]=Query(None,alias="Page.Index",ge=1),
    Page_Size: Optional[int]=Query(None,alias="Page.Size",ge=1), db: Session=Depends(get_db)):
    return repository.get_return_rate_reason(db, *_paging(Page_Index, Page_Size, Filters))

@router.get("/FailedPaymentReport", response_model=schemas.ReportListResponse)
def failed_payment(Filters: Optional[str]=Query(None,alias="Filters"),
    Page_Index: Optional[int]=Query(None,alias="Page.Index",ge=1),
    Page_Size: Optional[int]=Query(None,alias="Page.Size",ge=1), db: Session=Depends(get_db)):
    return repository.get_failed_payment_report(db, *_paging(Page_Index, Page_Size, Filters))

@router.get("/TaxByRegionReport", response_model=schemas.ReportListResponse)
def tax_by_region(Filters: Optional[str]=Query(None,alias="Filters"),
    Page_Index: Optional[int]=Query(None,alias="Page.Index",ge=1),
    Page_Size: Optional[int]=Query(None,alias="Page.Size",ge=1), db: Session=Depends(get_db)):
    return repository.get_tax_by_region(db, *_paging(Page_Index, Page_Size, Filters))

@router.get("/StockLevelReport", response_model=schemas.StockLevelReportResponse)
def stock_level_report(
    Filters:    Optional[str] = Query(None, alias="Filters"),
    page_index: Optional[int] = Query(None, alias="Page.Index", ge=1),
    page_size:  Optional[int] = Query(None, alias="Page.Size",  ge=1),
    db: Session = Depends(get_db),
):
    try:
        return repository.get_stock_level_report(
            db,
            filters=Filters,
            page_index=page_index or 1,
            page_size=page_size   or 20,
        )
    except Exception as e:
        traceback.print_exc()          # prints full stack trace to terminal
        raise   

@router.get("/InventoryAgingReport", response_model=schemas.InventoryAgingReportResponse)
def inventory_aging_report(
    Filters:    Optional[str] = Query(None, alias="Filters"),
    page_index: Optional[int] = Query(None, alias="Page.Index", ge=1),
    page_size:  Optional[int] = Query(None, alias="Page.Size",  ge=1),
    db: Session = Depends(get_db),
):
    try:
        return repository.get_inventory_aging_report(
            db,
            filters=Filters,
            page_index=page_index or 1,
            page_size=page_size   or 20,
        )
    except Exception as e:
        traceback.print_exc()
        raise

@router.get("/FastSlowMovingInventoryReport")
def fast_slow_moving(
    Filters: Optional[str] = Query(None, alias="Filters"),
    Page_Index: int = Query(1, alias="Page.Index"),
    Page_Size: int = Query(10, alias="Page.Size"),
    db: Session = Depends(get_db)
):
    return repository.get_fast_slow_moving_inventory(
        db, Filters, Page_Index, Page_Size
    )

@router.get("/ProductReturnRateReport", response_model=schemas.ReportListResponse)
def product_return_rate(Filters: Optional[str]=Query(None,alias="Filters"),
    Page_Index: Optional[int]=Query(None,alias="Page.Index",ge=1),
    Page_Size: Optional[int]=Query(None,alias="Page.Size",ge=1), db: Session=Depends(get_db)):
    return repository.get_product_return_rate(db, *_paging(Page_Index, Page_Size, Filters))

@router.get("/StockReorderAlertsReport", response_model=schemas.ReportListResponse)
def stock_reorder_alerts(Filters: Optional[str]=Query(None,alias="Filters"),
    Page_Index: Optional[int]=Query(None,alias="Page.Index",ge=1),
    Page_Size: Optional[int]=Query(None,alias="Page.Size",ge=1), db: Session=Depends(get_db)):
    return repository.get_stock_reorder_alerts(db, *_paging(Page_Index, Page_Size, Filters))
# Customer Reports API list:
@router.get("/CustomerAcquisitionReport", response_model=schemas.ReportListResponse)
def customer_acquisition_report(
    group_by:   Optional[str] = Query("month", alias="group_by"),
    start_date: Optional[str] = Query(None,    alias="start_date"),
    end_date:   Optional[str] = Query(None,    alias="end_date"),
    Page_Index: Optional[int] = Query(1,       alias="Page.Index", ge=1),
    Page_Size:  Optional[int] = Query(200,     alias="Page.Size",  ge=1),
    db: Session = Depends(get_db),
):
    """
    New customer registrations grouped by time period.

    Query params:
      group_by   : "day|week|month|year"  (default: "month")
      start_date : "YYYY-MM-DD"           (default: 1 year ago)
      end_date   : "YYYY-MM-DD"           (default: today)
      Page.Index : int                    (default: 1)
      Page.Size  : int                    (default: 200)

    Example:
      /CustomerAcquisitionReport?group_by=month&start_date=2024-01-01
    """
    filters = {
        "group_by":   group_by,
        "start_date": start_date,
        "end_date":   end_date,
    }
    page_index = Page_Index or 1
    page_size  = Page_Size  or 200
    return repository.get_customer_acquisition_report(db, filters, page_index, page_size)
@router.get("/CustomerLifetimeValue", response_model=schemas.ReportListResponse)
def customer_lifetime_value(
    top_n:      Optional[int] = Query(20,  alias="top_n"),
    min_orders: Optional[int] = Query(1,   alias="min_orders"),
    Page_Index: Optional[int] = Query(1,   alias="Page.Index", ge=1),
    Page_Size:  Optional[int] = Query(200, alias="Page.Size",  ge=1),
    db: Session = Depends(get_db),
):
    filters = {"top_n": top_n, "min_orders": min_orders}
    return repository.get_customer_lifetime_value(db, filters, Page_Index or 1, Page_Size or 200)


@router.get("/CustomerSegmentation", response_model=schemas.ReportListResponse)
def customer_segmentation(
    segment_by: Optional[str] = Query("location", alias="segment_by"),
    Page_Index: Optional[int] = Query(1,           alias="Page.Index", ge=1),
    Page_Size:  Optional[int] = Query(200,         alias="Page.Size",  ge=1),
    db: Session = Depends(get_db),
):
    filters = {"segment_by": segment_by}
    return repository.get_customer_segmentation(db, filters, Page_Index or 1, Page_Size or 200)


@router.get("/CustomerChurnRate", response_model=schemas.ReportListResponse)
def customer_churn_rate(
    inactive_days: Optional[int] = Query(90, alias="inactive_days"),
    period_months: Optional[int] = Query(12, alias="period_months"),
    Page_Index:    Optional[int] = Query(1,  alias="Page.Index", ge=1),
    Page_Size:     Optional[int] = Query(200,alias="Page.Size",  ge=1),
    db: Session = Depends(get_db),
):
    filters = {"inactive_days": inactive_days, "period_months": period_months}
    return repository.get_customer_churn_rate(db, filters, Page_Index or 1, Page_Size or 200)


@router.get("/TopCustomersByRevenue", response_model=schemas.ReportListResponse)
def top_customers_by_revenue(
    top_n:      Optional[int] = Query(10,  alias="top_n"),
    start_date: Optional[str] = Query(None,alias="start_date"),
    end_date:   Optional[str] = Query(None,alias="end_date"),
    Page_Index: Optional[int] = Query(1,   alias="Page.Index", ge=1),
    Page_Size:  Optional[int] = Query(200, alias="Page.Size",  ge=1),
    db: Session = Depends(get_db),
):
    filters = {"top_n": top_n, "start_date": start_date, "end_date": end_date}
    return repository.get_top_customers_by_revenue(db, filters, Page_Index or 1, Page_Size or 200)


@router.get("/RepeatVsNewCustomers", response_model=schemas.ReportListResponse)
def repeat_vs_new_customers(
    start_date: Optional[str] = Query(None, alias="start_date"),
    end_date:   Optional[str] = Query(None, alias="end_date"),
    Page_Index: Optional[int] = Query(1,    alias="Page.Index", ge=1),
    Page_Size:  Optional[int] = Query(200,  alias="Page.Size",  ge=1),
    db: Session = Depends(get_db),
):
    filters = {"start_date": start_date, "end_date": end_date}
    return repository.get_repeat_vs_new_customers(db, filters, Page_Index or 1, Page_Size or 200)