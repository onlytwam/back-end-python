import asyncio
from datetime import datetime
from database import SessionLocal
from app.ekart.notifications import _get_order_details, _get_order_items, _cfg
from app.shared.email_template import twam_email, greeting, info_box, items_table, totals_block, section_divider
from app.sendemail.repository import send_email
from app.sendemail.schemas import EmailCreate

def send_previews():
    db = SessionLocal()
    order_id = 209
    order = _get_order_details(db, order_id)
    items = _get_order_items(db, order_id)
    company = _cfg(db, 'COMPANY_NAME', 'Only TWAM')
    support = _cfg(db, 'SUPPORT_EMAIL', 'customersupport@onlytwam.com')
    
    subtotal = sum(i['price'] * i['quantity'] for i in items)
    support_it = [{"name": i["name"], "quantity": i["quantity"], "price": i["price"], "size": i.get("size"), "color": i.get("color"), "stock_id": i.get("stock_id")} for i in items]

    # Order Confirmation Preview
    support_content = (
        greeting(order["customer_name"])
        + f'''<p style="margin:0 0 16px;">A new order has been confirmed.</p>'''
        + info_box([
            ("Customer Name",  order["customer_name"]),
            ("Customer Email", order["customer_email"]),
            ("Customer Phone", order.get("customer_phone", "?")),
            ("Order Number",   f"#{order['order_number']}"),
            ("Order Date",     datetime.now().strftime("%d %b %Y")),
            ("Status",         "Confirmed \u2714"),
        ], accent="#2E8B57")
        + items_table(support_it)
        + totals_block(subtotal, order.get("delivery_charge") or 0, order.get("total_amount") or 0, order.get("coupon_amount") or 0)
        + section_divider()
        + info_box([
            ("Ship To", order.get("customer_name", "")),
            ("Address", ", ".join(filter(None, [order.get("address_line"), order.get("city"), order.get("pincode")]))),
            ("Phone",   order.get("customer_phone", "")),
        ])
    )
    
    body1 = twam_email(title=f"[Order Confirmed] #{order['order_number']} - {order['customer_name']}", content=support_content)
    
    send_email(db, EmailCreate(
        name="Customer Support",
        email=support,
        subject=f"[Order Confirmed] #{order['order_number']} - {order['customer_name']}",
        message=body1
    ))
    print("Sent Order Confirmed preview to Support")
    
    # Ekart Shipment Preview
    support_shipped_content = (
        greeting("Customer Support Team")
        + f'''<p style="margin:0 0 16px;">A shipment has been created for the following order.</p>'''
        + info_box([
            ("Customer Name",  order["customer_name"]),
            ("Customer Email", order["customer_email"] or "?"),
            ("Customer Phone", order.get("customer_phone", "?")),
            ("Order Number",   f"#{order['order_number']}"),
            ("Tracking ID",    order["tracking_id"] or "Processing..."),
            ("Tracking URL",   "N/A"),
        ], accent="#2E8B57")
        + items_table(support_it)
    )
    
    body2 = twam_email(title=f"[Order Shipped] #{order['order_number']} - {order['customer_name']}", content=support_shipped_content)
    
    send_email(db, EmailCreate(
        name="Customer Support",
        email=support,
        subject=f"[Order Shipped] #{order['order_number']} - {order['customer_name']}",
        message=body2
    ))
    print("Sent Order Shipped preview to Support")

send_previews()
