from flask import Flask, request, jsonify, session, send_from_directory, send_file
from flask_cors import CORS
import psycopg2
import psycopg2.extras
import hashlib
import os
import uuid
import json
from datetime import datetime, date
from werkzeug.utils import secure_filename
import requests

app = Flask(__name__, static_folder='static')
app.secret_key = os.environ.get('SECRET_KEY', 'rollcall-dev-key')
CORS(app, supports_credentials=True)

DATABASE_URL = os.environ.get('DATABASE_URL', '')
# Railway uses postgres:// but psycopg2 requires postgresql://
if DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)
UPLOAD_FOLDER = 'uploads'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# ─────────────────────────────────────────────
#  DATABASE
# ─────────────────────────────────────────────

def get_db():
    conn = psycopg2.connect(
        DATABASE_URL,
        cursor_factory=psycopg2.extras.RealDictCursor,
        options="-c timezone=America/New_York"
    )
    return conn

def init_db():
    conn = psycopg2.connect(
        DATABASE_URL,
        options="-c timezone=America/New_York"
    )
    c = conn.cursor()

    c.execute('''CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, email TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'board')''')

    c.execute('''CREATE TABLE IF NOT EXISTS interest_types (
        id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL, color TEXT DEFAULT 'gray',
        created_at TIMESTAMP DEFAULT NOW())''')

    c.execute('''CREATE TABLE IF NOT EXISTS volunteers (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, email TEXT NOT NULL,
        phone TEXT, birthday TEXT, status TEXT NOT NULL DEFAULT 'active',
        interests TEXT DEFAULT '[]', created_at TIMESTAMP DEFAULT NOW())''')

    c.execute('''CREATE TABLE IF NOT EXISTS events (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, event_date TEXT,
        description TEXT, created_at TIMESTAMP DEFAULT NOW())''')

    c.execute('''CREATE TABLE IF NOT EXISTS hours (
        id TEXT PRIMARY KEY, volunteer_id TEXT NOT NULL REFERENCES volunteers(id) ON DELETE CASCADE,
        event TEXT NOT NULL, event_id TEXT, date TEXT NOT NULL, hours REAL NOT NULL,
        role TEXT, notes TEXT, created_at TIMESTAMP DEFAULT NOW())''')

    c.execute('''CREATE TABLE IF NOT EXISTS notes (
        id TEXT PRIMARY KEY, volunteer_id TEXT NOT NULL REFERENCES volunteers(id) ON DELETE CASCADE,
        author TEXT NOT NULL, content TEXT NOT NULL, created_at TIMESTAMP DEFAULT NOW())''')

    c.execute('''CREATE TABLE IF NOT EXISTS volunteer_history (
        id TEXT PRIMARY KEY, volunteer_id TEXT NOT NULL REFERENCES volunteers(id) ON DELETE CASCADE,
        event TEXT NOT NULL, role TEXT NOT NULL, date TEXT NOT NULL,
        notes TEXT, created_at TIMESTAMP DEFAULT NOW())''')

    c.execute('''CREATE TABLE IF NOT EXISTS volunteer_files (
        id TEXT PRIMARY KEY, volunteer_id TEXT NOT NULL REFERENCES volunteers(id) ON DELETE CASCADE,
        name TEXT NOT NULL, size TEXT, type TEXT, date TEXT, created_at TIMESTAMP DEFAULT NOW())''')

    c.execute('''CREATE TABLE IF NOT EXISTS waiver_types (
        id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL, description TEXT,
        template_body TEXT, created_at TIMESTAMP DEFAULT NOW())''')

    c.execute('''CREATE TABLE IF NOT EXISTS volunteer_waivers (
        id TEXT PRIMARY KEY, volunteer_id TEXT NOT NULL REFERENCES volunteers(id) ON DELETE CASCADE,
        waiver_type_id TEXT NOT NULL REFERENCES waiver_types(id) ON DELETE CASCADE,
        signed_date TEXT NOT NULL, expiry_date TEXT, filename TEXT, original_name TEXT,
        file_size TEXT, signed_name TEXT, signed_via TEXT DEFAULT 'upload',
        uploaded_by TEXT, created_at TIMESTAMP DEFAULT NOW())''')

    c.execute('''CREATE TABLE IF NOT EXISTS email_templates (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, subject TEXT NOT NULL,
        body TEXT NOT NULL, created_at TIMESTAMP DEFAULT NOW())''')

    c.execute('''CREATE TABLE IF NOT EXISTS youth_participants (
        id TEXT PRIMARY KEY, first_name TEXT NOT NULL, last_name TEXT NOT NULL,
        dob TEXT, program TEXT, status TEXT NOT NULL DEFAULT 'active',
        medical_notes TEXT, allergies TEXT, photo_consent INTEGER DEFAULT 0,
        medical_consent INTEGER DEFAULT 0, created_at TIMESTAMP DEFAULT NOW())''')

    c.execute('''CREATE TABLE IF NOT EXISTS youth_guardians (
        id TEXT PRIMARY KEY, youth_id TEXT NOT NULL REFERENCES youth_participants(id) ON DELETE CASCADE,
        name TEXT NOT NULL, relationship TEXT NOT NULL, phone TEXT, email TEXT,
        is_primary INTEGER DEFAULT 0, created_at TIMESTAMP DEFAULT NOW())''')

    c.execute('''CREATE TABLE IF NOT EXISTS youth_emergency_contacts (
        id TEXT PRIMARY KEY, youth_id TEXT NOT NULL REFERENCES youth_participants(id) ON DELETE CASCADE,
        name TEXT NOT NULL, relationship TEXT, phone TEXT NOT NULL, created_at TIMESTAMP DEFAULT NOW())''')

    c.execute('''CREATE TABLE IF NOT EXISTS youth_waivers (
        id TEXT PRIMARY KEY, youth_id TEXT NOT NULL REFERENCES youth_participants(id) ON DELETE CASCADE,
        waiver_type_id TEXT NOT NULL REFERENCES waiver_types(id) ON DELETE CASCADE,
        signed_date TEXT NOT NULL, expiry_date TEXT, signed_by TEXT,
        filename TEXT, original_name TEXT, created_at TIMESTAMP DEFAULT NOW())''')

    # youth_programs table
    c.execute('''CREATE TABLE IF NOT EXISTS youth_programs (
        id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL,
        description TEXT, created_at TIMESTAMP DEFAULT NOW())''')

    # youth program enrollments (many-to-many)
    c.execute('''CREATE TABLE IF NOT EXISTS youth_program_enrollments (
        id TEXT PRIMARY KEY,
        youth_id TEXT NOT NULL REFERENCES youth_participants(id) ON DELETE CASCADE,
        program_id TEXT NOT NULL REFERENCES youth_programs(id) ON DELETE CASCADE,
        enrolled_date TEXT,
        notes TEXT,
        created_at TIMESTAMP DEFAULT NOW(),
        UNIQUE(youth_id, program_id))''')

    # productions
    c.execute('''CREATE TABLE IF NOT EXISTS productions (
        id TEXT PRIMARY KEY, name TEXT NOT NULL,
        production_type TEXT DEFAULT 'show',
        start_date TEXT, end_date TEXT,
        description TEXT, status TEXT DEFAULT 'upcoming',
        created_at TIMESTAMP DEFAULT NOW())''')

    # production members (volunteers in a production)
    c.execute('''CREATE TABLE IF NOT EXISTS production_members (
        id TEXT PRIMARY KEY,
        production_id TEXT NOT NULL REFERENCES productions(id) ON DELETE CASCADE,
        volunteer_id TEXT NOT NULL REFERENCES volunteers(id) ON DELETE CASCADE,
        role TEXT NOT NULL,
        department TEXT,
        status TEXT DEFAULT 'confirmed',
        notes TEXT,
        created_at TIMESTAMP DEFAULT NOW(),
        UNIQUE(production_id, volunteer_id))''')

    # add active column to users
    conn.commit()

    # pending hours (kiosk submissions awaiting approval)
    c.execute("""CREATE TABLE IF NOT EXISTS pending_hours (
        id TEXT PRIMARY KEY,
        volunteer_id TEXT NOT NULL REFERENCES volunteers(id) ON DELETE CASCADE,
        event TEXT NOT NULL,
        event_id TEXT,
        date TEXT NOT NULL,
        hours REAL NOT NULL,
        role TEXT,
        notes TEXT,
        submitted_at TIMESTAMP DEFAULT NOW(),
        status TEXT DEFAULT 'pending')""")

    # event required waivers
    c.execute("""CREATE TABLE IF NOT EXISTS event_waivers (
        id TEXT PRIMARY KEY,
        event_id TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
        waiver_type_id TEXT NOT NULL REFERENCES waiver_types(id) ON DELETE CASCADE,
        UNIQUE(event_id, waiver_type_id))""")

    # production required waivers
    c.execute("""CREATE TABLE IF NOT EXISTS production_waivers (
        id TEXT PRIMARY KEY,
        production_id TEXT NOT NULL REFERENCES productions(id) ON DELETE CASCADE,
        waiver_type_id TEXT NOT NULL REFERENCES waiver_types(id) ON DELETE CASCADE,
        UNIQUE(production_id, waiver_type_id))""")

    # volunteer emergency contacts
    c.execute("""CREATE TABLE IF NOT EXISTS volunteer_emergency_contacts (
        id TEXT PRIMARY KEY,
        volunteer_id TEXT NOT NULL REFERENCES volunteers(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        relationship TEXT,
        phone TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT NOW())""")

    # opening checklist template (separate from closing)
    c.execute("""CREATE TABLE IF NOT EXISTS opening_checklist_items (
        id TEXT PRIMARY KEY,
        label TEXT NOT NULL,
        item_type TEXT NOT NULL DEFAULT 'checkbox',
        required BOOLEAN DEFAULT TRUE,
        sort_order INTEGER DEFAULT 0,
        hint TEXT,
        created_at TIMESTAMP DEFAULT NOW())""")

    # seed default opening checklist items (only if none exist)
    c.execute("SELECT COUNT(*) as cnt FROM opening_checklist_items")
    if c.fetchone()[0] == 0:
        opening_items = [
            (str(__import__('uuid').uuid4()), 'Space is clean and ready', 'checkbox', True, 1, ''),
            (str(__import__('uuid').uuid4()), 'All equipment/props in place', 'checkbox', True, 2, ''),
            (str(__import__('uuid').uuid4()), 'Lights and sound checked', 'checkbox', True, 3, ''),
            (str(__import__('uuid').uuid4()), 'Bathrooms stocked and clean', 'checkbox', True, 4, ''),
            (str(__import__('uuid').uuid4()), 'Emergency exits clear', 'checkbox', True, 5, ''),
            (str(__import__('uuid').uuid4()), 'Headcount / expected attendance', 'text', False, 6, 'How many people are expected tonight?'),
            (str(__import__('uuid').uuid4()), 'Opening notes', 'text', False, 7, 'Anything staff should know before the event starts'),
        ]
        for item in opening_items:
            c.execute("INSERT INTO opening_checklist_items (id,label,item_type,required,sort_order,hint) VALUES (%s,%s,%s,%s,%s,%s)", item)

    # youth authorized pickups
    c.execute("""CREATE TABLE IF NOT EXISTS youth_authorized_pickups (
        id TEXT PRIMARY KEY,
        youth_id TEXT NOT NULL REFERENCES youth_participants(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        relationship TEXT,
        phone TEXT,
        priority INTEGER DEFAULT 0,
        notes TEXT,
        created_at TIMESTAMP DEFAULT NOW())""")

    # production attendance (kiosk sign-in/out for cast & crew)
    c.execute("""CREATE TABLE IF NOT EXISTS prod_attendance (
        id TEXT PRIMARY KEY,
        volunteer_id TEXT NOT NULL REFERENCES volunteers(id) ON DELETE CASCADE,
        event_id TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
        signed_in_at TIMESTAMP DEFAULT NOW(),
        signed_out_at TIMESTAMP,
        created_at TIMESTAMP DEFAULT NOW())""")

    # youth production members (kids cast in Rising Stars productions)
    c.execute("""CREATE TABLE IF NOT EXISTS youth_production_members (
        id TEXT PRIMARY KEY,
        production_id TEXT NOT NULL REFERENCES productions(id) ON DELETE CASCADE,
        youth_id TEXT NOT NULL REFERENCES youth_participants(id) ON DELETE CASCADE,
        role TEXT,
        created_at TIMESTAMP DEFAULT NOW(),
        UNIQUE(production_id, youth_id))""")

    # youth parent sign-in/out
    c.execute("""CREATE TABLE IF NOT EXISTS youth_sign_ins (
        id TEXT PRIMARY KEY,
        youth_id TEXT NOT NULL REFERENCES youth_participants(id) ON DELETE CASCADE,
        event_id TEXT REFERENCES events(id),
        program_id TEXT REFERENCES youth_programs(id),
        signed_in_at TIMESTAMP,
        signed_in_by TEXT,
        signed_out_at TIMESTAMP,
        signed_out_by TEXT,
        notes TEXT,
        created_at TIMESTAMP DEFAULT NOW())""")

    # summer camps (programs with dates)
    # already covered by youth_programs — just add date columns via migration

    # event types (customizable)
    c.execute("""CREATE TABLE IF NOT EXISTS event_types (
        id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL,
        color TEXT DEFAULT 'blue',
        created_at TIMESTAMP DEFAULT NOW())""")

    # seed default event types (name has UNIQUE constraint so ON CONFLICT works correctly)
    for et in [
        ('Rehearsal', 'amber'), ('Performance', 'teal'), ('Meeting', 'blue'),
        ('Build Day', 'pink'), ('Strike', 'purple'), ('Other', 'gray'),
    ]:
        c.execute("INSERT INTO event_types (id,name,color) VALUES (%s,%s,%s) ON CONFLICT (name) DO NOTHING",
                  (str(__import__('uuid').uuid4()), et[0], et[1]))

    # ELICs (approved event leads)
    c.execute("""CREATE TABLE IF NOT EXISTS elics (
        id TEXT PRIMARY KEY,
        volunteer_id TEXT NOT NULL REFERENCES volunteers(id) ON DELETE CASCADE,
        pin TEXT NOT NULL,
        is_master BOOLEAN DEFAULT FALSE,
        active BOOLEAN DEFAULT TRUE,
        notes TEXT,
        created_at TIMESTAMP DEFAULT NOW(),
        UNIQUE(volunteer_id))""")

    # event ELIC assignment
    c.execute("""CREATE TABLE IF NOT EXISTS event_elics (
        id TEXT PRIMARY KEY,
        event_id TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
        elic_id TEXT NOT NULL REFERENCES elics(id) ON DELETE CASCADE,
        UNIQUE(event_id, elic_id))""")

    # checklist template items
    c.execute("""CREATE TABLE IF NOT EXISTS checklist_items (
        id TEXT PRIMARY KEY,
        label TEXT NOT NULL,
        item_type TEXT NOT NULL DEFAULT 'checkbox',
        required BOOLEAN DEFAULT TRUE,
        sort_order INTEGER DEFAULT 0,
        hint TEXT,
        created_at TIMESTAMP DEFAULT NOW())""")

    # event open/close log
    c.execute("""CREATE TABLE IF NOT EXISTS event_logs (
        id TEXT PRIMARY KEY,
        event_id TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
        elic_id TEXT NOT NULL REFERENCES elics(id),
        action TEXT NOT NULL,
        timestamp TIMESTAMP DEFAULT NOW(),
        notes TEXT)""")

    # closing checklist responses
    c.execute("""CREATE TABLE IF NOT EXISTS event_checklist_responses (
        id TEXT PRIMARY KEY,
        event_log_id TEXT NOT NULL REFERENCES event_logs(id) ON DELETE CASCADE,
        checklist_item_id TEXT,
        label TEXT NOT NULL,
        item_type TEXT NOT NULL,
        response TEXT,
        created_at TIMESTAMP DEFAULT NOW())""")

    # seed default checklist items (only if none exist)
    c.execute("SELECT COUNT(*) as cnt FROM checklist_items")
    if c.fetchone()[0] == 0:
        default_items = [
            (str(__import__('uuid').uuid4()), 'Bathrooms cleaned and stocked', 'checkbox', True, 1, ''),
            (str(__import__('uuid').uuid4()), 'Thermostat set to away temperature', 'checkbox', True, 2, 'Set to 78°F cooling / 65°F heating'),
            (str(__import__('uuid').uuid4()), 'All trash emptied and taken out', 'checkbox', True, 3, ''),
            (str(__import__('uuid').uuid4()), 'Garage door and back door locked', 'checkbox', True, 4, 'Check both doors'),
            (str(__import__('uuid').uuid4()), 'All lights turned off', 'checkbox', True, 5, 'Include stage lights, lobby, bathrooms'),
            (str(__import__('uuid').uuid4()), 'Space swept and items put away', 'checkbox', True, 6, ''),
            (str(__import__('uuid').uuid4()), 'Any incidents to report?', 'text', False, 7, 'Describe any incidents, injuries, or issues that occurred'),
            (str(__import__('uuid').uuid4()), 'Additional notes', 'text', False, 8, 'Anything else the admin should know'),
        ]
        for item in default_items:
            c.execute("INSERT INTO checklist_items (id,label,item_type,required,sort_order,hint) VALUES (%s,%s,%s,%s,%s,%s)", item)

    # pending profile updates (kiosk)
    c.execute("""CREATE TABLE IF NOT EXISTS pending_profile_updates (
        id TEXT PRIMARY KEY,
        volunteer_id TEXT NOT NULL REFERENCES volunteers(id) ON DELETE CASCADE,
        field_name TEXT NOT NULL,
        old_value TEXT,
        new_value TEXT NOT NULL,
        submitted_at TIMESTAMP DEFAULT NOW(),
        status TEXT DEFAULT 'pending')""")

    # Run migrations in separate try blocks so failures don't roll back table creation
    for col_sql in [
        "ALTER TABLE waiver_types ADD COLUMN IF NOT EXISTS required_all BOOLEAN DEFAULT FALSE",
        "ALTER TABLE waiver_types ADD COLUMN IF NOT EXISTS required_for_volunteering BOOLEAN DEFAULT FALSE",
        "ALTER TABLE waiver_types ADD COLUMN IF NOT EXISTS can_sign_online BOOLEAN DEFAULT FALSE",
        "ALTER TABLE waiver_types ADD COLUMN IF NOT EXISTS expires_days INTEGER",
        "ALTER TABLE interest_types ADD COLUMN IF NOT EXISTS sub_options TEXT DEFAULT '[]'",
        "ALTER TABLE interest_types ADD COLUMN IF NOT EXISTS sub_options_label TEXT DEFAULT ''",
        "UPDATE interest_types SET sub_options='[]' WHERE sub_options IS NULL",
        "UPDATE interest_types SET sub_options_label='' WHERE sub_options_label IS NULL",
        "ALTER TABLE volunteer_applications ADD COLUMN IF NOT EXISTS sub_selections TEXT DEFAULT '{}'",
        "UPDATE volunteer_applications SET sub_selections='{}' WHERE sub_selections IS NULL",
        # Sync required_all from required_for_volunteering — they should be the same column
        "UPDATE waiver_types SET required_all=required_for_volunteering WHERE required_for_volunteering=TRUE AND (required_all IS NULL OR required_all=FALSE)",
        "UPDATE waiver_types SET required_for_volunteering=required_all WHERE required_all=TRUE AND (required_for_volunteering IS NULL OR required_for_volunteering=FALSE)",
        "ALTER TABLE events ADD COLUMN IF NOT EXISTS start_time TEXT",
        "ALTER TABLE events ADD COLUMN IF NOT EXISTS auto_log_hours BOOLEAN DEFAULT FALSE",
        "ALTER TABLE events ADD COLUMN IF NOT EXISTS end_time TEXT",
        "ALTER TABLE events ADD COLUMN IF NOT EXISTS end_date TEXT",
        "ALTER TABLE events ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'draft'",
        "ALTER TABLE events ADD COLUMN IF NOT EXISTS event_type_id TEXT",
        "ALTER TABLE events ADD COLUMN IF NOT EXISTS location TEXT",
        "ALTER TABLE events ADD COLUMN IF NOT EXISTS room TEXT",
        "ALTER TABLE events ADD COLUMN IF NOT EXISTS production_id TEXT",
        "ALTER TABLE events ADD COLUMN IF NOT EXISTS expected_volunteers INTEGER",
        "ALTER TABLE events ADD COLUMN IF NOT EXISTS notes TEXT",
        "ALTER TABLE productions ADD COLUMN IF NOT EXISTS stage TEXT DEFAULT 'mainstage'",
        "ALTER TABLE productions ADD COLUMN IF NOT EXISTS youth_program_id TEXT",
        "ALTER TABLE productions ADD COLUMN IF NOT EXISTS default_elic_id TEXT",
        "ALTER TABLE youth_programs ADD COLUMN IF NOT EXISTS default_elic_id TEXT",
        "ALTER TABLE events ADD COLUMN IF NOT EXISTS program_id TEXT",
        "ALTER TABLE events ADD COLUMN IF NOT EXISTS requires_background_check BOOLEAN DEFAULT FALSE",
        "ALTER TABLE volunteers ADD COLUMN IF NOT EXISTS background_check_date TEXT",
        "ALTER TABLE volunteers ADD COLUMN IF NOT EXISTS background_check_status TEXT DEFAULT 'none'",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS role_permissions TEXT DEFAULT '{}'",
        "ALTER TABLE youth_programs ADD COLUMN IF NOT EXISTS start_date TEXT",
        "ALTER TABLE youth_programs ADD COLUMN IF NOT EXISTS end_date TEXT",
        "ALTER TABLE youth_programs ADD COLUMN IF NOT EXISTS program_type TEXT DEFAULT 'class'",
        "ALTER TABLE youth_programs ADD COLUMN IF NOT EXISTS instructor_id TEXT",
        "ALTER TABLE volunteer_waivers ADD COLUMN IF NOT EXISTS emergency_contact_name TEXT",
        "ALTER TABLE volunteer_waivers ADD COLUMN IF NOT EXISTS emergency_contact_phone TEXT",
        "ALTER TABLE volunteer_waivers ADD COLUMN IF NOT EXISTS emergency_contact_relationship TEXT",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS active BOOLEAN DEFAULT TRUE",
        "ALTER TABLE youth_participants ADD COLUMN IF NOT EXISTS programs TEXT DEFAULT '[]'",
        "ALTER TABLE youth_production_members ADD COLUMN IF NOT EXISTS role TEXT",
        # volunteer-participant linking
        "ALTER TABLE volunteers ADD COLUMN IF NOT EXISTS linked_participant_id TEXT REFERENCES youth_participants(id) ON DELETE SET NULL",
        "ALTER TABLE youth_participants ADD COLUMN IF NOT EXISTS linked_volunteer_id TEXT REFERENCES volunteers(id) ON DELETE SET NULL",
        # stage manager role (no DB change needed, just allow it in validation)
        # notifications read tracking
        """CREATE TABLE IF NOT EXISTS notification_reads (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            notification_type TEXT NOT NULL,
            notification_id TEXT NOT NULL,
            read_at TIMESTAMP DEFAULT NOW(),
            UNIQUE(user_id, notification_type, notification_id))""",
        # production conflicts
        """CREATE TABLE IF NOT EXISTS production_conflicts (
            id TEXT PRIMARY KEY,
            production_id TEXT NOT NULL REFERENCES productions(id) ON DELETE CASCADE,
            event_id TEXT REFERENCES events(id) ON DELETE CASCADE,
            youth_id TEXT REFERENCES youth_participants(id) ON DELETE CASCADE,
            volunteer_id TEXT REFERENCES volunteers(id) ON DELETE CASCADE,
            status TEXT NOT NULL DEFAULT 'absent',
            source TEXT NOT NULL DEFAULT 'admin',
            notes TEXT,
            approved BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMP DEFAULT NOW(),
            created_by_portal BOOLEAN DEFAULT FALSE)""",
        "ALTER TABLE volunteer_waivers ADD COLUMN IF NOT EXISTS youth_id TEXT REFERENCES youth_participants(id) ON DELETE CASCADE",
        # portal features
        "ALTER TABLE youth_participants ADD COLUMN IF NOT EXISTS family_id TEXT",
        "ALTER TABLE youth_participants ADD COLUMN IF NOT EXISTS passphrase TEXT",
        "ALTER TABLE volunteers ADD COLUMN IF NOT EXISTS portal_passphrase TEXT",
        # portal content tables
        """CREATE TABLE IF NOT EXISTS families (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            passphrase TEXT UNIQUE NOT NULL,
            created_at TIMESTAMP DEFAULT NOW())""",
        """CREATE TABLE IF NOT EXISTS portal_announcements (
            id TEXT PRIMARY KEY,
            program_id TEXT REFERENCES youth_programs(id) ON DELETE CASCADE,
            production_id TEXT REFERENCES productions(id) ON DELETE CASCADE,
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            status TEXT DEFAULT 'published',
            author_id TEXT REFERENCES users(id),
            created_at TIMESTAMP DEFAULT NOW())""",
        """CREATE TABLE IF NOT EXISTS portal_files (
            id TEXT PRIMARY KEY,
            program_id TEXT REFERENCES youth_programs(id) ON DELETE CASCADE,
            production_id TEXT REFERENCES productions(id) ON DELETE CASCADE,
            title TEXT NOT NULL,
            drive_url TEXT NOT NULL,
            description TEXT,
            author_id TEXT REFERENCES users(id),
            created_at TIMESTAMP DEFAULT NOW())""",
        # portal folders
        "ALTER TABLE portal_files ADD COLUMN IF NOT EXISTS folder TEXT DEFAULT 'General'",
        # email settings
        """CREATE TABLE IF NOT EXISTS email_settings (
            id INTEGER PRIMARY KEY DEFAULT 1,
            resend_api_key TEXT DEFAULT '',
            from_email TEXT DEFAULT 'info@hwtco.org',
            report_recipients TEXT DEFAULT '',
            alert_pending_hours BOOLEAN DEFAULT TRUE,
            alert_profile_updates BOOLEAN DEFAULT TRUE,
            alert_callouts BOOLEAN DEFAULT TRUE,
            alert_waiver_expiry BOOLEAN DEFAULT TRUE,
            auto_send_checklist_report BOOLEAN DEFAULT TRUE,
            updated_at TIMESTAMP DEFAULT NOW())""",
        "INSERT INTO email_settings (id) VALUES (1) ON CONFLICT (id) DO NOTHING",
        "ALTER TABLE email_settings ADD COLUMN IF NOT EXISTS report_recipient_user_ids TEXT DEFAULT '[]'",
        "ALTER TABLE email_settings ADD COLUMN IF NOT EXISTS alert_conflicts BOOLEAN DEFAULT TRUE",
        "ALTER TABLE email_settings ADD COLUMN IF NOT EXISTS alert_waivers BOOLEAN DEFAULT TRUE",
        "ALTER TABLE email_settings ADD COLUMN IF NOT EXISTS alert_event_not_opened BOOLEAN DEFAULT TRUE",
        "ALTER TABLE email_settings ADD COLUMN IF NOT EXISTS alert_event_not_closed BOOLEAN DEFAULT TRUE",
        "ALTER TABLE email_settings ADD COLUMN IF NOT EXISTS alert_new_rsvp BOOLEAN DEFAULT TRUE",
        "ALTER TABLE email_settings ADD COLUMN IF NOT EXISTS alert_role_filled BOOLEAN DEFAULT TRUE",
        "ALTER TABLE productions ADD COLUMN IF NOT EXISTS venue TEXT",
        "ALTER TABLE youth_programs ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'active'",
        """CREATE TABLE IF NOT EXISTS production_general_content (
            id TEXT PRIMARY KEY,
            production_id TEXT NOT NULL REFERENCES productions(id) ON DELETE CASCADE,
            html_content TEXT DEFAULT '',
            updated_at TIMESTAMP DEFAULT NOW(),
            updated_by TEXT)""",
        "ALTER TABLE portal_announcements ADD COLUMN IF NOT EXISTS pushed_at TIMESTAMP",
        "ALTER TABLE portal_announcements ADD COLUMN IF NOT EXISTS push_count INTEGER DEFAULT 0",
        "ALTER TABLE portal_announcements ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'published'",
        "ALTER TABLE volunteer_applications ADD COLUMN IF NOT EXISTS pronouns TEXT",
        "ALTER TABLE volunteer_applications ADD COLUMN IF NOT EXISTS is_adult BOOLEAN DEFAULT TRUE",
        "UPDATE users SET role='staff' WHERE role NOT IN ('admin','staff')",
        "ALTER TABLE portal_announcements ADD COLUMN IF NOT EXISTS body_draft TEXT",
        "ALTER TABLE portal_announcements ADD COLUMN IF NOT EXISTS title_draft TEXT",
        """CREATE TABLE IF NOT EXISTS kiosk_sessions (
            id TEXT PRIMARY KEY,
            volunteer_id TEXT NOT NULL REFERENCES volunteers(id) ON DELETE CASCADE,
            event_id TEXT REFERENCES events(id),
            event_name TEXT,
            role TEXT DEFAULT '',
            started_at TIMESTAMP DEFAULT NOW(),
            ended_at TIMESTAMP,
            hours NUMERIC(5,2),
            status TEXT DEFAULT 'active')""",
        """CREATE TABLE IF NOT EXISTS nav_icons (
            key TEXT PRIMARY KEY,
            lucide_name TEXT NOT NULL)""",
        "ALTER TABLE productions ADD COLUMN IF NOT EXISTS image_url TEXT",
        "ALTER TABLE productions ADD COLUMN IF NOT EXISTS performance_location TEXT",
        "ALTER TABLE productions ADD COLUMN IF NOT EXISTS portal_color TEXT",
        "ALTER TABLE productions ADD COLUMN IF NOT EXISTS portal_image_url TEXT",
        "ALTER TABLE productions ADD COLUMN IF NOT EXISTS director TEXT",
        # meet the team
        """CREATE TABLE IF NOT EXISTS production_team_members (
            id TEXT PRIMARY KEY,
            production_id TEXT NOT NULL REFERENCES productions(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            role TEXT,
            bio TEXT,
            headshot_url TEXT,
            sort_order INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT NOW())""",
        "ALTER TABLE pending_profile_updates ADD COLUMN IF NOT EXISTS youth_id TEXT REFERENCES youth_participants(id) ON DELETE CASCADE",
        """CREATE TABLE IF NOT EXISTS portal_folders (
            id TEXT PRIMARY KEY,
            program_id TEXT REFERENCES youth_programs(id) ON DELETE CASCADE,
            production_id TEXT REFERENCES productions(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            sort_order INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT NOW())""",
        # audit trail columns
        "ALTER TABLE volunteers ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP",
        "ALTER TABLE volunteers ADD COLUMN IF NOT EXISTS created_by TEXT",
        "ALTER TABLE volunteers ADD COLUMN IF NOT EXISTS updated_by TEXT",
        "ALTER TABLE youth_participants ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP",
        "ALTER TABLE youth_participants ADD COLUMN IF NOT EXISTS created_by TEXT",
        "ALTER TABLE youth_participants ADD COLUMN IF NOT EXISTS updated_by TEXT",
        "ALTER TABLE events ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP",
        "ALTER TABLE events ADD COLUMN IF NOT EXISTS created_by TEXT",
        "ALTER TABLE events ADD COLUMN IF NOT EXISTS updated_by TEXT",
        "ALTER TABLE productions ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP",
        "ALTER TABLE productions ADD COLUMN IF NOT EXISTS created_by TEXT",
        "ALTER TABLE productions ADD COLUMN IF NOT EXISTS updated_by TEXT",
        "ALTER TABLE youth_programs ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP",
        "ALTER TABLE youth_programs ADD COLUMN IF NOT EXISTS created_by TEXT",
        "ALTER TABLE youth_programs ADD COLUMN IF NOT EXISTS updated_by TEXT",
        # missing columns found in audit
        "ALTER TABLE productions ADD COLUMN IF NOT EXISTS general_content TEXT DEFAULT ''",
        "ALTER TABLE elics ADD COLUMN IF NOT EXISTS assigned_events TEXT DEFAULT '[]'",
        "ALTER TABLE volunteers ADD COLUMN IF NOT EXISTS linked_youth_id TEXT",
        "ALTER TABLE volunteers ADD COLUMN IF NOT EXISTS pronouns TEXT DEFAULT ''",
        "UPDATE volunteers SET pronouns='' WHERE pronouns IS NULL",
        # Backfill pronouns from approved applications for existing volunteers
        """UPDATE volunteers v SET pronouns=a.pronouns
           FROM volunteer_applications a
           WHERE a.volunteer_id=v.id
           AND (a.pronouns IS NOT NULL AND a.pronouns != '')
           AND (v.pronouns IS NULL OR v.pronouns='')""",
        "ALTER TABLE volunteers ADD COLUMN IF NOT EXISTS sub_selections TEXT DEFAULT '{}'",
        "UPDATE volunteers SET sub_selections='{}' WHERE sub_selections IS NULL",
        """CREATE TABLE IF NOT EXISTS event_rsvps (
            id TEXT PRIMARY KEY,
            event_id TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
            volunteer_id TEXT REFERENCES volunteers(id) ON DELETE CASCADE,
            volunteer_name TEXT,
            volunteer_email TEXT,
            token TEXT UNIQUE,
            role_id TEXT,
            role_name TEXT DEFAULT '',
            status TEXT DEFAULT 'interested',
            notes TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT NOW())""",
        """CREATE TABLE IF NOT EXISTS event_roles (
            id TEXT PRIMARY KEY,
            event_id TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            slots INTEGER NOT NULL DEFAULT 1,
            description TEXT DEFAULT '',
            sort_order INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT NOW())""",
        "ALTER TABLE event_rsvps ADD COLUMN IF NOT EXISTS role_id TEXT",
        "ALTER TABLE event_rsvps ADD COLUMN IF NOT EXISTS role_name TEXT DEFAULT ''",
        "ALTER TABLE events ADD COLUMN IF NOT EXISTS rsvp_enabled BOOLEAN DEFAULT FALSE",
        "ALTER TABLE events ADD COLUMN IF NOT EXISTS rsvp_message TEXT DEFAULT ''",
        """CREATE TABLE IF NOT EXISTS event_staff (
            id TEXT PRIMARY KEY,
            event_id TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
            volunteer_id TEXT NOT NULL REFERENCES volunteers(id) ON DELETE CASCADE,
            role TEXT DEFAULT '',
            notes TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT NOW(),
            UNIQUE(event_id, volunteer_id))""",
        "ALTER TABLE production_members ADD COLUMN IF NOT EXISTS bio TEXT DEFAULT ''",
        "ALTER TABLE production_members ADD COLUMN IF NOT EXISTS photo_url TEXT DEFAULT ''",
        "ALTER TABLE events ADD COLUMN IF NOT EXISTS event_date TEXT",
        # missing tables
        """CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT)""",
        """CREATE TABLE IF NOT EXISTS alerts (
            id TEXT PRIMARY KEY,
            type TEXT DEFAULT 'info',
            message TEXT NOT NULL,
            source TEXT DEFAULT '',
            status TEXT DEFAULT 'active',
            created_at TIMESTAMP DEFAULT NOW())""",
        """CREATE TABLE IF NOT EXISTS production_required_waivers (
            id TEXT PRIMARY KEY,
            production_id TEXT NOT NULL REFERENCES productions(id) ON DELETE CASCADE,
            waiver_type_id TEXT NOT NULL REFERENCES waiver_types(id) ON DELETE CASCADE,
            UNIQUE(production_id, waiver_type_id))""",
        # meet the team — standalone public-facing entries (no volunteer required)
        """CREATE TABLE IF NOT EXISTS production_team_bios (
            id TEXT PRIMARY KEY,
            production_id TEXT NOT NULL REFERENCES productions(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            role TEXT DEFAULT '',
            bio TEXT DEFAULT '',
            headshot_url TEXT DEFAULT '',
            sort_order INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT NOW())""",
        # one-time dedup: keep only the oldest checklist item per label
        """DELETE FROM checklist_items WHERE id NOT IN (
            SELECT DISTINCT ON (label) id FROM checklist_items ORDER BY label, created_at ASC)""",
        """DELETE FROM opening_checklist_items WHERE id NOT IN (
            SELECT DISTINCT ON (label) id FROM opening_checklist_items ORDER BY label, created_at ASC)""",
        # volunteer interest/application form
        """CREATE TABLE IF NOT EXISTS volunteer_applications (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            email TEXT NOT NULL,
            phone TEXT,
            interests TEXT DEFAULT '[]',
            how_heard TEXT,
            notes TEXT,
            status TEXT DEFAULT 'pending',
            reviewed_by TEXT,
            reviewed_at TIMESTAMP,
            volunteer_id TEXT REFERENCES volunteers(id) ON DELETE SET NULL,
            created_at TIMESTAMP DEFAULT NOW())""",

        # ── Donor & Sponsor Management ──
        """CREATE TABLE IF NOT EXISTS donor_tiers (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            min_amount NUMERIC(10,2) NOT NULL DEFAULT 0,
            max_amount NUMERIC(10,2),
            color TEXT DEFAULT 'teal',
            description TEXT,
            sort_order INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT NOW())""",

        """CREATE TABLE IF NOT EXISTS donor_tier_benefits (
            id TEXT PRIMARY KEY,
            tier_id TEXT NOT NULL REFERENCES donor_tiers(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            description TEXT,
            is_trackable BOOLEAN DEFAULT TRUE,
            sort_order INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT NOW())""",

        """CREATE TABLE IF NOT EXISTS donor_campaigns (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT,
            goal_amount NUMERIC(10,2),
            start_date TEXT,
            end_date TEXT,
            status TEXT DEFAULT 'active',
            created_at TIMESTAMP DEFAULT NOW())""",

        """CREATE TABLE IF NOT EXISTS campaign_benefits (
            id TEXT PRIMARY KEY,
            campaign_id TEXT NOT NULL REFERENCES donor_campaigns(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            description TEXT DEFAULT '',
            min_amount NUMERIC(10,2) NOT NULL DEFAULT 0,
            is_trackable BOOLEAN DEFAULT FALSE,
            sort_order INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT NOW())""",

        """CREATE TABLE IF NOT EXISTS donors (
            id TEXT PRIMARY KEY,
            type TEXT DEFAULT 'individual',
            display_name TEXT NOT NULL,
            legal_name TEXT,
            email TEXT,
            phone TEXT,
            address TEXT,
            website TEXT,
            volunteer_id TEXT REFERENCES volunteers(id) ON DELETE SET NULL,
            tier_id TEXT REFERENCES donor_tiers(id) ON DELETE SET NULL,
            tier_override BOOLEAN DEFAULT FALSE,
            is_anonymous BOOLEAN DEFAULT FALSE,
            recognition_name TEXT,
            notes TEXT,
            internal_rating TEXT DEFAULT 'normal',
            status TEXT DEFAULT 'active',
            first_donation_date TEXT,
            last_donation_date TEXT,
            total_donated NUMERIC(10,2) DEFAULT 0,
            created_at TIMESTAMP DEFAULT NOW(),
            created_by TEXT)""",

        """CREATE TABLE IF NOT EXISTS donor_donations (
            id TEXT PRIMARY KEY,
            donor_id TEXT NOT NULL REFERENCES donors(id) ON DELETE CASCADE,
            campaign_id TEXT REFERENCES donor_campaigns(id) ON DELETE SET NULL,
            amount NUMERIC(10,2) NOT NULL,
            donation_date TEXT NOT NULL,
            type TEXT DEFAULT 'cash',
            payment_status TEXT DEFAULT 'received',
            check_number TEXT,
            notes TEXT,
            thank_you_sent BOOLEAN DEFAULT FALSE,
            thank_you_sent_at TIMESTAMP,
            thank_you_sent_by TEXT,
            created_at TIMESTAMP DEFAULT NOW(),
            created_by TEXT)""",

        """CREATE TABLE IF NOT EXISTS donor_benefit_usage (
            id TEXT PRIMARY KEY,
            donor_id TEXT NOT NULL REFERENCES donors(id) ON DELETE CASCADE,
            benefit_id TEXT NOT NULL REFERENCES donor_tier_benefits(id) ON DELETE CASCADE,
            used_at TIMESTAMP DEFAULT NOW(),
            notes TEXT,
            recorded_by TEXT)""",

        """CREATE TABLE IF NOT EXISTS donor_communications (
            id TEXT PRIMARY KEY,
            donor_id TEXT NOT NULL REFERENCES donors(id) ON DELETE CASCADE,
            type TEXT DEFAULT 'note',
            subject TEXT,
            body TEXT,
            sent_at TIMESTAMP DEFAULT NOW(),
            sent_by TEXT)""",

        """CREATE TABLE IF NOT EXISTS donor_email_templates (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            subject TEXT NOT NULL,
            body TEXT NOT NULL,
            from_email TEXT DEFAULT '',
            from_name TEXT DEFAULT '',
            template_type TEXT DEFAULT 'thankyou',
            is_default BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT NOW())""",

        """CREATE TABLE IF NOT EXISTS scheduled_reports (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            report_type TEXT NOT NULL,
            cadence TEXT DEFAULT 'monthly',
            send_day INTEGER DEFAULT 1,
            recipient_user_ids TEXT DEFAULT '[]',
            recipient_emails TEXT DEFAULT '',
            params TEXT DEFAULT '{}',
            is_active BOOLEAN DEFAULT TRUE,
            last_sent_at TIMESTAMP,
            next_send_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT NOW())""",

        # Missing tables that get referenced
        """CREATE TABLE IF NOT EXISTS schedule_conflicts (
            id TEXT PRIMARY KEY,
            production_id TEXT REFERENCES productions(id) ON DELETE CASCADE,
            event_id TEXT REFERENCES events(id) ON DELETE SET NULL,
            volunteer_id TEXT REFERENCES volunteers(id) ON DELETE CASCADE,
            status TEXT DEFAULT 'absent',
            event_date TEXT,
            notes TEXT,
            approved BOOLEAN DEFAULT FALSE,
            source TEXT DEFAULT 'staff',
            created_at TIMESTAMP DEFAULT NOW())""",

        """CREATE TABLE IF NOT EXISTS portal_files (
            id TEXT PRIMARY KEY,
            context_type TEXT NOT NULL,
            context_id TEXT NOT NULL,
            name TEXT NOT NULL,
            url TEXT,
            file_type TEXT,
            created_at TIMESTAMP DEFAULT NOW())""",

        """CREATE TABLE IF NOT EXISTS carpools (
            id TEXT PRIMARY KEY,
            event_id TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            driver_name TEXT NOT NULL,
            driver_phone TEXT DEFAULT '',
            code TEXT NOT NULL UNIQUE,
            max_seats INTEGER DEFAULT 6,
            notes TEXT DEFAULT '',
            status TEXT DEFAULT 'open',
            created_at TIMESTAMP DEFAULT NOW())""",

        """CREATE TABLE IF NOT EXISTS carpool_members (
            id TEXT PRIMARY KEY,
            carpool_id TEXT NOT NULL REFERENCES carpools(id) ON DELETE CASCADE,
            youth_id TEXT NOT NULL REFERENCES youth_participants(id) ON DELETE CASCADE,
            added_by TEXT DEFAULT '',
            added_via TEXT DEFAULT 'admin',
            UNIQUE(carpool_id, youth_id))""",
    ]:
        try:
            c.execute(col_sql)
            conn.commit()
        except Exception:
            conn.rollback()

    # Seed default HWTC donor tiers if none exist
    try:
        c.execute("SELECT COUNT(*) FROM donor_tiers")
        if c.fetchone()[0] == 0:
            default_tiers = [
                ('Theatregoer',              0,      100,    'gray',   8),
                ('Dramaturg',                101,    499,    'blue',   7),
                ('Playwright',               500,    1499,   'teal',   6),
                ('Director',                 1500,   2999,   'green',  5),
                ('Associate Producer',       3000,   4999,   'amber',  4),
                ('Producer',                 5000,   6999,   'orange', 3),
                ('Executive Producer',       7000,   9999,   'purple', 2),
                ('Production Sponsor',       10000,  49999,  'pink',   1),
                ('Season Production Sponsor',50000,  None,   'red',    0),
            ]
            import uuid as _uuid2
            for name, min_a, max_a, color, sort in default_tiers:
                c.execute(
                    "INSERT INTO donor_tiers (id,name,min_amount,max_amount,color,sort_order) VALUES (%s,%s,%s,%s,%s,%s)",
                    (str(_uuid2.uuid4()), name, min_a, max_a, color, sort)
                )
            conn.commit()
    except Exception:
        conn.rollback()

    conn.close()

    # One-time backfill: copy application notes to volunteer profile notes
    try:
        conn2 = get_db()
        c2 = conn2.cursor()
        c2.execute("""
            INSERT INTO notes (id, volunteer_id, author, content)
            SELECT gen_random_uuid()::text, a.volunteer_id, 'Join Form', a.notes
            FROM volunteer_applications a
            WHERE a.status = 'approved'
              AND a.volunteer_id IS NOT NULL
              AND (a.notes IS NOT NULL AND a.notes != '')
              AND NOT EXISTS (
                SELECT 1 FROM notes n
                WHERE n.volunteer_id = a.volunteer_id
                  AND n.author = 'Join Form'
                  AND n.content = a.notes
              )
        """)
        conn2.commit()
        conn2.close()
    except Exception:
        try: conn2.rollback(); conn2.close()
        except Exception: pass

