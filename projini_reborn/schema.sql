-- SQL Schema for Lead Details Database

-- Table: Properties
CREATE TABLE IF NOT EXISTS Properties (
    PropertyId INTEGER PRIMARY KEY AUTOINCREMENT,
    PropertyName TEXT NOT NULL UNIQUE,
    IsActive INTEGER NOT NULL CHECK (IsActive IN (0, 1)) DEFAULT 1,
    CreatedAt DATETIME DEFAULT CURRENT_TIMESTAMP,
    UpdatedAt DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- Table: Lead_Details
CREATE TABLE IF NOT EXISTS Lead_Details (
    LeadId INTEGER PRIMARY KEY AUTOINCREMENT,
    FirstName TEXT NOT NULL,
    LastName TEXT,
    MobileNo TEXT,
    EmailId TEXT,
    Address TEXT,
    Requirements TEXT,
    AssignedTo TEXT,
    Stage TEXT CHECK(Stage IN (
        'Fresh Lead',
        'Contact Attempted',
        'In Follow-up',
        'Interested',
        'Ready for Site Visit',
        'Site Visit Completed',
        'Proposal Presented',
        'In Negotiation',
        'Booking Confirmed',
        'Booking Complete – Won',
        'Future Prospect',
        'Not Interested',
        'Invalid / Not Qualified',
        'Closed – Lost'
    )) DEFAULT 'Fresh Lead',
    Source TEXT,
    Priority TEXT CHECK(Priority IN ('Hot', 'Warm', 'Cold')) DEFAULT 'Warm',
    AssignedProperty TEXT,
    ActivityType TEXT CHECK(ActivityType IN ('Call', 'Meeting', 'Task', 'Site Visit')),
    ActivityStatus TEXT CHECK(ActivityStatus IN ('Pending', 'Completed', 'Cancelled')),
    ActivityNote TEXT,
    CreatedAt DATETIME DEFAULT CURRENT_TIMESTAMP,
    UpdatedAt DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (AssignedProperty) REFERENCES Properties(PropertyName)
);

-- Indexes for efficient lookups
CREATE INDEX IF NOT EXISTS idx_lead_details_mobile ON Lead_Details(MobileNo);
CREATE INDEX IF NOT EXISTS idx_lead_details_email ON Lead_Details(EmailId);
CREATE INDEX IF NOT EXISTS idx_lead_details_assigned ON Lead_Details(AssignedTo);
CREATE INDEX IF NOT EXISTS idx_lead_details_stage ON Lead_Details(Stage);
CREATE INDEX IF NOT EXISTS idx_lead_details_priority ON Lead_Details(Priority);
CREATE INDEX IF NOT EXISTS idx_lead_details_assigned_property ON Lead_Details(AssignedProperty);
CREATE INDEX IF NOT EXISTS idx_lead_details_activity_type ON Lead_Details(ActivityType);
CREATE INDEX IF NOT EXISTS idx_lead_details_activity_status ON Lead_Details(ActivityStatus);
