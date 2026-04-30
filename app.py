from flask import Flask, request, jsonify, session, send_from_directory
from flask_cors import CORS
import psycopg2
import psycopg2.extras
import hashlib
import os
import json
from datetime import datetime
import secrets
import cloudinary
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

app = Flask(__name__, static_folder='static')
app.secret_key = os.environ.get('SECRET_KEY', 'bloombooks-dev-key')
CORS(app, supports_credentials=True)
DATABASE_URL = os.environ.get('DATABASE_URL', '')
# Railway sometimes provides postgres:// — psycopg2 requires postgresql://
if DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)

cloudinary.config(
    cloud_name=os.environ.get('CLOUDINARY_CLOUD_NAME', ''),
    api_key=os.environ.get('CLOUDINARY_API_KEY', ''),
    api_secret=os.environ.get('CLOUDINARY_API_SECRET', '')
)

EMAIL_HOST = os.environ.get('EMAIL_HOST', 'smtp.gmail.com')
EMAIL_PORT = int(os.environ.get('EMAIL_PORT', 587))
EMAIL_USER = os.environ.get('EMAIL_USER', '')
EMAIL_PASS = os.environ.get('EMAIL_PASS', '')
APP_URL    = os.environ.get('APP_URL', 'http://localhost:5001')

class DBWrapper:
    def __init__(self, conn):
        self._conn = conn
    def execute(self, sql, params=None):
        sql = sql.replace('?', '%s')
        c = self._conn.cursor()
        c.execute(sql, params or ())
        return c
    def commit(self):  self._conn.commit()
    def close(self):   self._conn.close()
    def cursor(self):  return self._conn.cursor()

def get_db():
    if not DATABASE_URL:
        raise RuntimeError('DATABASE_URL environment variable is not set.')
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    return DBWrapper(conn)

def init_db():
    conn = get_db()
    c = conn.cursor()

    c.execute('''CREATE TABLE IF NOT EXISTS bb_users (
        id SERIAL PRIMARY KEY, name TEXT NOT NULL, email TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'volunteer',
        training_complete INTEGER DEFAULT 0,
        created_at TEXT DEFAULT (to_char(now(),'YYYY-MM-DD HH24:MI:SS')))''')

    c.execute('''CREATE TABLE IF NOT EXISTS bb_productions (
        id SERIAL PRIMARY KEY, name TEXT NOT NULL, season TEXT NOT NULL,
        description TEXT, total_budget REAL DEFAULT 0, status TEXT DEFAULT 'active',
        created_at TEXT DEFAULT (to_char(now(),'YYYY-MM-DD HH24:MI:SS')))''')

    c.execute('''CREATE TABLE IF NOT EXISTS bb_production_members (
        id SERIAL PRIMARY KEY, production_id INTEGER REFERENCES bb_productions(id) ON DELETE CASCADE,
        user_id INTEGER REFERENCES bb_users(id) ON DELETE CASCADE,
        member_role TEXT NOT NULL DEFAULT 'member',
        created_at TEXT DEFAULT (to_char(now(),'YYYY-MM-DD HH24:MI:SS')),
        UNIQUE(production_id, user_id))''')

    c.execute('''CREATE TABLE IF NOT EXISTS bb_budgets (
        id SERIAL PRIMARY KEY, name TEXT NOT NULL, area TEXT NOT NULL,
        season TEXT NOT NULL, total_amount REAL NOT NULL, spent REAL DEFAULT 0,
        is_active INTEGER DEFAULT 1, production_id INTEGER REFERENCES bb_productions(id),
        created_at TEXT DEFAULT (to_char(now(),'YYYY-MM-DD HH24:MI:SS')))''')

    c.execute('''CREATE TABLE IF NOT EXISTS bb_purchase_requests (
        id SERIAL PRIMARY KEY, type TEXT NOT NULL DEFAULT 'pre_approval',
        status TEXT NOT NULL DEFAULT 'pending_treasurer', title TEXT NOT NULL,
        description TEXT, vendor TEXT, estimated_cost REAL NOT NULL, actual_cost REAL,
        budget_id INTEGER REFERENCES bb_budgets(id),
        production_id INTEGER REFERENCES bb_productions(id),
        submitted_by INTEGER REFERENCES bb_users(id),
        is_emergency INTEGER DEFAULT 0, emergency_reason TEXT,
        producer_note TEXT, treasurer_note TEXT, president_note TEXT,
        producer_acted_by INTEGER REFERENCES bb_users(id),
        treasurer_acted_by INTEGER REFERENCES bb_users(id),
        president_acted_by INTEGER REFERENCES bb_users(id),
        producer_acted_at TEXT, treasurer_acted_at TEXT, president_acted_at TEXT,
        submitted_at TEXT DEFAULT (to_char(now(),'YYYY-MM-DD HH24:MI:SS')),
        updated_at TEXT DEFAULT (to_char(now(),'YYYY-MM-DD HH24:MI:SS')))''')

    c.execute('''CREATE TABLE IF NOT EXISTS bb_receipts (
        id SERIAL PRIMARY KEY, request_id INTEGER REFERENCES bb_purchase_requests(id),
        image_url TEXT NOT NULL, public_id TEXT,
        uploaded_at TEXT DEFAULT (to_char(now(),'YYYY-MM-DD HH24:MI:SS')))''')

    c.execute('''CREATE TABLE IF NOT EXISTS bb_reimbursements (
        id SERIAL PRIMARY KEY, request_id INTEGER UNIQUE REFERENCES bb_purchase_requests(id),
        user_id INTEGER REFERENCES bb_users(id), amount REAL NOT NULL,
        status TEXT DEFAULT 'pending', method TEXT, paid_at TEXT, notes TEXT,
        created_at TEXT DEFAULT (to_char(now(),'YYYY-MM-DD HH24:MI:SS')))''')

    c.execute('''CREATE TABLE IF NOT EXISTS bb_training_modules (
        id SERIAL PRIMARY KEY, title TEXT NOT NULL, description TEXT,
        slides TEXT DEFAULT '[]', questions TEXT DEFAULT '[]',
        pass_mark INTEGER DEFAULT 80, is_active INTEGER DEFAULT 1,
        created_at TEXT DEFAULT (to_char(now(),'YYYY-MM-DD HH24:MI:SS')))''')

    c.execute('''CREATE TABLE IF NOT EXISTS bb_training_completions (
        id SERIAL PRIMARY KEY, user_id INTEGER REFERENCES bb_users(id),
        module_id INTEGER REFERENCES bb_training_modules(id),
        score INTEGER, passed INTEGER DEFAULT 0,
        completed_at TEXT DEFAULT (to_char(now(),'YYYY-MM-DD HH24:MI:SS')),
        UNIQUE(user_id, module_id))''')

    c.execute('''CREATE TABLE IF NOT EXISTS bb_audit_log (
        id SERIAL PRIMARY KEY, user_id INTEGER REFERENCES bb_users(id),
        action TEXT NOT NULL, entity_type TEXT, entity_id INTEGER, detail TEXT,
        created_at TEXT DEFAULT (to_char(now(),'YYYY-MM-DD HH24:MI:SS')))''')

    # Department owners — who manages each budget/department
    c.execute('''CREATE TABLE IF NOT EXISTS bb_budget_members (
        id SERIAL PRIMARY KEY,
        budget_id INTEGER REFERENCES bb_budgets(id) ON DELETE CASCADE,
        user_id   INTEGER REFERENCES bb_users(id) ON DELETE CASCADE,
        is_owner  INTEGER DEFAULT 1,
        created_at TEXT DEFAULT (to_char(now(),'YYYY-MM-DD HH24:MI:SS')),
        UNIQUE(budget_id, user_id))''')

    # ── Migrations ────────────────────────────────────────────────────────────
    migrations = [
        ("bb_budgets",           "production_id",     "INTEGER"),
        ("bb_purchase_requests", "production_id",     "INTEGER"),
        ("bb_purchase_requests", "producer_note",     "TEXT"),
        ("bb_purchase_requests", "producer_acted_by", "INTEGER"),
        ("bb_purchase_requests", "producer_acted_at", "TEXT"),
        ("bb_users",             "is_active",         "INTEGER DEFAULT 1"),
        ("bb_purchase_requests", "purchase_method",   "TEXT DEFAULT 'in_store'"),
        ("bb_purchase_requests", "item_url",          "TEXT"),
        ("bb_users",             "receipt_token",     "TEXT"),
    ]
    for table, column, col_type in migrations:
        c.execute("""SELECT COUNT(*) AS n FROM information_schema.columns
                     WHERE table_name=%s AND column_name=%s""", (table, column))
        if c.fetchone()['n'] == 0:
            c.execute(f'ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {col_type}')
        # Backfill is_active=1 for existing users
    c.execute("UPDATE bb_users SET is_active=1 WHERE is_active IS NULL")

    def hp(pw): return hashlib.sha256(pw.encode()).hexdigest()
    for u in [
        ('Admin User','admin@horizonwest.org',hp('admin123'),'admin',1),
        ('Treasurer','treasurer@horizonwest.org',hp('treasurer123'),'treasurer',1),
        ('President','president@horizonwest.org',hp('president123'),'president',1),
        ('Jane Volunteer','volunteer@horizonwest.org',hp('volunteer123'),'volunteer',1),
    ]:
        c.execute('INSERT INTO bb_users (name,email,password,role,training_complete) VALUES (%s,%s,%s,%s,%s) ON CONFLICT (email) DO NOTHING', u)

    c.execute('SELECT COUNT(*) AS n FROM bb_productions')
    if c.fetchone()['n'] == 0:
        c.execute("INSERT INTO bb_productions (name,season,description,total_budget) VALUES (%s,%s,%s,%s)",
                  ('Spring Musical 2025','2024-2025','Main stage spring production',38000))
        c.execute('SELECT id FROM bb_productions ORDER BY id DESC LIMIT 1')
        pid = c.fetchone()['id']
        c.execute('SELECT id FROM bb_users WHERE email=%s',('admin@horizonwest.org',))
        admin = c.fetchone()
        if admin:
            c.execute('INSERT INTO bb_production_members (production_id,user_id,member_role) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING',
                      (pid, admin['id'], 'producer'))
        for dept in [
            ('Scenery & Props','Props','2024-2025',6000,pid),
            ('Costumes','Costumes','2024-2025',2105,pid),
            ('Lighting & AV','Technical','2024-2025',1000,pid),
            ('Licensing & Rights','Admin','2024-2025',6844,pid),
            ('Venue & Space','Admin','2024-2025',19517,pid),
        ]:
            c.execute('INSERT INTO bb_budgets (name,area,season,total_amount,production_id) VALUES (%s,%s,%s,%s,%s)', dept)

    c.execute('SELECT COUNT(*) AS n FROM bb_budgets WHERE production_id IS NULL')
    if c.fetchone()['n'] == 0:
        for b in [('Marketing & Outreach','Marketing','2024-2025',800),('General Operations','Operations','2024-2025',1200)]:
            c.execute('INSERT INTO bb_budgets (name,area,season,total_amount) VALUES (%s,%s,%s,%s)', b)

    c.execute('SELECT COUNT(*) AS n FROM bb_training_modules')
    if c.fetchone()['n'] == 0:
        qs = json.dumps([
            {"question":"Before purchasing for a show, who do you check with first?","options":["The Treasurer","The President","The production Producer","Any board member"],"correct":2,"explanation":"Show purchases go to the Producer first — they manage the production budget and must approve before it goes to Treasurer and President."},
            {"question":"What should you check before buying props or costumes?","options":["Check Amazon for best price","Check HWTC storage and ask the producer what we already have","Ask other volunteers","Post in the group chat"],"correct":1,"explanation":"HWTC has storage full of usable items. Always check storage first and ask the producer — it saves money and storage space."},
            {"question":"What is the deadline for submitting reimbursable purchases?","options":["Closing night","Anytime during the run","Before the first day of tech/load-in","Within a week of opening night"],"correct":2,"explanation":"All reimbursable purchases must be completed before the first day of tech/load-in. Last-minute costs during tech must be handled by an eligible Board Member."},
            {"question":"No receipt means what?","options":["You can submit without it if under $20","No reimbursement — always keep your receipts","The producer can vouch for the purchase","You have 30 days to find it"],"correct":1,"explanation":"No receipt = no reimbursement, full stop. Snap a photo immediately after every purchase."},
            {"question":"What happens to the department budget when a purchase is approved?","options":["Nothing — tracked manually","The amount auto-deducts from the department budget","You notify the treasurer separately","It updates at season end"],"correct":1,"explanation":"BloomBooks automatically deducts approved spend from the department budget so everyone can see remaining funds in real time."},
        ])
        c.execute("INSERT INTO bb_training_modules (title,description,questions,pass_mark,is_active) VALUES (%s,%s,%s,%s,%s)",
                  ('HWTC Purchasing Policy Training','Complete this before making purchases.',qs,80,1))

    conn.commit()
    conn.close()

