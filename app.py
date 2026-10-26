from flask import Flask, render_template, request, jsonify, session
from flask_cors import CORS
import os, json, uuid, hashlib, threading, time
from datetime import datetime, date, timedelta
try:
    import pytz
    IST = pytz.timezone("Asia/Kolkata")
    def now_ist():
        return datetime.now(IST)
    def today_ist():
        return datetime.now(IST).date()
except ImportError:
    def now_ist():
        return datetime.utcnow() + timedelta(hours=5, minutes=30)
    def today_ist():
        return (datetime.utcnow() + timedelta(hours=5, minutes=30)).date()

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "stakedx-secret-2025-xK9mP3rZ")
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=(os.environ.get("RAILWAY_ENVIRONMENT") == "production") or bool(os.environ.get("RENDER")),
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
)
CORS(app, supports_credentials=True, origins=["*"])

DATABASE_URL = os.environ.get("DATABASE_URL", "")

# ─── DB CONNECTION ────────────────────────────────────────────────────────────
# For PostgreSQL: use a simple per-request connection (psycopg2 is not thread-safe to share)
# For SQLite: use thread-local connections to avoid opening/closing per query

_sqlite_local = threading.local()

def get_db():
    if DATABASE_URL:
        import psycopg2, psycopg2.extras
        conn = psycopg2.connect(DATABASE_URL)
        conn.autocommit = False
        return conn, "pg"
    else:
        import sqlite3
        conn = getattr(_sqlite_local, "conn", None)
        if conn is None:
            conn = sqlite3.connect("stakedx.db", check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA cache_size=4000")
            _sqlite_local.conn = conn
        return conn, "sqlite"

def get_db_fresh():
    """Always returns a fresh connection (used inside background threads)."""
    if DATABASE_URL:
        import psycopg2, psycopg2.extras
        conn = psycopg2.connect(DATABASE_URL)
        conn.autocommit = False
        return conn, "pg"
    else:
        import sqlite3
        conn = sqlite3.connect("stakedx.db")
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn, "sqlite"

def db_execute(conn, db_type, sql, params=()):
    if db_type == "pg":
        import psycopg2.extras
        sql = sql.replace("?", "%s")
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    else:
        cur = conn.cursor()
    cur.execute(sql, params)
    return cur

def rows_to_list(rows, db_type):
    return [dict(r) for r in rows] if rows else []

def row_to_dict(row, db_type):
    if row is None: return None
    return dict(row)

def init_db():
    conn, db_type = get_db_fresh()
    try:
        tables = [
            """CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL, name TEXT, password TEXT,
                balance REAL DEFAULT 0, invested REAL DEFAULT 0, profit REAL DEFAULT 0,
                ref_code TEXT, referred_by TEXT, joined TEXT, status TEXT DEFAULT 'active',
                auth_provider TEXT DEFAULT 'google', referral_applied INTEGER DEFAULT 0)""",
            """CREATE TABLE IF NOT EXISTS transactions (
                id TEXT PRIMARY KEY, user_email TEXT, type TEXT, amount REAL,
                network TEXT, ref TEXT, tx_hash TEXT, address TEXT,
                status TEXT DEFAULT 'pending', date TEXT)""",
            """CREATE TABLE IF NOT EXISTS stakes (
                id TEXT PRIMARY KEY, user_email TEXT, plan_id INTEGER, plan TEXT, emoji TEXT,
                amount REAL, daily REAL, period INTEGER, daily_earn REAL, earned REAL DEFAULT 0,
                start TEXT, start_date TEXT, days_left INTEGER, last_credited TEXT,
                status TEXT DEFAULT 'active')""",
            """CREATE TABLE IF NOT EXISTS profit_log (
                id TEXT PRIMARY KEY, user_email TEXT, stake_id TEXT, amount REAL, date TEXT)""",
            """CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY, value TEXT)""",
        ]
        for stmt in tables:
            try:
                db_execute(conn, db_type, stmt, ())
                conn.commit()
            except Exception as e:
                print(f"[init_db] table create warning: {e}")
                try: conn.rollback()
                except: pass

        # Add indexes for performance
        indexes = [
            "CREATE INDEX IF NOT EXISTS idx_stakes_email_status ON stakes(user_email, status)",
            "CREATE INDEX IF NOT EXISTS idx_txs_email_date ON transactions(user_email, date DESC)",
            "CREATE INDEX IF NOT EXISTS idx_txs_type_status ON transactions(type, status)",
            "CREATE INDEX IF NOT EXISTS idx_profit_log_email_date ON profit_log(user_email, date)",
            "CREATE INDEX IF NOT EXISTS idx_users_ref_code ON users(ref_code)",
        ]
        for idx in indexes:
            try:
                db_execute(conn, db_type, idx, ())
                conn.commit()
            except Exception:
                try: conn.rollback()
                except: pass

        migrations = [
            "ALTER TABLE users ADD COLUMN referred_by TEXT",
            "ALTER TABLE users ADD COLUMN referral_applied INTEGER DEFAULT 0",
            "ALTER TABLE transactions ADD COLUMN tx_hash TEXT",
        ]
        for col_sql in migrations:
            try:
                db_execute(conn, db_type, col_sql, ())
                conn.commit()
            except Exception:
                try: conn.rollback()
                except: pass
    finally:
        conn.close()

init_db()

PLANS = [
    {"id":0,"label":"Basic","emoji":"🌱","daily":0.8,"period":45,"min":50,"max":99,"color":"#4ade80","bg":"linear-gradient(135deg,#052e16,#14532d,#16a34a)","desc":"Low entry, steady daily returns for beginners"},
    {"id":1,"label":"Starter","emoji":"🥉","daily":1.2,"period":30,"min":100,"max":499,"color":"#cd7f32","bg":"linear-gradient(135deg,#3d2414,#7c4012,#cd7f32)","desc":"Perfect for first-time investors"},
    {"id":2,"label":"Silver","emoji":"🥈","daily":1.5,"period":25,"min":200,"max":999,"color":"#9ca3af","hot":True,"bg":"linear-gradient(135deg,#1f2937,#374151,#6b7280)","desc":"Entry-level plan for new investors"},
    {"id":3,"label":"Gold","emoji":"🏆","daily":2.0,"period":20,"min":500,"max":4999,"color":"#f59e0b","bg":"linear-gradient(135deg,#1c1400,#78350f,#d97706)","desc":"Balanced returns for growing investors"},
    {"id":4,"label":"Elite","emoji":"💎","daily":2.5,"period":15,"min":1000,"max":100000,"color":"#8b5cf6","bg":"linear-gradient(135deg,#1e0a3c,#4c1d95,#7c3aed)","desc":"Maximum returns for elite investors"},
]

DEFAULT_WALLETS = {
    "TRC20": os.environ.get("WALLET_TRC20","TQn9Y2khEsLJW1ChVWFMSMeRDow5KcbLSE"),
    "BEP20": os.environ.get("WALLET_BEP20","0x742d35Cc6634C0532925a3b844Bc454e4438f44e"),
}

DEFAULT_SETTINGS = {
    "telegram_link": "https://t.me/stakedx_support",
    "contact_text": "Contact Support",
}

ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL","admin@stakedx.com")
ADMIN_PASS  = os.environ.get("ADMIN_PASS","admin123")

NETWORK_FEES = {"TRC20": 1.0, "BEP20": 0.0}

# ─── SETTINGS CACHE ──────────────────────────────────────────────────────────
_settings_cache = {}
_settings_cache_ts = {}
_CACHE_TTL = 60  # seconds

def _ensure_app_settings(conn, db_type):
    try:
        db_execute(conn, db_type,
            "CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT)", ())
        conn.commit()
    except Exception:
        try: conn.rollback()
        except: pass

def get_setting(key, default=""):
    now = time.time()
    if key in _settings_cache and (now - _settings_cache_ts.get(key, 0)) < _CACHE_TTL:
        return _settings_cache[key]
    conn, db_type = get_db()
    try:
        try:
            cur = db_execute(conn, db_type, "SELECT value FROM app_settings WHERE key=?", (key,))
            r = cur.fetchone()
            val = dict(r)["value"] if r else default
            _settings_cache[key] = val
            _settings_cache_ts[key] = now
            return val
        except Exception:
            try: conn.rollback()
            except: pass
            _ensure_app_settings(conn, db_type)
            return default
    finally:
        if DATABASE_URL: conn.close()

def set_setting(key, value):
    conn, db_type = get_db()
    try:
        _ensure_app_settings(conn, db_type)
        if db_type == "pg":
            db_execute(conn, db_type,
                "INSERT INTO app_settings (key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=?",
                (key, value, value))
        else:
            db_execute(conn, db_type,
                "INSERT OR REPLACE INTO app_settings (key,value) VALUES (?,?)", (key, value))
        conn.commit()
        # Invalidate cache
        _settings_cache[key] = value
        _settings_cache_ts[key] = time.time()
    finally:
        if DATABASE_URL: conn.close()

def get_wallets():
    return {
        "TRC20": get_setting("wallet_trc20", DEFAULT_WALLETS["TRC20"]),
        "BEP20": get_setting("wallet_bep20", DEFAULT_WALLETS["BEP20"]),
    }

def hash_pass(p):
    return hashlib.sha256(p.encode()).hexdigest()

def _calc_live_balance(email, conn, db_type):
    today = today_ist().isoformat()
    cur = db_execute(conn, db_type,
        "SELECT daily_earn, last_credited FROM stakes WHERE user_email=? AND status='active'", (email,))
    stakes = rows_to_list(cur.fetchall(), db_type)
    live = 0.0
    for s in stakes:
        if s.get("last_credited") != today:
            live += float(s.get("daily_earn") or 0)
    return round(live, 6)

def _safe_user(email):
    conn, db_type = get_db()
    try:
        # Single query to get user + stakes + recent txs in one round-trip
        cur = db_execute(conn, db_type, "SELECT * FROM users WHERE email=?", (email,))
        u = cur.fetchone()
        if not u: return {}
        cur2 = db_execute(conn, db_type,
            "SELECT * FROM stakes WHERE user_email=? ORDER BY start_date DESC", (email,))
        cur3 = db_execute(conn, db_type,
            "SELECT * FROM transactions WHERE user_email=? ORDER BY date DESC LIMIT 50", (email,))
        d = row_to_dict(u, db_type)
        d.pop("password", None)
        d["stakes"] = rows_to_list(cur2.fetchall(), db_type)
        d["txs"]    = rows_to_list(cur3.fetchall(), db_type)
        live_income = _calc_live_balance(email, conn, db_type)
        d["live_today_income"] = live_income
        d["display_balance"] = round(float(d.get("balance", 0)) + live_income, 6)
        return d
    finally:
        if DATABASE_URL: conn.close()

def process_daily_profits():
    conn, db_type = get_db_fresh()
    try:
        today = today_ist().isoformat()
        cur = db_execute(conn, db_type, "SELECT * FROM stakes WHERE status='active'", ())
        stakes = rows_to_list(cur.fetchall(), db_type)
        for s in stakes:
            if s.get("last_credited") == today: continue
            email      = s["user_email"]
            daily_earn = float(s.get("daily_earn") or 0)
            days_left  = int(s.get("days_left") or 0)
            earned     = float(s.get("earned") or 0)
            principal  = float(s.get("amount") or 0)
            last_cr    = s.get("last_credited") or s.get("start_date") or today
            try:
                missed_days = (datetime.strptime(today,"%Y-%m-%d").date() - datetime.strptime(last_cr,"%Y-%m-%d").date()).days
            except: missed_days = 1
            if missed_days <= 0: continue
            missed_days = min(missed_days, days_left)
            total_credit  = round(daily_earn * missed_days, 6)
            new_earned    = round(earned + total_credit, 6)
            new_days_left = days_left - missed_days
            if new_days_left <= 0:
                db_execute(conn, db_type,
                    "UPDATE stakes SET earned=?,days_left=0,last_credited=?,status='completed' WHERE id=?",
                    (new_earned, today, s["id"]))
                db_execute(conn, db_type,
                    "UPDATE users SET balance=balance+?,profit=profit+?,invested=CASE WHEN invested>=? THEN invested-? ELSE 0 END WHERE email=?",
                    (total_credit+principal, total_credit, principal, principal, email))
                tx_id = "TX"+str(uuid.uuid4())[:8].upper()
                db_execute(conn, db_type,
                    "INSERT INTO transactions (id,user_email,type,amount,network,status,date) VALUES (?,?,?,?,?,?,?)",
                    (tx_id, email, "earnings", round(total_credit+principal,6), "—", "completed",
                     now_ist().strftime("%b %d, %Y · %I:%M %p")))
            else:
                db_execute(conn, db_type,
                    "UPDATE stakes SET earned=?,days_left=?,last_credited=? WHERE id=?",
                    (new_earned, new_days_left, today, s["id"]))
                db_execute(conn, db_type,
                    "UPDATE users SET balance=balance+?,profit=profit+? WHERE email=?",
                    (total_credit, total_credit, email))
            for i in range(missed_days):
                log_date = (datetime.strptime(today,"%Y-%m-%d") - timedelta(days=missed_days-1-i)).strftime("%Y-%m-%d")
                db_execute(conn, db_type,
                    "INSERT INTO profit_log (id,user_email,stake_id,amount,date) VALUES (?,?,?,?,?)",
                    (str(uuid.uuid4()), email, s["id"], daily_earn, log_date))
        conn.commit()
        print(f"[CRON] Daily profits processed at {now_ist()}")
    except Exception as e:
        print(f"[CRON] Error: {e}")
        try: conn.rollback()
        except: pass
    finally:
        conn.close()

def _process_user_daily_profit(email):
    conn, db_type = get_db()
    try:
        today = today_ist().isoformat()
        cur = db_execute(conn, db_type,
            "SELECT * FROM stakes WHERE user_email=? AND status='active'", (email,))
        stakes = rows_to_list(cur.fetchall(), db_type)
        if not stakes: return
        credited_any = False
        for s in stakes:
            if s.get("last_credited") == today: continue
            daily_earn = float(s.get("daily_earn") or 0)
            days_left  = int(s.get("days_left") or 0)
            principal  = float(s.get("amount") or 0)
            if days_left <= 1:
                credit = daily_earn + principal
                db_execute(conn, db_type,
                    "UPDATE stakes SET last_credited=?,earned=earned+?,days_left=0,status='completed' WHERE id=?",
                    (today, daily_earn, s["id"]))
                db_execute(conn, db_type,
                    "UPDATE users SET balance=balance+?,profit=profit+?,invested=CASE WHEN invested>=? THEN invested-? ELSE 0 END WHERE email=?",
                    (credit, daily_earn, principal, principal, email))
            else:
                credit = daily_earn
                db_execute(conn, db_type,
                    "UPDATE stakes SET last_credited=?,earned=earned+?,days_left=days_left-1 WHERE id=?",
                    (today, daily_earn, s["id"]))
                db_execute(conn, db_type,
                    "UPDATE users SET balance=balance+?,profit=profit+? WHERE email=?",
                    (credit, daily_earn, email))
            credited_any = True
            try:
                db_execute(conn, db_type,
                    "INSERT INTO profit_log (id,user_email,amount,date) VALUES (?,?,?,?)",
                    ("PL"+str(uuid.uuid4())[:8].upper(), email, daily_earn, today))
            except: pass
            try:
                db_execute(conn, db_type,
                    "INSERT INTO transactions (id,user_email,type,amount,network,status,date) VALUES (?,?,?,?,?,?,?)",
                    ("TX"+str(uuid.uuid4())[:8].upper(), email, "earnings", daily_earn, "—", "completed",
                     now_ist().strftime("%b %d, %Y · %I:%M %p")))
            except: pass
        if credited_any:
            conn.commit()
    except Exception as e:
        print(f"[USER_PROFIT] Error for {email}: {e}")
        try: conn.rollback()
        except: pass
    finally:
        if DATABASE_URL: conn.close()

def start_cron():
    def _run():
        try: process_daily_profits()
        except Exception as e: print(f"[CRON] Startup failed: {e}")
        while True:
            time.sleep(3600)
            try: process_daily_profits()
            except Exception as e: print(f"[CRON] Failed: {e}")
    threading.Thread(target=_run, daemon=True).start()

start_cron()

# ════════════════════════════════════════
# ROUTES
# ════════════════════════════════════════
@app.route("/")
def index():
    wallets = get_wallets()
    return render_template("index.html", plans=json.dumps(PLANS), wallets=json.dumps(wallets))

@app.route("/admin")
def admin_page():
    if session.get("role") != "admin":
        return render_template("admin_login.html")
    return render_template("admin.html")

@app.route("/api/google_auth", methods=["POST"])
def google_auth():
    d = request.json
    email = d.get("email","").lower().strip()
    name  = d.get("name","")
    ref_input = d.get("referral_code","").strip().upper()
    if not email: return jsonify({"ok":False,"msg":"Email missing"}),400
    conn, db_type = get_db()
    try:
        cur = db_execute(conn, db_type, "SELECT * FROM users WHERE email=?", (email,))
        u = cur.fetchone()
        is_new = not u
        if is_new:
            uid = str(uuid.uuid4())
            ref_code = "STDK-"+str(uuid.uuid4())[:8].upper()
            joined = now_ist().strftime("%b %Y")
            referred_by = None
            if ref_input:
                cur2 = db_execute(conn, db_type, "SELECT email FROM users WHERE ref_code=?", (ref_input,))
                ref_user = cur2.fetchone()
                if ref_user:
                    rr = dict(ref_user)["email"]
                    if rr != email: referred_by = rr
            db_execute(conn, db_type,
                "INSERT INTO users (id,email,name,password,balance,invested,profit,ref_code,referred_by,joined,status,auth_provider,referral_applied) VALUES (?,?,?,?,0,0,0,?,?,?,?,?,?)",
                (uid,email,name or email.split("@")[0],"",ref_code,referred_by,joined,"active","google",1 if referred_by else 0))
            conn.commit()
        else:
            u_dict = row_to_dict(u, db_type)
            if u_dict.get("status") == "suspended":
                return jsonify({"ok":False,"msg":"Account suspended"}),403
        session["email"]=email; session["role"]="user"
        user_data = _safe_user(email)
        user_data["needs_referral"] = not user_data.get("referred_by") and not user_data.get("referral_applied")
        return jsonify({"ok":True,"role":"user","user":user_data,"is_new":is_new})
    finally:
        if DATABASE_URL: conn.close()

@app.route("/api/apply_referral", methods=["POST"])
def apply_referral():
    email = session.get("email")
    if not email: return jsonify({"ok":False,"msg":"Not authenticated"}),401
    ref_code = request.json.get("code","").strip().upper()
    if not ref_code: return jsonify({"ok":False,"msg":"Please enter referral code"}),400
    conn, db_type = get_db()
    try:
        cur = db_execute(conn, db_type, "SELECT referred_by,referral_applied FROM users WHERE email=?", (email,))
        u = row_to_dict(cur.fetchone(), db_type)
        if not u: return jsonify({"ok":False,"msg":"User not found"}),404
        if u.get("referral_applied"): return jsonify({"ok":False,"msg":"Referral code already applied"}),400
        if u.get("referred_by"): return jsonify({"ok":False,"msg":"Referral already set"}),400
        cur2 = db_execute(conn, db_type, "SELECT email FROM users WHERE ref_code=?", (ref_code,))
        ref_user = cur2.fetchone()
        if not ref_user: return jsonify({"ok":False,"msg":"Invalid referral code"}),400
        ref_email = dict(ref_user)["email"]
        if ref_email == email: return jsonify({"ok":False,"msg":"Cannot use your own code"}),400
        db_execute(conn, db_type,
            "UPDATE users SET referred_by=?,referral_applied=1 WHERE email=?", (ref_email, email))
        conn.commit()
    finally:
        if DATABASE_URL: conn.close()
    return jsonify({"ok":True,"msg":"Referral applied! You'll earn bonus on your next deposit."})

@app.route("/api/admin_login", methods=["POST"])
def admin_login():
    d = request.json
    if d.get("password","") == ADMIN_PASS:
        session["email"]=ADMIN_EMAIL; session["role"]="admin"
        return jsonify({"ok":True})
    return jsonify({"ok":False,"msg":"Invalid credentials"}),401

@app.route("/api/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"ok":True})

@app.route("/api/me")
def me():
    email = session.get("email")
    if not email: return jsonify({"ok":False}),401
    if session.get("role")=="admin": return jsonify({"ok":True,"role":"admin"})
    return jsonify({"ok":True,"role":"user","user":_safe_user(email)})

@app.route("/api/deposit", methods=["POST"])
def deposit():
    email = session.get("email")
    if not email or session.get("role")!="user": return jsonify({"ok":False,"msg":"Not authenticated"}),401
    d = request.json
    try: amt = float(d.get("amount",0))
    except: return jsonify({"ok":False,"msg":"Invalid amount"}),400
    net     = d.get("network","TRC20")
    tx_hash = d.get("tx_hash","").strip()
    if amt < 10: return jsonify({"ok":False,"msg":"Minimum deposit is $10"}),400
    if not tx_hash: return jsonify({"ok":False,"msg":"Transaction ID is required"}),400
    conn, db_type = get_db()
    try:
        cur0 = db_execute(conn, db_type, "SELECT status FROM users WHERE email=?", (email,))
        u0 = row_to_dict(cur0.fetchone(), db_type)
        if u0 and u0.get("status") == "suspended":
            return jsonify({"ok":False,"msg":"Account suspended"}),403
        cur = db_execute(conn, db_type,
            "SELECT id FROM transactions WHERE tx_hash=? AND type='deposit'", (tx_hash,))
        if cur.fetchone():
            return jsonify({"ok":False,"msg":"This Transaction ID already submitted. Duplicate not allowed."}),400
        tx_id = "TX"+str(uuid.uuid4())[:8].upper()
        dt = now_ist().strftime("%b %d, %Y · %I:%M %p")
        db_execute(conn, db_type,
            "INSERT INTO transactions (id,user_email,type,amount,network,tx_hash,status,date) VALUES (?,?,?,?,?,?,?,?)",
            (tx_id,email,"deposit",amt,net,tx_hash,"pending",dt))
        conn.commit()
    finally:
        if DATABASE_URL: conn.close()
    return jsonify({"ok":True,"tx":{"id":tx_id,"user_email":email,"type":"deposit","amount":amt,"network":net,"tx_hash":tx_hash,"status":"pending","date":dt}})

@app.route("/api/withdraw", methods=["POST"])
def withdraw():
    email = session.get("email")
    if not email or session.get("role")!="user": return jsonify({"ok":False,"msg":"Not authenticated"}),401
    d = request.json
    amt  = float(d.get("amount",0))
    addr = d.get("address","").strip()
    net  = d.get("network","TRC20")
    fee  = NETWORK_FEES.get(net, 0.0)
    total_needed = round(amt + fee, 6)
    if amt < 10: return jsonify({"ok":False,"msg":"Minimum withdrawal is $10"}),400
    if not addr or len(addr)<10: return jsonify({"ok":False,"msg":"Valid wallet address required"}),400
    conn, db_type = get_db()
    try:
        cur0 = db_execute(conn, db_type, "SELECT status FROM users WHERE email=?", (email,))
        u0 = row_to_dict(cur0.fetchone(), db_type)
        if u0 and u0.get("status") == "suspended":
            return jsonify({"ok":False,"msg":"Account suspended"}),403
        today = today_ist().isoformat()
        cur2 = db_execute(conn, db_type,
            "SELECT * FROM stakes WHERE user_email=? AND status='active'", (email,))
        for s in rows_to_list(cur2.fetchall(), db_type):
            if s.get("last_credited") != today:
                de = float(s.get("daily_earn") or 0)
                ne = float(s.get("earned") or 0) + de
                nd = int(s.get("days_left") or 0) - 1
                pr = float(s.get("amount") or 0)
                if nd <= 0:
                    db_execute(conn, db_type,
                        "UPDATE stakes SET earned=?,days_left=0,last_credited=?,status='completed' WHERE id=?",
                        (ne, today, s["id"]))
                    db_execute(conn, db_type,
                        "UPDATE users SET balance=balance+?,profit=profit+?,invested=CASE WHEN invested>=? THEN invested-? ELSE 0 END WHERE email=?",
                        (de+pr, de, pr, pr, email))
                else:
                    db_execute(conn, db_type,
                        "UPDATE stakes SET earned=?,days_left=?,last_credited=? WHERE id=?",
                        (ne, nd, today, s["id"]))
                    db_execute(conn, db_type,
                        "UPDATE users SET balance=balance+?,profit=profit+? WHERE email=?",
                        (de, de, email))
                db_execute(conn, db_type,
                    "INSERT INTO profit_log (id,user_email,stake_id,amount,date) VALUES (?,?,?,?,?)",
                    (str(uuid.uuid4()), email, s["id"], de, today))
        conn.commit()

        if db_type == "pg":
            cur3 = db_execute(conn, db_type,
                "UPDATE users SET balance=balance-? WHERE email=? AND balance>=? RETURNING balance",
                (total_needed, email, total_needed))
            row = cur3.fetchone()
            if not row:
                conn.rollback()
                cur_chk = db_execute(conn, db_type, "SELECT balance FROM users WHERE email=?", (email,))
                u_chk = row_to_dict(cur_chk.fetchone(), db_type)
                avail = float(u_chk["balance"]) if u_chk else 0
                fee_msg = f" (incl. ${fee:.0f} {net} fee)" if fee > 0 else ""
                return jsonify({"ok":False,"msg":f"Need ${total_needed:.2f}{fee_msg}. Available: ${avail:.2f}"}),400
            new_bal = round(float(dict(row)["balance"]), 6)
        else:
            cur_chk = db_execute(conn, db_type, "SELECT balance FROM users WHERE email=?", (email,))
            u_chk = row_to_dict(cur_chk.fetchone(), db_type)
            if not u_chk:
                return jsonify({"ok":False,"msg":"User not found"}),404
            avail = round(float(u_chk["balance"]), 6)
            if avail < total_needed:
                fee_msg = f" (incl. ${fee:.0f} {net} fee)" if fee > 0 else ""
                return jsonify({"ok":False,"msg":f"Need ${total_needed:.2f}{fee_msg}. Available: ${avail:.2f}"}),400
            cur3 = db_execute(conn, db_type,
                "UPDATE users SET balance=balance-? WHERE email=? AND balance>=?",
                (total_needed, email, total_needed))
            if cur3.rowcount == 0:
                conn.rollback()
                return jsonify({"ok":False,"msg":"Insufficient balance. Please try again."}),400
            new_bal = round(avail - total_needed, 6)

        tx_id = "TX"+str(uuid.uuid4())[:8].upper()
        dt = now_ist().strftime("%b %d, %Y · %I:%M %p")
        db_execute(conn, db_type,
            "INSERT INTO transactions (id,user_email,type,amount,network,address,ref,status,date) VALUES (?,?,?,?,?,?,?,?,?)",
            (tx_id,email,"withdrawal",amt,net,addr,f"Fee:${fee:.0f}" if fee>0 else "","pending",dt))
        conn.commit()
    finally:
        if DATABASE_URL: conn.close()
    return jsonify({"ok":True,"tx":{"id":tx_id,"type":"withdrawal","amount":amt,"network":net,"fee":fee,"status":"pending","date":dt},"balance":new_bal})

@app.route("/api/stake", methods=["POST"])
def stake():
    email = session.get("email")
    if not email or session.get("role")!="user": return jsonify({"ok":False,"msg":"Not authenticated"}),401
    d = request.json
    plan_id = int(d.get("plan_id",1)); amt = float(d.get("amount",0))
    plan = next((p for p in PLANS if p["id"]==plan_id), None)
    if not plan: return jsonify({"ok":False,"msg":"Invalid plan"}),400
    if amt < plan["min"]: return jsonify({"ok":False,"msg":f"Minimum for {plan['label']} is ${plan['min']}"}),400
    if plan["max"]<100000 and amt>plan["max"]: return jsonify({"ok":False,"msg":f"Maximum for {plan['label']} is ${plan['max']}"}),400
    conn, db_type = get_db()
    try:
        cur = db_execute(conn, db_type, "SELECT * FROM users WHERE email=?", (email,))
        u = row_to_dict(cur.fetchone(), db_type)
        if not u: return jsonify({"ok":False,"msg":"User not found"}),404
        if u.get("status") == "suspended":
            return jsonify({"ok":False,"msg":"Account suspended"}),403
        live_income = _calc_live_balance(email, conn, db_type)
        total_avail = round(float(u["balance"]) + live_income, 6)
        if total_avail < amt:
            return jsonify({"ok":False,"msg":f"Insufficient balance. Available: ${total_avail:.2f}"}),400
        new_bal    = round(total_avail - amt, 6)
        new_inv    = round(float(u["invested"]) + amt, 6)
        daily_earn = round(amt * plan["daily"] / 100, 6)
        s_id  = str(uuid.uuid4())
        today = today_ist().isoformat()
        start = now_ist().strftime("%d %b %Y")
        db_execute(conn, db_type,
            "UPDATE users SET balance=balance+?-?,invested=?,profit=profit+? WHERE email=?",
            (live_income, amt, new_inv, live_income, email))
        yesterday = (today_ist() - timedelta(days=1)).isoformat()
        db_execute(conn, db_type,
            "INSERT INTO stakes (id,user_email,plan_id,plan,emoji,amount,daily,period,daily_earn,earned,start,start_date,days_left,last_credited,status) VALUES (?,?,?,?,?,?,?,?,?,0,?,?,?,?,?)",
            (s_id,email,plan_id,plan["label"],plan["emoji"],amt,plan["daily"],plan["period"],daily_earn,start,today,plan["period"],yesterday,"active"))
        tx_id = "TX"+str(uuid.uuid4())[:8].upper()
        dt = now_ist().strftime("%b %d, %Y · %I:%M %p")
        db_execute(conn, db_type,
            "INSERT INTO transactions (id,user_email,type,amount,network,status,date) VALUES (?,?,?,?,?,?,?)",
            (tx_id,email,"stake",amt,"—","active",dt))
        conn.commit()
    finally:
        if DATABASE_URL: conn.close()
    s_obj = {"id":s_id,"plan":plan["label"],"emoji":plan["emoji"],"amount":amt,"daily":plan["daily"],"period":plan["period"],"daily_earn":daily_earn,"earned":0,"start":start,"days_left":plan["period"],"status":"active","plan_id":plan_id}
    return jsonify({"ok":True,"stake":s_obj,"balance":new_bal})

@app.route("/api/profit_today")
def profit_today():
    email = session.get("email")
    if not email: return jsonify({"ok":False}),401
    today = today_ist().isoformat()
    try: _process_user_daily_profit(email)
    except Exception as e: print(f"[profit_today] auto-credit failed: {e}")
    conn, db_type = get_db()
    try:
        cur = db_execute(conn, db_type,
            "SELECT COALESCE(SUM(amount),0) as v FROM profit_log WHERE user_email=? AND date=?",
            (email, today))
        row = cur.fetchone()
        credited_today = float((dict(row) if row else {}).get("v", 0))
        live_income  = _calc_live_balance(email, conn, db_type)
        cur2 = db_execute(conn, db_type, "SELECT balance,profit FROM users WHERE email=?", (email,))
        u = row_to_dict(cur2.fetchone(), db_type)
        db_bal       = float(u["balance"] if u else 0)
        total_profit = float(u["profit"]  if u else 0)
        today_total  = round(credited_today + live_income, 6)
        display_bal  = round(db_bal + live_income, 6)
    finally:
        if DATABASE_URL: conn.close()
    return jsonify({"ok":True,"today":today_total,"total_profit":total_profit,"display_balance":display_bal,"live_income":live_income})

@app.route("/api/transactions")
def get_txs():
    email = session.get("email")
    if not email: return jsonify({"ok":False}),401
    conn, db_type = get_db()
    try:
        cur = db_execute(conn, db_type,
            "SELECT * FROM transactions WHERE user_email=? ORDER BY date DESC LIMIT 50", (email,))
        txs = rows_to_list(cur.fetchall(), db_type)
    finally:
        if DATABASE_URL: conn.close()
    return jsonify({"ok":True,"txs":txs})

@app.route("/api/plans")
def get_plans_api():
    return jsonify({"ok":True,"plans":PLANS})

@app.route("/api/wallets")
def get_wallets_api():
    return jsonify({"ok":True,"wallets":get_wallets()})

@app.route("/api/settings")
def get_settings_api():
    return jsonify({
        "ok": True,
        "telegram_link": get_setting("telegram_link", DEFAULT_SETTINGS["telegram_link"]),
        "contact_text":  get_setting("contact_text",  DEFAULT_SETTINGS["contact_text"]),
    })

# ════════════════════════════════════════
# ADMIN ROUTES
# ════════════════════════════════════════
@app.route("/api/admin/users")
def admin_users():
    if session.get("role")!="admin": return jsonify({"ok":False}),403
    conn, db_type = get_db()
    try:
        cur = db_execute(conn, db_type,
            "SELECT id,email,name,balance,invested,profit,ref_code,referred_by,joined,status,auth_provider,referral_applied FROM users", ())
        users = rows_to_list(cur.fetchall(), db_type)
    finally:
        if DATABASE_URL: conn.close()
    return jsonify({"ok":True,"users":users})

@app.route("/api/admin/transactions")
def admin_txs():
    if session.get("role")!="admin": return jsonify({"ok":False}),403
    conn, db_type = get_db()
    try:
        cur = db_execute(conn, db_type,
            "SELECT * FROM transactions ORDER BY date DESC", ())
        txs = rows_to_list(cur.fetchall(), db_type)
    finally:
        if DATABASE_URL: conn.close()
    return jsonify({"ok":True,"transactions":txs})

@app.route("/api/admin/stakes")
def admin_stakes():
    if session.get("role")!="admin": return jsonify({"ok":False}),403
    conn, db_type = get_db()
    try:
        cur = db_execute(conn, db_type,
            "SELECT * FROM stakes ORDER BY start_date DESC", ())
        stakes = rows_to_list(cur.fetchall(), db_type)
    finally:
        if DATABASE_URL: conn.close()
    return jsonify({"ok":True,"stakes":stakes})

@app.route("/api/admin/approve_tx", methods=["POST"])
def approve_tx():
    if session.get("role")!="admin": return jsonify({"ok":False}),403
    tid = request.json.get("tx_id")
    conn, db_type = get_db()
    try:
        cur = db_execute(conn, db_type, "SELECT * FROM transactions WHERE id=?", (tid,))
        tx = row_to_dict(cur.fetchone(), db_type)
        if not tx: return jsonify({"ok":False,"msg":"TX not found"}),404
        if tx.get("status") == "completed":
            return jsonify({"ok":False,"msg":"Transaction already approved"}),400
        db_execute(conn, db_type, "UPDATE transactions SET status='completed' WHERE id=?", (tid,))
        if tx["type"] == "deposit":
            db_execute(conn, db_type,
                "UPDATE users SET balance=balance+? WHERE email=?",
                (tx["amount"], tx["user_email"]))
            cur2 = db_execute(conn, db_type,
                "SELECT referred_by FROM users WHERE email=?", (tx["user_email"],))
            u2 = row_to_dict(cur2.fetchone(), db_type)
            if u2 and u2.get("referred_by"):
                ref_email = u2["referred_by"]
                bonus = round(tx["amount"] * 0.05, 6)
                db_execute(conn, db_type,
                    "UPDATE users SET balance=balance+?,profit=profit+? WHERE email=?",
                    (bonus, bonus, ref_email))
                ref_tx_id = "TX"+str(uuid.uuid4())[:8].upper()
                dt = now_ist().strftime("%b %d, %Y · %I:%M %p")
                db_execute(conn, db_type,
                    "INSERT INTO transactions (id,user_email,type,amount,network,ref,status,date) VALUES (?,?,?,?,?,?,?,?)",
                    (ref_tx_id, ref_email, "earnings", bonus, "—",
                     f"Referral bonus from {tx['user_email']}", "completed", dt))
        conn.commit()
    finally:
        if DATABASE_URL: conn.close()
    return jsonify({"ok":True})

@app.route("/api/admin/reject_tx", methods=["POST"])
def reject_tx():
    if session.get("role")!="admin": return jsonify({"ok":False}),403
    tid = request.json.get("tx_id")
    conn, db_type = get_db()
    try:
        cur = db_execute(conn, db_type, "SELECT * FROM transactions WHERE id=?", (tid,))
        tx = row_to_dict(cur.fetchone(), db_type)
        if not tx: return jsonify({"ok":False,"msg":"TX not found"}),404
        if tx["type"]=="withdrawal":
            fee = NETWORK_FEES.get(tx.get("network","TRC20"), 0.0)
            db_execute(conn, db_type,
                "UPDATE users SET balance=balance+? WHERE email=?",
                (tx["amount"]+fee, tx["user_email"]))
        db_execute(conn, db_type, "UPDATE transactions SET status='rejected' WHERE id=?", (tid,))
        conn.commit()
    finally:
        if DATABASE_URL: conn.close()
    return jsonify({"ok":True})

@app.route("/api/admin/toggle_user", methods=["POST"])
def toggle_user():
    if session.get("role")!="admin": return jsonify({"ok":False}),403
    email = request.json.get("email")
    conn, db_type = get_db()
    try:
        cur = db_execute(conn, db_type, "SELECT status FROM users WHERE email=?", (email,))
        u = row_to_dict(cur.fetchone(), db_type)
        if not u: return jsonify({"ok":False}),404
        new_status = "suspended" if u["status"]=="active" else "active"
        db_execute(conn, db_type, "UPDATE users SET status=? WHERE email=?", (new_status,email))
        conn.commit()
    finally:
        if DATABASE_URL: conn.close()
    return jsonify({"ok":True,"status":new_status})

@app.route("/api/admin/update_plan", methods=["POST"])
def update_plan():
    if session.get("role")!="admin": return jsonify({"ok":False}),403
    d = request.json; pid = int(d.get("id"))
    for p in PLANS:
        if p["id"]==pid:
            p["daily"]=float(d.get("daily",p["daily"]))
            p["period"]=int(d.get("period",p["period"]))
            p["label"]=d.get("label",p["label"])
            return jsonify({"ok":True,"plan":p})
    return jsonify({"ok":False}),404

@app.route("/api/admin/update_balance", methods=["POST"])
def update_balance():
    if session.get("role")!="admin": return jsonify({"ok":False}),403
    d = request.json; email = d.get("email"); amount = float(d.get("amount",0))
    conn, db_type = get_db()
    try:
        db_execute(conn, db_type, "UPDATE users SET balance=balance+? WHERE email=?", (amount,email))
        conn.commit()
        cur = db_execute(conn, db_type, "SELECT balance FROM users WHERE email=?", (email,))
        u = row_to_dict(cur.fetchone(), db_type)
    finally:
        if DATABASE_URL: conn.close()
    return jsonify({"ok":True,"balance":u["balance"] if u else 0})

@app.route("/api/admin/update_wallets", methods=["POST"])
def update_wallets_api():
    if session.get("role")!="admin": return jsonify({"ok":False}),403
    d = request.json
    if "trc20" in d: set_setting("wallet_trc20", d["trc20"].strip())
    if "bep20" in d: set_setting("wallet_bep20", d["bep20"].strip())
    return jsonify({"ok":True,"wallets":get_wallets()})

@app.route("/api/admin/upload_qr", methods=["POST"])
def upload_qr():
    if session.get("role")!="admin": return jsonify({"ok":False}),403
    d = request.json
    network   = d.get("network","").upper()
    image_b64 = d.get("image","").strip()
    if network not in ("TRC20","BEP20"):
        return jsonify({"ok":False,"msg":"Invalid network"}),400
    if not image_b64 or not image_b64.startswith("data:image"):
        return jsonify({"ok":False,"msg":"Invalid image data"}),400
    if len(image_b64) > 700000:
        return jsonify({"ok":False,"msg":"Image too large. Max ~500KB."}),400
    set_setting(f"qr_{network.lower()}", image_b64)
    return jsonify({"ok":True,"msg":f"{network} QR updated successfully"})

@app.route("/api/qr_codes")
def get_qr_codes():
    return jsonify({
        "ok":   True,
        "TRC20": get_setting("qr_trc20", ""),
        "BEP20": get_setting("qr_bep20", ""),
    })

@app.route("/api/admin/update_settings", methods=["POST"])
def update_settings_api():
    if session.get("role")!="admin": return jsonify({"ok":False}),403
    d = request.json
    if "telegram_link" in d: set_setting("telegram_link", d["telegram_link"].strip())
    if "contact_text"  in d: set_setting("contact_text",  d["contact_text"].strip())
    return jsonify({"ok":True})

@app.route("/api/admin/stats")
def admin_stats():
    if session.get("role")!="admin": return jsonify({"ok":False}),403
    conn, db_type = get_db()
    try:
        # Single query approach for all stats
        def _scalar(sql):
            cur = db_execute(conn, db_type, sql, ())
            r = cur.fetchone()
            return list(dict(r).values())[0] if r else 0
        total_dep  = float(_scalar("SELECT COALESCE(SUM(amount),0) FROM transactions WHERE type='deposit' AND status='completed'"))
        pending_w  = int(_scalar("SELECT COUNT(*) FROM transactions WHERE type='withdrawal' AND status='pending'"))
        pending_d  = int(_scalar("SELECT COUNT(*) FROM transactions WHERE type='deposit' AND status='pending'"))
        aum        = float(_scalar("SELECT COALESCE(SUM(invested),0) FROM users"))
        total_u    = int(_scalar("SELECT COUNT(*) FROM users"))
        active_u   = int(_scalar("SELECT COUNT(*) FROM users WHERE status='active'"))
        total_prof = float(_scalar("SELECT COALESCE(SUM(profit),0) FROM users"))
    finally:
        if DATABASE_URL: conn.close()
    return jsonify({"ok":True,"total_users":total_u,"active_users":active_u,
                    "total_deposited":total_dep,"pending_withdrawals":pending_w,
                    "pending_deposits":pending_d,"aum":aum,"total_profit":total_prof})

@app.route("/api/admin/run_profits", methods=["POST"])
def run_profits_manual():
    if session.get("role")!="admin": return jsonify({"ok":False}),403
    process_daily_profits()
    return jsonify({"ok":True,"msg":"Daily profits processed"})

if __name__ == "__main__":
    port = int(os.environ.get("PORT",5000))
    app.run(host="0.0.0.0", port=port, debug=False)
