from flask import Flask, request, jsonify, session, send_from_directory
from flask_cors import CORS
import psycopg2
import psycopg2.extras
import hashlib
import os
import json
from datetime import datetime
import cloudinary
import cloudinary.uploader
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

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
EMAIL_HOST = os.environ.get('EMAIL_HOST', 'smtp.gmail.com')
EMAIL_PORT = int(os.environ.get('EMAIL_PORT', 587))
EMAIL_USER = os.environ.get('EMAIL_USER', '')
EMAIL_PASS = os.environ.get('EMAIL_PASS', '')
APP_URL    = os.environ.get('APP_URL', 'http://localhost:5001')

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
    if not EMAIL_USER or not EMAIL_PASS:
        print(f"[EMAIL SKIPPED - no creds] To: {to} | Subject: {subject}")
        return
    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From'] = f"BloomBooks — HWTC <{EMAIL_USER}>"
        msg['To'] = to
        msg.attach(MIMEText(body_html, 'html'))
        with smtplib.SMTP(EMAIL_HOST, EMAIL_PORT) as server:
            server.starttls()
            server.login(EMAIL_USER, EMAIL_PASS)
            server.send_message(msg)
    except Exception as e:
        print(f"[EMAIL ERROR] {e}")

def get_user_email(user_id):
    conn = get_db()
    u = conn.execute('SELECT email, name FROM bb_users WHERE id=?', (user_id,)).fetchone()
    conn.close()
    return dict(u) if u else None

def get_role_emails(role):
    conn = get_db()
    users = conn.execute('SELECT email, name FROM bb_users WHERE role=?', (role,)).fetchall()
    conn.close()
    return [dict(u) for u in users]

def email_html(title, body, cta_text=None, cta_url=None):
    cta = f'<a href="{cta_url}" style="display:inline-block;margin-top:16px;padding:10px 20px;background:#0f6e56;color:#fff;text-decoration:none;border-radius:6px;font-size:14px">{cta_text}</a>' if cta_text else ''
    return f'''
    <div style="font-family:sans-serif;max-width:520px;margin:0 auto;padding:24px">
      <div style="background:#0f6e56;color:#fff;padding:16px 20px;border-radius:8px 8px 0 0">
        <p style="margin:0;font-size:12px;opacity:.7">Horizon West Theater Company</p>
        <h2 style="margin:4px 0 0;font-size:18px">{title}</h2>
      </div>
      <div style="background:#f9f9f7;border:1px solid #e0ddd6;border-top:none;padding:20px;border-radius:0 0 8px 8px">
        {body}
        {cta}
      </div>
    </div>'''

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

@app.route('/api/users/<int:uid>', methods=['PATCH'])
def update_user(uid):
    err = require_auth(['admin'])
    if err: return err
    data = request.json
    conn = get_db()
    if 'role' in data:
        conn.execute('UPDATE bb_users SET role=%s WHERE id=?', (data['role'], uid))
    if 'training_complete' in data:
        conn.execute('UPDATE bb_users SET training_complete=%s WHERE id=?', (data['training_complete'], uid))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

# ─── Budgets ─────────────────────────────────────────────────────────────────
@app.route('/api/budgets', methods=['GET'])
def list_budgets():
    err = require_auth()
    if err: return err
    conn = get_db()
    budgets = conn.execute('SELECT * FROM bb_budgets ORDER BY is_active DESC, name').fetchall()
    conn.close()
    return jsonify([dict(b) for b in budgets])

@app.route('/api/budgets', methods=['POST'])
def create_budget():
    err = require_auth(['admin', 'treasurer', 'president'])
    if err: return err
    data = request.json
    conn = get_db()
    conn.execute('INSERT INTO bb_budgets (name,area,season,total_amount) VALUES (%s,%s,%s,%s)',
                 (data['name'], data['area'], data['season'], float(data['total_amount'])))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

