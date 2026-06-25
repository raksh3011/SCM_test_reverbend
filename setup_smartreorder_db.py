import sqlite3

con = sqlite3.connect("smartreorder.db")
cur = con.cursor()

# Remove current data
cur.execute("DELETE FROM product")
cur.execute("DELETE FROM supplier")

# Insert rainfall scenario data
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