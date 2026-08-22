import asyncio
from database import SessionLocal
from sqlalchemy import text
from app.payment.router import _auto_create_ekart_shipment

async def main():
    db = SessionLocal()
    db.execute(text("UPDATE twam.\"Orders\" SET \"State\" = 'Confirmed' WHERE \"OrderId\" = 209"))
    db.commit()
    print("Set Order 209 to Confirmed")
    await _auto_create_ekart_shipment(209)

if __name__ == '__main__':
    asyncio.run(main())
