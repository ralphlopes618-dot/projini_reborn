import sqlite3
import os
import random
from datetime import datetime, timedelta

DB_NAME = 'leads.db'
SCHEMA_FILE = 'schema.sql'

def generate_sample_leads(count=100):
    """Programmatically generates a list of realistic sample leads."""
    random.seed(42)  # For reproducible sample data
    
    first_names = ['Ralph', 'Jane', 'Amit', 'Priya', 'John', 'Raj', 'Vikram', 'Neha', 'Sanjay', 'Sunita', 'Rahul', 'Anita', 'Karan', 'Simran', 'Rohan']
    last_names = ['Lopes', 'Smith', 'Patel', 'Sharma', 'Doe', 'Kumar', 'Singh', 'Mehta', 'Joshi', 'Gupta']
    
    cities = ['Beyond Mira Road', 'Mira Road', 'Andheri West', 'Thane West', 'Kandivali East']
    townships = ['Viva Vrindavan Township', 'Sai Sadan Oasis', 'Golden Heights', 'Green Meadows', 'Royal Residency']
    localities = ['PK Nagar', 'Sector 4', 'Andheri Link Road', 'Ghodbunder Road', 'Thakur Village']
    
    assignees = ['Ralph Lopes', 'Sarah Jenkins', 'David Miller', 'Emma Watson']
    
    stages = [
        'Fresh Lead', 'Contact Attempted', 'In Follow-up', 'Interested',
        'Ready for Site Visit', 'Site Visit Completed', 'Proposal Presented',
        'In Negotiation', 'Booking Confirmed', 'Booking Complete – Won',
        'Future Prospect', 'Not Interested', 'Invalid / Not Qualified', 'Closed – Lost'
    ]
    
    sources = ['99 Acres', 'MagicBricks', 'Google Ads', 'Referral', 'Housing.com', 'Facebook Ads', 'Direct Walk-in']
    priorities = ['Hot', 'Warm', 'Cold']
    
    # Active properties for lead assignment
    active_properties = [
        "Viva Vrindavan Township (Residential – 2 BHK)",
        "Lodha Crown (Residential – 1 & 2 BHK)",
        "JP North Barcelona (Residential – 2 & 3 BHK)",
        "Shree Ostwal Paradise (Residential – 1 & 2 BHK)",
        "Delta Greenville (Residential – 2 & 3 BHK)",
        "Rustomjee Urbania (Residential – Premium Apartments)"
    ]
    
    activity_types = ['Call', 'Meeting', 'Task', 'Site Visit']
    activity_statuses = ['Pending', 'Completed', 'Cancelled']
    meeting_responses = [
        'Interested',
        'Not Interested',
        'Call Back Later',
        'Follow-up Required',
        'Site Visit Scheduled',
        'Proposal Requested',
        'Price Negotiation',
        'No Response',
        'Invalid / Wrong Number',
        'Customer Cancelled'
    ]
    activity_notes = [
        "Followed up to check interest. Requested callback next week.",
        "Detailed pricing breakdown discussed. Prefers 2 BHK config.",
        "Site visit scheduled. Customer is coming with family.",
        "Conducted site visit. Client loved the balcony view, negotiating pricing.",
        "Pending initial call response. Sent WhatsApp follow-up.",
        "Meeting completed. Looking for a bank loan options.",
        "Cancelled the meeting due to personal emergency, requested reschedule.",
        "Shared project brochures and floor plans via email.",
        "Negotiation on closing fee ongoing. Very hot lead."
    ]
    
    leads = []
    for i in range(1, count + 1):
        first = first_names[i % len(first_names)]
        last = last_names[i % len(last_names)]
        mobile = f"+9198765{str(i).zfill(5)}"
        email = f"{first.lower()}.{last.lower()}{i}@example.com"
        
        city = cities[i % len(cities)]
        township = townships[i % len(townships)]
        locality = localities[i % len(localities)]
        
        address = f"Flat {100 + i}, Tower {i%5 + 1}, {township}, {locality}, {city}"
        
        beds = (i % 3) + 1  # 1, 2, or 3 beds
        price_val = 3000000 + (i * 150000)  # price scales up
        price_lac = price_val // 100000
        
        requirements = f"Rs{price_lac} Lac, {beds} Bed, Flat/Apartment For Sale in {township}, {locality}, {city}. City: {city}. Price: {price_val}"
        
        assignee = assignees[i % len(assignees)]
        
        # Distribute stages and priorities realisticially
        if i % 10 == 0:
            stage = 'Booking Complete – Won'
            priority = 'Hot'
        elif i % 7 == 0:
            stage = 'Closed – Lost'
            priority = 'Cold'
        else:
            stage = stages[i % len(stages)]
            priority = priorities[i % len(priorities)]
            
        source = sources[i % len(sources)]
        
        # Generate activity details (leave some leads unassigned for realism)
        if i % 3 == 0:
            activity_type = None
            activity_status = None
            activity_note = None
            meeting_response = None
            activity_datetime = None
            feedback = None
        else:
            activity_type = activity_types[i % len(activity_types)]
            activity_status = random.choice(activity_statuses)
            activity_note = random.choice(activity_notes)
            meeting_response = None
            activity_datetime = None
            if activity_status == 'Pending':
                activity_datetime = (
                    datetime.now() + timedelta(days=(i % 14) + 1, hours=i % 8)
                ).strftime("%Y-%m-%d %H:%M:%S")
            else:
                meeting_response = meeting_responses[i % len(meeting_responses)]
            feedback = activity_note
            
        leads.append((
            first, last, mobile, email, address, requirements, assignee, stage,
            source, priority, None, activity_type, activity_status,
            activity_note, meeting_response, activity_datetime, feedback
        ))
        
    return leads