# ─────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────

def require_auth():
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    return None

def require_admin():
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    if session.get('role') != 'admin':
        return jsonify({'error': 'Admin required'}), 403
    return None

# ─────────────────────────────────────────────
#  EMAIL HELPERS
# ─────────────────────────────────────────────

def get_email_settings():
    try:
        conn = get_db()
        row = fetchone(conn, 'SELECT * FROM email_settings WHERE id=1')
        conn.close()
        return row or {}
    except Exception:
        return {}

def get_recipient_emails(settings=None):
    if settings is None:
        settings = get_email_settings()
    emails = []
    try:
        conn = get_db()
        ids_raw = settings.get('report_recipient_user_ids') or '[]'
        user_ids = json.loads(ids_raw) if isinstance(ids_raw, str) else (ids_raw or [])
        if user_ids:
            placeholders = ','.join(['%s'] * len(user_ids))
            users = fetchall(conn, f'SELECT email FROM users WHERE id IN ({placeholders})', tuple(user_ids))
            emails = [u['email'] for u in users if u.get('email')]
        if not emails:
            raw = settings.get('report_recipients','')
            if raw:
                emails = [e.strip() for e in raw.split(',') if e.strip()]
        conn.close()
    except Exception:
        pass
    return emails

def send_email(to_emails, subject, html_body, from_email=None):
    """Send via Resend API."""
    settings = get_email_settings()
    api_key = settings.get('resend_api_key','').strip()
    if not api_key:
        app.logger.warning('Resend API key not configured — email not sent')
        return False, 'Resend API key not configured'
    from_addr = from_email or settings.get('from_email','info@hwtco.org')
    if isinstance(to_emails, str):
        to_emails = [e.strip() for e in to_emails.split(',') if e.strip()]
    if not to_emails:
        return False, 'No recipients'
    try:
        import requests as _req
        resp = _req.post('https://api.resend.com/emails',
            headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
            json={'from': from_addr, 'to': to_emails, 'subject': subject, 'html': html_body},
            timeout=10)
        if resp.status_code not in (200, 201, 202):
            app.logger.error(f'Resend error: {resp.status_code} {resp.text}')
            return False, f'Resend error {resp.status_code}: {resp.text[:200]}'
        return True, None
    except Exception as e:
        app.logger.error(f'Email send error: {e}')
        return False, str(e)

def serialize_row(r):
    out = {}
    for k, v in r.items():
        if isinstance(v, (datetime, date)):
            out[k] = v.isoformat()
        else:
            out[k] = v
    return out

def fetchall(conn, sql, params=()):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as c:
        c.execute(sql, params)
        return [serialize_row(r) for r in c.fetchall()]

def fetchone(conn, sql, params=()):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as c:
        c.execute(sql, params)
        r = c.fetchone()
        return serialize_row(r) if r else None

def execute(conn, sql, params=()):
    with conn.cursor() as c:
        c.execute(sql, params)

def get_waiver_summary(conn, vol_id):
    from datetime import date as _date, datetime as _datetime
    waivers = fetchall(conn,
        'SELECT vw.*, wt.name as type_name FROM volunteer_waivers vw JOIN waiver_types wt ON vw.waiver_type_id=wt.id WHERE vw.volunteer_id=%s ORDER BY vw.signed_date DESC',
        (vol_id,))
    today = _date.today()
    # Check required waivers first
    required = fetchall(conn, 'SELECT id FROM waiver_types WHERE required_for_volunteering=TRUE OR required_all=TRUE')
    signed_type_ids = set(w['waiver_type_id'] for w in waivers)
    has_missing_required = any(r['id'] not in signed_type_ids for r in required)

    worst = 'none'
    for w in waivers:
        if not w['expiry_date']:
            if worst == 'none': worst = 'valid'
            continue
        try:
            diff = (_datetime.strptime(str(w['expiry_date'])[:10], '%Y-%m-%d').date() - today).days
        except Exception:
            if worst == 'none': worst = 'valid'
            continue
        if diff < 0: worst = 'expired'; break
        elif diff < 30 and worst != 'expired': worst = 'expiring'
        elif worst == 'none': worst = 'valid'

    # If missing a required waiver, downgrade to expired (worst)
    if has_missing_required and worst not in ('expired',):
        worst = 'expired'
    return worst, waivers

# ─────────────────────────────────────────────
#  SERVE FRONTEND
# ─────────────────────────────────────────────

@app.route('/')
def index():
    resp = send_from_directory('static', 'index.html')
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    return resp
@app.route('/api/debug')
def debug():
    try:
        conn = get_db()
        tables = fetchall(conn, "SELECT table_name FROM information_schema.tables WHERE table_schema='public' ORDER BY table_name")
        counts = {}
        for t in tables:
            name = t['table_name']
            try:
                row = fetchone(conn, f'SELECT COUNT(*) as c FROM "{name}"')
                counts[name] = row['c']
            except:
                counts[name] = 'error'
        conn.close()
        return jsonify({'status': 'ok', 'db_url_set': bool(DATABASE_URL), 'tables': counts})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e), 'db_url_set': bool(DATABASE_URL)})



# ─────────────────────────────────────────────
#  AUTH
# ─────────────────────────────────────────────

@app.route('/api/auth/login', methods=['POST'])
def login():
    d = request.json
    pw_hash = hashlib.sha256(d.get('password','').encode()).hexdigest()
    conn = get_db()
    user = fetchone(conn, 'SELECT * FROM users WHERE email=%s AND password_hash=%s', (d.get('email',''), pw_hash))
    conn.close()
    if not user: return jsonify({'error': 'Invalid email or password'}), 401
    if not user.get('active', True): return jsonify({'error': 'Your account has been deactivated. Contact an administrator.'}), 403
    session['user_id'] = user['id']
    session['user_name'] = user['name']
    session['role'] = user['role']
    # Store permissions in session for fast checking
    if user['role'] == 'admin':
        session['permissions'] = '{}'  # admin bypasses all checks
    else:
        session['permissions'] = user.get('role_permissions') or '{}'
    perms_dict = {'id': user['id'], 'name': user['name'], 'email': user['email'],
                  'role': user['role'], 'permissions': json.loads(session['permissions'] or '{}')}
    return jsonify(perms_dict)