def hash_pw(pw): return hashlib.sha256(pw.encode()).hexdigest()

def current_user():
    uid = session.get('user_id')
    if not uid: return None
    conn = get_db()
    u = conn.execute('SELECT * FROM bb_users WHERE id=%s',(uid,)).fetchone()
    conn.close()
    return dict(u) if u else None

def require_auth(roles=None):
    u = current_user()
    if not u: return jsonify({'error':'Not authenticated'}),401
    if roles and u['role'] not in roles: return jsonify({'error':'Insufficient permissions'}),403
    return None

# Roles that have org-wide admin access
ADMIN_ROLES = ('admin','treasurer','president')

def is_producer_of(user_id, production_id):
    """True if user is a producer of this production (either via global role+membership OR member_role=producer)."""
    conn = get_db()
    r = conn.execute('''SELECT id FROM bb_production_members
                        WHERE user_id=%s AND production_id=%s
                        AND (member_role='producer')''',(user_id, production_id)).fetchone()
    conn.close()
    return r is not None

def user_production_ids(user_id, role=None):
    conn = get_db()
    if role:
        rows = conn.execute('SELECT production_id FROM bb_production_members WHERE user_id=%s AND member_role=%s',(user_id,role)).fetchall()
    else:
        rows = conn.execute('SELECT production_id FROM bb_production_members WHERE user_id=%s',(user_id,)).fetchall()
    conn.close()
    return [r['production_id'] for r in rows]

