from database import SessionLocal
from sqlalchemy import text

db = SessionLocal()
try:
    # Add columns to twam.Stores
    db.execute(text('ALTER TABLE twam."Stores" ADD COLUMN IF NOT EXISTS "LoginEmail" VARCHAR(255);'))
    db.execute(text('ALTER TABLE twam."Stores" ADD COLUMN IF NOT EXISTS "LoginPassword" VARCHAR(255);'))
    
    # Add columns to twam.Warehouses
    db.execute(text('ALTER TABLE twam."Warehouses" ADD COLUMN IF NOT EXISTS "LoginEmail" VARCHAR(255);'))
    db.execute(text('ALTER TABLE twam."Warehouses" ADD COLUMN IF NOT EXISTS "LoginPassword" VARCHAR(255);'))
    
    db.commit()
    print("Columns added successfully!")
except Exception as e:
    db.rollback()
    print(f"Error: {e}")
finally:
    db.close()