@app.route('/api/auth/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({'ok': True})

@app.route('/api/auth/me')
def me():
    if 'user_id' not in session: return jsonify({'user': None})
    conn = get_db()
    u = fetchone(conn, 'SELECT id, name, email, role, role_permissions FROM users WHERE id=%s', (session['user_id'],))
    conn.close()
    if not u: return jsonify({'user': None})
    perms = {}
    if u['role'] != 'admin':
        try: perms = json.loads(u.get('role_permissions') or '{}')
        except Exception: perms = {}
    return jsonify({'user': {'id': u['id'], 'name': u['name'], 'email': u['email'],
                             'role': u['role'], 'permissions': perms}})

@app.route('/api/auth/change-password', methods=['POST'])
def change_password():
    """Self-service password change — any logged-in user."""
    if 'user_id' not in session:
        return jsonify({'error': 'Not logged in'}), 401
    d = request.json or {}
    current_pw  = d.get('current_password','')
    new_pw      = d.get('new_password','')
    if not current_pw or not new_pw:
        return jsonify({'error': 'Both current and new password are required'}), 400
    if len(new_pw) < 6:
        return jsonify({'error': 'New password must be at least 6 characters'}), 400
    conn = get_db()
    # Verify current password
    current_hash = hashlib.sha256(current_pw.encode()).hexdigest()
    user = fetchone(conn, 'SELECT id FROM users WHERE id=%s AND password_hash=%s',
                    (session['user_id'], current_hash))
    if not user:
        conn.close()
        return jsonify({'error': 'Current password is incorrect'}), 400
    new_hash = hashlib.sha256(new_pw.encode()).hexdigest()
    execute(conn, 'UPDATE users SET password_hash=%s WHERE id=%s', (new_hash, session['user_id']))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

# ─────────────────────────────────────────────
#  INTEREST TYPES
# ─────────────────────────────────────────────

@app.route('/api/interest-types')
def get_interest_types():
    err = require_auth()
    if err: return err
    conn = get_db()
    types = fetchall(conn, 'SELECT * FROM interest_types ORDER BY name')
    conn.close()
    return jsonify(types)

@app.route('/api/interest-types', methods=['POST'])
def create_interest_type():
    err = require_admin()
    if err: return err
    d = request.json or {}
    if not (d.get('name') or '').strip(): return jsonify({'error': 'Name is required'}), 400
    tid = str(uuid.uuid4())
    conn = get_db()
    try:
        sub_opts = json.dumps(d.get('sub_options') or [])
        sub_label = (d.get('sub_options_label') or '').strip()
        execute(conn, 'INSERT INTO interest_types (id,name,color,sub_options,sub_options_label) VALUES (%s,%s,%s,%s,%s)',
            (tid, (d.get('name') or '').strip(), d.get('color','gray'), sub_opts, sub_label))
        conn.commit()
        row = fetchone(conn, 'SELECT * FROM interest_types WHERE id=%s', (tid,))
        conn.close()
        return jsonify(row)
    except Exception as e:
        conn.rollback(); conn.close()
        app.logger.error(f'create_interest_type: {e}')
        if 'unique' in str(e).lower() or 'duplicate' in str(e).lower():
            return jsonify({'error': 'An interest type with that name already exists'}), 400
        return jsonify({'error': str(e)}), 500

@app.route('/api/interest-types/<tid>', methods=['PUT'])
def update_interest_type(tid):
    err = require_admin()
    if err: return err
    d = request.json or {}
    conn = get_db()
    try:
        sub_opts = json.dumps(d.get('sub_options') or [])
        sub_label = (d.get('sub_options_label') or '').strip()
        execute(conn, '''UPDATE interest_types SET name=%s, color=%s, sub_options=%s, sub_options_label=%s
            WHERE id=%s''',
            ((d.get('name') or '').strip(), d.get('color','gray'), sub_opts, sub_label, tid))
        conn.commit()
        row = fetchone(conn, 'SELECT * FROM interest_types WHERE id=%s', (tid,))
        conn.close()
        return jsonify(row)
    except Exception as e:
        conn.rollback(); conn.close()
        return jsonify({'error': str(e)}), 500

@app.route('/api/interest-types/<tid>', methods=['DELETE'])
def delete_interest_type(tid):
    err = require_admin()
    if err: return err
    conn = get_db()
    execute(conn, 'DELETE FROM interest_types WHERE id=%s', (tid,))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

# ─────────────────────────────────────────────
#  EVENTS
# ─────────────────────────────────────────────

@app.route('/api/events')
def get_events():
    err = require_auth()
    if err: return err
    conn = get_db()
    events = fetchall(conn, '''SELECT e.*,
        COALESCE(e.requires_background_check, FALSE) as requires_background_check,
        et.name as event_type_name, et.color as event_type_color,
        p.name as production_name, COALESCE(p.stage,'mainstage') as production_stage,
        pg.name as program_name
        FROM events e
        LEFT JOIN event_types et ON e.event_type_id=et.id
        LEFT JOIN productions p ON e.production_id=p.id
        LEFT JOIN youth_programs pg ON e.program_id=pg.id
        ORDER BY e.event_date DESC NULLS LAST, e.start_time ASC NULLS LAST''')
    for e in events:
        e['required_waivers'] = fetchall(conn,
            'SELECT ew.*, wt.name as waiver_name FROM event_waivers ew JOIN waiver_types wt ON ew.waiver_type_id=wt.id WHERE ew.event_id=%s', (e['id'],))
        e['elics'] = fetchall(conn, """SELECT ee.id as assignment_id, el.id as elic_id,
            el.is_master, v.name as volunteer_name, v.id as volunteer_id
            FROM event_elics ee JOIN elics el ON ee.elic_id=el.id
            JOIN volunteers v ON el.volunteer_id=v.id
            WHERE ee.event_id=%s""", (e['id'],))
        e['staff'] = fetchall(conn, """SELECT es.*, v.name as volunteer_name,
            v.background_check_status, v.email
            FROM event_staff es JOIN volunteers v ON es.volunteer_id=v.id
            WHERE es.event_id=%s ORDER BY es.role, v.name""", (e['id'],))
        e['status'] = e.get('status') or 'draft'
    conn.close()
    return jsonify(events)

@app.route('/api/events', methods=['POST'])
def create_event():
    err = require_admin()
    if err: return err
    d = request.json or {}
    if not (d.get('name') or '').strip():
        return jsonify({'error': 'Event name is required'}), 400
    if not (d.get('event_date') or '').strip():
        return jsonify({'error': 'Event date is required'}), 400
    eid = str(uuid.uuid4())
    conn = get_db()
    execute(conn, '''INSERT INTO events
        (id,name,event_date,end_date,start_time,end_time,event_type_id,location,room,production_id,program_id,expected_volunteers,description,notes,status,requires_background_check,auto_log_hours)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'draft',%s,%s)''',
        (eid, d.get('name',''), d.get('event_date') or None, d.get('end_date') or None,
         d.get('start_time') or None, d.get('end_time') or None,
         d.get('event_type_id') or None, d.get('location',''), d.get('room',''),
         d.get('production_id') or None, d.get('program_id') or None,
         d.get('expected_volunteers') or None,
         d.get('description',''), d.get('notes',''), d.get('requires_background_check',False),
         d.get('auto_log_hours', False)))
    conn.commit()
    row = fetchone(conn, '''SELECT e.*,
        COALESCE(e.requires_background_check, FALSE) as requires_background_check,
        et.name as event_type_name, et.color as event_type_color,
        p.name as production_name, COALESCE(p.stage,'mainstage') as production_stage,
        pg.name as program_name
        FROM events e
        LEFT JOIN event_types et ON e.event_type_id=et.id
        LEFT JOIN productions p ON e.production_id=p.id
        LEFT JOIN youth_programs pg ON e.program_id=pg.id
        WHERE e.id=%s''', (eid,))
    row['required_waivers'] = []; row['elics'] = []
    conn.close()
    return jsonify(row)

@app.route('/api/events/<eid>', methods=['PUT'])
def update_event(eid):
    err = require_admin()
    if err: return err
    d = request.json or {}
    conn = get_db()
    execute(conn, '''UPDATE events SET name=%s,event_date=%s,end_date=%s,start_time=%s,end_time=%s,
        event_type_id=%s,location=%s,room=%s,production_id=%s,program_id=%s,expected_volunteers=%s,
        description=%s,notes=%s,requires_background_check=%s,auto_log_hours=%s,
        rsvp_enabled=%s,rsvp_message=%s WHERE id=%s''',
        (d.get('name',''), d.get('event_date') or None, d.get('end_date') or None,
         d.get('start_time') or None, d.get('end_time') or None,
         d.get('event_type_id') or None, d.get('location',''), d.get('room',''),
         d.get('production_id') or None, d.get('program_id') or None,
         d.get('expected_volunteers') or None,
         d.get('description',''), d.get('notes',''), d.get('requires_background_check',False),
         d.get('auto_log_hours', False), d.get('rsvp_enabled', False),
         d.get('rsvp_message',''), eid))
    conn.commit()
    row = fetchone(conn, '''SELECT e.*,
        COALESCE(e.requires_background_check, FALSE) as requires_background_check,
        et.name as event_type_name, et.color as event_type_color,
        p.name as production_name, COALESCE(p.stage,'mainstage') as production_stage,
        pg.name as program_name
        FROM events e
        LEFT JOIN event_types et ON e.event_type_id=et.id
        LEFT JOIN productions p ON e.production_id=p.id
        LEFT JOIN youth_programs pg ON e.program_id=pg.id
        WHERE e.id=%s''', (eid,))
    row['required_waivers'] = fetchall(conn,
        'SELECT ew.*, wt.name as waiver_name FROM event_waivers ew JOIN waiver_types wt ON ew.waiver_type_id=wt.id WHERE ew.event_id=%s', (eid,))
    row['elics'] = fetchall(conn, """SELECT ee.id as assignment_id, el.id as elic_id,
        el.is_master, v.name as volunteer_name FROM event_elics ee
        JOIN elics el ON ee.elic_id=el.id JOIN volunteers v ON el.volunteer_id=v.id
        WHERE ee.event_id=%s""", (eid,))
    conn.close()
    return jsonify(row)

@app.route('/api/events/<eid>', methods=['DELETE'])
def delete_event(eid):
    err = require_admin()
    if err: return err
    conn = get_db()
    cur = conn.cursor()
    try:
        def try_sql(sql, params=()):
            try:
                cur.execute('SAVEPOINT sp')
                cur.execute(sql, params)
                cur.execute('RELEASE SAVEPOINT sp')
            except Exception as e:
                cur.execute('ROLLBACK TO SAVEPOINT sp')
                app.logger.warning(f'delete_event skip: {e}')

        try_sql('UPDATE youth_sign_ins SET event_id=NULL WHERE event_id=%s', (eid,))
        try_sql('UPDATE kiosk_sessions SET event_id=NULL WHERE event_id=%s', (eid,))
        try_sql('DELETE FROM event_waivers WHERE event_id=%s', (eid,))
        try_sql('DELETE FROM event_elics WHERE event_id=%s', (eid,))
        try_sql('DELETE FROM event_checklist_responses WHERE event_id=%s', (eid,))
        try_sql('DELETE FROM hours WHERE event_id=%s', (eid,))

        # Carpools
        try:
            cur.execute('SAVEPOINT sp_carpools')
            cur.execute('SELECT id FROM carpools WHERE event_id=%s', (eid,))
            carpool_ids = [r[0] for r in cur.fetchall()]
            for cid in carpool_ids:
                cur.execute('DELETE FROM carpool_members WHERE carpool_id=%s', (cid,))
            if carpool_ids:
                cur.execute('DELETE FROM carpools WHERE event_id=%s', (eid,))
            cur.execute('RELEASE SAVEPOINT sp_carpools')
        except Exception as e:
            cur.execute('ROLLBACK TO SAVEPOINT sp_carpools')
            app.logger.warning(f'delete_event carpools: {e}')

        # The main delete — if this fails we want the real error
        cur.execute('DELETE FROM events WHERE id=%s', (eid,))
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({'ok': True})

    except Exception as e:
        try: conn.rollback()
        except: pass
        try: cur.close()
        except: pass
        conn.close()
        app.logger.error(f'delete_event {eid}: {e}')
        return jsonify({'error': str(e)}), 500

# ─────────────────────────────────────────────
#  VOLUNTEERS
# ─────────────────────────────────────────────

@app.route('/api/volunteers')
def get_volunteers():
    err = require_auth()
    if err: return err
    conn = get_db()
    vols = fetchall(conn, '''SELECT *, COALESCE(background_check_status,'none') as background_check_status FROM volunteers ORDER BY name''')
    for v in vols:
        v['total_hours'] = fetchone(conn, 'SELECT COALESCE(SUM(hours),0) as t FROM hours WHERE volunteer_id=%s', (v['id'],))['t']
        v['waiver_status'], v['waivers'] = get_waiver_summary(conn, v['id'])
    conn.close()
    return jsonify(vols)

@app.route('/api/volunteers/<vol_id>')
def get_volunteer(vol_id):
    err = require_auth()
    if err: return err
    conn = get_db()
    vol = fetchone(conn, 'SELECT * FROM volunteers WHERE id=%s', (vol_id,))
    if not vol: conn.close(); return jsonify({'error': 'Not found'}), 404
    vol['hours']   = fetchall(conn, 'SELECT * FROM hours WHERE volunteer_id=%s ORDER BY date DESC', (vol_id,))
    vol['notes']   = fetchall(conn, 'SELECT * FROM notes WHERE volunteer_id=%s ORDER BY created_at DESC', (vol_id,))
    vol['history'] = fetchall(conn, 'SELECT * FROM volunteer_history WHERE volunteer_id=%s ORDER BY date DESC', (vol_id,))
    vol['files']   = fetchall(conn, 'SELECT * FROM volunteer_files WHERE volunteer_id=%s ORDER BY created_at DESC', (vol_id,))
    vol['emergency_contacts'] = fetchall(conn, 'SELECT * FROM volunteer_emergency_contacts WHERE volunteer_id=%s ORDER BY created_at DESC', (vol_id,))
    vol['productions'] = fetchall(conn, '''SELECT pm.*, p.name as production_name, p.production_type,
        p.start_date, p.end_date, p.status as production_status
        FROM production_members pm JOIN productions p ON pm.production_id=p.id
        WHERE pm.volunteer_id=%s ORDER BY p.start_date DESC NULLS LAST''', (vol_id,))
    vol['waiver_status'], vol['waivers'] = get_waiver_summary(conn, vol_id)
    vol['total_hours'] = fetchone(conn, 'SELECT COALESCE(SUM(hours),0) as t FROM hours WHERE volunteer_id=%s', (vol_id,))['t']
    conn.close()
    return jsonify(vol)

@app.route('/api/volunteers', methods=['POST'])
def create_volunteer():
    err = require_admin()
    if err: return err
    d = request.json
    vid = str(uuid.uuid4())
    conn = get_db()
    execute(conn, 'INSERT INTO volunteers (id,name,email,phone,birthday,status,interests,background_check_status,background_check_date) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)',
            (vid, d.get('name',''), d.get('email',''), d.get('phone',''), d.get('birthday') or None, d.get('status','active'), json.dumps(d.get('interests',[])), d.get('background_check_status','none'), d.get('background_check_date') or None))
    conn.commit()
    vol = fetchone(conn, 'SELECT * FROM volunteers WHERE id=%s', (vid,))
    vol['total_hours'] = 0; vol['waiver_status'] = 'none'; vol['waivers'] = []
    conn.close()
    return jsonify(vol)

@app.route('/api/volunteers/<vol_id>', methods=['PUT'])
def update_volunteer(vol_id):
    err = require_admin()
    if err: return err
    d = request.json or {}
    conn = get_db()
    sub_selections = json.dumps(d.get('sub_selections') or {})
    execute(conn, '''UPDATE volunteers SET name=%s,email=%s,phone=%s,pronouns=%s,birthday=%s,status=%s,
        interests=%s,sub_selections=%s,background_check_status=%s,background_check_date=%s
        WHERE id=%s''',
        (d.get('name',''), d.get('email',''), d.get('phone',''), d.get('pronouns',''),
         d.get('birthday') or None, d.get('status','active'),
         json.dumps(d.get('interests',[])), sub_selections,
         d.get('background_check_status','none'), d.get('background_check_date') or None, vol_id))
    conn.commit()
    vol = fetchone(conn, 'SELECT * FROM volunteers WHERE id=%s', (vol_id,))
    conn.close()
    return jsonify(vol)

@app.route('/api/volunteers/<vol_id>', methods=['DELETE'])
def delete_volunteer(vol_id):
    err = require_admin()
    if err: return err
    conn = get_db()
    waivers = fetchall(conn, 'SELECT filename FROM volunteer_waivers WHERE volunteer_id=%s', (vol_id,))
    for w in waivers:
        if w['filename']:
            try: os.remove(os.path.join(UPLOAD_FOLDER, w['filename']))
            except: pass
    execute(conn, 'DELETE FROM volunteers WHERE id=%s', (vol_id,))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

# ─────────────────────────────────────────────
#  HOURS
# ─────────────────────────────────────────────

@app.route('/api/hours')
def get_hours():
    err = require_auth()
    if err: return err
    conn = get_db()
    rows = fetchall(conn,
        'SELECT h.*, v.name as volunteer_name, v.email as volunteer_email FROM hours h JOIN volunteers v ON h.volunteer_id=v.id ORDER BY h.date DESC')
    conn.close()
    return jsonify(rows)

@app.route('/api/hours', methods=['POST'])
def create_hours():
    err = require_admin()
    if err: return err
    d = request.json
    hid = str(uuid.uuid4())
    conn = get_db()
    execute(conn, 'INSERT INTO hours (id,volunteer_id,event,event_id,date,hours,role,notes) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)',
            (hid, d.get('volunteer_id'), d.get('event',''), d.get('event_id'), d.get('date',''), d.get('hours',0), d.get('role',''), d.get('notes','')))
    conn.commit()
    row = fetchone(conn, 'SELECT h.*, v.name as volunteer_name FROM hours h JOIN volunteers v ON h.volunteer_id=v.id WHERE h.id=%s', (hid,))
    conn.close()
    return jsonify(row)

@app.route('/api/hours/<hid>', methods=['DELETE'])
def delete_hours(hid):
    err = require_admin()
    if err: return err
    conn = get_db()
    execute(conn, 'DELETE FROM hours WHERE id=%s', (hid,))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

# ─────────────────────────────────────────────
#  NOTES
# ─────────────────────────────────────────────

@app.route('/api/volunteers/<vol_id>/notes', methods=['POST'])
def create_note(vol_id):
    err = require_admin()
    if err: return err
    d = request.json
    nid = str(uuid.uuid4())
    conn = get_db()
    execute(conn, 'INSERT INTO notes (id,volunteer_id,author,content) VALUES (%s,%s,%s,%s)',
            (nid, vol_id, session['user_name'], d.get('content','')))
    conn.commit()
    note = fetchone(conn, 'SELECT * FROM notes WHERE id=%s', (nid,))
    conn.close()
    return jsonify(note)

# ─────────────────────────────────────────────
#  HISTORY
# ─────────────────────────────────────────────

@app.route('/api/volunteers/<vol_id>/history', methods=['POST'])
def create_history(vol_id):
    err = require_admin()
    if err: return err
    d = request.json
    hid = str(uuid.uuid4())
    conn = get_db()
    execute(conn, 'INSERT INTO volunteer_history (id,volunteer_id,event,role,date,notes) VALUES (%s,%s,%s,%s,%s,%s)',
            (hid, vol_id, d.get('event',''), d.get('role',''), d.get('date',''), d.get('notes','')))
    conn.commit()
    row = fetchone(conn, 'SELECT * FROM volunteer_history WHERE id=%s', (hid,))
    conn.close()
    return jsonify(row)

# ─────────────────────────────────────────────
#  FILES
# ─────────────────────────────────────────────

@app.route('/api/volunteers/<vol_id>/files', methods=['POST'])
def create_file(vol_id):
    err = require_admin()
    if err: return err
    d = request.json
    fid = str(uuid.uuid4())
    conn = get_db()
    execute(conn, 'INSERT INTO volunteer_files (id,volunteer_id,name,size,type,date) VALUES (%s,%s,%s,%s,%s,%s)',
            (fid, vol_id, d.get('name',''), d.get('size',''), d.get('type',''), date.today().isoformat()))
    conn.commit()
    row = fetchone(conn, 'SELECT * FROM volunteer_files WHERE id=%s', (fid,))
    conn.close()
    return jsonify(row)

# ─────────────────────────────────────────────
#  WAIVER TYPES
# ─────────────────────────────────────────────

@app.route('/api/waiver-types')
def get_waiver_types():
    err = require_auth()
    if err: return err
    conn = get_db()
    types = fetchall(conn, 'SELECT * FROM waiver_types ORDER BY name')
    conn.close()
    return jsonify(types)

@app.route('/api/waiver-types', methods=['POST'])
def create_waiver_type():
    err = require_admin()
    if err: return err
    d = request.json
    if not (d.get('name') or '').strip(): return jsonify({'error': 'Name is required'}), 400
    tid = str(uuid.uuid4())
    conn = get_db()
    try:
        execute(conn, '''INSERT INTO waiver_types (id,name,description,template_body,can_sign_online)
            VALUES (%s,%s,%s,%s,%s)''',
            (tid, (d.get('name') or '').strip(), d.get('description',''),
             d.get('template_body',''), bool(d.get('can_sign_online',False))))
        conn.commit()
    except psycopg2.IntegrityError:
        conn.rollback(); conn.close()
        return jsonify({'error': 'Waiver type already exists'}), 400
    row = fetchone(conn, 'SELECT * FROM waiver_types WHERE id=%s', (tid,))
    conn.close()
    return jsonify(row)

@app.route('/api/waiver-types/<tid>', methods=['PUT'])
def update_waiver_type(tid):
    err = require_admin()
    if err: return err
    d = request.json
    conn = get_db()
    execute(conn, '''UPDATE waiver_types SET name=%s, description=%s, template_body=%s,
        can_sign_online=%s WHERE id=%s''',
        (d.get('name',''), d.get('description',''), d.get('template_body',''),
         bool(d.get('can_sign_online',False)), tid))
    conn.commit()
    row = fetchone(conn, 'SELECT * FROM waiver_types WHERE id=%s', (tid,))
    conn.close()
    return jsonify(row)

@app.route('/api/waiver-types/<tid>', methods=['DELETE'])
def delete_waiver_type(tid):
    err = require_admin()
    if err: return err
    conn = get_db()
    execute(conn, 'DELETE FROM waiver_types WHERE id=%s', (tid,))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/waiver-types/<tid>/public')
def get_waiver_type_public(tid):
    conn = get_db()
    row = fetchone(conn, 'SELECT id,name,description,template_body FROM waiver_types WHERE id=%s', (tid,))
    conn.close()
    if not row: return jsonify({'error': 'Not found'}), 404
    return jsonify(row)

# ─────────────────────────────────────────────
#  VOLUNTEER WAIVERS
# ─────────────────────────────────────────────

@app.route('/api/volunteers/<vol_id>/waivers', methods=['POST'])
def upload_waiver(vol_id):
    err = require_admin()
    if err: return err
    waiver_type_id = request.form.get('waiver_type_id')
    signed_date    = request.form.get('signed_date')
    expiry_date    = request.form.get('expiry_date') or None
    signed_name    = request.form.get('signed_name') or None
    signed_via     = request.form.get('signed_via', 'upload')
    ec_name        = request.form.get('emergency_contact_name') or None
    ec_phone       = request.form.get('emergency_contact_phone') or None
    ec_rel         = request.form.get('emergency_contact_relationship') or None
    if not waiver_type_id or not signed_date:
        return jsonify({'error': 'Waiver type and signed date are required'}), 400
    filename = original_name = file_size = None
    if 'file' in request.files:
        f = request.files['file']
        if f and f.filename:
            ext = os.path.splitext(secure_filename(f.filename))[1].lower()
            if ext not in ['.pdf','.jpg','.jpeg','.png','.doc','.docx']:
                return jsonify({'error': 'Invalid file type'}), 400
            filename = str(uuid.uuid4()) + ext
            original_name = f.filename
            f.save(os.path.join(UPLOAD_FOLDER, filename))
            size_bytes = os.path.getsize(os.path.join(UPLOAD_FOLDER, filename))
            file_size = f'{size_bytes//1024} KB' if size_bytes >= 1024 else f'{size_bytes} B'
    wid = str(uuid.uuid4())
    conn = get_db()
    execute(conn,
        'INSERT INTO volunteer_waivers (id,volunteer_id,waiver_type_id,signed_date,expiry_date,filename,original_name,file_size,signed_name,signed_via,uploaded_by,emergency_contact_name,emergency_contact_phone,emergency_contact_relationship) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
        (wid, vol_id, waiver_type_id, signed_date, expiry_date, filename, original_name, file_size, signed_name, signed_via, session['user_name'], ec_name, ec_phone, ec_rel))
    conn.commit()
    row = fetchone(conn,
        'SELECT vw.*, wt.name as type_name FROM volunteer_waivers vw JOIN waiver_types wt ON vw.waiver_type_id=wt.id WHERE vw.id=%s', (wid,))
    conn.close()
    return jsonify(row)

@app.route('/api/sign-waiver', methods=['POST'])
def sign_waiver_online():
    d = request.json
    vol_id         = d.get('volunteer_id')
    waiver_type_id = d.get('waiver_type_id')
    signed_name    = (d.get('signed_name') or '').strip()
    if not vol_id or not waiver_type_id or not signed_name:
        return jsonify({'error': 'volunteer_id, waiver_type_id, and signed_name are required'}), 400
    today = date.today().isoformat()
    exp = date(date.today().year + 1, date.today().month, date.today().day).isoformat()
    wid = str(uuid.uuid4())
    conn = get_db()
    execute(conn,
        'INSERT INTO volunteer_waivers (id,volunteer_id,waiver_type_id,signed_date,expiry_date,signed_name,signed_via,uploaded_by) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)',
        (wid, vol_id, waiver_type_id, today, exp, signed_name, 'online', 'Self-signed'))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/waivers/<wid>/download')
def download_waiver(wid):
    err = require_auth()
    if err: return err
    conn = get_db()
    w = fetchone(conn, 'SELECT * FROM volunteer_waivers WHERE id=%s', (wid,))
    conn.close()
    if not w or not w['filename']: return jsonify({'error': 'No file attached'}), 404
    filepath = os.path.join(UPLOAD_FOLDER, w['filename'])
    if not os.path.exists(filepath): return jsonify({'error': 'File not found on disk'}), 404
    return send_file(filepath, as_attachment=True, download_name=w['original_name'] or w['filename'])

@app.route('/api/waivers/<wid>', methods=['DELETE'])
def delete_waiver_record(wid):
    err = require_admin()
    if err: return err
    conn = get_db()
    w = fetchone(conn, 'SELECT * FROM volunteer_waivers WHERE id=%s', (wid,))
    if w and w['filename']:
        try: os.remove(os.path.join(UPLOAD_FOLDER, w['filename']))
        except: pass
    execute(conn, 'DELETE FROM volunteer_waivers WHERE id=%s', (wid,))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

# ─────────────────────────────────────────────
#  YOUTH PROGRAMS
# ─────────────────────────────────────────────

@app.route('/api/youth-participants/bulk-import', methods=['POST'])
def bulk_import_youth():
    err = require_admin()
    if err: return err
    d = request.json or {}
    rows = d.get('rows', [])
    program_id = d.get('program_id') or None
    enrolled_date = d.get('enrolled_date') or None
    if not rows:
        return jsonify({'error': 'No rows provided'}), 400

    conn = get_db()
    results = {'created': 0, 'updated': 0, 'skipped': 0, 'errors': []}

    for i, row in enumerate(rows):
        try:
            first = (row.get('first_name') or '').strip()
            last  = (row.get('last_name') or '').strip()
            if not first or not last:
                results['errors'].append(f'Row {i+1}: first_name and last_name are required')
                continue

            action  = row.get('action', 'create')  # create | update | skip
            existing_id = row.get('existing_id')

            if action == 'skip':
                results['skipped'] += 1
                continue

            dob            = (row.get('dob') or '').strip() or None
            status         = (row.get('status') or 'active').strip()
            medical_notes  = (row.get('medical_notes') or '').strip()
            allergies      = (row.get('allergies') or '').strip()
            photo_consent  = 1 if str(row.get('photo_consent','')).lower() in ('1','yes','true','y') else 0
            medical_consent= 1 if str(row.get('medical_consent','')).lower() in ('1','yes','true','y') else 0
            passphrase     = (row.get('passphrase') or '').strip() or None

            if action == 'update' and existing_id:
                execute(conn, '''UPDATE youth_participants SET
                    first_name=%s, last_name=%s, dob=%s, status=%s,
                    medical_notes=%s, allergies=%s,
                    photo_consent=%s, medical_consent=%s
                    WHERE id=%s''',
                    (first, last, dob, status, medical_notes, allergies,
                     photo_consent, medical_consent, existing_id))
                yid = existing_id
                results['updated'] += 1
            else:
                yid = str(uuid.uuid4())
                execute(conn, '''INSERT INTO youth_participants
                    (id, first_name, last_name, dob, status,
                     medical_notes, allergies, photo_consent, medical_consent, passphrase)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                    (yid, first, last, dob, status,
                     medical_notes, allergies, photo_consent, medical_consent, passphrase))
                results['created'] += 1

            # Guardian
            guardian_name  = (row.get('guardian_name') or '').strip()
            guardian_phone = (row.get('guardian_phone') or '').strip()
            guardian_email = (row.get('guardian_email') or '').strip()
            guardian_rel   = (row.get('guardian_relationship') or 'Parent/Guardian').strip()
            if guardian_name:
                # Upsert guardian — update if exists (update action), insert if new
                existing_g = fetchone(conn, "SELECT id FROM youth_guardians WHERE youth_id=%s AND is_primary=1", (yid,))
                if existing_g:
                    execute(conn, '''UPDATE youth_guardians SET name=%s, relationship=%s, phone=%s, email=%s
                        WHERE id=%s''', (guardian_name, guardian_rel, guardian_phone, guardian_email, existing_g['id']))
                else:
                    execute(conn, '''INSERT INTO youth_guardians
                        (id, youth_id, name, relationship, phone, email, is_primary)
                        VALUES (%s,%s,%s,%s,%s,%s,1)''',
                        (str(uuid.uuid4()), yid, guardian_name, guardian_rel, guardian_phone, guardian_email))

            # Emergency contact
            ec_name  = (row.get('emergency_name') or '').strip()
            ec_phone = (row.get('emergency_phone') or '').strip()
            ec_rel   = (row.get('emergency_relationship') or '').strip()
            if ec_name and ec_phone:
                execute(conn, '''INSERT INTO youth_emergency_contacts
                    (id, youth_id, name, relationship, phone)
                    VALUES (%s,%s,%s,%s,%s)
                    ON CONFLICT DO NOTHING''',
                    (str(uuid.uuid4()), yid, ec_name, ec_rel, ec_phone))

            # Authorized pickups (pickup1 and pickup2)
            for priority, prefix in enumerate(['pickup1', 'pickup2'], 1):
                pu_name  = (row.get(f'{prefix}_name') or '').strip()
                pu_phone = (row.get(f'{prefix}_phone') or '').strip()
                pu_rel   = (row.get(f'{prefix}_relationship') or '').strip()
                if pu_name:
                    execute(conn, '''INSERT INTO youth_authorized_pickups
                        (id, youth_id, name, relationship, phone, priority)
                        VALUES (%s,%s,%s,%s,%s,%s)
                        ON CONFLICT DO NOTHING''',
                        (str(uuid.uuid4()), yid, pu_name, pu_rel, pu_phone, priority))

            # Program enrollment
            if program_id:
                try:
                    execute(conn, '''INSERT INTO youth_program_enrollments
                        (id, youth_id, program_id, enrolled_date)
                        VALUES (%s,%s,%s,%s)
                        ON CONFLICT (youth_id, program_id) DO NOTHING''',
                        (str(uuid.uuid4()), yid, program_id, enrolled_date))
                except Exception:
                    pass

        except Exception as e:
            results['errors'].append(f'Row {i+1} ({row.get("first_name","")} {row.get("last_name","")}): {str(e)}')
            try: conn.rollback()
            except Exception: pass

    conn.commit()
    conn.close()
    return jsonify(results)

@app.route('/api/youth-participants/check-duplicates', methods=['POST'])
def check_youth_duplicates():
    """Check a list of participants for duplicates against existing records."""
    err = require_auth()
    if err: return err
    d = request.json or {}
    rows = d.get('rows', [])
    conn = get_db()
    results = []
    for row in rows:
        first = (row.get('first_name') or '').strip().lower()
        last  = (row.get('last_name') or '').strip().lower()
        dob   = (row.get('dob') or '').strip()
        # Match on first+last+dob, or first+last if no dob
        if dob:
            match = fetchone(conn, '''SELECT id, first_name, last_name, dob FROM youth_participants
                WHERE LOWER(first_name)=%s AND LOWER(last_name)=%s AND dob=%s''', (first, last, dob))
        else:
            match = fetchone(conn, '''SELECT id, first_name, last_name, dob FROM youth_participants
                WHERE LOWER(first_name)=%s AND LOWER(last_name)=%s''', (first, last))
        results.append({'existing': match})
    conn.close()
    return jsonify(results)
def get_youth_programs():
    err = require_auth()
    if err: return err
    conn = get_db()
    programs = fetchall(conn, '''SELECT yp.*, v.name as default_elic_name
        FROM youth_programs yp
        LEFT JOIN elics el ON yp.default_elic_id=el.id
        LEFT JOIN volunteers v ON el.volunteer_id=v.id
        ORDER BY yp.name''')
    conn.close()
    return jsonify(programs)

@app.route('/api/youth-programs', methods=['POST'])
def create_youth_program():
    err = require_admin()
    if err: return err
    d = request.json
    if not (d.get('name') or '').strip(): return jsonify({'error': 'Name is required'}), 400
    pid = str(uuid.uuid4())
    conn = get_db()
    try:
        execute(conn, 'INSERT INTO youth_programs (id,name,description,program_type,start_date,end_date,instructor_id,default_elic_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)',
                (pid, (d.get('name') or '').strip(), d.get('description',''),
                 d.get('program_type','class'), d.get('start_date') or None,
                 d.get('end_date') or None, d.get('instructor_id') or None,
                 d.get('default_elic_id') or None))
        conn.commit()
    except psycopg2.IntegrityError:
        conn.rollback(); conn.close()
        return jsonify({'error': 'Program already exists'}), 400
    row = fetchone(conn, '''SELECT yp.*, v.name as default_elic_name FROM youth_programs yp LEFT JOIN elics el ON yp.default_elic_id=el.id LEFT JOIN volunteers v ON el.volunteer_id=v.id WHERE yp.id=%s''', (pid,))
    conn.close()
    return jsonify(row)

@app.route('/api/youth-programs/<pid>', methods=['PUT'])
def update_youth_program(pid):
    err = require_admin()
    if err: return err
    d = request.json
    if not (d.get('name') or '').strip(): return jsonify({'error': 'Name is required'}), 400
    conn = get_db()
    execute(conn, 'UPDATE youth_programs SET name=%s,description=%s,program_type=%s,start_date=%s,end_date=%s,instructor_id=%s,default_elic_id=%s WHERE id=%s',
            ((d.get('name') or '').strip(), d.get('description',''),
             d.get('program_type','class'), d.get('start_date') or None,
             d.get('end_date') or None, d.get('instructor_id') or None,
             d.get('default_elic_id') or None, pid))
    conn.commit()
    row = fetchone(conn, '''SELECT yp.*, v.name as default_elic_name FROM youth_programs yp LEFT JOIN elics el ON yp.default_elic_id=el.id LEFT JOIN volunteers v ON el.volunteer_id=v.id WHERE yp.id=%s''', (pid,))
    conn.close()
    return jsonify(row)

@app.route('/api/youth-programs/<pid>', methods=['DELETE'])
def delete_youth_program(pid):
    err = require_admin()
    if err: return err
    conn = get_db()
    # Clear any FK references that don't cascade
    execute(conn, 'UPDATE youth_sign_ins SET program_id=NULL WHERE program_id=%s', (pid,))
    execute(conn, 'UPDATE events SET program_id=NULL WHERE program_id=%s', (pid,))
    execute(conn, 'DELETE FROM youth_programs WHERE id=%s', (pid,))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

# ─────────────────────────────────────────────
#  EMAIL TEMPLATES
# ─────────────────────────────────────────────

@app.route('/api/email-templates')
def get_email_templates():
    err = require_auth()
    if err: return err
    conn = get_db()
    templates = fetchall(conn, 'SELECT * FROM email_templates ORDER BY name')
    conn.close()
    return jsonify(templates)

@app.route('/api/email-templates', methods=['POST'])
def create_email_template():
    err = require_admin()
    if err: return err
    d = request.json
    tid = str(uuid.uuid4())
    conn = get_db()
    execute(conn, 'INSERT INTO email_templates (id,name,subject,body) VALUES (%s,%s,%s,%s)',
            (tid, d.get('name',''), d.get('subject',''), d.get('body','')))
    conn.commit()
    row = fetchone(conn, 'SELECT * FROM email_templates WHERE id=%s', (tid,))
    conn.close()
    return jsonify(row)

@app.route('/api/email-templates/<tid>', methods=['DELETE'])
def delete_email_template(tid):
    err = require_admin()
    if err: return err
    conn = get_db()
    execute(conn, 'DELETE FROM email_templates WHERE id=%s', (tid,))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

# ─────────────────────────────────────────────
#  YOUTH
# ─────────────────────────────────────────────

@app.route('/api/youth')
def get_youth():
    err = require_auth()
    if err: return err
    conn = get_db()
    youth = fetchall(conn, 'SELECT * FROM youth_participants ORDER BY last_name,first_name')
    for y in youth:
        y['guardians'] = fetchall(conn, 'SELECT * FROM youth_guardians WHERE youth_id=%s ORDER BY is_primary DESC', (y['id'],))
        y['emergency_contacts'] = fetchall(conn, 'SELECT * FROM youth_emergency_contacts WHERE youth_id=%s', (y['id'],))
        y['authorized_pickups'] = fetchall(conn, 'SELECT * FROM youth_authorized_pickups WHERE youth_id=%s ORDER BY priority', (y['id'],))
        y['waivers'] = fetchall(conn,
            'SELECT yw.*, wt.name as type_name FROM youth_waivers yw JOIN waiver_types wt ON yw.waiver_type_id=wt.id WHERE yw.youth_id=%s ORDER BY yw.signed_date DESC', (y['id'],))
        y['enrollments'] = fetchall(conn,
            'SELECT e.*, p.name as program_name FROM youth_program_enrollments e JOIN youth_programs p ON e.program_id=p.id WHERE e.youth_id=%s ORDER BY e.enrolled_date DESC', (y['id'],))
    conn.close()
    return jsonify(youth)

@app.route('/api/youth/<yid>')
def get_youth_participant(yid):
    err = require_auth()
    if err: return err
    conn = get_db()
    y = fetchone(conn, 'SELECT * FROM youth_participants WHERE id=%s', (yid,))
    if not y: conn.close(); return jsonify({'error': 'Not found'}), 404
    y['guardians'] = fetchall(conn, 'SELECT * FROM youth_guardians WHERE youth_id=%s ORDER BY is_primary DESC', (yid,))
    y['emergency_contacts'] = fetchall(conn, 'SELECT * FROM youth_emergency_contacts WHERE youth_id=%s', (yid,))
    y['authorized_pickups'] = fetchall(conn, 'SELECT * FROM youth_authorized_pickups WHERE youth_id=%s ORDER BY priority', (yid,))
    y['waivers'] = fetchall(conn,
        'SELECT yw.*, wt.name as type_name FROM youth_waivers yw JOIN waiver_types wt ON yw.waiver_type_id=wt.id WHERE yw.youth_id=%s ORDER BY yw.signed_date DESC', (yid,))
    y['enrollments'] = fetchall(conn,
        'SELECT e.*, p.name as program_name FROM youth_program_enrollments e JOIN youth_programs p ON e.program_id=p.id WHERE e.youth_id=%s ORDER BY e.enrolled_date DESC', (yid,))
    conn.close()
    return jsonify(y)

@app.route('/api/youth', methods=['POST'])
def create_youth():
    err = require_admin()
    if err: return err
    d = request.json
    yid = str(uuid.uuid4())
    conn = get_db()
    execute(conn,
        'INSERT INTO youth_participants (id,first_name,last_name,dob,program,status,medical_notes,allergies,photo_consent,medical_consent) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
        (yid, d.get('first_name',''), d.get('last_name',''), d.get('dob') or None, d.get('program',''), d.get('status','active'),
         d.get('medical_notes',''), d.get('allergies',''), 1 if d.get('photo_consent') else 0, 1 if d.get('medical_consent') else 0))
    for g in d.get('guardians', []):
        execute(conn, 'INSERT INTO youth_guardians (id,youth_id,name,relationship,phone,email,is_primary) VALUES (%s,%s,%s,%s,%s,%s,%s)',
                (str(uuid.uuid4()), yid, g['name'], g.get('relationship',''), g.get('phone',''), g.get('email',''), 1 if g.get('is_primary') else 0))
    if d.get('emergency_name') and d.get('emergency_phone'):
        execute(conn, 'INSERT INTO youth_emergency_contacts (id,youth_id,name,relationship,phone) VALUES (%s,%s,%s,%s,%s)',
                (str(uuid.uuid4()), yid, d.get('emergency_name',''), d.get('emergency_relationship',''), d.get('emergency_phone','')))
    conn.commit()
    y = fetchone(conn, 'SELECT * FROM youth_participants WHERE id=%s', (yid,))
    y['guardians'] = fetchall(conn, 'SELECT * FROM youth_guardians WHERE youth_id=%s', (yid,))
    y['emergency_contacts'] = fetchall(conn, 'SELECT * FROM youth_emergency_contacts WHERE youth_id=%s', (yid,))
    y['waivers'] = []
    conn.close()
    return jsonify(y)

@app.route('/api/youth/<yid>', methods=['PUT'])
def update_youth(yid):
    err = require_admin()
    if err: return err
    d = request.json
    conn = get_db()
    execute(conn,
        'UPDATE youth_participants SET first_name=%s,last_name=%s,dob=%s,program=%s,status=%s,medical_notes=%s,allergies=%s,photo_consent=%s,medical_consent=%s WHERE id=%s',
        (d.get('first_name',''), d.get('last_name',''), d.get('dob') or None, d.get('program',''), d.get('status','active'),
         d.get('medical_notes',''), d.get('allergies',''), 1 if d.get('photo_consent') else 0, 1 if d.get('medical_consent') else 0, yid))
    conn.commit()
    y = fetchone(conn, 'SELECT * FROM youth_participants WHERE id=%s', (yid,))
    conn.close()
    return jsonify(y)

@app.route('/api/youth/<yid>', methods=['DELETE'])
def delete_youth(yid):
    err = require_admin()
    if err: return err
    conn = get_db()
    execute(conn, 'DELETE FROM youth_participants WHERE id=%s', (yid,))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/youth/<yid>/guardians', methods=['POST'])
def add_guardian(yid):
    err = require_admin()
    if err: return err
    d = request.json
    gid = str(uuid.uuid4())
    conn = get_db()
    execute(conn, 'INSERT INTO youth_guardians (id,youth_id,name,relationship,phone,email,is_primary) VALUES (%s,%s,%s,%s,%s,%s,%s)',
            (gid, yid, d.get('name',''), d.get('relationship',''), d.get('phone',''), d.get('email',''), 1 if d.get('is_primary') else 0))
    conn.commit()
    row = fetchone(conn, 'SELECT * FROM youth_guardians WHERE id=%s', (gid,))
    conn.close()
    return jsonify(row)

@app.route('/api/youth/guardians/<gid>', methods=['DELETE'])
def delete_guardian(gid):
    err = require_admin()
    if err: return err
    conn = get_db()
    execute(conn, 'DELETE FROM youth_guardians WHERE id=%s', (gid,))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/youth/<yid>/emergency-contacts', methods=['POST'])
def add_emergency_contact(yid):
    err = require_admin()
    if err: return err
    d = request.json
    eid = str(uuid.uuid4())
    conn = get_db()
    execute(conn, 'INSERT INTO youth_emergency_contacts (id,youth_id,name,relationship,phone) VALUES (%s,%s,%s,%s,%s)',
            (eid, yid, d.get('name',''), d.get('relationship',''), d['phone']))
    conn.commit()
    row = fetchone(conn, 'SELECT * FROM youth_emergency_contacts WHERE id=%s', (eid,))
    conn.close()
    return jsonify(row)

@app.route('/api/youth/<yid>/waivers', methods=['POST'])
def add_youth_waiver(yid):
    err = require_admin()
    if err: return err
    waiver_type_id = request.form.get('waiver_type_id')
    signed_date    = request.form.get('signed_date')
    expiry_date    = request.form.get('expiry_date') or None
    signed_by      = request.form.get('signed_by') or None
    if not waiver_type_id or not signed_date:
        return jsonify({'error': 'Waiver type and signed date are required'}), 400
    filename = original_name = None
    if 'file' in request.files:
        f = request.files['file']
        if f and f.filename:
            ext = os.path.splitext(secure_filename(f.filename))[1].lower()
            filename = str(uuid.uuid4()) + ext
            original_name = f.filename
            f.save(os.path.join(UPLOAD_FOLDER, filename))
    wid = str(uuid.uuid4())
    conn = get_db()
    execute(conn, 'INSERT INTO youth_waivers (id,youth_id,waiver_type_id,signed_date,expiry_date,signed_by,filename,original_name) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)',
            (wid, yid, waiver_type_id, signed_date, expiry_date, signed_by, filename, original_name))
    conn.commit()
    row = fetchone(conn,
        'SELECT yw.*, wt.name as type_name FROM youth_waivers yw JOIN waiver_types wt ON yw.waiver_type_id=wt.id WHERE yw.id=%s', (wid,))
    conn.close()
    return jsonify(row)

# ─────────────────────────────────────────────
#  DASHBOARD
# ─────────────────────────────────────────────

@app.route('/api/dashboard')
def dashboard():
    err = require_auth()
    if err: return err
    conn = get_db()
    total_vols  = fetchone(conn, 'SELECT COUNT(*) as c FROM volunteers')['c']
    total_hours = fetchone(conn, 'SELECT COALESCE(SUM(hours),0) as s FROM hours')['s']
    total_youth = fetchone(conn, "SELECT COUNT(*) as c FROM youth_participants WHERE status='active'")['c']
    today = date.today()
    all_waivers = fetchall(conn,
        'SELECT vw.volunteer_id, vw.expiry_date, v.name FROM volunteer_waivers vw JOIN volunteers v ON vw.volunteer_id=v.id')
    vol_worst = {}; vol_names = {}
    for w in all_waivers:
        vid = w['volunteer_id']; vol_names[vid] = w['name']
        if not w['expiry_date']:
            if vid not in vol_worst: vol_worst[vid] = 'valid'
            continue
        diff = (datetime.strptime(w['expiry_date'], '%Y-%m-%d').date() - today).days
        prev = vol_worst.get(vid, 'none')
        if diff < 0: vol_worst[vid] = 'expired'
        elif diff < 30 and prev != 'expired': vol_worst[vid] = 'expiring'
        elif prev == 'none': vol_worst[vid] = 'valid'
    expiring = sum(1 for s in vol_worst.values() if s == 'expiring')
    expired  = sum(1 for s in vol_worst.values() if s == 'expired')
    alerts = []
    for vid, status in vol_worst.items():
        if status == 'expiring': alerts.append({'type':'warning','name':vol_names[vid],'msg':'waiver expiring soon','id':vid})
        if status == 'expired':  alerts.append({'type':'danger', 'name':vol_names[vid],'msg':'waiver has expired','id':vid})
    monthly = []
    for i in range(5, -1, -1):
        m = today.month - i; y = today.year + (m - 1) // 12; m = ((m - 1) % 12) + 1
        label = datetime(y, m, 1).strftime('%b')
        total = fetchone(conn,
            "SELECT COALESCE(SUM(hours),0) as s FROM hours WHERE TO_CHAR(TO_DATE(date,'YYYY-MM-DD'),'YYYY-MM')=%s",
            (f'{y:04d}-{m:02d}',))['s']
        monthly.append({'label': label, 'total': float(total)})
    top = fetchall(conn, '''
        SELECT v.id, v.name, COALESCE(SUM(h.hours),0) as total_hours, COUNT(DISTINCT h.event) as total_events
        FROM volunteers v LEFT JOIN hours h ON v.id=h.volunteer_id
        GROUP BY v.id, v.name ORDER BY total_hours DESC LIMIT 5
    ''')
    conn.close()
    return jsonify({'total_volunteers': total_vols, 'total_hours': float(total_hours), 'total_youth': total_youth,
                    'expiring_waivers': expiring, 'expired_waivers': expired,
                    'alerts': alerts, 'monthly_hours': monthly, 'top_volunteers': top})

# ─────────────────────────────────────────────
#  USERS
# ─────────────────────────────────────────────

@app.route('/api/users', methods=['POST'])
def create_user():
    err = require_admin()
    if err: return err
    d = request.json
    if not d.get('name') or not d.get('email') or not d.get('password'):
        return jsonify({'error': 'Name, email, and password are required'}), 400
    pw_hash = hashlib.sha256(d.get('password','').encode()).hexdigest()
    uid_ = str(uuid.uuid4())
    conn = get_db()
    try:
        execute(conn, 'INSERT INTO users (id,name,email,password_hash,role,role_permissions) VALUES (%s,%s,%s,%s,%s,%s)',
                (uid_, d.get('name',''), d.get('email',''), pw_hash, d.get('role','staff'), '{}'))
        conn.commit()
    except psycopg2.IntegrityError:
        conn.rollback(); conn.close()
        return jsonify({'error': 'Email already exists'}), 400
    conn.close()
    return jsonify({'ok': True})


# ─────────────────────────────────────────────
#  PRODUCTIONS
# ─────────────────────────────────────────────

@app.route('/api/productions')
def get_productions():
    err = require_auth()
    if err: return err
    conn = get_db()
    prods = fetchall(conn, """SELECT p.*, COALESCE(p.stage,'mainstage') as stage,
        v.name as default_elic_name
        FROM productions p
        LEFT JOIN elics el ON p.default_elic_id=el.id
        LEFT JOIN volunteers v ON el.volunteer_id=v.id
        ORDER BY p.start_date DESC NULLS LAST""")
    for p in prods:
        p['members'] = fetchall(conn, '''
            SELECT pm.*, v.name as volunteer_name, v.email as volunteer_email
            FROM production_members pm
            JOIN volunteers v ON pm.volunteer_id=v.id
            WHERE pm.production_id=%s ORDER BY pm.role''', (p['id'],))
        p['required_waivers'] = fetchall(conn,
            'SELECT pw.*, wt.name as waiver_name FROM production_waivers pw JOIN waiver_types wt ON pw.waiver_type_id=wt.id WHERE pw.production_id=%s', (p['id'],))
    conn.close()
    return jsonify(prods)

@app.route('/api/productions', methods=['POST'])
def create_production():
    err = require_admin()
    if err: return err
    d = request.json
    pid = str(uuid.uuid4())
    conn = get_db()
    execute(conn, 'INSERT INTO productions (id,name,production_type,stage,start_date,end_date,description,status,default_elic_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)',
            (pid, d.get('name',''), d.get('production_type','show'), d.get('stage','mainstage'),
             d.get('start_date') or None, d.get('end_date') or None,
             d.get('description',''), d.get('status','upcoming'),
             d.get('default_elic_id') or None))
    conn.commit()
    prod = fetchone(conn, '''SELECT p.*, COALESCE(p.stage,'mainstage') as stage, v.name as default_elic_name FROM productions p LEFT JOIN elics el ON p.default_elic_id=el.id LEFT JOIN volunteers v ON el.volunteer_id=v.id WHERE p.id=%s''', (pid,))
    prod['members'] = []
    conn.close()
    return jsonify(prod)

@app.route('/api/productions/<pid>', methods=['PUT'])
def update_production(pid):
    err = require_admin()
    if err: return err
    d = request.json
    conn = get_db()
    execute(conn, 'UPDATE productions SET name=%s,production_type=%s,stage=%s,start_date=%s,end_date=%s,description=%s,status=%s,default_elic_id=%s,image_url=%s WHERE id=%s',
            (d.get('name',''), d.get('production_type','show'), d.get('stage','mainstage'),
             d.get('start_date') or None, d.get('end_date') or None,
             d.get('description',''), d.get('status','upcoming'),
             d.get('default_elic_id') or None,
             d.get('image_url') or None, pid))
    conn.commit()
    prod = fetchone(conn, '''SELECT p.*, COALESCE(p.stage,'mainstage') as stage, v.name as default_elic_name FROM productions p LEFT JOIN elics el ON p.default_elic_id=el.id LEFT JOIN volunteers v ON el.volunteer_id=v.id WHERE p.id=%s''', (pid,))
    prod['members'] = fetchall(conn, '''
        SELECT pm.*, v.name as volunteer_name, v.email as volunteer_email
        FROM production_members pm JOIN volunteers v ON pm.volunteer_id=v.id
        WHERE pm.production_id=%s ORDER BY pm.role''', (pid,))
    conn.close()
    return jsonify(prod)

@app.route('/api/productions/<pid>', methods=['DELETE'])
def delete_production(pid):
    err = require_admin()
    if err: return err
    conn = get_db()
    execute(conn, 'DELETE FROM productions WHERE id=%s', (pid,))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/productions/<pid>/members', methods=['POST'])
def add_production_member(pid):
    err = require_admin()
    if err: return err
    d = request.json
    mid = str(uuid.uuid4())
    conn = get_db()
    try:
        execute(conn, 'INSERT INTO production_members (id,production_id,volunteer_id,role,department,status,notes) VALUES (%s,%s,%s,%s,%s,%s,%s)',
                (mid, pid, d.get('volunteer_id'), d.get('role',''), d.get('department',''), d.get('status','confirmed'), d.get('notes','')))
        conn.commit()
    except psycopg2.IntegrityError:
        conn.rollback(); conn.close()
        return jsonify({'error': 'This volunteer is already in this production'}), 400
    row = fetchone(conn, '''SELECT pm.*, v.name as volunteer_name, v.email as volunteer_email
        FROM production_members pm JOIN volunteers v ON pm.volunteer_id=v.id WHERE pm.id=%s''', (mid,))
    conn.close()
    return jsonify(row)

@app.route('/api/productions/members/<mid>', methods=['PUT'])
@app.route('/api/productions/<pid>/team-member/<mid>', methods=['PUT'])
def update_production_member(mid, pid=None):
    err = require_auth()
    if err: return err
    d = request.json or {}
    conn = get_db()
    execute(conn, '''UPDATE production_members SET
        role=%s, department=%s, status=%s, notes=%s,
        bio=%s, photo_url=%s WHERE id=%s''',
        (d.get('role',''), d.get('department',''),
         d.get('status','confirmed'), d.get('notes',''),
         d.get('bio',''), d.get('photo_url',''), mid))
    conn.commit()
    row = fetchone(conn, '''SELECT pm.*, v.name as volunteer_name, v.email as volunteer_email
        FROM production_members pm JOIN volunteers v ON pm.volunteer_id=v.id WHERE pm.id=%s''', (mid,))
    conn.close()
    return jsonify(row)

@app.route('/api/productions/members/<mid>', methods=['DELETE'])
def remove_production_member(mid):
    err = require_admin()
    if err: return err
    conn = get_db()
    execute(conn, 'DELETE FROM production_members WHERE id=%s', (mid,))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

# ─────────────────────────────────────────────
#  YOUTH PROGRAM ENROLLMENTS
# ─────────────────────────────────────────────

@app.route('/api/youth/<yid>/enrollments', methods=['POST'])
def enroll_youth(yid):
    err = require_admin()
    if err: return err
    d = request.json
    eid = str(uuid.uuid4())
    conn = get_db()
    try:
        execute(conn, 'INSERT INTO youth_program_enrollments (id,youth_id,program_id,enrolled_date,notes) VALUES (%s,%s,%s,%s,%s)',
                (eid, yid, d['program_id'], d.get('enrolled_date') or date.today().isoformat(), d.get('notes','')))
        conn.commit()
    except psycopg2.IntegrityError:
        conn.rollback(); conn.close()
        return jsonify({'error': 'Already enrolled in this program'}), 400
    row = fetchone(conn, '''SELECT e.*, p.name as program_name FROM youth_program_enrollments e
        JOIN youth_programs p ON e.program_id=p.id WHERE e.id=%s''', (eid,))
    conn.close()
    return jsonify(row)

@app.route('/api/youth/enrollments/<eid>', methods=['DELETE'])
def unenroll_youth(eid):
    err = require_admin()
    if err: return err
    conn = get_db()
    execute(conn, 'DELETE FROM youth_program_enrollments WHERE id=%s', (eid,))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

# ─────────────────────────────────────────────
#  USER MANAGEMENT
# ─────────────────────────────────────────────

@app.route('/api/users')
def get_users():
    err = require_admin()
    if err: return err
    conn = get_db()
    users = fetchall(conn, 'SELECT id,name,email,role,role_permissions,COALESCE(active,TRUE) as active FROM users ORDER BY name')
    conn.close()
    return jsonify(users)

@app.route('/api/users/<uid>/toggle', methods=['POST'])
def toggle_user(uid):
    err = require_admin()
    if err: return err
    if uid == session['user_id']:
        return jsonify({'error': 'Cannot deactivate your own account'}), 400
    conn = get_db()
    user = fetchone(conn, 'SELECT COALESCE(active,TRUE) as active FROM users WHERE id=%s', (uid,))
    if not user: conn.close(); return jsonify({'error': 'Not found'}), 404
    new_active = not user['active']
    execute(conn, 'UPDATE users SET active=%s WHERE id=%s', (new_active, uid))
    conn.commit(); conn.close()
    return jsonify({'ok': True, 'active': new_active})

# ─────────────────────────────────────────────
#  DONOR & SPONSOR MANAGEMENT
# ─────────────────────────────────────────────

def get_cumulative_benefits(conn, tier_id):
    """Return all benefits for a tier including all benefits from lower tiers (cumulative)."""
    if not tier_id: return []
    tier = fetchone(conn, 'SELECT min_amount FROM donor_tiers WHERE id=%s', (tier_id,))
    if not tier: return []
    min_amount = tier['min_amount'] or 0
    return fetchall(conn, '''
        SELECT b.*, t.name as tier_name, t.min_amount
        FROM donor_tier_benefits b
        JOIN donor_tiers t ON b.tier_id=t.id
        WHERE t.min_amount <= %s
        ORDER BY t.min_amount ASC, b.sort_order ASC, b.name ASC
    ''', (min_amount,))

# ── Tiers ──
@app.route('/api/donor-tiers')
def get_donor_tiers():
    err = require_auth()
    if err: return err
    conn = get_db()
    tiers = fetchall(conn, '''
        SELECT t.*, COUNT(b.id) as benefit_count
        FROM donor_tiers t
        LEFT JOIN donor_tier_benefits b ON b.tier_id=t.id
        GROUP BY t.id ORDER BY t.min_amount ASC''')
    for tier in tiers:
        tier['own_benefits'] = fetchall(conn, 'SELECT * FROM donor_tier_benefits WHERE tier_id=%s ORDER BY sort_order,name', (tier['id'],))
        tier['benefits'] = get_cumulative_benefits(conn, tier['id'])
    conn.close()
    return jsonify(tiers)

@app.route('/api/donor-tiers', methods=['POST'])
def create_donor_tier():
    err = require_auth()
    if err: return err
    d = request.json
    tid = str(uuid.uuid4())
    conn = get_db()
    execute(conn, '''INSERT INTO donor_tiers (id,name,min_amount,max_amount,color,description,sort_order)
        VALUES (%s,%s,%s,%s,%s,%s,%s)''',
        (tid, d.get('name',''), d.get('min_amount',0), d.get('max_amount') or None,
         d.get('color','teal'), d.get('description',''), d.get('sort_order',0)))
    conn.commit()
    row = fetchone(conn, 'SELECT * FROM donor_tiers WHERE id=%s', (tid,))
    row['benefits'] = []
    conn.close()
    return jsonify(row)

@app.route('/api/donor-tiers/<tid>', methods=['PUT'])
def update_donor_tier(tid):
    err = require_auth()
    if err: return err
    d = request.json
    conn = get_db()
    execute(conn, 'UPDATE donor_tiers SET name=%s,min_amount=%s,max_amount=%s,color=%s,description=%s,sort_order=%s WHERE id=%s',
        (d.get('name',''), d.get('min_amount',0), d.get('max_amount') or None,
         d.get('color','teal'), d.get('description',''), d.get('sort_order',0), tid))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/donor-tiers/<tid>', methods=['DELETE'])
def delete_donor_tier(tid):
    err = require_auth()
    if err: return err
    conn = get_db()
    execute(conn, 'DELETE FROM donor_tiers WHERE id=%s', (tid,))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

# ── Benefits ──
@app.route('/api/donor-tiers/<tid>/benefits', methods=['POST'])
def add_tier_benefit(tid):
    err = require_auth()
    if err: return err
    d = request.json
    bid = str(uuid.uuid4())
    conn = get_db()
    execute(conn, '''INSERT INTO donor_tier_benefits (id,tier_id,name,description,is_trackable,sort_order)
        VALUES (%s,%s,%s,%s,%s,%s)''',
        (bid, tid, d.get('name',''), d.get('description',''), d.get('is_trackable',True), d.get('sort_order',0)))
    conn.commit()
    row = fetchone(conn, 'SELECT * FROM donor_tier_benefits WHERE id=%s', (bid,))
    conn.close()
    return jsonify(row)

@app.route('/api/donor-benefits/<bid>', methods=['PUT'])
def update_tier_benefit(bid):
    err = require_auth()
    if err: return err
    d = request.json
    conn = get_db()
    execute(conn, 'UPDATE donor_tier_benefits SET name=%s,description=%s,is_trackable=%s,sort_order=%s WHERE id=%s',
        (d.get('name',''), d.get('description',''), d.get('is_trackable',True), d.get('sort_order',0), bid))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/donor-benefits/<bid>', methods=['DELETE'])
def delete_tier_benefit(bid):
    err = require_auth()
    if err: return err
    conn = get_db()
    execute(conn, 'DELETE FROM donor_tier_benefits WHERE id=%s', (bid,))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

# ── Campaigns ──
@app.route('/api/donor-campaigns')
def get_donor_campaigns():
    err = require_auth()
    if err: return err
    conn = get_db()
    campaigns = fetchall(conn, '''
        SELECT c.*, COALESCE(SUM(d.amount),0) as raised
        FROM donor_campaigns c
        LEFT JOIN donor_donations d ON d.campaign_id=c.id AND d.payment_status='received'
        GROUP BY c.id ORDER BY c.created_at DESC''')
    for c in campaigns:
        try:
            c['benefits'] = fetchall(conn, '''SELECT * FROM campaign_benefits
                WHERE campaign_id=%s ORDER BY min_amount ASC, sort_order ASC, name ASC''', (c['id'],))
        except Exception:
            c['benefits'] = []
    conn.close()
    return jsonify(campaigns)

@app.route('/api/donor-campaigns/<cid>/benefits')
def get_campaign_benefits(cid):
    err = require_auth()
    if err: return err
    conn = get_db()
    rows = fetchall(conn, 'SELECT * FROM campaign_benefits WHERE campaign_id=%s ORDER BY min_amount ASC, sort_order ASC', (cid,))
    conn.close()
    return jsonify(rows)

@app.route('/api/donor-campaigns/<cid>/benefits', methods=['POST'])
def create_campaign_benefit(cid):
    err = require_auth()
    if err: return err
    d = request.json or {}
    if not (d.get('name') or '').strip(): return jsonify({'error': 'Name is required'}), 400
    bid = str(uuid.uuid4())
    conn = get_db()
    execute(conn, '''INSERT INTO campaign_benefits (id,campaign_id,name,description,min_amount,is_trackable,sort_order)
        VALUES (%s,%s,%s,%s,%s,%s,%s)''',
        (bid, cid, (d.get('name') or '').strip(), (d.get('description') or '').strip(),
         float(d.get('min_amount') or 0), bool(d.get('is_trackable',False)),
         int(d.get('sort_order',0))))
    conn.commit()
    row = fetchone(conn, 'SELECT * FROM campaign_benefits WHERE id=%s', (bid,))
    conn.close()
    return jsonify(row)

@app.route('/api/donor-campaigns/benefits/<bid>', methods=['PUT'])
def update_campaign_benefit(bid):
    err = require_auth()
    if err: return err
    d = request.json or {}
    conn = get_db()
    execute(conn, '''UPDATE campaign_benefits SET name=%s, description=%s, min_amount=%s, is_trackable=%s
        WHERE id=%s''',
        ((d.get('name') or '').strip(), (d.get('description') or '').strip(),
         float(d.get('min_amount') or 0), bool(d.get('is_trackable',False)), bid))
    conn.commit()
    row = fetchone(conn, 'SELECT * FROM campaign_benefits WHERE id=%s', (bid,))
    conn.close()
    return jsonify(row or {'ok': True})

@app.route('/api/donor-campaigns/benefits/<bid>', methods=['DELETE'])
def delete_campaign_benefit(bid):
    err = require_auth()
    if err: return err
    conn = get_db()
    execute(conn, 'DELETE FROM campaign_benefits WHERE id=%s', (bid,))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/donor-campaigns', methods=['POST'])
def create_donor_campaign():
    err = require_auth()
    if err: return err
    d = request.json
    cid = str(uuid.uuid4())
    conn = get_db()
    execute(conn, '''INSERT INTO donor_campaigns (id,name,description,goal_amount,start_date,end_date,status)
        VALUES (%s,%s,%s,%s,%s,%s,%s)''',
        (cid, d.get('name',''), d.get('description',''), d.get('goal_amount') or None,
         d.get('start_date') or None, d.get('end_date') or None, d.get('status','active')))
    conn.commit()
    row = fetchone(conn, 'SELECT * FROM donor_campaigns WHERE id=%s', (cid,))
    conn.close()
    return jsonify(row)

@app.route('/api/donor-campaigns/<cid>', methods=['PUT'])
def update_donor_campaign(cid):
    err = require_auth()
    if err: return err
    d = request.json
    conn = get_db()
    execute(conn, 'UPDATE donor_campaigns SET name=%s,description=%s,goal_amount=%s,start_date=%s,end_date=%s,status=%s WHERE id=%s',
        (d.get('name',''), d.get('description',''), d.get('goal_amount') or None,
         d.get('start_date') or None, d.get('end_date') or None, d.get('status','active'), cid))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/donor-campaigns/<cid>', methods=['DELETE'])
def delete_donor_campaign(cid):
    err = require_auth()
    if err: return err
    conn = get_db()
    execute(conn, 'DELETE FROM donor_campaigns WHERE id=%s', (cid,))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

# ── Donors — static routes MUST come before <did> dynamic routes ──
@app.route('/api/donors')
def get_donors():
    err = require_auth()
    if err: return err
    conn = get_db()
    donors = fetchall(conn, '''
        SELECT dn.*, t.name as tier_name, t.color as tier_color,
               v.name as volunteer_name
        FROM donors dn
        LEFT JOIN donor_tiers t ON dn.tier_id=t.id
        LEFT JOIN volunteers v ON dn.volunteer_id=v.id
        ORDER BY dn.total_donated DESC, dn.display_name ASC''')
    conn.close()
    return jsonify(donors)

@app.route('/api/donors', methods=['POST'])
def create_donor():
    err = require_auth()
    if err: return err
    d = request.json
    did = str(uuid.uuid4())
    conn = get_db()
    execute(conn, '''INSERT INTO donors
        (id,type,display_name,legal_name,email,phone,address,website,
         volunteer_id,is_anonymous,recognition_name,notes,internal_rating,status,created_by)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'active',%s)''',
        (did, d.get('type','individual'), d.get('display_name',''), d.get('legal_name',''),
         d.get('email',''), d.get('phone',''), d.get('address',''), d.get('website',''),
         d.get('volunteer_id') or None, d.get('is_anonymous',False),
         d.get('recognition_name',''), d.get('notes',''),
         d.get('internal_rating','normal'), session.get('user_name','')))
    conn.commit()
    row = fetchone(conn, '''SELECT dn.*, t.name as tier_name, t.color as tier_color
        FROM donors dn LEFT JOIN donor_tiers t ON dn.tier_id=t.id WHERE dn.id=%s''', (did,))
    conn.close()
    return jsonify(row)

@app.route('/api/donors/summary')
def donor_summary():
    err = require_auth()
    if err: return err
    conn = get_db()
    from datetime import datetime
    year = datetime.now().year
    total_row = fetchone(conn, '''SELECT COALESCE(SUM(amount),0) as total
        FROM donor_donations WHERE payment_status='received'
        AND EXTRACT(YEAR FROM donation_date::date)=%s''', (year,))
    donor_count = fetchone(conn, "SELECT COUNT(*) as c FROM donors WHERE status='active'")
    new_this_year = fetchone(conn, '''SELECT COUNT(*) as c FROM donors
        WHERE EXTRACT(YEAR FROM created_at)=%s''', (year,))
    tier_breakdown = fetchall(conn, '''SELECT t.name, t.color, COUNT(dn.id) as count
        FROM donor_tiers t LEFT JOIN donors dn ON dn.tier_id=t.id AND dn.status='active'
        GROUP BY t.id,t.name,t.color ORDER BY t.min_amount DESC''')
    lapsed = fetchone(conn, '''SELECT COUNT(*) as c FROM donors
        WHERE status='active' AND last_donation_date IS NOT NULL
        AND last_donation_date::date < CURRENT_DATE - INTERVAL \'12 months\' ''')
    conn.close()
    return jsonify({
        'total_raised_this_year': float(total_row['total']),
        'active_donors': donor_count['c'],
        'new_this_year': new_this_year['c'],
        'lapsed_count': lapsed['c'] if lapsed else 0,
        'tier_breakdown': tier_breakdown
    })

