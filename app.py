from flask import Flask, request, jsonify, session, send_from_directory
from flask_cors import CORS
import psycopg2
import psycopg2.extras
import hashlib
import os
import json
import secrets
import re
import io
from datetime import datetime, date
import cloudinary
import cloudinary.uploader
import cloudinary.utils
import requests as req_lib
import base64
import time
from cryptography.fernet import Fernet, InvalidToken
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak, HRFlowable
from reportlab.lib.enums import TA_CENTER
from reportlab.pdfgen import canvas as pdfcanvas
from pypdf import PdfReader, PdfWriter
from pypdf.generic import NameObject, BooleanObject

app = Flask(__name__, static_folder='static')
app.secret_key = os.environ.get('SECRET_KEY', 'bloombooks-dev-key')
CORS(app, supports_credentials=True)

DATABASE_URL = os.environ.get('DATABASE_URL', '')

# ─── Cloudinary config ───────────────────────────────────────────────────────
cloudinary.config(
    cloud_name=os.environ.get('CLOUDINARY_CLOUD_NAME', ''),
    api_key=os.environ.get('CLOUDINARY_API_KEY', ''),
    api_secret=os.environ.get('CLOUDINARY_API_SECRET', '')
)

# ─── Contractor field-level encryption ─────────────────────────────────────────
# SSNs/EINs and bank account/routing numbers are encrypted at rest with Fernet
# (AES-128-CBC + HMAC). Set CONTRACTOR_ENCRYPTION_KEY in production — generate one
# with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# If it's not set, a key is derived from SECRET_KEY so local/dev still works, but
# that fallback is NOT safe for production (it means anyone with SECRET_KEY can
# decrypt contractor data — always set a dedicated key on Railway).
_contractor_cipher = None
def get_cipher():
    global _contractor_cipher
    if _contractor_cipher is not None:
        return _contractor_cipher
    key = os.environ.get('CONTRACTOR_ENCRYPTION_KEY', '').strip()
    if key:
        _contractor_cipher = Fernet(key.encode())
    else:
        print("[CONTRACTORS] WARNING: CONTRACTOR_ENCRYPTION_KEY is not set. Falling back to a "
              "key derived from SECRET_KEY. Set CONTRACTOR_ENCRYPTION_KEY as its own env var "
              "before storing real SSNs/EINs or bank details in production.")
        digest = hashlib.sha256(app.secret_key.encode()).digest()
        _contractor_cipher = Fernet(base64.urlsafe_b64encode(digest))
    return _contractor_cipher

def encrypt_value(plain):
    """Encrypt a sensitive string for storage. Returns None for empty input."""
    if plain is None or str(plain).strip() == '':
        return None
    return get_cipher().encrypt(str(plain).encode()).decode()

def decrypt_value(token):
    """Decrypt a value previously produced by encrypt_value. Returns None on failure."""
    if not token:
        return None
    try:
        return get_cipher().decrypt(token.encode()).decode()
    except (InvalidToken, Exception):
        return None

def last4(value):
    """Last 4 alphanumeric characters of a value, for safe display (e.g. ***-**-1234)."""
    if not value:
        return ''
    digits = ''.join(ch for ch in str(value) if ch.isalnum())
    return digits[-4:] if len(digits) >= 4 else digits

def verify_password(user, password):
    return bool(password) and hash_pw(password) == user['password']

# ─── Email config ─────────────────────────────────────────────────────────────
RESEND_API_KEY = os.environ.get('RESEND_API_KEY', '')
FROM_EMAIL     = os.environ.get('FROM_EMAIL', 'BloomBooks <info@hwtco.org>')
APP_URL        = os.environ.get('APP_URL', 'http://localhost:5001')
ORG_NAME       = os.environ.get('ORG_NAME', 'Horizon West Theater Company')

# ─── Database ─────────────────────────────────────────────────────────────────
class DBWrapper:
    """Wraps a psycopg2 connection to behave like sqlite3 — conn.execute() returns cursor."""
    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=None):
        # Fix any remaining ? placeholders just in case
        sql = sql.replace('?', '%s')
        c = self._conn.cursor()
        c.execute(sql, params or ())
        return c

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()

    def cursor(self):
        return self._conn.cursor()

def get_db():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    return DBWrapper(conn)

DEFAULT_AGREEMENT_TEMPLATE = """INDEPENDENT CONTRACTOR AGREEMENT

This Independent Contractor Agreement ("Agreement") is entered into as of {{effective_date}} between {{org_name}} ("Organization") and {{contractor_name}}{{business_name_suffix}} ("Contractor").

1. SERVICES
Contractor agrees to provide the following services to the Organization: {{scope}}

2. TERM
This Agreement begins on {{start_date}} and ends on {{end_date}}, unless terminated earlier as provided below.

3. COMPENSATION
The Organization agrees to pay Contractor {{rate}}. Payment terms: {{payment_terms}}

4. INDEPENDENT CONTRACTOR STATUS
Contractor is an independent contractor, not an employee of the Organization. Contractor is responsible for their own taxes, insurance, and benefits. Nothing in this Agreement creates a partnership, joint venture, or employment relationship.

5. TERMINATION
Either party may terminate this Agreement with written notice if the other party materially breaches its terms and fails to cure the breach within a reasonable time after notice.

6. CONFIDENTIALITY
Contractor agrees to keep confidential any non-public information about the Organization's operations, participants, donors, or finances learned in the course of providing services, both during and after this engagement.

7. OWNERSHIP OF WORK PRODUCT
Any materials, creative work, or deliverables created by Contractor specifically for the Organization under this Agreement belong to the Organization upon payment in full, unless otherwise agreed in writing.

8. INDEMNIFICATION
Each party agrees to be responsible for its own acts and omissions in connection with this Agreement.

9. GOVERNING LAW
This Agreement is governed by the laws of the State of Florida.

10. ENTIRE AGREEMENT
This Agreement represents the entire understanding between the parties regarding its subject matter and supersedes any prior discussions or agreements.

By signing below, both parties agree to the terms of this Agreement.
"""


INSTRUCTOR_AGREEMENT_TEMPLATE = """INSTRUCTOR AGREEMENT

This Instructor Agreement is between Horizon West Theater Company ("HWTC") and the instructor identified below ("Instructor") for the class or workshop described in this agreement.

Instructor Name: {{contractor_name}}
Class/Workshop: {{class_workshop}}
Date(s): {{class_dates}}
Location: {{location}}
Instructor Compensation: {{rate}}

1. INSTRUCTIONAL SERVICES
The Instructor agrees to provide instruction for the class or workshop listed above.

Horizon West Theater Company will provide the facility, establish the available class schedule in coordination with the Instructor, manage participant registration, collect registration fees, and provide reasonable administrative support for the class.

The Instructor is being engaged for their individual knowledge, experience, skills, and expertise in the subject matter. The Instructor will independently develop or select the curriculum, lesson content, activities, and methods of instruction used for the class.

HWTC may establish general program requirements relating to participant safety, appropriate conduct, facility use, class length, age group, and the general description or purpose of the class but will not direct the Instructor's day-to-day instructional methods or curriculum.

2. COMPENSATION
The Instructor will receive the compensation listed above for providing the agreed-upon instructional services. Unless otherwise agreed upon, HWTC's standard instructor compensation is an amount equal to the registration fee charged for one participant in the class or workshop. Compensation is for instructional services only and is separate from any unpaid volunteer, board, committee, or other service the Instructor may provide to Horizon West Theater Company.

3. INDEPENDENT INSTRUCTIONAL RELATIONSHIP
The Instructor is engaged to provide instructional services for the specific class or workshop described in this agreement. The Instructor is free to provide similar instructional or professional services to other organizations or individuals. This agreement does not guarantee or require any future teaching engagements with HWTC. The Instructor is responsible for determining the manner and method of instruction and is not entitled to employee benefits from HWTC. The parties intend for the Instructor to provide these services as an independent contractor. The Instructor is responsible for any taxes or other obligations applicable to compensation received under this agreement.

4. INSTRUCTOR CURRICULUM & MATERIALS
The Instructor retains ownership of any original curriculum, lesson plans, exercises, choreography, instructional materials, techniques, or other materials created or developed independently by the Instructor and used in connection with the class.

The Instructor is providing Horizon West Theater Company with their individual knowledge, experience, skills, and instructional services for the limited purpose of conducting the class or workshop described in this agreement.

Unless separately agreed to in writing, the Instructor's curriculum and original instructional materials do not become the property of Horizon West Theater Company as a result of teaching the class.

The Instructor grants HWTC permission to use any materials provided by the Instructor as reasonably necessary to administer and conduct the specific class or workshop covered by this agreement. This permission does not authorize HWTC to reproduce, distribute, sell, or use the Instructor's curriculum to conduct future classes without the Instructor's permission.

The Instructor is responsible for ensuring that materials they provide or incorporate into their curriculum are either their own, appropriately licensed, or otherwise permitted for their intended use.

5. SAFETY & CONDUCT
The Instructor agrees to conduct the class in a safe, professional, and respectful manner and to follow applicable HWTC policies regarding participant safety, facility use, and conduct.

The Instructor will promptly communicate any safety concerns, participant incidents, or other significant issues arising during the class to an appropriate HWTC representative.

6. CANCELLATION OR CHANGES
If the Instructor is unable to teach the scheduled class, they agree to notify HWTC as soon as reasonably possible.

HWTC may cancel or reschedule a class due to enrollment, facility availability, safety concerns, weather, or other circumstances. If a class is canceled before instructional services are provided, instructor compensation may be adjusted accordingly.

7. VOLUNTEER OR BOARD SERVICE
If the Instructor also serves as a volunteer, committee member, officer, or member of the Board of Directors of Horizon West Theater Company, this agreement applies only to the paid instructional services described above. Any other volunteer or board service remains separate and uncompensated unless specifically authorized by HWTC.

AGREEMENT
By signing below, both parties acknowledge and agree to the terms above.

Horizon West Theater Company
Authorized Representative: {{org_rep_name}}
Approved: {{effective_date}}

Instructor
The Instructor's typed signature and signing date are captured electronically at the end of this document.
"""


def init_db():
    conn = get_db()
    c = conn.cursor()

    c.execute('''
    CREATE TABLE IF NOT EXISTS bb_users (
        id                SERIAL PRIMARY KEY,
        name              TEXT NOT NULL,
        email             TEXT UNIQUE NOT NULL,
        password          TEXT NOT NULL,
        role              TEXT NOT NULL DEFAULT 'volunteer',
        training_complete INTEGER DEFAULT 0,
        created_at        TEXT DEFAULT (to_char(now(), 'YYYY-MM-DD HH24:MI:SS'))
    )''')
    c.execute("ALTER TABLE bb_users ADD COLUMN IF NOT EXISTS can_submit_org_level INTEGER DEFAULT 0")

    c.execute('''
    CREATE TABLE IF NOT EXISTS bb_budgets (
        id           SERIAL PRIMARY KEY,
        name         TEXT NOT NULL,
        area         TEXT NOT NULL,
        season       TEXT NOT NULL,
        total_amount REAL NOT NULL,
        spent        REAL DEFAULT 0,
        is_active    INTEGER DEFAULT 1,
        created_at   TEXT DEFAULT (to_char(now(), 'YYYY-MM-DD HH24:MI:SS'))
    )''')

    c.execute('''
    CREATE TABLE IF NOT EXISTS bb_purchase_requests (
        id                 SERIAL PRIMARY KEY,
        type               TEXT NOT NULL DEFAULT 'pre_approval',
        status             TEXT NOT NULL DEFAULT 'pending_treasurer',
        title              TEXT NOT NULL,
        description        TEXT,
        vendor             TEXT,
        estimated_cost     REAL NOT NULL,
        actual_cost        REAL,
        budget_id          INTEGER REFERENCES bb_budgets(id),
        submitted_by       INTEGER REFERENCES bb_users(id),
        is_emergency       INTEGER DEFAULT 0,
        emergency_reason   TEXT,
        treasurer_note     TEXT,
        president_note     TEXT,
        treasurer_acted_by INTEGER REFERENCES bb_users(id),
        president_acted_by INTEGER REFERENCES bb_users(id),
        treasurer_acted_at TEXT,
        president_acted_at TEXT,
        submitted_at       TEXT DEFAULT (to_char(now(), 'YYYY-MM-DD HH24:MI:SS')),
        updated_at         TEXT DEFAULT (to_char(now(), 'YYYY-MM-DD HH24:MI:SS'))
    )''')

    c.execute('''
    CREATE TABLE IF NOT EXISTS bb_receipts (
        id          SERIAL PRIMARY KEY,
        request_id  INTEGER REFERENCES bb_purchase_requests(id),
        image_url   TEXT NOT NULL,
        public_id   TEXT,
        uploaded_at TEXT DEFAULT (to_char(now(), 'YYYY-MM-DD HH24:MI:SS'))
    )''')

    c.execute('''
    CREATE TABLE IF NOT EXISTS bb_reimbursements (
        id         SERIAL PRIMARY KEY,
        request_id INTEGER UNIQUE REFERENCES bb_purchase_requests(id),
        user_id    INTEGER REFERENCES bb_users(id),
        amount     REAL NOT NULL,
        status     TEXT DEFAULT 'pending',
        method     TEXT,
        paid_at    TEXT,
        notes      TEXT,
        created_at TEXT DEFAULT (to_char(now(), 'YYYY-MM-DD HH24:MI:SS'))
    )''')

    c.execute('''
    CREATE TABLE IF NOT EXISTS bb_training_modules (
        id          SERIAL PRIMARY KEY,
        title       TEXT NOT NULL,
        description TEXT,
        slides      TEXT DEFAULT '[]',
        questions   TEXT DEFAULT '[]',
        pass_mark   INTEGER DEFAULT 80,
        is_active   INTEGER DEFAULT 1,
        created_at  TEXT DEFAULT (to_char(now(), 'YYYY-MM-DD HH24:MI:SS'))
    )''')

    c.execute('''
    CREATE TABLE IF NOT EXISTS bb_training_completions (
        id           SERIAL PRIMARY KEY,
        user_id      INTEGER REFERENCES bb_users(id),
        module_id    INTEGER REFERENCES bb_training_modules(id),
        score        INTEGER,
        passed       INTEGER DEFAULT 0,
        completed_at TEXT DEFAULT (to_char(now(), 'YYYY-MM-DD HH24:MI:SS')),
        UNIQUE(user_id, module_id)
    )''')

    c.execute('''
    CREATE TABLE IF NOT EXISTS bb_audit_log (
        id          SERIAL PRIMARY KEY,
        user_id     INTEGER REFERENCES bb_users(id),
        action      TEXT NOT NULL,
        entity_type TEXT,
        entity_id   INTEGER,
        detail      TEXT,
        created_at  TEXT DEFAULT (to_char(now(), 'YYYY-MM-DD HH24:MI:SS'))
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS bb_production_revenue (
        id            SERIAL PRIMARY KEY,
        production_id INTEGER REFERENCES bb_productions(id) ON DELETE CASCADE,
        source        TEXT NOT NULL,
        description   TEXT,
        expected      REAL DEFAULT 0,
        actual        REAL DEFAULT 0,
        received_date TEXT,
        created_by    INTEGER REFERENCES bb_users(id),
        created_at    TEXT DEFAULT (to_char(now(), 'YYYY-MM-DD HH24:MI:SS')),
        updated_at    TEXT DEFAULT (to_char(now(), 'YYYY-MM-DD HH24:MI:SS'))
    )''')

    # Links a BloomBooks production to a RoleCall "Rising Stars" production so its
    # enrollment revenue can be read live from the shared database. BloomBooks owns
    # this table; RoleCall is never written to.
    c.execute('''CREATE TABLE IF NOT EXISTS bb_rolecall_links (
        bb_production_id   INTEGER PRIMARY KEY REFERENCES bb_productions(id) ON DELETE CASCADE,
        rc_production_id   TEXT NOT NULL,
        rc_production_name TEXT,
        linked_by          INTEGER REFERENCES bb_users(id),
        linked_at          TEXT DEFAULT (to_char(now(), 'YYYY-MM-DD HH24:MI:SS')),
        updated_at         TEXT DEFAULT (to_char(now(), 'YYYY-MM-DD HH24:MI:SS'))
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS bb_statements (
        id            SERIAL PRIMARY KEY,
        title         TEXT NOT NULL,
        description   TEXT,
        production_id INTEGER REFERENCES bb_productions(id),
        budget_id     INTEGER REFERENCES bb_budgets(id),
        created_by    INTEGER REFERENCES bb_users(id),
        status        TEXT DEFAULT 'draft',
        submitted_at  TEXT,
        created_at    TEXT DEFAULT (to_char(now(), 'YYYY-MM-DD HH24:MI:SS')),
        updated_at    TEXT DEFAULT (to_char(now(), 'YYYY-MM-DD HH24:MI:SS'))
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS bb_statement_items (
        id           SERIAL PRIMARY KEY,
        statement_id INTEGER REFERENCES bb_statements(id) ON DELETE CASCADE,
        request_id   INTEGER REFERENCES bb_purchase_requests(id) ON DELETE CASCADE,
        UNIQUE(statement_id, request_id)
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS bb_space_capacity_hours (
        day_of_week INTEGER PRIMARY KEY,
        open_time   TEXT DEFAULT '08:00',
        close_time  TEXT DEFAULT '22:00',
        closed      INTEGER DEFAULT 0
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS bb_pricing_settings (
        id               SERIAL PRIMARY KEY,
        facility_budget_id INTEGER REFERENCES bb_budgets(id) ON DELETE SET NULL,
        season_weeks     INTEGER DEFAULT 36,
        updated_at       TEXT DEFAULT (to_char(now(), 'YYYY-MM-DD HH24:MI:SS'))
    )''')

    # ─── Contractors (secure section: W9s, agreements, payments, banking) ─────
    c.execute('''CREATE TABLE IF NOT EXISTS bb_contractors (
        id                  SERIAL PRIMARY KEY,
        name                TEXT NOT NULL,
        business_name       TEXT,
        contact_email       TEXT,
        contact_phone       TEXT,
        address             TEXT,
        tax_classification  TEXT DEFAULT 'individual',
        tax_id_type         TEXT DEFAULT 'ssn',
        ein_ssn_encrypted   TEXT,
        ein_ssn_last4       TEXT,
        status              TEXT DEFAULT 'active',
        notes               TEXT,
        created_by          INTEGER REFERENCES bb_users(id),
        created_at          TEXT DEFAULT (to_char(now(), 'YYYY-MM-DD HH24:MI:SS')),
        updated_at          TEXT DEFAULT (to_char(now(), 'YYYY-MM-DD HH24:MI:SS'))
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS bb_contractor_documents (
        id               SERIAL PRIMARY KEY,
        contractor_id    INTEGER NOT NULL REFERENCES bb_contractors(id) ON DELETE CASCADE,
        doc_type         TEXT NOT NULL DEFAULT 'other',
        filename         TEXT,
        cloud_public_id  TEXT NOT NULL,
        resource_type    TEXT DEFAULT 'raw',
        access_type      TEXT DEFAULT 'private',
        cloud_version    TEXT,
        format           TEXT,
        effective_date   TEXT,
        expires_at       TEXT,
        uploaded_by      INTEGER REFERENCES bb_users(id),
        uploaded_at      TEXT DEFAULT (to_char(now(), 'YYYY-MM-DD HH24:MI:SS'))
    )''')
    c.execute("ALTER TABLE bb_contractor_documents ADD COLUMN IF NOT EXISTS access_type TEXT DEFAULT 'private'")
    c.execute("ALTER TABLE bb_contractor_documents ADD COLUMN IF NOT EXISTS cloud_version TEXT")

    c.execute('''CREATE TABLE IF NOT EXISTS bb_contractor_bank_accounts (
        id                          SERIAL PRIMARY KEY,
        contractor_id               INTEGER NOT NULL REFERENCES bb_contractors(id) ON DELETE CASCADE,
        nickname                    TEXT,
        account_holder_name         TEXT,
        account_type                TEXT DEFAULT 'checking',
        routing_number_encrypted    TEXT,
        routing_last4               TEXT,
        account_number_encrypted    TEXT,
        account_last4               TEXT,
        is_primary                  INTEGER DEFAULT 0,
        is_active                   INTEGER DEFAULT 1,
        created_by                  INTEGER REFERENCES bb_users(id),
        created_at                  TEXT DEFAULT (to_char(now(), 'YYYY-MM-DD HH24:MI:SS'))
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS bb_contractor_payments (
        id                SERIAL PRIMARY KEY,
        contractor_id     INTEGER NOT NULL REFERENCES bb_contractors(id) ON DELETE CASCADE,
        amount            REAL NOT NULL,
        method            TEXT NOT NULL,
        bank_account_id   INTEGER REFERENCES bb_contractor_bank_accounts(id),
        payment_date      TEXT NOT NULL,
        reference_number  TEXT,
        budget_id         INTEGER REFERENCES bb_budgets(id),
        request_id        INTEGER REFERENCES bb_purchase_requests(id),
        memo              TEXT,
        status            TEXT DEFAULT 'paid',
        void_reason       TEXT,
        paid_by           INTEGER REFERENCES bb_users(id),
        created_at        TEXT DEFAULT (to_char(now(), 'YYYY-MM-DD HH24:MI:SS'))
    )''')

    # Links a payment to specific RoleCall events it covers — e.g. paying for
    # one class session, several sessions, or a whole program's worth at
    # once. RoleCall shares this same database, so this is just a plain TEXT
    # reference to events.id there (no real FK, since that table isn't ours
    # to constrain against — RoleCall's own schema changes shouldn't be able
    # to break BloomBooks migrations).
    c.execute('''CREATE TABLE IF NOT EXISTS bb_contractor_payment_events (
        id                  SERIAL PRIMARY KEY,
        payment_id          INTEGER NOT NULL REFERENCES bb_contractor_payments(id) ON DELETE CASCADE,
        rolecall_event_id   TEXT NOT NULL,
        created_at          TEXT DEFAULT (to_char(now(), 'YYYY-MM-DD HH24:MI:SS'))
    )''')
    c.execute('CREATE INDEX IF NOT EXISTS ix_bb_cpe_payment ON bb_contractor_payment_events(payment_id)')
    c.execute('CREATE INDEX IF NOT EXISTS ix_bb_cpe_event ON bb_contractor_payment_events(rolecall_event_id)')

    # ─── E-signature: templates & signing requests ─────────────────────────────
    c.execute('''CREATE TABLE IF NOT EXISTS bb_document_templates (
        id          SERIAL PRIMARY KEY,
        name        TEXT NOT NULL UNIQUE,
        doc_type    TEXT NOT NULL DEFAULT 'agreement',
        body        TEXT NOT NULL,
        created_by  INTEGER REFERENCES bb_users(id),
        created_at  TEXT DEFAULT (to_char(now(), 'YYYY-MM-DD HH24:MI:SS')),
        updated_at  TEXT DEFAULT (to_char(now(), 'YYYY-MM-DD HH24:MI:SS'))
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS bb_signing_requests (
        id                 SERIAL PRIMARY KEY,
        contractor_id      INTEGER NOT NULL REFERENCES bb_contractors(id) ON DELETE CASCADE,
        doc_type           TEXT NOT NULL,
        template_id        INTEGER REFERENCES bb_document_templates(id),
        title              TEXT,
        body_snapshot      TEXT,
        custom_fields      TEXT DEFAULT '{}',
        token              TEXT NOT NULL UNIQUE,
        status             TEXT DEFAULT 'pending',
        recipient_email    TEXT,
        expires_at         TEXT,
        created_by         INTEGER REFERENCES bb_users(id),
        created_at         TEXT DEFAULT (to_char(now(), 'YYYY-MM-DD HH24:MI:SS')),
        signed_at          TEXT,
        signer_name        TEXT,
        signer_ip          TEXT,
        signer_user_agent  TEXT,
        consent_at         TEXT,
        final_document_id  INTEGER REFERENCES bb_contractor_documents(id),
        void_reason        TEXT
    )''')

    c.execute('''INSERT INTO bb_document_templates (name, doc_type, body) VALUES (%s,%s,%s)
                 ON CONFLICT (name) DO NOTHING''',
        ('Standard Contractor Agreement', 'agreement', DEFAULT_AGREEMENT_TEMPLATE))
    c.execute('''INSERT INTO bb_document_templates (name, doc_type, body) VALUES (%s,%s,%s)
                 ON CONFLICT (name) DO NOTHING''',
        ('Instructor Agreement', 'agreement', INSTRUCTOR_AGREEMENT_TEMPLATE))

    c.execute("ALTER TABLE bb_production_members ADD COLUMN IF NOT EXISTS display_title TEXT DEFAULT ''")

    # Seed admin users
    def hash_pw(pw):
        return hashlib.sha256(pw.encode()).hexdigest()

    seed_users = [
        ('Admin User',     'admin@horizonwest.org',     hash_pw('admin123'),     'admin',     1),
        ('Treasurer',      'treasurer@horizonwest.org', hash_pw('treasurer123'), 'treasurer', 1),
        ('President',      'president@horizonwest.org', hash_pw('president123'), 'president', 1),
        ('Jane Volunteer', 'volunteer@horizonwest.org', hash_pw('volunteer123'), 'volunteer', 1),
    ]
    for u in seed_users:
        c.execute('''INSERT INTO bb_users (name,email,password,role,training_complete)
                     VALUES (%s,%s,%s,%s,%s) ON CONFLICT (email) DO NOTHING''', u)

    # Seed budgets (only if table is empty)
    c.execute('SELECT COUNT(*) AS n FROM bb_budgets')
    if c.fetchone()['n'] == 0:
        seed_budgets = [
            ('Spring Musical 2025', 'Production', '2024-2025', 3500),
            ('Fall Play 2025',      'Production', '2024-2025', 2000),
            ('Marketing & Outreach','Marketing',  '2024-2025', 800),
            ('General Operations',  'Operations', '2024-2025', 1200),
            ('Costumes & Wardrobe', 'Production', '2024-2025', 1500),
        ]
        for b in seed_budgets:
            c.execute('INSERT INTO bb_budgets (name,area,season,total_amount) VALUES (%s,%s,%s,%s)', b)

    # Seed training module (only if table is empty)
    c.execute('SELECT COUNT(*) AS n FROM bb_training_modules')
    if c.fetchone()['n'] == 0:
        sample_questions = json.dumps([
            {
                "question": "What must you do BEFORE making a purchase for HWTC?",
                "options": ["Buy it and submit a receipt later", "Get pre-approval from the Treasurer and President", "Ask a fellow volunteer", "Post in the group chat"],
                "correct": 1,
                "explanation": "All purchases require pre-approval through the purchasing system unless it is a genuine emergency."
            },
            {
                "question": "What qualifies as an emergency purchase?",
                "options": ["Anything under $20", "Items needed immediately that cannot wait for the approval process", "Anything from a thrift store", "Purchases made on weekends"],
                "correct": 1,
                "explanation": "Emergency purchases are items genuinely needed right away where waiting for approval is not possible — like a last-minute prop find at a thrift store during tech week."
            },
            {
                "question": "What do you need to submit with every purchase?",
                "options": ["Just the amount", "A receipt (photo or scan)", "An invoice from the vendor", "Nothing if it's under $10"],
                "correct": 1,
                "explanation": "A receipt is required for every purchase — even small ones. This protects you and the organization."
            },
            {
                "question": "Who gives final approval on all purchases?",
                "options": ["The Director", "Any board member", "The Treasurer only", "Both the Treasurer AND the President"],
                "correct": 3,
                "explanation": "Both the Treasurer and President must approve all purchases. The Treasurer reviews first, then the President gives final sign-off."
            },
            {
                "question": "What happens to your budget area when a purchase is approved?",
                "options": ["Nothing, budgets are tracked manually", "The approved amount is automatically deducted from your budget", "You notify the treasurer separately", "It updates at end of season"],
                "correct": 1,
                "explanation": "Budget tracking is automatic. Once a request is fully approved, the cost is deducted from your budget area so everyone can see remaining funds in real time."
            }
        ])
        c.execute('''INSERT INTO bb_training_modules (title, description, questions, pass_mark, is_active)
                     VALUES (%s, %s, %s, %s, %s)''',
                  ('HWTC Purchasing Policy Training',
                   'Complete this training before making any purchases for Horizon West Theater Company.',
                   sample_questions, 80, 1))

    conn.commit()
    conn.close()

    # ── Migrations — safely add any columns missing from older deployments ────
    conn = get_db()
    c = conn.cursor()
    migrations = [
        ("bb_budgets",           "production_id",     "INTEGER"),
        ("bb_budgets",           "parent_id",         "INTEGER"),
        ("bb_purchase_requests", "production_id",     "INTEGER"),
        ("bb_purchase_requests", "producer_note",     "TEXT"),
        ("bb_purchase_requests", "producer_acted_by", "INTEGER"),
        ("bb_purchase_requests", "producer_acted_at", "TEXT"),
        ("bb_purchase_requests", "purchase_method",   "TEXT DEFAULT 'in_store'"),
        ("bb_purchase_requests", "item_url",          "TEXT"),
        ("bb_purchase_requests", "authorized_by",     "TEXT"),
        ("bb_purchase_requests", "reimb_method",      "TEXT"),
        ("bb_purchase_requests", "reimb_handle",      "TEXT"),
        ("bb_purchase_requests", "needs_revision",    "INTEGER DEFAULT 0"),
        ("bb_purchase_requests", "revision_note",     "TEXT"),
        ("bb_purchase_requests", "statement_id",      "INTEGER"),
        ("bb_users",             "is_active",         "INTEGER DEFAULT 1"),
        ("bb_users",             "receipt_token",     "TEXT"),
        ("bb_users",             "reimb_method",      "TEXT"),
        ("bb_users",             "reimb_handle",      "TEXT"),
        # ─── Resident Producer / Build Show / budget approval workflow ─────────
        ("bb_productions",       "category",                   "TEXT"),
        ("bb_productions",       "source_licensing_request_id","TEXT"),
        ("bb_productions",       "source_rc_production_id",    "TEXT"),
        ("bb_productions",       "license_cost",               "REAL DEFAULT 0"),
        ("bb_productions",       "venue_rate",                 "REAL DEFAULT 0"),
        ("bb_productions",       "est_ticket_sales",           "REAL DEFAULT 0"),
        ("bb_productions",       "est_concessions",            "REAL DEFAULT 0"),
        ("bb_productions",       "est_enrollment",              "REAL DEFAULT 0"),
        ("bb_productions",       "rehearsals_per_week",        "REAL DEFAULT 0"),
        ("bb_productions",       "rehearsal_weeks",            "REAL DEFAULT 0"),
        ("bb_productions",       "rehearsal_hours_per_session","REAL DEFAULT 0"),
        ("bb_productions",       "rehearsal_weekly_hours",     "REAL DEFAULT 0"),
        ("bb_productions",       "studio_charge",              "REAL DEFAULT 0"),
        ("bb_productions",       "hard_costs_locked",          "INTEGER DEFAULT 0"),
        ("bb_productions",       "hard_costs_total",           "REAL DEFAULT 0"),
        ("bb_productions",       "board_approved",             "INTEGER DEFAULT 0"),
        ("bb_productions",       "board_approved_at",          "TEXT"),
        ("bb_productions",       "board_approved_by",          "INTEGER"),
    ]
    for table, column, col_type in migrations:
        c.execute("SELECT COUNT(*) AS n FROM information_schema.columns WHERE table_name=%s AND column_name=%s",
                  (table, column))
        if c.fetchone()['n'] == 0:
            c.execute(f'ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {col_type}')
    c.execute("UPDATE bb_users SET is_active=1 WHERE is_active IS NULL")
    conn.commit()
    conn.close()

