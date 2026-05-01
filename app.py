from flask import Flask, request, jsonify, session, send_from_directory
from flask_cors import CORS
import psycopg2
import psycopg2.extras
import hashlib
import os
import json
import secrets
from datetime import datetime
import cloudinary
import cloudinary.uploader
import requests as req_lib

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

# ─── Email config ─────────────────────────────────────────────────────────────
RESEND_API_KEY = os.environ.get('RESEND_API_KEY', '')
FROM_EMAIL     = os.environ.get('FROM_EMAIL', 'BloomBooks <info@hwtco.org>')
APP_URL        = os.environ.get('APP_URL', 'http://localhost:5001')

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
        ("bb_users",             "is_active",         "INTEGER DEFAULT 1"),
        ("bb_users",             "receipt_token",     "TEXT"),
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
    method_label = '🌐 Online' if purchase_method == 'online' else '🏪 In-store'
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
            send_email(p['email'], f'🎭 Purchase request needs your approval: {req_title}',
                email_html('New Purchase Request — Producer Review Needed', body,
                           'Review in BloomBooks', APP_URL))
        if req_type == 'sap':
            sap_note = '<p style="color:#c97c10;font-size:13px">This SAP still requires your approval after producer review.</p>'
            for a in get_admin_emails():
                send_email(a['email'], f'📋 SAP submitted (FYI): {req_title}',
                    email_html('SAP Submitted — FYI', body + sap_note, 'View in BloomBooks', APP_URL))
    else:
        prefix = '📋 SAP' if req_type == 'sap' else '📄 New request'
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
    send_email(submitter_email, f'✅ Request received: {req_title}',
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
            send_email(a['email'], f'📄 Awaiting treasurer review: {req_title}',
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
            send_email(a['email'], f'📄 Final sign-off needed: {req_title}',
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
            send_email(submitter['email'], f'🎉 Approved — go buy it! {req_title}',
                email_html('Purchase Approved! ✓', approved_body, 'View in BloomBooks', APP_URL))

    elif new_status == 'denied':
        if submitter:
            denied_body = (
                f'<p>Your request for <strong>{req_title}</strong> was not approved at this time.</p>'
                + (f'<p><strong>Reason:</strong> {note}</p>' if note else '')
                + '<p style="font-size:13px;color:#666">Please reach out to the treasurer or producer with any questions.</p>'
            )
            send_email(submitter['email'], f'❌ Request not approved: {req_title}',
                email_html('Purchase Request — Not Approved', denied_body))
        denied_admin_body = f'<p><strong>{acted_by_name}</strong> denied the request for <strong>{req_title}</strong>.</p>{note_html}'
        for a in get_admin_emails():
            send_email(a['email'], f'❌ Request denied: {req_title}',
                email_html('Request Denied', denied_admin_body))
        if production_id:
            for p in get_production_producers(production_id):
                send_email(p['email'], f'❌ Request denied: {req_title}',
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
        f'<p style="font-size:13px;color:#666">Thank you for your contribution to Horizon West Theater Company! 🎭</p>'
    )
    send_email(u['email'], f'💸 Reimbursement processed: ${float(amount):.2f}',
        email_html('You Have Been Reimbursed!', paid_body))


def notify_welcome(name, email, temp_password, role):
    """Welcome email to newly created user with their login details."""
    role_label = role.replace('_', ' ').title()
    welcome_body = (
        f'<p>An account has been created for you in BloomBooks, the purchasing and reimbursement system for Horizon West Theater Company.</p>'
        f'<table style="width:100%;border-collapse:collapse;margin:12px 0;background:#fff;border:1px solid #e0ddd6;border-radius:6px">'
        f'<tr><td style="padding:8px 12px;color:#666;border-bottom:1px solid #e0ddd6;width:100px">Email</td><td style="padding:8px 12px;font-weight:600;border-bottom:1px solid #e0ddd6">{email}</td></tr>'
        f'<tr><td style="padding:8px 12px;color:#666;border-bottom:1px solid #e0ddd6">Password</td><td style="padding:8px 12px;font-weight:600;color:#0f6e56;border-bottom:1px solid #e0ddd6">{temp_password}</td></tr>'
        f'<tr><td style="padding:8px 12px;color:#666">Role</td><td style="padding:8px 12px">{role_label}</td></tr>'
        f'</table>'
        f'<p style="font-size:13px;color:#666">Please sign in and complete your purchasing training before submitting any requests.</p>'
    )
    send_email(email, '🎭 Welcome to BloomBooks — Horizon West Theater Company',
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
    data = request.json
    email = data.get('email', '').strip().lower()
    name  = data.get('name', '').strip()
    pw    = data.get('password', '')
    if not email or not name or not pw:
        return jsonify({'error': 'All fields required'}), 400
    conn = get_db()
    try:
        conn.execute('INSERT INTO bb_users (name,email,password,role) VALUES (%s,%s,%s,%s)',
                     (name, email, hash_pw(pw), 'volunteer'))
        conn.commit()
        u = conn.execute('SELECT * FROM bb_users WHERE email=?', (email,)).fetchone()
        session['user_id'] = u['id']
        conn.close()
        return jsonify({'user': dict(u)})
    except psycopg2.IntegrityError:
        conn.close()
        return jsonify({'error': 'Email already registered'}), 409

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
    if is_admin:
        budgets = conn.execute('''SELECT b.*,p.name as production_name FROM bb_budgets b
                                  LEFT JOIN bb_productions p ON b.production_id=p.id
                                  ORDER BY b.is_active DESC,p.name,b.name''').fetchall()
    else:
        my_ids = [r['production_id'] for r in
                  conn.execute('SELECT production_id FROM bb_production_members WHERE user_id=%s',(u['id'],)).fetchall()]
        if my_ids:
            ph = ','.join(['%s']*len(my_ids))
            budgets = conn.execute(f'''SELECT b.*,p.name as production_name FROM bb_budgets b
                                       LEFT JOIN bb_productions p ON b.production_id=p.id
                                       WHERE (b.production_id IN ({ph}) OR b.production_id IS NULL)
                                       AND b.is_active=1''', my_ids).fetchall()
        else:
            budgets = conn.execute('''SELECT b.*,p.name as production_name FROM bb_budgets b
                                      LEFT JOIN bb_productions p ON b.production_id=p.id
                                      WHERE b.production_id IS NULL AND b.is_active=1''').fetchall()
    conn.close()
    return jsonify([dict(b) for b in budgets])

@app.route('/api/budgets', methods=['POST'])
def create_budget():
    u = current_user()
    if not u: return jsonify({'error':'Not authenticated'}),401
    data = request.json
    prod_id   = data.get('production_id') or None
    parent_id = data.get('parent_id') or None
    if u['role'] not in ('admin','treasurer','president','producer'):
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
    if u['role'] not in ('admin','treasurer','president'):
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
    if u['role'] not in ('admin','treasurer','president'):
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
    ok = send_email(u['email'], '🧪 BloomBooks test email',
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

    if u['role'] in ('admin', 'treasurer', 'president'):
        rows = conn.execute('''
            SELECT rb.*, u.name as user_name, u.email as user_email,
                   pr.title, pr.estimated_cost, pr.actual_cost, pr.is_emergency
            FROM bb_reimbursements rb
            JOIN bb_users u ON rb.user_id = u.id
            JOIN bb_purchase_requests pr ON rb.request_id = pr.id
            ORDER BY rb.created_at DESC
        ''').fetchall()
    else:
        rows = conn.execute('''
            SELECT rb.*, u.name as user_name, u.email as user_email,
                   pr.title, pr.estimated_cost, pr.actual_cost, pr.is_emergency
            FROM bb_reimbursements rb
            JOIN bb_users u ON rb.user_id = u.id
            JOIN bb_purchase_requests pr ON rb.request_id = pr.id
            WHERE rb.user_id=%s
            ORDER BY rb.created_at DESC
        ''', (u['id'],)).fetchall()

    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/reimbursements/<int:rid>/pay', methods=['POST'])
def mark_paid(rid):
    err = require_auth(['treasurer', 'admin', 'president'])
    if err: return err
    u = current_user()
    data = request.json
    now = datetime.now().isoformat()

    conn = get_db()
    rb = conn.execute('SELECT * FROM bb_reimbursements WHERE id=?', (rid,)).fetchone()
    if not rb:
        conn.close()
        return jsonify({'error': 'Not found'}), 404

    conn.execute('UPDATE bb_reimbursements SET status=%s,method=%s,paid_at=%s,notes=%s WHERE id=?',
                 ('paid', data.get('method',''), now, data.get('notes',''), rid))
    # update request status
    conn.execute("UPDATE bb_purchase_requests SET status='reimbursed', updated_at=%s WHERE id=?",
                 (now, rb['request_id']))
    conn.commit()

    # notify user
    user_info = get_user_email(rb['user_id'])
    conn.close()
    log_action(u['id'], 'marked_paid', 'reimbursement', rid)
    req_info = get_db().execute('SELECT title FROM bb_purchase_requests WHERE id=%s',(rb['request_id'],)).fetchone()
    req_title = req_info['title'] if req_info else 'your purchase'
    notify_reimbursement_paid(rb['user_id'], rb['amount'], data.get('method',''), req_title)
    return jsonify({'ok': True})

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
        my_pending   = conn.execute("SELECT COUNT(*) as count FROM bb_purchase_requests WHERE submitted_by=%s AND status LIKE 'pending%'", (u['id'],)).fetchone()['count']
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
    err = require_auth(['admin','treasurer','president'])
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
    err = require_auth(['admin','treasurer','president'])
    if err: return err
    token = ensure_receipt_token(uid)
    return jsonify({'token': token, 'link': f"{APP_URL}/receipt/{token}"})

@app.route('/api/users/<int:uid>/receipt-token/regenerate', methods=['POST'])
def regenerate_receipt_token(uid):
    err = require_auth(['admin','treasurer','president'])
    if err: return err
    token = secrets.token_urlsafe(24)
    conn = get_db()
    conn.execute('UPDATE bb_users SET receipt_token=%s WHERE id=%s',(token, uid))
    conn.commit(); conn.close()
    return jsonify({'token': token, 'link': f"{APP_URL}/receipt/{token}"})

@app.route('/api/receipt/<token>', methods=['GET'])
def get_receipt_page_data(token):
    conn = get_db()
    u = conn.execute('SELECT id,name,email,training_complete FROM bb_users WHERE receipt_token=%s AND is_active=1',(token,)).fetchone()
    if not u:
        conn.close()
        return jsonify({'error': 'Invalid or expired link'}), 404
    u = dict(u); uid = u['id']
    reqs = conn.execute('''SELECT id,title,estimated_cost,actual_cost,status,type,vendor,submitted_at
                            FROM bb_purchase_requests
                            WHERE submitted_by=%s AND status NOT IN ('denied','reimbursed')
                            ORDER BY submitted_at DESC''', (uid,)).fetchall()
    my_prod_ids = [r['production_id'] for r in
                   conn.execute('SELECT production_id FROM bb_production_members WHERE user_id=%s',(uid,)).fetchall()]
    if my_prod_ids:
        ph = ','.join(['%s']*len(my_prod_ids))
        budgets = conn.execute(f'''SELECT b.id,b.name,b.area,b.total_amount,b.spent,b.production_id,
                                          p.name as production_name
                                   FROM bb_budgets b LEFT JOIN bb_productions p ON b.production_id=p.id
                                   WHERE (b.production_id IN ({ph}) OR b.production_id IS NULL)
                                   AND b.is_active=1 ORDER BY p.name,b.name''', my_prod_ids).fetchall()
        productions = conn.execute(f"SELECT id,name,season FROM bb_productions WHERE id IN ({ph}) AND status='active'", my_prod_ids).fetchall()
    else:
        budgets = conn.execute('''SELECT b.id,b.name,b.area,b.total_amount,b.spent,b.production_id,
                                         p.name as production_name
                                  FROM bb_budgets b LEFT JOIN bb_productions p ON b.production_id=p.id
                                  WHERE b.production_id IS NULL AND b.is_active=1 ORDER BY b.name''').fetchall()
        productions = []
    conn.close()
    return jsonify({'user':u,'requests':[dict(r) for r in reqs],
                    'budgets':[dict(b) for b in budgets],'productions':[dict(p) for p in productions]})

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
    u = conn.execute('SELECT id,name,training_complete FROM bb_users WHERE receipt_token=%s AND is_active=1',(token,)).fetchone()
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
    print(f"\n🎭 BloomBooks is running!")
    print(f"   Open http://localhost:{port} in your browser\n")
    print("   Demo accounts:")
    print("   admin@horizonwest.org      / admin123")
    print("   treasurer@horizonwest.org  / treasurer123")
    print("   president@horizonwest.org  / president123")
    print("   volunteer@horizonwest.org  / volunteer123\n")
    app.run(host='0.0.0.0', port=port, debug=False)