def init_db():
    # Remove existing database file for a clean setup
    if os.path.exists(DB_NAME):
        print(f"Removing existing database '{DB_NAME}' for clean setup...")
        try:
            os.remove(DB_NAME)
        except PermissionError:
            print("Warning: Database file is locked. Using existing database.")

    print(f"Connecting to database '{DB_NAME}'...")
    conn = sqlite3.connect(DB_NAME)
    # Enable foreign keys
    conn.execute("PRAGMA foreign_keys = ON;")
    cursor = conn.cursor()

    print(f"Reading schema from '{SCHEMA_FILE}'...")
    with open(SCHEMA_FILE, 'r', encoding='utf-8') as f:
        schema_sql = f.read()

    print("Executing schema...")
    cursor.executescript(schema_sql)
    conn.commit()
    print("Schema executed successfully.")

    # Populate active/inactive properties
    active_properties = [
        "Viva Vrindavan Township (Residential – 2 BHK)",
        "Lodha Crown (Residential – 1 & 2 BHK)",
        "JP North Barcelona (Residential – 2 & 3 BHK)",
        "Shree Ostwal Paradise (Residential – 1 & 2 BHK)",
        "Delta Greenville (Residential – 2 & 3 BHK)",
        "Rustomjee Urbania (Residential – Premium Apartments)"
    ]
    inactive_properties = [
        "Inactive Heights (Residential – 1 BHK)",
        "Legacy Estates (Residential – Studio)"
    ]

    print("Inserting property list...")
    for prop in active_properties:
        cursor.execute("INSERT OR IGNORE INTO Properties (PropertyName, IsActive) VALUES (?, 1)", (prop,))
        cursor.execute("INSERT OR IGNORE INTO AvailableProperties (PropertyName, IsActive) VALUES (?, 1)", (prop,))
    for prop in inactive_properties:
        cursor.execute("INSERT OR IGNORE INTO Properties (PropertyName, IsActive) VALUES (?, 0)", (prop,))
        cursor.execute("INSERT OR IGNORE INTO AvailableProperties (PropertyName, IsActive) VALUES (?, 0)", (prop,))
    conn.commit()
    print("Seeded properties list.")

    # Generate 100 sample leads
    sample_leads = generate_sample_leads(100)
    
    print(f"Inserting {len(sample_leads)} sample lead details...")
    cursor.executemany("""
        INSERT INTO Lead_Details (
            FirstName, LastName, MobileNo, EmailId, Address, Requirements,
            AssignedTo, Stage, Source, Priority, AssignedProperties, ActivityType,
            ActivityStatus, ActivityNote, MeetingResponse, ActivityDateTime, Feedback
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, sample_leads)
    conn.commit()
    print(f"Inserted {len(sample_leads)} sample leads with assigned properties and activities.")

    # Fetch and display database schema info
    print("\n--- Verifying Lead_Details Table Structure ---")
    cursor.execute("PRAGMA table_info(Lead_Details)")
    columns = cursor.fetchall()
    for col in columns:
        print(f"Column: {col[1]} | Type: {col[2]} | NotNull: {col[3]} | Default: {col[4]} | PK: {col[5]}")

    print("\n--- Verifying Properties Table Structure ---")
    cursor.execute("PRAGMA table_info(Properties)")
    columns = cursor.fetchall()
    for col in columns:
        print(f"Column: {col[1]} | Type: {col[2]} | NotNull: {col[3]} | Default: {col[4]} | PK: {col[5]}")

    print("\n--- Verifying Indexes ---")
    cursor.execute("PRAGMA index_list(Lead_Details)")
    indexes = cursor.fetchall()
    for idx in indexes:
        print(f"Index Name: {idx[1]} | Unique: {idx[2]}")

    # Display count of rows
    cursor.execute("SELECT COUNT(*) FROM Lead_Details")
    total_leads = cursor.fetchone()[0]
    print(f"\nTotal Lead Records in Database: {total_leads}")

    cursor.execute("SELECT COUNT(*) FROM Properties")
    total_props = cursor.fetchone()[0]
    print(f"Total Property Records in Database: {total_props}")

    conn.close()

if __name__ == "__main__":
    init_db()