@app.route('/api/donors/import', methods=['POST'])
@app.route('/api/donations/all')
def get_all_donations():
    """Return all donations with donor name, for bulk editing."""
    err = require_auth()
    if err: return err
    conn = get_db()
    rows = fetchall(conn, """
        SELECT dd.*, dn.display_name as donor_name, c.name as campaign_name
        FROM donor_donations dd
        JOIN donors dn ON dd.donor_id = dn.id
        LEFT JOIN donor_campaigns c ON dd.campaign_id = c.id
        ORDER BY dd.donation_date ASC NULLS LAST, dn.display_name ASC
    """)
    conn.close()
    return jsonify(rows)

@app.route('/api/donations/bulk-update', methods=['POST'])
def bulk_update_donations():
    """Update multiple donations at once."""
    err = require_auth()
    if err: return err
    updates = request.json.get('updates', [])
    if not updates:
        return jsonify({'error': 'No updates provided'}), 400
    conn = get_db()
    updated = 0
    errors = []
    affected_donors = set()
    for u in updates:
        did = u.get('id')
        if not did:
            continue
        try:
            execute(conn, """UPDATE donor_donations SET
                amount=%s, donation_date=%s, type=%s,
                payment_status=%s, campaign_id=%s,
                check_number=%s, notes=%s WHERE id=%s""",
                (u.get('amount'), u.get('donation_date'),
                 u.get('type','cash'), u.get('payment_status','received'),
                 u.get('campaign_id') or None,
                 u.get('check_number',''), u.get('notes',''), did))
            affected_donors.add(u['donor_id'])
            updated += 1
        except Exception as e:
            errors.append(str(e)[:60])
    conn.commit()
    for donor_id in affected_donors:
        try:
            recalc_donor_totals(conn, donor_id)
        except Exception:
            pass
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'updated': updated, 'errors': errors[:5]})

def bulk_import_donors():
    err = require_auth()
    if err: return err
    rows = request.json.get('rows', [])
    if not rows:
        return jsonify({'error': 'No rows provided'}), 400
    conn = get_db()
    imported = 0
    skipped = 0
    errors = []
    for i, row in enumerate(rows):
        display_name = (row.get('display_name') or row.get('name') or '').strip()
        if not display_name:
            skipped += 1
            continue
        try:
            did = str(uuid.uuid4())
            execute(conn, '''INSERT INTO donors
                (id,type,display_name,legal_name,email,phone,address,
                 is_anonymous,recognition_name,notes,status,created_by)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'active',%s)''',
                (did, row.get('type','individual'), display_name,
                 row.get('legal_name',''), row.get('email',''),
                 row.get('phone',''), row.get('address',''), False,
                 row.get('recognition_name',''), row.get('notes',''),
                 session.get('user_name','')))
            amount_str = str(row.get('total_donated') or row.get('amount') or '').replace('$','').replace(',','').strip()
            if amount_str:
                try:
                    amount = float(amount_str)
                    if amount > 0:
                        import datetime as _dt
                        date_str = row.get('last_donation_date') or row.get('donation_date') or _dt.date.today().isoformat()
                        pid = str(uuid.uuid4())
                        execute(conn, '''INSERT INTO donor_donations
                            (id,donor_id,amount,donation_date,type,payment_status,notes,created_by)
                            VALUES (%s,%s,%s,%s,'cash','received','Imported',%s)''',
                            (pid, did, amount, date_str, session.get('user_name','')))
                        recalc_donor_totals(conn, did)
                except (ValueError, TypeError):
                    pass
            conn.commit()
            imported += 1
        except Exception as e:
            conn.rollback()
            errors.append('Row {}: {}'.format(i+2, str(e)[:80]))
            skipped += 1
    conn.close()
    return jsonify({'ok': True, 'imported': imported, 'skipped': skipped, 'errors': errors[:10]})

# ── Dynamic donor routes ──
@app.route('/api/donors/<did>', methods=['PUT'])
def update_donor(did):
    err = require_auth()
    if err: return err
    d = request.json
    conn = get_db()
    execute(conn, '''UPDATE donors SET type=%s,display_name=%s,legal_name=%s,email=%s,phone=%s,
        address=%s,website=%s,volunteer_id=%s,is_anonymous=%s,recognition_name=%s,
        notes=%s,internal_rating=%s,status=%s WHERE id=%s''',
        (d.get('type','individual'), d.get('display_name',''), d.get('legal_name',''),
         d.get('email',''), d.get('phone',''), d.get('address',''), d.get('website',''),
         d.get('volunteer_id') or None, d.get('is_anonymous',False),
         d.get('recognition_name',''), d.get('notes',''),
         d.get('internal_rating','normal'), d.get('status','active'), did))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/donors/<did>', methods=['DELETE'])
def delete_donor(did):
    err = require_auth()
    if err: return err
    conn = get_db()
    execute(conn, 'DELETE FROM donors WHERE id=%s', (did,))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/donors/<primary_id>/merge', methods=['POST'])
def merge_donors(primary_id):
    """Merge one or more duplicate donors into a primary donor."""
    err = require_auth()
    if err: return err
    d = request.json
    merge_ids = d.get('merge_ids', [])
    if not merge_ids:
        return jsonify({'error': 'No donors to merge'}), 400
    conn = get_db()
    try:
        primary = fetchone(conn, 'SELECT * FROM donors WHERE id=%s', (primary_id,))
        if not primary:
            conn.close(); return jsonify({'error': 'Primary donor not found'}), 404
        moved_donations = 0
        for mid in merge_ids:
            if mid == primary_id:
                continue
            # Move all donations
            execute(conn, 'UPDATE donor_donations SET donor_id=%s WHERE donor_id=%s', (primary_id, mid))
            # Move benefit usage
            execute(conn, 'UPDATE donor_benefit_usage SET donor_id=%s WHERE donor_id=%s', (primary_id, mid))
            # Move communications
            execute(conn, 'UPDATE donor_communications SET donor_id=%s WHERE donor_id=%s', (primary_id, mid))
            # Count what we moved
            count = fetchone(conn, 'SELECT COUNT(*) as c FROM donor_donations WHERE donor_id=%s', (primary_id,))
            moved_donations = count['c'] if count else 0
            # Delete the duplicate
            execute(conn, 'DELETE FROM donors WHERE id=%s', (mid,))
        # Recalculate primary totals
        recalc_donor_totals(conn, primary_id)
        conn.commit()
        conn.close()
        return jsonify({'ok': True, 'moved_donations': moved_donations})
    except Exception as e:
        conn.rollback(); conn.close()
        return jsonify({'error': str(e)}), 500

@app.route('/api/donors/<did>/detail')
def get_donor_detail(did):
    err = require_auth()
    if err: return err
    conn = get_db()
    donor = fetchone(conn, '''
        SELECT dn.*, t.name as tier_name, t.color as tier_color,
               v.name as volunteer_name
        FROM donors dn
        LEFT JOIN donor_tiers t ON dn.tier_id=t.id
        LEFT JOIN volunteers v ON dn.volunteer_id=v.id
        WHERE dn.id=%s''', (did,))
    if not donor:
        conn.close(); return jsonify({'error': 'Not found'}), 404
    donor['donations'] = fetchall(conn, '''
        SELECT dd.*, c.name as campaign_name
        FROM donor_donations dd
        LEFT JOIN donor_campaigns c ON dd.campaign_id=c.id
        WHERE dd.donor_id=%s ORDER BY dd.donation_date DESC''', (did,))
    donor['benefit_usage'] = fetchall(conn, '''
        SELECT bu.*, b.name as benefit_name, t.name as tier_name
        FROM donor_benefit_usage bu
        JOIN donor_tier_benefits b ON bu.benefit_id=b.id
        JOIN donor_tiers t ON b.tier_id=t.id
        WHERE bu.donor_id=%s ORDER BY bu.used_at DESC''', (did,))
    donor['communications'] = fetchall(conn, '''
        SELECT * FROM donor_communications WHERE donor_id=%s ORDER BY sent_at DESC''', (did,))
    # Cumulative tier benefits (this tier + all lower tiers)
    donor['benefits'] = get_cumulative_benefits(conn, donor.get('tier_id'))

    # Campaign-specific benefits — for each campaign this donor has donated to,
    # show which benefits they've earned based on their total to that campaign
    try:
        campaign_totals = fetchall(conn, '''
            SELECT campaign_id, c.name as campaign_name,
                   SUM(amount) FILTER (WHERE payment_status='received') as total
            FROM donor_donations dd
            JOIN donor_campaigns c ON dd.campaign_id=c.id
            WHERE dd.donor_id=%s AND dd.campaign_id IS NOT NULL
            GROUP BY campaign_id, c.name
            HAVING SUM(amount) FILTER (WHERE payment_status='received') > 0
        ''', (did,))
        campaign_benefits = []
        for ct in campaign_totals:
            earned = fetchall(conn, '''
                SELECT * FROM campaign_benefits
                WHERE campaign_id=%s AND min_amount <= %s
                ORDER BY min_amount ASC, sort_order ASC
            ''', (ct['campaign_id'], float(ct['total'] or 0)))
            if earned:
                campaign_benefits.append({
                    'campaign_id': ct['campaign_id'],
                    'campaign_name': ct['campaign_name'],
                    'total_donated': float(ct['total'] or 0),
                    'benefits': earned
                })
        donor['campaign_benefits'] = campaign_benefits
    except Exception as e:
        app.logger.warning(f'campaign benefits for donor {did}: {e}')
        donor['campaign_benefits'] = []

    conn.close()
    return jsonify(donor)

def recalc_donor_totals(conn, donor_id):
    total_row = fetchone(conn, '''
        SELECT COALESCE(SUM(amount),0) as total FROM donor_donations
        WHERE donor_id=%s AND payment_status='received' ''', (donor_id,))
    total = float(total_row['total']) if total_row else 0.0
    dates_row = fetchone(conn, '''
        SELECT MIN(donation_date) as first_date, MAX(donation_date) as last_date
        FROM donor_donations WHERE donor_id=%s AND payment_status='received' ''', (donor_id,))
    donor = fetchone(conn, 'SELECT tier_override FROM donors WHERE id=%s', (donor_id,))
    new_tier_id = None
    if not (donor and donor.get('tier_override')):
        tier_row = fetchone(conn, '''
            SELECT id FROM donor_tiers
            WHERE min_amount <= %s AND (max_amount IS NULL OR max_amount >= %s)
            ORDER BY min_amount DESC LIMIT 1''', (total, total))
        if tier_row: new_tier_id = tier_row['id']
    first_date = dates_row['first_date'] if dates_row else None
    last_date  = dates_row['last_date']  if dates_row else None
    if not (donor and donor.get('tier_override')):
        execute(conn, '''UPDATE donors SET total_donated=%s,
            first_donation_date=%s, last_donation_date=%s, tier_id=%s WHERE id=%s''',
            (total, first_date, last_date, new_tier_id, donor_id))
    else:
        execute(conn, '''UPDATE donors SET total_donated=%s,
            first_donation_date=%s, last_donation_date=%s WHERE id=%s''',
            (total, first_date, last_date, donor_id))

