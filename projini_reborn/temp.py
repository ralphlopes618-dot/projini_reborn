# import sqlite3

# conn = sqlite3.connect('leads.db')
# cursor = conn.cursor()

# # Execute query to fetch all first names
# cursor.execute("SELECT FirstName FROM Lead_Details")
# first_names = [row[0] for row in cursor.fetchall()]

# # Print the names
# print(f"Fetched {len(first_names)} first names:")
# print(first_names)

# conn.close()


import sqlite3

conn = sqlite3.connect('leads.db')
cursor = conn.cursor()

# Fetch first 5 rows
# cursor.execute("SELECT * FROM Lead_Details LIMIT 5")
# rows = cursor.fetchall()

# # Get column names
# column_names = [description[0] for description in cursor.description]

# print("Columns:")
# print(column_names)

# print("\nFirst 5 rows:")
# for row in rows:
#     print(dict(zip(column_names, row)))

cursor.execute("SELECT AssignedTo FROM Lead_Details")
rows = cursor.fetchall()

print(f"Fetched {len(rows)} assigned-to values:")
for row in rows:
    print(row[0])

conn.close()