# ─── Helpers ──────────────────────────────────────────────────────────────────
def hash_pw(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

def current_user():
    uid = session.get('user_id')
    if not uid:
        return None
    conn = get_db()
    u = conn.execute('SELECT * FROM bb_users WHERE id=?', (uid,)).fetchone()
    conn.close()
    return dict(u) if u else None

def require_auth(roles=None):
    u = current_user()
    if not u:
        return jsonify({'error': 'Not authenticated'}), 401
    if roles and u['role'] not in roles:
        return jsonify({'error': 'Insufficient permissions'}), 403
    return None

# ─── Production / budget permission helpers ───────────────────────────────────
ORG_APPROVER_ROLES = ('admin', 'treasurer', 'president')
# Resident Producer has full authority over show setup and budget-setting (same as
# admin/treasurer/president for productions/budgets specifically) but NOT over
# org-wide things like user management, contractors, or reimbursement payout —
# those stay on ORG_APPROVER_ROLES only. This lets HWTC onboard a Resident
# Producer who isn't a board member without granting full admin rights.
PRODUCTION_ADMIN_ROLES = ('admin', 'treasurer', 'president', 'resident_producer')

def get_production_producers(pid):
    """Return the list of producer users (id/name/email) for a production."""
    if not pid:
        return []
    conn = get_db()
    rows = conn.execute('''SELECT u.id, u.name, u.email
                           FROM bb_production_members m
                           JOIN bb_users u ON m.user_id = u.id
                           WHERE m.production_id=%s AND m.member_role=%s''',
                        (pid, 'producer')).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def is_producer_of(uid, pid):
    """True if the user is a producer on the given production."""
    if not uid or not pid:
        return False
    conn = get_db()
    row = conn.execute('''SELECT 1 FROM bb_production_members
                          WHERE user_id=%s AND production_id=%s AND member_role=%s''',
                       (uid, pid, 'producer')).fetchone()
    conn.close()
    return bool(row)

def user_owned_budget_ids(uid):
    """IDs of budgets this user is an assigned owner of (via bb_budget_members)."""
    if not uid:
        return []
    conn = get_db()
    rows = conn.execute('SELECT budget_id FROM bb_budget_members WHERE user_id=%s', (uid,)).fetchall()
    conn.close()
    return [r['budget_id'] for r in rows]

def user_can_use_budget(u, budget_id):
    """
    Can this user submit a purchase request against this budget?
      • Org approvers (admin/treasurer/president): any budget.
      • Anyone with can_submit_org_level: any org-level budget (production_id IS NULL).
      • Anyone: a budget they personally own (bb_budget_members).
      • Production budgets: any member of that production.
    """
    if not budget_id:
        return False
    if u['role'] in ORG_APPROVER_ROLES:
        return True
    conn = get_db()
    b = conn.execute('SELECT * FROM bb_budgets WHERE id=%s', (budget_id,)).fetchone()
    if not b:
        conn.close(); return False
    b = dict(b)
    owned = conn.execute('SELECT 1 FROM bb_budget_members WHERE user_id=%s AND budget_id=%s',
                         (u['id'], budget_id)).fetchone()
    if owned:
        conn.close(); return True
    if b.get('production_id'):
        member = conn.execute('SELECT 1 FROM bb_production_members WHERE user_id=%s AND production_id=%s',
                              (u['id'], b['production_id'])).fetchone()
        conn.close()
        return bool(member)
    # Org-level budget the user doesn't personally own → allowed only with general
    # org-level submit access (a lighter-weight grant than a full approver role).
    conn.close()
    return bool(u.get('can_submit_org_level'))

# ─── RoleCall "Rising Stars" revenue read-through ─────────────────────────────
# Both apps share the same Postgres database. RoleCall uses un-prefixed tables
# (productions, program_registrations); BloomBooks uses bb_ tables. We read
# RoleCall's enrollment revenue LIVE — nothing is copied, so it never goes stale.
# All money in RoleCall is stored as integer CENTS.
RISING_STARS_SOURCE = 'Rising Stars Enrollment (RoleCall)'

def get_rolecall_link(conn, pid):
    """Return the link row for a BloomBooks production, or None."""
    try:
        row = conn.execute('SELECT * FROM bb_rolecall_links WHERE bb_production_id=%s', (pid,)).fetchone()
        return dict(row) if row else None
    except Exception:
        return None

def get_rolecall_rising_stars_revenue(conn, rc_production_id):
    """
    Live revenue for one RoleCall Rising Stars production, converted to dollars.
      confirmed_dollars → money actually in hand (maps to BloomBooks 'actual').
        Prefers the real Square-confirmed amount over price × participant_count
        (the two can drift — promo codes, price changes after the fact). A
        Step Up hold only counts once it's actually been charged — a pending
        hold is a promise, not money collected yet. Comp'd enrollments are $0
        by design. Deduped by order, since sibling registrations sharing one
        Square order all carry that order's full amount.
      pending_dollars → additional amount still outstanding: pending
        (uncharged) Step Up holds, plus waitlisted registrations that could
        still convert. Added to confirmed_dollars downstream for 'expected'.
    Returns None if the RoleCall production can't be read (e.g. tables absent).
    """
    if not rc_production_id:
        return None
    try:
        row = conn.execute('''WITH dedup_regs AS (
            SELECT DISTINCT ON (COALESCE(pr.square_order_id, pr.id))
                pr.id, pr.status,
                CASE WHEN pr.is_comped THEN 0
                    WHEN su.hold_status = 'charged' THEN su.amount
                    WHEN su.hold_status = 'pending' THEN 0
                    ELSE COALESCE(pr.amount_paid_cents,
                        COALESCE(prod.price,0) * COALESCE(pr.participant_count,1)
                        - COALESCE(pr.discount_amount,0) - COALESCE(pr.sibling_discount_amount,0))
                END AS confirmed_amount,
                CASE WHEN NOT pr.is_comped AND su.hold_status = 'pending' THEN su.amount ELSE 0 END AS pending_step_up_amount
            FROM program_registrations pr
            JOIN productions prod ON prod.id = pr.production_id
            LEFT JOIN step_up_child_holds su ON su.registration_id = pr.id
            WHERE pr.production_id = %s AND pr.status != 'cancelled'
            ORDER BY COALESCE(pr.square_order_id, pr.id), pr.id
        ),
        waitlist_amt AS (
            SELECT pr.id,
                CASE WHEN pr.is_comped THEN 0 ELSE
                    COALESCE(prod.price,0) * COALESCE(pr.participant_count,1)
                    - COALESCE(pr.discount_amount,0) - COALESCE(pr.sibling_discount_amount,0)
                END AS amount
            FROM program_registrations pr JOIN productions prod ON prod.id=pr.production_id
            WHERE pr.production_id = %s AND pr.status = 'waitlisted'
        )
        SELECT prod.name AS rc_name,
            COALESCE((SELECT SUM(confirmed_amount) FROM dedup_regs WHERE status='confirmed'), 0) AS confirmed_cents,
            COALESCE((SELECT SUM(pending_step_up_amount) FROM dedup_regs WHERE status='confirmed'), 0) AS pending_step_up_cents,
            COALESCE((SELECT SUM(amount) FROM waitlist_amt), 0) AS waitlist_cents,
            (SELECT COUNT(*) FROM dedup_regs WHERE status='confirmed') AS confirmed_regs,
            (SELECT COUNT(*) FROM program_registrations WHERE production_id=%s AND status='pending_payment') AS pending_regs
        FROM productions prod WHERE prod.id = %s''',
            (rc_production_id, rc_production_id, rc_production_id, rc_production_id)).fetchone()
    except Exception as e:
        app.logger.warning(f'RoleCall revenue read failed for {rc_production_id}: {e}')
        return None
    if not row:
        return None
    row = dict(row)
    confirmed = int(row.get('confirmed_cents') or 0) / 100.0
    pending_step_up = int(row.get('pending_step_up_cents') or 0) / 100.0
    waitlist = int(row.get('waitlist_cents') or 0) / 100.0
    return {
        'rc_production_id':   rc_production_id,
        'rc_production_name': row.get('rc_name'),
        'confirmed_dollars':  round(confirmed, 2),
        'pending_dollars':    round(pending_step_up + waitlist, 2),
        'confirmed_regs':     int(row.get('confirmed_regs') or 0),
        'pending_regs':       int(row.get('pending_regs') or 0),
    }

def rolecall_revenue_line(conn, pid):
    """
    Build a synthetic, read-only revenue row for the production's Revenue tab,
    or None if there's no link. Shaped like a bb_production_revenue row so the
    frontend can render it inline.
    """
    link = get_rolecall_link(conn, pid)
    if not link:
        return None
    rev = get_rolecall_rising_stars_revenue(conn, link['rc_production_id'])
    if rev is None:
        # Link exists but RoleCall is unreachable — surface a zeroed, flagged row.
        return {
            'id': None, 'source': RISING_STARS_SOURCE,
            'description': 'Linked to RoleCall, but live data is currently unavailable.',
            'expected': 0, 'actual': 0, 'received_date': None,
            'rolecall_live': True, 'readonly': True, 'available': False,
            'rc_production_id': link['rc_production_id'],
            'rc_production_name': link.get('rc_production_name'),
        }
    desc = (f"{rev['confirmed_regs']} confirmed"
            + (f", {rev['pending_regs']} pending" if rev['pending_regs'] else '')
            + " — live from RoleCall")
    return {
        'id': None, 'source': RISING_STARS_SOURCE, 'description': desc,
        'expected': round(rev['confirmed_dollars'] + rev['pending_dollars'], 2),
        'actual':   rev['confirmed_dollars'],
        'received_date': None,
        'rolecall_live': True, 'readonly': True, 'available': True,
        'rc_production_id': rev['rc_production_id'],
        'rc_production_name': rev['rc_production_name'],
        'confirmed_regs': rev['confirmed_regs'], 'pending_regs': rev['pending_regs'],
    }

def log_action(user_id, action, entity_type=None, entity_id=None, detail=None):
    conn = get_db()
    conn.execute('INSERT INTO bb_audit_log (user_id,action,entity_type,entity_id,detail) VALUES (%s,%s,%s,%s,%s)',
                 (user_id, action, entity_type, entity_id, detail))
    conn.commit()
    conn.close()

def send_email(to, subject, body_html):
    """Send via Resend API. 'to' can be a string email or list of strings."""
    if not RESEND_API_KEY:
        print(f"[EMAIL SKIPPED — no RESEND_API_KEY] To:{to} | {subject}")
        return False
    if isinstance(to, str):
        to_list = [t.strip() for t in to.split(',') if t.strip()]
    else:
        to_list = [t for t in to if t]
    if not to_list:
        print(f"[EMAIL SKIPPED — empty recipients] {subject}")
        return False
    print(f"[EMAIL] Sending to {to_list} | {subject}")
    try:
        resp = req_lib.post(
            'https://api.resend.com/emails',
            headers={'Authorization': f'Bearer {RESEND_API_KEY}', 'Content-Type': 'application/json'},
            json={'from': FROM_EMAIL, 'to': to_list, 'subject': subject, 'html': body_html},
            timeout=10
        )
        print(f"[EMAIL] Resend response: {resp.status_code} {resp.text[:200]}")
        if resp.status_code not in (200, 201, 202):
            return False
        return True
    except Exception as e:
        print(f"[EMAIL ERROR] {e}")
        return False

def get_user_email(user_id):
    conn = get_db()
    u = conn.execute('SELECT email,name FROM bb_users WHERE id=%s', (user_id,)).fetchone()
    conn.close()
    return dict(u) if u else None

def get_role_emails(role):
    conn = get_db()
    users = conn.execute('SELECT email,name FROM bb_users WHERE role=%s AND is_active=1', (role,)).fetchall()
    conn.close()
    return [dict(u) for u in users]

def get_admin_emails():
    """Return emails for president + treasurer — org-level approvers."""
    conn = get_db()
    users = conn.execute(
        "SELECT email,name FROM bb_users WHERE role IN ('president','treasurer','admin') AND is_active=1"
    ).fetchall()
    conn.close()
    return [dict(u) for u in users]

def email_html(title, body, cta_text=None, cta_url=None):
    cta = ''
    if cta_text:
        cta = (
            f'<a href="{cta_url}" style="display:inline-block;margin-top:16px;padding:10px 20px;'
            f'background:#0f6e56;color:#fff;text-decoration:none;border-radius:6px;font-size:14px">'
            f'{cta_text}</a>'
        )
    return (
        '<!DOCTYPE html><html><body>'
        '<div style="font-family:-apple-system,BlinkMacSystemFont,sans-serif;max-width:540px;margin:0 auto;padding:24px">'
        '<div style="background:#0f6e56;color:#fff;padding:18px 24px;border-radius:10px 10px 0 0">'
        '<p style="margin:0 0 4px;font-size:11px;opacity:.6;text-transform:uppercase;letter-spacing:.8px">Horizon West Theater Company</p>'
        f'<h2 style="margin:0;font-size:20px;font-weight:700">{title}</h2>'
        '</div>'
        '<div style="background:#f9f9f7;border:1px solid #e0ddd6;border-top:none;padding:22px 24px;border-radius:0 0 10px 10px">'
        f'{body}{cta}'
        '<hr style="border:none;border-top:1px solid #e0ddd6;margin:20px 0 14px">'
        '<p style="margin:0;font-size:11px;color:#aaa">BloomBooks &middot; Horizon West Theater Company &middot; Suite 108</p>'
        '</div></div></body></html>'
    )

# ── Notification helpers ───────────────────────────────────────────────────────

def notify_request_submitted(req_id, req_title, submitter_name, submitter_email,
                              estimated_cost, req_type, purchase_method, item_url,
                              production_id, status):
    """Fire notifications when a new request is submitted."""
    type_label   = 'SAP (Self-Authorized Purchase)' if req_type == 'sap' else 'Pre-approval request'
    method_label = 'Online' if purchase_method == 'online' else 'In-store'
    amount_str   = f'${float(estimated_cost):.2f}'
    url_line     = f'<p style="margin:8px 0"><a href="{item_url}" style="color:#0f6e56">{item_url}</a></p>' if item_url else ''
    body = (
        f'<p><strong>{submitter_name}</strong> submitted a <strong>{type_label}</strong>.</p>'
        f'<table style="width:100%;border-collapse:collapse;margin:12px 0">'
        f'<tr><td style="padding:6px 0;color:#666;width:120px">Item</td><td style="padding:6px 0;font-weight:600">{req_title}</td></tr>'
        f'<tr><td style="padding:6px 0;color:#666">Amount</td><td style="padding:6px 0;font-weight:600">{amount_str}</td></tr>'
        f'<tr><td style="padding:6px 0;color:#666">Method</td><td style="padding:6px 0">{method_label}</td></tr>'
        f'</table>{url_line}'
    )

    if status == 'pending_producer' and production_id:
        for p in get_production_producers(production_id):
            send_email(p['email'], f'Purchase request needs your approval: {req_title}',
                email_html('New Purchase Request — Producer Review Needed', body,
                           'Review in BloomBooks', APP_URL))
        if req_type == 'sap':
            sap_note = '<p style="color:#c97c10;font-size:13px">This SAP still requires your approval after producer review.</p>'
            for a in get_admin_emails():
                send_email(a['email'], f'SAP submitted (FYI): {req_title}',
                    email_html('SAP Submitted — FYI', body + sap_note, 'View in BloomBooks', APP_URL))
    else:
        prefix = 'SAP' if req_type == 'sap' else 'New request'
        for a in get_admin_emails():
            send_email(a['email'], f'{prefix}: {req_title}',
                email_html('New Purchase Request — Review Needed', body, 'Review in BloomBooks', APP_URL))

    # Confirm receipt to submitter
    confirm_body = (
        f'<p>Hi {submitter_name.split()[0]}, your purchase request has been submitted and is in the approval queue.</p>'
        f'<table style="width:100%;border-collapse:collapse;margin:12px 0">'
        f'<tr><td style="padding:5px 0;color:#666;width:100px">Item</td><td style="padding:5px 0;font-weight:600">{req_title}</td></tr>'
        f'<tr><td style="padding:5px 0;color:#666">Amount</td><td style="padding:5px 0">{amount_str}</td></tr>'
        f'<tr><td style="padding:5px 0;color:#666">Type</td><td style="padding:5px 0">{type_label}</td></tr>'
        f'</table>'
        f'<p style="font-size:13px;color:#666">You will receive updates by email as it moves through the approval process.</p>'
    )
    send_email(submitter_email, f'Request received: {req_title}',
        email_html('Your Request Was Received', confirm_body, 'View in BloomBooks', APP_URL))


def notify_request_status_change(req_id, req_title, submitter_id, new_status,
                                  acted_by_name, note, production_id, estimated_cost, actual_cost=None):
    """Notify relevant parties when a request status changes."""
    submitter = get_user_email(submitter_id)
    amount    = f'${float(actual_cost or estimated_cost):.2f}'
    note_html = f'<p><em>Note: {note}</em></p>' if note else ''

    if new_status == 'pending_treasurer':
        if submitter:
            send_email(submitter['email'], f'✓ Producer approved: {req_title}',
                email_html('Producer Approved — Awaiting Treasurer',
                    f'<p>The producer approved your request for <strong>{req_title}</strong>. It is now with the Treasurer for review.</p>{note_html}',
                    'View in BloomBooks', APP_URL))
        for a in get_admin_emails():
            send_email(a['email'], f'Awaiting treasurer review: {req_title}',
                email_html('Producer Approved — Treasurer Review Needed',
                    f'<p><strong>{acted_by_name}</strong> approved <strong>{req_title}</strong> ({amount}). Treasurer review needed.</p>{note_html}',
                    'Review in BloomBooks', APP_URL))

    elif new_status == 'pending_president':
        if submitter:
            send_email(submitter['email'], f'✓ Treasurer approved: {req_title}',
                email_html('Treasurer Approved — Awaiting President',
                    f'<p>The treasurer approved your request for <strong>{req_title}</strong>. Awaiting president sign-off.</p>{note_html}',
                    'View in BloomBooks', APP_URL))
        for a in get_role_emails('president'):
            send_email(a['email'], f'Final sign-off needed: {req_title}',
                email_html('President Sign-Off Needed',
                    f'<p>Treasurer approved <strong>{req_title}</strong> ({amount}). Needs your final approval.</p>{note_html}',
                    'Review in BloomBooks', APP_URL))

    elif new_status == 'approved':
        if submitter:
            approved_body = (
                f'<p>Your request for <strong>{req_title}</strong> ({amount}) has been <strong>fully approved</strong>!</p>'
                '<p>You are cleared to purchase. Keep your receipt — submit it through BloomBooks for reimbursement.</p>'
                + note_html
            )
            send_email(submitter['email'], f'Approved — go buy it! {req_title}',
                email_html('Purchase Approved! ✓', approved_body, 'View in BloomBooks', APP_URL))

    elif new_status == 'denied':
        if submitter:
            denied_body = (
                f'<p>Your request for <strong>{req_title}</strong> was not approved at this time.</p>'
                + (f'<p><strong>Reason:</strong> {note}</p>' if note else '')
                + '<p style="font-size:13px;color:#666">Please reach out to the treasurer or producer with any questions.</p>'
            )
            send_email(submitter['email'], f'Request not approved: {req_title}',
                email_html('Purchase Request — Not Approved', denied_body))
        denied_admin_body = f'<p><strong>{acted_by_name}</strong> denied the request for <strong>{req_title}</strong>.</p>{note_html}'
        for a in get_admin_emails():
            send_email(a['email'], f'Request denied: {req_title}',
                email_html('Request Denied', denied_admin_body))
        if production_id:
            for p in get_production_producers(production_id):
                send_email(p['email'], f'Request denied: {req_title}',
                    email_html('Request Denied', denied_admin_body))


def notify_reimbursement_paid(user_id, amount, method, req_title):
    """Notify volunteer their reimbursement has been processed."""
    u = get_user_email(user_id)
    if not u:
        return
    paid_body = (
        f'<p>Hi {u["name"].split()[0]}, your reimbursement has been processed.</p>'
        f'<table style="width:100%;border-collapse:collapse;margin:12px 0">'
        f'<tr><td style="padding:5px 0;color:#666;width:100px">Request</td><td style="padding:5px 0;font-weight:600">{req_title}</td></tr>'
        f'<tr><td style="padding:5px 0;color:#666">Amount</td><td style="padding:5px 0;font-weight:600">${float(amount):.2f}</td></tr>'
        f'<tr><td style="padding:5px 0;color:#666">Method</td><td style="padding:5px 0">{method or "—"}</td></tr>'
        f'</table>'
        f'<p style="font-size:13px;color:#666">Thank you for your contribution to Horizon West Theater Company!</p>'
    )
    send_email(u['email'], f'Reimbursement processed: ${float(amount):.2f}',
        email_html('You Have Been Reimbursed!', paid_body))


def notify_welcome(name, email, temp_password, role):
    """Welcome email to newly created user with their login details."""
    role_label = role.replace('_', ' ').title()
    welcome_body = (
        f'<p>An account has been created for you in BloomBooks, the purchasing, reimbursement, and contractor management system for Horizon West Theater Company.</p>'
        f'<table style="width:100%;border-collapse:collapse;margin:12px 0;background:#fff;border:1px solid #e0ddd6;border-radius:6px">'
        f'<tr><td style="padding:8px 12px;color:#666;border-bottom:1px solid #e0ddd6;width:100px">Email</td><td style="padding:8px 12px;font-weight:600;border-bottom:1px solid #e0ddd6">{email}</td></tr>'
        f'<tr><td style="padding:8px 12px;color:#666;border-bottom:1px solid #e0ddd6">Password</td><td style="padding:8px 12px;font-weight:600;color:#0f6e56;border-bottom:1px solid #e0ddd6">{temp_password}</td></tr>'
        f'<tr><td style="padding:8px 12px;color:#666">Role</td><td style="padding:8px 12px">{role_label}</td></tr>'
        f'</table>'
        f'<p style="font-size:13px;color:#666">Please sign in and complete your purchasing training before submitting any requests.</p>'
    )
    send_email(email, 'Welcome to BloomBooks — Horizon West Theater Company',
        email_html(f'Welcome to BloomBooks, {name.split()[0]}!', welcome_body, 'Sign in to BloomBooks', APP_URL))


# ─── Auth routes ─────────────────────────────────────────────────────────────
@app.route('/api/auth/login', methods=['POST'])
def login():
    data = request.json
    conn = get_db()
    u = conn.execute('SELECT * FROM bb_users WHERE email=%s AND password=%s',
                     (data['email'].strip().lower(), hash_pw(data['password']))).fetchone()
    conn.close()
    if not u:
        return jsonify({'error': 'Invalid email or password'}), 401
    session['user_id'] = u['id']
    return jsonify({'user': dict(u)})

@app.route('/api/auth/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({'ok': True})

@app.route('/api/auth/me', methods=['GET'])
def me():
    u = current_user()
    if not u:
        return jsonify({'user': None})
    return jsonify({'user': u})

@app.route('/api/auth/register', methods=['POST'])
def register():
    # Public self-registration is disabled. Accounts are created by an
    # admin/treasurer/president via the Users page (/api/users/create).
    return jsonify({'error': 'Self-registration is disabled. Please contact an administrator for access.'}), 403

# ─── Users (admin) ───────────────────────────────────────────────────────────
@app.route('/api/users', methods=['GET'])
def list_users():
    err = require_auth(['admin', 'treasurer', 'president'])
    if err: return err
    conn = get_db()
    users = conn.execute('SELECT id,name,email,role,training_complete,created_at FROM bb_users ORDER BY name').fetchall()
    conn.close()
    return jsonify([dict(u) for u in users])

@app.route('/api/users/create', methods=['POST'])
def create_user():
    err = require_auth(['admin','treasurer','president'])
    if err: return err
    data = request.json
    name     = data.get('name', '').strip()
    email    = data.get('email', '').strip().lower()
    password = data.get('password', '')
    role     = data.get('role', 'volunteer')
    training = int(data.get('training_complete', 0))
    if not name or not email or not password:
        return jsonify({'error': 'Name, email and password are required'}), 400
    conn = get_db()
    try:
        conn.execute('INSERT INTO bb_users (name,email,password,role,training_complete,is_active) VALUES (%s,%s,%s,%s,%s,%s)',
                     (name, email, hash_pw(password), role, training, 1))
        conn.commit()
        conn.close()
        notify_welcome(name, email, password, role)
        return jsonify({'ok': True})
    except psycopg2.IntegrityError:
        conn.close()
        return jsonify({'error': 'An account with that email already exists'}), 409


    err = require_auth(['admin'])
    if err: return err
    data = request.json
    conn = get_db()
    if 'role' in data:
        conn.execute('UPDATE bb_users SET role=%s WHERE id=%s', (data['role'], uid))
    if 'training_complete' in data:
        conn.execute('UPDATE bb_users SET training_complete=%s WHERE id=%s', (data['training_complete'], uid))
    if 'password' in data and data['password']:
        conn.execute('UPDATE bb_users SET password=%s WHERE id=%s', (hash_pw(data['password']), uid))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

# ─── Budgets ─────────────────────────────────────────────────────────────────
@app.route('/api/budgets', methods=['GET'])
def list_budgets():
    err = require_auth()
    if err: return err
    u = current_user()
    conn = get_db()
    is_admin = u['role'] in ('admin','treasurer','president')
    can_org = is_admin or bool(u.get('can_submit_org_level'))
    owned_ids = user_owned_budget_ids(u['id'])
    if is_admin:
        budgets = conn.execute('''SELECT b.*,p.name as production_name FROM bb_budgets b
                                  LEFT JOIN bb_productions p ON b.production_id=p.id
                                  ORDER BY b.is_active DESC,p.name,b.name''').fetchall()
    else:
        my_ids = [r['production_id'] for r in
                  conn.execute('SELECT production_id FROM bb_production_members WHERE user_id=%s',(u['id'],)).fetchall()]
        # Non-admins see: production budgets for their productions + any budget they own
        # + (if granted) every org-level budget, even ones they don't personally own.
        clauses, params = [], []
        if my_ids:
            ph = ','.join(['%s']*len(my_ids))
            clauses.append(f'b.production_id IN ({ph})'); params.extend(my_ids)
        if owned_ids:
            ph = ','.join(['%s']*len(owned_ids))
            clauses.append(f'b.id IN ({ph})'); params.extend(owned_ids)
        if can_org:
            clauses.append('b.production_id IS NULL')
        if clauses:
            where = '(' + ' OR '.join(clauses) + ') AND b.is_active=1'
            budgets = conn.execute(f'''SELECT b.*,p.name as production_name FROM bb_budgets b
                                       LEFT JOIN bb_productions p ON b.production_id=p.id
                                       WHERE {where}''', params).fetchall()
        else:
            budgets = []
    conn.close()
    owned_set = set(owned_ids)
    result = []
    for b in budgets:
        bd = dict(b)
        bd['i_own'] = bd['id'] in owned_set
        result.append(bd)
    return jsonify(result)

@app.route('/api/budgets', methods=['POST'])
def create_budget():
    u = current_user()
    if not u: return jsonify({'error':'Not authenticated'}),401
    data = request.json
    prod_id   = data.get('production_id') or None
    parent_id = data.get('parent_id') or None
    if u['role'] not in ('admin','treasurer','president','resident_producer','producer'):
        if not prod_id or not is_producer_of(u['id'],int(prod_id)):
            return jsonify({'error':'Insufficient permissions'}),403
    # Parent categories have no amount of their own — children roll up to them
    amount = 0 if data.get('is_category') else float(data.get('total_amount', 0))
    conn = get_db()
    conn.execute('INSERT INTO bb_budgets (name,area,season,total_amount,production_id,parent_id) VALUES (%s,%s,%s,%s,%s,%s)',
                 (data['name'], data.get('area','General'), data.get('season',''), amount, prod_id, parent_id))
    conn.commit(); conn.close()
    return jsonify({'ok':True})

@app.route('/api/budgets/<int:bid>', methods=['PATCH'])
def update_budget(bid):
    u = current_user()
    if not u: return jsonify({'error':'Not authenticated'}),401
    conn = get_db()
    b = conn.execute('SELECT * FROM bb_budgets WHERE id=%s',(bid,)).fetchone()
    if not b: conn.close(); return jsonify({'error':'Not found'}),404
    b = dict(b)
    if u['role'] not in ('admin','treasurer','president','resident_producer'):
        if not b.get('production_id') or not is_producer_of(u['id'],b['production_id']):
            conn.close(); return jsonify({'error':'Insufficient permissions'}),403
    data = request.json
    fields,vals = [],[]
    for f in ['name','area','season','total_amount','is_active','parent_id']:
        if f in data:
            fields.append(f'{f}=%s'); vals.append(data[f])
    if fields:
        vals.append(bid)
        conn.execute(f'UPDATE bb_budgets SET {",".join(fields)} WHERE id=%s', vals)
        conn.commit()
    conn.close()
    return jsonify({'ok':True})

@app.route('/api/budgets/<int:bid>', methods=['DELETE'])
def delete_budget(bid):
    u = current_user()
    if not u: return jsonify({'error':'Not authenticated'}),401
    conn = get_db()
    b = conn.execute('SELECT * FROM bb_budgets WHERE id=%s',(bid,)).fetchone()
    if not b: conn.close(); return jsonify({'error':'Not found'}),404
    b = dict(b)
    if u['role'] not in ('admin','treasurer','president','resident_producer'):
        if not b.get('production_id') or not is_producer_of(u['id'],b['production_id']):
            conn.close(); return jsonify({'error':'Insufficient permissions'}),403
    conn.execute('UPDATE bb_purchase_requests SET budget_id=NULL WHERE budget_id=%s',(bid,))
    conn.execute('DELETE FROM bb_budget_members WHERE budget_id=%s',(bid,))
    conn.execute('UPDATE bb_budgets SET parent_id=NULL WHERE parent_id=%s',(bid,))
    conn.execute('DELETE FROM bb_budgets WHERE id=%s',(bid,))
    conn.commit(); conn.close()
    log_action(u['id'],'deleted_budget','budget',bid,b['name'])
    return jsonify({'ok':True})

# ─── Purchase Requests ────────────────────────────────────────────────────────
@app.route('/api/requests', methods=['GET'])
def list_requests():
    err = require_auth()
    if err: return err
    u = current_user()
    conn = get_db()
    is_admin = u['role'] in ('admin','treasurer','president')
    producer_ids = [r['production_id'] for r in
                    conn.execute('SELECT production_id FROM bb_production_members WHERE user_id=%s AND member_role=%s',
                                 (u['id'],'producer')).fetchall()]
    owned_budget_rows = conn.execute('SELECT budget_id FROM bb_budget_members WHERE user_id=%s',(u['id'],)).fetchall()
    owned_budget_ids = [r['budget_id'] for r in owned_budget_rows]

    base = '''SELECT r.*,
                sub.name as submitter_name, sub.email as submitter_email,
                b.name as budget_name, b.area as budget_area,
                b.total_amount as budget_total, b.spent as budget_spent,
                p.name as production_name
              FROM bb_purchase_requests r
              LEFT JOIN bb_users sub ON r.submitted_by=sub.id
              LEFT JOIN bb_budgets b ON r.budget_id=b.id
              LEFT JOIN bb_productions p ON r.production_id=p.id'''

    status_filter = request.args.get('status')
    mine_only     = request.args.get('mine') == '1'
    prod_filter   = request.args.get('production_id')
    conditions, params = [], []

    if mine_only:
        conditions.append('r.submitted_by=%s'); params.append(u['id'])
    elif not is_admin:
        sub_conds = ['r.submitted_by=%s']
        sub_params = [u['id']]
        if producer_ids:
            ph = ','.join(['%s']*len(producer_ids))
            sub_conds.append(f'r.production_id IN ({ph})')
            sub_params.extend(producer_ids)
        if owned_budget_ids:
            ph = ','.join(['%s']*len(owned_budget_ids))
            sub_conds.append(f'r.budget_id IN ({ph})')
            sub_params.extend(owned_budget_ids)
        conditions.append(f'({" OR ".join(sub_conds)})'); params.extend(sub_params)

    if status_filter: conditions.append('r.status=%s'); params.append(status_filter)
    if prod_filter:   conditions.append('r.production_id=%s'); params.append(int(prod_filter))
    if conditions: base += ' WHERE ' + ' AND '.join(conditions)
    base += ' ORDER BY r.submitted_at DESC'

    rows = conn.execute(base, params).fetchall()
    conn.close()
    result = []
    for row in rows:
        r = dict(row)
        conn2 = get_db()
        receipts = conn2.execute('SELECT * FROM bb_receipts WHERE request_id=%s',(r['id'],)).fetchall()
        r['receipts'] = [dict(rec) for rec in receipts]
        conn2.close()
        result.append(r)
    return jsonify(result)

@app.route('/api/requests', methods=['POST'])
def create_request():
    err = require_auth()
    if err: return err
    u = current_user()
    if not u['training_complete'] and u['role'] == 'volunteer':
        return jsonify({'error': 'You must complete purchasing training before submitting requests.'}), 403
    data = request.json
    is_sap   = 1 if data.get('is_sap') else 0
    req_type = 'sap' if is_sap else 'pre_approval'
    purchase_method = data.get('purchase_method', 'in_store')
    item_url = data.get('item_url', '')
    prod_id  = data.get('production_id') or None
    budget_id = data.get('budget_id') or None
    # Enforce budget permissions — block org-level budgets for non-owners, etc.
    if budget_id and not user_can_use_budget(u, int(budget_id)):
        return jsonify({'error': 'You are not permitted to submit against that budget.'}), 403
    if prod_id and get_production_producers(int(prod_id)):
        status = 'pending_producer'
    else:
        status = 'pending_treasurer'
    conn = get_db()
    conn.execute(
        '''INSERT INTO bb_purchase_requests
           (type,status,title,description,vendor,estimated_cost,budget_id,production_id,
            submitted_by,is_emergency,emergency_reason,purchase_method,item_url)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
        (req_type, status, data['title'], data.get('description',''), data.get('vendor',''),
         float(data['estimated_cost']), data.get('budget_id') or None,
         prod_id, u['id'], is_sap, data.get('sap_reason',''), purchase_method, item_url)
    )
    row = conn.execute('SELECT lastval() AS id').fetchone()
    req_id = row['id']
    conn.commit()
    conn.close()
    log_action(u['id'], 'submitted_request', 'request', req_id, data['title'])
    notify_request_submitted(
        req_id=req_id, req_title=data['title'],
        submitter_name=u['name'], submitter_email=u['email'],
        estimated_cost=data['estimated_cost'], req_type=req_type,
        purchase_method=purchase_method, item_url=item_url,
        production_id=int(prod_id) if prod_id else None, status=status
    )
    return jsonify({'ok': True, 'id': req_id})

@app.route('/api/requests/<int:rid>', methods=['DELETE'])
def delete_request(rid):
    u = current_user()
    if not u: return jsonify({'error':'Not authenticated'}),401
    conn = get_db()
    req = conn.execute('SELECT * FROM bb_purchase_requests WHERE id=%s',(rid,)).fetchone()
    if not req: conn.close(); return jsonify({'error':'Not found'}),404
    req = dict(req)
    # Submitter can delete their own pending requests; admins/treasurer/president can delete anything
    is_admin = u['role'] in ('admin','treasurer','president')
    is_owner = req['submitted_by'] == u['id']
    is_pending = req['status'].startswith('pending_')
    if not is_admin and not (is_owner and is_pending):
        conn.close()
        return jsonify({'error': 'You can only delete your own pending requests'}),403
    # Clean up related records first
    conn.execute('DELETE FROM bb_receipts WHERE request_id=%s',(rid,))
    conn.execute('DELETE FROM bb_reimbursements WHERE request_id=%s',(rid,))
    # If already approved, reverse the budget spend
    if req['status'] in ('approved','reimbursed') and req.get('budget_id') and req.get('actual_cost'):
        conn.execute('UPDATE bb_budgets SET spent=GREATEST(0,spent-%s) WHERE id=%s',
                     (req['actual_cost'], req['budget_id']))
    conn.execute('DELETE FROM bb_purchase_requests WHERE id=%s',(rid,))
    conn.commit(); conn.close()
    log_action(u['id'],'deleted_request','request',rid,req['title'])
    return jsonify({'ok':True})

@app.route('/api/requests/<int:rid>/approve', methods=['POST'])
def approve_request(rid):
    err = require_auth(['treasurer', 'president', 'admin'])
    if err: return err
    u = current_user()
    data = request.json
    action = data.get('action')  # 'approve' or 'deny'
    note   = data.get('note', '')

    conn = get_db()
    req = conn.execute('SELECT * FROM bb_purchase_requests WHERE id=?', (rid,)).fetchone()
    if not req:
        conn.close()
        return jsonify({'error': 'Request not found'}), 404

    req = dict(req)
    new_status = req['status']
    now = datetime.now().isoformat()

    if u['role'] in ('treasurer', 'admin') and req['status'] == 'pending_treasurer':
        if action == 'approve':
            new_status = 'pending_president'
            conn.execute('UPDATE bb_purchase_requests SET status=%s,treasurer_note=%s,treasurer_acted_by=%s,treasurer_acted_at=%s,updated_at=%s WHERE id=?',
                         (new_status, note, u['id'], now, now, rid))
        else:
            new_status = 'denied'
            conn.execute('UPDATE bb_purchase_requests SET status=%s,treasurer_note=%s,treasurer_acted_by=%s,treasurer_acted_at=%s,updated_at=%s WHERE id=?',
                         (new_status, note, u['id'], now, now, rid))

    elif u['role'] in ('president', 'admin') and req['status'] == 'pending_president':
        if action == 'approve':
            new_status = 'approved'
            actual = float(data.get('actual_cost', req['estimated_cost']))
            conn.execute('UPDATE bb_purchase_requests SET status=%s,president_note=%s,president_acted_by=%s,president_acted_at=%s,actual_cost=%s,updated_at=%s WHERE id=?',
                         (new_status, note, u['id'], now, actual, now, rid))
            # update budget
            conn.execute('UPDATE bb_budgets SET spent=spent+? WHERE id=?', (actual, req['budget_id']))
            # create reimbursement record
            conn.execute('INSERT INTO bb_reimbursements (request_id,user_id,amount) VALUES (%s,%s,%s)',
                         (rid, req['submitted_by'], actual))
        else:
            new_status = 'denied'
            conn.execute('UPDATE bb_purchase_requests SET status=%s,president_note=%s,president_acted_by=%s,president_acted_at=%s,updated_at=%s WHERE id=?',
                         (new_status, note, u['id'], now, now, rid))

    else:
        conn.close()
        return jsonify({'error': 'Action not permitted at this stage'}), 400

    conn.commit()
    conn.close()

    log_action(u['id'], f'{action}d_request', 'request', rid, f'status→{new_status}')
    notify_request_status_change(
        req_id=rid, req_title=req['title'],
        submitter_id=req['submitted_by'], new_status=new_status,
        acted_by_name=u['name'], note=note,
        production_id=req.get('production_id'),
        estimated_cost=req['estimated_cost'],
        actual_cost=req.get('actual_cost')
    )
    return jsonify({'ok': True, 'new_status': new_status})

@app.route('/api/debug/test-email', methods=['POST'])
def test_email():
    u = current_user()
    if not u: return jsonify({'error': 'Not authenticated'}), 401
    ok = send_email(u['email'], 'BloomBooks test email',
        email_html('Test Email', f'<p>This is a test from BloomBooks. If you can see this, emails are working!</p><p>Sent to: {u["email"]}</p>'))
    return jsonify({'ok': ok, 'sent_to': u['email'], 'resend_configured': bool(RESEND_API_KEY), 'from': FROM_EMAIL})
def debug_config():
    conn = get_db()
    receipt_count = conn.execute('SELECT COUNT(*) as n FROM bb_receipts').fetchone()['n']
    recent_receipts = conn.execute('SELECT * FROM bb_receipts ORDER BY uploaded_at DESC LIMIT 5').fetchall()
    # Test joining receipts to requests
    joined = conn.execute('''SELECT r.id, r.title, r.status, COUNT(rec.id) as receipt_count
                             FROM bb_purchase_requests r
                             LEFT JOIN bb_receipts rec ON rec.request_id = r.id
                             GROUP BY r.id, r.title, r.status
                             ORDER BY r.id DESC LIMIT 5''').fetchall()
    conn.close()
    return jsonify({
        'cloudinary_configured': bool(cloudinary.config().cloud_name),
        'cloudinary_cloud_name': cloudinary.config().cloud_name or 'NOT SET',
        'resend_configured': bool(RESEND_API_KEY),
        'resend_key_prefix': RESEND_API_KEY[:8] + '...' if RESEND_API_KEY else 'NOT SET',
        'from_email': FROM_EMAIL,
        'app_url': APP_URL,
        'database_connected': bool(DATABASE_URL),
        'receipt_count': receipt_count,
        'recent_receipts': [dict(r) for r in recent_receipts],
        'requests_with_receipt_counts': [dict(r) for r in joined],
    })

# ─── Receipts ─────────────────────────────────────────────────────────────────
@app.route('/api/requests/<int:rid>/receipts', methods=['POST'])
def upload_receipt(rid):
    err = require_auth()
    if err: return err
    u = current_user()

    if 'file' not in request.files:
        print(f"[RECEIPT] No file in request.files. Keys: {list(request.files.keys())}")
        return jsonify({'error': 'No file provided'}), 400

    file = request.files['file']
    print(f"[RECEIPT] File received: {file.filename}, content_type: {file.content_type}")

    if not cloudinary.config().cloud_name:
        print(f"[RECEIPT] Cloudinary not configured")
        return jsonify({'error': 'Cloudinary not configured.'}), 500

    try:
        print(f"[RECEIPT] Uploading to Cloudinary...")
        result = cloudinary.uploader.upload(
            file,
            folder='bloombooks/receipts',
            resource_type='auto'
        )
        image_url = result['secure_url']
        public_id = result['public_id']
        print(f"[RECEIPT] Cloudinary upload OK: {image_url}")

        conn = get_db()
        conn.execute('INSERT INTO bb_receipts (request_id,image_url,public_id) VALUES (%s,%s,%s)',
                     (rid, image_url, public_id))
        conn.commit()
        conn.close()
        print(f"[RECEIPT] Saved to DB for request {rid}")

        log_action(u['id'], 'uploaded_receipt', 'request', rid)
        return jsonify({'ok': True, 'image_url': image_url})
    except Exception as e:
        print(f"[RECEIPT ERROR] {type(e).__name__}: {e}")
        return jsonify({'error': str(e)}), 500

# ─── Reimbursements ───────────────────────────────────────────────────────────
@app.route('/api/reimbursements', methods=['GET'])
def list_reimbursements():
    err = require_auth()
    if err: return err
    u = current_user()
    conn = get_db()
    if u['role'] in ('admin','treasurer','president'):
        rows = conn.execute('''SELECT rb.*,u.name as user_name,u.email as user_email,
                               pr.title,pr.estimated_cost,pr.actual_cost,pr.is_emergency,
                               pr.type as request_type,
                               COALESCE(NULLIF(pr.reimb_method,''), u.reimb_method) as reimb_method,
                               COALESCE(NULLIF(pr.reimb_handle,''), u.reimb_handle) as reimb_handle
                               FROM bb_reimbursements rb
                               JOIN bb_users u ON rb.user_id=u.id
                               JOIN bb_purchase_requests pr ON rb.request_id=pr.id
                               ORDER BY rb.created_at DESC''').fetchall()
    else:
        rows = conn.execute('''SELECT rb.*,u.name as user_name,u.email as user_email,
                               pr.title,pr.estimated_cost,pr.actual_cost,pr.is_emergency,
                               pr.type as request_type,
                               COALESCE(NULLIF(pr.reimb_method,''), u.reimb_method) as reimb_method,
                               COALESCE(NULLIF(pr.reimb_handle,''), u.reimb_handle) as reimb_handle
                               FROM bb_reimbursements rb
                               JOIN bb_users u ON rb.user_id=u.id
                               JOIN bb_purchase_requests pr ON rb.request_id=pr.id
                               WHERE rb.user_id=%s ORDER BY rb.created_at DESC''',(u['id'],)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/reimbursements/<int:rid>/pay', methods=['POST'])
def mark_paid(rid):
    err = require_auth(['treasurer','admin','president'])
    if err: return err
    u = current_user()
    data = request.json
    now  = datetime.now().isoformat()
    conn = get_db()
    rb = conn.execute('SELECT * FROM bb_reimbursements WHERE id=%s',(rid,)).fetchone()
    if not rb: conn.close(); return jsonify({'error':'Not found'}),404
    rb = dict(rb)
    # Pull the request's reimb preference if not overridden
    req = conn.execute('SELECT title,reimb_method,reimb_handle FROM bb_purchase_requests WHERE id=%s',
                       (rb['request_id'],)).fetchone()
    method = data.get('method') or (req['reimb_method'] if req else '') or ''
    handle = data.get('handle') or (req['reimb_handle'] if req else '') or ''
    notes  = data.get('notes','')
    if handle: notes = f"{method} — {handle}" + (f"\n{notes}" if notes else '')
    conn.execute('UPDATE bb_reimbursements SET status=%s,method=%s,paid_at=%s,notes=%s WHERE id=%s',
                 ('paid',method,now,notes,rid))
    conn.execute("UPDATE bb_purchase_requests SET status='reimbursed',updated_at=%s WHERE id=%s",
                 (now,rb['request_id']))
    conn.commit(); conn.close()
    log_action(u['id'],'marked_paid','reimbursement',rid)
    req_title = req['title'] if req else 'your purchase'
    notify_reimbursement_paid(rb['user_id'], rb['amount'], method, req_title)
    return jsonify({'ok':True})

# ─── Training ─────────────────────────────────────────────────────────────────
@app.route('/api/training', methods=['GET'])
def get_training():
    err = require_auth()
    if err: return err
    conn = get_db()
    module = conn.execute('SELECT * FROM bb_training_modules WHERE is_active=1 ORDER BY id LIMIT 1').fetchone()
    conn.close()
    if not module:
        return jsonify({'module': None})
    m = dict(module)
    m['questions'] = json.loads(m['questions'])
    m['slides'] = json.loads(m['slides'])
    return jsonify({'module': m})

@app.route('/api/training', methods=['PUT'])
def update_training():
    err = require_auth(['admin'])
    if err: return err
    data = request.json
    conn = get_db()
    conn.execute('''UPDATE bb_training_modules SET title=%s,description=%s,questions=%s,pass_mark=%s
                    WHERE is_active=1''',
                 (data.get('title'), data.get('description'),
                  json.dumps(data.get('questions', [])),
                  int(data.get('pass_mark', 80))))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

@app.route('/api/training/slides', methods=['POST'])
def upload_slide():
    err = require_auth(['admin'])
    if err: return err

    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400

    if not cloudinary.config().cloud_name:
        return jsonify({'error': 'Cloudinary not configured'}), 500

    file = request.files['file']
    try:
        result = cloudinary.uploader.upload(file, folder='bloombooks/slides')
        return jsonify({'ok': True, 'url': result['secure_url'], 'public_id': result['public_id']})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/training/slides/update', methods=['POST'])
def update_slides():
    err = require_auth(['admin'])
    if err: return err
    data = request.json
    conn = get_db()
    conn.execute('UPDATE bb_training_modules SET slides=%s WHERE is_active=1',
                 (json.dumps(data.get('slides', [])),))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

@app.route('/api/training/complete', methods=['POST'])
def complete_training():
    err = require_auth()
    if err: return err
    u = current_user()
    data = request.json
    score = int(data.get('score', 0))

    conn = get_db()
    module = conn.execute('SELECT * FROM bb_training_modules WHERE is_active=1 LIMIT 1').fetchone()
    if not module:
        conn.close()
        return jsonify({'error': 'No active training module'}), 404

    pass_mark = module['pass_mark']
    passed = 1 if score >= pass_mark else 0

    conn.execute('''INSERT INTO bb_training_completions (user_id,module_id,score,passed)
                    VALUES (%s,%s,%s,%s)
                    ON CONFLICT(user_id,module_id) DO UPDATE SET score=EXCLUDED.score,passed=EXCLUDED.passed,completed_at=to_char(now(),'YYYY-MM-DD HH24:MI:SS')''',
                 (u['id'], module['id'], score, passed, score, passed))

    if passed:
        conn.execute('UPDATE bb_users SET training_complete=1 WHERE id=?', (u['id'],))

    conn.commit()
    conn.close()

    log_action(u['id'], 'completed_training', 'training', module['id'], f'score={score} passed={passed}')
    return jsonify({'ok': True, 'passed': bool(passed), 'score': score, 'pass_mark': pass_mark})

@app.route('/api/training/status', methods=['GET'])
def training_status():
    err = require_auth()
    if err: return err
    u = current_user()
    conn = get_db()
    module = conn.execute('SELECT * FROM bb_training_modules WHERE is_active=1 LIMIT 1').fetchone()
    if not module:
        conn.close()
        return jsonify({'required': False})
    completion = conn.execute('SELECT * FROM bb_training_completions WHERE user_id=%s AND module_id=?',
                               (u['id'], module['id'])).fetchone()
    conn.close()
    return jsonify({
        'required': True,
        'completed': bool(completion and completion['passed']),
        'score': completion['score'] if completion else None
    })

# ─── Dashboard stats ─────────────────────────────────────────────────────────
@app.route('/api/stats', methods=['GET'])
def stats():
    err = require_auth()
    if err: return err
    u = current_user()
    conn = get_db()

    if u['role'] in ('admin', 'treasurer', 'president'):
        pending_treasurer = conn.execute("SELECT COUNT(*) as count FROM bb_purchase_requests WHERE status='pending_treasurer'").fetchone()['count']
        pending_president = conn.execute("SELECT COUNT(*) as count FROM bb_purchase_requests WHERE status='pending_president'").fetchone()['count']
        pending_reimburse  = conn.execute("SELECT COUNT(*) as count FROM bb_reimbursements WHERE status='pending'").fetchone()['count']
        total_requests     = conn.execute("SELECT COUNT(*) as count FROM bb_purchase_requests").fetchone()['count']
        total_spent        = conn.execute("SELECT COALESCE(SUM(actual_cost),0) as count FROM bb_purchase_requests WHERE status IN ('approved','reimbursed')").fetchone()['count']
        emergency_count    = conn.execute("SELECT COUNT(*) as count FROM bb_purchase_requests WHERE is_emergency=1").fetchone()['count']
        result = {
            'pending_treasurer': pending_treasurer,
            'pending_president': pending_president,
            'pending_reimburse': pending_reimburse,
            'total_requests': total_requests,
            'total_spent': round(total_spent, 2),
            'emergency_count': emergency_count,
        }
    else:
        my_requests  = conn.execute("SELECT COUNT(*) as count FROM bb_purchase_requests WHERE submitted_by=%s", (u['id'],)).fetchone()['count']
        my_approved  = conn.execute("SELECT COUNT(*) as count FROM bb_purchase_requests WHERE submitted_by=%s AND status IN ('approved','reimbursed')", (u['id'],)).fetchone()['count']
        my_pending   = conn.execute("SELECT COUNT(*) as count FROM bb_purchase_requests WHERE submitted_by=%s AND status LIKE %s", (u['id'], 'pending%')).fetchone()['count']
        my_owed      = conn.execute("SELECT COALESCE(SUM(amount),0) as count FROM bb_reimbursements WHERE user_id=%s AND status='pending'", (u['id'],)).fetchone()['count']
        result = {
            'my_requests': my_requests,
            'my_approved': my_approved,
            'my_pending': my_pending,
            'my_owed': round(my_owed, 2),
        }

    conn.close()
    return jsonify(result)

@app.route('/api/audit', methods=['GET'])
def audit_log():
    err = require_auth(['admin', 'treasurer', 'president'])
    if err: return err
    conn = get_db()
    rows = conn.execute('''
        SELECT a.*, u.name as user_name FROM bb_audit_log a
        LEFT JOIN bb_users u ON a.user_id = u.id
        ORDER BY a.created_at DESC LIMIT 100
    ''').fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/users/<int:uid>', methods=['PATCH'])
def update_user(uid):
    err = require_auth(['admin','treasurer','president'])
    if err: return err
    data = request.json
    conn = get_db()
    if 'name'              in data: conn.execute('UPDATE bb_users SET name=%s WHERE id=%s',(data['name'],uid))
    if 'email'             in data: conn.execute('UPDATE bb_users SET email=%s WHERE id=%s',(data['email'].strip().lower(),uid))
    if 'role'              in data: conn.execute('UPDATE bb_users SET role=%s WHERE id=%s',(data['role'],uid))
    if 'training_complete' in data: conn.execute('UPDATE bb_users SET training_complete=%s WHERE id=%s',(data['training_complete'],uid))
    if 'is_active'         in data: conn.execute('UPDATE bb_users SET is_active=%s WHERE id=%s',(data['is_active'],uid))
    if 'can_submit_org_level' in data: conn.execute('UPDATE bb_users SET can_submit_org_level=%s WHERE id=%s',(int(bool(data['can_submit_org_level'])),uid))
    if 'password' in data and data['password']:
        conn.execute('UPDATE bb_users SET password=%s WHERE id=%s',(hash_pw(data['password']),uid))
    conn.commit(); conn.close()
    return jsonify({'ok':True})

@app.route('/api/users/<int:uid>/budget-access', methods=['GET'])
def get_user_budget_access(uid):
    err = require_auth(['admin','treasurer','president'])
    if err: return err
    conn = get_db()
    target = conn.execute('SELECT id, can_submit_org_level FROM bb_users WHERE id=%s', (uid,)).fetchone()
    if not target:
        conn.close(); return jsonify({'error': 'Not found'}), 404
    owned = conn.execute('''SELECT b.id, b.name, b.production_id, p.name AS production_name
                            FROM bb_budget_members bm
                            JOIN bb_budgets b ON b.id = bm.budget_id
                            LEFT JOIN bb_productions p ON b.production_id = p.id
                            WHERE bm.user_id=%s ORDER BY p.name, b.name''', (uid,)).fetchall()
    conn.close()
    return jsonify({
        'can_submit_org_level': bool(target['can_submit_org_level']),
        'budgets': [dict(b) for b in owned]
    })

# ─── Productions ──────────────────────────────────────────────────────────────
@app.route('/api/productions', methods=['GET'])
def list_productions():
    err = require_auth()
    if err: return err
    u = current_user()
    conn = get_db()
    is_admin = u['role'] in ('admin','treasurer','president')
    if is_admin:
        prods = conn.execute('SELECT * FROM bb_productions ORDER BY status,name').fetchall()
    else:
        my_ids = [r['production_id'] for r in
                  conn.execute('SELECT production_id FROM bb_production_members WHERE user_id=%s',(u['id'],)).fetchall()]
        if not my_ids:
            conn.close(); return jsonify([])
        ph = ','.join(['%s']*len(my_ids))
        prods = conn.execute(f'SELECT * FROM bb_productions WHERE id IN ({ph}) ORDER BY status,name', my_ids).fetchall()
    result = []
    for p in prods:
        prod = dict(p)
        members = conn.execute('''SELECT u.id AS user_id,u.name,u.email,u.role,m.member_role,m.display_title
                                  FROM bb_production_members m JOIN bb_users u ON m.user_id=u.id
                                  WHERE m.production_id=%s ORDER BY m.member_role,u.name''',(prod['id'],)).fetchall()
        prod['members'] = [dict(m) for m in members]
        budgets = conn.execute('SELECT * FROM bb_budgets WHERE production_id=%s AND is_active=1',(prod['id'],)).fetchall()
        prod['budgets'] = []
        for b in budgets:
            bd = dict(b)
            owners = conn.execute('''SELECT bm.user_id,u.name,u.email FROM bb_budget_members bm
                                     JOIN bb_users u ON bm.user_id=u.id WHERE bm.budget_id=%s''',(bd['id'],)).fetchall()
            bd['owners'] = [dict(o) for o in owners]
            prod['budgets'].append(bd)
        prod['total_spent'] = sum(b['spent'] for b in prod['budgets'])
        rev = conn.execute('SELECT expected,actual FROM bb_production_revenue WHERE production_id=%s',(prod['id'],)).fetchall()
        exp = sum(r['expected'] for r in rev)
        act = sum(r['actual']   for r in rev)
        # Fold in the live RoleCall Rising Stars enrollment revenue, if linked.
        rc_line = rolecall_revenue_line(conn, prod['id'])
        if rc_line:
            exp += rc_line['expected']
            act += rc_line['actual']
            prod['rolecall_linked'] = True
            prod['rolecall_revenue_actual'] = rc_line['actual']
        prod['total_revenue_expected'] = exp
        prod['total_revenue_actual']   = act
        prod['net_cost'] = prod['total_spent'] - act
        prod['i_am_producer'] = any(m['user_id']==u['id'] and m['member_role']=='producer' for m in prod['members'])
        prod['i_am_resident_producer'] = u['role'] == 'resident_producer' or \
            any(m['user_id']==u['id'] and m['member_role']=='resident_producer' for m in prod['members'])
        prod['remaining_balance'] = round((prod.get('total_budget') or 0) - (prod.get('hard_costs_total') or 0), 2)
        result.append(prod)
    conn.close()
    return jsonify(result)

@app.route('/api/productions', methods=['POST'])
def create_production():
    err = require_auth(['admin','treasurer','president','resident_producer'])
    if err: return err
    data = request.json
    name,season = data.get('name','').strip(), data.get('season','').strip()
    if not name or not season: return jsonify({'error':'Name and season required'}),400
    conn = get_db()
    conn.execute('INSERT INTO bb_productions (name,season,description,total_budget,status,category) VALUES (%s,%s,%s,%s,%s,%s)',
                 (name,season,data.get('description',''),float(data.get('total_budget',0)),'active',(data.get('category') or '').strip() or None))
    row = conn.execute('SELECT id FROM bb_productions WHERE name=%s AND season=%s ORDER BY id DESC LIMIT 1',(name,season)).fetchone()
    prod_id = row['id']
    if data.get('producer_id'):
        conn.execute('INSERT INTO bb_production_members (production_id,user_id,member_role) VALUES (%s,%s,%s)',
                     (prod_id,data['producer_id'],'producer'))
    conn.commit(); conn.close()
    log_action(current_user()['id'],'created_production','production',prod_id,name)
    return jsonify({'ok':True,'id':prod_id})

@app.route('/api/productions/<int:pid>', methods=['DELETE'])
def delete_production(pid):
    u = current_user()
    if not u: return jsonify({'error':'Not authenticated'}),401
    if u['role'] not in ('admin','treasurer','president','resident_producer'):
        return jsonify({'error':'Insufficient permissions'}),403
    conn = get_db()
    # Nullify production_id on requests and budgets rather than cascade-failing
    conn.execute('UPDATE bb_purchase_requests SET production_id=NULL WHERE production_id=%s',(pid,))
    conn.execute('UPDATE bb_budgets SET production_id=NULL WHERE production_id=%s',(pid,))
    conn.execute('DELETE FROM bb_production_members WHERE production_id=%s',(pid,))
    conn.execute('DELETE FROM bb_production_revenue WHERE production_id=%s',(pid,))
    conn.execute('DELETE FROM bb_productions WHERE id=%s',(pid,))
    conn.commit(); conn.close()
    log_action(u['id'],'deleted_production','production',pid)
    return jsonify({'ok':True})

@app.route('/api/productions/<int:pid>', methods=['PATCH'])
def update_production(pid):
    u = current_user()
    if not u: return jsonify({'error':'Not authenticated'}),401
    if u['role'] not in ('admin','treasurer','president','resident_producer') and not is_producer_of(u['id'],pid):
        return jsonify({'error':'Insufficient permissions'}),403
    data = request.json
    conn = get_db()
    prod = conn.execute('SELECT board_approved FROM bb_productions WHERE id=%s',(pid,)).fetchone()
    locked = bool(prod and dict(prod).get('board_approved'))
    for f in ['name','season','description','total_budget','status','category']:
        if f in data:
            if locked and f == 'total_budget':
                continue  # total_budget changes go through /board-approve once locked
            conn.execute(f'UPDATE bb_productions SET {f}=%s WHERE id=%s',(data[f],pid))
    conn.commit(); conn.close()
    return jsonify({'ok':True})

@app.route('/api/productions/<int:pid>/members', methods=['POST'])
def add_production_member(pid):
    u = current_user()
    if not u: return jsonify({'error':'Not authenticated'}),401
    if u['role'] not in ('admin','treasurer','president','resident_producer') and not is_producer_of(u['id'],pid):
        return jsonify({'error':'Insufficient permissions'}),403
    data = request.json
    conn = get_db()
    try:
        conn.execute('INSERT INTO bb_production_members (production_id,user_id,member_role,display_title) VALUES (%s,%s,%s,%s)',
                     (pid,data['user_id'],data.get('member_role','member'),data.get('display_title','').strip()))
        conn.commit(); conn.close(); return jsonify({'ok':True})
    except psycopg2.IntegrityError:
        conn.close(); return jsonify({'error':'Person already a member'}),409

@app.route('/api/productions/<int:pid>/members/<int:uid>', methods=['PATCH'])
def update_production_member(pid, uid):
    u = current_user()
    if not u: return jsonify({'error':'Not authenticated'}),401
    if u['role'] not in ('admin','treasurer','president','resident_producer') and not is_producer_of(u['id'],pid):
        return jsonify({'error':'Insufficient permissions'}),403
    data = request.json or {}
    conn = get_db()
    conn.execute('UPDATE bb_production_members SET display_title=%s WHERE production_id=%s AND user_id=%s',
                 (data.get('display_title','').strip(), pid, uid))
    conn.commit(); conn.close()
    return jsonify({'ok':True})

@app.route('/api/productions/<int:pid>/members/<int:uid>', methods=['DELETE'])
def remove_production_member(pid, uid):
    u = current_user()
    if not u: return jsonify({'error':'Not authenticated'}),401
    if u['role'] not in ('admin','treasurer','president','resident_producer') and not is_producer_of(u['id'],pid):
        return jsonify({'error':'Insufficient permissions'}),403
    conn = get_db()
    conn.execute('DELETE FROM bb_production_members WHERE production_id=%s AND user_id=%s',(pid,uid))
    conn.commit(); conn.close()
    return jsonify({'ok':True})

# ─── Budget members ───────────────────────────────────────────────────────────
@app.route('/api/budgets/<int:bid>/members', methods=['POST'])
def add_budget_member(bid):
    u = current_user()
    if not u: return jsonify({'error':'Not authenticated'}),401
    conn = get_db()
    b = conn.execute('SELECT * FROM bb_budgets WHERE id=%s',(bid,)).fetchone()
    if not b: conn.close(); return jsonify({'error':'Not found'}),404
    b = dict(b)
    if u['role'] not in ('admin','treasurer','president','resident_producer'):
        if not b.get('production_id') or not is_producer_of(u['id'],b['production_id']):
            conn.close(); return jsonify({'error':'Insufficient permissions'}),403
    data = request.json
    try:
        conn.execute('INSERT INTO bb_budget_members (budget_id,user_id,is_owner) VALUES (%s,%s,%s)',
                     (bid,data['user_id'],int(data.get('is_owner',1))))
        conn.commit(); conn.close(); return jsonify({'ok':True})
    except psycopg2.IntegrityError:
        conn.close(); return jsonify({'error':'Already assigned'}),409

@app.route('/api/budgets/<int:bid>/members/<int:uid>', methods=['DELETE'])
def remove_budget_member(bid, uid):
    u = current_user()
    if not u: return jsonify({'error':'Not authenticated'}),401
    conn = get_db()
    b = conn.execute('SELECT * FROM bb_budgets WHERE id=%s',(bid,)).fetchone()
    if not b: conn.close(); return jsonify({'error':'Not found'}),404
    b = dict(b)
    if u['role'] not in ('admin','treasurer','president','resident_producer'):
        if not b.get('production_id') or not is_producer_of(u['id'],b['production_id']):
            conn.close(); return jsonify({'error':'Insufficient permissions'}),403
    conn.execute('DELETE FROM bb_budget_members WHERE budget_id=%s AND user_id=%s',(bid,uid))
    conn.commit(); conn.close()
    return jsonify({'ok':True})

# ─── Receipt token ────────────────────────────────────────────────────────────
@app.route('/api/users/<int:uid>/receipt-token', methods=['GET'])
def get_receipt_token(uid):
    err = require_auth(['admin','treasurer','president','resident_producer'])
    if err: return err
    conn = get_db()
    u = conn.execute('SELECT receipt_token FROM bb_users WHERE id=%s',(uid,)).fetchone()
    if not u or not u['receipt_token']:
        token = secrets.token_urlsafe(24)
        conn.execute('UPDATE bb_users SET receipt_token=%s WHERE id=%s',(token,uid))
        conn.commit()
    else:
        token = u['receipt_token']
    conn.close()
    return jsonify({'token':token,'link':f"{APP_URL}/receipt/{token}"})

# ─── Statements ───────────────────────────────────────────────────────────────
@app.route('/api/statements', methods=['GET'])
def list_statements():
    err = require_auth()
    if err: return err
    u = current_user()
    conn = get_db()
    is_admin = u['role'] in ('admin','treasurer','president')
    if is_admin:
        rows = conn.execute('''SELECT s.*,u.name as creator_name,p.name as production_name,b.name as budget_name
                               FROM bb_statements s LEFT JOIN bb_users u ON s.created_by=u.id
                               LEFT JOIN bb_productions p ON s.production_id=p.id
                               LEFT JOIN bb_budgets b ON s.budget_id=b.id
                               ORDER BY s.updated_at DESC''').fetchall()
    else:
        rows = conn.execute('''SELECT s.*,u.name as creator_name,p.name as production_name,b.name as budget_name
                               FROM bb_statements s LEFT JOIN bb_users u ON s.created_by=u.id
                               LEFT JOIN bb_productions p ON s.production_id=p.id
                               LEFT JOIN bb_budgets b ON s.budget_id=b.id
                               WHERE s.created_by=%s ORDER BY s.updated_at DESC''',(u['id'],)).fetchall()
    result = []
    for row in rows:
        s = dict(row)
        items = conn.execute('''SELECT r.*,sub.name as submitter_name,b.name as budget_name,b.area as budget_area
                                FROM bb_statement_items si
                                JOIN bb_purchase_requests r ON si.request_id=r.id
                                LEFT JOIN bb_users sub ON r.submitted_by=sub.id
                                LEFT JOIN bb_budgets b ON r.budget_id=b.id
                                WHERE si.statement_id=%s''',(s['id'],)).fetchall()
        s['items'] = []
        for item in items:
            it = dict(item)
            conn2 = get_db()
            receipts = conn2.execute('SELECT * FROM bb_receipts WHERE request_id=%s',(it['id'],)).fetchall()
            it['receipts'] = [dict(r) for r in receipts]
            conn2.close()
            s['items'].append(it)
        s['total'] = sum(i.get('actual_cost') or i.get('estimated_cost',0) for i in s['items'])
        result.append(s)
    conn.close()
    return jsonify(result)

@app.route('/api/statements', methods=['POST'])
def create_statement():
    err = require_auth()
    if err: return err
    u = current_user()
    data = request.json
    if not data.get('title'): return jsonify({'error':'Title is required'}),400
    now = datetime.now().isoformat()
    conn = get_db()
    conn.execute('''INSERT INTO bb_statements (title,description,production_id,budget_id,created_by,updated_at)
                    VALUES (%s,%s,%s,%s,%s,%s)''',
                 (data['title'], data.get('description',''),
                  data.get('production_id') or None, data.get('budget_id') or None,
                  u['id'], now))
    row = conn.execute('SELECT lastval() AS id').fetchone()
    sid = row['id']
    conn.commit(); conn.close()
    log_action(u['id'],'created_statement','statement',sid,data['title'])
    return jsonify({'ok':True,'id':sid})

@app.route('/api/statements/<int:sid>', methods=['PATCH'])
def update_statement(sid):
    err = require_auth()
    if err: return err
    u = current_user()
    conn = get_db()
    s = conn.execute('SELECT * FROM bb_statements WHERE id=%s',(sid,)).fetchone()
    if not s: conn.close(); return jsonify({'error':'Not found'}),404
    if dict(s)['created_by'] != u['id'] and u['role'] not in ('admin','treasurer','president'):
        conn.close(); return jsonify({'error':'Insufficient permissions'}),403
    data = request.json
    now = datetime.now().isoformat()
    fields,vals = ['updated_at=%s'],[now]
    for f in ['title','description','production_id','budget_id']:
        if f in data: fields.append(f'{f}=%s'); vals.append(data[f] or None)
    vals.append(sid)
    conn.execute(f'UPDATE bb_statements SET {",".join(fields)} WHERE id=%s',vals)
    conn.commit(); conn.close()
    return jsonify({'ok':True})

@app.route('/api/statements/<int:sid>', methods=['DELETE'])
def delete_statement(sid):
    err = require_auth()
    if err: return err
    u = current_user()
    conn = get_db()
    s = conn.execute('SELECT * FROM bb_statements WHERE id=%s',(sid,)).fetchone()
    if not s: conn.close(); return jsonify({'error':'Not found'}),404
    s = dict(s)
    if s['created_by'] != u['id'] and u['role'] not in ('admin','treasurer','president'):
        conn.close(); return jsonify({'error':'Insufficient permissions'}),403
    if s['status'] not in ('draft',):
        conn.close(); return jsonify({'error':'Only draft statements can be deleted'}),400
    # Delete linked requests too
    items = conn.execute('SELECT request_id FROM bb_statement_items WHERE statement_id=%s',(sid,)).fetchall()
    for item in items:
        conn.execute('DELETE FROM bb_receipts WHERE request_id=%s',(item['request_id'],))
        conn.execute('DELETE FROM bb_purchase_requests WHERE id=%s',(item['request_id'],))
    conn.execute('DELETE FROM bb_statement_items WHERE statement_id=%s',(sid,))
    conn.execute('DELETE FROM bb_statements WHERE id=%s',(sid,))
    conn.commit(); conn.close()
    return jsonify({'ok':True})

@app.route('/api/statements/<int:sid>/items', methods=['POST'])
def add_statement_item(sid):
    err = require_auth()
    if err: return err
    u = current_user()
    conn = get_db()
    s = conn.execute('SELECT * FROM bb_statements WHERE id=%s',(sid,)).fetchone()
    if not s: conn.close(); return jsonify({'error':'Not found'}),404
    s = dict(s)
    if s['created_by'] != u['id']:
        conn.close(); return jsonify({'error':'Insufficient permissions'}),403
    if s['status'] != 'draft':
        conn.close(); return jsonify({'error':'Statement already submitted'}),400
    data = request.json
    if not data.get('title') or not data.get('estimated_cost'):
        conn.close(); return jsonify({'error':'Title and estimated cost are required'}),400
    now = datetime.now().isoformat()
    # Create as a draft request (status='draft')
    conn.execute('''INSERT INTO bb_purchase_requests
                    (type,status,title,description,vendor,estimated_cost,budget_id,production_id,
                     submitted_by,is_emergency,purchase_method,item_url,authorized_by,
                     reimb_method,reimb_handle,statement_id,submitted_at,updated_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                 (data.get('type','pre_approval'), 'draft',
                  data['title'], data.get('description',''), data.get('vendor',''),
                  float(data['estimated_cost']), s.get('budget_id') or data.get('budget_id') or None,
                  s.get('production_id') or data.get('production_id') or None,
                  u['id'], 1 if data.get('type')=='sap' else 0,
                  data.get('purchase_method','in_store'), data.get('item_url',''),
                  data.get('authorized_by',''), data.get('reimb_method',''), data.get('reimb_handle',''),
                  sid, now, now))
    row = conn.execute('SELECT lastval() AS id').fetchone()
    req_id = row['id']
    conn.execute('INSERT INTO bb_statement_items (statement_id,request_id) VALUES (%s,%s)',(sid,req_id))
    conn.execute('UPDATE bb_statements SET updated_at=%s WHERE id=%s',(now,sid))
    conn.commit(); conn.close()
    return jsonify({'ok':True,'request_id':req_id})

@app.route('/api/statements/<int:sid>/items/<int:rid>', methods=['PATCH'])
def update_statement_item(sid, rid):
    err = require_auth()
    if err: return err
    u = current_user()
    conn = get_db()
    s = conn.execute('SELECT * FROM bb_statements WHERE id=%s',(sid,)).fetchone()
    if not s or dict(s)['created_by'] != u['id']:
        conn.close(); return jsonify({'error':'Insufficient permissions'}),403
    data = request.json
    fields,vals = [],[]
    for f in ['title','description','vendor','estimated_cost','actual_cost','purchase_method',
              'item_url','authorized_by','reimb_method','reimb_handle']:
        if f in data:
            fields.append(f'{f}=%s')
            vals.append(float(data[f]) if f in ('estimated_cost','actual_cost') else data[f])
    if fields:
        vals.append(rid)
        conn.execute(f'UPDATE bb_purchase_requests SET {",".join(fields)} WHERE id=%s',vals)
        conn.commit()
    conn.close()
    return jsonify({'ok':True})

@app.route('/api/statements/<int:sid>/items/<int:rid>', methods=['DELETE'])
def delete_statement_item(sid, rid):
    err = require_auth()
    if err: return err
    u = current_user()
    conn = get_db()
    s = conn.execute('SELECT * FROM bb_statements WHERE id=%s',(sid,)).fetchone()
    if not s or dict(s)['created_by'] != u['id']:
        conn.close(); return jsonify({'error':'Insufficient permissions'}),403
    conn.execute('DELETE FROM bb_receipts WHERE request_id=%s',(rid,))
    conn.execute('DELETE FROM bb_statement_items WHERE statement_id=%s AND request_id=%s',(sid,rid))
    conn.execute('DELETE FROM bb_purchase_requests WHERE id=%s',(rid,))
    conn.commit(); conn.close()
    return jsonify({'ok':True})

@app.route('/api/statements/<int:sid>/submit', methods=['POST'])
def submit_statement(sid):
    err = require_auth()
    if err: return err
    u = current_user()
    conn = get_db()
    s = conn.execute('SELECT * FROM bb_statements WHERE id=%s',(sid,)).fetchone()
    if not s: conn.close(); return jsonify({'error':'Not found'}),404
    s = dict(s)
    if s['created_by'] != u['id']:
        conn.close(); return jsonify({'error':'Insufficient permissions'}),403
    if s['status'] != 'draft':
        conn.close(); return jsonify({'error':'Already submitted'}),400
    items = conn.execute('''SELECT r.* FROM bb_statement_items si
                            JOIN bb_purchase_requests r ON si.request_id=r.id
                            WHERE si.statement_id=%s''',(sid,)).fetchall()
    if not items:
        conn.close(); return jsonify({'error':'Add at least one item before submitting'}),400
    now = datetime.now().isoformat()
    # Determine initial status for each item
    prod_id = s.get('production_id')
    if prod_id and get_production_producers(prod_id):
        item_status = 'pending_producer'
    else:
        item_status = 'pending_treasurer'
    for item in items:
        conn.execute('''UPDATE bb_purchase_requests SET status=%s,submitted_at=%s,updated_at=%s
                        WHERE id=%s''',(item_status,now,now,item['id']))
    conn.execute("UPDATE bb_statements SET status='submitted',submitted_at=%s,updated_at=%s WHERE id=%s",
                 (now,now,sid))
    conn.commit()
    conn.close()
    log_action(u['id'],'submitted_statement','statement',sid,s['title'])
    # Notify approvers
    for item in items:
        item = dict(item)
        notify_request_submitted(
            req_id=item['id'], req_title=item['title'],
            submitter_name=u['name'], submitter_email=u['email'],
            estimated_cost=item['estimated_cost'], req_type=item['type'],
            purchase_method=item.get('purchase_method','in_store'),
            item_url=item.get('item_url',''),
            production_id=prod_id, status=item_status
        )
    return jsonify({'ok':True})

# ─── Send back (needs revision) ───────────────────────────────────────────────
@app.route('/api/requests/<int:rid>/send-back', methods=['POST'])
def send_back_request(rid):
    u = current_user()
    if not u: return jsonify({'error':'Not authenticated'}),401
    data = request.json
    note = data.get('note','').strip()
    if not note: return jsonify({'error':'Please provide a reason for sending back'}),400
    conn = get_db()
    req = conn.execute('SELECT * FROM bb_purchase_requests WHERE id=%s',(rid,)).fetchone()
    if not req: conn.close(); return jsonify({'error':'Not found'}),404
    req = dict(req)
    # Only approvers at the current stage can send back
    can_act = (u['role'] in ('admin','treasurer','president') or
               (req['status']=='pending_producer' and is_producer_of(u['id'],req.get('production_id'))))
    if not can_act:
        conn.close(); return jsonify({'error':'Insufficient permissions'}),403
    now = datetime.now().isoformat()
    conn.execute('''UPDATE bb_purchase_requests SET status='needs_revision',
                    needs_revision=1, revision_note=%s, updated_at=%s WHERE id=%s''',
                 (note, now, rid))
    conn.commit(); conn.close()
    log_action(u['id'],'sent_back_request','request',rid,note)
    # Notify submitter
    submitter = get_user_email(req['submitted_by'])
    if submitter:
        send_email(submitter['email'], f'↩ Changes needed: {req["title"]}',
            email_html('Changes Needed on Your Request',
                f'<p>Your request for <strong>{req["title"]}</strong> has been sent back for revision.</p>'
                f'<p><strong>What needs to change:</strong> {note}</p>'
                f'<p>Please update your request and resubmit.</p>',
                'View in BloomBooks', APP_URL))
    return jsonify({'ok':True})

@app.route('/api/requests/<int:rid>/resubmit', methods=['POST'])
def resubmit_request(rid):
    err = require_auth()
    if err: return err
    u = current_user()
    conn = get_db()
    req = conn.execute('SELECT * FROM bb_purchase_requests WHERE id=%s',(rid,)).fetchone()
    if not req: conn.close(); return jsonify({'error':'Not found'}),404
    req = dict(req)
    if req['submitted_by'] != u['id']:
        conn.close(); return jsonify({'error':'Only the submitter can resubmit'}),403
    if req['status'] != 'needs_revision':
        conn.close(); return jsonify({'error':'This request does not need revision'}),400
    data = request.json
    now = datetime.now().isoformat()
    # Determine which stage to send back to
    prod_id = req.get('production_id')
    if prod_id and get_production_producers(prod_id):
        new_status = 'pending_producer'
    else:
        new_status = 'pending_treasurer'
    fields = ['status=%s','needs_revision=0','updated_at=%s']
    vals   = [new_status, now]
    for f in ['title','description','vendor','estimated_cost','purchase_method','item_url','authorized_by']:
        if f in data:
            fields.append(f'{f}=%s')
            vals.append(float(data[f]) if f=='estimated_cost' else data[f])
    vals.append(rid)
    conn.execute(f'UPDATE bb_purchase_requests SET {",".join(fields)} WHERE id=%s', vals)
    conn.commit(); conn.close()
    log_action(u['id'],'resubmitted_request','request',rid)
    # Notify approvers
    notify_request_submitted(
        req_id=rid, req_title=req['title'],
        submitter_name=u['name'], submitter_email=u['email'],
        estimated_cost=data.get('estimated_cost', req['estimated_cost']),
        req_type=req['type'], purchase_method=req.get('purchase_method','in_store'),
        item_url=req.get('item_url',''), production_id=prod_id, status=new_status
    )
    return jsonify({'ok':True})

# ─── User profile ─────────────────────────────────────────────────────────────
@app.route('/api/profile', methods=['GET'])
def get_profile():
    err = require_auth()
    if err: return err
    u = current_user()
    return jsonify({'user': u})

@app.route('/api/profile', methods=['PATCH'])
def update_profile():
    err = require_auth()
    if err: return err
    u = current_user()
    data = request.json
    conn = get_db()
    fields, vals = [], []
    if 'reimb_method' in data: fields.append('reimb_method=%s'); vals.append(data['reimb_method'])
    if 'reimb_handle' in data: fields.append('reimb_handle=%s'); vals.append(data['reimb_handle'])
    if 'password' in data and data['password']:
        fields.append('password=%s'); vals.append(hash_pw(data['password']))
    if fields:
        vals.append(u['id'])
        conn.execute(f'UPDATE bb_users SET {",".join(fields)} WHERE id=%s', vals)
        conn.commit()
    conn.close()
    return jsonify({'ok': True})

# ─── Production Revenue ───────────────────────────────────────────────────────
@app.route('/api/productions/<int:pid>/revenue', methods=['GET'])
def list_revenue(pid):
    err = require_auth()
    if err: return err
    u = current_user()
    if u['role'] not in ('admin','treasurer','president','resident_producer') and not is_producer_of(u['id'], pid):
        return jsonify({'error': 'Insufficient permissions'}), 403
    conn = get_db()
    rows = conn.execute('SELECT * FROM bb_production_revenue WHERE production_id=%s ORDER BY created_at DESC', (pid,)).fetchall()
    result = [dict(r) for r in rows]
    # Prepend the live RoleCall Rising Stars line, if this production is linked.
    rc_line = rolecall_revenue_line(conn, pid)
    conn.close()
    if rc_line:
        result.insert(0, rc_line)
    return jsonify(result)

@app.route('/api/productions/<int:pid>/revenue', methods=['POST'])
def create_revenue(pid):
    u = current_user()
    if not u: return jsonify({'error':'Not authenticated'}),401
    if u['role'] not in ('admin','treasurer','president','resident_producer') and not is_producer_of(u['id'], pid):
        return jsonify({'error': 'Insufficient permissions'}), 403
    data = request.json
    if not data.get('source'): return jsonify({'error': 'Source is required'}), 400
    now = datetime.now().isoformat()
    conn = get_db()
    conn.execute('''INSERT INTO bb_production_revenue
                    (production_id,source,description,expected,actual,received_date,created_by,updated_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s)''',
                 (pid, data['source'], data.get('description',''),
                  float(data.get('expected', 0)), float(data.get('actual', 0)),
                  data.get('received_date','') or None, u['id'], now))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/productions/<int:pid>/revenue/<int:rid>', methods=['PATCH'])
def update_revenue(pid, rid):
    u = current_user()
    if not u: return jsonify({'error':'Not authenticated'}),401
    if u['role'] not in ('admin','treasurer','president','resident_producer') and not is_producer_of(u['id'], pid):
        return jsonify({'error': 'Insufficient permissions'}), 403
    data = request.json
    now = datetime.now().isoformat()
    conn = get_db()
    fields, vals = [], []
    for f in ['source','description','expected','actual','received_date']:
        if f in data:
            fields.append(f'{f}=%s')
            vals.append(float(data[f]) if f in ('expected','actual') else (data[f] or None))
    fields.append('updated_at=%s'); vals.append(now)
    vals.append(rid)
    conn.execute(f'UPDATE bb_production_revenue SET {",".join(fields)} WHERE id=%s AND production_id=%s',
                 vals + [pid])
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/productions/<int:pid>/revenue/<int:rid>', methods=['DELETE'])
def delete_revenue(pid, rid):
    u = current_user()
    if not u: return jsonify({'error':'Not authenticated'}),401
    if u['role'] not in ('admin','treasurer','president','resident_producer') and not is_producer_of(u['id'], pid):
        return jsonify({'error': 'Insufficient permissions'}), 403
    conn = get_db()
    conn.execute('DELETE FROM bb_production_revenue WHERE id=%s AND production_id=%s', (rid, pid))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

# ─── RoleCall Rising Stars link management ───────────────────────────────────
@app.route('/api/rolecall/rising-stars', methods=['GET'])
def list_rolecall_rising_stars():
    """List RoleCall Rising Stars productions available to link (admins only)."""
    u = current_user()
    if not u: return jsonify({'error':'Not authenticated'}),401
    if u['role'] not in ORG_APPROVER_ROLES:
        return jsonify({'error':'Insufficient permissions'}),403
    conn = get_db()
    try:
        rows = conn.execute('''SELECT id, name,
                                      COALESCE(registration_status,'') AS registration_status
                               FROM productions
                               WHERE stage='rising_stars'
                                 AND registration_status IS NOT NULL
                                 AND registration_status != 'draft'
                               ORDER BY name''').fetchall()
        items = [dict(r) for r in rows]
    except Exception as e:
        conn.close()
        app.logger.warning(f'RoleCall rising-stars list failed: {e}')
        return jsonify({'error':'Could not reach RoleCall data','items':[]}),200
    # Attach which BloomBooks production (if any) each is already linked to.
    links = {l['rc_production_id']: l for l in
             [dict(x) for x in conn.execute('SELECT * FROM bb_rolecall_links').fetchall()]}
    conn.close()
    for it in items:
        lk = links.get(it['id'])
        it['linked_to_bb_production_id'] = lk['bb_production_id'] if lk else None
    return jsonify({'items': items})

@app.route('/api/productions/<int:pid>/rolecall-link', methods=['GET'])
def get_rolecall_link_route(pid):
    """Current link + live revenue preview for a production."""
    err = require_auth()
    if err: return err
    u = current_user()
    if u['role'] not in ORG_APPROVER_ROLES and not is_producer_of(u['id'], pid):
        return jsonify({'error':'Insufficient permissions'}),403
    conn = get_db()
    link = get_rolecall_link(conn, pid)
    rev = get_rolecall_rising_stars_revenue(conn, link['rc_production_id']) if link else None
    conn.close()
    return jsonify({'linked': bool(link), 'link': link, 'revenue': rev})

@app.route('/api/productions/<int:pid>/rolecall-link', methods=['POST'])
def set_rolecall_link(pid):
    """Link a BloomBooks production to a RoleCall Rising Stars production (admins)."""
    u = current_user()
    if not u: return jsonify({'error':'Not authenticated'}),401
    if u['role'] not in ORG_APPROVER_ROLES:
        return jsonify({'error':'Insufficient permissions'}),403
    rc_id = (request.json or {}).get('rc_production_id')
    if not rc_id:
        return jsonify({'error':'rc_production_id is required'}),400
    conn = get_db()
    rc = conn.execute("SELECT id, name FROM productions WHERE id=%s AND stage='rising_stars'", (rc_id,)).fetchone()
    if not rc:
        conn.close()
        return jsonify({'error':'That RoleCall Rising Stars production was not found'}),404
    rc = dict(rc); now = datetime.now().isoformat()
    conn.execute('''INSERT INTO bb_rolecall_links (bb_production_id, rc_production_id, rc_production_name, linked_by, updated_at)
                    VALUES (%s,%s,%s,%s,%s)
                    ON CONFLICT (bb_production_id) DO UPDATE
                      SET rc_production_id=EXCLUDED.rc_production_id,
                          rc_production_name=EXCLUDED.rc_production_name,
                          updated_at=EXCLUDED.updated_at''',
                 (pid, rc['id'], rc.get('name'), u['id'], now))
    conn.commit()
    rev = get_rolecall_rising_stars_revenue(conn, rc['id'])
    conn.close()
    return jsonify({'ok': True, 'revenue': rev})

@app.route('/api/productions/<int:pid>/rolecall-link', methods=['DELETE'])
def delete_rolecall_link(pid):
    """Remove the RoleCall link (admins)."""
    u = current_user()
    if not u: return jsonify({'error':'Not authenticated'}),401
    if u['role'] not in ORG_APPROVER_ROLES:
        return jsonify({'error':'Insufficient permissions'}),403
    conn = get_db()
    conn.execute('DELETE FROM bb_rolecall_links WHERE bb_production_id=%s', (pid,))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

# ─── Build Show: RoleCall licensing → BloomBooks production ───────────────────
# A show becomes eligible here once RoleCall marks it approved_to_produce (which
# itself requires contract_received=true). We do a one-time copy at build time —
# not a live sync — because License Cost and Venue Rate have no home in RoleCall;
# only ticket price/capacity/rehearsal schedule come from there. The one write
# back to RoleCall (built_in_bloombooks) is a deliberate, narrow exception to the
# "read-only" rule above, purely so an already-built show doesn't show up twice.
SUGGESTED_DEPARTMENT_SPLIT = [
    ('Sets & Scenic',        0.30),
    ('Costumes',             0.20),
    ('Props & Makeup',       0.10),
    ('Music & Sound',        0.15),
    ('Marketing & Programs', 0.15),
    ('Contingency',          0.10),
]

import math

def _rc_rehearsal_schedule(rc):
    """Turn a licensing request's rehearsal fields into a weekly-hours total and
    a week count, for the studio charge calc. Prefers rehearsal_blocks (supports
    multiple day/time combinations, e.g. Sat 11-2 AND Thu 5-6:30) + the rehearsal
    period date range; falls back to the older single-block fields for shows
    approved before rehearsal_blocks existed."""
    weekly_hours = 0.0
    sessions_per_week = 0
    try:
        blocks = json.loads(rc.get('rehearsal_blocks') or '[]')
    except Exception:
        blocks = []
    if isinstance(blocks, list) and blocks:
        for b in blocks:
            if not isinstance(b, dict):
                continue
            days = b.get('days') or []
            hrs = _pc_hours_between(b.get('start_time') or '', b.get('end_time') or '')
            sessions_per_week += len(days) if isinstance(days, list) else 0
            weekly_hours += (len(days) if isinstance(days, list) else 0) * hrs
    else:
        try:
            legacy_days = json.loads(rc.get('rehearsal_days') or '[]')
        except Exception:
            legacy_days = []
        if isinstance(legacy_days, list) and legacy_days:
            hrs = _pc_hours_between(rc.get('rehearsal_start_time') or '', rc.get('rehearsal_end_time') or '')
            sessions_per_week = len(legacy_days)
            weekly_hours = len(legacy_days) * hrs

    period_start = rc.get('rehearsal_period_start')
    period_end = rc.get('rehearsal_period_end')
    weeks = None
    if period_start and period_end:
        try:
            sd = period_start if isinstance(period_start, date) else datetime.strptime(str(period_start), '%Y-%m-%d').date()
            ed = period_end if isinstance(period_end, date) else datetime.strptime(str(period_end), '%Y-%m-%d').date()
            weeks = max(1, math.ceil((ed - sd).days / 7))
        except Exception:
            weeks = None

    return {
        'rehearsal_weekly_hours': round(weekly_hours, 2),
        'rehearsal_sessions_per_week': sessions_per_week,
        'rehearsal_weeks_computed': weeks,
    }

def _rc_row_to_estimate(rc):
    """Estimate ticket sales from whatever RoleCall's licensing request has on
    file (average ticket price × audience capacity × number of shows). Returns
    0 if the inputs aren't there — resident producer fills in the rest by hand."""
    avg_cents = rc.get('average_ticket_price_cents') or 0
    capacity = rc.get('audience_capacity') or 0
    shows = rc.get('number_of_shows') or 0
    return round((avg_cents/100.0) * capacity * shows, 2)

@app.route('/api/rolecall/licensing-requests/ready', methods=['GET'])
def list_ready_licensing_requests():
    """Shows that are contract-signed + board-approved-to-produce in RoleCall,
    and haven't been built out in BloomBooks yet."""
    err = require_auth(roles=list(PRODUCTION_ADMIN_ROLES))
    if err: return err
    conn = get_db()
    try:
        rows = conn.execute('''SELECT id, production_id, production_name, production_type,
                                       licensor, venue_name, audience_capacity, number_of_shows,
                                       average_ticket_price_cents, production_start_date, production_end_date,
                                       approved_to_produce_date, approved_to_produce_by,
                                       rehearsal_blocks, rehearsal_period_start, rehearsal_period_end,
                                       rehearsal_days, rehearsal_start_time, rehearsal_end_time
                               FROM licensing_requests
                               WHERE contract_received=TRUE AND approved_to_produce=TRUE
                                 AND COALESCE(built_in_bloombooks,FALSE)=FALSE
                               ORDER BY approved_to_produce_date DESC NULLS LAST''').fetchall()
        items = []
        for r in rows:
            r = dict(r)
            r['estimated_ticket_sales'] = _rc_row_to_estimate(r)
            sched = _rc_rehearsal_schedule(r)
            r.update(sched)
            items.append(r)
    except Exception as e:
        conn.close()
        app.logger.warning(f'RoleCall ready-licensing-requests read failed: {e}')
        return jsonify({'error': 'Could not reach RoleCall data', 'items': []}), 200
    conn.close()
    return jsonify({'items': items})

@app.route('/api/productions/build-from-licensing', methods=['POST'])
def build_production_from_licensing():
    """Create a BloomBooks production from an approved-to-produce RoleCall
    licensing request, auto-filling what RoleCall has (name, ticket-price-based
    estimate, and the rehearsal schedule captured when the show was approved to
    produce) and leaving License Cost / Venue Rate / Concessions / Enrollment
    for the Resident Producer to fill in by hand."""
    err = require_auth(roles=list(PRODUCTION_ADMIN_ROLES))
    if err: return err
    u = current_user()
    data = request.json or {}
    lic_id = data.get('licensing_request_id')
    if not lic_id:
        return jsonify({'error': 'licensing_request_id is required'}), 400
    category = (data.get('category') or '').strip()
    season = (data.get('season') or '').strip()
    if not season:
        return jsonify({'error': 'Season is required'}), 400

    conn = get_db()
    rc = conn.execute('''SELECT * FROM licensing_requests WHERE id=%s
                         AND contract_received=TRUE AND approved_to_produce=TRUE
                         AND COALESCE(built_in_bloombooks,FALSE)=FALSE''', (lic_id,)).fetchone()
    if not rc:
        conn.close()
        return jsonify({'error': 'Licensing request not found, not yet approved to produce, or already built'}), 404
    rc = dict(rc)
    rc_prod_id = rc.get('production_id')

    # Rehearsal schedule: computed from the blocks + date range captured on the
    # licensing request at approve-to-produce time (falls back to the older
    # single-block fields, or the linked RoleCall production's meeting schedule,
    # for shows approved before that capture step existed).
    sched = _rc_rehearsal_schedule(rc)
    weekly_hours = sched['rehearsal_weekly_hours']
    sessions_per_week = sched['rehearsal_sessions_per_week']
    if not weekly_hours and rc_prod_id:
        try:
            rp = conn.execute('''SELECT meeting_days, meeting_start_time, meeting_end_time
                                 FROM productions WHERE id=%s''', (rc_prod_id,)).fetchone()
            if rp:
                rp = dict(rp)
                days = json.loads(rp.get('meeting_days') or '[]')
                sessions_per_week = len(days) if isinstance(days, list) else 0
                hrs = _pc_hours_between(rp.get('meeting_start_time') or '', rp.get('meeting_end_time') or '')
                weekly_hours = sessions_per_week * hrs
        except Exception as e:
            app.logger.warning(f'RoleCall rehearsal schedule read failed for {rc_prod_id}: {e}')

    rehearsal_weeks = float(data.get('rehearsal_weeks') or sched['rehearsal_weeks_computed'] or 8)
    studio = _compute_studio_charge(conn, weekly_hours, rehearsal_weeks)
    est_ticket_sales = _rc_row_to_estimate(rc)
    avg_hours_per_session = round(weekly_hours / sessions_per_week, 2) if sessions_per_week else 0

    conn.execute('''INSERT INTO bb_productions
            (name, season, description, total_budget, status, category,
             source_licensing_request_id, source_rc_production_id,
             est_ticket_sales, rehearsals_per_week, rehearsal_weeks,
             rehearsal_hours_per_session, rehearsal_weekly_hours, studio_charge)
        VALUES (%s,%s,%s,0,'active',%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
        (rc['production_name'], season, f"Built from RoleCall licensing request {rc.get('ref_number','')}",
         category or None, lic_id, rc_prod_id,
         est_ticket_sales, sessions_per_week, rehearsal_weeks,
         avg_hours_per_session, weekly_hours, studio['studio_charge']))
    row = conn.execute('SELECT id FROM bb_productions WHERE source_licensing_request_id=%s ORDER BY id DESC LIMIT 1',
                        (lic_id,)).fetchone()
    prod_id = row['id']

    if data.get('add_me_as_resident_producer', True) and u['role'] == 'resident_producer':
        conn.execute('INSERT INTO bb_production_members (production_id,user_id,member_role) VALUES (%s,%s,%s)',
                     (prod_id, u['id'], 'resident_producer'))

    # Narrow, deliberate write back to RoleCall so this doesn't get imported twice.
    try:
        conn.execute("UPDATE licensing_requests SET built_in_bloombooks=TRUE WHERE id=%s", (lic_id,))
    except Exception as e:
        app.logger.warning(f'Could not flag licensing_request {lic_id} as built: {e}')

    conn.commit(); conn.close()
    log_action(u['id'], 'built_show_from_licensing', 'production', prod_id, rc['production_name'])
    return jsonify({'ok': True, 'id': prod_id, 'studio_charge': studio, 'estimated_ticket_sales': est_ticket_sales})

@app.route('/api/productions/<int:pid>/rehearsal-schedule', methods=['PUT'])
def update_rehearsal_schedule(pid):
    """Edit the rehearsal cadence used to charge the show for studio use, and
    recompute the at-cost charge."""
    err = require_auth(roles=list(PRODUCTION_ADMIN_ROLES))
    if err: return err
    d = request.json or {}
    conn = get_db()
    prod = conn.execute('SELECT * FROM bb_productions WHERE id=%s', (pid,)).fetchone()
    if not prod:
        conn.close(); return jsonify({'error': 'Not found'}), 404
    if dict(prod).get('hard_costs_locked'):
        conn.close(); return jsonify({'error': 'Hard costs are locked for this production'}), 409
    weekly_hours = float(d.get('rehearsal_weekly_hours') or 0)
    rehearsal_weeks = float(d.get('rehearsal_weeks') or 0)
    studio = _compute_studio_charge(conn, weekly_hours, rehearsal_weeks)
    conn.execute('''UPDATE bb_productions SET rehearsal_weekly_hours=%s, rehearsal_weeks=%s,
        studio_charge=%s WHERE id=%s''',
        (weekly_hours, rehearsal_weeks, studio['studio_charge'], pid))
    conn.commit(); conn.close()
    return jsonify({'ok': True, **studio})

@app.route('/api/productions/<int:pid>/hard-costs', methods=['PUT'])
def set_hard_costs(pid):
    """Resident Producer locks in License Cost + Venue Rate (the studio charge
    is already computed from the rehearsal schedule). Sum becomes hard_costs_total,
    which determines the remaining balance available for department allocations."""
    err = require_auth(roles=list(PRODUCTION_ADMIN_ROLES))
    if err: return err
    d = request.json or {}
    conn = get_db()
    prod = conn.execute('SELECT * FROM bb_productions WHERE id=%s', (pid,)).fetchone()
    if not prod:
        conn.close(); return jsonify({'error': 'Not found'}), 404
    prod = dict(prod)
    if prod.get('board_approved'):
        conn.close(); return jsonify({'error': 'Budget is already board-approved and locked'}), 409
    license_cost = float(d.get('license_cost', prod.get('license_cost') or 0))
    venue_rate = float(d.get('venue_rate', prod.get('venue_rate') or 0))
    est_concessions = float(d.get('est_concessions', prod.get('est_concessions') or 0))
    est_enrollment = float(d.get('est_enrollment', prod.get('est_enrollment') or 0))
    hard_costs_total = round(license_cost + venue_rate + (prod.get('studio_charge') or 0), 2)
    conn.execute('''UPDATE bb_productions SET license_cost=%s, venue_rate=%s, est_concessions=%s,
        est_enrollment=%s, hard_costs_total=%s WHERE id=%s''',
        (license_cost, venue_rate, est_concessions, est_enrollment, hard_costs_total, pid))
    conn.commit(); conn.close()
    return jsonify({'ok': True, 'hard_costs_total': hard_costs_total})

@app.route('/api/productions/<int:pid>/board-approve', methods=['POST'])
def board_approve_production(pid):
    """Board approves the overall number: total budget minus hard costs = the
    remaining balance the Resident Producer can then chunk across departments.
    Locks hard costs (and the total budget) against further changes."""
    err = require_auth(roles=list(PRODUCTION_ADMIN_ROLES))
    if err: return err
    u = current_user()
    d = request.json or {}
    conn = get_db()
    prod = conn.execute('SELECT * FROM bb_productions WHERE id=%s', (pid,)).fetchone()
    if not prod:
        conn.close(); return jsonify({'error': 'Not found'}), 404
    prod = dict(prod)
    total_budget = float(d.get('total_budget', prod.get('total_budget') or 0))
    if total_budget < (prod.get('hard_costs_total') or 0):
        conn.close(); return jsonify({'error': 'Total budget is less than the hard costs already set'}), 400
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    conn.execute('''UPDATE bb_productions SET total_budget=%s, hard_costs_locked=1,
        board_approved=1, board_approved_at=%s, board_approved_by=%s WHERE id=%s''',
        (total_budget, now, u['id'], pid))
    conn.commit(); conn.close()
    log_action(u['id'], 'board_approved_production', 'production', pid, f'Total budget ${total_budget:,.2f}')
    return jsonify({'ok': True, 'remaining_balance': round(total_budget - (prod.get('hard_costs_total') or 0), 2)})

@app.route('/api/productions/<int:pid>/suggested-allocations', methods=['GET'])
def get_suggested_allocations(pid):
    """Suggested department split of the remaining balance (after hard costs).
    Purely advisory — the Resident Producer edits, removes, or adds lines before
    actually creating the budgets via POST /allocations."""
    err = require_auth(roles=list(PRODUCTION_ADMIN_ROLES))
    if err: return err
    conn = get_db()
    prod = conn.execute('SELECT * FROM bb_productions WHERE id=%s', (pid,)).fetchone()
    conn.close()
    if not prod:
        return jsonify({'error': 'Not found'}), 404
    prod = dict(prod)
    remaining = round((prod.get('total_budget') or 0) - (prod.get('hard_costs_total') or 0), 2)
    suggestions = [{'name': name, 'amount': round(remaining * pct, 2)} for name, pct in SUGGESTED_DEPARTMENT_SPLIT]
    return jsonify({'remaining_balance': remaining, 'suggestions': suggestions})

@app.route('/api/productions/<int:pid>/allocations', methods=['POST'])
def create_allocations(pid):
    """Turn a (possibly edited) list of department lines into real bb_budgets
    rows for the production. Requires board approval first."""
    err = require_auth(roles=list(PRODUCTION_ADMIN_ROLES))
    if err: return err
    u = current_user()
    d = request.json or {}
    allocations = d.get('allocations') or []
    if not allocations:
        return jsonify({'error': 'At least one allocation is required'}), 400
    conn = get_db()
    prod = conn.execute('SELECT * FROM bb_productions WHERE id=%s', (pid,)).fetchone()
    if not prod:
        conn.close(); return jsonify({'error': 'Not found'}), 404
    prod = dict(prod)
    if not prod.get('board_approved'):
        conn.close(); return jsonify({'error': 'Board must approve the overall budget before allocating departments'}), 409
    remaining = round((prod.get('total_budget') or 0) - (prod.get('hard_costs_total') or 0), 2)
    requested = round(sum(float(a.get('amount') or 0) for a in allocations), 2)
    if requested > remaining + 0.01:
        conn.close()
        return jsonify({'error': f'Allocations total ${requested:,.2f}, which exceeds the ${remaining:,.2f} remaining balance'}), 400
    created = []
    for a in allocations:
        name = (a.get('name') or '').strip()
        amount = float(a.get('amount') or 0)
        if not name:
            continue
        conn.execute('INSERT INTO bb_budgets (name,area,season,total_amount,production_id) VALUES (%s,%s,%s,%s,%s)',
                     (name, name, prod.get('season',''), amount, pid))
        created.append({'name': name, 'amount': amount})
    conn.commit(); conn.close()
    log_action(u['id'], 'created_budget_allocations', 'production', pid, f'{len(created)} department budgets')
    return jsonify({'ok': True, 'created': created})

@app.route('/api/productions/<int:pid>/revenue-forecast', methods=['GET'])
def get_revenue_forecast(pid):
    """Forecasted Revenue = average actual revenue of the last 3 completed
    productions in the same category (RS, MS, etc.) — distinct from Expected
    Revenue, which is this show's own revenue-line entries."""
    err = require_auth()
    if err: return err
    u = current_user()
    if u['role'] not in ORG_APPROVER_ROLES and not is_producer_of(u['id'], pid):
        return jsonify({'error': 'Insufficient permissions'}), 403
    conn = get_db()
    prod = conn.execute('SELECT * FROM bb_productions WHERE id=%s', (pid,)).fetchone()
    if not prod:
        conn.close(); return jsonify({'error': 'Not found'}), 404
    prod = dict(prod)
    category = prod.get('category')
    if not category:
        conn.close()
        return jsonify({'forecasted_revenue': None, 'basis': [], 'note': 'This show has no category set, so there is nothing comparable to average.'})
    comps = conn.execute('''SELECT id, name, season FROM bb_productions
                            WHERE category=%s AND id!=%s ORDER BY id DESC LIMIT 3''', (category, pid)).fetchall()
    comps = [dict(c) for c in comps]
    basis = []
    for c in comps:
        rev = conn.execute('SELECT COALESCE(SUM(actual),0) AS actual FROM bb_production_revenue WHERE production_id=%s', (c['id'],)).fetchone()
        actual = rev['actual'] or 0
        rc_line = rolecall_revenue_line(conn, c['id'])
        if rc_line:
            actual += rc_line['actual']
        basis.append({'id': c['id'], 'name': c['name'], 'season': c['season'], 'actual_revenue': round(actual, 2)})
    conn.close()
    forecasted = round(sum(b['actual_revenue'] for b in basis) / len(basis), 2) if basis else None
    return jsonify({'forecasted_revenue': forecasted, 'basis': basis,
                     'note': None if len(basis) == 3 else f'Only {len(basis)} comparable show(s) on record — average may not be reliable yet.'})

# ─── Receipt token / mobile receipt link ─────────────────────────────────────
def ensure_receipt_token(user_id):
    conn = get_db()
    u = conn.execute('SELECT receipt_token FROM bb_users WHERE id=%s',(user_id,)).fetchone()
    if not u or not u['receipt_token']:
        token = secrets.token_urlsafe(24)
        conn.execute('UPDATE bb_users SET receipt_token=%s WHERE id=%s',(token, user_id))
        conn.commit(); conn.close()
        return token
    conn.close()
    return u['receipt_token']

@app.route('/api/users/<int:uid>', methods=['DELETE'])
def delete_user(uid):
    err = require_auth(['admin','treasurer','president','resident_producer'])
    if err: return err
    u = current_user()
    if u['id'] == uid:
        return jsonify({'error': 'You cannot delete your own account'}), 400
    conn = get_db()
    conn.execute('DELETE FROM bb_production_members WHERE user_id=%s', (uid,))
    conn.execute('DELETE FROM bb_budget_members WHERE user_id=%s', (uid,))
    conn.execute('DELETE FROM bb_users WHERE id=%s', (uid,))
    conn.commit(); conn.close()
    log_action(u['id'], 'deleted_user', 'user', uid)
    return jsonify({'ok': True})
def get_receipt_token(uid):
    err = require_auth(['admin','treasurer','president','resident_producer'])
    if err: return err
    token = ensure_receipt_token(uid)
    return jsonify({'token': token, 'link': f"{APP_URL}/receipt/{token}"})

@app.route('/api/users/<int:uid>/receipt-token/regenerate', methods=['POST'])
def regenerate_receipt_token(uid):
    err = require_auth(['admin','treasurer','president','resident_producer'])
    if err: return err
    token = secrets.token_urlsafe(24)
    conn = get_db()
    conn.execute('UPDATE bb_users SET receipt_token=%s WHERE id=%s',(token, uid))
    conn.commit(); conn.close()
    return jsonify({'token': token, 'link': f"{APP_URL}/receipt/{token}"})

@app.route('/api/receipt/<token>', methods=['GET'])
def get_receipt_page_data(token):
    conn = get_db()
    u = conn.execute('SELECT id,name,email,role,training_complete,can_submit_org_level FROM bb_users WHERE receipt_token=%s AND is_active=1',(token,)).fetchone()
    if not u:
        conn.close()
        return jsonify({'error': 'Invalid or expired link'}), 404
    u = dict(u); uid = u['id']
    is_admin = u['role'] in ('admin','treasurer','president')
    can_org = is_admin or bool(u.get('can_submit_org_level'))
    reqs = conn.execute('''SELECT id,title,estimated_cost,actual_cost,status,type,vendor,submitted_at
                            FROM bb_purchase_requests
                            WHERE submitted_by=%s AND status NOT IN ('denied','reimbursed')
                            ORDER BY submitted_at DESC''', (uid,)).fetchall()
    my_prod_ids = [r['production_id'] for r in
                   conn.execute('SELECT production_id FROM bb_production_members WHERE user_id=%s',(uid,)).fetchall()]
    owned_ids = [r['budget_id'] for r in
                 conn.execute('SELECT budget_id FROM bb_budget_members WHERE user_id=%s',(uid,)).fetchall()]
    # Same rules as the desktop budget list: production budgets for their productions
    # + any budget they personally own + (if granted) every org-level budget.
    if is_admin:
        budgets = conn.execute('''SELECT b.id,b.name,b.area,b.total_amount,b.spent,b.production_id,
                                          p.name as production_name
                                   FROM bb_budgets b LEFT JOIN bb_productions p ON b.production_id=p.id
                                   WHERE b.is_active=1 ORDER BY p.name,b.name''').fetchall()
    else:
        clauses, params = [], []
        if my_prod_ids:
            ph = ','.join(['%s']*len(my_prod_ids))
            clauses.append(f'b.production_id IN ({ph})'); params.extend(my_prod_ids)
        if owned_ids:
            ph = ','.join(['%s']*len(owned_ids))
            clauses.append(f'b.id IN ({ph})'); params.extend(owned_ids)
        if can_org:
            clauses.append('b.production_id IS NULL')
        if clauses:
            where = '(' + ' OR '.join(clauses) + ') AND b.is_active=1'
            budgets = conn.execute(f'''SELECT b.id,b.name,b.area,b.total_amount,b.spent,b.production_id,
                                              p.name as production_name
                                       FROM bb_budgets b LEFT JOIN bb_productions p ON b.production_id=p.id
                                       WHERE {where} ORDER BY p.name,b.name''', params).fetchall()
        else:
            budgets = []
    if my_prod_ids:
        ph = ','.join(['%s']*len(my_prod_ids))
        productions = conn.execute(f"SELECT id,name,season FROM bb_productions WHERE id IN ({ph}) AND status='active'", my_prod_ids).fetchall()
    else:
        productions = []
    conn.close()
    owned_set = set(owned_ids)
    budget_list = []
    for b in budgets:
        bd = dict(b)
        bd['i_own'] = bd['id'] in owned_set
        budget_list.append(bd)
    return jsonify({'user':u,'requests':[dict(r) for r in reqs],
                    'budgets':budget_list,'productions':[dict(p) for p in productions]})

@app.route('/api/receipt/<token>/statements', methods=['GET'])
def mobile_list_statements(token):
    conn = get_db()
    u = conn.execute('SELECT id,name FROM bb_users WHERE receipt_token=%s AND is_active=1',(token,)).fetchone()
    if not u: conn.close(); return jsonify({'error':'Invalid or expired link'}),404
    uid = u['id']
    rows = conn.execute('''SELECT s.*,p.name as production_name,b.name as budget_name
                           FROM bb_statements s
                           LEFT JOIN bb_productions p ON s.production_id=p.id
                           LEFT JOIN bb_budgets b ON s.budget_id=b.id
                           WHERE s.created_by=%s ORDER BY s.updated_at DESC''',(uid,)).fetchall()
    result = []
    for row in rows:
        s = dict(row)
        items = conn.execute('''SELECT r.id,r.title,r.estimated_cost,r.actual_cost,r.status,r.type
                                FROM bb_statement_items si
                                JOIN bb_purchase_requests r ON si.request_id=r.id
                                WHERE si.statement_id=%s''',(s['id'],)).fetchall()
        s['items'] = [dict(i) for i in items]
        s['total'] = sum(i.get('actual_cost') or i.get('estimated_cost',0) for i in s['items'])
        result.append(s)
    conn.close()
    return jsonify(result)

@app.route('/api/receipt/<token>/statements', methods=['POST'])
def mobile_create_statement(token):
    conn = get_db()
    u = conn.execute('SELECT id,name FROM bb_users WHERE receipt_token=%s AND is_active=1',(token,)).fetchone()
    if not u: conn.close(); return jsonify({'error':'Invalid or expired link'}),404
    u = dict(u)
    data = request.json
    if not data.get('title'): return jsonify({'error':'Title is required'}),400
    now = datetime.now().isoformat()
    conn.execute('INSERT INTO bb_statements (title,description,created_by,updated_at) VALUES (%s,%s,%s,%s)',
                 (data['title'], data.get('description',''), u['id'], now))
    row = conn.execute('SELECT lastval() AS id').fetchone()
    sid = row['id']
    conn.commit(); conn.close()
    return jsonify({'ok':True,'id':sid})

@app.route('/api/receipt/<token>/statements/<int:sid>/items', methods=['POST'])
def mobile_add_statement_item(token, sid):
    conn = get_db()
    u = conn.execute('SELECT id,name FROM bb_users WHERE receipt_token=%s AND is_active=1',(token,)).fetchone()
    if not u: conn.close(); return jsonify({'error':'Invalid or expired link'}),404
    u = dict(u)
    s = conn.execute('SELECT * FROM bb_statements WHERE id=%s AND created_by=%s',(sid,u['id'])).fetchone()
    if not s: conn.close(); return jsonify({'error':'Statement not found'}),404
    if dict(s)['status'] != 'draft': conn.close(); return jsonify({'error':'Statement already submitted'}),400

    data = request.form if request.files else request.json or {}
    title   = (data.get('title') or '').strip()
    cost    = data.get('estimated_cost','')
    budget_id = data.get('budget_id')
    req_type  = data.get('type','pre_approval')
    is_sap    = 1 if req_type == 'sap' else 0

    if not title:     return jsonify({'error':'Title is required'}),400
    if not cost:      return jsonify({'error':'Amount is required'}),400
    if not budget_id: return jsonify({'error':'Please select a budget'}),400

    now = datetime.now().isoformat()
    conn.execute('''INSERT INTO bb_purchase_requests
                    (type,status,title,description,vendor,estimated_cost,budget_id,production_id,
                     submitted_by,is_emergency,purchase_method,item_url,authorized_by,
                     reimb_method,reimb_handle,statement_id,submitted_at,updated_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                 (req_type,'draft',title,data.get('description',''),data.get('vendor',''),
                  float(cost),int(budget_id),
                  int(data['production_id']) if data.get('production_id') else None,
                  u['id'],is_sap,data.get('purchase_method','in_store'),
                  data.get('item_url',''),data.get('authorized_by',''),
                  data.get('reimb_method',''),data.get('reimb_handle',''),
                  sid,now,now))
    row = conn.execute('SELECT lastval() AS id').fetchone()
    req_id = row['id']
    conn.execute('INSERT INTO bb_statement_items (statement_id,request_id) VALUES (%s,%s)',(sid,req_id))
    conn.execute('UPDATE bb_statements SET updated_at=%s WHERE id=%s',(now,sid))

    # Upload receipt if provided
    if is_sap and 'file' in request.files and request.files['file'].filename:
        if cloudinary.config().cloud_name:
            try:
                result = cloudinary.uploader.upload(request.files['file'],folder='bloombooks/receipts',resource_type='auto')
                conn.execute('INSERT INTO bb_receipts (request_id,image_url,public_id) VALUES (%s,%s,%s)',
                             (req_id,result['secure_url'],result['public_id']))
            except Exception: pass

    conn.commit(); conn.close()
    return jsonify({'ok':True,'request_id':req_id})

@app.route('/api/receipt/<token>/statements/<int:sid>/submit', methods=['POST'])
def mobile_submit_statement(token, sid):
    conn = get_db()
    u = conn.execute('SELECT id,name,email FROM bb_users WHERE receipt_token=%s AND is_active=1',(token,)).fetchone()
    if not u: conn.close(); return jsonify({'error':'Invalid or expired link'}),404
    u = dict(u)
    s = conn.execute('SELECT * FROM bb_statements WHERE id=%s AND created_by=%s',(sid,u['id'])).fetchone()
    if not s: conn.close(); return jsonify({'error':'Not found'}),404
    s = dict(s)
    if s['status'] != 'draft': conn.close(); return jsonify({'error':'Already submitted'}),400
    items = conn.execute('''SELECT r.* FROM bb_statement_items si
                            JOIN bb_purchase_requests r ON si.request_id=r.id
                            WHERE si.statement_id=%s''',(sid,)).fetchall()
    if not items: conn.close(); return jsonify({'error':'Add at least one item first'}),400
    now = datetime.now().isoformat()
    prod_id = s.get('production_id')
    item_status = 'pending_producer' if (prod_id and get_production_producers(prod_id)) else 'pending_treasurer'
    for item in items:
        conn.execute('UPDATE bb_purchase_requests SET status=%s,submitted_at=%s,updated_at=%s WHERE id=%s',
                     (item_status,now,now,item['id']))
    conn.execute("UPDATE bb_statements SET status='submitted',submitted_at=%s,updated_at=%s WHERE id=%s",(now,now,sid))
    conn.commit(); conn.close()
    return jsonify({'ok':True})

@app.route('/api/receipt/<token>/submit', methods=['POST'])
def submit_receipt_mobile(token):
    conn = get_db()
    try:
        u = conn.execute('SELECT id,name FROM bb_users WHERE receipt_token=%s AND is_active=1',(token,)).fetchone()
        if not u:
            conn.close(); return jsonify({'error':'Invalid or expired link'}),404
        u = dict(u)
        request_id = request.form.get('request_id')
        note       = request.form.get('note','')
        actual     = request.form.get('actual_cost','')
        if not request_id:
            conn.close(); return jsonify({'error':'No request selected'}),400
        request_id = int(request_id)
        req = conn.execute('SELECT * FROM bb_purchase_requests WHERE id=%s AND submitted_by=%s',(request_id,u['id'])).fetchone()
        if not req:
            conn.close(); return jsonify({'error':'Request not found'}),404
        image_url = None
        if 'file' in request.files and request.files['file'].filename:
            if not cloudinary.config().cloud_name:
                conn.close(); return jsonify({'error':'File upload not configured — contact an admin'}),500
            result = cloudinary.uploader.upload(request.files['file'],folder='bloombooks/receipts',resource_type='auto')
            image_url = result['secure_url']
            conn.execute('INSERT INTO bb_receipts (request_id,image_url,public_id) VALUES (%s,%s,%s)',
                         (request_id,image_url,result['public_id']))
        if actual:
            try: conn.execute('UPDATE bb_purchase_requests SET actual_cost=%s WHERE id=%s',(float(actual),request_id))
            except Exception: pass
        if note:
            existing = req['description'] or ''
            conn.execute('UPDATE bb_purchase_requests SET description=%s WHERE id=%s',
                         (f"{existing}\n\n[Receipt note]: {note}".strip(),request_id))
        conn.commit()
        log_action(u['id'],'mobile_receipt_upload','request',request_id)
        conn.close()
        return jsonify({'ok':True,'image_url':image_url})
    except Exception as e:
        print(f"[RECEIPT SUBMIT ERROR] {e}")
        try: conn.close()
        except Exception: pass
        return jsonify({'error': f'Submission error: {str(e)}'}), 500

@app.route('/api/receipt/<token>/new-request', methods=['POST'])
def mobile_new_request(token):
    conn = get_db()
    u = conn.execute('SELECT id,name,role,training_complete,can_submit_org_level FROM bb_users WHERE receipt_token=%s AND is_active=1',(token,)).fetchone()
    if not u: conn.close(); return jsonify({'error':'Invalid or expired link'}),404
    u = dict(u); uid = u['id']
    data      = request.form
    title     = data.get('title','').strip()
    budget_id = data.get('budget_id')
    est_cost  = data.get('estimated_cost','')
    req_type  = data.get('type','pre_approval')
    is_sap    = 1 if req_type == 'sap' else 0
    sap_reason= data.get('sap_reason','')
    method    = data.get('purchase_method','in_store')
    item_url  = data.get('item_url','')
    vendor    = data.get('vendor','')
    desc      = data.get('description','')
    prod_id   = data.get('production_id') or None
    if not title:     return jsonify({'error':'Title is required'}),400
    if not budget_id: return jsonify({'error':'Please select a budget'}),400
    if not est_cost:  return jsonify({'error':'Please enter the estimated amount'}),400
    if not user_can_use_budget(u, int(budget_id)):
        conn.close(); return jsonify({'error':'You are not permitted to submit against that budget.'}),403
    status = 'pending_producer' if (prod_id and get_production_producers(int(prod_id))) else 'pending_treasurer'
    conn.execute('''INSERT INTO bb_purchase_requests
                    (type,status,title,description,vendor,estimated_cost,budget_id,production_id,
                     submitted_by,is_emergency,emergency_reason,purchase_method,item_url)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                 (req_type,status,title,desc,vendor,float(est_cost),
                  int(budget_id),int(prod_id) if prod_id else None,
                  uid,is_sap,sap_reason,method,item_url))
    row = conn.execute('SELECT lastval() AS id').fetchone()
    req_id = row['id']
    if is_sap and 'file' in request.files and request.files['file'].filename:
        if cloudinary.config().cloud_name:
            try:
                result = cloudinary.uploader.upload(request.files['file'],folder='bloombooks/receipts',resource_type='auto')
                conn.execute('INSERT INTO bb_receipts (request_id,image_url,public_id) VALUES (%s,%s,%s)',
                             (req_id,result['secure_url'],result['public_id']))
            except Exception: pass
    conn.commit()
    log_action(uid,'mobile_new_request','request',req_id,title)
    user_info = get_user_email(uid)
    if user_info:
        notify_request_submitted(
            req_id=req_id, req_title=title, submitter_name=u['name'],
            submitter_email=user_info['email'], estimated_cost=est_cost,
            req_type=req_type, purchase_method=method, item_url=item_url,
            production_id=int(prod_id) if prod_id else None, status=status)
    conn.close()
    return jsonify({'ok':True,'id':req_id})

@app.route('/receipt/<token>')
def mobile_receipt_page(token):
    return send_from_directory(app.static_folder, 'receipt.html')

@app.route('/sign/<token>')
def esign_page(token):
    return send_from_directory(app.static_folder, 'sign.html')

# ─── Pricing Calculator ───────────────────────────────────────────────────────
# Figures out what to charge for programming (classes, workshops, Rising
# Stars) and external rentals, using real facility costs from the Budgets
# module rather than separately re-entered numbers.

PC_DAY_NAMES = ['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday']

def _pc_hours_between(start_time, end_time):
    try:
        sh, sm = [int(x) for x in start_time.split(':')]
        eh, em = [int(x) for x in end_time.split(':')]
        mins = (eh*60+em) - (sh*60+sm)
        return max(0, mins)/60.0
    except Exception:
        return 0.0

@app.route('/api/space-capacity-hours', methods=['GET'])
def get_space_capacity_hours():
    err = require_auth()
    if err: return err
    conn = get_db()
    rows = conn.execute('SELECT * FROM bb_space_capacity_hours ORDER BY day_of_week').fetchall()
    rows = [dict(r) for r in rows]
    if len(rows) < 7:
        existing = {r['day_of_week'] for r in rows}
        for dow in range(7):
            if dow not in existing:
                conn.execute('''INSERT INTO bb_space_capacity_hours (day_of_week, open_time, close_time, closed)
                    VALUES (%s,'08:00','22:00',0) ON CONFLICT (day_of_week) DO NOTHING''', (dow,))
        conn.commit()
        rows = [dict(r) for r in conn.execute('SELECT * FROM bb_space_capacity_hours ORDER BY day_of_week').fetchall()]
    conn.close()
    return jsonify(rows)

@app.route('/api/space-capacity-hours', methods=['PUT'])
def update_space_capacity_hours():
    err = require_auth(roles=['admin','treasurer','president','resident_producer','producer'])
    if err: return err
    data = request.json or {}
    conn = get_db()
    for h in data.get('hours', []):
        conn.execute('''INSERT INTO bb_space_capacity_hours (day_of_week, open_time, close_time, closed)
            VALUES (%s,%s,%s,%s)
            ON CONFLICT (day_of_week) DO UPDATE SET open_time=EXCLUDED.open_time,
                close_time=EXCLUDED.close_time, closed=EXCLUDED.closed''',
            (int(h['day_of_week']), h.get('open_time','08:00'), h.get('close_time','22:00'), 1 if h.get('closed') else 0))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/pricing-settings', methods=['GET'])
def get_pricing_settings():
    err = require_auth()
    if err: return err
    conn = get_db()
    row = conn.execute('SELECT * FROM bb_pricing_settings ORDER BY id LIMIT 1').fetchone()
    if not row:
        conn.execute('INSERT INTO bb_pricing_settings (season_weeks) VALUES (36)')
        conn.commit()
        row = conn.execute('SELECT * FROM bb_pricing_settings ORDER BY id LIMIT 1').fetchone()
    conn.close()
    return jsonify(dict(row))

@app.route('/api/pricing-settings', methods=['PUT'])
def update_pricing_settings():
    err = require_auth(roles=['admin','treasurer','president','resident_producer','producer'])
    if err: return err
    data = request.json or {}
    conn = get_db()
    row = conn.execute('SELECT id FROM bb_pricing_settings ORDER BY id LIMIT 1').fetchone()
    facility_budget_id = data.get('facility_budget_id') or None
    season_weeks = int(data.get('season_weeks', 36))
    if row:
        conn.execute('''UPDATE bb_pricing_settings SET facility_budget_id=%s, season_weeks=%s,
            updated_at=to_char(now(),'YYYY-MM-DD HH24:MI:SS') WHERE id=%s''',
            (facility_budget_id, season_weeks, row['id']))
    else:
        conn.execute('INSERT INTO bb_pricing_settings (facility_budget_id, season_weeks) VALUES (%s,%s)',
            (facility_budget_id, season_weeks))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

def _facility_cost_result(conn):
    """Shared calc behind /api/pricing-calc/facility-cost — also used by the
    Build Show rehearsal-space charge calculation so both stay in sync."""
    settings = conn.execute('SELECT * FROM bb_pricing_settings ORDER BY id LIMIT 1').fetchone()
    settings = dict(settings) if settings else {'facility_budget_id': None, 'season_weeks': 36}
    season_weeks = settings.get('season_weeks') or 36

    cap_rows = [dict(r) for r in conn.execute('SELECT * FROM bb_space_capacity_hours').fetchall()]
    weekly_hours = sum(_pc_hours_between(r['open_time'], r['close_time']) for r in cap_rows if not r['closed'])
    total_possible_hours = weekly_hours * season_weeks

    result = {
        'facility_budget_id': settings.get('facility_budget_id'),
        'budget_name': None,
        'budgeted_total': 0,
        'spent_total': 0,
        'season_weeks': season_weeks,
        'weekly_hours': round(weekly_hours, 1),
        'total_possible_hours': round(total_possible_hours, 1),
        'cost_per_hour_budgeted': None,
        'cost_per_hour_spent': None,
    }

    bid = settings.get('facility_budget_id')
    if bid:
        budget = conn.execute('SELECT * FROM bb_budgets WHERE id=%s', (bid,)).fetchone()
        if budget:
            budget = dict(budget)
            rollup = conn.execute('''SELECT COALESCE(SUM(total_amount),0) AS budgeted, COALESCE(SUM(spent),0) AS spent
                FROM bb_budgets WHERE id=%s OR parent_id=%s''', (bid, bid)).fetchone()
            result['budget_name'] = budget['name']
            result['budgeted_total'] = rollup['budgeted']
            result['spent_total'] = rollup['spent']
            if total_possible_hours > 0:
                result['cost_per_hour_budgeted'] = round(rollup['budgeted']/total_possible_hours, 2)
                if rollup['spent'] > 0:
                    result['cost_per_hour_spent'] = round(rollup['spent']/total_possible_hours, 2)
    return result

@app.route('/api/pricing-calc/facility-cost', methods=['GET'])
def get_facility_cost():
    err = require_auth()
    if err: return err
    conn = get_db()
    result = _facility_cost_result(conn)
    conn.close()
    return jsonify(result)

def _compute_studio_charge(conn, weekly_hours, rehearsal_weeks):
    """Rehearsal-space charge for a show, using the same at-cost $/hour the
    Pricing Calculator uses for everything else (prefers the budgeted rate;
    falls back to the actual-spent rate if nothing's budgeted yet). weekly_hours
    is the total rehearsal hours per week across all day/time blocks combined."""
    fc = _facility_cost_result(conn)
    rate = fc.get('cost_per_hour_budgeted') or fc.get('cost_per_hour_spent') or 0
    total_hours = float(weekly_hours or 0) * float(rehearsal_weeks or 0)
    charge = round(total_hours * rate, 2)
    return {'total_rehearsal_hours': round(total_hours, 1), 'cost_per_hour': rate, 'studio_charge': charge}

# ─── E-signature: templates, PDF generation, signing requests ─────────────────
def render_merge(body, values):
    """Replace {{field}} tags with values; unknown tags render blank."""
    def repl(m):
        return str(values.get(m.group(1).strip(), ''))
    return re.sub(r'\{\{\s*([\w.]+)\s*\}\}', repl, body or '')

def sign_request_meta():
    ip = request.headers.get('X-Forwarded-For', request.remote_addr or '').split(',')[0].strip()
    ua = (request.headers.get('User-Agent', '') or '')[:300]
    return ip, ua

def now_str():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')

def _signature_certificate(story, styles, signer_name, signer_ip, signer_ua, consent_at, signed_at, token):
    story.append(Spacer(1, 22))
    story.append(HRFlowable(width='100%', color=colors.HexColor('#cccccc'), thickness=0.7))
    story.append(Spacer(1, 10))
    story.append(Paragraph('Electronic Signature Certificate', styles['Heading3']))
    rows = [
        ['Signed by', signer_name],
        ['Consented to electronic signature at', consent_at],
        ['Signature completed at', signed_at],
        ['IP address', signer_ip or 'unknown'],
        ['Browser / device', (signer_ua or 'unknown')],
        ['Signing request ID', token[:16] + '…'],
    ]
    t = Table(rows, colWidths=[170, 330])
    t.setStyle(TableStyle([
        ('FONTSIZE', (0, 0), (-1, -1), 9),
        ('TEXTCOLOR', (0, 0), (0, -1), colors.HexColor('#555555')),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
    ]))
    story.append(t)
    story.append(Spacer(1, 8))
    story.append(Paragraph(
        'This document was signed electronically. Under the U.S. ESIGN Act and applicable state UETA law, '
        'this electronic signature has the same legal effect as a handwritten signature.',
        ParagraphStyle('small_italic', parent=styles['Italic'], fontSize=8, textColor=colors.HexColor('#777777'))))

# ── HWTC branding (logo extracted from the org's real letterhead) ─────────────
TEAL = colors.HexColor('#0f6e56')
TEAL_DARK = colors.HexColor('#0b4f3f')
PDF_INK = colors.HexColor('#1e1e1b')
PDF_INK2 = colors.HexColor('#4a4a45')
LOGO_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'assets', 'hwtc_logo.png')
LOGO_ASPECT = 683 / 331  # width / height of the source logo asset
PDF_MARGIN = 56
PDF_FOOTER_H = 20

def _pdf_footer(c):
    c.saveState()
    c.setFillColor(TEAL)
    c.rect(0, 0, letter[0], PDF_FOOTER_H, fill=1, stroke=0)
    c.setFillColor(TEAL_DARK)
    p = c.beginPath()
    p.moveTo(0, 0); p.lineTo(0, PDF_FOOTER_H + 26); p.lineTo(34, 0); p.close()
    c.drawPath(p, fill=1, stroke=0)
    c.restoreState()

def _pdf_header(c, title):
    c.saveState()
    if os.path.exists(LOGO_PATH):
        logo_w = 1.55 * inch
        logo_h = logo_w / LOGO_ASPECT
        logo_x = PDF_MARGIN
        logo_y = letter[1] - PDF_MARGIN - logo_h + 8
        c.drawImage(LOGO_PATH, logo_x, logo_y, width=logo_w, height=logo_h, mask='auto')
        line_y = logo_y + logo_h * 0.42
        line_x0 = logo_x + logo_w + 14
    else:
        line_y = letter[1] - PDF_MARGIN - 20
        line_x0 = PDF_MARGIN
    c.setStrokeColor(TEAL)
    c.setLineWidth(1.4)
    c.line(line_x0, line_y, letter[0] - PDF_MARGIN, line_y)
    c.setFillColor(PDF_INK)
    c.setFont('Helvetica-Bold', 13)
    c.drawRightString(letter[0] - PDF_MARGIN, line_y + 8, title.upper())
    c.restoreState()

def _pdf_page_decorations(title):
    def _fn(c, doc):
        _pdf_header(c, title)
        _pdf_footer(c)
    return _fn

def build_agreement_pdf(title, body_text, signer_name, signer_ip, signer_ua, consent_at, signed_at, token):
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter,
                             topMargin=PDF_MARGIN + 60, bottomMargin=PDF_MARGIN + 10,
                             leftMargin=PDF_MARGIN, rightMargin=PDF_MARGIN)
    styles = getSampleStyleSheet()
    body_style = ParagraphStyle('bodyc', parent=styles['Normal'], fontSize=10, leading=14.5, textColor=PDF_INK2)
    heading_style = ParagraphStyle('h4c', parent=styles['Heading4'], fontSize=10.5, textColor=PDF_INK, spaceBefore=4)
    story = [Spacer(1, 4)]
    for para in (body_text or '').split('\n\n'):
        para = para.strip()
        if not para:
            continue
        first_line = para.split('\n')[0]
        if first_line.isupper() and len(first_line) < 70:
            story.append(Paragraph(para.replace('\n', '<br/>'), heading_style))
        else:
            story.append(Paragraph(para.replace('\n', '<br/>'), body_style))
        story.append(Spacer(1, 7))
    story.append(Spacer(1, 16))
    sig_style = ParagraphStyle('sig', parent=body_style, textColor=PDF_INK)
    story.append(Paragraph(f'<b>Signed:</b> {signer_name}', sig_style))
    story.append(Paragraph(f'<b>Date:</b> {signed_at}', sig_style))
    _signature_certificate(story, styles, signer_name, signer_ip, signer_ua, consent_at, signed_at, token)
    deco = _pdf_page_decorations(title)
    doc.build(story, onFirstPage=deco, onLaterPages=deco)
    return buf.getvalue()

def build_bank_auth_pdf(fields, signer_name, signer_ip, signer_ua, consent_at, signed_at, token):
    """ACH/direct deposit authorization. The retained record shows the routing number in full
    (it identifies a bank/branch, not the person, and isn't sensitive on its own) but masks the
    account number to last 4 — the actual number stays encrypted in bb_contractor_bank_accounts
    and is only used from there for payments, so the PDF doesn't need to duplicate it in full."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter,
                             topMargin=PDF_MARGIN + 60, bottomMargin=PDF_MARGIN + 10,
                             leftMargin=PDF_MARGIN, rightMargin=PDF_MARGIN)
    styles = getSampleStyleSheet()
    body_style = ParagraphStyle('bodyc2', parent=styles['Normal'], fontSize=10, leading=14.5, textColor=PDF_INK2)
    heading_style = ParagraphStyle('h4c2', parent=styles['Heading4'], fontSize=11, textColor=PDF_INK, spaceBefore=4)

    holder = fields.get('account_holder_name') or signer_name
    intro = (
        f"I, {holder}, authorize {ORG_NAME} to deposit payments owed to me for services rendered as an "
        f"independent contractor directly into the bank account listed below via ACH (Automated Clearing "
        f"House) transfer, in lieu of a paper check."
    )
    understand_items = [
        f"This authorization remains in effect until I submit a new authorization form or notify {ORG_NAME} "
        f"in writing to cancel or change it.",
        f"It is my responsibility to promptly notify {ORG_NAME} of any changes to this account information.",
        f"{ORG_NAME} is not responsible for delays or errors caused by incorrect account information I provide.",
    ]

    story = [Paragraph('ACH / DIRECT DEPOSIT PAYMENT AUTHORIZATION', heading_style), Spacer(1, 10)]
    story.append(Paragraph(intro, body_style))
    story.append(Spacer(1, 8))
    story.append(Paragraph('By signing this form, I understand and agree that:', body_style))
    for item in understand_items:
        story.append(Paragraph(f'\u2022 {item}', body_style))
    story.append(Spacer(1, 16))

    rows = [
        ['Account holder name', holder],
        ['Account type', (fields.get('account_type') or '').capitalize()],
        ['Bank routing number', fields.get('routing_number_display', '')],
        ['Bank account number', fields.get('account_number_display', '')],
    ]
    t = Table(rows, colWidths=[160, 300])
    t.setStyle(TableStyle([
        ('FONTSIZE', (0, 0), (-1, -1), 10),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#dddddd')),
        ('BACKGROUND', (0, 0), (0, -1), colors.HexColor('#f7f7f5')),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 7),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 7),
        ('LEFTPADDING', (0, 0), (-1, -1), 8),
    ]))
    story.append(t)
    story.append(Spacer(1, 18))
    sig_style = ParagraphStyle('sig2', parent=body_style, textColor=PDF_INK)
    story.append(Paragraph(f'<b>Signed:</b> {signer_name}', sig_style))
    story.append(Paragraph(f'<b>Date:</b> {signed_at}', sig_style))
    _signature_certificate(story, styles, signer_name, signer_ip, signer_ua, consent_at, signed_at, token)

    deco = _pdf_page_decorations('Payment Authorization')
    doc.build(story, onFirstPage=deco, onLaterPages=deco)
    return buf.getvalue()

# ── Form W-9 — closer visual facsimile of the real IRS grid layout ─────────────
IRS_W9_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'assets', 'irs_form_w9.pdf')
W9_FIELD_PREFIX = 'topmostSubform[0].Page1[0]'

# Maps our internal tax_classification value -> the official form's checkbox "on" value.
# (Field IDs and on-values were extracted directly from the IRS's own fillable PDF.)
W9_CLASS_CHECKBOX_VALUES = {
    'individual': '/1', 'c_corp': '/2', 's_corp': '/3',
    'partnership': '/4', 'trust_estate': '/5', 'llc': '/6', 'other': '/7',
}

# Coordinates (PDF points, origin bottom-left) of the blank space above the
# "Signature of U.S. person" / "Date" captions in the Sign Here box — this part
# of the official form has no fillable field, so we overlay text onto it directly.
W9_SIGNATURE_XY = (78, 203)
W9_DATE_XY = (390, 203)

def _pdf_certificate_page_bytes(document_label, signer_name, signer_ip, signer_ua, consent_at, signed_at, token):
    """A single reportlab-drawn page recording the e-signature audit trail, to append after a filled form."""
    buf = io.BytesIO()
    c = pdfcanvas.Canvas(buf, pagesize=letter)
    W, H = letter
    m = 42
    c.setFont('Helvetica-Bold', 14)
    c.setFillColor(PDF_INK)
    c.drawString(m, H - 70, 'Electronic Signature Certificate')
    c.setStrokeColor(colors.HexColor('#cccccc'))
    c.line(m, H - 80, W - m, H - 80)
    rows = [
        ('Document', document_label), ('Signed by', signer_name),
        ('Consented to electronic signature at', consent_at), ('Signature completed at', signed_at),
        ('IP address', signer_ip or 'unknown'), ('Browser / device', signer_ua or 'unknown'),
        ('Signing request ID', token[:16] + '\u2026'),
    ]
    ry = H - 105
    for label, val in rows:
        c.setFont('Helvetica', 8.5); c.setFillColor(colors.HexColor('#555555'))
        c.drawString(m, ry, label)
        c.setFont('Helvetica', 9.5); c.setFillColor(PDF_INK)
        c.drawString(m + 210, ry, str(val)[:80])
        ry -= 16
    c.setFont('Helvetica-Oblique', 7.5); c.setFillColor(colors.HexColor('#777777'))
    c.drawString(m, ry - 10, 'This document was signed electronically. Under the U.S. ESIGN Act and applicable state UETA law,')
    c.drawString(m, ry - 20, 'this electronic signature has the same legal effect as a handwritten signature.')
    _pdf_footer(c)
    c.showPage()
    c.save()
    return buf.getvalue()

def build_w9_pdf(fields, tin_type, tin_display, signer_name, signer_ip, signer_ua, consent_at, signed_at, token):
    """Fills the actual official IRS Form W-9 (the fillable PDF the IRS publishes), rather than a redrawn copy."""
    P = W9_FIELD_PREFIX
    writer = PdfWriter(clone_from=IRS_W9_PATH)

    classification = fields.get('tax_classification', '')
    field_values = {
        f'{P}.f1_01[0]': fields.get('legal_name', ''),
        f'{P}.f1_02[0]': fields.get('business_name', '') or '',
        f'{P}.Address_ReadOrder[0].f1_07[0]': fields.get('address', ''),
        f'{P}.Address_ReadOrder[0].f1_08[0]': fields.get('city_state_zip', ''),
        f'{P}.f1_09[0]': ORG_NAME,  # "Requester's name and address (optional)"
        f'{P}.f1_10[0]': fields.get('account_numbers', '') or '',
        f'{P}.f1_05[0]': fields.get('exempt_payee_code', '') or '',
        f'{P}.f1_06[0]': fields.get('fatca_code', '') or '',
    }
    if classification in W9_CLASS_CHECKBOX_VALUES:
        idx = ['individual', 'c_corp', 's_corp', 'partnership', 'trust_estate', 'llc', 'other'].index(classification)
        field_values[f'{P}.Boxes3a-b_ReadOrder[0].c1_1[{idx}]'] = W9_CLASS_CHECKBOX_VALUES[classification]
    if classification == 'llc':
        field_values[f'{P}.Boxes3a-b_ReadOrder[0].f1_03[0]'] = fields.get('llc_tax_class', '')
    if classification == 'other':
        field_values[f'{P}.Boxes3a-b_ReadOrder[0].f1_04[0]'] = fields.get('other_description', '')

    digits = (tin_display or '').replace('-', '')
    if tin_type == 'ein':
        field_values[f'{P}.f1_14[0]'] = digits[:2]
        field_values[f'{P}.f1_15[0]'] = digits[2:9]
    else:
        field_values[f'{P}.f1_11[0]'] = digits[:3]
        field_values[f'{P}.f1_12[0]'] = digits[3:5]
        field_values[f'{P}.f1_13[0]'] = digits[5:9]

    writer.update_page_form_field_values(writer.pages[0], field_values, auto_regenerate=False)

    # This is an XFA-based IRS form; without stripping /XFA, some viewers render the
    # (blank) XFA layer instead of our filled AcroForm values. NeedAppearances tells
    # viewers to regenerate field appearances from the values we just set.
    acro = writer._root_object['/AcroForm']
    if '/XFA' in acro:
        del acro[NameObject('/XFA')]
    acro[NameObject('/NeedAppearances')] = BooleanObject(True)

    # Keep only the form page itself — the IRS's 6-page PDF is mostly instructions
    # for the person filling it out, not something a business needs to keep on file.
    for i in range(len(writer.pages) - 1, 0, -1):
        writer.remove_page(i)

    # The signature line isn't a fillable field on the official form, so overlay it.
    sig_buf = io.BytesIO()
    c = pdfcanvas.Canvas(sig_buf, pagesize=letter)
    c.setFont('Helvetica-Oblique', 13)
    c.setFillColorRGB(0.05, 0.05, 0.15)
    c.drawString(W9_SIGNATURE_XY[0], W9_SIGNATURE_XY[1], signer_name)
    c.setFont('Helvetica', 9)
    c.setFillColorRGB(0, 0, 0)
    c.drawString(W9_DATE_XY[0], W9_DATE_XY[1], signed_at[:10])
    c.save()
    sig_buf.seek(0)
    writer.pages[0].merge_page(PdfReader(sig_buf).pages[0])

    cert_bytes = _pdf_certificate_page_bytes('Form W-9', signer_name, signer_ip, signer_ua, consent_at, signed_at, token)
    writer.append_pages_from_reader(PdfReader(io.BytesIO(cert_bytes)))

    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()

def upload_pdf_private(pdf_bytes, folder):
    # Reverted to 'private' — this is the one delivery mechanism that's actually confirmed
    # working on this account (via the Admin API /download endpoint in signed_doc_url).
    # 'authenticated' + CDN delivery kept failing with "Unauthenticated access" across
    # several attempted fixes (version numbers, dot-escaping, image vs raw resource_type)
    # without a clear resolution, so reliability wins over the custom-filename nicety —
    # private downloads work, just always named after Cloudinary's internal asset ID.
    return cloudinary.uploader.upload(
        io.BytesIO(pdf_bytes), folder=folder, resource_type='raw', type='private'
    )

# ─── Contractors (secure: profiles, W9s/agreements, banking, payments) ────────
# This whole section is restricted to admin & treasurer only — it's the one part
# of BloomBooks that touches SSNs/EINs and bank account numbers, so access is
# deliberately narrower than the rest of the "Admin" area (which also includes
# president/producer). Every create/update/delete/reveal/download is written to
# bb_audit_log so there's a trail of who touched what and when.
CONTRACTOR_ROLES = ('admin', 'treasurer', 'president')

def require_contractor_access():
    u = current_user()
    if not u:
        return None, (jsonify({'error': 'Not authenticated'}), 401)
    if u['role'] not in CONTRACTOR_ROLES:
        return None, (jsonify({'error': 'Insufficient permissions'}), 403)
    return u, None

DOC_TYPE_LABELS = {'w9': 'W-9', 'agreement': 'Contractor Agreement', 'bank_auth': 'Bank/ACH Authorization', 'other': 'Document'}

def _slug_segment(text):
    return re.sub(r'[^A-Za-z0-9]+', '_', (text or '')).strip('_').lower()

def build_download_filename(contractor_name, doc_type, doc_title):
    """firstname.lastname.doctype — e.g. jane.smith.w9 or jane.smith.instructor_agreement"""
    parts = [p for p in (contractor_name or '').split() if p]
    first = _slug_segment(parts[0]) if parts else 'contractor'
    last = _slug_segment(parts[-1]) if len(parts) > 1 else ''
    if doc_type == 'w9':
        doc_slug = 'w9'
    else:
        doc_slug = _slug_segment(os.path.splitext(doc_title or '')[0]) or _slug_segment(doc_type) or 'document'
    return '.'.join([p for p in (first, last, doc_slug) if p])

def signed_doc_url(public_id, resource_type='raw', fmt=None, download_name=None, access_type='private', version=None):
    """Time-limited download link for a restricted Cloudinary asset.
    - 'authenticated' assets: delivered via a normal signed CDN URL, which supports a custom
      download filename via the fl_attachment:<name> transformation flag. This requires the
      asset's REAL version number (as returned by the upload call) — without it, the SDK
      falls back to a placeholder "v1" that doesn't match the asset Cloudinary actually has,
      and the signature check fails with "Unauthenticated access" even though the URL looks
      well-formed.
    - 'private' assets (legacy — uploaded before this fix): NOT servable through the CDN at all.
      Must go through Cloudinary's separate /download API (private_download_url), which only
      supports a boolean attachment flag — Cloudinary always names the file after its own
      internal ID, with no way to override it. Kept only so older documents still download."""
    safe_name = None
    if download_name:
        cleaned = re.sub(r'[\\/:"*?<>|]+', ' ', download_name).strip()
        if cleaned:
            safe_name = cleaned

    if access_type == 'authenticated':
        # Transformations (including fl_attachment, which sets the filename) only work on
        # image/video assets — raw assets 400 outright if you try. New documents always
        # upload as resource_type='image' now, but a few got uploaded as 'raw' during the
        # brief window before that fix; this keeps those downloadable (generic name only)
        # instead of erroring out.
        can_use_flags = (resource_type or 'image') != 'raw'
        flags_value = None
        if can_use_flags:
            if safe_name:
                # Cloudinary's transformation syntax splits on '.' to separate multiple
                # flags, so a literal dot inside the filename (e.g. "jane.smith.w9") gets
                # misparsed as extra, invalid flags. Escaping it as %2E is Cloudinary's
                # documented workaround; it still comes through as a literal dot in the
                # actual downloaded filename.
                escaped_name = safe_name.replace('.', '%2E')
                flags_value = f'attachment:{escaped_name}'
            else:
                flags_value = 'attachment'
        url, _opts = cloudinary.utils.cloudinary_url(
            public_id,
            resource_type=resource_type or 'image',
            type='authenticated',
            sign_url=True,
            secure=True,
            format=fmt or 'pdf',
            flags=flags_value,
            version=version,
            expires_at=int(time.time()) + 300,
        )
        return url

    return cloudinary.utils.private_download_url(
        public_id,
        fmt or 'pdf',
        resource_type=resource_type or 'raw',
        type='private',
        attachment=safe_name or True,
        expires_at=int(time.time()) + 300,
    )

def scrub_contractor(row):
    d = dict(row)
    d.pop('ein_ssn_encrypted', None)
    return d

@app.route('/api/contractors', methods=['GET'])
def list_contractors():
    u, err = require_contractor_access()
    if err: return err
    conn = get_db()
    rows = conn.execute('''
        SELECT ct.*,
          EXISTS(SELECT 1 FROM bb_contractor_documents d WHERE d.contractor_id=ct.id AND d.doc_type='w9') AS has_w9,
          EXISTS(SELECT 1 FROM bb_contractor_documents d WHERE d.contractor_id=ct.id AND d.doc_type='agreement') AS has_agreement,
          (SELECT COALESCE(SUM(amount),0) FROM bb_contractor_payments p WHERE p.contractor_id=ct.id AND p.status='paid') AS total_paid
        FROM bb_contractors ct ORDER BY ct.status ASC, ct.name ASC
    ''').fetchall()
    conn.close()
    return jsonify([scrub_contractor(r) for r in rows])

@app.route('/api/contractors', methods=['POST'])
def create_contractor():
    u, err = require_contractor_access()
    if err: return err
    data = request.json or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'Contractor name is required'}), 400
    conn = get_db()
    c = conn.execute('''INSERT INTO bb_contractors
        (name, business_name, contact_email, contact_phone, address, tax_classification, notes, created_by)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id''',
        (name, data.get('business_name'), data.get('contact_email'), data.get('contact_phone'),
         data.get('address'), data.get('tax_classification', 'individual'), data.get('notes'), u['id']))
    cid = c.fetchone()['id']
    conn.commit(); conn.close()
    log_action(u['id'], 'created_contractor', 'contractor', cid, name)
    return jsonify({'ok': True, 'id': cid})

@app.route('/api/contractors/<int:cid>', methods=['GET'])
def get_contractor(cid):
    u, err = require_contractor_access()
    if err: return err
    conn = get_db()
    ct = conn.execute('SELECT * FROM bb_contractors WHERE id=%s', (cid,)).fetchone()
    if not ct:
        conn.close(); return jsonify({'error': 'Not found'}), 404
    docs = conn.execute('''SELECT d.id, d.doc_type, d.filename, d.format, d.effective_date, d.expires_at, d.uploaded_by, d.uploaded_at,
                           (SELECT sr.custom_fields::json->>'class_workshop' FROM bb_signing_requests sr
                            WHERE sr.final_document_id = d.id LIMIT 1) AS class_workshop
                           FROM bb_contractor_documents d WHERE d.contractor_id=%s ORDER BY d.uploaded_at DESC''', (cid,)).fetchall()
    banks = conn.execute('''SELECT id, nickname, account_holder_name, account_type, routing_last4, account_last4,
                            is_primary, is_active, created_at
                            FROM bb_contractor_bank_accounts WHERE contractor_id=%s
                            ORDER BY is_primary DESC, created_at''', (cid,)).fetchall()
    payments = conn.execute('''SELECT p.*, u.name AS paid_by_name FROM bb_contractor_payments p
                               LEFT JOIN bb_users u ON p.paid_by = u.id
                               WHERE p.contractor_id=%s ORDER BY p.payment_date DESC, p.id DESC''', (cid,)).fetchall()
    conn.close()
    payments = [dict(p) for p in payments]
    if payments:
        conn2 = get_db()
        for p in payments:
            linked = conn2.execute('''SELECT e.name, e.event_date FROM bb_contractor_payment_events cpe
                JOIN events e ON e.id = cpe.rolecall_event_id
                WHERE cpe.payment_id=%s ORDER BY e.event_date''', (p['id'],)).fetchall()
            p['linked_classes'] = [dict(l) for l in linked]
        conn2.close()
    return jsonify({
        'contractor': scrub_contractor(ct),
        'documents': [dict(d) for d in docs],
        'bank_accounts': [dict(b) for b in banks],
        'payments': payments,
        'total_paid': sum(p['amount'] for p in payments if p['status'] == 'paid')
    })

@app.route('/api/contractors/<int:cid>', methods=['PUT'])
def update_contractor(cid):
    u, err = require_contractor_access()
    if err: return err
    data = request.json or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'Contractor name is required'}), 400
    conn = get_db()
    conn.execute('''UPDATE bb_contractors SET name=%s, business_name=%s, contact_email=%s, contact_phone=%s,
        address=%s, tax_classification=%s, status=%s, notes=%s,
        updated_at=to_char(now(),'YYYY-MM-DD HH24:MI:SS') WHERE id=%s''',
        (name, data.get('business_name'), data.get('contact_email'), data.get('contact_phone'),
         data.get('address'), data.get('tax_classification'), data.get('status', 'active'), data.get('notes'), cid))
    conn.commit(); conn.close()
    log_action(u['id'], 'updated_contractor', 'contractor', cid, name)
    return jsonify({'ok': True})

@app.route('/api/contractors/<int:cid>', methods=['DELETE'])
def delete_contractor(cid):
    u = current_user()
    if not u or u['role'] != 'admin':
        return jsonify({'error': 'Insufficient permissions'}), 403
    conn = get_db()
    n = conn.execute('SELECT COUNT(*) AS n FROM bb_contractor_payments WHERE contractor_id=%s', (cid,)).fetchone()['n']
    if n > 0:
        conn.close()
        return jsonify({'error': 'This contractor has recorded payments and can\'t be deleted — mark them inactive instead.'}), 400
    docs = conn.execute('SELECT * FROM bb_contractor_documents WHERE contractor_id=%s', (cid,)).fetchall()
    for doc in docs:
        try:
            cloudinary.uploader.destroy(doc['cloud_public_id'], resource_type=doc['resource_type'] or 'raw', type=doc['access_type'] or 'private')
        except Exception as e:
            print(f"[CONTRACTOR DELETE] Cloudinary destroy failed: {e}")
    conn.execute('DELETE FROM bb_contractors WHERE id=%s', (cid,))
    conn.commit(); conn.close()
    log_action(u['id'], 'deleted_contractor', 'contractor', cid)
    return jsonify({'ok': True})

# ── Tax ID (SSN/EIN) ────────────────────────────────────────────────────────
@app.route('/api/contractors/<int:cid>/tax-id', methods=['PUT'])
def set_contractor_tax_id(cid):
    u, err = require_contractor_access()
    if err: return err
    data = request.json or {}
    tax_id = (data.get('tax_id') or '').strip()
    if not tax_id:
        return jsonify({'error': 'Tax ID is required'}), 400
    conn = get_db()
    conn.execute('''UPDATE bb_contractors SET ein_ssn_encrypted=%s, ein_ssn_last4=%s, tax_id_type=%s,
        updated_at=to_char(now(),'YYYY-MM-DD HH24:MI:SS') WHERE id=%s''',
        (encrypt_value(tax_id), last4(tax_id), data.get('tax_id_type', 'ssn'), cid))
    conn.commit(); conn.close()
    log_action(u['id'], 'updated_contractor_tax_id', 'contractor', cid)
    return jsonify({'ok': True})

@app.route('/api/contractors/<int:cid>/tax-id/reveal', methods=['POST'])
def reveal_contractor_tax_id(cid):
    u, err = require_contractor_access()
    if err: return err
    data = request.json or {}
    if not verify_password(u, data.get('password', '')):
        log_action(u['id'], 'failed_reveal_attempt', 'contractor', cid, 'tax_id')
        return jsonify({'error': 'Incorrect password'}), 403
    conn = get_db()
    ct = conn.execute('SELECT ein_ssn_encrypted, tax_id_type FROM bb_contractors WHERE id=%s', (cid,)).fetchone()
    conn.close()
    if not ct or not ct['ein_ssn_encrypted']:
        return jsonify({'error': 'No tax ID on file'}), 404
    log_action(u['id'], 'revealed_contractor_tax_id', 'contractor', cid)
    return jsonify({'tax_id': decrypt_value(ct['ein_ssn_encrypted']), 'tax_id_type': ct['tax_id_type']})

# ── Documents (W9s, signed agreements, etc.) — stored privately in Cloudinary ──
@app.route('/api/contractors/<int:cid>/documents', methods=['POST'])
def upload_contractor_document(cid):
    u, err = require_contractor_access()
    if err: return err
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400
    file = request.files['file']
    doc_type = request.form.get('doc_type', 'other')
    if not cloudinary.config().cloud_name:
        return jsonify({'error': 'Cloudinary not configured.'}), 500
    try:
        result = cloudinary.uploader.upload(
            file,
            folder=f'bloombooks/contractors/{cid}',
            resource_type='auto',
            type='private',
            use_filename=True,
            unique_filename=True
        )
        conn = get_db()
        conn.execute('''INSERT INTO bb_contractor_documents
            (contractor_id, doc_type, filename, cloud_public_id, resource_type, access_type, cloud_version, format, effective_date, expires_at, uploaded_by)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
            (cid, doc_type, file.filename, result['public_id'], result.get('resource_type', 'raw'), 'private',
             result.get('version'), result.get('format'), request.form.get('effective_date') or None,
             request.form.get('expires_at') or None, u['id']))
        conn.commit(); conn.close()
        log_action(u['id'], 'uploaded_contractor_document', 'contractor', cid, f"doc_type={doc_type}")
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/contractors/<int:cid>/documents/<int:did>/download', methods=['GET'])
def download_contractor_document(cid, did):
    u, err = require_contractor_access()
    if err: return err
    conn = get_db()
    doc = conn.execute('''SELECT d.*, c.name AS contractor_name FROM bb_contractor_documents d
                          JOIN bb_contractors c ON c.id = d.contractor_id
                          WHERE d.id=%s AND d.contractor_id=%s''', (did, cid)).fetchone()
    conn.close()
    if not doc:
        return jsonify({'error': 'Not found'}), 404
    try:
        download_name = build_download_filename(doc['contractor_name'], doc['doc_type'], doc['filename'])
        url = signed_doc_url(doc['cloud_public_id'], doc['resource_type'], doc['format'],
                              download_name=download_name, access_type=doc['access_type'] or 'private',
                              version=doc['cloud_version'])
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    log_action(u['id'], 'downloaded_contractor_document', 'contractor', cid, f"doc_type={doc['doc_type']}")
    return jsonify({'url': url})

@app.route('/api/contractors/<int:cid>/documents/<int:did>', methods=['DELETE'])
def delete_contractor_document(cid, did):
    u, err = require_contractor_access()
    if err: return err
    conn = get_db()
    doc = conn.execute('SELECT * FROM bb_contractor_documents WHERE id=%s AND contractor_id=%s', (did, cid)).fetchone()
    if not doc:
        conn.close(); return jsonify({'error': 'Not found'}), 404
    try:
        cloudinary.uploader.destroy(doc['cloud_public_id'], resource_type=doc['resource_type'] or 'raw', type=doc['access_type'] or 'private')
    except Exception as e:
        print(f"[CONTRACTOR DOC DELETE] Cloudinary destroy failed: {e}")
    try:
        # A signed document is linked back to the signing request that produced it —
        # clear that link first so deleting the document doesn't hit a foreign-key error.
        conn.execute('UPDATE bb_signing_requests SET final_document_id=NULL WHERE final_document_id=%s', (did,))
        conn.execute('DELETE FROM bb_contractor_documents WHERE id=%s', (did,))
        conn.commit()
    except Exception as e:
        conn.close()
        return jsonify({'error': f'Could not delete document: {e}'}), 500
    conn.close()
    log_action(u['id'], 'deleted_contractor_document', 'contractor', cid, f"doc_type={doc['doc_type']}")
    return jsonify({'ok': True})

# ── Bank accounts (ACH/routing/account numbers — encrypted at rest) ────────────
@app.route('/api/contractors/<int:cid>/bank-accounts', methods=['POST'])
def add_contractor_bank_account(cid):
    u, err = require_contractor_access()
    if err: return err
    data = request.json or {}
    routing = (data.get('routing_number') or '').strip()
    account = (data.get('account_number') or '').strip()
    if not account:
        return jsonify({'error': 'Account number is required'}), 400
    conn = get_db()
    if data.get('is_primary'):
        conn.execute('UPDATE bb_contractor_bank_accounts SET is_primary=0 WHERE contractor_id=%s', (cid,))
    conn.execute('''INSERT INTO bb_contractor_bank_accounts
        (contractor_id, nickname, account_holder_name, account_type,
         routing_number_encrypted, routing_last4, account_number_encrypted, account_last4, is_primary, created_by)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
        (cid, data.get('nickname'), data.get('account_holder_name'), data.get('account_type', 'checking'),
         encrypt_value(routing), last4(routing), encrypt_value(account), last4(account),
         1 if data.get('is_primary') else 0, u['id']))
    conn.commit(); conn.close()
    log_action(u['id'], 'added_contractor_bank_account', 'contractor', cid, f"account ending {last4(account)}")
    return jsonify({'ok': True})

@app.route('/api/contractors/<int:cid>/bank-accounts/<int:bid>/reveal', methods=['POST'])
def reveal_contractor_bank_account(cid, bid):
    u, err = require_contractor_access()
    if err: return err
    data = request.json or {}
    if not verify_password(u, data.get('password', '')):
        log_action(u['id'], 'failed_reveal_attempt', 'contractor', cid, f"bank_account_id={bid}")
        return jsonify({'error': 'Incorrect password'}), 403
    conn = get_db()
    acct = conn.execute('SELECT * FROM bb_contractor_bank_accounts WHERE id=%s AND contractor_id=%s', (bid, cid)).fetchone()
    conn.close()
    if not acct:
        return jsonify({'error': 'Not found'}), 404
    log_action(u['id'], 'revealed_contractor_bank_account', 'contractor', cid, f"bank_account_id={bid}")
    return jsonify({
        'routing_number': decrypt_value(acct['routing_number_encrypted']),
        'account_number': decrypt_value(acct['account_number_encrypted']),
        'account_holder_name': acct['account_holder_name']
    })

@app.route('/api/contractors/<int:cid>/bank-accounts/<int:bid>', methods=['DELETE'])
def delete_contractor_bank_account(cid, bid):
    u, err = require_contractor_access()
    if err: return err
    conn = get_db()
    try:
        # Clear any payment records' reference to this bank account first, so the
        # delete doesn't hit a foreign-key error — the payment itself is untouched.
        conn.execute('UPDATE bb_contractor_payments SET bank_account_id=NULL WHERE bank_account_id=%s', (bid,))
        conn.execute('DELETE FROM bb_contractor_bank_accounts WHERE id=%s AND contractor_id=%s', (bid, cid))
        conn.commit()
    except Exception as e:
        conn.close()
        return jsonify({'error': f'Could not delete bank account: {e}'}), 500
    conn.close()
    log_action(u['id'], 'deleted_contractor_bank_account', 'contractor', cid, f"bank_account_id={bid}")
    return jsonify({'ok': True})

# ── Payments ─────────────────────────────────────────────────────────────────

@app.route('/api/contractors/<int:cid>/payable-events', methods=['GET'])
def get_contractor_payable_events(cid):
    """Pulls the RoleCall volunteer linked to this contractor and shows their
    paid-instruction programs, broken down by individual event/session —
    whether time's actually been logged for each one, and whether it's
    already been covered by a previous payment. RoleCall shares this same
    database, so this queries its tables directly."""
    u, err = require_contractor_access()
    if err: return err
    conn = get_db()
    vol = conn.execute('SELECT id, name FROM volunteers WHERE bb_contractor_id=%s', (cid,)).fetchone()
    if not vol:
        conn.close()
        return jsonify({'linked': False, 'programs': []})
    programs = conn.execute('''SELECT id, name, instructor_expected_pay FROM youth_programs
        WHERE instructor_id=%s AND is_paid_instruction=TRUE ORDER BY start_date DESC NULLS LAST''',
        (vol['id'],)).fetchall()
    result_programs = []
    for p in programs:
        events = conn.execute('''SELECT id, name, event_date FROM events
            WHERE program_id=%s ORDER BY event_date''', (p['id'],)).fetchall()
        event_list = []
        for e in events:
            logged = conn.execute('''SELECT COALESCE(SUM(hours),0) as t FROM hours
                WHERE event_id=%s AND volunteer_id=%s AND pay_type='paid_instruction' ''',
                (e['id'], vol['id'])).fetchone()
            logged_hours = float(logged['t']) if logged else 0.0
            paid_row = conn.execute('''SELECT cp.amount FROM bb_contractor_payment_events cpe
                JOIN bb_contractor_payments cp ON cp.id = cpe.payment_id
                WHERE cpe.rolecall_event_id=%s AND cp.status != 'void' LIMIT 1''', (e['id'],)).fetchone()
            event_list.append({
                'id': e['id'], 'name': e['name'], 'event_date': e['event_date'],
                'logged_hours': round(logged_hours, 2),
                'has_logged_time': logged_hours > 0,
                'already_paid': bool(paid_row),
            })
        paid_event_count = sum(1 for e in event_list if e['already_paid'])
        result_programs.append({
            'program_id': p['id'], 'program_name': p['name'],
            'expected_pay': float(p['instructor_expected_pay'] or 0),
            'events': event_list,
            'total_events': len(event_list),
            'paid_event_count': paid_event_count,
            'fully_paid': len(event_list) > 0 and paid_event_count == len(event_list),
        })
    conn.close()
    return jsonify({'linked': True, 'volunteer_name': vol['name'], 'programs': result_programs})

@app.route('/api/contractors/<int:cid>/payments', methods=['POST'])
def add_contractor_payment(cid):
    u, err = require_contractor_access()
    if err: return err
    data = request.json or {}
    try:
        amount = float(data.get('amount'))
    except (TypeError, ValueError):
        amount = 0
    method = (data.get('method') or '').strip()
    payment_date = (data.get('payment_date') or '').strip()
    event_ids = data.get('event_ids') or []
    if amount <= 0 or not method or not payment_date:
        return jsonify({'error': 'Amount, method, and payment date are required'}), 400
    conn = get_db()
    c = conn.execute('''INSERT INTO bb_contractor_payments
        (contractor_id, amount, method, bank_account_id, payment_date, reference_number, budget_id, request_id, memo, paid_by)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id''',
        (cid, amount, method, data.get('bank_account_id') or None, payment_date, data.get('reference_number'),
         data.get('budget_id') or None, data.get('request_id') or None, data.get('memo'), u['id']))
    pid = c.fetchone()['id']
    for eid in event_ids:
        if eid:
            conn.execute('INSERT INTO bb_contractor_payment_events (payment_id, rolecall_event_id) VALUES (%s,%s)', (pid, eid))
    conn.commit(); conn.close()
    log_action(u['id'], 'recorded_contractor_payment', 'contractor', cid, f"payment_id={pid} amount={amount} method={method}")
    return jsonify({'ok': True, 'id': pid})

@app.route('/api/contractors/payments/<int:pid>/void', methods=['POST'])
def void_contractor_payment(pid):
    u, err = require_contractor_access()
    if err: return err
    data = request.json or {}
    conn = get_db()
    pay = conn.execute('SELECT * FROM bb_contractor_payments WHERE id=%s', (pid,)).fetchone()
    if not pay:
        conn.close(); return jsonify({'error': 'Not found'}), 404
    conn.execute("UPDATE bb_contractor_payments SET status='void', void_reason=%s WHERE id=%s",
                 (data.get('reason', ''), pid))
    # Free up the events this payment covered so they can be paid again —
    # a void means the payment didn't actually happen (or was a mistake).
    conn.execute('DELETE FROM bb_contractor_payment_events WHERE payment_id=%s', (pid,))
    conn.commit(); conn.close()
    log_action(u['id'], 'voided_contractor_payment', 'contractor', pay['contractor_id'], f"payment_id={pid}")
    return jsonify({'ok': True})

# ── Access / audit trail for a contractor ───────────────────────────────────
@app.route('/api/contractors/<int:cid>/audit', methods=['GET'])
def contractor_audit(cid):
    u, err = require_contractor_access()
    if err: return err
    conn = get_db()
    rows = conn.execute('''SELECT a.*, u.name AS user_name FROM bb_audit_log a
                           LEFT JOIN bb_users u ON a.user_id = u.id
                           WHERE a.entity_type='contractor' AND a.entity_id=%s
                           ORDER BY a.created_at DESC LIMIT 200''', (cid,)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

# ─── Document templates (agreements) ───────────────────────────────────────────
@app.route('/api/document-templates', methods=['GET'])
def list_document_templates():
    u, err = require_contractor_access()
    if err: return err
    doc_type = request.args.get('doc_type', 'agreement')
    conn = get_db()
    rows = conn.execute('SELECT * FROM bb_document_templates WHERE doc_type=%s ORDER BY name', (doc_type,)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/document-templates', methods=['POST'])
def create_document_template():
    u, err = require_contractor_access()
    if err: return err
    data = request.json or {}
    name = (data.get('name') or '').strip()
    body = (data.get('body') or '').strip()
    if not name or not body:
        return jsonify({'error': 'Name and body are required'}), 400
    conn = get_db()
    try:
        c = conn.execute('INSERT INTO bb_document_templates (name, doc_type, body, created_by) VALUES (%s,%s,%s,%s) RETURNING id',
                          (name, data.get('doc_type', 'agreement'), body, u['id']))
        tid = c.fetchone()['id']
        conn.commit()
    except Exception as e:
        conn.close()
        msg = 'A template with that name already exists' if 'unique' in str(e).lower() else str(e)
        return jsonify({'error': msg}), 400
    conn.close()
    log_action(u['id'], 'created_document_template', 'template', tid, name)
    return jsonify({'ok': True, 'id': tid})

@app.route('/api/document-templates/<int:tid>', methods=['PUT'])
def update_document_template(tid):
    u, err = require_contractor_access()
    if err: return err
    data = request.json or {}
    name = (data.get('name') or '').strip()
    body = (data.get('body') or '').strip()
    if not name or not body:
        return jsonify({'error': 'Name and body are required'}), 400
    conn = get_db()
    conn.execute("UPDATE bb_document_templates SET name=%s, body=%s, updated_at=to_char(now(),'YYYY-MM-DD HH24:MI:SS') WHERE id=%s",
                 (name, body, tid))
    conn.commit(); conn.close()
    log_action(u['id'], 'updated_document_template', 'template', tid, name)
    return jsonify({'ok': True})

@app.route('/api/document-templates/<int:tid>', methods=['DELETE'])
def delete_document_template(tid):
    u, err = require_contractor_access()
    if err: return err
    conn = get_db()
    conn.execute('DELETE FROM bb_document_templates WHERE id=%s', (tid,))
    conn.commit(); conn.close()
    log_action(u['id'], 'deleted_document_template', 'template', tid)
    return jsonify({'ok': True})

# ─── Signing requests (admin side: create / list / void / resend) ─────────────
@app.route('/api/contractors/<int:cid>/signing-requests', methods=['GET'])
def list_signing_requests(cid):
    u, err = require_contractor_access()
    if err: return err
    conn = get_db()
    rows = conn.execute('SELECT * FROM bb_signing_requests WHERE contractor_id=%s ORDER BY created_at DESC', (cid,)).fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d.pop('body_snapshot', None)
        d['link'] = f"{APP_URL}/sign/{d['token']}"
        out.append(d)
    return jsonify(out)

@app.route('/api/contractors/<int:cid>/signing-requests', methods=['POST'])
def create_signing_request(cid):
    u, err = require_contractor_access()
    if err: return err
    data = request.json or {}
    doc_type = data.get('doc_type')
    if doc_type not in ('agreement', 'w9', 'bank_auth'):
        return jsonify({'error': 'doc_type must be agreement, w9, or bank_auth'}), 400
    conn = get_db()
    ct = conn.execute('SELECT * FROM bb_contractors WHERE id=%s', (cid,)).fetchone()
    if not ct:
        conn.close(); return jsonify({'error': 'Contractor not found'}), 404

    token = secrets.token_urlsafe(24)
    custom_fields = data.get('custom_fields') or {}
    title = None
    body_snapshot = None
    template_id = None

    if doc_type == 'agreement':
        template_id = data.get('template_id')
        tmpl = conn.execute('SELECT * FROM bb_document_templates WHERE id=%s', (template_id,)).fetchone() if template_id else None
        if not tmpl:
            conn.close(); return jsonify({'error': 'Template not found'}), 404
        merge_values = dict(custom_fields)
        merge_values.setdefault('org_name', ORG_NAME)
        merge_values.setdefault('contractor_name', ct['name'])
        merge_values.setdefault('org_rep_name', u['name'])
        merge_values['business_name_suffix'] = f" ({ct['business_name']})" if ct.get('business_name') else ''
        merge_values.setdefault('effective_date', datetime.now().strftime('%B %d, %Y'))
        body_snapshot = render_merge(tmpl['body'], merge_values)
        title = data.get('title') or tmpl['name']
    elif doc_type == 'bank_auth':
        title = 'Bank/ACH Payment Authorization'
    else:
        title = 'Form W-9'

    from datetime import timedelta
    expires_days = int(data.get('expires_days') or 14)
    expires_at = (datetime.now() + timedelta(days=expires_days)).strftime('%Y-%m-%d %H:%M:%S')

    c = conn.execute('''INSERT INTO bb_signing_requests
        (contractor_id, doc_type, template_id, title, body_snapshot, custom_fields, token, recipient_email, expires_at, created_by)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id''',
        (cid, doc_type, template_id, title, body_snapshot, json.dumps(custom_fields), token,
         data.get('recipient_email'), expires_at, u['id']))
    rid = c.fetchone()['id']
    conn.commit(); conn.close()

    link = f"{APP_URL}/sign/{token}"
    recipient = (data.get('recipient_email') or ct.get('contact_email') or '').strip()
    if recipient:
        subject = f"Please sign: {title} — {ORG_NAME}"
        body_html = (
            f'<p>Hi {ct["name"]},</p>'
            f'<p>{ORG_NAME} has sent you a document to review and sign: <strong>{title}</strong>.</p>'
            f'<p><a href="{link}" style="background:#0f6e56;color:#fff;padding:10px 18px;border-radius:6px;'
            f'text-decoration:none;display:inline-block">Review &amp; sign</a></p>'
            f'<p style="font-size:12px;color:#888">This link expires on {expires_at[:10]}. '
            f'If the button doesn\'t work, copy this link: {link}</p>'
        )
        send_email(recipient, subject, body_html)

    log_action(u['id'], 'sent_signing_request', 'contractor', cid, f"doc_type={doc_type} request_id={rid}")
    return jsonify({'ok': True, 'id': rid, 'token': token, 'link': link, 'emailed': bool(recipient)})

@app.route('/api/signing-requests/<int:rid>/void', methods=['POST'])
def void_signing_request(rid):
    u, err = require_contractor_access()
    if err: return err
    data = request.json or {}
    conn = get_db()
    sr = conn.execute('SELECT * FROM bb_signing_requests WHERE id=%s', (rid,)).fetchone()
    if not sr:
        conn.close(); return jsonify({'error': 'Not found'}), 404
    conn.execute("UPDATE bb_signing_requests SET status='void', void_reason=%s WHERE id=%s", (data.get('reason', ''), rid))
    conn.commit(); conn.close()
    log_action(u['id'], 'voided_signing_request', 'contractor', sr['contractor_id'], f"request_id={rid}")
    return jsonify({'ok': True})

@app.route('/api/signing-requests/<int:rid>/resend', methods=['POST'])
def resend_signing_request(rid):
    u, err = require_contractor_access()
    if err: return err
    data = request.json or {}
    conn = get_db()
    sr = conn.execute('SELECT * FROM bb_signing_requests WHERE id=%s', (rid,)).fetchone()
    if not sr:
        conn.close(); return jsonify({'error': 'Not found'}), 404
    ct = conn.execute('SELECT * FROM bb_contractors WHERE id=%s', (sr['contractor_id'],)).fetchone()
    conn.close()
    if sr['status'] != 'pending':
        return jsonify({'error': f"This request is {sr['status']}, not pending"}), 400
    recipient = (data.get('recipient_email') or sr.get('recipient_email') or (ct or {}).get('contact_email') or '').strip()
    if not recipient:
        return jsonify({'error': 'No recipient email on file'}), 400
    link = f"{APP_URL}/sign/{sr['token']}"
    subject = f"Reminder: please sign {sr['title']} — {ORG_NAME}"
    body_html = (
        f'<p>Hi {(ct or {}).get("name", "")},</p>'
        f'<p>Friendly reminder — {ORG_NAME} is waiting on your signature for <strong>{sr["title"]}</strong>.</p>'
        f'<p><a href="{link}" style="background:#0f6e56;color:#fff;padding:10px 18px;border-radius:6px;'
        f'text-decoration:none;display:inline-block">Review &amp; sign</a></p>'
    )
    send_email(recipient, subject, body_html)
    log_action(u['id'], 'resent_signing_request', 'contractor', sr['contractor_id'], f"request_id={rid}")
    return jsonify({'ok': True})

# ─── Public signing flow (token-based, no login) ───────────────────────────────
@app.route('/api/sign/<token>', methods=['GET'])
def get_signing_request(token):
    conn = get_db()
    sr = conn.execute('SELECT * FROM bb_signing_requests WHERE token=%s', (token,)).fetchone()
    if not sr:
        conn.close(); return jsonify({'error': 'This signing link is invalid.'}), 404
    ct = conn.execute('SELECT name, business_name FROM bb_contractors WHERE id=%s', (sr['contractor_id'],)).fetchone()
    conn.close()
    status = sr['status']
    if status == 'pending' and sr['expires_at'] and sr['expires_at'] < now_str():
        status = 'expired'
    out = {
        'status': status,
        'doc_type': sr['doc_type'],
        'title': sr['title'],
        'contractor_name': ct['name'] if ct else '',
        'org_name': ORG_NAME,
        'expires_at': sr['expires_at'],
    }
    if sr['doc_type'] == 'agreement':
        out['body'] = sr['body_snapshot']
    if status == 'signed':
        out['signed_at'] = sr['signed_at']
        out['signer_name'] = sr['signer_name']
    return jsonify(out)

@app.route('/api/sign/<token>', methods=['POST'])
def submit_signing_request(token):
    conn = get_db()
    sr = conn.execute('SELECT * FROM bb_signing_requests WHERE token=%s', (token,)).fetchone()
    if not sr:
        conn.close(); return jsonify({'error': 'This signing link is invalid.'}), 404
    if sr['status'] != 'pending':
        conn.close(); return jsonify({'error': f"This document is already {sr['status']}."}), 400
    if sr['expires_at'] and sr['expires_at'] < now_str():
        conn.close(); return jsonify({'error': 'This signing link has expired. Ask for a new one.'}), 400

    data = request.json or {}
    consent = bool(data.get('consent'))
    signer_name = (data.get('signer_name') or '').strip()
    if not consent:
        conn.close(); return jsonify({'error': 'You must consent to sign electronically.'}), 400
    if not signer_name:
        conn.close(); return jsonify({'error': 'Type your full legal name to sign.'}), 400

    ct = conn.execute('SELECT * FROM bb_contractors WHERE id=%s', (sr['contractor_id'],)).fetchone()
    ip, ua = sign_request_meta()
    ts = now_str()

    try:
        if sr['doc_type'] == 'agreement':
            pdf_bytes = build_agreement_pdf(sr['title'], sr['body_snapshot'], signer_name, ip, ua, ts, ts, token)
            doc_type_stored = 'agreement'
        elif sr['doc_type'] == 'bank_auth':
            fields = data.get('fields') or {}
            required = ['account_holder_name', 'routing_number', 'account_number', 'account_type']
            missing = [f for f in required if not str(fields.get(f, '')).strip()]
            if missing:
                conn.close(); return jsonify({'error': f"Missing required field(s): {', '.join(missing)}"}), 400
            routing = str(fields['routing_number']).strip()
            account = str(fields['account_number']).strip()

            # Save straight into the contractor's encrypted bank accounts — the same
            # storage the admin-side "add bank account" form uses. This becomes primary.
            conn.execute('UPDATE bb_contractor_bank_accounts SET is_primary=0 WHERE contractor_id=%s',
                         (sr['contractor_id'],))
            conn.execute('''INSERT INTO bb_contractor_bank_accounts
                (contractor_id, nickname, account_holder_name, account_type,
                 routing_number_encrypted, routing_last4, account_number_encrypted, account_last4, is_primary)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,1)''',
                (sr['contractor_id'], 'Added via payment authorization form', fields['account_holder_name'],
                 fields.get('account_type', 'checking'), encrypt_value(routing), last4(routing),
                 encrypt_value(account), last4(account)))

            display_fields = dict(fields)
            # Routing numbers identify a bank/branch, not the person, so showing it in full on the
            # retained PDF is normal practice. The account number is masked — the full number lives
            # only in the encrypted bb_contractor_bank_accounts row used for actually paying them.
            display_fields['routing_number_display'] = routing
            display_fields['account_number_display'] = f"{'*' * 6}{last4(account)}"
            pdf_bytes = build_bank_auth_pdf(display_fields, signer_name, ip, ua, ts, ts, token)
            doc_type_stored = 'bank_auth'
        else:
            fields = data.get('fields') or {}
            required = ['legal_name', 'tax_classification', 'address', 'city_state_zip', 'tin', 'tin_type']
            missing = [f for f in required if not str(fields.get(f, '')).strip()]
            if missing:
                conn.close(); return jsonify({'error': f"Missing required field(s): {', '.join(missing)}"}), 400
            tin = str(fields['tin']).strip()
            tin_type = fields['tin_type']
            # The stored PDF is the legal record of a completed W-9 (same as a paper form would be),
            # so it shows the real TIN — it's a private Cloudinary asset, downloadable only by
            # admin/treasurer/president through a short-lived signed URL.
            tin_display = tin
            pdf_bytes = build_w9_pdf(fields, tin_type, tin_display, signer_name, ip, ua, ts, ts, token)
            doc_type_stored = 'w9'

            # Populate the contractor's secure profile fields from the submitted W-9
            conn.execute('''UPDATE bb_contractors SET
                business_name=COALESCE(NULLIF(%s,''), business_name),
                address=%s, tax_classification=%s,
                ein_ssn_encrypted=%s, ein_ssn_last4=%s, tax_id_type=%s,
                updated_at=to_char(now(),'YYYY-MM-DD HH24:MI:SS')
                WHERE id=%s''',
                (fields.get('business_name', ''), f"{fields.get('address','')}, {fields.get('city_state_zip','')}",
                 fields.get('tax_classification'), encrypt_value(tin), last4(tin), tin_type, sr['contractor_id']))
            custom_fields_safe = {k: v for k, v in fields.items() if k not in ('tin',)}
            conn.execute("UPDATE bb_signing_requests SET custom_fields=%s WHERE id=%s",
                         (json.dumps(custom_fields_safe), sr['id']))

        result = upload_pdf_private(pdf_bytes, folder=f'bloombooks/contractors/{sr["contractor_id"]}')
        doc_c = conn.execute('''INSERT INTO bb_contractor_documents
            (contractor_id, doc_type, filename, cloud_public_id, resource_type, access_type, cloud_version, format, effective_date, uploaded_by)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id''',
            (sr['contractor_id'], doc_type_stored, f"{sr['title']}.pdf", result['public_id'],
             result.get('resource_type', 'raw'), 'private', result.get('version'),
             result.get('format', 'pdf'), ts[:10], None))
        doc_id = doc_c.fetchone()['id']

        conn.execute('''UPDATE bb_signing_requests SET status='signed', signed_at=%s, signer_name=%s,
            signer_ip=%s, signer_user_agent=%s, consent_at=%s, final_document_id=%s WHERE id=%s''',
            (ts, signer_name, ip, ua, ts, doc_id, sr['id']))
        conn.commit()
    except Exception as e:
        conn.close()
        return jsonify({'error': f'Could not complete signing: {e}'}), 500

    conn.close()
    log_action(None, 'contractor_signed_document', 'contractor', sr['contractor_id'],
               f"doc_type={sr['doc_type']} request_id={sr['id']} signer={signer_name}")
    return jsonify({'ok': True})

# ─── Static ───────────────────────────────────────────────────────────────────
@app.route('/', defaults={'path': ''})
@app.route('/<path:path>')
def serve(path):
    if path.startswith('api/'):
        return jsonify({'error': 'Not found'}), 404
    if path and os.path.exists(os.path.join(app.static_folder, path)):
        return send_from_directory(app.static_folder, path)
    return send_from_directory(app.static_folder, 'index.html')

# ─── Global error handlers (always return JSON, never HTML) ──────────────────
@app.errorhandler(404)
def not_found(e):
    return jsonify({'error': 'Not found'}), 404

@app.errorhandler(405)
def method_not_allowed(e):
    return jsonify({'error': 'Method not allowed'}), 405

@app.errorhandler(500)
def server_error(e):
    return jsonify({'error': f'Server error: {str(e)}'}), 500

# ─── Ensure DB is initialised before every request ───────────────────────────
_db_ready = False

@app.before_request
def ensure_db():
    global _db_ready
    if not _db_ready:
        init_db()
        _db_ready = True

# ─── Start ────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    init_db()
    port = int(os.environ.get('PORT', 5001))
    print(f"\nBloomBooks is running!")
    print(f"   Open http://localhost:{port} in your browser\n")
    print("   Demo accounts:")
    print("   admin@horizonwest.org      / admin123")
    print("   treasurer@horizonwest.org  / treasurer123")
    print("   president@horizonwest.org  / president123")
    print("   volunteer@horizonwest.org  / volunteer123\n")
    app.run(host='0.0.0.0', port=port, debug=False)
