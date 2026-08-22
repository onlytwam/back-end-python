import sys
import os

# Ensure the app module can be found
sys.path.insert(0, r"C:\Users\Admin\.gemini\antigravity\scratch\TwamProject\Twam-live-BE")

from database import SessionLocal
from app.ekart.notifications import _get_order_details, _get_order_items
from app.shared.email_template import twam_email, info_box, items_table, greeting, totals_block, cta_button
from app.sendemail.repository import send_email
from app.sendemail.schemas import EmailCreate

from sqlalchemy import text
def test_emails():
    db = SessionLocal()
    try:
        # Get the latest order
        order_id = db.execute(text("SELECT MAX(\"OrderId\") FROM twam.\"Orders\"")).scalar()
        print(f"Testing with Order ID: {order_id}")
        
        order = _get_order_details(db, order_id)
        items = _get_order_items(db, order_id)
        
        support_email = "customersupport@onlytwam.com"
        
        # 1. ORDER PLACED NOTIFICATION (Support Copy)
        support_it = [{"name": i["name"], "quantity": i["quantity"], "price": i["price"],
               "size": i.get("size"), "color": i.get("color"), "stock_id": i.get("stock_id")} for i in items]
        subtotal = sum(i["price"] * i["quantity"] for i in items)
        
        support_content = (
            greeting("Customer Support Team")
            + f'''<p style="margin:0 0 16px;">An order has been placed by {order['customer_name']}.</p>'''
            + info_box([
                ("Customer Name",  order["customer_name"]),
                ("Customer Email", order["customer_email"] or "—"),
                ("Customer Phone", order.get("customer_phone", "—")),
                ("Order Number",   f"#{order['order_number']}"),
                ("Order Date",     "Test Date"),
                ("Status",         "Confirmed ✓"),
            ], accent="#2E8B57")
            + items_table(support_it)
            + totals_block(subtotal,
                           order.get("delivery_charge") or 0,
                           order.get("total_amount") or 0,
                           order.get("coupon_discount") or 0)
        )
        support_body = twam_email(
            title   = f"[TEST Order Confirmed] #{order['order_number']} - {order['customer_name']}",
            content = support_content,
        )
        send_email(db, EmailCreate(
            name="Customer Support",
            email=support_email,
            subject=f"[TEST Order Confirmed] #{order['order_number']} - {order['customer_name']} ({order['customer_email']})",
            message=support_body,
        ))
        print("✅ Sent test Order Confirmed email to support")
        
        # 2. ORDER SHIPPED NOTIFICATION (Support Copy)
        tracking_url = "http://test-tracking-url.com"
        support_shipped_content = (
            greeting("Customer Support Team")
            + f'''<p style="margin:0 0 16px;">A shipment has been created for the following order.</p>'''
            + info_box([
                ("Customer Name",  order["customer_name"]),
                ("Customer Email", order["customer_email"] or "—"),
                ("Customer Phone", order.get("customer_phone", "—")),
                ("Order Number",   f"#{order['order_number']}"),
                ("Tracking ID",    order["tracking_id"] or "Processing…"),
                ("Tracking URL",   tracking_url or "—"),
            ], accent="#2E8B57")
            + items_table(support_it)
            + cta_button("Track Package", tracking_url)
        )
        support_shipped_body = twam_email(
            title   = f"[TEST Order Shipped] #{order['order_number']} — {order['customer_name']}",
            content = support_shipped_content,
        )
        send_email(db, EmailCreate(
            name="Customer Support",
            email=support_email,
            subject=f"[TEST Order Shipped] #{order['order_number']} — {order['customer_name']} ({order['customer_email'] or 'no email'}) — AWB {order['tracking_id'] or 'Processing'}",
            message=support_shipped_body,
        ))
        print("✅ Sent test Order Shipped email to support")

    except Exception as e:
        print(f"Error: {e}")
    finally:
        db.close()

if __name__ == "__main__":
    test_emails()
