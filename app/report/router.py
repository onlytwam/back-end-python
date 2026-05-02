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

router = APIRouter(
    dependencies=[Depends(get_current_user)],prefix="/api/Report", tags=["Report"])

def _paging(Page_Index, Page_Size, Filters):
    return json.loads(Filters) if Filters else None, Page_Index, Page_Size

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

@router.get("/SalesByRegionReport", response_model=schemas.ReportListResponse)
def sales_by_region(Filters: Optional[str]=Query(None,alias="Filters"),
    Page_Index: Optional[int]=Query(None,alias="Page.Index",ge=1),
    Page_Size: Optional[int]=Query(None,alias="Page.Size",ge=1), db: Session=Depends(get_db)):
    return repository.get_sales_by_region(db, *_paging(Page_Index, Page_Size, Filters))

@router.get("/TopLowSellingReport", response_model=schemas.ReportListResponse)
def top_low_selling(Filters: Optional[str]=Query(None,alias="Filters"),
    Page_Index: Optional[int]=Query(None,alias="Page.Index",ge=1),
    Page_Size: Optional[int]=Query(None,alias="Page.Size",ge=1), db: Session=Depends(get_db)):
    return repository.get_top_low_selling(db, *_paging(Page_Index, Page_Size, Filters))

#ProductsCategoryReport APi router
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

    print(f">>> type_val={type_val}, id_val={id_val}, from={from_date}, to={to_date}")  # debug

    return repository.get_products_category_report(
        db, type_val, id_val, from_date, to_date, None, Page_Index, Page_Size
    )

@router.get("/SalesbyPlatform", response_model=schemas.ReportListResponse)
def sales_by_platform(Filters: Optional[str]=Query(None,alias="Filters"),
    Page_Index: Optional[int]=Query(None,alias="Page.Index",ge=1),
    Page_Size: Optional[int]=Query(None,alias="Page.Size",ge=1), db: Session=Depends(get_db)):
    return repository.get_sales_by_platform(db, *_paging(Page_Index, Page_Size, Filters))

@router.get("/CouponUsageReport", response_model=schemas.ReportListResponse)
def coupon_usage(Filters: Optional[str]=Query(None,alias="Filters"),
    Page_Index: Optional[int]=Query(None,alias="Page.Index",ge=1),
    Page_Size: Optional[int]=Query(None,alias="Page.Size",ge=1), db: Session=Depends(get_db)):
    return repository.get_coupon_usage_report(db, *_paging(Page_Index, Page_Size, Filters))

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
 
 
@router.get("/RepeatVsNewCustomers", response_model=schemas.ReportListResponse)
def repeat_vs_new_customers(
    Filters: Optional[str] = Query(None, alias="Filters"),
    Page_Index: Optional[int] = Query(None, alias="Page.Index", ge=1),
    Page_Size: Optional[int] = Query(None, alias="Page.Size", ge=1),
    db: Session = Depends(get_db),
):
    """
    Splits customers in a date window into New vs Repeat buyers.
    New = customer placed their first-ever order in this period.
    Repeat = had at least one order before this period.
 
    Filters (JSON):
      start_date : "YYYY-MM-DD"  (default: 90 days ago)
      end_date   : "YYYY-MM-DD"  (default: today)
 
    Example:
      /RepeatVsNewCustomers?Filters={"start_date":"2024-01-01","end_date":"2024-12-31"}
    """
    return repository.get_repeat_vs_new_customers(
        db, *_paging(Page_Index, Page_Size, Filters)
    )
 
 
@router.get("/CustomerLifetimeValue", response_model=schemas.ReportListResponse)
def customer_lifetime_value(
    Filters: Optional[str] = Query(None, alias="Filters"),
    Page_Index: Optional[int] = Query(None, alias="Page.Index", ge=1),
    Page_Size: Optional[int] = Query(None, alias="Page.Size", ge=1),
    db: Session = Depends(get_db),
):
    """
    CLV report: total revenue, avg order value, order count,
    first/last order dates, and days as customer per user.
 
    Filters (JSON):
      min_orders : int  (default: 1)   — exclude customers with fewer orders
      top_n      : int  (default: 20)  — limit results
 
    Example:
      /CustomerLifetimeValue?Filters={"top_n":50,"min_orders":2}
    """
    return repository.get_customer_lifetime_value(
        db, *_paging(Page_Index, Page_Size, Filters)
    )
 
 
@router.get("/CustomerSegmentation", response_model=schemas.ReportListResponse)
def customer_segmentation(
    Filters: Optional[str] = Query(None, alias="Filters"),
    Page_Index: Optional[int] = Query(None, alias="Page.Index", ge=1),
    Page_Size: Optional[int] = Query(None, alias="Page.Size", ge=1),
    db: Session = Depends(get_db),
):
    """
    Segments customers by location, spending tier, or purchase frequency.
 
    Filters (JSON):
      segment_by : "location | spending | frequency"  (default: "location")
 
    Spending tiers  : Low (<2k) / Mid (2k–10k) / High (≥10k)
    Frequency tiers : One-time (1–2) / Occasional (3–9) / Frequent (10+)
 
    Example:
      /CustomerSegmentation?Filters={"segment_by":"spending"}
    """
    return repository.get_customer_segmentation(
        db, *_paging(Page_Index, Page_Size, Filters)
    )
 
 
@router.get("/CustomerChurnRate", response_model=schemas.ReportListResponse)
def customer_churn_rate(
    Filters: Optional[str] = Query(None, alias="Filters"),
    Page_Index: Optional[int] = Query(None, alias="Page.Index", ge=1),
    Page_Size: Optional[int] = Query(None, alias="Page.Size", ge=1),
    db: Session = Depends(get_db),
):
    """
    Churn = customers who ordered within `period_months` but whose
    last order is older than `inactive_days`. Returns monthly breakdown
    with ChurnRatePct per row.
 
    Filters (JSON):
      inactive_days  : int  (default: 90)   — days of inactivity = churned
      period_months  : int  (default: 12)   — analysis window in months
 
    Example:
      /CustomerChurnRate?Filters={"inactive_days":60,"period_months":6}
    """
    return repository.get_customer_churn_rate(
        db, *_paging(Page_Index, Page_Size, Filters)
    )
 
 
@router.get("/TopCustomersByRevenue", response_model=schemas.ReportListResponse)
def top_customers_by_revenue(
    Filters: Optional[str] = Query(None, alias="Filters"),
    Page_Index: Optional[int] = Query(None, alias="Page.Index", ge=1),
    Page_Size: Optional[int] = Query(None, alias="Page.Size", ge=1),
    db: Session = Depends(get_db),
):
    """
    Revenue leaderboard with rank, revenue share % bar, and order history.
 
    Filters (JSON):
      start_date : "YYYY-MM-DD"  (default: 1 year ago)
      end_date   : "YYYY-MM-DD"  (default: today)
      top_n      : int           (default: 10, max: 100)
 
    Example:
      /TopCustomersByRevenue?Filters={"start_date":"2024-01-01","top_n":25}
    """
    return repository.get_top_customers_by_revenue(
        db, *_paging(Page_Index, Page_Size, Filters)
    )