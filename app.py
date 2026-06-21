import os
import uuid
import io
import struct
import zlib
import functools
import pandas as pd
from flask import Flask, render_template, request, jsonify, session, send_file, Response
from db import upload_dataframe, get_tables, get_schema, execute_query, delete_table
from executor import execute_with_retry
from analytics import init_db, log_session, log_query, log_table_upload, get_stats
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "querymind-secret-2025")

FREE_QUERY_LIMIT = 3

init_db()


# ── helpers ──────────────────────────────────────────────
def get_session_id():
    if "session_id" not in session:
        session["session_id"] = str(uuid.uuid4())
    return session["session_id"]


def get_client_ip():
    return request.headers.get("X-Forwarded-For", request.remote_addr or "unknown").split(",")[0].strip()


def admin_required(f):
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        pwd = os.environ.get("ADMIN_PASSWORD", "")
        provided = request.args.get("key", "") or request.headers.get("X-Admin-Key", "")
        if not pwd or provided != pwd:
            return "Unauthorized", 401
        return f(*args, **kwargs)
    return wrapper


# ── routes ───────────────────────────────────────────────
@app.route("/ping")
def ping():
    return "OK", 200


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/config", methods=["GET"])
def config():
    has_backend_key = bool(os.environ.get("GROQ_API_KEY", "").strip())
    return jsonify({
        "has_backend_key": has_backend_key,
        "free_limit": FREE_QUERY_LIMIT
    })


