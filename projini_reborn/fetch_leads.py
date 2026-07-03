import sqlite3

DB_NAME = 'leads.db'

def get_connection():
    conn = sqlite3.connect(DB_NAME)
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn

def fetch_all_leads():
    print("\n=== Query 1: All Leads (With Stage, Source, Priority, Property, and Activities) ===")
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT LeadId, FirstName, LastName, MobileNo, EmailId, AssignedTo, Stage,
               Source, Priority, AssignedProperties, ActivityType, ActivityStatus,
               ActivityNote, MeetingResponse, ActivityDateTime, Feedback
        FROM Lead_Details
    """)
    rows = cursor.fetchall()
    for row in rows:
        print(f"ID: {row[0]} | Name: {row[1]} {row[2]} | Phone: {row[3]} | Assigned: {row[5]} | Stage: {row[6]} | Source: {row[7]} | Priority: {row[8]} | Property: {row[9]} | Activity: {row[10]} ({row[11]}) - Note: {row[12]} | Response: {row[13]} | DateTime: {row[14]} | Feedback: {row[15]}")
    conn.close()

def fetch_hot_leads():
    print("\n=== Query 2: Hot Priority Leads ===")
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT LeadId, FirstName, LastName, Stage, AssignedTo 
        FROM Lead_Details 
        WHERE Priority = 'Hot'
    """)
    rows = cursor.fetchall()
    for row in rows:
        print(f"ID: {row[0]} | Name: {row[1]} {row[2]} | Stage: {row[3]} | Assigned: {row[4]}")
    conn.close()

def get_leads_count_by_stage():
    print("\n=== Query 3: Lead Pipeline - Counts by Stage ===")
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT Stage, COUNT(*) as LeadCount 
        FROM Lead_Details 
        GROUP BY Stage
        ORDER BY LeadCount DESC
    """)
    rows = cursor.fetchall()
    for row in rows:
        print(f"Stage: {row[0]:<25} | Lead Count: {row[1]}")
    conn.close()

def fetch_leads_by_source(source_name):
    print(f"\n=== Query 4: Leads from Source: '{source_name}' ===")
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT LeadId, FirstName, LastName, Stage, Priority 
        FROM Lead_Details 
        WHERE Source = ?
    """, (source_name,))
    rows = cursor.fetchall()
    for row in rows:
        print(f"ID: {row[0]} | Name: {row[1]} {row[2]} | Stage: {row[3]} | Priority: {row[4]}")
    conn.close()

def fetch_active_properties():
    print("\n=== Query 5: Active Properties/Projects Available in the System ===")
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT PropertyId, PropertyName 
        FROM AvailableProperties
        WHERE IsActive = 1
    """)
    rows = cursor.fetchall()
    for row in rows:
        print(f"Property ID: {row[0]} | Property Name: {row[1]}")
    conn.close()

def fetch_leads_by_property(property_name):
    print(f"\n=== Query 6: Leads Assigned to Property: '{property_name}' ===")
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT LeadId, FirstName, LastName, Stage, Priority
        FROM Lead_Details
        WHERE AssignedProperties LIKE ?
    """, (f"%{property_name}%",))
    rows = cursor.fetchall()
    for row in rows:
        print(f"ID: {row[0]} | Name: {row[1]} {row[2]} | Stage: {row[3]} | Priority: {row[4]}")
    conn.close()

def fetch_leads_by_activity_status(activity_status):
    print(f"\n=== Query 7: Leads with Activity Status: '{activity_status}' ===")
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT LeadId, FirstName, LastName, ActivityType, ActivityNote 
        FROM Lead_Details 
        WHERE ActivityStatus = ?
    """, (activity_status,))
    rows = cursor.fetchall()
    for row in rows:
        print(f"ID: {row[0]} | Name: {row[1]} {row[2]} | Activity: {row[3]} | Note: {row[4]}")
    conn.close()

if __name__ == "__main__":
    print("Executing SQL queries on Lead_Details SQLite table...")
    fetch_all_leads()
    fetch_hot_leads()
    get_leads_count_by_stage()
    fetch_leads_by_source("99 Acres")
    fetch_active_properties()
    fetch_leads_by_property("Viva Vrindavan Township (Residential – 2 BHK)")
    fetch_leads_by_activity_status("Pending")
    fetch_leads_by_activity_status("Completed")
