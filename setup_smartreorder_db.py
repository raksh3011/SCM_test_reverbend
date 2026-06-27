import sqlite3

con = sqlite3.connect("smartreorder.db")
cur = con.cursor()

cur.executescript("""
CREATE TABLE IF NOT EXISTS product (
    product_id TEXT PRIMARY KEY,
    product_name TEXT,
    avg_daily_sales REAL,
    on_hand_qty INTEGER,
    safety_stock INTEGER
);

CREATE TABLE IF NOT EXISTS supplier (
    supplier_id TEXT PRIMARY KEY,
    supplier_name TEXT,
    unit_price REAL,
    lead_time_days INTEGER,
    reliability REAL,
    shipping_mode TEXT
);
""")

cur.execute("DELETE FROM product")
cur.execute("DELETE FROM supplier")

cur.executemany("INSERT INTO product VALUES (?,?,?,?,?)", [
    ("P1", "Bottled Water", 160, 250, 80),
    ("P2", "Cola",          700, 250, 100),
    ("P3", "Sports Drink",  140, 180, 50),
])

cur.executemany("INSERT INTO supplier VALUES (?,?,?,?,?,?)", [
    ("S10", "FastBev",  1.15, 2,  0.98, "express"),
    ("S20", "ValueBev", 0.95, 6,  0.91, "standard"),
    ("S30", "BulkBev",  0.85, 11, 0.87, "freight"),
])

con.commit()
con.close()

print("Database updated with Rainfall/Flooding scenario.")
