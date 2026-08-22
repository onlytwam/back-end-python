import asyncio
from database import SessionLocal
from app.ekart.notifications import _get_order_items

db = SessionLocal()
print(_get_order_items(db, 209))