@app.route('/api/budgets/<int:bid>', methods=['PATCH'])
def update_budget(bid):
    err = require_auth(['admin', 'treasurer', 'president'])
    if err: return err
    data = request.json
    conn = get_db()
    fields = []
    vals = []
    for f in ['name', 'area', 'season', 'total_amount', 'is_active']:
        if f in data:
            fields.append(f'{f}=?')
            vals.append(data[f])
    if fields:
        vals.append(bid)
        conn.execute(f'UPDATE bb_budgets SET {", ".join(fields)} WHERE id=?', vals)
        conn.commit()
    conn.close()
    return jsonify({'ok': True})

# ─── Purchase Requests ────────────────────────────────────────────────────────
@app.route('/api/requests', methods=['GET'])
def list_requests():
    err = require_auth()
    if err: return err
    u = current_user()
    conn = get_db()

    base = '''SELECT r.*, 
                u.name as submitter_name, u.email as submitter_email,
                b.name as budget_name, b.area as budget_area,
                b.total_amount as budget_total, b.spent as budget_spent
              FROM bb_purchase_requests r
              LEFT JOIN bb_users u ON r.submitted_by = u.id
              LEFT JOIN bb_budgets b ON r.budget_id = b.id'''

    status_filter = request.args.get('status')
    my_only = request.args.get('mine') == '1'

    conditions = []
    params = []

    if u['role'] == 'volunteer' or my_only:
        conditions.append('r.submitted_by=?')
        params.append(u['id'])

    if status_filter:
        conditions.append('r.status=?')
        params.append(status_filter)

    if conditions:
        base += ' WHERE ' + ' AND '.join(conditions)

    base += ' ORDER BY r.submitted_at DESC'

    rows = conn.execute(base, params).fetchall()
    result = []
    for row in rows:
        r = dict(row)
        # attach receipts
        receipts = conn.execute('SELECT * FROM bb_receipts WHERE request_id=?', (r['id'],)).fetchall()
        r['receipts'] = [dict(rec) for rec in receipts]
        result.append(r)
    conn.close()
    return jsonify(result)