@app.route("/api/upload", methods=["POST"])
def upload():
    session_id = get_session_id()
    log_session(session_id, get_client_ip())

    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400

    file = request.files["file"]
    table_name = request.form.get("table_name", "").strip()

    if not file.filename:
        return jsonify({"error": "No file selected"}), 400

    filename = file.filename.lower()

    try:
        if filename.endswith(".csv"):
            df = pd.read_csv(file)
        elif filename.endswith((".xlsx", ".xls")):
            df = pd.read_excel(file)
        else:
            return jsonify({"error": "Only CSV and Excel files are supported"}), 400

        if not table_name:
            base = os.path.splitext(file.filename)[0]
            table_name = "".join(c if c.isalnum() or c == "_" else "_" for c in base).lower()

        rows, cols = df.shape
        upload_dataframe(session_id, table_name, df)
        log_table_upload(session_id, table_name)

        preview = df.head(5).fillna("").astype(str).to_dict(orient="records")

        return jsonify({
            "success": True,
            "table_name": table_name,
            "rows": rows,
            "cols": cols,
            "columns": list(df.columns),
            "preview": preview
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/tables", methods=["GET"])
def tables():
    session_id = get_session_id()
    return jsonify({
        "tables": get_tables(session_id),
        "schema": get_schema(session_id)
    })


@app.route("/api/query", methods=["POST"])
def query():
    session_id = get_session_id()
    log_session(session_id, get_client_ip())

    data        = request.get_json()
    question    = data.get("question", "").strip()
    user_key    = data.get("api_key", "").strip()
    backend_key = os.environ.get("GROQ_API_KEY", "").strip()

    if not question:
        return jsonify({"error": "Question is required"}), 400

    # ── free query limit ──────────────────────────────────
    queries_used = session.get("query_count", 0)
    using_own_key = bool(user_key)

    if not using_own_key:
        if not backend_key:
            return jsonify({"error": "No API key configured.", "limit_reached": True}), 400
        if queries_used >= FREE_QUERY_LIMIT:
            return jsonify({
                "error": f"You've used all {FREE_QUERY_LIMIT} free queries.",
                "limit_reached": True,
                "queries_used": queries_used
            }), 429

    api_key = user_key or backend_key

    schema = get_schema(session_id)
    if not schema:
        return jsonify({"error": "No tables found. Please upload a CSV or Excel file first."}), 400

    result_data, final_query, error = execute_with_retry(session_id, schema, question, api_key)

    # increment counter only for backend-key queries
    if not using_own_key:
        session["query_count"] = queries_used + 1
        session.modified = True

    success = error is None
    log_query(session_id, question, final_query, success, used_own_key=using_own_key)

    if error:
        return jsonify({"sql": final_query, "error": error})

    columns, rows = result_data

    safe_rows = []
    for row in rows:
        safe_row = []
        for val in row:
            if val is None:
                safe_row.append(None)
            elif hasattr(val, "item"):
                safe_row.append(val.item())
            elif isinstance(val, (int, float, bool)):
                safe_row.append(val)
            else:
                safe_row.append(str(val))
        safe_rows.append(safe_row)

    remaining = None if using_own_key else max(0, FREE_QUERY_LIMIT - session.get("query_count", 0))

    return jsonify({
        "sql": final_query,
        "columns": columns,
        "rows": safe_rows,
        "count": len(safe_rows),
        "queries_used": session.get("query_count", 0),
        "queries_remaining": remaining,
        "using_own_key": using_own_key
    })


@app.route("/api/export", methods=["POST"])
def export():
    data    = request.get_json()
    columns = data.get("columns", [])
    rows    = data.get("rows", [])

    df  = pd.DataFrame(rows, columns=columns)
    buf = io.StringIO()
    df.to_csv(buf, index=False)
    buf.seek(0)

    return send_file(
        io.BytesIO(buf.getvalue().encode()),
        mimetype="text/csv",
        as_attachment=True,
        download_name="querymind_export.csv"
    )


@app.route("/api/delete_table", methods=["POST"])
def remove_table():
    session_id = get_session_id()
    data = request.get_json()
    delete_table(session_id, data.get("table_name", ""))
    return jsonify({"success": True})


@app.route("/api/clear", methods=["POST"])
def clear_session_data():
    session_id = get_session_id()
    from db import clear_session
    clear_session(session_id)
    session.clear()
    return jsonify({"success": True})


# ── PWA routes ────────────────────────────────────────────
@app.route("/manifest.json")
def manifest():
    return jsonify({
        "name": "QueryMind",
        "short_name": "QueryMind",
        "description": "AI-Powered Natural Language to SQL Engine by Pratham Soni",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#010b18",
        "theme_color": "#3b82f6",
        "orientation": "any",
        "icons": [
            {"src": "/app-icon.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
            {"src": "/app-icon.png", "sizes": "512x512", "type": "image/png", "purpose": "any"}
        ]
    })


@app.route("/sw.js")
def service_worker():
    js = """
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', e => e.respondWith(fetch(e.request).catch(() => caches.match(e.request))));
"""
    return Response(js, mimetype="application/javascript")


@app.route("/app-icon.png")
def app_icon():
    W = H = 192
    rows = []
    for y in range(H):
        row = [0]
        for x in range(W):
            t = (x + y) / (W + H - 2)
            r = int(29  + t * (124 - 29))
            g = int(78  + t * (58  - 78))
            b = int(216 + t * (237 - 216))
            row += [r, g, b, 255]
        rows.append(bytes(row))
    raw  = b''.join(rows)
    idat = zlib.compress(raw, 6)
    def chunk(tag, data):
        c = tag + data
        return struct.pack('>I', len(data)) + tag + data + struct.pack('>I', zlib.crc32(c) & 0xFFFFFFFF)
    ihdr = struct.pack('>IIBBBBB', W, H, 8, 6, 0, 0, 0)
    png  = b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', ihdr) + chunk(b'IDAT', idat) + chunk(b'IEND', b'')
    return Response(png, mimetype='image/png', headers={'Cache-Control': 'public, max-age=86400'})


# ── admin ─────────────────────────────────────────────────
@app.route("/admin")
@admin_required
def admin():
    return render_template("admin.html", stats=get_stats(), free_limit=FREE_QUERY_LIMIT)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
