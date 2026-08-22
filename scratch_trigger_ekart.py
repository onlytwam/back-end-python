import asyncio
import os
from database import SessionLocal
from app.payment.router import _auto_create_ekart_shipment

async def main():
    order_id = 217
    print(f"Manually triggering _auto_create_ekart_shipment for order {order_id}...")
    await _auto_create_ekart_shipment(order_id)
    print("Finished triggering shipment creation.")

if __name__ == "__main__":
    asyncio.run(main())