@app.route('/api/requests', methods=['POST'])
def create_request():
    err = require_auth()
    if err: return err
    u = current_user()

    # enforce training gate
    if not u['training_complete'] and u['role'] == 'volunteer':
        return jsonify({'error': 'You must complete purchasing training before submitting requests.'}), 403

    data = request.json
    is_emergency = 1 if data.get('is_emergency') else 0
    req_type = 'emergency' if is_emergency else 'pre_approval'
    status = 'pending_treasurer'

    conn = get_db()
    cur = conn.execute(
        '''INSERT INTO bb_purchase_requests 
           (type,status,title,description,vendor,estimated_cost,budget_id,submitted_by,is_emergency,emergency_reason)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
        (req_type, status, data['title'], data.get('description',''),
         data.get('vendor',''), float(data['estimated_cost']),
         data['budget_id'], u['id'], is_emergency, data.get('emergency_reason',''))
    )
    req_id = cur.lastrowid
    conn.commit()
    conn.close()

    log_action(u['id'], 'submitted_request', 'request', req_id, data['title'])

    # notify treasurers
    for t in get_role_emails('treasurer'):
        send_email(t['email'], f"New purchase request: {data['title']}",
            email_html('New Purchase Request',
                f'<p><b>{u["name"]}</b> submitted a purchase request.</p>'
                f'<p><b>Item:</b> {data["title"]}<br>'
                f'<b>Amount:</b> ${float(data["estimated_cost"]):.2f}<br>'
                f'<b>Type:</b> {"Emergency" if is_emergency else "Pre-approval"}</p>',
                'Review Request', f'{APP_URL}'))
    for p in get_role_emails('president'):
        send_email(p['email'], f"New purchase request submitted: {data['title']}",
            email_html('FYI: New Purchase Request',
                f'<p>A new request has been submitted and is awaiting treasurer review.</p>'
                f'<p><b>{u["name"]}</b> — {data["title"]} — ${float(data["estimated_cost"]):.2f}</p>'))

    return jsonify({'ok': True, 'id': req_id})

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
            # notify president
            for p in get_role_emails('president'):
                send_email(p['email'], f"Awaiting your approval: {req['title']}",
                    email_html('Purchase Request Needs Your Approval',
                        f'<p>The treasurer has approved a request and it now needs your sign-off.</p>'
                        f'<p><b>Item:</b> {req["title"]}<br><b>Amount:</b> ${req["estimated_cost"]:.2f}</p>'
                        + (f'<p><b>Treasurer note:</b> {note}</p>' if note else ''),
                        'Review & Approve', f'{APP_URL}'))
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

    # notify submitter
    submitter = get_user_email(req['submitted_by'])
    if submitter:
        if new_status == 'approved':
            send_email(submitter['email'], f'✓ Purchase approved: {req["title"]}',
                email_html('Your Purchase Request Was Approved!',
                    f'<p>Great news! Your request has been fully approved.</p>'
                    f'<p><b>Item:</b> {req["title"]}<br><b>Amount:</b> ${req["estimated_cost"]:.2f}</p>'
                    + (f'<p><b>Note:</b> {note}</p>' if note else '') +
                    '<p>You will be reimbursed once the treasurer processes payment.</p>'))
        elif new_status == 'denied':
            send_email(submitter['email'], f'Request not approved: {req["title"]}',
                email_html('Purchase Request Update',
                    f'<p>Your request was not approved at this time.</p>'
                    f'<p><b>Item:</b> {req["title"]}</p>'
                    + (f'<p><b>Reason:</b> {note}</p>' if note else '') +
                    '<p>Please reach out to the treasurer if you have questions.</p>'))
        elif new_status == 'pending_president':
            send_email(submitter['email'], f'Request update: {req["title"]}',
                email_html('Treasurer Approved — Awaiting President',
                    f'<p>The treasurer has approved your request. It is now awaiting the president\'s final sign-off.</p>'
                    f'<p><b>Item:</b> {req["title"]}<br><b>Amount:</b> ${req["estimated_cost"]:.2f}</p>'))

    return jsonify({'ok': True, 'new_status': new_status})

# ─── Receipts ─────────────────────────────────────────────────────────────────
@app.route('/api/requests/<int:rid>/receipts', methods=['POST'])
def upload_receipt(rid):
    err = require_auth()
    if err: return err
    u = current_user()

    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400

    file = request.files['file']
    if not cloudinary.config().cloud_name:
        return jsonify({'error': 'Cloudinary not configured. Please set CLOUDINARY_CLOUD_NAME, CLOUDINARY_API_KEY, and CLOUDINARY_API_SECRET.'}), 500

    try:
        result = cloudinary.uploader.upload(
            file,
            folder='bloombooks/receipts',
            resource_type='auto'
        )
        image_url = result['secure_url']
        public_id = result['public_id']

        conn = get_db()
        conn.execute('INSERT INTO bb_receipts (request_id,image_url,public_id) VALUES (%s,%s,%s)',
                     (rid, image_url, public_id))
        conn.commit()
        conn.close()

        log_action(u['id'], 'uploaded_receipt', 'request', rid)
        return jsonify({'ok': True, 'image_url': image_url})
    except Exception as e:
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
    if user_info:
        send_email(user_info['email'], 'Reimbursement processed',
            email_html('You Have Been Reimbursed',
                f'<p>Your reimbursement of <b>${rb["amount"]:.2f}</b> has been processed.</p>'
                + (f'<p><b>Method:</b> {data.get("method","")}</p>' if data.get('method') else '') +
                '<p>Thank you for your contribution to Horizon West Theater Company!</p>'))

    conn.close()
    log_action(u['id'], 'marked_paid', 'reimbursement', rid)
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
