from database import SessionLocal
from app.ekart.notifications import _get_order_details
db = SessionLocal()
print(_get_order_details(db, 209))