@app.route('/api/donors/<did>/donations', methods=['POST'])
def add_donation(did):
    err = require_auth()
    if err: return err
    d = request.json
    donation_id = str(uuid.uuid4())
    conn = get_db()
    execute(conn, '''INSERT INTO donor_donations
        (id,donor_id,campaign_id,amount,donation_date,type,payment_status,check_number,notes,created_by)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
        (donation_id, did, d.get('campaign_id') or None,
         d.get('amount',0), d['donation_date'],
         d.get('type','cash'), d.get('payment_status','received'),
         d.get('check_number',''), d.get('notes',''),
         session.get('user_name','')))
    conn.commit()
    recalc_donor_totals(conn, did)
    conn.commit()
    row = fetchone(conn, '''SELECT dd.*, c.name as campaign_name
        FROM donor_donations dd LEFT JOIN donor_campaigns c ON dd.campaign_id=c.id
        WHERE dd.id=%s''', (donation_id,))
    conn.close()
    return jsonify(row)

@app.route('/api/donations/<donation_id>', methods=['PUT'])
def update_donation(donation_id):
    err = require_auth()
    if err: return err
    d = request.json
    conn = get_db()
    execute(conn, '''UPDATE donor_donations SET amount=%s,donation_date=%s,type=%s,
        payment_status=%s,campaign_id=%s,check_number=%s,notes=%s WHERE id=%s''',
        (d.get('amount',0), d['donation_date'], d.get('type','cash'),
         d.get('payment_status','received'), d.get('campaign_id') or None,
         d.get('check_number',''), d.get('notes',''), donation_id))
    conn.commit()
    row = fetchone(conn, 'SELECT donor_id FROM donor_donations WHERE id=%s', (donation_id,))
    if row: recalc_donor_totals(conn, row['donor_id']); conn.commit()
    conn.close()
    return jsonify({'ok': True})

@app.route('/api/donations/<donation_id>', methods=['DELETE'])
def delete_donation(donation_id):
    err = require_auth()
    if err: return err
    conn = get_db()
    row = fetchone(conn, 'SELECT donor_id FROM donor_donations WHERE id=%s', (donation_id,))
    execute(conn, 'DELETE FROM donor_donations WHERE id=%s', (donation_id,))
    conn.commit()
    if row: recalc_donor_totals(conn, row['donor_id']); conn.commit()
    conn.close()
    return jsonify({'ok': True})

@app.route('/api/donations/<donation_id>/send-thankyou', methods=['POST'])
def send_thank_you(donation_id):
    err = require_auth()
    if err: return err
    d = request.json or {}
    template_id = d.get('template_id')
    conn = get_db()
    row = fetchone(conn, '''SELECT dd.*, dn.display_name, dn.recognition_name, dn.email,
        dn.is_anonymous, c.name as campaign_name
        FROM donor_donations dd
        JOIN donors dn ON dd.donor_id=dn.id
        LEFT JOIN donor_campaigns c ON dd.campaign_id=c.id
        WHERE dd.id=%s''', (donation_id,))
    if not row: conn.close(); return jsonify({'error': 'Not found'}), 404
    if not row.get('email'):
        conn.close(); return jsonify({'error': 'Donor has no email address on file'}), 400
    name         = row.get('recognition_name') or row['display_name']
    amount       = '${:,.2f}'.format(float(row['amount']))
    campaign_str = ' for ' + row['campaign_name'] if row.get('campaign_name') else ''
    date_str     = str(row.get('donation_date',''))

    # Look up donor's current tier and benefits for placeholders
    donor = fetchone(conn, '''SELECT dn.total_donated, t.name as tier_name, t.id as tier_id
        FROM donors dn LEFT JOIN donor_tiers t ON dn.tier_id=t.id
        WHERE dn.id=%s''', (row['donor_id'],))
    tier_name = donor['tier_name'] if donor and donor.get('tier_name') else ''
    benefits_html = ''
    benefits_text = ''

    all_benefits = []

    # Tier benefits (cumulative)
    if donor and donor.get('tier_id'):
        tier_benefits = get_cumulative_benefits(conn, donor['tier_id'])
        all_benefits += [dict(b, source='tier') for b in tier_benefits]

    # Campaign-specific benefits — include all that the donation amount qualifies for
    if row.get('campaign_id') and row.get('amount'):
        try:
            camp_benefits = fetchall(conn, '''SELECT * FROM campaign_benefits
                WHERE campaign_id=%s AND min_amount <= %s
                ORDER BY min_amount ASC, sort_order ASC''',
                (row['campaign_id'], float(row['amount'])))
            all_benefits += [dict(b, source='campaign') for b in camp_benefits]
        except Exception:
            pass

    if all_benefits:
        def benefit_li(b):
            source_tag = ''
            if b.get('source') == 'campaign':
                source_tag = f'<em style="font-size:11px;color:#888">Campaign</em> '
            elif b.get('tier_name'):
                source_tag = f'<em style="font-size:11px;color:#888">{b["tier_name"]}</em> '
            return (f'<li style="margin-bottom:4px">{source_tag}{b["name"]}'
                    + (f' — {b["description"]}' if b.get('description') else '')
                    + '</li>')
        benefits_html = '<ul style="margin:8px 0;padding-left:20px">' + \
            ''.join(benefit_li(b) for b in all_benefits) + '</ul>'
        benefits_text = '\n'.join(
            f'• {b["name"]}' + (f' — {b["description"]}' if b.get('description') else '')
            for b in all_benefits)
    # Load template
    tmpl = None
    if template_id:
        tmpl = fetchone(conn, 'SELECT * FROM donor_email_templates WHERE id=%s', (template_id,))
    if not tmpl:
        tmpl = fetchone(conn, "SELECT * FROM donor_email_templates WHERE is_default=TRUE AND template_type='thankyou' LIMIT 1")
    if tmpl:
        def sub(text):
            return (text or '')\
                .replace('{{name}}', name)\
                .replace('{{amount}}', amount)\
                .replace('{{campaign}}', row.get('campaign_name','') or '')\
                .replace('{{date}}', date_str)\
                .replace('{{tier}}', tier_name)\
                .replace('{{benefits}}', benefits_html)\
                .replace('{{benefits_text}}', benefits_text)
        subject    = sub(tmpl['subject'])
        html_body  = sub(tmpl['body'])
        from_email = tmpl.get('from_email') or None
        from_name  = tmpl.get('from_name') or ''
        from_addr  = (f'{from_name} <{from_email}>' if from_name and from_email else from_email) if from_email else None
    else:
        subject   = 'Thank You for Your Generous Support — HWTC'
        from_addr = None
        html_body = '''<div style="font-family:-apple-system,sans-serif;max-width:600px;margin:0 auto">
        <div style="background:linear-gradient(135deg,#0d3d4d,#145466);padding:32px;text-align:center;border-radius:12px 12px 0 0">
            <h1 style="color:#fff;font-size:24px;margin:16px 0 0">Thank You!</h1></div>
        <div style="padding:32px;background:#fff;border-radius:0 0 12px 12px;border:1px solid #e0e0db;border-top:none">
            <p style="font-size:16px">Dear {name},</p>
            <p style="font-size:15px;color:#5f5e5a;line-height:1.7">On behalf of Horizon West Theatre Company, thank you for your generous contribution of <strong>{amount}</strong>{campaign_str}.</p>
            <div style="background:#f0f8fa;border-left:4px solid #145466;padding:16px;margin:24px 0">
                <div style="font-weight:700">{amount}</div><div style="font-size:13px;color:#5f5e5a">Date: {date}</div></div>
            <p style="font-size:15px;color:#5f5e5a">With gratitude,<br/><strong>Horizon West Theatre Company</strong></p></div>
        <p style="text-align:center;font-size:11px;color:#9b9b94;margin-top:16px">Horizon West Theatre Company is a 501(c)(3) non-profit organization.</p>
        </div>'''.format(name=name, amount=amount, campaign_str=campaign_str, date=date_str)
    ok, err_msg = send_email([row['email']], subject, html_body, from_addr)
    if ok:
        execute(conn, '''UPDATE donor_donations SET thank_you_sent=TRUE,
            thank_you_sent_at=NOW(), thank_you_sent_by=%s WHERE id=%s''',
            (session.get('user_name',''), donation_id))
        cid = str(uuid.uuid4())
        execute(conn, '''INSERT INTO donor_communications (id,donor_id,type,subject,body,sent_by)
            VALUES (%s,%s,'email',%s,%s,%s)''',
            (cid, row['donor_id'], subject,
             'Thank you sent for {}{}'.format(amount, campaign_str),
             session.get('user_name','')))
        conn.commit(); conn.close()
        return jsonify({'ok': True})
    conn.close()
    return jsonify({'error': err_msg or 'Failed to send email'}), 500

# ── Donor Email Templates ──
@app.route('/api/donor-email-templates')
def get_donor_email_templates():
    err = require_auth()
    if err: return err
    conn = get_db()
    rows = fetchall(conn, 'SELECT * FROM donor_email_templates ORDER BY name')
    conn.close()
    return jsonify(rows)

@app.route('/api/donor-email-templates', methods=['POST'])
def create_donor_email_template():
    err = require_auth()
    if err: return err
    d = request.json
    tid = str(uuid.uuid4())
    conn = get_db()
    if d.get('is_default'):
        execute(conn, "UPDATE donor_email_templates SET is_default=FALSE WHERE template_type=%s",
            (d.get('template_type','thankyou'),))
    execute(conn, '''INSERT INTO donor_email_templates
        (id,name,subject,body,from_email,from_name,template_type,is_default)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s)''',
        (tid, d.get('name',''), d.get('subject',''), d.get('body',''),
         d.get('from_email',''), d.get('from_name',''),
         d.get('template_type','thankyou'), d.get('is_default',False)))
    conn.commit()
    row = fetchone(conn, 'SELECT * FROM donor_email_templates WHERE id=%s', (tid,))
    conn.close()
    return jsonify(row)

@app.route('/api/donor-email-templates/<tid>', methods=['PUT'])
def update_donor_email_template(tid):
    err = require_auth()
    if err: return err
    d = request.json
    conn = get_db()
    if d.get('is_default'):
        execute(conn, "UPDATE donor_email_templates SET is_default=FALSE WHERE template_type=%s AND id!=%s",
            (d.get('template_type','thankyou'), tid))
    execute(conn, '''UPDATE donor_email_templates SET name=%s,subject=%s,body=%s,
        from_email=%s,from_name=%s,template_type=%s,is_default=%s WHERE id=%s''',
        (d.get('name',''), d.get('subject',''), d.get('body',''),
         d.get('from_email',''), d.get('from_name',''),
         d.get('template_type','thankyou'), d.get('is_default',False), tid))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/donor-email-templates/<tid>', methods=['DELETE'])
def delete_donor_email_template(tid):
    err = require_auth()
    if err: return err
    conn = get_db()
    execute(conn, 'DELETE FROM donor_email_templates WHERE id=%s', (tid,))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/donors/<did>/benefits/use', methods=['POST'])
def record_benefit_use(did):
    err = require_auth()
    if err: return err
    d = request.json
    uid = str(uuid.uuid4())
    conn = get_db()
    execute(conn, '''INSERT INTO donor_benefit_usage (id,donor_id,benefit_id,notes,recorded_by)
        VALUES (%s,%s,%s,%s,%s)''',
        (uid, did, d.get('benefit_id'), d.get('notes',''), session.get('user_name','')))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/donor-benefit-usage/<uid>', methods=['DELETE'])
def delete_benefit_use(uid):
    err = require_auth()
    if err: return err
    conn = get_db()
    execute(conn, 'DELETE FROM donor_benefit_usage WHERE id=%s', (uid,))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/donors/<did>/tier', methods=['PUT'])
def set_donor_tier(did):
    err = require_auth()
    if err: return err
    d = request.json
    conn = get_db()
    execute(conn, 'UPDATE donors SET tier_id=%s, tier_override=%s WHERE id=%s',
        (d.get('tier_id') or None, d.get('override', False), did))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

# ─────────────────────────────────────────────
#  STATIC PAGES
# ─────────────────────────────────────────────

@app.route('/kiosk')
def kiosk_page():
    resp = send_from_directory('static', 'kiosk.html')
    resp.headers['Cache-Control'] = 'no-store'
    return resp

@app.route('/pickup')
def pickup_page():
    return send_from_directory('static', 'pickup.html')

@app.route('/portal')
def portal_page():
    resp = send_from_directory('static', 'portal.html')
    resp.headers['Cache-Control'] = 'no-store'
    return resp

@app.route('/join')
def join_page():
    resp = send_from_directory('static', 'join.html')
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    return resp

# ─────────────────────────────────────────────
#  NAV ICONS
# ─────────────────────────────────────────────

@app.route('/api/nav-icons')
def get_nav_icons():
    conn = get_db()
    rows = fetchall(conn, 'SELECT key, lucide_name FROM nav_icons')
    conn.close()
    return jsonify({r['key']: r['lucide_name'] for r in rows})

@app.route('/api/nav-icons', methods=['PUT'])
def save_nav_icons():
    err = require_admin()
    if err: return err
    d = request.json
    conn = get_db()
    for key, name in d.items():
        if name:
            execute(conn, '''INSERT INTO nav_icons (key, lucide_name) VALUES (%s,%s)
                ON CONFLICT (key) DO UPDATE SET lucide_name=EXCLUDED.lucide_name''', (key, name))
        else:
            execute(conn, 'DELETE FROM nav_icons WHERE key=%s', (key,))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

# ─────────────────────────────────────────────
#  EVENT TYPES
# ─────────────────────────────────────────────

@app.route('/api/event-types')
def get_event_types():
    err = require_auth()
    if err: return err
    conn = get_db()
    types = fetchall(conn, 'SELECT * FROM event_types ORDER BY name')
    conn.close()
    return jsonify(types)

@app.route('/api/event-types', methods=['POST'])
def create_event_type():
    err = require_auth()
    if err: return err
    d = request.json
    tid = str(uuid.uuid4())
    conn = get_db()
    execute(conn, 'INSERT INTO event_types (id,name,color,description) VALUES (%s,%s,%s,%s)',
        (tid, d.get('name',''), d.get('color','blue'), d.get('description','')))
    conn.commit()
    row = fetchone(conn, 'SELECT * FROM event_types WHERE id=%s', (tid,))
    conn.close()
    return jsonify(row)

@app.route('/api/event-types/<tid>', methods=['PUT'])
def update_event_type(tid):
    err = require_auth()
    if err: return err
    d = request.json
    conn = get_db()
    execute(conn, 'UPDATE event_types SET name=%s,color=%s,description=%s WHERE id=%s',
        (d.get('name',''), d.get('color','blue'), d.get('description',''), tid))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/event-types/<tid>', methods=['DELETE'])
def delete_event_type(tid):
    err = require_auth()
    if err: return err
    conn = get_db()
    execute(conn, 'DELETE FROM event_types WHERE id=%s', (tid,))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

# ─────────────────────────────────────────────
#  ELICS
# ─────────────────────────────────────────────

@app.route('/api/elics')
def get_elics():
    err = require_auth()
    if err: return err
    conn = get_db()
    elics = fetchall(conn, '''SELECT e.*, v.name as volunteer_name
        FROM elics e LEFT JOIN volunteers v ON e.volunteer_id=v.id ORDER BY v.name''')
    conn.close()
    return jsonify(elics)

@app.route('/api/elics', methods=['POST'])
def create_elic():
    err = require_auth()
    if err: return err
    d = request.json
    eid = str(uuid.uuid4())
    conn = get_db()
    execute(conn, '''INSERT INTO elics (id, volunteer_id, pin, is_master, assigned_events)
        VALUES (%s,%s,%s,%s,%s)''',
        (eid, d.get('volunteer_id'), d.get('pin','0000'),
         d.get('is_master', False), json.dumps(d.get('assigned_events',[]))))
    conn.commit()
    row = fetchone(conn, '''SELECT e.*, v.name as volunteer_name
        FROM elics e LEFT JOIN volunteers v ON e.volunteer_id=v.id WHERE e.id=%s''', (eid,))
    conn.close()
    return jsonify(row)

@app.route('/api/elics/<eid>', methods=['PUT'])
def update_elic(eid):
    err = require_auth()
    if err: return err
    d = request.json
    conn = get_db()
    execute(conn, 'UPDATE elics SET volunteer_id=%s, pin=%s, is_master=%s, assigned_events=%s WHERE id=%s',
        (d.get('volunteer_id'), d.get('pin','0000'),
         d.get('is_master',False), json.dumps(d.get('assigned_events',[])), eid))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/elics/<eid>', methods=['DELETE'])
def delete_elic(eid):
    err = require_auth()
    if err: return err
    conn = get_db()
    execute(conn, 'DELETE FROM elics WHERE id=%s', (eid,))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/kiosk/elic-auth', methods=['POST'])
@app.route('/api/kiosk/elic-login', methods=['POST'])
def kiosk_elic_auth():
    d = request.json
    pin = d.get('pin','')
    conn = get_db()
    elic = fetchone(conn, '''SELECT e.*, v.name as volunteer_name
        FROM elics e LEFT JOIN volunteers v ON e.volunteer_id=v.id
        WHERE e.pin=%s''', (pin,))
    if not elic:
        conn.close(); return jsonify({'error': 'Invalid PIN'}), 401
    # Get assigned events
    assigned = json.loads(elic.get('assigned_events') or '[]')
    if elic.get('is_master'):
        events = fetchall(conn, '''
            SELECT e.*, p.name as production_name,
                   COALESCE(p.stage,'mainstage') as stage,
                   p.stage as production_stage,
                   pg.name as program_name
            FROM events e
            LEFT JOIN productions p ON e.production_id=p.id
            LEFT JOIN youth_programs pg ON e.program_id=pg.id
            ORDER BY e.event_date DESC NULLS LAST, e.name''')
    else:
        if assigned:
            placeholders = ','.join(['%s']*len(assigned))
            events = fetchall(conn, f'''
                SELECT e.*, p.name as production_name,
                       COALESCE(p.stage,'mainstage') as stage,
                       p.stage as production_stage,
                       pg.name as program_name
                FROM events e
                LEFT JOIN productions p ON e.production_id=p.id
                LEFT JOIN youth_programs pg ON e.program_id=pg.id
                WHERE e.id IN ({placeholders})''', tuple(assigned))
        else:
            events = []
    conn.close()
    return jsonify({'ok': True, 'elic': elic, 'events': events})

# ─────────────────────────────────────────────
#  CHECKLIST ITEMS
# ─────────────────────────────────────────────

@app.route('/api/checklist-items')
def get_checklist_items():
    # No auth required — kiosk needs this without an admin session
    conn = get_db()
    items = fetchall(conn, 'SELECT * FROM checklist_items ORDER BY sort_order, label')
    conn.close()
    return jsonify(items)

@app.route('/api/checklist-items', methods=['POST'])
def create_checklist_item():
    err = require_auth()
    if err: return err
    d = request.json
    iid = str(uuid.uuid4())
    conn = get_db()
    execute(conn, 'INSERT INTO checklist_items (id,label,required,sort_order) VALUES (%s,%s,%s,%s)',
        (iid, d.get('label',''), d.get('required',False), d.get('sort_order',0)))
    conn.commit()
    row = fetchone(conn, 'SELECT * FROM checklist_items WHERE id=%s', (iid,))
    conn.close()
    return jsonify(row)

@app.route('/api/checklist-items/<iid>', methods=['PUT'])
def update_checklist_item(iid):
    err = require_auth()
    if err: return err
    d = request.json
    conn = get_db()
    execute(conn, 'UPDATE checklist_items SET label=%s, required=%s, sort_order=%s WHERE id=%s',
        (d.get('label',''), d.get('required',False), d.get('sort_order',0), iid))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/checklist-items/<iid>', methods=['DELETE'])
def delete_checklist_item(iid):
    err = require_auth()
    if err: return err
    conn = get_db()
    execute(conn, 'DELETE FROM checklist_items WHERE id=%s', (iid,))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/opening-checklist-items')
def get_opening_checklist_items():
    # No auth required — kiosk needs this without an admin session
    conn = get_db()
    items = fetchall(conn, 'SELECT * FROM opening_checklist_items ORDER BY sort_order, label')
    conn.close()
    return jsonify(items)

@app.route('/api/opening-checklist-items', methods=['POST'])
def create_opening_checklist_item():
    err = require_auth()
    if err: return err
    d = request.json
    iid = str(uuid.uuid4())
    conn = get_db()
    execute(conn, 'INSERT INTO opening_checklist_items (id,label,required,sort_order) VALUES (%s,%s,%s,%s)',
        (iid, d.get('label',''), d.get('required',False), d.get('sort_order',0)))
    conn.commit()
    row = fetchone(conn, 'SELECT * FROM opening_checklist_items WHERE id=%s', (iid,))
    conn.close()
    return jsonify(row)

@app.route('/api/opening-checklist-items/<iid>', methods=['PUT'])
def update_opening_checklist_item(iid):
    err = require_auth()
    if err: return err
    d = request.json
    conn = get_db()
    execute(conn, 'UPDATE opening_checklist_items SET label=%s, required=%s, sort_order=%s WHERE id=%s',
        (d.get('label',''), d.get('required',False), d.get('sort_order',0), iid))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/opening-checklist-items/<iid>', methods=['DELETE'])
def delete_opening_checklist_item(iid):
    err = require_auth()
    if err: return err
    conn = get_db()
    execute(conn, 'DELETE FROM opening_checklist_items WHERE id=%s', (iid,))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

# ─────────────────────────────────────────────
#  PENDING HOURS
# ─────────────────────────────────────────────

@app.route('/api/pending-hours')
def get_pending_hours():
    err = require_auth()
    if err: return err
    conn = get_db()
    rows = fetchall(conn, '''SELECT ph.*, v.name as volunteer_name
        FROM pending_hours ph
        LEFT JOIN volunteers v ON ph.volunteer_id=v.id
        WHERE ph.status IN ('pending','pending_review','pending_profile')
        ORDER BY ph.submitted_at DESC NULLS LAST''')
    conn.close()
    return jsonify(rows)

@app.route('/api/pending-hours/<hid>', methods=['PUT'])
def update_pending_hours(hid):
    err = require_auth()
    if err: return err
    d = request.json or {}
    hours = d.get('hours')
    notes = (d.get('notes') or '').strip()
    if hours is None: return jsonify({'error': 'hours required'}), 400
    try:
        hours = round(float(hours), 2)
        if hours <= 0: return jsonify({'error': 'Hours must be greater than 0'}), 400
    except (ValueError, TypeError):
        return jsonify({'error': 'Invalid hours value'}), 400
    conn = get_db()
    execute(conn, 'UPDATE pending_hours SET hours=%s, notes=%s WHERE id=%s', (hours, notes, hid))
    conn.commit(); conn.close()
    return jsonify({'ok': True, 'hours': hours})

@app.route('/api/hours/<hid>', methods=['PUT'])
def update_approved_hours(hid):
    err = require_auth()
    if err: return err
    d = request.json or {}
    hours = d.get('hours')
    notes = (d.get('notes') or '').strip()
    if hours is None: return jsonify({'error': 'hours required'}), 400
    try:
        hours = round(float(hours), 2)
        if hours <= 0: return jsonify({'error': 'Hours must be greater than 0'}), 400
    except (ValueError, TypeError):
        return jsonify({'error': 'Invalid hours value'}), 400
    conn = get_db()
    execute(conn, 'UPDATE hours SET hours=%s, notes=%s WHERE id=%s', (hours, notes, hid))
    conn.commit(); conn.close()
    return jsonify({'ok': True, 'hours': hours})

@app.route('/api/pending-hours/<hid>/approve', methods=['POST'])
def approve_pending_hours(hid):
    err = require_auth()
    if err: return err
    conn = get_db()
    ph = fetchone(conn, 'SELECT * FROM pending_hours WHERE id=%s', (hid,))
    if not ph: conn.close(); return jsonify({'error': 'Not found'}), 404
    pid = str(uuid.uuid4())
    try:
        execute(conn, '''INSERT INTO hours (id,volunteer_id,event,event_id,date,hours,role,notes)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)''',
            (pid, ph['volunteer_id'], ph['event'], ph.get('event_id'),
             ph['date'], ph['hours'], ph.get('role',''), ph.get('notes','')))
    except Exception:
        # May already exist — just mark as approved
        pass
    execute(conn, "UPDATE pending_hours SET status='approved' WHERE id=%s", (hid,))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/pending-hours/<hid>/reject', methods=['POST'])
def reject_pending_hours(hid):
    err = require_auth()
    if err: return err
    conn = get_db()
    execute(conn, "UPDATE pending_hours SET status='rejected' WHERE id=%s", (hid,))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

# ─────────────────────────────────────────────
#  ALERTS & NOTIFICATIONS
# ─────────────────────────────────────────────

@app.route('/api/alerts')
def get_alerts():
    err = require_auth()
    if err: return err
    conn = get_db()
    rows = fetchall(conn, '''SELECT * FROM alerts WHERE status='active'
        ORDER BY created_at DESC LIMIT 50''')
    conn.close()
    return jsonify(rows)

@app.route('/api/alerts', methods=['POST'])
def create_alert():
    err = require_auth()
    if err: return err
    d = request.json
    aid = str(uuid.uuid4())
    conn = get_db()
    execute(conn, '''INSERT INTO alerts (id,type,message,source,status)
        VALUES (%s,%s,%s,%s,'active')''',
        (aid, d.get('type','info'), d.get('message',''), d.get('source','')))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/alerts/<aid>/dismiss', methods=['POST'])
def dismiss_alert(aid):
    err = require_auth()
    if err: return err
    conn = get_db()
    execute(conn, "UPDATE alerts SET status='dismissed' WHERE id=%s", (aid,))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

# ─────────────────────────────────────────────
#  EVENT LOGS
# ─────────────────────────────────────────────

@app.route('/api/event-logs')
def get_event_logs():
    err = require_auth()
    if err: return err
    conn = get_db()
    rows = fetchall(conn, '''SELECT el.*, e.name as event_name
        FROM event_logs el LEFT JOIN events e ON el.event_id=e.id
        ORDER BY el.created_at DESC LIMIT 200''')
    conn.close()
    return jsonify(rows)

# ─────────────────────────────────────────────
#  EMAIL SETTINGS
# ─────────────────────────────────────────────

@app.route('/api/email-settings')
def get_email_settings_route():
    err = require_auth()
    if err: return err
    s = get_email_settings()
    return jsonify(s)

@app.route('/api/email-settings', methods=['PUT'])
def save_email_settings_route():
    err = require_admin()
    if err: return err
    d = request.json or {}
    conn = get_db()
    # Build dynamic UPDATE for any fields passed
    allowed = ['resend_api_key','from_email','report_recipients','report_recipient_user_ids',
        'alert_pending_hours','alert_profile_updates','alert_callouts','alert_waiver_expiry',
        'alert_conflicts','alert_waivers','alert_event_not_opened','alert_event_not_closed',
        'auto_send_checklist_report','alert_new_rsvp','alert_role_filled']
    sets = []; vals = []
    for key in allowed:
        if key in d:
            sets.append(f'{key}=%s')
            val = d[key]
            if isinstance(val, list): val = json.dumps(val)
            vals.append(val)
    if sets:
        vals.append(1)
        execute(conn, f"UPDATE email_settings SET {','.join(sets)} WHERE id=%s", vals)
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/email-settings/test', methods=['POST'])
def test_email_route():
    err = require_admin()
    if err: return err
    d = request.json or {}
    to = (d.get('to') or '').strip()
    # Fall back to current user's email
    if not to:
        conn = get_db()
        user = fetchone(conn, 'SELECT email FROM users WHERE id=%s', (session.get('user_id',''),))
        conn.close()
        to = user['email'] if user else ''
    if not to: return jsonify({'error': 'No recipient email found. Please save your email address in your user profile.'}), 400
    ok, msg = send_email([to], 'RoleCall Test Email',
        '<p style="font-family:sans-serif">This is a test email from RoleCall. If you received this, email is working correctly.</p>')
    if ok: return jsonify({'ok': True, 'sent_to': to})
    return jsonify({'error': msg or 'Failed to send'}), 500

# ─────────────────────────────────────────────
#  USERS (additional routes)
# ─────────────────────────────────────────────

@app.route('/api/users/<uid>', methods=['PUT'])
def update_user(uid):
    err = require_admin()
    if err: return err
    d = request.json
    conn = get_db()
    if d.get('password'):
        pw_hash = hashlib.sha256(d.get('password','').encode()).hexdigest()
        execute(conn, 'UPDATE users SET name=%s, email=%s, password_hash=%s WHERE id=%s',
            (d.get('name',''), d.get('email',''), pw_hash, uid))
    else:
        execute(conn, 'UPDATE users SET name=%s, email=%s WHERE id=%s',
            (d.get('name',''), d.get('email',''), uid))
    if 'permissions' in d:
        execute(conn, 'UPDATE users SET role_permissions=%s WHERE id=%s',
            (json.dumps(d.get('permissions','[]')), uid))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/users/<uid>', methods=['DELETE'])
def delete_user(uid):
    err = require_admin()
    if err: return err
    if uid == session.get('user_id'):
        return jsonify({'error': 'Cannot delete your own account'}), 400
    conn = get_db()
    execute(conn, 'DELETE FROM users WHERE id=%s', (uid,))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/users/<uid>/permissions', methods=['PUT'])
def update_user_permissions(uid):
    err = require_admin()
    if err: return err
    d = request.json
    conn = get_db()
    execute(conn, 'UPDATE users SET role_permissions=%s WHERE id=%s',
        (json.dumps(d.get('permissions',{})), uid))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/users/<uid>/send-reset-link', methods=['POST'])
def send_reset_link(uid):
    err = require_admin()
    if err: return err
    conn = get_db()
    user = fetchone(conn, 'SELECT * FROM users WHERE id=%s', (uid,))
    if not user: conn.close(); return jsonify({'error': 'User not found'}), 404
    import secrets
    temp_pw = secrets.token_urlsafe(10)
    # Use SHA-256 to match the login route
    pw_hash = hashlib.sha256(temp_pw.encode()).hexdigest()
    execute(conn, 'UPDATE users SET password_hash=%s WHERE id=%s', (pw_hash, uid))
    conn.commit()
    html_body = f'''<div style="font-family:-apple-system,sans-serif;max-width:500px;margin:0 auto">
        <div style="background:linear-gradient(135deg,#0d3d4d,#145466);padding:28px;text-align:center;border-radius:12px 12px 0 0">
            <h2 style="color:#fff;margin:0">RoleCall — Temporary Password</h2>
        </div>
        <div style="padding:28px;background:#fff;border-radius:0 0 12px 12px;border:1px solid #e0e0db;border-top:none">
            <p style="font-size:15px;color:#1a1a17">Hi {user['name']},</p>
            <p style="font-size:14px;color:#5f5e5a;line-height:1.7">A temporary password has been generated for your RoleCall account. Use it to log in, then change your password right away.</p>
            <div style="background:#f0f8fa;border-left:4px solid #145466;padding:16px 20px;border-radius:0 8px 8px 0;margin:20px 0;text-align:center">
                <div style="font-size:11px;font-weight:700;color:#145466;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:8px">Temporary Password</div>
                <div style="font-size:22px;font-weight:800;font-family:monospace;color:#0d3d4d;letter-spacing:2px">{temp_pw}</div>
            </div>
            <p style="font-size:13px;color:#9b9b94;">If you did not request this, please contact your administrator.</p>
        </div>
    </div>'''
    ok, msg = send_email([user['email']], 'Your RoleCall Temporary Password', html_body)
    conn.close()
    if ok: return jsonify({'ok': True})
    return jsonify({'error': msg or 'Failed to send email. Check that your Resend API key is configured in Settings → Email.'}), 500

# ─────────────────────────────────────────────
#  FAMILIES & PORTAL
# ─────────────────────────────────────────────

@app.route('/api/families')
def get_families():
    err = require_auth()
    if err: return err
    conn = get_db()
    families = fetchall(conn, '''SELECT f.*, COUNT(y.id) as youth_count
        FROM families f LEFT JOIN youth_participants y ON y.family_id=f.id
        GROUP BY f.id ORDER BY f.name''')
    conn.close()
    return jsonify(families)

@app.route('/api/families', methods=['POST'])
def create_family():
    err = require_auth()
    if err: return err
    d = request.json
    fid = str(uuid.uuid4())
    conn = get_db()
    execute(conn, 'INSERT INTO families (id,name,passphrase,email,phone) VALUES (%s,%s,%s,%s,%s)',
        (fid, d.get('name',''), d.get('passphrase',''), d.get('email',''), d.get('phone','')))
    conn.commit()
    row = fetchone(conn, 'SELECT * FROM families WHERE id=%s', (fid,))
    conn.close()
    return jsonify(row)

@app.route('/api/families/<fid>', methods=['PUT'])
def update_family(fid):
    err = require_auth()
    if err: return err
    d = request.json
    conn = get_db()
    execute(conn, 'UPDATE families SET name=%s, passphrase=%s, email=%s, phone=%s WHERE id=%s',
        (d.get('name',''), d.get('passphrase',''), d.get('email',''), d.get('phone',''), fid))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/youth/<yid>/passphrase', methods=['PUT'])
def set_youth_passphrase(yid):
    err = require_auth()
    if err: return err
    d = request.json
    conn = get_db()
    execute(conn, 'UPDATE youth SET passphrase=%s WHERE id=%s',
        (d.get('passphrase',''), yid))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/portal/auth', methods=['POST'])
@app.route('/api/portal/login', methods=['POST'])
def portal_auth():
    d = request.json or {}
    passphrase = (d.get('passphrase') or '').strip().lower()
    if not passphrase: return jsonify({'error': 'Passphrase required'}), 400
    conn = get_db()

    # Try family passphrase first
    family = fetchone(conn, 'SELECT * FROM families WHERE LOWER(passphrase)=%s', (passphrase,))
    if family:
        members = fetchall(conn, 'SELECT * FROM youth_participants WHERE family_id=%s ORDER BY first_name', (family['id'],))
        conn.close()
        return jsonify({
            'type': 'family',
            'family': family,
            'members': members,
            'passphrase': passphrase,
        })

    # Try individual youth passphrase
    youth = fetchone(conn, 'SELECT * FROM youth_participants WHERE LOWER(passphrase)=%s', (passphrase,))
    if youth:
        family_row = fetchone(conn, 'SELECT * FROM families WHERE id=%s', (youth.get('family_id'),)) if youth.get('family_id') else None
        conn.close()
        return jsonify({
            'type': 'participant',
            'participant': youth,
            'family': family_row,
            'members': [youth],
            'passphrase': passphrase,
        })

    conn.close()
    return jsonify({'error': 'Passphrase not found. Please check with HWTC staff.'}), 401

@app.route('/api/portal/announcements')
def get_portal_announcements():
    conn = get_db()
    prod_id = request.args.get('production_id')
    if prod_id:
        rows = fetchall(conn, '''SELECT * FROM portal_announcements
            WHERE production_id=%s AND status='published' ORDER BY created_at DESC''', (prod_id,))
    else:
        rows = fetchall(conn, "SELECT * FROM portal_announcements WHERE status='published' ORDER BY created_at DESC")
    conn.close()
    return jsonify(rows)

@app.route('/api/portal/contact-production', methods=['POST'])
def portal_contact_production():
    d = request.json
    conn = get_db()
    prod = fetchone(conn, 'SELECT * FROM productions WHERE id=%s', (d.get('production_id'),))
    conn.close()
    if not prod: return jsonify({'error': 'Not found'}), 404
    s = get_email_settings()
    recipients = get_recipient_emails(s)
    if recipients:
        send_email(recipients, f'Portal Message: {d.get("subject","")}',
            f'<p style="font-family:sans-serif">From: {d.get("from_name","")} ({d.get("from_email","")})<br/>'
            f'Production: {prod["name"]}<br/><br/>{d.get("message","")}</p>')
    return jsonify({'ok': True})

@app.route('/api/portal/participant/<yid>')
def portal_get_participant(yid):
    conn = get_db()
    errors = []

    # Program enrollments
    try:
        enrollments = fetchall(conn, '''SELECT ype.*, yp.name as program_name, yp.description
            FROM youth_program_enrollments ype
            JOIN youth_programs yp ON ype.program_id=yp.id
            WHERE ype.youth_id=%s ORDER BY ype.created_at DESC''', (yid,))
    except Exception as e:
        enrollments = []; errors.append(f'enrollments: {e}')

    # Productions
    try:
        productions = fetchall(conn, '''SELECT p.id, p.name, p.stage, p.status,
            p.description, p.image_url, p.director, p.venue,
            ypm.role as cast_role, ypm.id as member_id
            FROM youth_production_members ypm
            JOIN productions p ON ypm.production_id=p.id
            WHERE ypm.youth_id=%s ORDER BY p.name''', (yid,))
    except Exception as e:
        productions = []; errors.append(f'productions: {e}')

    # Announcements
    prod_ids = [p['id'] for p in productions]
    try:
        announcements = []
        if prod_ids:
            placeholders = ','.join(['%s']*len(prod_ids))
            announcements = fetchall(conn, f'''SELECT * FROM portal_announcements
                WHERE production_id IN ({placeholders}) AND status='published'
                ORDER BY created_at DESC''', tuple(prod_ids))
    except Exception as e:
        announcements = []; errors.append(f'announcements: {e}')

    # Files
    try:
        files = []
        if prod_ids:
            placeholders = ','.join(['%s']*len(prod_ids))
            files = fetchall(conn, f'''SELECT * FROM portal_files
                WHERE context_id IN ({placeholders})
                ORDER BY created_at DESC''', tuple(prod_ids))
    except Exception as e:
        files = []; errors.append(f'files: {e}')

    conn.close()
    if errors:
        app.logger.error(f'portal_get_participant {yid}: {errors}')

    return jsonify({
        'enrollments': enrollments,
        'productions': productions,
        'announcements': announcements,
        'files': files,
        '_errors': errors if errors else None,
    })

@app.route('/api/portal/youth/<yid>')
def portal_youth_profile(yid):
    conn = get_db()
    youth = fetchone(conn, '''SELECT y.*, f.name as family_name
        FROM youth_participants y LEFT JOIN families f ON y.family_id=f.id
        WHERE y.id=%s''', (yid,))
    if not youth: conn.close(); return jsonify({'error': 'Not found'}), 404
    authorized = fetchall(conn, 'SELECT * FROM youth_authorized_pickups WHERE youth_id=%s ORDER BY priority', (yid,))
    conn.close()
    youth['authorized_pickups'] = authorized
    return jsonify(youth)

@app.route('/api/portal/youth/<yid>/request-update', methods=['POST'])
def portal_youth_request_update(yid):
    d = request.json or {}
    conn = get_db()
    # Log a note for staff to review
    nid = str(uuid.uuid4())
    execute(conn, '''INSERT INTO pending_hours (id,volunteer_id,event,date,hours,notes,status)
        VALUES (%s,%s,'Profile Update Request',CURRENT_DATE,0,%s,'pending_review')''',
        (nid, yid, json.dumps(d)))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/portal/files')
def portal_get_files():
    context_id = request.args.get('context_id')
    context_type = request.args.get('context_type','production')
    conn = get_db()
    if context_id:
        rows = fetchall(conn, 'SELECT * FROM portal_files WHERE context_id=%s AND context_type=%s ORDER BY created_at DESC',
            (context_id, context_type))
    else:
        rows = fetchall(conn, 'SELECT * FROM portal_files ORDER BY created_at DESC')
    conn.close()
    return jsonify(rows)

@app.route('/api/portal/callout')
def portal_callout():
    """System-wide announcement shown on portal login screen."""
    conn = get_db()
    try:
        row = fetchone(conn, "SELECT value FROM settings WHERE key='portal_callout'")
        conn.close()
        if row:
            import json as _json
            val = _json.loads(row['value']) if isinstance(row['value'], str) else row['value']
            return jsonify({'callout': val})
    except Exception:
        conn.close()
    return jsonify({'callout': None})

@app.route('/api/portal/instructor-login', methods=['POST'])
def portal_instructor_login():
    d = request.json or {}
    email    = (d.get('email') or '').strip().lower()
    password = d.get('password','')
    if not email or not password:
        return jsonify({'error': 'Email and password required'}), 400
    conn = get_db()
    pw_hash = hashlib.sha256(password.encode()).hexdigest()
    user = fetchone(conn, 'SELECT * FROM users WHERE LOWER(email)=%s AND password_hash=%s', (email, pw_hash))
    if not user:
        conn.close()
        return jsonify({'error': 'Invalid email or password'}), 401
    perms = {}
    try: perms = json.loads(user.get('role_permissions') or '{}')
    except Exception: pass
    conn.close()
    return jsonify({
        'type': 'instructor',
        'user': {'id': user['id'], 'name': user['name'], 'email': user['email'],
                 'role': user['role'], 'permissions': perms}
    })

# ─────────────────────────────────────────────
#  PRODUCTIONS (additional routes)
# ─────────────────────────────────────────────

@app.route('/api/productions/<pid>/youth-members')
def get_prod_youth_members(pid):
    err = require_auth()
    if err: return err
    conn = get_db()
    rows = fetchall(conn, '''SELECT ypm.*, y.first_name, y.last_name,
        y.first_name||' '||y.last_name as name
        FROM youth_production_members ypm
        JOIN youth_participants y ON ypm.youth_id=y.id
        WHERE ypm.production_id=%s ORDER BY y.last_name, y.first_name''', (pid,))
    conn.close()
    return jsonify(rows)

@app.route('/api/productions/<pid>/youth-members', methods=['POST'])
def enroll_youth_in_prod(pid):
    err = require_auth()
    if err: return err
    d = request.json
    conn = get_db()
    try:
        # Support both single youth_id and bulk youth_ids array
        youth_ids = d.get('youth_ids') or ([d.get('youth_id')] if d.get('youth_id') else [])
        if not youth_ids:
            conn.close()
            return jsonify({'error': 'No youth specified'}), 400
        enrolled = 0
        for yid in youth_ids:
            mid = str(uuid.uuid4())
            try:
                execute(conn, '''INSERT INTO youth_production_members (id,production_id,youth_id,role,status)
                    VALUES (%s,%s,%s,%s,'enrolled') ON CONFLICT (production_id,youth_id) DO NOTHING''',
                    (mid, pid, yid, d.get('role','')))
                enrolled += 1
            except Exception:
                pass
        conn.commit()
        conn.close()
        return jsonify({'ok': True, 'enrolled': enrolled})
    except Exception as e:
        conn.rollback(); conn.close()
        return jsonify({'error': str(e)}), 500

@app.route('/api/productions/<pid>/youth-members/<mid>', methods=['PUT'])
def update_youth_prod_member(pid, mid):
    err = require_auth()
    if err: return err
    d = request.json or {}
    conn = get_db()
    execute(conn, 'UPDATE youth_production_members SET role=%s WHERE id=%s AND production_id=%s',
        (d.get('role',''), mid, pid))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/productions/<pid>/youth-members/<mid>', methods=['DELETE'])
def unenroll_youth_from_prod(pid, mid):
    err = require_auth()
    if err: return err
    conn = get_db()
    execute(conn, 'DELETE FROM youth_production_members WHERE id=%s AND production_id=%s', (mid, pid))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/productions/<pid>/conflicts')
def get_production_conflicts(pid):
    err = require_auth()
    if err: return err
    conn = get_db()
    rows = fetchall(conn, '''SELECT sc.*, v.name as person_name,
        e.name as event_name
        FROM schedule_conflicts sc
        LEFT JOIN volunteers v ON sc.volunteer_id=v.id
        LEFT JOIN events e ON sc.event_id=e.id
        WHERE sc.production_id=%s ORDER BY sc.created_at DESC''', (pid,))
    conn.close()
    return jsonify(rows)

@app.route('/api/productions/<pid>/conflicts', methods=['POST'])
def add_production_conflict(pid):
    err = require_auth()
    if err: return err
    d = request.json or {}
    cid = str(uuid.uuid4())
    conn = get_db()
    execute(conn, '''INSERT INTO schedule_conflicts
        (id,production_id,event_id,volunteer_id,status,event_date,notes,approved,source)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
        (cid, pid, d.get('event_id') or None, d.get('volunteer_id') or None,
         d.get('status','absent'), d.get('event_date') or None,
         d.get('notes',''), d.get('approved', False), d.get('source','staff')))
    conn.commit()
    row = fetchone(conn, '''SELECT sc.*, v.name as person_name, e.name as event_name
        FROM schedule_conflicts sc
        LEFT JOIN volunteers v ON sc.volunteer_id=v.id
        LEFT JOIN events e ON sc.event_id=e.id WHERE sc.id=%s''', (cid,))
    conn.close()
    return jsonify(row)

@app.route('/api/productions/<pid>/conflicts/<cid>', methods=['PUT'])
def update_production_conflict(pid, cid):
    err = require_auth()
    if err: return err
    d = request.json or {}
    conn = get_db()
    execute(conn, '''UPDATE schedule_conflicts SET
        status=%s, event_date=%s, notes=%s, approved=%s WHERE id=%s AND production_id=%s''',
        (d.get('status','absent'), d.get('event_date') or None,
         d.get('notes',''), d.get('approved', False), cid, pid))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/productions/<pid>/conflicts/<cid>', methods=['DELETE'])
def delete_production_conflict(pid, cid):
    err = require_auth()
    if err: return err
    conn = get_db()
    execute(conn, 'DELETE FROM schedule_conflicts WHERE id=%s AND production_id=%s', (cid, pid))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/productions/<pid>/team')
def get_production_team(pid):
    conn = get_db()
    # Return public-facing team bios (headshots, bios — no volunteer required)
    rows = fetchall(conn, '''SELECT * FROM production_team_bios
        WHERE production_id=%s ORDER BY sort_order, name''', (pid,))
    # Also include production_members (crew with volunteer links) as fallback
    if not rows:
        rows = fetchall(conn, '''SELECT pm.id, pm.role, pm.bio,
            pm.photo_url as headshot_url,
            v.name, pm.department, pm.status
            FROM production_members pm
            JOIN volunteers v ON pm.volunteer_id=v.id
            WHERE pm.production_id=%s ORDER BY pm.department, v.name''', (pid,))
    conn.close()
    return jsonify(rows)

@app.route('/api/productions/<pid>/team', methods=['POST'])
def add_team_bio(pid):
    err = require_auth()
    if err: return err
    d = request.json or {}
    if not d.get('name'):
        return jsonify({'error': 'Name is required'}), 400
    mid = str(uuid.uuid4())
    conn = get_db()
    execute(conn, '''INSERT INTO production_team_bios
        (id, production_id, name, role, bio, headshot_url, sort_order)
        VALUES (%s,%s,%s,%s,%s,%s,%s)''',
        (mid, pid, (d.get('name') or '').strip(), (d.get('role') or '').strip(),
         (d.get('bio') or '').strip(), (d.get('headshot_url') or '').strip(),
         d.get('sort_order', 0)))
    conn.commit()
    row = fetchone(conn, 'SELECT * FROM production_team_bios WHERE id=%s', (mid,))
    conn.close()
    return jsonify(row)

@app.route('/api/productions/<pid>/team/<mid>', methods=['PUT'])
def update_team_bio(pid, mid):
    err = require_auth()
    if err: return err
    d = request.json or {}
    conn = get_db()
    execute(conn, '''UPDATE production_team_bios SET
        name=%s, role=%s, bio=%s, headshot_url=%s, sort_order=%s
        WHERE id=%s AND production_id=%s''',
        ((d.get('name') or '').strip(), (d.get('role') or '').strip(),
         (d.get('bio') or '').strip(), (d.get('headshot_url') or '').strip(),
         d.get('sort_order', 0), mid, pid))
    conn.commit()
    row = fetchone(conn, 'SELECT * FROM production_team_bios WHERE id=%s', (mid,))
    conn.close()
    return jsonify(row or {'ok': True})

@app.route('/api/productions/<pid>/team/<mid>', methods=['DELETE'])
def delete_team_bio(pid, mid):
    err = require_auth()
    if err: return err
    conn = get_db()
    execute(conn, 'DELETE FROM production_team_bios WHERE id=%s AND production_id=%s', (mid, pid))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/productions/<pid>/general-content')
def get_general_content(pid):
    conn = get_db()
    row = fetchone(conn, 'SELECT general_content FROM productions WHERE id=%s', (pid,))
    conn.close()
    return jsonify({'content': row['general_content'] if row else ''})

@app.route('/api/productions/<pid>/general-content', methods=['PUT'])
def save_general_content(pid):
    err = require_auth()
    if err: return err
    d = request.json
    conn = get_db()
    execute(conn, 'UPDATE productions SET general_content=%s WHERE id=%s',
        (d.get('content',''), pid))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/productions/<pid>/about', methods=['PUT'])
def update_production_about(pid):
    err = require_auth()
    if err: return err
    d = request.json
    conn = get_db()
    execute(conn, '''UPDATE productions SET
        director=%s, venue=%s, performance_location=%s,
        start_date=%s, end_date=%s, description=%s,
        portal_color=%s, portal_image_url=%s WHERE id=%s''',
        (d.get('director',''), d.get('venue',''), d.get('performance_location',''),
         d.get('start_date') or None, d.get('end_date') or None,
         d.get('description',''), d.get('portal_color',''), d.get('portal_image_url',''), pid))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/productions/<pid>/announcements', methods=['POST'])
def create_portal_announcement(pid):
    err = require_auth()
    if err: return err
    d = request.json
    aid = str(uuid.uuid4())
    conn = get_db()
    execute(conn, '''INSERT INTO portal_announcements
        (id,production_id,title,body,status,created_by)
        VALUES (%s,%s,%s,%s,%s,%s)''',
        (aid, pid, d.get('title',''), d.get('body',''),
         d.get('status','draft'), session.get('user_name','')))
    conn.commit()
    row = fetchone(conn, 'SELECT * FROM portal_announcements WHERE id=%s', (aid,))
    conn.close()
    return jsonify(row)

@app.route('/api/productions/<pid>/announcements/<aid>', methods=['PUT'])
def update_portal_announcement(pid, aid):
    err = require_auth()
    if err: return err
    d = request.json
    conn = get_db()
    execute(conn, 'UPDATE portal_announcements SET title=%s, body=%s, status=%s WHERE id=%s AND production_id=%s',
        (d.get('title',''), d.get('body',''), d.get('status','draft'), aid, pid))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/productions/<pid>/announcements/<aid>', methods=['DELETE'])
def delete_portal_announcement(pid, aid):
    err = require_auth()
    if err: return err
    conn = get_db()
    execute(conn, 'DELETE FROM portal_announcements WHERE id=%s AND production_id=%s', (aid, pid))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/productions/<pid>/announcements/<aid>/push', methods=['POST'])
def push_announcement(pid, aid):
    err = require_auth()
    if err: return err
    conn = get_db()
    execute(conn, "UPDATE portal_announcements SET status='published' WHERE id=%s AND production_id=%s", (aid, pid))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/productions/<pid>/waivers', methods=['POST'])
def add_prod_waiver(pid):
    err = require_auth()
    if err: return err
    d = request.json
    rid = str(uuid.uuid4())
    conn = get_db()
    execute(conn, 'INSERT INTO production_required_waivers (id,production_id,waiver_type_id) VALUES (%s,%s,%s)',
        (rid, pid, d.get('waiver_type_id')))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/productions/<pid>/waivers/<wid>', methods=['DELETE'])
def remove_prod_waiver(pid, wid):
    err = require_auth()
    if err: return err
    conn = get_db()
    execute(conn, 'DELETE FROM production_required_waivers WHERE production_id=%s AND waiver_type_id=%s', (pid, wid))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

# ─────────────────────────────────────────────
#  KIOSK ROUTES
# ─────────────────────────────────────────────

@app.route('/api/kiosk/interest-types')
def kiosk_interest_types():
    conn = get_db()
    types = fetchall(conn, 'SELECT id, name, color FROM interest_types ORDER BY name')
    conn.close()
    return jsonify(types)

@app.route('/api/kiosk/volunteer-profile/<vol_id>')
def kiosk_volunteer_profile(vol_id):
    conn = get_db()
    vol = fetchone(conn,
        "SELECT id, name, phone, interests, COALESCE(background_check_status,'none') as background_check_status FROM volunteers WHERE id=%s AND status='active'",
        (vol_id,))
    if not vol: conn.close(); return jsonify({'error': 'Not found'}), 404
    ec = fetchone(conn, 'SELECT name, relationship, phone FROM volunteer_emergency_contacts WHERE volunteer_id=%s ORDER BY created_at DESC LIMIT 1', (vol_id,))
    vol['emergency_contact'] = ec or {}
    conn.close()
    return jsonify(vol)

@app.route('/api/kiosk/volunteers')
def kiosk_volunteers():
    q = request.args.get('q', '').strip().lower()
    if len(q) < 2:
        return jsonify([])
    conn = get_db()
    vols = fetchall(conn,
        "SELECT id, name, phone, interests FROM volunteers WHERE LOWER(name) LIKE %s AND status='active' ORDER BY name LIMIT 20",
        ('%' + q + '%',))
    conn.close()
    return jsonify(vols)

@app.route('/api/kiosk/events')
def kiosk_events():
    conn = get_db()
    events = fetchall(conn, """
        SELECT e.*,
               p.name as production_name,
               COALESCE(p.stage,'mainstage') as stage,
               p.stage as production_stage
        FROM events e
        LEFT JOIN productions p ON e.production_id=p.id
        WHERE e.status='open'
           OR (e.status IN ('draft','published','in_progress')
               AND e.event_date::date >= (CURRENT_DATE - INTERVAL '1 day')
               AND e.event_date::date <= (CURRENT_DATE + INTERVAL '1 day'))
        ORDER BY CASE WHEN e.status='open' THEN 0 ELSE 1 END, e.event_date ASC NULLS LAST
    """)
    conn.close()
    return jsonify(events)

@app.route('/api/kiosk/submit', methods=['POST'])
def kiosk_submit():
    d = request.json
    if not d.get('volunteer_id') or not d.get('event') or not d.get('hours'):
        return jsonify({'error': 'Missing required fields'}), 400
    try:
        hours = float(d.get('hours',0))
        if hours <= 0 or hours > 24:
            return jsonify({'error': 'Hours must be between 0.5 and 24'}), 400
    except Exception:
        return jsonify({'error': 'Invalid hours value'}), 400
    pid = str(uuid.uuid4())
    conn = get_db()
    today_row = fetchone(conn, "SELECT CURRENT_DATE::text as today")
    today = today_row['today'] if today_row else __import__('datetime').date.today().isoformat()
    execute(conn, "INSERT INTO pending_hours (id,volunteer_id,event,event_id,date,hours,role,notes,status) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'pending')",
        (pid, d.get('volunteer_id'), d.get('event',''), d.get('event_id'), today, hours, d.get('role',''), d.get('notes','')))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/kiosk/waiver-check')
