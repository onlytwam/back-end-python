from database import SessionLocal
from sqlalchemy import text
db = SessionLocal()
res = db.execute(text("SELECT table_name FROM information_schema.tables WHERE table_schema = 'billing';")).fetchall()
print(res)