def get_production_producers(production_id):
    conn = get_db()
    rows = conn.execute('''SELECT u.email,u.name FROM bb_users u
                           JOIN bb_production_members m ON m.user_id=u.id
                           WHERE m.production_id=%s AND m.member_role='producer' ''',(production_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def log_action(user_id, action, entity_type=None, entity_id=None, detail=None):
    conn = get_db()
    conn.execute('INSERT INTO bb_audit_log (user_id,action,entity_type,entity_id,detail) VALUES (%s,%s,%s,%s,%s)',
                 (user_id,action,entity_type,entity_id,detail))
    conn.commit()
    conn.close()

def send_email(to, subject, body_html):
    if not EMAIL_USER or not EMAIL_PASS:
        print(f"[EMAIL SKIPPED] To:{to} | {subject}"); return
    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From'] = f"BloomBooks — HWTC <{EMAIL_USER}>"
        msg['To'] = to
        msg.attach(MIMEText(body_html,'html'))
        with smtplib.SMTP(EMAIL_HOST,EMAIL_PORT) as s:
            s.starttls(); s.login(EMAIL_USER,EMAIL_PASS); s.send_message(msg)
    except Exception as e:
        print(f"[EMAIL ERROR] {e}")

def get_user_email(user_id):
    conn = get_db()
    u = conn.execute('SELECT email,name FROM bb_users WHERE id=%s',(user_id,)).fetchone()
    conn.close()
    return dict(u) if u else None

def get_role_emails(role):
    conn = get_db()
    users = conn.execute('SELECT email,name FROM bb_users WHERE role=%s',(role,)).fetchall()
    conn.close()
    return [dict(u) for u in users]

def email_html(title, body, cta_text=None, cta_url=None):
    cta = f'<a href="{cta_url}" style="display:inline-block;margin-top:16px;padding:10px 20px;background:#0f6e56;color:#fff;text-decoration:none;border-radius:6px;font-size:14px">{cta_text}</a>' if cta_text else ''
    return f'''<div style="font-family:sans-serif;max-width:520px;margin:0 auto;padding:24px">
<div style="background:#0f6e56;color:#fff;padding:16px 20px;border-radius:8px 8px 0 0">
<p style="margin:0;font-size:12px;opacity:.7">Horizon West Theater Company</p>
<h2 style="margin:4px 0 0;font-size:18px">{title}</h2></div>
<div style="background:#f9f9f7;border:1px solid #e0ddd6;border-top:none;padding:20px;border-radius:0 0 8px 8px">
{body}{cta}</div></div>'''

# ─── Auth ─────────────────────────────────────────────────────────────────────
@app.route('/api/auth/login', methods=['POST'])
def login():
    try:
        data = request.json
        conn = get_db()
        u = conn.execute('SELECT * FROM bb_users WHERE email=%s AND password=%s',
                         (data['email'].strip().lower(), hash_pw(data['password']))).fetchone()
        conn.close()
        if not u: return jsonify({'error':'Invalid email or password'}),401
        if u.get('is_active') == 0:
            return jsonify({'error':'This account has been deactivated. Please contact an admin.'}),403
        session['user_id'] = u['id']
        return jsonify({'user':dict(u)})
    except Exception as e:
        print(f"[LOGIN ERROR] {e}")
        return jsonify({'error': f'Server error — {str(e)}'}), 500

@app.route('/api/auth/logout', methods=['POST'])
def logout():
    session.clear(); return jsonify({'ok':True})

@app.route('/api/auth/me', methods=['GET'])
def me():
    try:
        u = current_user()
        return jsonify({'user': dict(u) if u else None})
    except Exception:
        return jsonify({'user': None})

@app.route('/api/auth/register', methods=['POST'])
def register():
    data = request.json
    email = data.get('email','').strip().lower()
    name  = data.get('name','').strip()
    pw    = data.get('password','')
    if not email or not name or not pw: return jsonify({'error':'All fields required'}),400
    conn = get_db()
    try:
        conn.execute('INSERT INTO bb_users (name,email,password,role) VALUES (%s,%s,%s,%s)',(name,email,hash_pw(pw),'volunteer'))
        conn.commit()
        u = conn.execute('SELECT * FROM bb_users WHERE email=%s',(email,)).fetchone()
        session['user_id'] = u['id']
        conn.close()
        return jsonify({'user':dict(u)})
    except psycopg2.IntegrityError:
        conn.close(); return jsonify({'error':'Email already registered'}),409

# ─── Users ────────────────────────────────────────────────────────────────────
@app.route('/api/users', methods=['GET'])
def list_users():
    err = require_auth(['admin','treasurer','president'])
    if err: return err
    conn = get_db()
    users = conn.execute('SELECT id,name,email,role,training_complete,is_active,created_at FROM bb_users ORDER BY name').fetchall()
    conn.close()
    return jsonify([dict(u) for u in users])

@app.route('/api/users/create', methods=['POST'])
def create_user():
    err = require_auth(['admin','treasurer','president'])
    if err: return err
    data = request.json
    name,email,pw = data.get('name','').strip(), data.get('email','').strip().lower(), data.get('password','')
    if not name or not email or not pw: return jsonify({'error':'Name, email and password required'}),400
    conn = get_db()
    try:
        conn.execute('INSERT INTO bb_users (name,email,password,role,training_complete,is_active) VALUES (%s,%s,%s,%s,%s,%s)',
                     (name,email,hash_pw(pw),data.get('role','volunteer'),int(data.get('training_complete',0)),1))
        conn.commit(); conn.close(); return jsonify({'ok':True})
    except psycopg2.IntegrityError:
        conn.close(); return jsonify({'error':'Email already exists'}),409

@app.route('/api/users/<int:uid>', methods=['PATCH'])
def update_user(uid):
    err = require_auth(['admin','treasurer','president'])
    if err: return err
    data = request.json
    conn = get_db()
    if 'name'             in data: conn.execute('UPDATE bb_users SET name=%s WHERE id=%s',(data['name'],uid))
    if 'email'            in data: conn.execute('UPDATE bb_users SET email=%s WHERE id=%s',(data['email'].strip().lower(),uid))
    if 'role'             in data: conn.execute('UPDATE bb_users SET role=%s WHERE id=%s',(data['role'],uid))
    if 'training_complete'in data: conn.execute('UPDATE bb_users SET training_complete=%s WHERE id=%s',(data['training_complete'],uid))
    if 'is_active'        in data: conn.execute('UPDATE bb_users SET is_active=%s WHERE id=%s',(data['is_active'],uid))
    if 'password' in data and data['password']:
        conn.execute('UPDATE bb_users SET password=%s WHERE id=%s',(hash_pw(data['password']),uid))
    conn.commit(); conn.close(); return jsonify({'ok':True})

@app.route('/api/users/<int:uid>', methods=['DELETE'])
def delete_user(uid):
    err = require_auth(['admin','treasurer','president'])
    if err: return err
    u = current_user()
    if u['id'] == uid:
        return jsonify({'error':'You cannot delete your own account'}),400
    conn = get_db()
    # Remove from production memberships and budget memberships first
    conn.execute('DELETE FROM bb_production_members WHERE user_id=%s',(uid,))
    conn.execute('DELETE FROM bb_budget_members WHERE user_id=%s',(uid,))
    conn.execute('DELETE FROM bb_users WHERE id=%s',(uid,))
    conn.commit(); conn.close()
    log_action(u['id'],'deleted_user','user',uid,f'deleted user {uid}')
    return jsonify({'ok':True})

# ─── Budget members (department owners) ──────────────────────────────────────
@app.route('/api/budgets/<int:bid>/members', methods=['GET'])
def list_budget_members(bid):
    err = require_auth()
    if err: return err
    conn = get_db()
    rows = conn.execute('''SELECT bm.*,u.name,u.email FROM bb_budget_members bm
                           JOIN bb_users u ON bm.user_id=u.id
                           WHERE bm.budget_id=%s ORDER BY u.name''',(bid,)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/budgets/<int:bid>/members', methods=['POST'])
def add_budget_member(bid):
    u = current_user()
    if not u: return jsonify({'error':'Not authenticated'}),401
    # Allow producer of the production this budget belongs to, or admin/treasurer/president
    conn = get_db()
    b = conn.execute('SELECT * FROM bb_budgets WHERE id=%s',(bid,)).fetchone()
    if not b: conn.close(); return jsonify({'error':'Budget not found'}),404
    b = dict(b)
    if u['role'] not in ('admin','treasurer','president','producer'):
        if not b['production_id'] or not is_producer_of(u['id'],b['production_id']):
            conn.close(); return jsonify({'error':'Insufficient permissions'}),403
    data = request.json
    try:
        conn.execute('INSERT INTO bb_budget_members (budget_id,user_id,is_owner) VALUES (%s,%s,%s)',
                     (bid, data['user_id'], int(data.get('is_owner',1))))
        conn.commit(); conn.close(); return jsonify({'ok':True})
    except psycopg2.IntegrityError:
        conn.close(); return jsonify({'error':'User already assigned to this department'}),409

@app.route('/api/budgets/<int:bid>/members/<int:uid>', methods=['DELETE'])
def remove_budget_member(bid, uid):
    u = current_user()
    if not u: return jsonify({'error':'Not authenticated'}),401
    conn = get_db()
    b = conn.execute('SELECT * FROM bb_budgets WHERE id=%s',(bid,)).fetchone()
    if not b: conn.close(); return jsonify({'error':'Budget not found'}),404
    b = dict(b)
    if u['role'] not in ('admin','treasurer','president','producer'):
        if not b['production_id'] or not is_producer_of(u['id'],b['production_id']):
            conn.close(); return jsonify({'error':'Insufficient permissions'}),403
    conn.execute('DELETE FROM bb_budget_members WHERE budget_id=%s AND user_id=%s',(bid,uid))
    conn.commit(); conn.close(); return jsonify({'ok':True})

# ─── Productions ──────────────────────────────────────────────────────────────
@app.route('/api/productions', methods=['GET'])
def list_productions():
    err = require_auth()
    if err: return err
    u = current_user()
    conn = get_db()
    is_admin = u['role'] in ('admin','treasurer','president')
    is_producer_role = u['role'] == 'producer'

    if is_admin:
        prods = conn.execute('SELECT * FROM bb_productions ORDER BY status,name').fetchall()
    else:
        # volunteers, producers, and department owners all see only their assigned productions
        my_ids = user_production_ids(u['id'])
        if not my_ids:
            conn.close(); return jsonify([])
        ph = ','.join(['%s']*len(my_ids))
        prods = conn.execute(f'SELECT * FROM bb_productions WHERE id IN ({ph}) ORDER BY status,name', my_ids).fetchall()

    result = []
    for p in prods:
        prod = dict(p)
        members = conn.execute('''SELECT u.id AS user_id, u.name, u.email, u.role, m.member_role
                                  FROM bb_production_members m JOIN bb_users u ON m.user_id=u.id
                                  WHERE m.production_id=%s ORDER BY m.member_role,u.name''',(prod['id'],)).fetchall()
        prod['members'] = [dict(m) for m in members]
        budgets = conn.execute('SELECT * FROM bb_budgets WHERE production_id=%s AND is_active=1',(prod['id'],)).fetchall()
        prod['budgets'] = []
        for b in budgets:
            bd = dict(b)
            owners = conn.execute('''SELECT bm.user_id,u.name,u.email,bm.is_owner
                                     FROM bb_budget_members bm JOIN bb_users u ON bm.user_id=u.id
                                     WHERE bm.budget_id=%s ORDER BY u.name''',(bd['id'],)).fetchall()
            bd['owners'] = [dict(o) for o in owners]
            prod['budgets'].append(bd)
        prod['total_spent'] = sum(b['spent'] for b in prod['budgets'])
        prod['i_am_producer'] = any(m['user_id']==u['id'] and m['member_role']=='producer' for m in prod['members'])
        result.append(prod)
    conn.close()
    return jsonify(result)

@app.route('/api/productions', methods=['POST'])
def create_production():
    err = require_auth(['admin','treasurer','president'])
    if err: return err
    data = request.json
    name,season = data.get('name','').strip(), data.get('season','').strip()
    if not name or not season: return jsonify({'error':'Name and season required'}),400
    conn = get_db()
    conn.execute('INSERT INTO bb_productions (name,season,description,total_budget,status) VALUES (%s,%s,%s,%s,%s)',
                 (name,season,data.get('description',''),float(data.get('total_budget',0)),'active'))
    row = conn.execute('SELECT id FROM bb_productions WHERE name=%s AND season=%s ORDER BY id DESC LIMIT 1',(name,season)).fetchone()
    prod_id = row['id']
    if data.get('producer_id'):
        conn.execute('INSERT INTO bb_production_members (production_id,user_id,member_role) VALUES (%s,%s,%s)',
                     (prod_id,data['producer_id'],'producer'))
    conn.commit(); conn.close()
    log_action(current_user()['id'],'created_production','production',prod_id,name)
    return jsonify({'ok':True,'id':prod_id})

@app.route('/api/productions/<int:pid>', methods=['PATCH'])
def update_production(pid):
    u = current_user()
    if not u: return jsonify({'error':'Not authenticated'}),401
    if u['role'] not in ('admin','treasurer','president') and not is_producer_of(u['id'],pid):
        return jsonify({'error':'Insufficient permissions'}),403
    data = request.json
    conn = get_db()
    for f in ['name','season','description','total_budget','status']:
        if f in data: conn.execute(f'UPDATE bb_productions SET {f}=%s WHERE id=%s',(data[f],pid))
    conn.commit(); conn.close(); return jsonify({'ok':True})

@app.route('/api/productions/<int:pid>/members', methods=['POST'])
def add_production_member(pid):
    u = current_user()
    if not u: return jsonify({'error':'Not authenticated'}),401
    if u['role'] != 'admin' and not is_producer_of(u['id'],pid):
        return jsonify({'error':'Insufficient permissions'}),403
    data = request.json
    conn = get_db()
    try:
        conn.execute('INSERT INTO bb_production_members (production_id,user_id,member_role) VALUES (%s,%s,%s)',
                     (pid,data['user_id'],data.get('member_role','member')))
        conn.commit(); conn.close(); return jsonify({'ok':True})
    except psycopg2.IntegrityError:
        conn.close(); return jsonify({'error':'Person already a member'}),409

@app.route('/api/productions/<int:pid>/members/<int:uid>', methods=['DELETE'])
def remove_production_member(pid, uid):
    u = current_user()
    if not u: return jsonify({'error':'Not authenticated'}),401
    if u['role'] != 'admin' and not is_producer_of(u['id'],pid):
        return jsonify({'error':'Insufficient permissions'}),403
    conn = get_db()
    conn.execute('DELETE FROM bb_production_members WHERE production_id=%s AND user_id=%s',(pid,uid))
    conn.commit(); conn.close(); return jsonify({'ok':True})

@app.route('/api/productions/<int:pid>/members/<int:uid>/role', methods=['PATCH'])
def update_member_role(pid, uid):
    err = require_auth(['admin','treasurer','president'])
    if err: return err
    data = request.json
    conn = get_db()
    conn.execute('UPDATE bb_production_members SET member_role=%s WHERE production_id=%s AND user_id=%s',
                 (data['member_role'],pid,uid))
    conn.commit(); conn.close(); return jsonify({'ok':True})

# ─── Budgets ──────────────────────────────────────────────────────────────────
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
        my_ids = user_production_ids(u['id'])
        if my_ids:
            ph = ','.join(['%s']*len(my_ids))
            budgets = conn.execute(f'''SELECT b.*,p.name as production_name FROM bb_budgets b
                                       LEFT JOIN bb_productions p ON b.production_id=p.id
                                       WHERE (b.production_id IN ({ph}) OR b.production_id IS NULL) AND b.is_active=1''',
                                   my_ids).fetchall()
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
    prod_id = data.get('production_id')
    if u['role'] not in ('admin','treasurer','president','producer'):
        if not prod_id or not is_producer_of(u['id'],int(prod_id)):
            return jsonify({'error':'Insufficient permissions'}),403
    conn = get_db()
    conn.execute('INSERT INTO bb_budgets (name,area,season,total_amount,production_id) VALUES (%s,%s,%s,%s,%s)',
                 (data['name'],data.get('area','General'),data.get('season',''),float(data['total_amount']),prod_id or None))
    conn.commit(); conn.close(); return jsonify({'ok':True})

@app.route('/api/budgets/<int:bid>', methods=['PATCH'])
def update_budget(bid):
    u = current_user()
    if not u: return jsonify({'error':'Not authenticated'}),401
    conn = get_db()
    b = conn.execute('SELECT * FROM bb_budgets WHERE id=%s',(bid,)).fetchone()
    if not b: conn.close(); return jsonify({'error':'Not found'}),404
    b = dict(b)
    if u['role'] not in ('admin','treasurer','president','producer'):
        if not b['production_id'] or not is_producer_of(u['id'],b['production_id']):
            conn.close(); return jsonify({'error':'Insufficient permissions'}),403
    data = request.json
    fields,vals = [],[]
    for f in ['name','area','season','total_amount','is_active']:
        if f in data: fields.append(f'{f}=%s'); vals.append(data[f])
    if fields:
        vals.append(bid)
        conn.execute(f'UPDATE bb_budgets SET {", ".join(fields)} WHERE id=%s',vals)
        conn.commit()
    conn.close(); return jsonify({'ok':True})

# ─── Purchase Requests ────────────────────────────────────────────────────────
@app.route('/api/requests', methods=['GET'])
def list_requests():
    err = require_auth()
    if err: return err
    u = current_user()
    conn = get_db()
    is_admin = u['role'] in ('admin','treasurer','president')
    producer_ids = user_production_ids(u['id'],'producer')

    # Budget IDs this user owns as department owner
    conn2 = get_db()
    owned_budget_rows = conn2.execute('SELECT budget_id FROM bb_budget_members WHERE user_id=%s',(u['id'],)).fetchall()
    conn2.close()
    owned_budget_ids = [r['budget_id'] for r in owned_budget_rows]

    base = '''SELECT r.*,sub.name as submitter_name,sub.email as submitter_email,
                b.name as budget_name,b.area as budget_area,
                b.total_amount as budget_total,b.spent as budget_spent,
                p.name as production_name
              FROM bb_purchase_requests r
              LEFT JOIN bb_users sub ON r.submitted_by=sub.id
              LEFT JOIN bb_budgets b ON r.budget_id=b.id
              LEFT JOIN bb_productions p ON r.production_id=p.id'''

    status_filter = request.args.get('status')
    mine_only = request.args.get('mine') == '1'
    prod_filter = request.args.get('production_id')
    conditions,params = [],[]

    if mine_only:
        conditions.append('r.submitted_by=%s'); params.append(u['id'])
    elif not is_admin:
        sub_conditions = ['r.submitted_by=%s']
        sub_params = [u['id']]
        if producer_ids:
            ph = ','.join(['%s']*len(producer_ids))
            sub_conditions.append(f'r.production_id IN ({ph})')
            sub_params.extend(producer_ids)
        if owned_budget_ids:
            ph = ','.join(['%s']*len(owned_budget_ids))
            sub_conditions.append(f'r.budget_id IN ({ph})')
            sub_params.extend(owned_budget_ids)
        conditions.append(f'({" OR ".join(sub_conditions)})')
        params.extend(sub_params)

    if status_filter: conditions.append('r.status=%s'); params.append(status_filter)
    if prod_filter: conditions.append('r.production_id=%s'); params.append(int(prod_filter))

    if conditions: base += ' WHERE '+' AND '.join(conditions)
    base += ' ORDER BY r.submitted_at DESC'

    rows = conn.execute(base,params).fetchall()
    result = []
    for row in rows:
        r = dict(row)
        receipts = conn.execute('SELECT * FROM bb_receipts WHERE request_id=%s',(r['id'],)).fetchall()
        r['receipts'] = [dict(rec) for rec in receipts]
        result.append(r)
    conn.close()
    return jsonify(result)

@app.route('/api/requests', methods=['POST'])
def create_request():
    err = require_auth()
    if err: return err
    u = current_user()
    if not u['training_complete'] and u['role'] == 'volunteer':
        return jsonify({'error':'Complete purchasing training first.'}),403
    data = request.json
    is_sap = 1 if data.get('is_sap') else 0
    req_type = 'sap' if is_sap else 'pre_approval'
    purchase_method = data.get('purchase_method', 'in_store')  # 'online' or 'in_store'
    item_url = data.get('item_url', '')
    prod_id = data.get('production_id')
    if prod_id and get_production_producers(int(prod_id)):
        status = 'pending_producer'
    else:
        status = 'pending_treasurer'
    conn = get_db()
    conn.execute('''INSERT INTO bb_purchase_requests
                    (type,status,title,description,vendor,estimated_cost,budget_id,production_id,
                     submitted_by,is_emergency,emergency_reason,purchase_method,item_url)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                 (req_type, status,
                  data['title'], data.get('description',''), data.get('vendor',''),
                  float(data['estimated_cost']), data.get('budget_id') or None,
                  prod_id or None, u['id'], is_sap, data.get('sap_reason',''),
                  purchase_method, item_url))
    row = conn.execute('SELECT lastval() AS id').fetchone()
    req_id = row['id']
    conn.commit(); conn.close()
    log_action(u['id'],'submitted_request','request',req_id,data['title'])
    type_label = 'SAP (Self-Authorized Purchase)' if is_sap else 'Pre-approval request'
    method_label = f'Online — {item_url}' if purchase_method == 'online' and item_url else purchase_method.replace('_',' ').title()
    if status == 'pending_producer' and prod_id:
        for p in get_production_producers(int(prod_id)):
            send_email(p['email'],f"Purchase request needs your approval: {data['title']}",
                email_html('Producer Approval Needed',
                    f'<p><b>{u["name"]}</b> submitted a {type_label.lower()} for your production.</p>'
                    f'<p><b>Item:</b> {data["title"]}<br><b>Amount:</b> ${float(data["estimated_cost"]):.2f}<br>'
                    f'<b>Method:</b> {method_label}</p>'
                    + (f'<p><a href="{item_url}">{item_url}</a></p>' if item_url else ''),
                    'Review in BloomBooks',APP_URL))
    else:
        for t in get_role_emails('treasurer'):
            send_email(t['email'],f"New purchase request: {data['title']}",
                email_html('New Purchase Request',
                    f'<p><b>{u["name"]}</b> submitted a {type_label.lower()}.<br>'
                    f'<b>Item:</b> {data["title"]}<br><b>Amount:</b> ${float(data["estimated_cost"]):.2f}<br>'
                    f'<b>Method:</b> {method_label}</p>'
                    + (f'<p><a href="{item_url}">{item_url}</a></p>' if item_url else ''),
                    'Review in BloomBooks',APP_URL))
    return jsonify({'ok':True,'id':req_id})

@app.route('/api/requests/<int:rid>/approve', methods=['POST'])
def approve_request(rid):
    u = current_user()
    if not u: return jsonify({'error':'Not authenticated'}),401
    data = request.json
    action,note = data.get('action'), data.get('note','')
    conn = get_db()
    req = conn.execute('SELECT * FROM bb_purchase_requests WHERE id=%s',(rid,)).fetchone()
    if not req: conn.close(); return jsonify({'error':'Not found'}),404
    req = dict(req)
    now = datetime.now().isoformat()
    new_status = req['status']

    if req['status'] == 'pending_producer':
        if u['role'] not in ('admin','treasurer','president') and not is_producer_of(u['id'], req['production_id']):
            conn.close(); return jsonify({'error':'Only the production producer can act here'}),403
        if action == 'approve':
            new_status = 'pending_treasurer'
            conn.execute('''UPDATE bb_purchase_requests SET status=%s,producer_note=%s,
                            producer_acted_by=%s,producer_acted_at=%s,updated_at=%s WHERE id=%s''',
                         (new_status,note,u['id'],now,now,rid))
            for t in get_role_emails('treasurer'):
                send_email(t['email'],f"Producer approved — needs treasurer review: {req['title']}",
                    email_html('Treasurer Review Needed',
                        f'<p>Producer approved <b>{req["title"]}</b> (${req["estimated_cost"]:.2f}).</p>'
                        +(f'<p><b>Note:</b> {note}</p>' if note else ''),
                        'Review in BloomBooks',APP_URL))
        else:
            new_status = 'denied'
            conn.execute('''UPDATE bb_purchase_requests SET status=%s,producer_note=%s,
                            producer_acted_by=%s,producer_acted_at=%s,updated_at=%s WHERE id=%s''',
                         (new_status,note,u['id'],now,now,rid))

    elif req['status'] == 'pending_treasurer':
        if u['role'] not in ('treasurer','admin'):
            conn.close(); return jsonify({'error':'Only the treasurer can act here'}),403
        if action == 'approve':
            new_status = 'pending_president'
            conn.execute('''UPDATE bb_purchase_requests SET status=%s,treasurer_note=%s,
                            treasurer_acted_by=%s,treasurer_acted_at=%s,updated_at=%s WHERE id=%s''',
                         (new_status,note,u['id'],now,now,rid))
            for p in get_role_emails('president'):
                send_email(p['email'],f"Awaiting your final approval: {req['title']}",
                    email_html('Final Approval Needed',
                        f'<p>Treasurer approved <b>{req["title"]}</b> (${req["estimated_cost"]:.2f}).</p>'
                        +(f'<p><b>Note:</b> {note}</p>' if note else ''),
                        'Review in BloomBooks',APP_URL))
        else:
            new_status = 'denied'
            conn.execute('''UPDATE bb_purchase_requests SET status=%s,treasurer_note=%s,
                            treasurer_acted_by=%s,treasurer_acted_at=%s,updated_at=%s WHERE id=%s''',
                         (new_status,note,u['id'],now,now,rid))

    elif req['status'] == 'pending_president':
        if u['role'] not in ('president','admin'):
            conn.close(); return jsonify({'error':'Only the president can act here'}),403
        if action == 'approve':
            new_status = 'approved'
            actual = float(data.get('actual_cost',req['estimated_cost']))
            conn.execute('''UPDATE bb_purchase_requests SET status=%s,president_note=%s,
                            president_acted_by=%s,president_acted_at=%s,actual_cost=%s,updated_at=%s WHERE id=%s''',
                         (new_status,note,u['id'],now,actual,now,rid))
            if req['budget_id']:
                conn.execute('UPDATE bb_budgets SET spent=spent+%s WHERE id=%s',(actual,req['budget_id']))
            conn.execute('INSERT INTO bb_reimbursements (request_id,user_id,amount) VALUES (%s,%s,%s)',
                         (rid,req['submitted_by'],actual))
        else:
            new_status = 'denied'
            conn.execute('''UPDATE bb_purchase_requests SET status=%s,president_note=%s,
                            president_acted_by=%s,president_acted_at=%s,updated_at=%s WHERE id=%s''',
                         (new_status,note,u['id'],now,now,rid))
    else:
        conn.close(); return jsonify({'error':'Action not permitted at this stage'}),400

    conn.commit(); conn.close()
    log_action(u['id'],f'{action}d_request','request',rid,f'status={new_status}')

    submitter = get_user_email(req['submitted_by'])
    if submitter:
        msgs = {
            'approved': ('✓ Purchase approved',f'Your request for <b>{req["title"]}</b> is fully approved. You will be reimbursed once the treasurer processes payment.'),
            'denied':   ('Purchase request update',f'Your request for <b>{req["title"]}</b> was not approved.'+(f'<br><b>Reason:</b> {note}' if note else '')),
            'pending_treasurer': ('Update: producer approved',f'Your request for <b>{req["title"]}</b> was approved by the producer and is now with the treasurer.'),
            'pending_president': ('Update: treasurer approved',f'Your request for <b>{req["title"]}</b> was approved by the treasurer and is now with the president.'),
        }
        if new_status in msgs:
            subj,body = msgs[new_status]
            send_email(submitter['email'],subj,email_html(subj,f'<p>{body}</p>'))

    return jsonify({'ok':True,'new_status':new_status})

# ─── Receipts ─────────────────────────────────────────────────────────────────
@app.route('/api/requests/<int:rid>/receipts', methods=['POST'])
def upload_receipt(rid):
    err = require_auth()
    if err: return err
    if 'file' not in request.files: return jsonify({'error':'No file'}),400
    if not cloudinary.config().cloud_name: return jsonify({'error':'Cloudinary not configured'}),500
    try:
        r = cloudinary.uploader.upload(request.files['file'],folder='bloombooks/receipts',resource_type='auto')
        conn = get_db()
        conn.execute('INSERT INTO bb_receipts (request_id,image_url,public_id) VALUES (%s,%s,%s)',
                     (rid,r['secure_url'],r['public_id']))
        conn.commit(); conn.close()
        log_action(current_user()['id'],'uploaded_receipt','request',rid)
        return jsonify({'ok':True,'image_url':r['secure_url']})
    except Exception as e:
        return jsonify({'error':str(e)}),500

# ─── Reimbursements ───────────────────────────────────────────────────────────
@app.route('/api/reimbursements', methods=['GET'])
def list_reimbursements():
    err = require_auth()
    if err: return err
    u = current_user()
    conn = get_db()
    if u['role'] in ('admin','treasurer','president'):
        rows = conn.execute('''SELECT rb.*,u.name as user_name,u.email as user_email,
                                      pr.title,pr.estimated_cost,pr.actual_cost,pr.is_emergency
                               FROM bb_reimbursements rb JOIN bb_users u ON rb.user_id=u.id
                               JOIN bb_purchase_requests pr ON rb.request_id=pr.id
                               ORDER BY rb.created_at DESC''').fetchall()
    else:
        rows = conn.execute('''SELECT rb.*,u.name as user_name,u.email as user_email,
                                      pr.title,pr.estimated_cost,pr.actual_cost,pr.is_emergency
                               FROM bb_reimbursements rb JOIN bb_users u ON rb.user_id=u.id
                               JOIN bb_purchase_requests pr ON rb.request_id=pr.id
                               WHERE rb.user_id=%s ORDER BY rb.created_at DESC''',(u['id'],)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/reimbursements/<int:rid>/pay', methods=['POST'])
def mark_paid(rid):
    err = require_auth(['treasurer','admin','president'])
    if err: return err
    data = request.json; now = datetime.now().isoformat()
    conn = get_db()
    rb = conn.execute('SELECT * FROM bb_reimbursements WHERE id=%s',(rid,)).fetchone()
    if not rb: conn.close(); return jsonify({'error':'Not found'}),404
    rb = dict(rb)
    conn.execute('UPDATE bb_reimbursements SET status=%s,method=%s,paid_at=%s,notes=%s WHERE id=%s',
                 ('paid',data.get('method',''),now,data.get('notes',''),rid))
    conn.execute("UPDATE bb_purchase_requests SET status='reimbursed',updated_at=%s WHERE id=%s",(now,rb['request_id']))
    conn.commit()
    ui = get_user_email(rb['user_id'])
    if ui:
        send_email(ui['email'],'Reimbursement processed',
            email_html('You Have Been Reimbursed',
                f'<p>Your reimbursement of <b>${rb["amount"]:.2f}</b> has been processed.</p>'
                +(f'<p><b>Method:</b> {data.get("method","")}</p>' if data.get('method') else '')))
    conn.close()
    log_action(current_user()['id'],'marked_paid','reimbursement',rid)
    return jsonify({'ok':True})

# ─── Training ─────────────────────────────────────────────────────────────────
@app.route('/api/training', methods=['GET'])
def get_training():
    err = require_auth()
    if err: return err
    conn = get_db()
    m = conn.execute('SELECT * FROM bb_training_modules WHERE is_active=1 ORDER BY id LIMIT 1').fetchone()
    conn.close()
    if not m: return jsonify({'module':None})
    m = dict(m); m['questions']=json.loads(m['questions']); m['slides']=json.loads(m['slides'])
    return jsonify({'module':m})

@app.route('/api/training/status', methods=['GET'])
def training_status():
    err = require_auth()
    if err: return err
    u = current_user()
    conn = get_db()
    m = conn.execute('SELECT * FROM bb_training_modules WHERE is_active=1 LIMIT 1').fetchone()
    if not m: conn.close(); return jsonify({'required':False})
    c2 = conn.execute('SELECT * FROM bb_training_completions WHERE user_id=%s AND module_id=%s',(u['id'],m['id'])).fetchone()
    conn.close()
    return jsonify({'required':True,'completed':bool(c2 and c2['passed']),'score':c2['score'] if c2 else None})

@app.route('/api/training/complete', methods=['POST'])
def complete_training():
    err = require_auth()
    if err: return err
    u = current_user()
    score = int(request.json.get('score',0))
    conn = get_db()
    m = conn.execute('SELECT * FROM bb_training_modules WHERE is_active=1 LIMIT 1').fetchone()
    if not m: conn.close(); return jsonify({'error':'No active module'}),404
    passed = 1 if score >= m['pass_mark'] else 0
    conn.execute('''INSERT INTO bb_training_completions (user_id,module_id,score,passed) VALUES (%s,%s,%s,%s)
                    ON CONFLICT(user_id,module_id) DO UPDATE SET score=EXCLUDED.score,passed=EXCLUDED.passed,
                    completed_at=to_char(now(),'YYYY-MM-DD HH24:MI:SS')''',(u['id'],m['id'],score,passed))
    if passed: conn.execute('UPDATE bb_users SET training_complete=1 WHERE id=%s',(u['id'],))
    conn.commit(); conn.close()
    return jsonify({'ok':True,'passed':bool(passed),'score':score,'pass_mark':m['pass_mark']})

@app.route('/api/training/slides/update', methods=['POST'])
def update_slides():
    err = require_auth(['admin','treasurer','president'])
    if err: return err
    conn = get_db()
    conn.execute('UPDATE bb_training_modules SET slides=%s WHERE is_active=1',(json.dumps(request.json.get('slides',[])),))
    conn.commit(); conn.close(); return jsonify({'ok':True})

@app.route('/api/training/slides', methods=['POST'])
def upload_slide():
    err = require_auth(['admin','treasurer','president'])
    if err: return err
    if 'file' not in request.files: return jsonify({'error':'No file'}),400
    if not cloudinary.config().cloud_name: return jsonify({'error':'Cloudinary not configured'}),500
    try:
        r = cloudinary.uploader.upload(request.files['file'],folder='bloombooks/slides')
        return jsonify({'ok':True,'url':r['secure_url'],'public_id':r['public_id']})
    except Exception as e:
        return jsonify({'error':str(e)}),500

@app.route('/api/training', methods=['PUT'])
def update_training():
    err = require_auth(['admin','treasurer','president'])
    if err: return err
    data = request.json
    conn = get_db()
    conn.execute('UPDATE bb_training_modules SET title=%s,description=%s,questions=%s,pass_mark=%s WHERE is_active=1',
                 (data.get('title'),data.get('description'),json.dumps(data.get('questions',[])),int(data.get('pass_mark',80))))
    conn.commit(); conn.close(); return jsonify({'ok':True})

# ─── Stats ────────────────────────────────────────────────────────────────────
@app.route('/api/stats', methods=['GET'])
def stats():
    err = require_auth()
    if err: return err
    u = current_user()
    conn = get_db()
    is_admin = u['role'] in ('admin','treasurer','president')
    prod_ids = user_production_ids(u['id'],'producer')
    r = {}
    if is_admin:
        for key,sql in [
            ('pending_producer',"SELECT COUNT(*) as n FROM bb_purchase_requests WHERE status='pending_producer'"),
            ('pending_treasurer',"SELECT COUNT(*) as n FROM bb_purchase_requests WHERE status='pending_treasurer'"),
            ('pending_president',"SELECT COUNT(*) as n FROM bb_purchase_requests WHERE status='pending_president'"),
            ('pending_reimburse',"SELECT COUNT(*) as n FROM bb_reimbursements WHERE status='pending'"),
            ('total_requests',"SELECT COUNT(*) as n FROM bb_purchase_requests"),
            ('sap_count',"SELECT COUNT(*) as n FROM bb_purchase_requests WHERE type='sap'"),
        ]:
            r[key] = conn.execute(sql).fetchone()['n']
        r['total_spent'] = round(conn.execute("SELECT COALESCE(SUM(actual_cost),0) as n FROM bb_purchase_requests WHERE status IN ('approved','reimbursed')").fetchone()['n'],2)
    else:
        r['my_requests'] = conn.execute("SELECT COUNT(*) as n FROM bb_purchase_requests WHERE submitted_by=%s",(u['id'],)).fetchone()['n']
        r['my_approved'] = conn.execute("SELECT COUNT(*) as n FROM bb_purchase_requests WHERE submitted_by=%s AND status IN ('approved','reimbursed')",(u['id'],)).fetchone()['n']
        r['my_pending']  = conn.execute("SELECT COUNT(*) as n FROM bb_purchase_requests WHERE submitted_by=%s AND status LIKE 'pending%%'",(u['id'],)).fetchone()['n']
        r['my_owed']     = round(conn.execute("SELECT COALESCE(SUM(amount),0) as n FROM bb_reimbursements WHERE user_id=%s AND status='pending'",(u['id'],)).fetchone()['n'],2)
        # Producers see their production queue count
        all_prod_ids = prod_ids if prod_ids else []
        if u['role'] == 'producer' and not all_prod_ids:
            all_prod_ids = user_production_ids(u['id'])
        if all_prod_ids:
            ph = ','.join(['%s']*len(all_prod_ids))
            r['pending_my_productions'] = conn.execute(
                f"SELECT COUNT(*) as n FROM bb_purchase_requests WHERE status='pending_producer' AND production_id IN ({ph})",
                all_prod_ids).fetchone()['n']
            r['is_producer'] = True
    conn.close()
    return jsonify(r)

@app.route('/api/audit', methods=['GET'])
def audit_log():
    err = require_auth(['admin','treasurer','president'])
    if err: return err
    conn = get_db()
    rows = conn.execute('''SELECT a.*,u.name as user_name FROM bb_audit_log a
                           LEFT JOIN bb_users u ON a.user_id=u.id
                           ORDER BY a.created_at DESC LIMIT 100''').fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

# ─── Receipt token / mobile receipt link ─────────────────────────────────────
def ensure_receipt_token(user_id):
    """Generate a receipt token for user if they don't have one yet."""
    conn = get_db()
    u = conn.execute('SELECT receipt_token FROM bb_users WHERE id=%s',(user_id,)).fetchone()
    if not u or not u['receipt_token']:
        token = secrets.token_urlsafe(24)
        conn.execute('UPDATE bb_users SET receipt_token=%s WHERE id=%s',(token, user_id))
        conn.commit()
        conn.close()
        return token
    conn.close()
    return u['receipt_token']

@app.route('/api/users/<int:uid>/receipt-token', methods=['GET'])
def get_receipt_token(uid):
    err = require_auth(['admin','treasurer','president'])
    if err: return err
    token = ensure_receipt_token(uid)
    link = f"{APP_URL}/receipt/{token}"
    return jsonify({'token': token, 'link': link})

@app.route('/api/users/<int:uid>/receipt-token/regenerate', methods=['POST'])
def regenerate_receipt_token(uid):
    err = require_auth(['admin','treasurer','president'])
    if err: return err
    token = secrets.token_urlsafe(24)
    conn = get_db()
    conn.execute('UPDATE bb_users SET receipt_token=%s WHERE id=%s',(token, uid))
    conn.commit(); conn.close()
    link = f"{APP_URL}/receipt/{token}"
    return jsonify({'token': token, 'link': link})

@app.route('/api/receipt/<token>', methods=['GET'])
def get_receipt_page_data(token):
    """Public endpoint — returns user info and open requests for the receipt submission page."""
    conn = get_db()
    u = conn.execute('SELECT id,name,email FROM bb_users WHERE receipt_token=%s AND is_active=1',(token,)).fetchone()
    if not u:
        conn.close()
        return jsonify({'error': 'Invalid or expired link'}), 404
    u = dict(u)
    # Open requests: pending or approved but not yet reimbursed
    requests = conn.execute('''SELECT id,title,estimated_cost,actual_cost,status,type,vendor,submitted_at
                                FROM bb_purchase_requests
                                WHERE submitted_by=%s
                                AND status NOT IN ('denied','reimbursed')
                                ORDER BY submitted_at DESC''', (u['id'],)).fetchall()
    conn.close()
    return jsonify({'user': u, 'requests': [dict(r) for r in requests]})

@app.route('/api/receipt/<token>/submit', methods=['POST'])
def submit_receipt_mobile(token):
    """Public endpoint — upload a receipt photo from the mobile receipt page."""
    conn = get_db()
    u = conn.execute('SELECT id,name FROM bb_users WHERE receipt_token=%s AND is_active=1',(token,)).fetchone()
    if not u:
        conn.close()
        return jsonify({'error': 'Invalid or expired link'}), 404
    u = dict(u)

    request_id = request.form.get('request_id')
    note       = request.form.get('note', '')
    actual     = request.form.get('actual_cost', '')

    if not request_id:
        return jsonify({'error': 'No request selected'}), 400

    # Verify request belongs to this user
    req = conn.execute('SELECT * FROM bb_purchase_requests WHERE id=%s AND submitted_by=%s',
                       (request_id, u['id'])).fetchone()
    if not req:
        conn.close()
        return jsonify({'error': 'Request not found'}), 404

    # Upload receipt if provided
    image_url = None
    if 'file' in request.files and request.files['file'].filename:
        if not cloudinary.config().cloud_name:
            conn.close()
            return jsonify({'error': 'File upload not configured'}), 500
        try:
            result = cloudinary.uploader.upload(
                request.files['file'],
                folder='bloombooks/receipts',
                resource_type='auto'
            )
            image_url = result['secure_url']
            conn.execute('INSERT INTO bb_receipts (request_id,image_url,public_id) VALUES (%s,%s,%s)',
                         (request_id, image_url, result['public_id']))
        except Exception as e:
            conn.close()
            return jsonify({'error': f'Upload failed: {str(e)}'}), 500

    # Update actual cost if provided
    if actual:
        try:
            conn.execute('UPDATE bb_purchase_requests SET actual_cost=%s WHERE id=%s',
                         (float(actual), request_id))
        except Exception:
            pass

    # Add note to description if provided
    if note:
        existing = req['description'] or ''
        new_desc = f"{existing}\n\n[Receipt note]: {note}".strip()
        conn.execute('UPDATE bb_purchase_requests SET description=%s WHERE id=%s',
                     (new_desc, request_id))

    conn.commit()
    log_action(u['id'], 'mobile_receipt_upload', 'request', int(request_id), f'via mobile link')
    conn.close()
    return jsonify({'ok': True, 'image_url': image_url})

@app.route('/receipt/<token>')
def mobile_receipt_page(token):
    """Serve the mobile receipt submission page."""
    return send_from_directory(app.static_folder, 'receipt.html')

# ─── Error handlers & boot ────────────────────────────────────────────────────
@app.errorhandler(404)
def not_found(e):         return jsonify({'error':'Not found'}),404
@app.errorhandler(405)
def method_not_allowed(e): return jsonify({'error':'Method not allowed'}),405
@app.errorhandler(500)
def server_error(e):      return jsonify({'error':f'Server error: {str(e)}'}),500

_db_ready = False
@app.before_request
def ensure_db():
    global _db_ready
    if not _db_ready:
        try:
            init_db()
            _db_ready = True
        except Exception as e:
            print(f"[INIT_DB ERROR] {e}")
            # Don't set _db_ready=True so it retries next request
            # But do let the request proceed so the error is visible

@app.route('/', defaults={'path':''})
@app.route('/<path:path>')
def serve(path):
    if path.startswith('api/'): return jsonify({'error':'Not found'}),404
    if path and os.path.exists(os.path.join(app.static_folder,path)):
        return send_from_directory(app.static_folder,path)
    return send_from_directory(app.static_folder,'index.html')

if __name__ == '__main__':
    init_db()
    port = int(os.environ.get('PORT',5001))
    print(f"\n🎭 BloomBooks — http://localhost:{port}\n")
    app.run(host='0.0.0.0',port=port,debug=False)
