from database import SessionLocal
from sqlalchemy import text

db = SessionLocal()
order = db.execute(text('SELECT count(*) FROM twam."OrderItems" WHERE "OrderId" = 209')).scalar()
print(f"OrderId 209 has {order} items")
order = db.execute(text('SELECT count(*) FROM twam."OrderItems" WHERE "OrderId" = 217')).scalar()
print(f"OrderId 217 has {order} items")