def kiosk_waiver_check():
    vol_id = request.args.get('volunteer_id')
    if not vol_id: return jsonify({'issues': [], 'all_clear': True})
    conn = get_db()
    from datetime import date as _date
    today = _date.today()
    required = fetchall(conn, '''SELECT wt.* FROM waiver_types wt
        WHERE wt.required_all=TRUE OR wt.required_for_volunteering=TRUE''')
    issues = []
    for wt in required:
        signed = fetchone(conn, '''SELECT * FROM volunteer_waivers WHERE volunteer_id=%s AND waiver_type_id=%s
            ORDER BY signed_date DESC LIMIT 1''', (vol_id, wt['id']))
        if not signed:
            issues.append({'waiver_type_id': wt['id'], 'name': wt['name'],
                'description': wt.get('description',''), 'status': 'missing',
                'can_sign_online': bool(wt.get('can_sign_online')),
                'template_body': wt.get('template_body','')})
        elif wt.get('expires_days') and signed.get('expiry_date'):
            try:
                exp = __import__('datetime').date.fromisoformat(str(signed['expiry_date'])[:10])
                if exp < today:
                    issues.append({'waiver_type_id': wt['id'], 'name': wt['name'],
                        'description': wt.get('description',''), 'status': 'expired',
                        'can_sign_online': bool(wt.get('can_sign_online')),
                        'template_body': wt.get('template_body','')})
            except Exception:
                pass
    conn.close()
    return jsonify({'issues': issues, 'all_clear': len(issues) == 0})

@app.route('/api/kiosk/sign-waiver', methods=['POST'])
def kiosk_sign_waiver():
    d = request.json
    vol_id = d.get('volunteer_id')
    waiver_type_id = d.get('waiver_type_id')
    signed_name = d.get('signed_name', '')
    if not vol_id or not waiver_type_id: return jsonify({'error': 'Missing fields'}), 400
    conn = get_db()
    wt = fetchone(conn, 'SELECT * FROM waiver_types WHERE id=%s', (waiver_type_id,))
    if not wt: conn.close(); return jsonify({'error': 'Waiver type not found'}), 404
    from datetime import date as _date, timedelta
    today = _date.today()
    expiry = None
    if wt.get('expires_days'):
        expiry = (today + timedelta(days=int(wt['expires_days']))).isoformat()
    wid = str(uuid.uuid4())
    execute(conn, '''INSERT INTO volunteer_waivers
        (id,volunteer_id,waiver_type_id,signed_date,expiry_date,signed_name,signed_via)
        VALUES (%s,%s,%s,%s,%s,%s,'kiosk')''',
        (wid, vol_id, waiver_type_id, today.isoformat(), expiry, signed_name))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/kiosk/update-profile', methods=['POST'])
def kiosk_update_profile():
    d = request.json
    vol_id = d.get('volunteer_id')
    if not vol_id: return jsonify({'error': 'Missing volunteer_id'}), 400
    conn = get_db()
    updates = []
    params = []
    if d.get('phone') is not None:
        updates.append('phone=%s'); params.append(d['phone'])
    if d.get('interests') is not None:
        updates.append('interests=%s'); params.append(json.dumps(d.get('interests','[]')))
    if updates:
        execute(conn, f"UPDATE volunteers SET {','.join(updates)} WHERE id=%s", tuple(params + [vol_id]))
    if d.get('emergency_contact'):
        ec = d['emergency_contact']
        existing = fetchone(conn, 'SELECT id FROM volunteer_emergency_contacts WHERE volunteer_id=%s', (vol_id,))
        if existing:
            execute(conn, 'UPDATE volunteer_emergency_contacts SET name=%s, relationship=%s, phone=%s WHERE volunteer_id=%s',
                (ec.get('name',''), ec.get('relationship',''), ec.get('phone',''), vol_id))
        else:
            ecid = str(uuid.uuid4())
            execute(conn, 'INSERT INTO volunteer_emergency_contacts (id,volunteer_id,name,relationship,phone) VALUES (%s,%s,%s,%s,%s)',
                (ecid, vol_id, ec.get('name',''), ec.get('relationship',''), ec.get('phone','')))
    conn.commit()
    # Log update request for admin review
    pid = str(uuid.uuid4())
    execute(conn, "INSERT INTO pending_hours (id,volunteer_id,event,date,hours,notes,status) VALUES (%s,%s,'Profile Update',CURRENT_DATE,0,'Profile update submitted via kiosk','pending_profile')",
        (pid, vol_id))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/kiosk/session/active/<vol_id>')
def kiosk_active_session(vol_id):
    conn = get_db()
    s = fetchone(conn, "SELECT * FROM kiosk_sessions WHERE volunteer_id=%s AND status='active'", (vol_id,))
    conn.close()
    if s: return jsonify({'active': True, 'session': s})
    return jsonify({'active': False})

@app.route('/api/kiosk/session/begin', methods=['POST'])
def kiosk_begin_session():
    d = request.json or {}
    vol_id          = d.get('volunteer_id') or ''
    event_id        = d.get('event_id') or None
    role            = (d.get('role') or '').strip()
    override_reason = (d.get('override_reason') or '').strip()
    if not vol_id: return jsonify({'error': 'Missing volunteer_id'}), 400
    conn = get_db()
    # Require event or override reason
    if not event_id and not override_reason:
        conn.close()
        return jsonify({'error': 'Please select an event or provide an override reason.'}), 400
    # Check event is open if one is specified
    if event_id:
        evt = fetchone(conn, 'SELECT status, name FROM events WHERE id=%s', (event_id,))
        if evt and evt.get('status') != 'open':
            conn.close()
            return jsonify({'error': 'This event is not open yet. Please wait for staff to open it.'}), 400
    existing = fetchone(conn, "SELECT id FROM kiosk_sessions WHERE volunteer_id=%s AND status='active'", (vol_id,))
    if existing: conn.close(); return jsonify({'error': 'Already volunteering — please stop your current session first.'}), 400
    event_name = d.get('event_name','')
    if event_id and not event_name:
        evt = fetchone(conn, 'SELECT name FROM events WHERE id=%s', (event_id,))
        if evt: event_name = evt['name']
    if not event_name and override_reason:
        event_name = f'Override: {override_reason}'
    sid = str(uuid.uuid4())
    execute(conn, "INSERT INTO kiosk_sessions (id,volunteer_id,event_id,event_name,role,status) VALUES (%s,%s,%s,%s,%s,'active')",
        (sid, vol_id, event_id or None, event_name, role))
    conn.commit()
    session_row = fetchone(conn, 'SELECT * FROM kiosk_sessions WHERE id=%s', (sid,))
    conn.close()
    return jsonify({'ok': True, 'session_id': sid, 'started_at': str(session_row['started_at']),
                    'is_override': bool(override_reason)})

@app.route('/api/kiosk/session/stop', methods=['POST'])
def kiosk_stop_session():
    d = request.json or {}
    vol_id = d.get('volunteer_id')
    role   = d.get('role','')
    if not vol_id: return jsonify({'error': 'Missing volunteer_id'}), 400
    conn = get_db()
    try:
        sess = fetchone(conn, "SELECT * FROM kiosk_sessions WHERE volunteer_id=%s AND status='active'", (vol_id,))
        if not sess: conn.close(); return jsonify({'error': 'No active session found'}), 400
        time_row = fetchone(conn, "SELECT EXTRACT(EPOCH FROM (NOW() - started_at)) as secs FROM kiosk_sessions WHERE id=%s", (sess['id'],))
        elapsed_secs  = float(time_row['secs']) if time_row and time_row['secs'] else 0
        elapsed_hours = round(max(0.25, elapsed_secs / 3600), 2)
        today_row = fetchone(conn, "SELECT CURRENT_DATE::text as today")
        today = today_row['today'] if today_row else __import__('datetime').date.today().isoformat()
        execute(conn, "UPDATE kiosk_sessions SET ended_at=NOW(), hours=%s, status='completed', role=%s WHERE id=%s",
            (elapsed_hours, role or sess['role'], sess['id']))
        pid = str(uuid.uuid4())
        # Override sessions (no event) need admin review before approval
        hours_status = 'pending' if sess.get('event_id') else 'pending_review'
        override_note = ' [Override — no event selected]' if not sess.get('event_id') else ''
        execute(conn, "INSERT INTO pending_hours (id,volunteer_id,event,event_id,date,hours,role,notes,status) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (pid, vol_id, sess['event_name'] or 'Volunteer Session', sess['event_id'],
             today, elapsed_hours, role or sess['role'],
             'Recorded via kiosk timer' + override_note, hours_status))
        conn.commit()
        try:
            s = get_email_settings()
            if s.get('alert_pending_hours'):
                recipients = get_recipient_emails(s)
                vol = fetchone(conn, 'SELECT name FROM volunteers WHERE id=%s', (vol_id,))
                vol_name = vol['name'] if vol else 'A volunteer'
                if recipients:
                    send_email(recipients, 'RoleCall — Hours Submitted: ' + vol_name,
                        '<p style="font-family:sans-serif"><strong>' + vol_name + '</strong> logged <strong>'
                        + str(elapsed_hours) + ' hours</strong> via kiosk timer for <strong>'
                        + (sess['event_name'] or 'a session') + '</strong>.</p>')
        except Exception:
            pass
        conn.close()
        return jsonify({'ok': True, 'hours': elapsed_hours})
    except Exception as e:
        try: conn.close()
        except Exception: pass
        return jsonify({'error': str(e)}), 500

@app.route('/api/kiosk/session/stop-by-id', methods=['POST'])
def kiosk_stop_session_by_id():
    d = request.json or {}
    sid = d.get('session_id')
    if not sid: return jsonify({'error': 'Missing session_id'}), 400
    conn = get_db()
    sess = fetchone(conn, "SELECT * FROM kiosk_sessions WHERE id=%s AND status='active'", (sid,))
    if not sess: conn.close(); return jsonify({'error': 'Session not found or already stopped'}), 404
    time_row = fetchone(conn, "SELECT EXTRACT(EPOCH FROM (NOW() - started_at)) as secs FROM kiosk_sessions WHERE id=%s", (sid,))
    elapsed_secs  = float(time_row['secs']) if time_row and time_row['secs'] else 0
    elapsed_hours = round(max(0.25, elapsed_secs / 3600), 2)
    today_row = fetchone(conn, "SELECT CURRENT_DATE::text as today")
    today = today_row['today'] if today_row else __import__('datetime').date.today().isoformat()
    execute(conn, "UPDATE kiosk_sessions SET ended_at=NOW(), hours=%s, status='completed' WHERE id=%s", (elapsed_hours, sid))
    pid = str(uuid.uuid4())
    execute(conn, "INSERT INTO pending_hours (id,volunteer_id,event,event_id,date,hours,role,notes,status) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'pending')",
        (pid, sess['volunteer_id'], sess['event_name'] or 'Volunteer Session',
         sess['event_id'], today, elapsed_hours, sess['role'], 'Stopped by ELIC'))
    conn.commit(); conn.close()
    return jsonify({'ok': True, 'hours': elapsed_hours})

@app.route('/api/kiosk/active-sessions')
def kiosk_active_sessions():
    conn = get_db()
    rows = fetchall(conn, """
        SELECT ks.id, ks.volunteer_id, ks.event_id, ks.event_name, ks.role,
               ks.started_at, ks.status, v.name as volunteer_name,
               EXTRACT(EPOCH FROM (NOW() - ks.started_at)) as elapsed_secs
        FROM kiosk_sessions ks
        JOIN volunteers v ON ks.volunteer_id=v.id
        WHERE ks.status='active' ORDER BY ks.started_at ASC
    """)
    conn.close()
    return jsonify(rows)

@app.route('/api/kiosk/log-full-event', methods=['POST'])
def kiosk_log_full_event():
    d = request.json or {}
    vol_id   = d.get('volunteer_id')
    event_id = d.get('event_id')
    role     = d.get('role','')
    if not vol_id or not event_id: return jsonify({'error': 'Missing volunteer_id or event_id'}), 400
    conn = get_db()
    evt = fetchone(conn, 'SELECT * FROM events WHERE id=%s', (event_id,))
    if not evt: conn.close(); return jsonify({'error': 'Event not found'}), 404
    hours = None
    if evt.get('start_time') and evt.get('end_time'):
        try:
            from datetime import datetime as _dt
            fmt = '%H:%M'
            start = _dt.strptime(str(evt['start_time'])[:5], fmt)
            end   = _dt.strptime(str(evt['end_time'])[:5], fmt)
            diff  = (end - start).seconds / 3600
            if diff > 0: hours = round(diff, 2)
        except Exception: pass
    if not hours: conn.close(); return jsonify({'error': 'Event has no start/end time set.'}), 400
    today_row = fetchone(conn, "SELECT CURRENT_DATE::text as today")
    today = today_row['today'] if today_row else __import__('datetime').date.today().isoformat()
    sid = str(uuid.uuid4())
    execute(conn, "INSERT INTO kiosk_sessions (id,volunteer_id,event_id,event_name,role,started_at,ended_at,hours,status) VALUES (%s,%s,%s,%s,%s,NOW(),NOW(),%s,'completed')",
        (sid, vol_id, event_id, evt['name'], role, hours))
    pid = str(uuid.uuid4())
    execute(conn, "INSERT INTO pending_hours (id,volunteer_id,event,event_id,date,hours,role,notes,status) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'pending')",
        (pid, vol_id, evt['name'], event_id, today, hours, role, 'Full event — logged via kiosk'))
    conn.commit(); conn.close()
    return jsonify({'ok': True, 'hours': hours, 'event': evt['name']})

@app.route('/api/kiosk/sign-in', methods=['POST'])
def kiosk_youth_sign_in():
    d = request.json
    conn = get_db()
    yid = d.get('youth_id')
    eid = d.get('event_id')
    if not yid: conn.close(); return jsonify({'error': 'Missing youth_id'}), 400
    existing = fetchone(conn, "SELECT id FROM youth_sign_ins WHERE youth_id=%s AND event_id=%s AND signed_out_at IS NULL", (yid, eid))
    if existing: conn.close(); return jsonify({'error': 'Already signed in'}), 400
    sid = str(uuid.uuid4())
    execute(conn, "INSERT INTO youth_sign_ins (id,youth_id,event_id,signed_in_at) VALUES (%s,%s,%s,NOW())",
        (sid, yid, eid))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/kiosk/sign-out', methods=['POST'])
def kiosk_youth_sign_out():
    d = request.json
    conn = get_db()
    yid = d.get('youth_id')
    eid = d.get('event_id')
    execute(conn, "UPDATE youth_sign_ins SET signed_out_at=NOW() WHERE youth_id=%s AND event_id=%s AND signed_out_at IS NULL",
        (yid, eid))
    conn.commit(); conn.close()
    return jsonify({'ok': True})



# ─────────────────────────────────────────────
#  APPLICATIONS (volunteer interest form)
# ─────────────────────────────────────────────

@app.route('/api/join/interest-types')
def join_interest_types():
    conn = get_db()
    types = fetchall(conn, 'SELECT id, name, color, sub_options, sub_options_label FROM interest_types ORDER BY name')
    conn.close()
    return jsonify(types)

@app.route('/api/join/submit', methods=['POST'])
def join_submit():
    d = request.json or {}
    if not d.get('name') or not d.get('email'):
        return jsonify({'error': 'Name and email are required'}), 400
    aid = str(uuid.uuid4())
    conn = get_db()
    try:
        sub_selections = json.dumps(d.get('sub_selections') or {})
        execute(conn, '''INSERT INTO volunteer_applications
            (id, name, email, phone, pronouns, is_adult, interests, how_heard, notes, status, sub_selections)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'pending',%s)''',
            (aid, (d.get('name') or '').strip(), (d.get('email') or '').strip().lower(),
             (d.get('phone') or '').strip(), (d.get('pronouns') or '').strip(),
             d.get('is_adult', True), json.dumps(d.get('interests', [])),
             (d.get('how_heard') or '').strip(), (d.get('notes') or '').strip(),
             sub_selections))
        conn.commit()
    except Exception as e:
        conn.rollback(); conn.close()
        app.logger.error(f'join_submit: {e}')
        return jsonify({'error': 'Submission failed. Please try again.'}), 500
    try:
        s = get_email_settings()
        recipients = get_recipient_emails(s)
        if recipients:
            interests_str = ', '.join(d.get('interests', [])) or 'None specified'
            age_str = '18 or older' if d.get('is_adult', True) else 'Under 18'
            # Build sub-selections rows
            sub_rows = ''
            sub_sel = d.get('sub_selections') or {}
            for interest, selections in sub_sel.items():
                if selections:
                    sub_rows += f'<tr><td style="padding:8px;font-weight:600;color:#666;width:140px">&nbsp;&nbsp;↳ {interest}</td><td style="padding:8px">{", ".join(selections)}</td></tr>'
            html_body = f'''<div style="font-family:-apple-system,sans-serif;max-width:600px">
                <h2 style="color:#0d3d4d">New Volunteer Interest Submission</h2>
                <table style="width:100%;border-collapse:collapse;font-size:14px">
                  <tr><td style="padding:8px;font-weight:600;color:#666;width:140px">Name</td><td style="padding:8px">{d.get('name','')}</td></tr>
                  <tr style="background:#f9f9f9"><td style="padding:8px;font-weight:600;color:#666">Email</td><td style="padding:8px">{d.get('email','')}</td></tr>
                  <tr><td style="padding:8px;font-weight:600;color:#666">Phone</td><td style="padding:8px">{d.get('phone','—')}</td></tr>
                  <tr style="background:#f9f9f9"><td style="padding:8px;font-weight:600;color:#666">Pronouns</td><td style="padding:8px">{d.get('pronouns','—') or '—'}</td></tr>
                  <tr><td style="padding:8px;font-weight:600;color:#666">Age</td><td style="padding:8px">{age_str}</td></tr>
                  <tr style="background:#f9f9f9"><td style="padding:8px;font-weight:600;color:#666">Interests</td><td style="padding:8px">{interests_str}</td></tr>
                  {sub_rows}
                  <tr><td style="padding:8px;font-weight:600;color:#666">How they heard</td><td style="padding:8px">{d.get('how_heard','—')}</td></tr>
                  <tr style="background:#f9f9f9"><td style="padding:8px;font-weight:600;color:#666">Notes</td><td style="padding:8px">{d.get('notes','—') or '—'}</td></tr>
                </table>
            </div>'''
            send_email(recipients, f'New Volunteer Interest — {d["name"]}', html_body)
    except Exception:
        pass
    conn.close()
    return jsonify({'ok': True, 'id': aid})

@app.route('/api/applications')
def get_applications():
    err = require_auth()
    if err: return err
    conn = get_db()
    apps = fetchall(conn, 'SELECT * FROM volunteer_applications ORDER BY created_at DESC')
    # Flag duplicates — email already exists in volunteers table
    for a in apps:
        if a.get('status') == 'pending':
            existing = fetchone(conn, 'SELECT id, name FROM volunteers WHERE LOWER(email)=LOWER(%s)', (a['email'],))
            a['duplicate_volunteer'] = {'id': existing['id'], 'name': existing['name']} if existing else None
        else:
            a['duplicate_volunteer'] = None
    conn.close()
    return jsonify(apps)

@app.route('/api/applications/<aid>/approve', methods=['POST'])
def approve_application(aid):
    err = require_auth()
    if err: return err
    conn = get_db()
    try:
        app_row = fetchone(conn, 'SELECT * FROM volunteer_applications WHERE id=%s', (aid,))
        if not app_row:
            conn.close(); return jsonify({'error': 'Application not found'}), 404

        # Check if volunteer with this email already exists
        existing = fetchone(conn, 'SELECT id FROM volunteers WHERE LOWER(email)=LOWER(%s)', (app_row['email'],))
        sub_selections = app_row.get('sub_selections') or '{}'
        if existing:
            vid = existing['id']
            # Update name/phone/pronouns if blank, merge sub_selections
            execute(conn, """UPDATE volunteers SET
                name=COALESCE(NULLIF(name,''),%s),
                phone=COALESCE(NULLIF(phone,''),%s),
                pronouns=CASE WHEN pronouns IS NULL OR pronouns='' THEN %s ELSE pronouns END
                WHERE id=%s""",
                (app_row['name'], app_row.get('phone',''), app_row.get('pronouns',''), vid))
            try:
                old_row = fetchone(conn, 'SELECT sub_selections FROM volunteers WHERE id=%s', (vid,))
                old_ss = json.loads((old_row or {}).get('sub_selections') or '{}')
                new_ss = json.loads(sub_selections)
                merged = {**old_ss, **new_ss}
                execute(conn, 'UPDATE volunteers SET sub_selections=%s WHERE id=%s', (json.dumps(merged), vid))
            except Exception:
                pass
        else:
            vid = str(uuid.uuid4())
            interests = app_row.get('interests') or '[]'
            execute(conn, "INSERT INTO volunteers (id,name,email,phone,pronouns,status,interests,sub_selections) VALUES (%s,%s,%s,%s,%s,'active',%s,%s)",
                (vid, app_row['name'], app_row['email'], app_row.get('phone',''),
                 app_row.get('pronouns',''), interests, sub_selections))

        execute(conn, "UPDATE volunteer_applications SET status='approved', volunteer_id=%s, reviewed_at=NOW(), reviewed_by=%s WHERE id=%s",
            (vid, session.get('user_name',''), aid))

        # Copy application notes to volunteer profile notes
        app_notes = (app_row.get('notes') or '').strip()
        if app_notes:
            nid = str(uuid.uuid4())
            execute(conn, "INSERT INTO notes (id,volunteer_id,author,content) VALUES (%s,%s,%s,%s)",
                (nid, vid, 'Join Form', app_notes))

        conn.commit()
        vol = fetchone(conn, 'SELECT * FROM volunteers WHERE id=%s', (vid,))
        conn.close()
        return jsonify({'ok': True, 'volunteer': vol})
    except Exception as e:
        conn.rollback(); conn.close()
        app.logger.error(f'approve_application {aid}: {e}')
        return jsonify({'error': str(e)}), 500

@app.route('/api/applications/<aid>/decline', methods=['POST'])
def decline_application(aid):
    err = require_auth()
    if err: return err
    conn = get_db()
    execute(conn, "UPDATE volunteer_applications SET status='declined', reviewed_at=NOW(), reviewed_by=%s WHERE id=%s",
        (session.get('user_name',''), aid))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/applications/<aid>', methods=['DELETE'])
def delete_application(aid):
    err = require_auth()
    if err: return err
    conn = get_db()
    execute(conn, 'DELETE FROM volunteer_applications WHERE id=%s', (aid,))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

# ─────────────────────────────────────────────
#  PORTAL INSTRUCTOR CONTENT
# ─────────────────────────────────────────────

@app.route('/api/portal/instructor/content/<context_type>/<context_id>')
def get_portal_instructor_content(context_type, context_id):
    conn = get_db()
    rows = fetchall(conn, '''SELECT * FROM portal_files
        WHERE context_type=%s AND context_id=%s ORDER BY created_at DESC''',
        (context_type, context_id))
    conn.close()
    return jsonify(rows)

# ─────────────────────────────────────────────
#  REPORTS
# ─────────────────────────────────────────────

def build_volunteer_monthly_report(year, month):
    """Build volunteer monthly recap data."""
    conn = get_db()
    import calendar
    month_name = calendar.month_name[int(month)]
    date_prefix = f'{int(year):04d}-{int(month):02d}'

    # Total hours this month
    total = fetchone(conn, """
        SELECT COALESCE(SUM(h.hours),0) as total, COUNT(DISTINCT h.volunteer_id) as vol_count
        FROM hours h WHERE h.date LIKE %s""", (date_prefix+'%',))

    # Top volunteers by hours this month
    top_vols = fetchall(conn, """
        SELECT v.name, SUM(h.hours) as hours, COUNT(*) as entries
        FROM hours h JOIN volunteers v ON h.volunteer_id=v.id
        WHERE h.date LIKE %s GROUP BY v.id, v.name
        ORDER BY hours DESC LIMIT 20""", (date_prefix+'%',))

    # Hours by event
    by_event = fetchall(conn, """
        SELECT h.event, SUM(h.hours) as hours, COUNT(DISTINCT h.volunteer_id) as vol_count
        FROM hours h WHERE h.date LIKE %s
        GROUP BY h.event ORDER BY hours DESC LIMIT 15""", (date_prefix+'%',))

    # New volunteers this month
    new_vols = fetchall(conn, """
        SELECT name, email FROM volunteers
        WHERE created_at::text LIKE %s ORDER BY created_at""", (date_prefix+'%',))

    # Pending hours awaiting approval
    pending = fetchone(conn, """
        SELECT COUNT(*) as c, COALESCE(SUM(hours),0) as total
        FROM pending_hours WHERE date LIKE %s AND status='pending'""", (date_prefix+'%',))

    # Lapsed volunteers (no hours in 60+ days)
    lapsed = fetchall(conn, """
        SELECT v.name, v.email, MAX(h.date) as last_date
        FROM volunteers v JOIN hours h ON h.volunteer_id=v.id
        WHERE v.status='active'
        GROUP BY v.id, v.name, v.email
        HAVING MAX(h.date) < (CURRENT_DATE - INTERVAL '60 days')::text
        ORDER BY last_date ASC LIMIT 20""")

    conn.close()
    return {
        'month': month_name, 'year': int(year),
        'total_hours': float(total['total']) if total else 0,
        'active_volunteers': int(total['vol_count']) if total else 0,
        'top_volunteers': top_vols,
        'hours_by_event': by_event,
        'new_volunteers': new_vols,
        'pending_hours': float(pending['total']) if pending else 0,
        'pending_count': int(pending['c']) if pending else 0,
        'lapsed_volunteers': lapsed,
    }

def build_top_volunteers_report(start_date, end_date, limit=50):
    conn = get_db()
    rows = fetchall(conn, """
        SELECT v.name, v.email, v.phone,
               COALESCE(SUM(h.hours),0) as total_hours,
               COUNT(DISTINCT h.event) as events_count,
               MIN(h.date) as first_date, MAX(h.date) as last_date
        FROM volunteers v
        LEFT JOIN hours h ON h.volunteer_id=v.id
            AND h.date >= %s AND h.date <= %s
        WHERE v.status='active'
        GROUP BY v.id, v.name, v.email, v.phone
        ORDER BY total_hours DESC, v.name ASC
        LIMIT %s""", (start_date, end_date, limit))
    conn.close()
    return rows

def build_lapsed_volunteers_report(days=90):
    conn = get_db()
    rows = fetchall(conn, """
        SELECT v.name, v.email, v.phone,
               MAX(h.date) as last_date,
               COALESCE(SUM(h.hours),0) as total_hours_ever
        FROM volunteers v
        LEFT JOIN hours h ON h.volunteer_id=v.id
        WHERE v.status='active'
        GROUP BY v.id, v.name, v.email, v.phone
        HAVING MAX(h.date) < (CURRENT_DATE - INTERVAL '%s days')::text
            OR MAX(h.date) IS NULL
        ORDER BY last_date ASC NULLS FIRST""" % int(days))
    conn.close()
    return rows

def build_hours_by_event_report(start_date, end_date):
    conn = get_db()
    rows = fetchall(conn, """
        SELECT h.event, h.event_id,
               SUM(h.hours) as total_hours,
               COUNT(DISTINCT h.volunteer_id) as volunteer_count,
               COUNT(*) as entry_count,
               MIN(h.date) as first_date, MAX(h.date) as last_date
        FROM hours h
        WHERE h.date >= %s AND h.date <= %s
        GROUP BY h.event, h.event_id
        ORDER BY total_hours DESC""", (start_date, end_date))
    conn.close()
    return rows

def build_report_email_html(report_type, data, params=None):
    """Generate HTML email for a report."""
    from datetime import date
    today = date.today().strftime('%B %d, %Y')
    header = f'''<div style="font-family:-apple-system,sans-serif;max-width:700px;margin:0 auto">
    <div style="background:linear-gradient(135deg,#0d3d4d,#145466);padding:28px 32px;border-radius:12px 12px 0 0;color:#fff">
        <div style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:1px;opacity:0.7;margin-bottom:6px">Horizon West Theatre Company</div>
        <div style="font-size:22px;font-weight:800">{{}}</div>
        <div style="font-size:13px;opacity:0.7;margin-top:4px">Generated {today}</div>
    </div>
    <div style="background:#fff;padding:28px 32px;border:1px solid #e0e0db;border-top:none;border-radius:0 0 12px 12px">'''
    footer = '''</div><p style="text-align:center;font-size:11px;color:#9b9b94;margin-top:16px">
        RoleCall — Horizon West Theatre Company Management System</p></div>'''

    def stat_box(label, value, color='#145466'):
        return f'<div style="background:#f0f8fa;border-radius:10px;padding:16px 20px;text-align:center"><div style="font-size:28px;font-weight:900;color:{color}">{value}</div><div style="font-size:12px;color:#5f5e5a;margin-top:4px">{label}</div></div>'

    def table(headers, rows, cols):
        th = ''.join(f'<th style="padding:8px 12px;text-align:left;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:0.5px;color:#5f5e5a;border-bottom:2px solid #e0e0db">{h}</th>' for h in headers)
        trs = ''
        for i, r in enumerate(rows):
            bg = '#f9f9f9' if i%2==0 else '#fff'
            tds = ''.join(f'<td style="padding:8px 12px;font-size:13px;border-bottom:1px solid #e0e0db">{str(r.get(c,"") or "—")}</td>' for c in cols)
            trs += f'<tr style="background:{bg}">{tds}</tr>'
        return f'<table style="width:100%;border-collapse:collapse;margin-top:12px"><thead><tr>{th}</tr></thead><tbody>{trs}</tbody></table>'

    if report_type == 'monthly_recap':
        title = f'{data["month"]} {data["year"]} — Volunteer Monthly Recap'
        body = f'''<div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;margin-bottom:24px">
            {stat_box('Total Hours Logged', f'{data["total_hours"]:.1f}h')}
            {stat_box('Active Volunteers', data["active_volunteers"])}
            {stat_box('Pending Approval', f'{data["pending_count"]} ({data["pending_hours"]:.1f}h)', '#d97706')}
        </div>'''
        if data['top_volunteers']:
            body += f'<div style="font-size:15px;font-weight:700;margin-bottom:8px">Top Volunteers</div>'
            body += table(['Name','Hours','Events'], data['top_volunteers'][:10], ['name','hours','events_count'])
        if data['hours_by_event']:
            body += f'<div style="font-size:15px;font-weight:700;margin:20px 0 8px">Hours by Event</div>'
            body += table(['Event','Hours','Volunteers'], data['hours_by_event'], ['event','hours','vol_count'])
        if data['new_volunteers']:
            body += f'<div style="font-size:15px;font-weight:700;margin:20px 0 8px">New Volunteers ({len(data["new_volunteers"])})</div>'
            body += table(['Name','Email'], data['new_volunteers'], ['name','email'])
        if data['lapsed_volunteers']:
            body += f'<div style="font-size:15px;font-weight:700;margin:20px 0 8px">⚠️ Lapsed Volunteers (60+ days)</div>'
            body += table(['Name','Last Active','Email'], data['lapsed_volunteers'], ['name','last_date','email'])

    elif report_type == 'top_volunteers':
        title = f'Top Volunteers — {params.get("start_date","")} to {params.get("end_date","")}'
        body = table(['#','Name','Hours','Events','Last Active'],
            [{**r, '#': i+1} for i,r in enumerate(data)],
            ['#','name','total_hours','events_count','last_date'])

    elif report_type == 'lapsed_volunteers':
        title = f'Lapsed Volunteers ({params.get("days",90)}+ days inactive)'
        body = f'<p style="font-size:14px;color:#5f5e5a;margin-bottom:16px">{len(data)} volunteer{"s" if len(data)!=1 else ""} with no hours in the last {params.get("days",90)} days.</p>'
        body += table(['Name','Last Active','Total Hours','Email'], data, ['name','last_date','total_hours_ever','email'])

    elif report_type == 'hours_by_event':
        title = f'Hours by Event — {params.get("start_date","")} to {params.get("end_date","")}'
        body = table(['Event','Total Hours','Volunteers','Entries'], data, ['event','total_hours','volunteer_count','entry_count'])

    else:
        title = 'Volunteer Report'
        body = '<p>Report data</p>'

    return header.format(title) + body + footer, title

@app.route('/api/reports/run', methods=['POST'])
def run_report():
    err = require_auth()
    if err: return err
    d = request.json or {}
    rtype = d.get('report_type')
    params = d.get('params', {})

    import datetime as _dt
    today = _dt.date.today()
    last_month = (today.replace(day=1) - _dt.timedelta(days=1))

    if rtype == 'monthly_recap':
        year  = params.get('year', last_month.year)
        month = params.get('month', last_month.month)
        data  = build_volunteer_monthly_report(year, month)

    elif rtype == 'top_volunteers':
        start = params.get('start_date', today.replace(day=1).isoformat())
        end   = params.get('end_date', today.isoformat())
        limit = params.get('limit', 50)
        data  = build_top_volunteers_report(start, end, limit)

    elif rtype == 'lapsed_volunteers':
        days = params.get('days', 90)
        data = build_lapsed_volunteers_report(days)

    elif rtype == 'hours_by_event':
        start = params.get('start_date', today.replace(day=1).isoformat())
        end   = params.get('end_date', today.isoformat())
        data  = build_hours_by_event_report(start, end)

    else:
        return jsonify({'error': 'Unknown report type'}), 400

    return jsonify({'ok': True, 'data': data, 'report_type': rtype, 'params': params})

