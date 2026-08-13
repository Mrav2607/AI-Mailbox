import os, csv
from sqlalchemy import create_engine, text

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+psycopg://user:pass@localhost:5432/cortexmail")
engine = create_engine(DATABASE_URL)

with engine.connect() as conn, open("receipts_export.csv", "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["message_id", "merchant", "currency", "total", "purchased_at"])
    for row in conn.execute(text("SELECT message_id, merchant, currency, total, purchased_at FROM receipt ORDER BY purchased_at DESC")):
        w.writerow(row)
print("Wrote receipts_export.csv")
