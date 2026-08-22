from database import SessionLocal
from sqlalchemy import text

db = SessionLocal()
try:
    res = db.execute(text('SELECT "StoreId", "StoreName", "LoginEmail", "LoginPassword" FROM twam."Stores"')).fetchall()
    print("STORES:", res)
finally:
    db.close()