@app.route('/api/reports/export-csv', methods=['POST'])
def export_report_csv():
    err = require_auth()
    if err: return err
    d = request.json or {}
    rtype  = d.get('report_type')
    data   = d.get('data', [])
    params = d.get('params', {})

    import csv, io
    output = io.StringIO()
    writer = csv.writer(output)

    col_maps = {
        'monthly_recap':    None,  # handled specially
        'top_volunteers':   (['Name','Email','Phone','Total Hours','Events','First Date','Last Date'],
                             ['name','email','phone','total_hours','events_count','first_date','last_date']),
        'lapsed_volunteers':(['Name','Email','Phone','Last Active','Total Hours Ever'],
                             ['name','email','phone','last_date','total_hours_ever']),
        'hours_by_event':   (['Event','Total Hours','Volunteers','Entries','First Date','Last Date'],
                             ['event','total_hours','volunteer_count','entry_count','first_date','last_date']),
    }

    if rtype == 'monthly_recap' and isinstance(data, dict):
        writer.writerow([f'{data.get("month","")} {data.get("year","")} — Volunteer Monthly Recap'])
        writer.writerow([])
        writer.writerow(['Metric','Value'])
        writer.writerow(['Total Hours', data.get('total_hours',0)])
        writer.writerow(['Active Volunteers', data.get('active_volunteers',0)])
        writer.writerow(['Pending Hours', data.get('pending_hours',0)])
        writer.writerow([])
        writer.writerow(['TOP VOLUNTEERS'])
        writer.writerow(['Name','Hours','Events'])
        for r in data.get('top_volunteers',[]):
            writer.writerow([r.get('name',''), r.get('hours',''), r.get('events_count','')])
        writer.writerow([])
        writer.writerow(['HOURS BY EVENT'])
        writer.writerow(['Event','Hours','Volunteers'])
        for r in data.get('hours_by_event',[]):
            writer.writerow([r.get('event',''), r.get('hours',''), r.get('vol_count','')])
        writer.writerow([])
        writer.writerow(['NEW VOLUNTEERS'])
        writer.writerow(['Name','Email'])
        for r in data.get('new_volunteers',[]):
            writer.writerow([r.get('name',''), r.get('email','')])
    elif rtype in col_maps and col_maps[rtype] and isinstance(data, list):
        headers, cols = col_maps[rtype]
        writer.writerow(headers)
        for row in data:
            writer.writerow([row.get(c,'') for c in cols])
    else:
        writer.writerow(['Export not available for this report type'])

    csv_content = output.getvalue()
    from flask import Response
    return Response(csv_content, mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename=rolecall_{rtype}.csv'})

@app.route('/api/reports/send-now', methods=['POST'])
def send_report_now():
    """Manually send a report via email."""
    err = require_auth()
    if err: return err
    d = request.json or {}
    rtype   = d.get('report_type')
    params  = d.get('params', {})
    emails  = d.get('emails', [])

    import datetime as _dt
    today = _dt.date.today()
    last_month = (today.replace(day=1) - _dt.timedelta(days=1))

    if rtype == 'monthly_recap':
        data = build_volunteer_monthly_report(
            params.get('year', last_month.year), params.get('month', last_month.month))
    elif rtype == 'top_volunteers':
        data = build_top_volunteers_report(
            params.get('start_date', today.replace(day=1).isoformat()),
            params.get('end_date', today.isoformat()))
    elif rtype == 'lapsed_volunteers':
        data = build_lapsed_volunteers_report(params.get('days', 90))
    elif rtype == 'hours_by_event':
        data = build_hours_by_event_report(
            params.get('start_date', today.replace(day=1).isoformat()),
            params.get('end_date', today.isoformat()))
    else:
        return jsonify({'error': 'Unknown report type'}), 400

    html, subject = build_report_email_html(rtype, data, params)
    if not emails:
        settings = get_email_settings()
        emails = get_recipient_emails(settings)
    if not emails:
        return jsonify({'error': 'No recipients configured'}), 400

    ok, msg = send_email(emails, subject, html)
    if ok: return jsonify({'ok': True, 'sent_to': emails})
    return jsonify({'error': msg or 'Failed to send'}), 500

# ── Scheduled Reports ──
@app.route('/api/scheduled-reports')
def get_scheduled_reports():
    err = require_auth()
    if err: return err
    conn = get_db()
    rows = fetchall(conn, 'SELECT * FROM scheduled_reports ORDER BY name')
    conn.close()
    return jsonify(rows)

@app.route('/api/scheduled-reports', methods=['POST'])
def create_scheduled_report():
    err = require_auth()
    if err: return err
    d = request.json
    rid = str(uuid.uuid4())
    conn = get_db()
    # Calculate next send date
    import datetime as _dt
    next_send = _compute_next_send(d.get('cadence','monthly'), d.get('send_day',1))
    execute(conn, '''INSERT INTO scheduled_reports
        (id,name,report_type,cadence,send_day,recipient_user_ids,recipient_emails,params,is_active,next_send_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
        (rid, d.get('name',''), d['report_type'], d.get('cadence','monthly'),
         d.get('send_day',1), json.dumps(d.get('recipient_user_ids',[])),
         d.get('recipient_emails',''), json.dumps(d.get('params',{})),
         d.get('is_active',True), next_send))
    conn.commit()
    row = fetchone(conn, 'SELECT * FROM scheduled_reports WHERE id=%s', (rid,))
    conn.close()
    return jsonify(row)

@app.route('/api/scheduled-reports/<rid>', methods=['PUT'])
def update_scheduled_report(rid):
    err = require_auth()
    if err: return err
    d = request.json
    conn = get_db()
    next_send = _compute_next_send(d.get('cadence','monthly'), d.get('send_day',1))
    execute(conn, '''UPDATE scheduled_reports SET name=%s,report_type=%s,cadence=%s,
        send_day=%s,recipient_user_ids=%s,recipient_emails=%s,params=%s,is_active=%s,next_send_at=%s WHERE id=%s''',
        (d.get('name',''), d['report_type'], d.get('cadence','monthly'),
         d.get('send_day',1), json.dumps(d.get('recipient_user_ids',[])),
         d.get('recipient_emails',''), json.dumps(d.get('params',{})),
         d.get('is_active',True), next_send, rid))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/scheduled-reports/<rid>', methods=['DELETE'])
def delete_scheduled_report(rid):
    err = require_auth()
    if err: return err
    conn = get_db()
    execute(conn, 'DELETE FROM scheduled_reports WHERE id=%s', (rid,))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

def _compute_next_send(cadence, send_day):
    import datetime as _dt
    today = _dt.date.today()
    day = max(1, min(28, int(send_day or 1)))
    if cadence == 'monthly':
        # Next month on send_day
        if today.day < day:
            try: return _dt.date(today.year, today.month, day).isoformat()
            except Exception: pass
        # Move to next month
        nm = today.month % 12 + 1
        ny = today.year + (1 if today.month == 12 else 0)
        try: return _dt.date(ny, nm, day).isoformat()
        except Exception: return None
    elif cadence == 'weekly':
        # Next occurrence of send_day (0=Mon)
        days_ahead = (int(send_day) - today.weekday()) % 7
        if days_ahead == 0: days_ahead = 7
        return (today + _dt.timedelta(days=days_ahead)).isoformat()
    return None

# Cron-style scheduler — called on every request, fires due reports
_last_cron_check = [None]
def maybe_run_scheduled_reports():
    import datetime as _dt
    now = _dt.datetime.now()
    last = _last_cron_check[0]
    # Only check once per hour — use total_seconds() not .seconds
    if last and (now - last).total_seconds() < 3600: return
    _last_cron_check[0] = now
    try:
        conn = get_db()
        due = fetchall(conn, """SELECT * FROM scheduled_reports
            WHERE is_active=TRUE AND next_send_at IS NOT NULL
            AND next_send_at::date <= CURRENT_DATE""")
        conn.close()
        for r in due:
            try:
                _fire_scheduled_report(r)
            except Exception as e:
                app.logger.error(f'Scheduled report error {r["id"]}: {e}')
    except Exception as e:
        app.logger.error(f'Cron check error: {e}')

def _fire_scheduled_report(r):
    import datetime as _dt
    rtype  = r['report_type']
    params = json.loads(r.get('params') or '{}')
    today  = _dt.date.today()
    lm     = (today.replace(day=1) - _dt.timedelta(days=1))

    if rtype == 'monthly_recap':
        data = build_volunteer_monthly_report(lm.year, lm.month)
    elif rtype == 'top_volunteers':
        start = params.get('start_date', lm.replace(day=1).isoformat())
        end   = params.get('end_date', lm.isoformat())
        data  = build_top_volunteers_report(start, end)
    elif rtype == 'lapsed_volunteers':
        data = build_lapsed_volunteers_report(params.get('days', 90))
    elif rtype == 'hours_by_event':
        start = params.get('start_date', lm.replace(day=1).isoformat())
        end   = params.get('end_date', lm.isoformat())
        data  = build_hours_by_event_report(start, end)
    else:
        return

    html, subject = build_report_email_html(rtype, data, params)

    # Build recipient list
    emails = []
    try:
        uids = json.loads(r.get('recipient_user_ids') or '[]')
        if uids:
            conn = get_db()
            placeholders = ','.join(['%s']*len(uids))
            users = fetchall(conn, f'SELECT email FROM users WHERE id IN ({placeholders})', tuple(uids))
            conn.close()
            emails = [u['email'] for u in users if u.get('email')]
    except Exception: pass
    raw = r.get('recipient_emails','')
    if raw:
        emails += [e.strip() for e in raw.split(',') if e.strip()]
    emails = list(set(emails))
    if not emails: return

    ok, _ = send_email(emails, subject, html)
    if ok:
        # Update last sent and compute next send
        next_send = _compute_next_send(r['cadence'], r['send_day'])
        conn = get_db()
        execute(conn, 'UPDATE scheduled_reports SET last_sent_at=NOW(), next_send_at=%s WHERE id=%s',
                (next_send, r['id']))
        conn.commit(); conn.close()

# Hook cron into every request
@app.after_request
def after_request_cron(response):
    try: maybe_run_scheduled_reports()
    except Exception: pass
    return response

# ─────────────────────────────────────────────
#  PRODUCTION SIGN-IN (ELIC Kiosk)
# ─────────────────────────────────────────────

@app.route('/api/kiosk/production-roster/<event_id>')
def kiosk_production_roster(event_id):
    conn = get_db()
    evt = fetchone(conn, 'SELECT * FROM events WHERE id=%s', (event_id,))
    if not evt:
        conn.close()
        return jsonify({'error': 'Event not found'}), 404
    prod_id = evt.get('production_id')
    if not prod_id:
        conn.close()
        return jsonify({'members': [], 'production_id': None})
    members = fetchall(conn, '''
        SELECT pm.id as member_id, pm.volunteer_id, pm.role, pm.department, pm.status,
               v.name as volunteer_name,
               pa.id as attendance_id, pa.signed_in_at,
               CASE WHEN pa.id IS NOT NULL AND pa.signed_out_at IS NULL THEN TRUE ELSE FALSE END as signed_in
        FROM production_members pm
        JOIN volunteers v ON pm.volunteer_id=v.id
        LEFT JOIN prod_attendance pa
            ON pa.volunteer_id=pm.volunteer_id AND pa.event_id=%s AND pa.signed_out_at IS NULL
        WHERE pm.production_id=%s AND pm.status != 'dropped'
        ORDER BY pm.department, v.name
    ''', (event_id, prod_id))
    conn.close()
    return jsonify({'ok': True, 'members': members, 'production_id': prod_id})

@app.route('/api/kiosk/production-signin', methods=['POST'])
def kiosk_production_signin():
    d = request.json or {}
    vol_id   = d.get('volunteer_id')
    event_id = d.get('event_id')
    if not vol_id or not event_id:
        return jsonify({'error': 'Missing volunteer_id or event_id'}), 400
    conn = get_db()
    existing = fetchone(conn,
        'SELECT id FROM prod_attendance WHERE volunteer_id=%s AND event_id=%s AND signed_out_at IS NULL',
        (vol_id, event_id))
    if existing:
        conn.close()
        return jsonify({'error': 'Already signed in', 'attendance_id': existing['id']})
    aid = str(uuid.uuid4())
    execute(conn, 'INSERT INTO prod_attendance (id,volunteer_id,event_id,signed_in_at) VALUES (%s,%s,%s,NOW())',
        (aid, vol_id, event_id))
    conn.commit(); conn.close()
    return jsonify({'ok': True, 'attendance_id': aid})

@app.route('/api/kiosk/production-signout', methods=['POST'])
def kiosk_production_signout():
    d = request.json or {}
    att_id   = d.get('attendance_id')
    vol_id   = d.get('volunteer_id')
    event_id = d.get('event_id')
    role     = d.get('role', '')
    if not vol_id or not event_id:
        return jsonify({'error': 'Missing required fields'}), 400
    conn = get_db()
    att = None
    if att_id:
        att = fetchone(conn, 'SELECT * FROM prod_attendance WHERE id=%s', (att_id,))
    if not att:
        att = fetchone(conn,
            'SELECT * FROM prod_attendance WHERE volunteer_id=%s AND event_id=%s AND signed_out_at IS NULL',
            (vol_id, event_id))
    if not att:
        conn.close()
        return jsonify({'error': 'No active sign-in found'}), 404

    # Use full event duration, not elapsed time
    evt = fetchone(conn, 'SELECT * FROM events WHERE id=%s', (event_id,))
    evt_name = evt['name'] if evt else 'Production'
    event_hours = None
    if evt and evt.get('start_time') and evt.get('end_time'):
        try:
            from datetime import datetime as _dt
            fmt = '%H:%M'
            start = _dt.strptime(str(evt['start_time'])[:5], fmt)
            end   = _dt.strptime(str(evt['end_time'])[:5], fmt)
            diff  = (end - start).seconds / 3600
            if diff > 0:
                event_hours = round(diff, 2)
        except Exception:
            pass
    # Fall back to elapsed time if event has no start/end times set
    if not event_hours:
        time_row = fetchone(conn,
            'SELECT EXTRACT(EPOCH FROM (NOW() - signed_in_at)) as secs FROM prod_attendance WHERE id=%s',
            (att['id'],))
        elapsed_secs  = float(time_row['secs']) if time_row and time_row['secs'] else 0
        event_hours   = round(max(0.25, elapsed_secs / 3600), 2)
        hours_source  = 'elapsed time (no event times set)'
    else:
        hours_source = f'full event duration ({evt.get("start_time","")}–{evt.get("end_time","")})'

    today_row = fetchone(conn, 'SELECT CURRENT_DATE::text as today')
    today = today_row['today'] if today_row else __import__('datetime').date.today().isoformat()

    execute(conn, 'UPDATE prod_attendance SET signed_out_at=NOW() WHERE id=%s', (att['id'],))
    pid = str(uuid.uuid4())
    execute(conn, '''INSERT INTO pending_hours (id,volunteer_id,event,event_id,date,hours,role,notes,status)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'pending')''',
        (pid, vol_id, evt_name, event_id, today, event_hours, role or '',
         f'Production member — {hours_source}'))
    conn.commit(); conn.close()
    return jsonify({'ok': True, 'hours': event_hours, 'hours_source': hours_source})



# ─────────────────────────────────────────────
#  KIOSK — OPEN/CLOSE EVENT & YOUTH
# ─────────────────────────────────────────────

@app.route('/api/kiosk/open-event-checklist', methods=['POST'])
def kiosk_open_event():
    d = request.json or {}
    elic_id  = d.get('elic_id')
    event_id = d.get('event_id')
    responses = d.get('responses', [])
    if not elic_id or not event_id:
        return jsonify({'error': 'Missing elic_id or event_id'}), 400
    conn = get_db()
    try:
        # Log the opening
        log_id = str(uuid.uuid4())
        execute(conn, '''INSERT INTO event_logs (id,event_id,elic_id,action,notes)
            VALUES (%s,%s,%s,'open','Event opened via kiosk')''', (log_id, event_id, elic_id))
        # Save checklist responses
        for r in responses:
            rid = str(uuid.uuid4())
            execute(conn, '''INSERT INTO event_checklist_responses
                (id,event_log_id,checklist_item_id,label,item_type,response)
                VALUES (%s,%s,%s,%s,%s,%s)''',
                (rid, log_id, r.get('item_id',''), r.get('label',''),
                 r.get('type','checkbox'), str(r.get('response',''))))
        # Mark event as open
        execute(conn, "UPDATE events SET status='open' WHERE id=%s", (event_id,))
        conn.commit(); conn.close()
        return jsonify({'ok': True, 'log_id': log_id})
    except Exception as e:
        conn.rollback(); conn.close()
        return jsonify({'error': str(e)}), 500

@app.route('/api/kiosk/close-event', methods=['POST'])
def kiosk_close_event():
    d = request.json or {}
    elic_id  = d.get('elic_id')
    event_id = d.get('event_id')
    responses = d.get('responses', [])
    if not elic_id or not event_id:
        return jsonify({'error': 'Missing elic_id or event_id'}), 400
    conn = get_db()
    try:
        log_id = str(uuid.uuid4())
        execute(conn, '''INSERT INTO event_logs (id,event_id,elic_id,action,notes)
            VALUES (%s,%s,%s,'close','Event closed via kiosk')''', (log_id, event_id, elic_id))
        for r in responses:
            rid = str(uuid.uuid4())
            execute(conn, '''INSERT INTO event_checklist_responses
                (id,event_log_id,checklist_item_id,label,item_type,response)
                VALUES (%s,%s,%s,%s,%s,%s)''',
                (rid, log_id, r.get('item_id',''), r.get('label',''),
                 r.get('type','checkbox'), str(r.get('response',''))))
        execute(conn, "UPDATE events SET status='closed' WHERE id=%s", (event_id,))
        # Auto-approve pending kiosk hours for this event
        pending = fetchall(conn,
            "SELECT * FROM pending_hours WHERE event_id=%s AND status='pending'", (event_id,))
        for ph in pending:
            hid = str(uuid.uuid4())
            try:
                execute(conn, '''INSERT INTO hours (id,volunteer_id,event,event_id,date,hours,role,notes)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s)''',
                    (hid, ph['volunteer_id'], ph['event'], ph['event_id'],
                     ph['date'], ph['hours'], ph.get('role',''), ph.get('notes','')))
            except Exception:
                pass
        execute(conn, "UPDATE pending_hours SET status='approved' WHERE event_id=%s AND status='pending'", (event_id,))
        conn.commit()
        # Send checklist report email
        try:
            s = get_email_settings()
            recipients = get_recipient_emails(s)
            if recipients:
                evt = fetchone(conn, 'SELECT name FROM events WHERE id=%s', (event_id,))
                evt_name = evt['name'] if evt else event_id
                now_str = __import__('datetime').datetime.now().strftime('%I:%M %p')
                # Build checklist summary
                cl_rows = ''
                for r in responses:
                    val = str(r.get('response',''))
                    if val == 'true': val_str = '✅ Done'
                    elif val == 'false': val_str = '❌ Not Done'
                    else: val_str = val or '—'
                    cl_rows += f'<tr><td style="padding:6px 12px;border-bottom:1px solid #eee">{r.get("label","")}</td><td style="padding:6px 12px;border-bottom:1px solid #eee;font-weight:600">{val_str}</td></tr>'
                # Build hours summary
                hrs_rows = ''
                if pending:
                    for ph in pending:
                        vol = fetchone(conn, 'SELECT name FROM volunteers WHERE id=%s', (ph['volunteer_id'],))
                        vname = vol['name'] if vol else 'Unknown'
                        hrs_rows += f'<tr><td style="padding:6px 12px;border-bottom:1px solid #eee">{vname}</td><td style="padding:6px 12px;border-bottom:1px solid #eee;font-weight:600">{ph["hours"]}h</td><td style="padding:6px 12px;border-bottom:1px solid #eee;color:#16a34a">Auto-approved</td></tr>'
                body = f'''<div style="font-family:sans-serif;max-width:600px;margin:0 auto">
                    <h2 style="color:#145466">🔒 Event Closed: {evt_name}</h2>
                    <p>Closed at <strong>{now_str}</strong> via ELIC kiosk.</p>
                    {f"""<h3 style="margin-top:24px">✅ Closing Checklist</h3>
                    <table style="width:100%;border-collapse:collapse;border:1px solid #eee;border-radius:8px;overflow:hidden">
                    <thead><tr style="background:#f0fdf4"><th style="padding:8px 12px;text-align:left">Item</th><th style="padding:8px 12px;text-align:left">Response</th></tr></thead>
                    <tbody>{cl_rows}</tbody></table>""" if cl_rows else "<p><em>No checklist items recorded.</em></p>"}
                    {f"""<h3 style="margin-top:24px">⏱ Hours Auto-Approved ({len(pending)} volunteer{'s' if len(pending)!=1 else ''})</h3>
                    <table style="width:100%;border-collapse:collapse;border:1px solid #eee;border-radius:8px;overflow:hidden">
                    <thead><tr style="background:#eff6ff"><th style="padding:8px 12px;text-align:left">Volunteer</th><th style="padding:8px 12px;text-align:left">Hours</th><th style="padding:8px 12px;text-align:left">Status</th></tr></thead>
                    <tbody>{hrs_rows}</tbody></table>""" if pending else "<p><em>No hours recorded for this event.</em></p>"}
                </div>'''
                send_email(recipients, f'Event Closed: {evt_name}', body)
        except Exception as e:
            app.logger.error(f'close-event email error: {e}')
        conn.close()
        return jsonify({'ok': True, 'log_id': log_id})
    except Exception as e:
        conn.rollback(); conn.close()
        return jsonify({'error': str(e)}), 500

@app.route('/api/kiosk/youth')
def kiosk_get_youth():
    """Get youth participants for ELIC youth sign-in."""
    conn = get_db()
    # Get all active youth for today's events linked to this kiosk session
    youth = fetchall(conn, '''
        SELECT yp.id, yp.first_name, yp.last_name, yp.dob,
               ysi.id as sign_in_id, ysi.signed_in_at, ysi.signed_out_at
        FROM youth_participants yp
        LEFT JOIN youth_sign_ins ysi ON ysi.youth_id=yp.id
            AND ysi.signed_in_at >= NOW() - INTERVAL '12 hours'
            AND ysi.signed_out_at IS NULL
        WHERE yp.status='active'
        ORDER BY yp.last_name, yp.first_name''')
    conn.close()
    return jsonify(youth)

@app.route('/api/kiosk/authorized-pickups/<yid>')
def kiosk_authorized_pickups(yid):
    conn = get_db()
    pickups = fetchall(conn,
        'SELECT * FROM youth_authorized_pickups WHERE youth_id=%s ORDER BY priority', (yid,))
    conn.close()
    return jsonify(pickups)

@app.route('/api/kiosk/youth-for-event/<event_id>')
def kiosk_youth_for_event(event_id):
    """Get youth enrolled in the production or program linked to this event."""
    conn = get_db()
    evt = fetchone(conn, 'SELECT production_id, program_id FROM events WHERE id=%s', (event_id,))
    if not evt:
        conn.close()
        return jsonify([])

    youth = []

    if evt.get('production_id'):
        # Rising Stars production — get enrolled cast
        youth = fetchall(conn, '''
            SELECT yp.id, yp.first_name, yp.last_name, yp.dob,
                   ypm.role,
                   ysi.id as sign_in_id, ysi.signed_in_at, ysi.signed_out_at
            FROM youth_production_members ypm
            JOIN youth_participants yp ON ypm.youth_id=yp.id
            LEFT JOIN youth_sign_ins ysi ON ysi.youth_id=yp.id
                AND ysi.event_id=%s AND ysi.signed_out_at IS NULL
            WHERE ypm.production_id=%s
            ORDER BY yp.last_name, yp.first_name''', (event_id, evt['production_id']))

    elif evt.get('program_id'):
        # Youth program — get enrolled participants
        youth = fetchall(conn, '''
            SELECT yp.id, yp.first_name, yp.last_name, yp.dob,
                   NULL as role,
                   ysi.id as sign_in_id, ysi.signed_in_at, ysi.signed_out_at
            FROM youth_program_enrollments ype
            JOIN youth_participants yp ON ype.youth_id=yp.id
            LEFT JOIN youth_sign_ins ysi ON ysi.youth_id=yp.id
                AND ysi.event_id=%s AND ysi.signed_out_at IS NULL
            WHERE ype.program_id=%s
            ORDER BY yp.last_name, yp.first_name''', (event_id, evt['program_id']))

    conn.close()
    return jsonify(youth)


# ─────────────────────────────────────────────
#  YOUTH SIGN-INS (ELIC Kiosk)
# ─────────────────────────────────────────────

@app.route('/api/youth-sign-ins')
def get_youth_sign_ins():
    conn = get_db()
    event_id = request.args.get('event_id')
    if event_id:
        rows = fetchall(conn, '''
            SELECT ysi.*, yp.first_name, yp.last_name,
                   yp.first_name||\' \'||yp.last_name as youth_name
            FROM youth_sign_ins ysi
            JOIN youth_participants yp ON ysi.youth_id=yp.id
            WHERE ysi.event_id=%s
            ORDER BY ysi.signed_in_at DESC''', (event_id,))
    else:
        rows = fetchall(conn, '''
            SELECT ysi.*, yp.first_name, yp.last_name,
                   yp.first_name||\' \'||yp.last_name as youth_name
            FROM youth_sign_ins ysi
            JOIN youth_participants yp ON ysi.youth_id=yp.id
            WHERE ysi.signed_in_at >= NOW() - INTERVAL '12 hours'
            ORDER BY ysi.signed_in_at DESC''')
    conn.close()
    return jsonify(rows)

@app.route('/api/youth-sign-ins', methods=['POST'])
def create_youth_sign_in():
    d = request.json or {}
    yid          = d.get('youth_id')
    event_id     = d.get('event_id')
    signed_in_by = d.get('signed_in_by', '')
    if not yid: return jsonify({'error': 'Missing youth_id'}), 400
    conn = get_db()
    # Check not already signed in
    existing = fetchone(conn,
        'SELECT ysi.*, y.first_name, y.last_name FROM youth_sign_ins ysi JOIN youth_participants y ON ysi.youth_id=y.id WHERE ysi.youth_id=%s AND ysi.event_id=%s AND ysi.signed_out_at IS NULL',
        (yid, event_id))
    if existing:
        conn.close()
        return jsonify(existing)  # return existing record, not error
    sid = str(uuid.uuid4())
    execute(conn, '''INSERT INTO youth_sign_ins (id,youth_id,event_id,signed_in_at,signed_in_by)
        VALUES (%s,%s,%s,NOW(),%s)''', (sid, yid, event_id, signed_in_by))
    conn.commit()
    row = fetchone(conn, '''SELECT ysi.*, y.first_name, y.last_name
        FROM youth_sign_ins ysi
        JOIN youth_participants y ON ysi.youth_id=y.id
        WHERE ysi.id=%s''', (sid,))
    conn.close()
    return jsonify(row)

@app.route('/api/youth-sign-ins/<sid>/sign-out', methods=['POST'])
def youth_sign_out(sid):
    d = request.json or {}
    signed_out_by = d.get('signed_out_by', '')
    conn = get_db()
    execute(conn, 'UPDATE youth_sign_ins SET signed_out_at=NOW(), signed_out_by=%s WHERE id=%s',
        (signed_out_by, sid))
    conn.commit()
    row = fetchone(conn, '''SELECT ysi.*, y.first_name, y.last_name
        FROM youth_sign_ins ysi
        JOIN youth_participants y ON ysi.youth_id=y.id
        WHERE ysi.id=%s''', (sid,))
    conn.close()
    return jsonify(row or {'ok': True, 'id': sid, 'signed_out_at': 'now'})


# ─────────────────────────────────────────────
#  CARPOOLS
# ─────────────────────────────────────────────

def _gen_carpool_code():
    import random
    words = ['BLUE','RED','STAR','SUN','MOON','OAK','FOX','BAY','SKY','ZEN','ACE','ARC']
    return random.choice(words) + str(random.randint(10,99))

@app.route('/api/carpools')
def get_carpools():
    err = require_auth()
    if err: return err
    event_id = request.args.get('event_id')
    conn = get_db()
    if event_id:
        rows = fetchall(conn, 'SELECT c.*, COUNT(cm.id) as member_count FROM carpools c LEFT JOIN carpool_members cm ON cm.carpool_id=c.id WHERE c.event_id=%s GROUP BY c.id ORDER BY c.name', (event_id,))
    else:
        rows = fetchall(conn, """SELECT c.*, COUNT(cm.id) as member_count,
            e.name as event_name, e.event_date
            FROM carpools c
            LEFT JOIN carpool_members cm ON cm.carpool_id=c.id
            LEFT JOIN events e ON c.event_id=e.id
            GROUP BY c.id, e.name, e.event_date
            ORDER BY e.event_date DESC NULLS LAST, c.name""")
    for row in rows:
        row['members'] = fetchall(conn, 'SELECT cm.*, y.first_name, y.last_name FROM carpool_members cm JOIN youth_participants y ON cm.youth_id=y.id WHERE cm.carpool_id=%s ORDER BY y.last_name, y.first_name', (row['id'],))
    conn.close()
    return jsonify(rows)

@app.route('/api/carpools', methods=['POST'])
def create_carpool():
    err = require_auth()
    if err: return err
    d = request.json or {}
    cid = str(uuid.uuid4())
    conn = get_db()
    code = _gen_carpool_code()
    for _ in range(10):
        if not fetchone(conn, 'SELECT id FROM carpools WHERE code=%s', (code,)): break
        code = _gen_carpool_code()
    execute(conn, "INSERT INTO carpools (id,event_id,name,driver_name,driver_phone,code,max_seats,notes,status) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'open')",
        (cid, d.get('event_id'), d.get('name',''), d['driver_name'], d.get('driver_phone',''), code, d.get('max_seats',6), d.get('notes','')))
    conn.commit()
    row = fetchone(conn, 'SELECT * FROM carpools WHERE id=%s', (cid,))
    row['members'] = []; row['member_count'] = 0
    conn.close()
    return jsonify(row)

@app.route('/api/carpools/<cid>', methods=['PUT'])
def update_carpool(cid):
    err = require_auth()
    if err: return err
    d = request.json or {}
    conn = get_db()
    execute(conn, 'UPDATE carpools SET name=%s,driver_name=%s,driver_phone=%s,max_seats=%s,notes=%s,status=%s WHERE id=%s',
        (d.get('name',''), d['driver_name'], d.get('driver_phone',''), d.get('max_seats',6), d.get('notes',''), d.get('status','open'), cid))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/carpools/<cid>', methods=['DELETE'])
def delete_carpool(cid):
    err = require_auth()
    if err: return err
    conn = get_db()
    execute(conn, 'DELETE FROM carpools WHERE id=%s', (cid,))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/carpools/<cid>/members', methods=['POST'])
def add_carpool_member(cid):
    err = require_auth()
    if err: return err
    d = request.json or {}
    mid = str(uuid.uuid4())
    conn = get_db()
    execute(conn, "INSERT INTO carpool_members (id,carpool_id,youth_id,added_by,added_via) VALUES (%s,%s,%s,%s,'admin') ON CONFLICT (carpool_id,youth_id) DO NOTHING",
        (mid, cid, d.get('youth_id'), session.get('user_name','')))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/carpools/<cid>/members/<mid>', methods=['DELETE'])
def remove_carpool_member(cid, mid):
    err = require_auth()
    if err: return err
    conn = get_db()
    execute(conn, 'DELETE FROM carpool_members WHERE id=%s AND carpool_id=%s', (mid, cid))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/portal/carpools')
def portal_get_carpools():
    event_id = request.args.get('event_id')
    conn = get_db()
    try:
        if event_id:
            carpools = fetchall(conn, """SELECT c.*, COUNT(cm.id) as member_count
                FROM carpools c LEFT JOIN carpool_members cm ON cm.carpool_id=c.id
                WHERE c.event_id=%s AND c.status='open'
                GROUP BY c.id ORDER BY c.name""", (event_id,))
        else:
            carpools = fetchall(conn, """SELECT c.*, COUNT(cm.id) as member_count,
                e.name as event_name, e.event_date
                FROM carpools c
                LEFT JOIN carpool_members cm ON cm.carpool_id=c.id
                LEFT JOIN events e ON c.event_id=e.id
                WHERE c.status='open'
                AND (e.event_date IS NULL OR e.event_date >= CURRENT_DATE::text)
                GROUP BY c.id, e.name, e.event_date ORDER BY e.name, c.name""")
        for c in carpools:
            c['members'] = fetchall(conn,
                'SELECT cm.id, y.first_name, y.last_name FROM carpool_members cm JOIN youth_participants y ON cm.youth_id=y.id WHERE cm.carpool_id=%s',
                (c['id'],))
        conn.close()
        return jsonify(carpools)
    except Exception as e:
        conn.close()
        app.logger.error(f'portal_get_carpools error: {e}')
        return jsonify([])

@app.route('/api/portal/carpools/join', methods=['POST'])
def portal_join_carpool():
    d = request.json or {}
    code       = (d.get('code') or '').strip().upper()
    carpool_id = (d.get('carpool_id') or '').strip()
    youth_ids  = d.get('youth_ids', [])
    passphrase = (d.get('passphrase') or '').strip().lower()
    if not youth_ids:
        return jsonify({'error': 'At least one child required'}), 400
    conn = get_db()
    # Find carpool by ID or code
    if carpool_id:
        carpool = fetchone(conn, "SELECT * FROM carpools WHERE id=%s AND status='open'", (carpool_id,))
    elif code:
        carpool = fetchone(conn, "SELECT * FROM carpools WHERE UPPER(code)=%s AND status='open'", (code,))
    else:
        conn.close()
        return jsonify({'error': 'Carpool ID or code required'}), 400
    if not carpool:
        conn.close()
        return jsonify({'error': 'Carpool not found or no longer open'}), 404
    added = 0
    for yid in youth_ids:
        mid = str(uuid.uuid4())
        try:
            execute(conn, "INSERT INTO carpool_members (id,carpool_id,youth_id,added_by,added_via) VALUES (%s,%s,%s,%s,'portal') ON CONFLICT (carpool_id,youth_id) DO NOTHING",
                (mid, carpool['id'], yid, passphrase or 'parent'))
            added += 1
        except Exception: pass
    conn.commit()
    carpool = fetchone(conn, 'SELECT * FROM carpools WHERE id=%s', (carpool['id'],))
    carpool['members'] = fetchall(conn, 'SELECT cm.id, cm.youth_id, y.first_name, y.last_name FROM carpool_members cm JOIN youth_participants y ON cm.youth_id=y.id WHERE cm.carpool_id=%s', (carpool['id'],))
    conn.close()
    return jsonify({'ok': True, 'added': added, 'carpool': carpool})

@app.route('/api/pickup/queue')
def pickup_queue():
    conn = get_db()
    try:
        # Only show kids from OPEN events (or signed in within last 8 hours as fallback)
        individuals = fetchall(conn, """
            SELECT ysi.*, y.first_name, y.last_name, e.name as event_name, e.id as event_id
            FROM youth_sign_ins ysi
            JOIN youth_participants y ON ysi.youth_id=y.id
            LEFT JOIN events e ON ysi.event_id=e.id
            WHERE ysi.signed_out_at IS NULL
            AND (
                (e.status = 'open')
                OR (ysi.event_id IS NULL AND ysi.signed_in_at >= NOW() - INTERVAL '8 hours')
            )
            AND NOT EXISTS (
                SELECT 1 FROM carpool_members cm
                JOIN carpools cp ON cm.carpool_id=cp.id
                WHERE cm.youth_id=ysi.youth_id AND cp.event_id=ysi.event_id
            )
            ORDER BY e.name, y.last_name, y.first_name
        """)
        # Also include recently signed-out kids (last 2 hours) so picked-up column updates
        signed_out = fetchall(conn, """
            SELECT ysi.*, y.first_name, y.last_name, e.name as event_name, e.id as event_id
            FROM youth_sign_ins ysi
            JOIN youth_participants y ON ysi.youth_id=y.id
            LEFT JOIN events e ON ysi.event_id=e.id
            WHERE ysi.signed_out_at IS NOT NULL
            AND ysi.signed_out_at >= NOW() - INTERVAL '2 hours'
            AND NOT EXISTS (
                SELECT 1 FROM carpool_members cm
                JOIN carpools cp ON cm.carpool_id=cp.id
                WHERE cm.youth_id=ysi.youth_id AND cp.event_id=ysi.event_id
            )
            ORDER BY ysi.signed_out_at DESC
        """)
        individuals = individuals + signed_out
    except Exception as e:
        app.logger.error(f'pickup_queue individuals error: {e}')
        individuals = []

    try:
        carpools_rows = fetchall(conn, """
            SELECT cp.id as carpool_id, cp.name as carpool_name, cp.code as carpool_code,
                   cp.driver_name, cp.driver_phone, cp.event_id, e.name as event_name,
                   COUNT(DISTINCT CASE WHEN ysi.signed_out_at IS NULL THEN ysi.id END) as signed_in_count,
                   COUNT(DISTINCT cm.id) as total_members
            FROM carpools cp
            JOIN events e ON cp.event_id=e.id
            LEFT JOIN carpool_members cm ON cm.carpool_id=cp.id
            LEFT JOIN youth_sign_ins ysi ON ysi.youth_id=cm.youth_id
                AND ysi.event_id=cp.event_id
            WHERE e.status = 'open'
            GROUP BY cp.id, cp.name, cp.code, cp.driver_name, cp.driver_phone, cp.event_id, e.name
            HAVING COUNT(DISTINCT cm.id) > 0
            ORDER BY cp.name
        """)
        for cp in carpools_rows:
            cp['kids'] = fetchall(conn, """
                SELECT ysi.id as sign_in_id, ysi.youth_id, ysi.signed_out_at,
                       y.first_name, y.last_name, cm.id as member_id
                FROM carpool_members cm
                JOIN youth_participants y ON cm.youth_id=y.id
                LEFT JOIN youth_sign_ins ysi ON ysi.youth_id=cm.youth_id
                    AND ysi.event_id=%s
                WHERE cm.carpool_id=%s
                ORDER BY y.last_name, y.first_name
            """, (cp['event_id'], cp['carpool_id']))
    except Exception as e:
        app.logger.error(f'pickup_queue carpools error: {e}')
        carpools_rows = []

    conn.close()
    return jsonify({'individuals': individuals, 'carpools': carpools_rows})

@app.route('/api/pickup/clear', methods=['POST'])
def pickup_clear():
    """Sign out everyone currently waiting — used for manual clear at end of day."""
    err = require_auth()
    if err: return err
    conn = get_db()
    execute(conn, """UPDATE youth_sign_ins SET signed_out_at=NOW(), signed_out_by='staff-clear'
        WHERE signed_out_at IS NULL""")
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

@app.route('/api/pickup/cleanup', methods=['POST'])
def pickup_cleanup():
    """Clear orphaned sign-ins — kids stuck in deleted/closed events."""
    err = require_auth()
    if err: return err
    conn = get_db()
    # Sign out anyone in a closed or deleted event who is still marked as signed in
    result = fetchone(conn, """
        SELECT COUNT(*) as count FROM youth_sign_ins ysi
        LEFT JOIN events e ON ysi.event_id=e.id
        WHERE ysi.signed_out_at IS NULL
        AND (e.id IS NULL OR e.status != 'open')
    """)
    count = result['count'] if result else 0
    execute(conn, """UPDATE youth_sign_ins SET signed_out_at=NOW(), signed_out_by='auto-cleanup'
        WHERE signed_out_at IS NULL
        AND event_id IN (
            SELECT id FROM events WHERE status != 'open'
        )""")
    execute(conn, """UPDATE youth_sign_ins SET signed_out_at=NOW(), signed_out_by='auto-cleanup'
        WHERE signed_out_at IS NULL
        AND event_id IS NOT NULL
        AND event_id NOT IN (SELECT id FROM events)""")
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'cleared': count})

@app.route('/api/pickup/carpool-signout', methods=['POST'])
def carpool_signout():
    d = request.json or {}
    carpool_id    = d.get('carpool_id')
    event_id      = d.get('event_id')
    signed_out_by = d.get('signed_out_by', '')
    if not carpool_id or not event_id:
        return jsonify({'error': 'Missing fields'}), 400
    conn = get_db()
    execute(conn, 'UPDATE youth_sign_ins SET signed_out_at=NOW(), signed_out_by=%s WHERE youth_id IN (SELECT youth_id FROM carpool_members WHERE carpool_id=%s) AND event_id=%s AND signed_out_at IS NULL',
        (signed_out_by, carpool_id, event_id))
    conn.commit(); conn.close()
    return jsonify({'ok': True})



# ─────────────────────────────────────────────
#  MISSING ROUTES — added by audit
# ─────────────────────────────────────────────

# ── Notifications ──
@app.route('/api/notifications')
def get_notifications():
    err = require_auth()
    if err: return err
    conn = get_db()
    needs_action = []
    activity     = []

    # Pending hours awaiting approval
    try:
        pending = fetchall(conn, '''
            SELECT ph.id, ph.volunteer_id, ph.event, ph.event_id, ph.date,
                   ph.hours, ph.role, ph.notes, ph.status, ph.submitted_at,
                   v.name as volunteer_name
            FROM pending_hours ph
            LEFT JOIN volunteers v ON ph.volunteer_id=v.id
            WHERE ph.status IN (\'pending\',\'pending_review\')
            ORDER BY ph.submitted_at DESC NULLS LAST
            LIMIT 100''')
        for ph in pending:
            is_override = ph.get('status') == 'pending_review'
            needs_action.append({
                'id':    ph['id'],
                'type':  'pending_hours',
                'icon':  '⏱',
                'color': 'amber',
                'title': f'{ph["volunteer_name"] or "A volunteer"} — {ph["hours"]}h',
                'sub':   f'{ph["event"] or "General"} · {ph["date"] or ""}' +
                         (' · Needs review (no event)' if is_override else ''),
                'data':  ph,
            })
    except Exception as e:
        app.logger.warning(f'notifications pending_hours: {e}')

    # Profile update requests
    try:
        profiles = fetchall(conn, '''
            SELECT ph.id, ph.volunteer_id, ph.notes, ph.submitted_at,
                   v.name as volunteer_name
            FROM pending_hours ph
            LEFT JOIN volunteers v ON ph.volunteer_id=v.id
            WHERE ph.status = \'pending_profile\'
            ORDER BY ph.submitted_at DESC NULLS LAST LIMIT 20''')
        for p in profiles:
            needs_action.append({
                'id':    p['id'],
                'type':  'profile_update',
                'icon':  '👤',
                'color': 'blue',
                'title': f'{p["volunteer_name"] or "A volunteer"} — profile update',
                'sub':   'Requested profile change awaiting review',
                'data':  p,
            })
    except Exception as e:
        app.logger.warning(f'notifications profiles: {e}')

    # Recent approved hours (activity feed)
    try:
        recent = fetchall(conn, '''
            SELECT h.*, v.name as volunteer_name
            FROM hours h LEFT JOIN volunteers v ON h.volunteer_id=v.id
            WHERE h.created_at >= NOW() - INTERVAL \'7 days\'
            ORDER BY h.created_at DESC LIMIT 20''')
        for h in recent:
            activity.append({
                'id':    h['id'],
                'type':  'hours_approved',
                'icon':  '✅',
                'color': 'green',
                'title': f'{h["volunteer_name"] or "Volunteer"} — {h["hours"]}h approved',
                'sub':   f'{h["event"] or ""} · {h["date"] or ""}',
                'data':  h,
            })
    except Exception as e:
        app.logger.warning(f'notifications activity: {e}')

    conn.close()
    return jsonify({
        'needs_action':  needs_action,
        'activity':      activity,
        'total_action':  len(needs_action),
    })

# ── Email settings extras ──
@app.route('/api/email-settings/check-events', methods=['POST'])
def email_check_events():
    return jsonify({'ok': True})

@app.route('/api/email-settings/send-report/<rid>')
def email_send_report(rid):
    err = require_auth()
    if err: return err
    return jsonify({'ok': True})

# ── Event waivers ──
@app.route('/api/events/<eid>/waivers', methods=['POST'])
def add_event_waiver(eid):
    err = require_auth()
    if err: return err
    d = request.json or {}
    rid = str(uuid.uuid4())
    conn = get_db()
    execute(conn, 'INSERT INTO event_waivers (id,event_id,waiver_type_id) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING',
        (rid, eid, d.get('waiver_type_id')))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/events/<eid>/waivers/<wid>', methods=['DELETE'])
def remove_event_waiver(eid, wid):
    err = require_auth()
    if err: return err
    conn = get_db()
    execute(conn, 'DELETE FROM event_waivers WHERE id=%s AND event_id=%s', (wid, eid))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

# ── Event ELICs ──
@app.route('/api/events/<eid>/elics', methods=['POST'])
def add_event_elic(eid):
    err = require_auth()
    if err: return err
    d = request.json or {}
    rid = str(uuid.uuid4())
    conn = get_db()
    execute(conn, 'INSERT INTO event_elics (id,event_id,elic_id) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING',
        (rid, eid, d.get('elic_id')))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/events/<eid>/elics/<rid>', methods=['DELETE'])
def remove_event_elic(eid, rid):
    err = require_auth()
    if err: return err
    conn = get_db()
    execute(conn, 'DELETE FROM event_elics WHERE id=%s AND event_id=%s', (rid, eid))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/events/default-elic')
def get_default_elic():
    err = require_auth()
    if err: return err
    prod_id = request.args.get('production_id')
    prog_id = request.args.get('program_id')
    conn = get_db()
    elic = None
    if prod_id:
        p = fetchone(conn, 'SELECT default_elic_id FROM productions WHERE id=%s', (prod_id,))
        if p and p.get('default_elic_id'):
            elic = fetchone(conn, '''SELECT e.*, v.name as volunteer_name FROM elics e
                JOIN volunteers v ON e.volunteer_id=v.id WHERE e.id=%s''', (p['default_elic_id'],))
    conn.close()
    return jsonify({'elic': elic})

# ── Families ──
@app.route('/api/families/<fid>', methods=['DELETE'])
def delete_family(fid):
    err = require_auth()
    if err: return err
    conn = get_db()
    execute(conn, 'DELETE FROM families WHERE id=%s', (fid,))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/families/<fid>/members', methods=['POST'])
def add_family_member(fid):
    err = require_auth()
    if err: return err
    d = request.json or {}
    conn = get_db()
    execute(conn, 'UPDATE youth_participants SET family_id=%s WHERE id=%s', (fid, d.get('youth_id')))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/families/<fid>/members/<yid>', methods=['DELETE'])
def remove_family_member(fid, yid):
    err = require_auth()
    if err: return err
    conn = get_db()
    execute(conn, 'UPDATE youth_participants SET family_id=NULL WHERE id=%s AND family_id=%s', (yid, fid))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

# ── Portal announcements (admin manage) ──
@app.route('/api/portal/announcements', methods=['POST'])
def create_portal_announcement_admin():
    err = require_auth()
    if err: return err
    d = request.json or {}
    aid = str(uuid.uuid4())
    conn = get_db()
    execute(conn, '''INSERT INTO portal_announcements
        (id,production_id,program_id,title,body,status,author_id)
        VALUES (%s,%s,%s,%s,%s,%s,%s)''',
        (aid, d.get('production_id'), d.get('program_id'),
         d.get('title',''), d.get('body',''),
         d.get('status','published'), session.get('user_id','')))
    conn.commit()
    row = fetchone(conn, 'SELECT * FROM portal_announcements WHERE id=%s', (aid,))
    conn.close()
    return jsonify(row)

@app.route('/api/portal/announcements/<aid>', methods=['DELETE'])
def delete_portal_announcement_admin(aid):
    err = require_auth()
    if err: return err
    conn = get_db()
    execute(conn, 'DELETE FROM portal_announcements WHERE id=%s', (aid,))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

# ── Portal files & folders ──
@app.route('/api/portal/files', methods=['POST'])
def create_portal_file():
    err = require_auth()
    if err: return err
    d = request.json or {}
    fid = str(uuid.uuid4())
    conn = get_db()
    execute(conn, '''INSERT INTO portal_files (id,context_type,context_id,name,url,file_type)
        VALUES (%s,%s,%s,%s,%s,%s)''',
        (fid, d.get('context_type','production'), d.get('production_id') or d.get('context_id',''),
         d.get('name',''), d.get('url',''), d.get('file_type','')))
    conn.commit()
    row = fetchone(conn, 'SELECT * FROM portal_files WHERE id=%s', (fid,))
    conn.close()
    return jsonify(row)

@app.route('/api/portal/files/<fid>', methods=['DELETE'])
def delete_portal_file(fid):
    err = require_auth()
    if err: return err
    conn = get_db()
    execute(conn, 'DELETE FROM portal_files WHERE id=%s', (fid,))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/portal/folders')
def get_portal_folders():
    err = require_auth()
    if err: return err
    prod_id = request.args.get('production_id')
    conn = get_db()
    rows = fetchall(conn, 'SELECT * FROM portal_files WHERE context_id=%s ORDER BY name', (prod_id,)) if prod_id else []
    conn.close()
    return jsonify(rows)

@app.route('/api/portal/folders', methods=['POST'])
def create_portal_folder():
    err = require_auth()
    if err: return err
    d = request.json or {}
    fid = str(uuid.uuid4())
    conn = get_db()
    execute(conn, 'INSERT INTO portal_files (id,context_type,context_id,name,file_type) VALUES (%s,%s,%s,%s,%s)',
        (fid, 'production', d.get('production_id',''), d.get('name',''), 'folder'))
    conn.commit()
    row = fetchone(conn, 'SELECT * FROM portal_files WHERE id=%s', (fid,))
    conn.close()
    return jsonify(row)

@app.route('/api/portal/folders/<fid>', methods=['DELETE'])
def delete_portal_folder(fid):
    err = require_auth()
    if err: return err
    conn = get_db()
    execute(conn, 'DELETE FROM portal_files WHERE id=%s', (fid,))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

# ── Portal callout (POST = set callout) ──
@app.route('/api/portal/callout', methods=['POST'])
def set_portal_callout():
    err = require_admin()
    if err: return err
    d = request.json or {}
    conn = get_db()
    execute(conn, "INSERT INTO settings (key,value) VALUES ('portal_callout',%s) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
        (json.dumps(d.get('callout')),))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

# ── Portal youth update ──
@app.route('/api/portal/youth/<yid>', methods=['POST'])
def portal_update_youth(yid):
    d = request.json or {}
    conn = get_db()
    # Queue for staff review
    pid = str(uuid.uuid4())
    execute(conn, "INSERT INTO pending_hours (id,volunteer_id,event,date,hours,notes,status) VALUES (%s,%s,'Profile Update Request',CURRENT_DATE,0,%s,'pending_review')",
        (pid, yid, json.dumps(d)))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

# ── Portal program waiver status ──
@app.route('/api/portal/program/<pid>/waiver-status')
def portal_program_waiver_status(pid):
    yid = request.args.get('youth_id')
    conn = get_db()
    rows = fetchall(conn, '''SELECT wt.name, yw.signed_date, yw.expiry_date
        FROM youth_waivers yw JOIN waiver_types wt ON yw.waiver_type_id=wt.id
        WHERE yw.youth_id=%s''', (yid,)) if yid else []
    conn.close()
    return jsonify(rows)

# ── Youth programs enrollment ──
@app.route('/api/youth-programs/<pid>/enrolled')
def get_program_enrolled(pid):
    err = require_auth()
    if err: return err
    conn = get_db()
    rows = fetchall(conn, '''SELECT ype.*, y.first_name, y.last_name
        FROM youth_program_enrollments ype
        JOIN youth_participants y ON ype.youth_id=y.id
        WHERE ype.program_id=%s ORDER BY y.last_name, y.first_name''', (pid,))
    conn.close()
    return jsonify(rows)

@app.route('/api/youth-programs/<pid>/enroll', methods=['POST'])
def enroll_in_program(pid):
    err = require_auth()
    if err: return err
    d = request.json or {}
    eid = str(uuid.uuid4())
    conn = get_db()
    execute(conn, '''INSERT INTO youth_program_enrollments (id,youth_id,program_id,enrolled_date,notes)
        VALUES (%s,%s,%s,%s,%s) ON CONFLICT (youth_id,program_id) DO NOTHING''',
        (eid, d.get('youth_id'), pid,
         d.get('enrolled_date',''), d.get('notes','')))
    conn.commit()
    row = fetchone(conn, '''SELECT ype.*, y.first_name, y.last_name, yp.name as program_name
        FROM youth_program_enrollments ype
        JOIN youth_participants y ON ype.youth_id=y.id
        JOIN youth_programs yp ON ype.program_id=yp.id
        WHERE ype.id=%s''', (eid,))
    conn.close()
    return jsonify(row or {'ok': True})

@app.route('/api/youth-enrollments/<eid>', methods=['DELETE'])
def delete_youth_enrollment(eid):
    err = require_auth()
    if err: return err
    conn = get_db()
    execute(conn, 'DELETE FROM youth_program_enrollments WHERE id=%s', (eid,))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

# ── Volunteer / Youth linking ──
@app.route('/api/volunteers/<vol_id>/link-participant', methods=['POST'])
def link_volunteer_to_participant(vol_id):
    err = require_auth()
    if err: return err
    d = request.json or {}
    conn = get_db()
    execute(conn, 'UPDATE youth_participants SET linked_volunteer_id=%s WHERE id=%s', (vol_id, d.get('youth_id')))
    execute(conn, 'UPDATE volunteers SET linked_youth_id=%s WHERE id=%s', (d.get('youth_id'), vol_id))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/youth/<yid>/link-volunteer', methods=['POST'])
def link_youth_to_volunteer(yid):
    err = require_auth()
    if err: return err
    d = request.json or {}
    conn = get_db()
    execute(conn, 'UPDATE youth_participants SET linked_volunteer_id=%s WHERE id=%s', (d.get('volunteer_id'), yid))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

# ── Users role update ──
@app.route('/api/users/<uid>/role', methods=['PUT'])
def update_user_role(uid):
    err = require_admin()
    if err: return err
    d = request.json or {}
    conn = get_db()
    execute(conn, 'UPDATE users SET role=%s WHERE id=%s', (d.get('role','staff'), uid))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

# ── Waiver toggle required ──
@app.route('/api/waiver-types/<tid>/toggle-required', methods=['POST'])
def toggle_waiver_required(tid):
    err = require_admin()
    if err: return err
    conn = get_db()
    # Toggle both columns — required_for_volunteering is used by kiosk, required_all by admin UI
    execute(conn, '''UPDATE waiver_types
        SET required_for_volunteering = NOT COALESCE(required_for_volunteering, FALSE),
            required_all = NOT COALESCE(required_all, FALSE)
        WHERE id=%s''', (tid,))
    conn.commit()
    row = fetchone(conn, 'SELECT required_for_volunteering, required_all FROM waiver_types WHERE id=%s', (tid,))
    conn.close()
    new_val = bool(row['required_for_volunteering']) if row else False
    return jsonify({'ok': True, 'required_all': new_val, 'required_for_volunteering': new_val})

# ── Donors missing routes ──
@app.route('/api/donor-benefits')
def get_all_donor_benefits():
    err = require_auth()
    if err: return err
    conn = get_db()
    rows = fetchall(conn, '''SELECT db.*, dt.name as tier_name FROM donor_tier_benefits db
        JOIN donor_tiers dt ON db.tier_id=dt.id ORDER BY dt.min_amount, db.sort_order''')
    conn.close()
    return jsonify(rows)

@app.route('/api/donations')
def get_all_donations_list():
    err = require_auth()
    if err: return err
    conn = get_db()
    rows = fetchall(conn, '''SELECT dd.*, dn.display_name, c.name as campaign_name
        FROM donor_donations dd
        JOIN donors dn ON dd.donor_id=dn.id
        LEFT JOIN donor_campaigns c ON dd.campaign_id=c.id
        ORDER BY dd.donation_date DESC NULLS LAST LIMIT 500''')
    conn.close()
    return jsonify(rows)

# ── Kiosk unauthorized pickup notify ──
@app.route('/api/kiosk/unauthorized-pickup-notify', methods=['POST'])
def kiosk_unauthorized_pickup_notify():
    d = request.json or {}
    try:
        s = get_email_settings()
        recipients = get_recipient_emails(s)
        if recipients:
            send_email(recipients, 'ALERT: Unauthorized Pickup Attempt',
                f'<p style="font-family:sans-serif;color:#dc2626"><strong>Unauthorized pickup attempt</strong> at the kiosk.<br/>'
                f'Youth: {d.get("youth_name","Unknown")}<br/>'
                f'Attempted by: {d.get("person_name","Unknown")}<br/>'
                f'Time: {__import__("datetime").datetime.now().strftime("%I:%M %p")}</p>')
    except Exception:
        pass
    return jsonify({'ok': True})

# ── Pending profile updates ──
@app.route('/api/pending-profile-updates/<uid>/approve', methods=['POST'])
def approve_profile_update(uid):
    err = require_auth()
    if err: return err
    conn = get_db()
    execute(conn, "UPDATE pending_hours SET status='approved' WHERE id=%s", (uid,))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/pending-profile-updates/<uid>/reject', methods=['POST'])
def reject_profile_update(uid):
    err = require_auth()
    if err: return err
    conn = get_db()
    execute(conn, "UPDATE pending_hours SET status='rejected' WHERE id=%s", (uid,))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

init_db()

# ── Global error handlers — return JSON for all API errors ──
@app.errorhandler(500)
def internal_error(e):
    app.logger.error(f'500: {e}')
    return jsonify({'error': str(e)}), 500

@app.errorhandler(404)
def not_found(e):
    if request.path.startswith('/api/'):
        return jsonify({'error': 'Route not found'}), 404
    return e

@app.errorhandler(405)
def method_not_allowed(e):
    if request.path.startswith('/api/'):
        return jsonify({'error': 'Method not allowed'}), 405
    return e

@app.errorhandler(Exception)
def handle_exception(e):
    import traceback
    app.logger.error(f'Unhandled: {traceback.format_exc()}')
    # Try to clean up any aborted transaction
    try:
        from psycopg2 import DatabaseError
        conn = get_db()
        conn.rollback()
        conn.close()
    except Exception:
        pass
    return jsonify({'error': str(e)}), 500




# ── Event Staff ──────────────────────────────────────────────────

@app.route('/api/events/<eid>/staff')
def get_event_staff(eid):
    conn = get_db()
    staff = fetchall(conn, '''SELECT es.*, v.name as volunteer_name,
        v.background_check_status, v.email
        FROM event_staff es
        JOIN volunteers v ON es.volunteer_id=v.id
        WHERE es.event_id=%s ORDER BY es.role, v.name''', (eid,))
    conn.close()
    return jsonify(staff)

@app.route('/api/events/<eid>/staff', methods=['POST'])
def add_event_staff(eid):
    err = require_auth()
    if err: return err
    d = request.json or {}
    vol_id = d.get('volunteer_id')
    if not vol_id: return jsonify({'error': 'volunteer_id required'}), 400
    sid = str(uuid.uuid4())
    conn = get_db()
    try:
        execute(conn, '''INSERT INTO event_staff (id,event_id,volunteer_id,role,notes)
            VALUES (%s,%s,%s,%s,%s)''',
            (sid, eid, vol_id, (d.get('role') or '').strip(), (d.get('notes') or '').strip()))
        conn.commit()
        row = fetchone(conn, '''SELECT es.*, v.name as volunteer_name,
            v.background_check_status, v.email
            FROM event_staff es JOIN volunteers v ON es.volunteer_id=v.id
            WHERE es.id=%s''', (sid,))
        conn.close()
        return jsonify(row)
    except Exception as e:
        conn.rollback(); conn.close()
        if 'unique' in str(e).lower():
            return jsonify({'error': 'This volunteer is already on the staff list'}), 400
        return jsonify({'error': str(e)}), 500

@app.route('/api/events/staff/<sid>', methods=['PUT'])
def update_event_staff(sid):
    err = require_auth()
    if err: return err
    d = request.json or {}
    conn = get_db()
    execute(conn, 'UPDATE event_staff SET role=%s, notes=%s WHERE id=%s',
        ((d.get('role') or '').strip(), (d.get('notes') or '').strip(), sid))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/events/staff/<sid>', methods=['DELETE'])
def remove_event_staff(sid):
    err = require_auth()
    if err: return err
    conn = get_db()
    execute(conn, 'DELETE FROM event_staff WHERE id=%s', (sid,))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

# ── Event Roles ──────────────────────────────────────────────────

@app.route('/api/events/<eid>/roles')
def get_event_roles(eid):
    conn = get_db()
    roles = fetchall(conn, '''
        SELECT r.*,
            COUNT(rv.id) FILTER (WHERE rv.status='interested') as filled
        FROM event_roles r
        LEFT JOIN event_rsvps rv ON rv.role_id=r.id AND rv.status='interested'
        WHERE r.event_id=%s
        GROUP BY r.id ORDER BY r.sort_order ASC, r.name ASC''', (eid,))
    conn.close()
    return jsonify(roles)

@app.route('/api/events/<eid>/roles', methods=['POST'])
def create_event_role(eid):
    err = require_auth()
    if err: return err
    d = request.json or {}
    if not (d.get('name') or '').strip():
        return jsonify({'error': 'Role name is required'}), 400
    rid = str(uuid.uuid4())
    conn = get_db()
    execute(conn, '''INSERT INTO event_roles (id,event_id,name,slots,description,sort_order)
        VALUES (%s,%s,%s,%s,%s,%s)''',
        (rid, eid, d['name'].strip(), int(d.get('slots') or 1),
         (d.get('description') or '').strip(), int(d.get('sort_order') or 0)))
    conn.commit()
    row = fetchone(conn, '''SELECT r.*, 0 as filled FROM event_roles r WHERE r.id=%s''', (rid,))
    conn.close()
    return jsonify(row)

@app.route('/api/events/roles/<rid>', methods=['PUT'])
def update_event_role(rid):
    err = require_auth()
    if err: return err
    d = request.json or {}
    conn = get_db()
    execute(conn, '''UPDATE event_roles SET name=%s, slots=%s, description=%s
        WHERE id=%s''',
        ((d.get('name') or '').strip(), int(d.get('slots') or 1),
         (d.get('description') or '').strip(), rid))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/events/roles/<rid>', methods=['DELETE'])
def delete_event_role(rid):
    err = require_auth()
    if err: return err
    conn = get_db()
    execute(conn, 'DELETE FROM event_roles WHERE id=%s', (rid,))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

# ── Event RSVPs ──────────────────────────────────────────────────

@app.route('/api/events/<eid>/rsvps')
def get_event_rsvps(eid):
    err = require_auth()
    if err: return err
    conn = get_db()
    rsvps = fetchall(conn, '''SELECT r.*, v.name as vol_name, v.email as vol_email
        FROM event_rsvps r LEFT JOIN volunteers v ON r.volunteer_id=v.id
        WHERE r.event_id=%s ORDER BY r.created_at ASC''', (eid,))
    conn.close()
    return jsonify(rsvps)

@app.route('/api/events/<eid>/rsvp-invite', methods=['POST'])
def send_rsvp_invite(eid):
    err = require_auth()
    if err: return err
    d = request.json or {}
    conn = get_db()
    evt = fetchone(conn, 'SELECT * FROM events WHERE id=%s', (eid,))
    if not evt: conn.close(); return jsonify({'error': 'Event not found'}), 404

    # Get roles for this event
    roles = fetchall(conn, '''
        SELECT r.*, COUNT(rv.id) FILTER (WHERE rv.status=\'interested\') as filled
        FROM event_roles r
        LEFT JOIN event_rsvps rv ON rv.role_id=r.id AND rv.status=\'interested\'
        WHERE r.event_id=%s GROUP BY r.id ORDER BY r.sort_order, r.name''', (eid,))

    target_ids = d.get('volunteer_ids') or []
    if not target_ids:
        vols = fetchall(conn, "SELECT id,name,email FROM volunteers WHERE status='active' AND email!='' AND email IS NOT NULL")
    else:
        vols = fetchall(conn, "SELECT id,name,email FROM volunteers WHERE id=ANY(%s) AND email IS NOT NULL", (target_ids,))

    sent = 0
    custom_msg = (d.get('message') or '').strip()
    base_url = request.host_url.rstrip('/')

    # Build roles table HTML for email
    roles_html = ''
    if roles:
        roles_html = '<h3 style="color:#145466;margin-top:24px">Available Roles</h3>'
        roles_html += '<table style="width:100%;border-collapse:collapse;font-size:14px;margin:8px 0">'
        roles_html += '<thead><tr style="background:#f0fdf4"><th style="padding:8px 12px;text-align:left">Role</th><th style="padding:8px 12px;text-align:left">Slots</th><th style="padding:8px 12px;text-align:left">Notes</th></tr></thead><tbody>'
        for r in roles:
            available = max(0, int(r['slots']) - int(r['filled'] or 0))
            roles_html += f'<tr style="border-bottom:1px solid #eee"><td style="padding:8px 12px;font-weight:600">{r["name"]}</td>'
            roles_html += f'<td style="padding:8px 12px;color:{"#16a34a" if available>0 else "#dc2626"}">{available} of {r["slots"]} open</td>'
            roles_html += f'<td style="padding:8px 12px;color:#666">{r.get("description","")}</td></tr>'
        roles_html += '</tbody></table><p style="font-size:13px;color:#888">You can choose your preferred role when you sign up.</p>'

    for v in vols:
        if not v.get('email'): continue
        existing = fetchone(conn, 'SELECT token FROM event_rsvps WHERE event_id=%s AND volunteer_id=%s', (eid, v['id']))
        if existing:
            token = existing['token']
        else:
            token = str(uuid.uuid4())
            execute(conn, '''INSERT INTO event_rsvps (id,event_id,volunteer_id,volunteer_name,volunteer_email,token,status)
                VALUES (%s,%s,%s,%s,%s,%s,'invited')''',
                (str(uuid.uuid4()), eid, v['id'], v['name'], v['email'], token))

        rsvp_url = f"{base_url}/rsvp/{token}"
        date_str = evt.get('event_date','')
        time_str = evt.get('start_time','')
        if time_str:
            try:
                from datetime import datetime as _dt
                t = _dt.strptime(time_str, '%H:%M')
                time_str = t.strftime('%I:%M %p').lstrip('0')
            except Exception:
                pass
        body = f'''<div style="font-family:-apple-system,sans-serif;max-width:600px;margin:0 auto">
          <h2 style="color:#145466">You're Invited: {evt['name']}</h2>
          <p>Hi {v['name']},</p>
          <p>We're looking for volunteers for an upcoming event and would love to have you join us!</p>
          <table style="width:100%;border-collapse:collapse;font-size:14px;margin:16px 0">
            <tr><td style="padding:8px;font-weight:600;color:#666;width:120px">Event</td><td style="padding:8px">{evt['name']}</td></tr>
            <tr style="background:#f9f9f9"><td style="padding:8px;font-weight:600;color:#666">Date</td><td style="padding:8px">{date_str}</td></tr>
            {f'<tr><td style="padding:8px;font-weight:600;color:#666">Time</td><td style="padding:8px">{time_str}</td></tr>' if time_str else ''}
            {f'<tr style="background:#f9f9f9"><td style="padding:8px;font-weight:600;color:#666">Location</td><td style="padding:8px">{evt["location"]}</td></tr>' if evt.get('location') else ''}
          </table>
          {f'<p>{custom_msg}</p>' if custom_msg else ''}
          {f'<p style="color:#555">{evt["description"]}</p>' if evt.get('description') else ''}
          {roles_html}
          <div style="text-align:center;margin:28px 0">
            <a href="{rsvp_url}" style="background:#145466;color:#fff;text-decoration:none;padding:14px 32px;border-radius:8px;font-size:16px;font-weight:700;display:inline-block">
              ✋ {"Pick a role & sign up!" if roles else "Yes, I can help!"}
            </a>
          </div>
          <p style="font-size:13px;color:#888">If you're unable to make it, no action needed — we only want to hear from those who can volunteer. This link is unique to you: {rsvp_url}</p>
        </div>'''
        try:
            send_email([v['email']], f'Volunteer Opportunity: {evt["name"]}', body)
            sent += 1
        except Exception as e:
            app.logger.warning(f'RSVP invite email failed for {v["email"]}: {e}')

    conn.commit(); conn.close()
    return jsonify({'ok': True, 'sent': sent})

@app.route('/rsvp/<token>')
def rsvp_page(token):
    conn = get_db()
    rsvp = fetchone(conn, '''SELECT r.*, e.name as event_name, e.event_date, e.start_time, e.location, e.description, e.id as event_id
        FROM event_rsvps r JOIN events e ON r.event_id=e.id WHERE r.token=%s''', (token,))
    if not rsvp:
        conn.close()
        return '<html><body style="font-family:sans-serif;text-align:center;padding:60px"><h2>Link not found or expired.</h2></body></html>', 404

    # Already signed up
    if rsvp.get('status') == 'interested':
        role_line = f'<p style="color:#16a34a;font-weight:600">Your role: {rsvp["role_name"]}</p>' if rsvp.get('role_name') else ''
        conn.close()
        return f'''<html><head><title>RSVP Confirmed</title>
        <meta name="viewport" content="width=device-width,initial-scale=1"></head>
        <body style="font-family:-apple-system,sans-serif;text-align:center;padding:60px 20px;max-width:500px;margin:0 auto">
          <div style="font-size:48px;margin-bottom:16px">✅</div>
          <h2 style="color:#145466">You're already signed up!</h2>
          <p>Thanks {rsvp.get("volunteer_name","")} — we have your RSVP for <strong>{rsvp["event_name"]}</strong>.</p>
          {role_line}
          <p style="color:#888">We'll be in touch with more details.</p>
        </body></html>'''

    # Load available roles
    roles = fetchall(conn, '''
        SELECT r.*, COUNT(rv.id) FILTER (WHERE rv.status=\'interested\') as filled
        FROM event_roles r
        LEFT JOIN event_rsvps rv ON rv.role_id=r.id AND rv.status=\'interested\'
        WHERE r.event_id=%s GROUP BY r.id ORDER BY r.sort_order, r.name''', (rsvp['event_id'],))
    conn.close()

    date_str = rsvp.get('event_date','')
    vol_name = rsvp.get('volunteer_name','')

    if roles:
        # Show role selection form
        roles_html = ''
        for r in roles:
            available = max(0, int(r['slots']) - int(r['filled'] or 0))
            disabled = 'disabled' if available <= 0 else ''
            style = 'opacity:0.5;cursor:not-allowed' if available <= 0 else 'cursor:pointer'
            badge = f'<span style="font-size:11px;color:{"#16a34a" if available>0 else "#dc2626"};font-weight:600">{""+str(available)+" spot"+ ("s" if available!=1 else "")+" left" if available>0 else "Full"}</span>'
            desc = f'<div style="font-size:12px;color:#666;margin-top:2px">{r["description"]}</div>' if r.get('description') else ''
            roles_html += f'''<label style="display:flex;align-items:center;gap:12px;padding:12px 16px;border:2px solid #e2e8f0;border-radius:10px;margin-bottom:8px;{style}" 
                onclick="if(!this.querySelector('input').disabled) this.closest('form').querySelectorAll('label').forEach(l=>l.style.borderColor='#e2e8f0'); this.style.borderColor='#145466';">
                <input type="radio" name="role_id" value="{r["id"]}" {disabled} style="accent-color:#145466;flex-shrink:0" required/>
                <div style="flex:1">
                  <div style="font-weight:600;font-size:15px">{r["name"]} {badge}</div>
                  {desc}
                </div>
            </label>'''

        return f'''<html><head><title>Sign Up — {rsvp["event_name"]}</title>
        <meta name="viewport" content="width=device-width,initial-scale=1"></head>
        <body style="font-family:-apple-system,sans-serif;max-width:500px;margin:0 auto;padding:40px 20px">
          <div style="text-align:center;margin-bottom:28px">
            <div style="font-size:40px;margin-bottom:12px">✋</div>
            <h2 style="color:#145466;margin-bottom:6px">Sign up to volunteer!</h2>
            <p style="color:#555">Hi {vol_name} — choose your role for:</p>
            <div style="background:#f0fdf4;border:2px solid #86efac;border-radius:10px;padding:14px;margin:16px 0">
              <div style="font-size:18px;font-weight:700;color:#145466">{rsvp["event_name"]}</div>
              {f'<div style="color:#555;margin-top:4px">{date_str}</div>' if date_str else ''}
              {f'<div style="color:#888;font-size:13px">{rsvp["location"]}</div>' if rsvp.get("location") else ''}
            </div>
          </div>
          <form method="POST" action="/rsvp/{token}">
            <div style="font-size:13px;font-weight:700;text-transform:uppercase;letter-spacing:0.5px;color:#888;margin-bottom:10px">Choose a role</div>
            {roles_html}
            <button type="submit" style="width:100%;background:#145466;color:#fff;border:none;border-radius:10px;padding:16px;font-size:16px;font-weight:700;cursor:pointer;margin-top:12px">
              ✅ Confirm Sign-up
            </button>
          </form>
          <p style="text-align:center;font-size:12px;color:#aaa;margin-top:20px">If you can't make it, no action needed.</p>
        </body></html>'''
    else:
        # No roles — just confirm directly
        conn2 = get_db()
        execute(conn2, "UPDATE event_rsvps SET status='interested' WHERE token=%s", (token,))
        conn2.commit(); conn2.close()
        return f'''<html><head><title>RSVP Confirmed — {rsvp["event_name"]}</title>
        <meta name="viewport" content="width=device-width,initial-scale=1"></head>
        <body style="font-family:-apple-system,sans-serif;text-align:center;padding:60px 20px;max-width:500px;margin:0 auto">
          <div style="font-size:48px;margin-bottom:16px">🎉</div>
          <h2 style="color:#145466">You're in!</h2>
          <p>Thanks {vol_name} — we've recorded your interest in volunteering for <strong>{rsvp["event_name"]}</strong>.</p>
          <p style="color:#888;font-size:14px">We'll follow up with more details. Thank you!</p>
        </body></html>'''

@app.route('/rsvp/<token>', methods=['POST'])
def rsvp_submit(token):
    from flask import request as req
    role_id = req.form.get('role_id','').strip()
    conn = get_db()
    rsvp = fetchone(conn, '''SELECT r.*, e.name as event_name, e.event_date, e.location, e.id as event_id
        FROM event_rsvps r JOIN events e ON r.event_id=e.id WHERE r.token=%s''', (token,))
    if not rsvp:
        conn.close()
        return '<html><body style="font-family:sans-serif;text-align:center;padding:60px"><h2>Link not found.</h2></body></html>', 404

    role_name = ''
    if role_id:
        role = fetchone(conn, 'SELECT * FROM event_roles WHERE id=%s AND event_id=%s', (role_id, rsvp['event_id']))
        if role:
            # Check slot availability
            filled = fetchone(conn, "SELECT COUNT(*) as c FROM event_rsvps WHERE role_id=%s AND status='interested'", (role_id,))
            if filled and int(filled['c']) >= int(role['slots']):
                conn.close()
                return f'''<html><head><meta name="viewport" content="width=device-width,initial-scale=1"></head>
                <body style="font-family:-apple-system,sans-serif;text-align:center;padding:60px 20px;max-width:500px;margin:0 auto">
                  <div style="font-size:48px;margin-bottom:16px">😔</div>
                  <h2 style="color:#dc2626">That role just filled up</h2>
                  <p>Sorry, the <strong>{role["name"]}</strong> slot was just taken. <a href="/rsvp/{token}">Go back</a> to choose another role.</p>
                </body></html>''', 409
            role_name = role['name']

    execute(conn, "UPDATE event_rsvps SET status='interested', role_id=%s, role_name=%s WHERE token=%s",
        (role_id or None, role_name, token))
    conn.commit()
    date_str = rsvp.get('event_date','')

    # Send admin alert emails
    try:
        s = get_email_settings()
        recipients = get_recipient_emails(s)
        if recipients:
            vol_name = rsvp.get('volunteer_name','A volunteer')
            evt_name = rsvp['event_name']
            role_line = f' for <strong>{role_name}</strong>' if role_name else ''
            # New RSVP alert
            if s.get('alert_new_rsvp', True):
                send_email(recipients, f'New Sign-up: {evt_name}',
                    f'<div style="font-family:sans-serif"><p>✋ <strong>{vol_name}</strong> signed up to volunteer{role_line} for <strong>{evt_name}</strong>.</p>'
                    f'{f"<p>Date: {date_str}</p>" if date_str else ""}</div>')
            # Role filled alert
            if role_id and role_name and s.get('alert_role_filled', True):
                filled_now = fetchone(conn, "SELECT COUNT(*) as c FROM event_rsvps WHERE role_id=%s AND status='interested'", (role_id,))
                role_row = fetchone(conn, 'SELECT slots FROM event_roles WHERE id=%s', (role_id,))
                if filled_now and role_row and int(filled_now['c']) >= int(role_row['slots']):
                    send_email(recipients, f'Role Filled: {role_name} — {evt_name}',
                        f'<div style="font-family:sans-serif"><p>🎉 The <strong>{role_name}</strong> role for <strong>{evt_name}</strong> is now fully filled ({role_row["slots"]} of {role_row["slots"]} slots).</p></div>')
    except Exception as e:
        app.logger.warning(f'rsvp alert email error: {e}')

    conn.close()
    return f'''<html><head><title>Signed Up!</title>
    <meta name="viewport" content="width=device-width,initial-scale=1"></head>
    <body style="font-family:-apple-system,sans-serif;text-align:center;padding:60px 20px;max-width:500px;margin:0 auto">
      <div style="font-size:48px;margin-bottom:16px">🎉</div>
      <h2 style="color:#145466">You're signed up!</h2>
      <p>Thanks {rsvp.get("volunteer_name","")}! We've got you down for:</p>
      <div style="background:#f0fdf4;border:2px solid #86efac;border-radius:12px;padding:20px;margin:24px 0">
        <div style="font-size:20px;font-weight:700;color:#145466">{rsvp["event_name"]}</div>
        {f'<div style="color:#555;margin-top:6px">{date_str}</div>' if date_str else ''}
        {f'<div style="color:#16a34a;font-weight:600;font-size:16px;margin-top:8px">Role: {role_name}</div>' if role_name else ''}
      </div>
      <p style="color:#888;font-size:14px">We'll follow up with more details as the event approaches. Thank you!</p>
    </body></html>'''

@app.route('/api/events/<eid>/rsvps/<rid>', methods=['DELETE'])
def remove_rsvp(eid, rid):
    err = require_auth()
    if err: return err
    conn = get_db()
    execute(conn, 'DELETE FROM event_rsvps WHERE id=%s AND event_id=%s', (rid, eid))
    conn.commit(); conn.close()
    return jsonify({'ok': True})


if __name__ == '__main__':
    print('\n🎭 RoleCall is running!')
    print('   Open http://localhost:5000 in your browser\n')
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=False)
