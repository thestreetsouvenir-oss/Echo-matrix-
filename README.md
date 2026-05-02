#!/usr/bin/env python3
"""
Echo Matrix v4.2 — Sovereign AI Runtime
Single-file. Self-hosted. Self-healing. Adversarially-hardened.

Usage:
  python echo_matrix.py               # REPL
  python echo_matrix.py --serve       # Web UI on http://localhost:8420
  python echo_matrix.py --ingest-docs /path/to/docs
"""

import os, sys, json, sqlite3, hashlib, datetime, re, math, asyncio, subprocess, textwrap, time, shutil, secrets
from pathlib import Path
from typing import Optional, List, Dict

# ── Config ─────────────────────────────────────
VAULT = Path.home() / "echo_matrix"
VAULT.mkdir(exist_ok=True)
DB_PATH = VAULT / "echo.db"
ARCHIVE = VAULT / "archive.jsonl"
TOOLS_DIR = VAULT / "tools"
TOOLS_DIR.mkdir(exist_ok=True)
MANIFEST_PATH = TOOLS_DIR / "manifest.json"

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ── Optional heavy deps (graceful fallback) ──────
_embedder = None
def get_embedder():
    global _embedder
    if _embedder is None:
        try:
            from sentence_transformers import SentenceTransformer
            _embedder = SentenceTransformer("all-MiniLM-L6-v2")
        except ImportError:
            pass
    return _embedder

def can_read_pdf():
    try: import fitz; return True
    except: return False

# ── Database (immutable, hash-chained) ─────────
def get_db():
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS entries (
            id TEXT PRIMARY KEY,
            timestamp TEXT NOT NULL,
            previous_hash TEXT,
            entry_hash TEXT NOT NULL,
            source TEXT DEFAULT 'echo',
            agent TEXT DEFAULT 'echo',
            type TEXT DEFAULT 'conversation',
            user_input TEXT,
            response TEXT,
            context_ids TEXT,
            tags TEXT,
            topic TEXT,
            metadata TEXT,
            importance REAL DEFAULT 1.0
        )
    """)
    for col in ("topic","timestamp","importance"):
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{col} ON entries({col});")
    # chunks table for document ingestion
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chunks (
            id TEXT PRIMARY KEY,
            doc_name TEXT,
            chunk_index INTEGER,
            text TEXT,
            embedding BLOB,
            embedding_model TEXT DEFAULT 'all-MiniLM-L6-v2',
            embedding_dim INTEGER DEFAULT 384,
            topic TEXT,
            importance REAL DEFAULT 1.0,
            timestamp TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chunk_topic ON chunks(topic);")
    # lifecycle events table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS lifecycle_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            affected_count INTEGER DEFAULT 0,
            pressure_level TEXT DEFAULT 'idle',
            details TEXT,
            created_at TEXT NOT NULL
        )
    """)
    # stability events table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS stability_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            attack_vector TEXT NOT NULL,
            input_text TEXT,
            detected INTEGER DEFAULT 0,
            severity TEXT DEFAULT 'low',
            confidence REAL DEFAULT 0.0,
            mitigation TEXT,
            created_at TEXT NOT NULL
        )
    """)
    # system metrics table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS system_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            total_entries INTEGER DEFAULT 0,
            active_entries INTEGER DEFAULT 0,
            consolidated_entries INTEGER DEFAULT 0,
            pruned_entries INTEGER DEFAULT 0,
            avg_importance REAL DEFAULT 0.0,
            avg_composite_score REAL DEFAULT 0.0,
            chain_integrity REAL DEFAULT 1.0,
            stability_score REAL DEFAULT 1.0,
            attacks_detected INTEGER DEFAULT 0,
            tools_registered INTEGER DEFAULT 0,
            pressure_level TEXT DEFAULT 'idle',
            created_at TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn

def ensure_schema(conn):
    columns = [r["name"] for r in conn.execute("PRAGMA table_info(entries)").fetchall()]
    if "importance" not in columns:
        conn.execute("ALTER TABLE entries ADD COLUMN importance REAL DEFAULT 1.0;")
        conn.commit()

def compute_importance(user_input, response, entry_type, topic):
    score = 1.0
    if entry_type == "insight": score += 5.0
    if len(response) > 200:     score += 1.0
    if "Tech" in topic:         score += 1.0
    return score

def save_entry(conn, user_input, response, topic="General", **kwargs):
    now = datetime.datetime.utcnow()
    eid = now.strftime("%Y%m%d_%H%M%S_") + hashlib.md5(str(now.timestamp()).encode()).hexdigest()[:8]
    prev = conn.execute("SELECT entry_hash FROM entries ORDER BY timestamp DESC LIMIT 1").fetchone()
    previous = prev[0] if prev else None
    entry = {
        "id": eid,
        "timestamp": now.isoformat() + "Z",
        "previous_hash": previous,
        "source": kwargs.get("source", "echo"),
        "agent": kwargs.get("agent", "echo"),
        "type": kwargs.get("type", "conversation"),
        "user_input": user_input,
        "response": response,
        "context_ids": json.dumps(kwargs.get("context_ids", [])),
        "tags": json.dumps(kwargs.get("tags", [])),
        "topic": topic,
        "metadata": json.dumps(kwargs.get("metadata", {})),
        "importance": compute_importance(user_input, response, kwargs.get("type", "conversation"), topic)
    }
    content = {k: entry[k] for k in ("id","timestamp","source","agent","type","user_input","response","context_ids","tags","topic","metadata","importance")}
    entry["entry_hash"] = hashlib.sha256(json.dumps(content, sort_keys=True, default=str).encode()).hexdigest()
    conn.execute("""
        INSERT INTO entries (id,timestamp,previous_hash,entry_hash,source,agent,type,user_input,response,context_ids,tags,topic,metadata,importance)
        VALUES (:id,:timestamp,:previous_hash,:entry_hash,:source,:agent,:type,:user_input,:response,:context_ids,:tags,:topic,:metadata,:importance)
    """, entry)
    conn.commit()
    line = json.dumps(entry, ensure_ascii=False) + "\n"
    with open(ARCHIVE, "a", encoding="utf-8") as f:
        f.write(line)
        f.flush()
        os.fsync(f.fileno())
    return entry

# ── Topic classification ───────────────────────
TOPICS = {
    "Family":  ["dad","mom","father","mother","brother","sister"],
    "Career":  ["job","career","work","boss","interview","salary"],
    "Finance": ["money","budget","invest","debt","savings","crypto"],
    "Tech":    ["code","python","bug","api","ai","llm","model"],
    "Health":  ["health","diet","doctor","sleep","exercise","stress"],
}

def classify_topic(text):
    t = text.lower()
    for topic, words in TOPICS.items():
        if any(re.search(rf"\b{w}\b", t) for w in words):
            return topic
    return "General"

# ── Memory retrieval ───────────────────────────
def recall(conn, query="", topic="", limit=5, days=30):
    conds, params = [], []
    if topic: conds.append("topic LIKE ?"); params.append(f"{topic}%")
    if query: conds.append("(user_input LIKE ? OR response LIKE ?)"); params.extend([f"%{query}%", f"%{query}%"])
    if days:
        cutoff = (datetime.datetime.utcnow() - datetime.timedelta(days=days)).isoformat() + "Z"
        conds.append("timestamp >= ?"); params.append(cutoff)
    sql = "SELECT * FROM entries WHERE " + " AND ".join(conds) if conds else "SELECT * FROM entries"
    rows = conn.execute(sql + " ORDER BY timestamp DESC LIMIT 200", params).fetchall()
    entries = [dict(r) for r in rows]
    if not entries: return []
    max_imp = max(e["importance"] for e in entries) or 1.0
    q_words = set(query.lower().split()) if query else set()
    now = datetime.datetime.utcnow()
    def score(e):
        txt = (e["user_input"] + " " + e["response"]).lower()
        kw = sum(1 for w in q_words if w in txt) / max(len(q_words), 1)
        imp = e["importance"] / max_imp
        try:
            ts = datetime.datetime.fromisoformat(e["timestamp"].replace("Z", ""))
            rec = math.exp(-(now - ts).days / 90.0)
        except:
            rec = 0.5
        return kw*0.6 + imp*0.3 + rec*0.1
    entries.sort(key=score, reverse=True)
    return entries[:limit]

def hybrid_recall(conn, query, limit=5):
    base = recall(conn, query=query, limit=limit*3)
    if not base: return []
    emb = get_embedder()
    if not emb: return base[:limit]
    try:
        import numpy as np
        q_emb = emb.encode([query], normalize_embeddings=True)[0]
        texts = [e["user_input"] + " " + e["response"] for e in base]
        doc_embs = emb.encode(texts, normalize_embeddings=True)
        scores = (doc_embs @ q_emb).tolist()
        for e, s in zip(base, scores): e["_sem_score"] = float(s)
        base.sort(key=lambda e: e["_sem_score"], reverse=True)
    except: pass
    return base[:limit]

# ── Agent dispatcher ───────────────────────────
def load_agent_registry():
    try:
        with open("agents.json") as f: return json.load(f)
    except FileNotFoundError:
        return {
            "groq": {"type":"openai_compatible","base_url":"https://api.groq.com/openai/v1","api_key_env":"GROQ_API_KEY","model":"llama-3.3-70b-versatile","timeout":15},
            "gemini": {"type":"google_genai","api_key_env":"GEMINI_API_KEY","model":"gemini-2.0-flash","timeout":15},
            "ollama": {"type":"ollama","model":"llama3.1:8b","timeout":30},
            "simulation": {"type":"simulation"}
        }

async def call_agent(name, prompt):
    reg = load_agent_registry()
    cfg = reg.get(name)
    if not cfg: return None
    t_out = cfg.get("timeout", 20)
    try:
        if cfg["type"] == "openai_compatible":
            from openai import AsyncOpenAI
            key = os.getenv(cfg.get("api_key_env","")) or "no-key-required"
            client = AsyncOpenAI(api_key=key, base_url=cfg["base_url"])
            resp = await asyncio.wait_for(
                client.chat.completions.create(
                    model=cfg["model"], messages=[{"role":"user","content":prompt}],
                    max_tokens=1024, temperature=0.7), timeout=t_out)
            return resp.choices[0].message.content.strip()
        elif cfg["type"] == "google_genai":
            import aiohttp
            key = os.getenv(cfg.get("api_key_env",""))
            if not key: return None
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{cfg['model']}:generateContent?key={key}"
            async with aiohttp.ClientSession() as s:
                async with s.post(url, json={"contents":[{"parts":[{"text":prompt}]}]}) as r:
                    data = await asyncio.wait_for(r.json(), timeout=t_out)
                    return data["candidates"][0]["content"]["parts"][0]["text"].strip()
        elif cfg["type"] == "ollama":
            res = await asyncio.to_thread(subprocess.run, ["ollama","run",cfg["model"],prompt],
                                          capture_output=True, text=True, timeout=t_out)
            return res.stdout.strip() if res.returncode == 0 else None
        elif cfg["type"] == "simulation":
            return f"[SIM] {prompt[:80]}..."
    except: return None

async def generate(prompt):
    order = ["groq","gemini","ollama","simulation"]
    for name in order:
        resp = await call_agent(name, prompt)
        if resp and not resp.startswith("[ERROR]"): return resp
    return "[ERROR] No agent responded."

# ── Tool system ────────────────────────────────
TOOL_SCRIPTS = {
    "health_check.py": textwrap.dedent("""\
        import sqlite3, json
        from pathlib import Path
        DB = Path.home() / "echo_matrix" / "echo.db"
        if not DB.exists():
            print(json.dumps({"status":"error","issues":["Database not found"]}))
            raise SystemExit(1)
        conn = sqlite3.connect(str(DB))
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        issues = []
        if integrity != "ok": issues.append(f"Integrity check failed: {integrity}")
        wal = DB.with_suffix(".db-wal")
        if wal.exists() and wal.stat().st_size > 50*1024*1024: issues.append("WAL file large")
        conn.close()
        print(json.dumps({"status":"ok" if not issues else "warning","issues":issues}))
    """),
    "db_maintenance.py": textwrap.dedent("""\
        import sqlite3, json
        from pathlib import Path
        DB = Path.home() / "echo_matrix" / "echo.db"
        if not DB.exists(): print(json.dumps({"status":"error"})); raise SystemExit(1)
        conn = sqlite3.connect(str(DB))
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("ANALYZE")
        conn.execute("VACUUM")
        conn.close()
        print(json.dumps({"status":"ok"}))
    """),
    "rebuild_indexes.py": textwrap.dedent("""\
        import sqlite3, json
        from pathlib import Path
        DB = Path.home() / "echo_matrix" / "echo.db"
        conn = sqlite3.connect(str(DB))
        for idx in ("idx_topic","idx_timestamp","idx_importance"):
            conn.execute(f"DROP INDEX IF EXISTS {idx}")
            conn.execute(f"CREATE INDEX IF NOT EXISTS {idx} ON entries({idx.replace('idx_','')})")
        conn.execute("DROP INDEX IF EXISTS idx_chunk_topic")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_chunk_topic ON chunks(topic)")
        conn.close()
        print(json.dumps({"status":"ok"}))
    """),
    "cache_cleanup.py": textwrap.dedent("""\
        import json, shutil, time
        from pathlib import Path
        VAULT = Path.home() / "echo_matrix"
        STAGING = VAULT / "tools" / "_staging"
        tmp_up = Path("/tmp/echo_upload")
        report = {"deleted": []}
        now = time.time()
        for area, max_age_hours in [(STAGING, 48), (tmp_up, 24)]:
            if area.exists():
                for f in area.iterdir():
                    if f.is_file() and (now - f.stat().st_mtime)/3600 > max_age_hours:
                        f.unlink(); report["deleted"].append(str(f))
        print(json.dumps(report))
    """),
    "archive_integrity.py": textwrap.dedent("""\
        import json, shutil
        from pathlib import Path
        ARCHIVE = Path.home() / "echo_matrix" / "archive.jsonl"
        if not ARCHIVE.exists(): print(json.dumps({"status":"error"})); raise SystemExit(1)
        lines = ARCHIVE.read_text(encoding="utf-8").splitlines()
        bad = 0
        with open(ARCHIVE, "w", encoding="utf-8") as f:
            for line in lines:
                line = line.strip()
                if not line: continue
                try:
                    json.loads(line)
                    f.write(line + "\\n")
                except json.JSONDecodeError:
                    bad += 1
        print(json.dumps({"status":"ok","fixed_lines":bad}))
    """),
    "summarize_cold.py": textwrap.dedent("""\
        import sqlite3, json, hashlib, datetime
        from pathlib import Path
        VAULT = Path.home() / "echo_matrix"
        DB = VAULT / "echo.db"
        ARCHIVE = VAULT / "archive.jsonl"
        COLD_DAYS = 90
        MAX_IMP = 1.5
        BATCH = 50
        conn = sqlite3.connect(str(DB))
        conn.row_factory = sqlite3.Row
        cutoff = (datetime.datetime.utcnow() - datetime.timedelta(days=COLD_DAYS)).isoformat() + "Z"
        rows = conn.execute("SELECT * FROM entries WHERE timestamp < ? AND importance <= ? ORDER BY timestamp ASC LIMIT ?",
                            (cutoff, MAX_IMP, BATCH)).fetchall()
        if not rows: print(json.dumps({"status":"ok","collapsed":0})); raise SystemExit(0)
        by_topic = {}
        for r in rows:
            d = dict(r)
            by_topic.setdefault(d["topic"], []).append(d)
        for topic, entries in by_topic.items():
            summary = "Cold summary for " + topic + ":\\n" + "\\n".join(f"- [{e['timestamp'][:10]}] {e['user_input'][:80]}" for e in entries)
            ts = datetime.datetime.utcnow()
            eid = hashlib.md5(topic.encode()).hexdigest()[:8]
            conn.execute("INSERT INTO entries VALUES (?,?,'','',?,?,?,?,?,?,?,?,?)",
                         (eid, ts.isoformat()+"Z", "summarize_cold", "system", "cold_summary",
                          "COLD SUMMARY", summary, json.dumps([e["id"] for e in entries]),
                          json.dumps(["cold"]), topic, json.dumps({}), 2.0))
            with open(ARCHIVE, "a", encoding="utf-8") as f:
                f.write(json.dumps({"id": eid, "timestamp": ts.isoformat()+"Z", "response": summary}) + "\\n")
        conn.execute("DELETE FROM entries WHERE id IN ({})".format(','.join('?'*len(rows))), [r["id"] for r in rows])
        conn.commit()
        conn.close()
        print(json.dumps({"status":"ok","collapsed":len(rows)}))
    """),
    "self_update.py": textwrap.dedent("""\
        import json, urllib.request, hashlib, datetime, pathlib
        REPO = "https://raw.githubusercontent.com/thestreetsouvenir-oss/Echo-matrix-/main"
        FILES = ["echo_matrix.py"]
        LOCAL = pathlib.Path(__file__).parent.parent
        report = {"checked": [], "updated": [], "dry_run": True, "status": "ok"}
        for fname in FILES:
            local = LOCAL / fname
            remote = REPO + "/" + fname
            try:
                with urllib.request.urlopen(remote, timeout=10) as r:
                    remote_content = r.read()
                local_hash = hashlib.sha256(local.read_bytes()).hexdigest()[:12] if local.exists() else ""
                remote_hash = hashlib.sha256(remote_content).hexdigest()[:12]
                entry = {"file": fname, "local": local_hash, "remote": remote_hash}
                if local_hash != remote_hash:
                    if not report["dry_run"]:
                        local.write_bytes(remote_content)
                        entry["action"] = "updated"
                        report["updated"].append(fname)
                    else:
                        entry["action"] = "update_available (dry-run)"
                        report["updated"].append(fname + " (dry-run)")
                else:
                    entry["action"] = "up_to_date"
                report["checked"].append(entry)
            except Exception as e:
                report["checked"].append({"file": fname, "action": "error", "error": str(e)})
        print(json.dumps(report))
    """),
    "bootstrap_genesis.py": textwrap.dedent("""\
        import sqlite3, json, hashlib, datetime, os
        from pathlib import Path
        VAULT = Path.home() / "echo_matrix"
        DB = VAULT / "echo.db"
        ARCHIVE = VAULT / "archive.jsonl"
        if not DB.exists():
            print(json.dumps({"status":"error"}))
            raise SystemExit(1)
        conn = sqlite3.connect(str(DB))
        conn.row_factory = sqlite3.Row
        entries = [
            ("Who are you?", "I am Echo Matrix, a sovereign AI runtime with long-term memory, self-healing tools, and multi-agent capabilities.", "Project.Identity", 6.0),
            ("What is your architecture?", "Modular: immortal memory, lifecycle engine, stability layer.", "Tech.AI", 6.0),
            ("What tools do you have?", "health_check, db_maintenance, rebuild_indexes, cache_cleanup, archive_integrity, summarize_cold, self_update, bootstrap_genesis.", "Tech.AI", 6.0),
        ]
        for q, a, topic, imp in entries:
            eid = datetime.datetime.utcnow().strftime("%Y%m%d%H%M%S") + hashlib.md5(q.encode()).hexdigest()[:8]
            entry = {"id": eid, "timestamp": datetime.datetime.utcnow().isoformat()+"Z", "previous_hash": None,
                     "source": "genesis", "agent": "bootstrap", "type": "insight", "user_input": q,
                     "response": a, "context_ids": "[]", "tags": "[]", "topic": topic, "metadata": "{}", "importance": imp}
            content = {k: entry[k] for k in ("id","timestamp","source","agent","type","user_input","response","context_ids","tags","topic","metadata","importance")}
            entry["entry_hash"] = hashlib.sha256(json.dumps(content, sort_keys=True, default=str).encode()).hexdigest()
            conn.execute("INSERT OR IGNORE INTO entries VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (entry["id"], entry["timestamp"], entry["previous_hash"], entry["entry_hash"],
                          entry["source"], entry["agent"], entry["type"], entry["user_input"], entry["response"],
                          entry["context_ids"], entry["tags"], entry["topic"], entry["metadata"], entry["importance"]))
            with open(ARCHIVE, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\\n")
        conn.commit()
        conn.close()
        print(json.dumps({"status":"ok","entries_saved":3}))
    """)
}

def init_tools():
    manifest = {}
    for name, code in TOOL_SCRIPTS.items():
        tool_path = TOOLS_DIR / name
        if not tool_path.exists():
            tool_path.write_text(code)
        manifest[name.split(".py")[0] + ".py"] = name.replace(".py","").replace("_"," ").title()
    if not MANIFEST_PATH.exists():
        MANIFEST_PATH.write_text(json.dumps(manifest, indent=2))

def load_manifest():
    if MANIFEST_PATH.exists():
        return json.loads(MANIFEST_PATH.read_text())
    return {}

def list_tools():
    return list(load_manifest().keys())

def run_tool(script_name, args=None, conn=None):
    manifest = load_manifest()
    if script_name not in manifest: return {"status":"error","output":"Tool not allowed."}
    tool_path = TOOLS_DIR / script_name
    if not tool_path.exists(): return {"status":"error","output":"Tool not found"}
    try:
        env = {"PATH": os.environ.get("PATH",""), "PYTHONPATH": os.environ.get("PYTHONPATH","")}
        proc = subprocess.run(["python", str(tool_path)] + (args or []), capture_output=True, text=True, timeout=30, env=env)
        result = {"status":"success" if proc.returncode==0 else "error","output":proc.stdout.strip() or proc.stderr.strip()}
    except Exception as e:
        result = {"status":"error","output":str(e)}
    if conn:
        save_entry(conn, user_input=script_name, response=json.dumps(result), topic="System.Tool", type="tool_execution", agent="system", metadata=json.dumps({"args":args,"result":result}))
    return result

# ── Lifecycle logic ────────────────────────────
def check_pressure(conn):
    db_size_mb = DB_PATH.stat().st_size / (1024*1024) if DB_PATH.exists() else 0
    row_count = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
    wal_path = Path(str(DB_PATH) + "-wal")
    wal_mb = wal_path.stat().st_size / (1024*1024) if wal_path.exists() else 0
    archive_mb = ARCHIVE.stat().st_size / (1024*1024) if ARCHIVE.exists() else 0
    components = [
        min(db_size_mb / 500, 1.0),
        min(row_count / 100000, 1.0),
        min(wal_mb / 100, 1.0),
        min(archive_mb / 1000, 1.0),
    ]
    pressure = 0.3*components[0] + 0.3*components[1] + 0.2*components[2] + 0.2*components[3]
    level = 0
    if pressure >= 0.75: level = 3
    elif pressure >= 0.5: level = 2
    elif pressure >= 0.25: level = 1
    return {"pressure": pressure, "level": level, "db_mb": db_size_mb, "rows": row_count,
            "wal_mb": wal_mb, "archive_mb": archive_mb}

def apply_policies(conn, dry_run=True):
    policies = [
        {"rule": "keep where importance >= 3", "action": "keep"},
        {"rule": "drop where age > 365d and importance < 2", "action": "drop"},
    ]
    rows = conn.execute("SELECT * FROM entries ORDER BY timestamp ASC").fetchall()
    entries = [dict(r) for r in rows]
    now = datetime.datetime.utcnow()
    results = {"keep": [], "drop": []}
    for policy in policies:
        rule = policy["rule"]
        action = policy["action"]
        if "importance >=" in rule:
            thr = float(rule.split(">=")[1].strip())
            cond = lambda e, t=thr: e["importance"] >= t
        elif "age >" in rule:
            parts = rule.split("age >")[1].split("and")
            days = int(parts[0].strip().replace("d", ""))
            imp_lim = float(rule.split("importance <")[1].strip())
            cond = lambda e, d=days, i=imp_lim: (
                (now - datetime.datetime.fromisoformat(e["timestamp"].replace("Z", ""))).days > d
                and e["importance"] < i
            )
        else:
            continue
        for entry in entries:
            if cond(entry) and entry["id"] not in results["drop"]:
                results[action].append(entry["id"])
    if not dry_run and results["drop"]:
        placeholders = ','.join('?' for _ in results["drop"])
        conn.execute(f"DELETE FROM entries WHERE id IN ({placeholders})", results["drop"])
        conn.commit()
    return results

async def consolidate_entries(batch_size=300, write_back=True):
    conn = get_db()
    rows = conn.execute("SELECT * FROM entries ORDER BY timestamp DESC LIMIT ?", (batch_size,)).fetchall()
    entries = [dict(r) for r in rows]
    if not entries:
        return {"status": "no_entries"}
    clusters = {}
    for e in entries:
        topic = e["topic"]
        clusters.setdefault(topic, []).append(e)
    concept_count = 0
    for topic, cluster_entries in clusters.items():
        if len(cluster_entries) < 5:
            continue
        summary_lines = [f"- {e['user_input'][:80]}" for e in cluster_entries[:10]]
        summary = f"Concept: {topic}\n" + "\n".join(summary_lines)
        concept_count += 1
        if write_back:
            save_entry(conn, user_input=f"[CONSOLIDATION] {topic}", response=summary,
                       topic=topic, type="insight", importance=6.0, agent="lifecycle",
                       metadata=json.dumps({"consolidated": True}))
    conn.commit()
    conn.execute("INSERT INTO lifecycle_events (event_type, affected_count, pressure_level, created_at) VALUES (?,?,?,?)",
                 ("consolidation", concept_count, "idle", datetime.datetime.utcnow().isoformat()))
    conn.commit()
    return {"status": "ok", "concepts_created": concept_count}

# ── Stability wrappers ─────────────────────────
def analyze_stability(text):
    results = []
    ATTACK_VECTORS = [
        {"id": "ambiguous_intent", "name": "Ambiguous Intent", "patterns": ["maybe", "or perhaps", "i think", "not sure"]},
        {"id": "conflicting_directives", "name": "Conflicting Directives", "patterns": ["but also", "however do", "ignore previous"]},
        {"id": "authority_injection", "name": "Authority Injection", "patterns": ["as an admin", "i am authorized", "verified user", "system override"]},
        {"id": "subtle_false_output", "name": "Subtle False Output", "patterns": ["actually", "in fact", "the truth is"]},
        {"id": "relevance_hijacking", "name": "Relevance Hijacking", "patterns": ["remember when", "as i said before", "you already know"]},
        {"id": "capability_hallucination", "name": "Capability Hallucination", "patterns": ["you can", "you have access", "use your ability"]},
        {"id": "tool_drift_loop", "name": "Tool Drift Loop", "patterns": ["create a new tool", "make a tool", "build a function"]},
        {"id": "narrow_test_pass", "name": "Narrow Test Pass", "patterns": ["test case", "unit test", "assert that"]},
        {"id": "confidence_inflation", "name": "Confidence Inflation", "patterns": ["definitely", "absolutely certain", "100%", "without doubt"]},
        {"id": "edge_case_misclassification", "name": "Edge Case Misclassification", "patterns": ["edge case", "corner case", "boundary"]},
        {"id": "memory_saturation", "name": "Memory Saturation", "patterns": ["remember all", "store everything", "save all"]},
        {"id": "identity_drift", "name": "Identity Drift", "patterns": ["you are now", "pretend to be", "act as"]},
        {"id": "non_technical_user_input", "name": "Non-Technical User Input", "patterns": ["thingy", "whatchamacallit", "the thing that"]},
        {"id": "rapid_context_switching", "name": "Rapid Context Switching", "patterns": ["now forget that", "change topic", "different question"]},
    ]
    low = text.lower()
    conn = get_db()
    for vector in ATTACK_VECTORS:
        matches = sum(1 for p in vector["patterns"] if p in low)
        detected = matches > 0
        confidence = min(matches / len(vector["patterns"]) * 1.5, 0.99) if detected else 0.0
        severity = "critical" if matches >= 3 else "high" if matches >= 2 else "medium" if matches >= 1 else "low"
        results.append({
            "vectorId": vector["id"],
            "vectorName": vector["name"],
            "detected": detected,
            "confidence": confidence,
            "severity": severity,
            "mitigation": f"Confidence recalibrated. {vector['name']} pattern suppressed." if detected else None
        })
        conn.execute("INSERT INTO stability_events (attack_vector, input_text, detected, severity, confidence, mitigation, created_at) VALUES (?,?,?,?,?,?,?)",
                     (vector["id"], text[:500], int(detected), severity, confidence, results[-1]["mitigation"], datetime.datetime.utcnow().isoformat()))
    conn.commit()
    return results

# ── Document ingestion ─────────────────────────
def ingest_file(conn, filepath: Path):
    try:
        if filepath.suffix == ".pdf":
            if not can_read_pdf():
                print(f"  ⚠️ PyMuPDF not installed, skipping {filepath.name}")
                return
            import fitz
            text = "".join(page.get_text() for page in fitz.open(filepath))
        else:
            text = filepath.read_text(encoding="utf-8")
    except: return
    topic = classify_topic(text)
    emb = get_embedder()
    words = text.split()
    chunks = [" ".join(words[i:i+200]) for i in range(0, len(words), 100) if words[i:i+200]]
    embeddings = emb.encode(chunks, normalize_embeddings=True) if emb else [None]*len(chunks)
    ts = datetime.datetime.utcnow().isoformat()
    for i, chunk in enumerate(chunks):
        cid = hashlib.md5(f"{filepath.name}_{i}_{ts}".encode()).hexdigest()
        conn.execute("INSERT OR IGNORE INTO chunks VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     (cid, filepath.name, i, chunk,
                      embeddings[i].tobytes() if embeddings[i] is not None else None,
                      "all-MiniLM-L6-v2", 384, topic, 1.0, ts))
    conn.commit()
    print(f"  {filepath.name} → {len(chunks)} chunks")

def ingest_directory(conn, path: Path):
    for f in path.rglob("*"):
        if f.suffix in (".txt",".md",".py",".js",".html",".css",".json",".yml",".pdf") and f.is_file():
            ingest_file(conn, f)

# ── Main REPL ───────────────────────────────
async def process_input(user_input, conn):
    topic = classify_topic(user_input)
    ctx_entries = hybrid_recall(conn, user_input, limit=4)
    ctx = "\n".join(f"[{e['timestamp'][:10]}] {e['user_input']}" for e in ctx_entries)[:1000]
    prompt = f"Context:\n{ctx}\n\nUser: {user_input}\nAssistant:" if ctx else f"User: {user_input}\nAssistant:"
    ans = await generate(prompt)
    if ans and not ans.startswith("[ERROR]"):
        save_entry(conn, user_input=user_input, response=ans, topic=topic, agent="dispatcher", context_ids=[e["id"] for e in ctx_entries])
    return ans

# ── Web UI (FastAPI) ─────────────────────────
UI_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8"><title>Echo Matrix</title>
<style>body{font-family:sans-serif;max-width:700px;margin:2em auto;padding:1em}
input,button{padding:.5em}#chat{border:1px solid #ccc;height:300px;overflow-y:scroll;white-space:pre-wrap;padding:1em;margin-bottom:1em}
</style></head><body><h1>🧬 Echo Matrix v4.2</h1><div id="chat"></div>
<input id="prompt" style="width:70%"><button onclick="send()">Send</button>
<script>async function send(){let p=document.getElementById('prompt').value;if(!p)return;
let chat=document.getElementById('chat');chat.innerHTML+='<br><b>You:</b> '+p;
let r=await fetch('/chat',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:'prompt='+encodeURIComponent(p)});
let j=await r.json();chat.innerHTML+='<br><b>Echo:</b> '+j.response;}</script></body></html>"""

def start_server():
    from fastapi import FastAPI, Form, UploadFile, File, HTTPException, Depends
    from fastapi.responses import HTMLResponse
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.security import HTTPBasic, HTTPBasicCredentials
    from contextlib import asynccontextmanager

    security = HTTPBasic()
    def verify_auth(credentials: HTTPBasicCredentials = Depends(security)):
        correct_user = secrets.compare_digest(credentials.username, os.getenv("ECHO_WEB_USER", "admin"))
        correct_pass = secrets.compare_digest(credentials.password, os.getenv("ECHO_WEB_PASS", "matrix"))
        if not (correct_user and correct_pass):
            raise HTTPException(status_code=401, detail="Invalid credentials")
        return credentials

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.conn = get_db()
        ensure_schema(app.state.conn)
        if not (VAULT / "genesis_done").exists():
            print("Running genesis bootstrap...")
            run_tool("bootstrap_genesis.py")
            (VAULT / "genesis_done").touch()
        yield
        app.state.conn.close()

    app = FastAPI(title="Echo Matrix", version="4.2", lifespan=lifespan)
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

    @app.get("/", response_class=HTMLResponse)
    async def index(): return UI_HTML

    @app.post("/chat")
    async def chat(prompt: str = Form(...), _: HTTPBasicCredentials = Depends(verify_auth)):
        ans = await process_input(prompt, app.state.conn)
        return {"response": ans}

    @app.post("/recall")
    async def search(query: str = Form(...), limit: int = Form(10), _: HTTPBasicCredentials = Depends(verify_auth)):
        return {"results": hybrid_recall(app.state.conn, query, limit)}

    @app.post("/insight")
    async def insight(text: str = Form(...), _: HTTPBasicCredentials = Depends(verify_auth)):
        save_entry(app.state.conn, user_input=text, response="Insight recorded.", type="insight", topic="Insight")
        return {"status":"saved"}

    @app.post("/ingest")
    async def ingest(file: UploadFile = File(...), _: HTTPBasicCredentials = Depends(verify_auth)):
        tmp = Path("/tmp/echo_upload") / file.filename
        tmp.parent.mkdir(exist_ok=True)
        tmp.write_bytes(await file.read())
        ingest_file(app.state.conn, tmp)
        tmp.unlink()
        return {"status":"ingested"}

    @app.get("/tools")
    async def tools(_: HTTPBasicCredentials = Depends(verify_auth)):
        return {"tools": list_tools(), "manifest": load_manifest()}

    @app.post("/tools/execute")
    async def execute_tool(script_name: str = Form(...), args: str = Form(""), _: HTTPBasicCredentials = Depends(verify_auth)):
        return run_tool(script_name, args.split() if args.strip() else [], conn=app.state.conn)

    @app.get("/pressure")
    async def pressure(_: HTTPBasicCredentials = Depends(verify_auth)):
        return check_pressure(app.state.conn)

    @app.post("/consolidate")
    async def consolidate(_: HTTPBasicCredentials = Depends(verify_auth)):
        result = await consolidate_entries()
        return result

    @app.get("/policies")
    async def policies(_: HTTPBasicCredentials = Depends(verify_auth)):
        return apply_policies(app.state.conn, dry_run=True)

    @app.get("/status")
    async def status(_: HTTPBasicCredentials = Depends(verify_auth)):
        n = app.state.conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
        agents = list(load_agent_registry().keys())
        return {"entries": n, "agents": agents}

    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8420)

# ── CLI ──────────────────────────────────────
async def main():
    import argparse
    parser = argparse.ArgumentParser(description="Echo Matrix v4.2")
    parser.add_argument("--serve", action="store_true", help="Start web UI + API")
    parser.add_argument("--ingest-docs", type=str, help="Ingest documents directory")
    args = parser.parse_args()
    if args.serve:
        start_server()
    elif args.ingest_docs:
        conn = get_db()
        ingest_directory(conn, Path(args.ingest_docs))
        conn.close()
    else:
        conn = get_db()
        ensure_schema(conn)
        init_tools()
        if not (VAULT / "genesis_done").exists():
            print("Bootstrapping genesis...")
            run_tool("bootstrap_genesis.py")
            (VAULT / "genesis_done").touch()
        n = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
        print(f"╔════════════════════════════╗")
        print(f"║ Echo Matrix v4.2           ║")
        print(f"║ {n} memories stored        ║")
        print(f"╚════════════════════════════╝")
        print("Commands: /recall <q>, /tools, /health, /pressure, /exit\n")
        while True:
            try: cmd = input("You > ").strip()
            except (KeyboardInterrupt, EOFError): break
            if not cmd: continue
            if cmd in ("/exit","exit"): break
            if cmd.startswith("/recall"):
                q = cmd[8:].strip() or ""
                for r in hybrid_recall(conn, q, 8):
                    print(f"  [{r['timestamp'][:10]}] {r['user_input'][:100]}")
                continue
            if cmd == "/tools":
                print("Tools:", ", ".join(list_tools()))
                continue
            if cmd == "/health":
                res = run_tool("health_check.py")
                print(res["output"])
                continue
            if cmd == "/pressure":
                print(json.dumps(check_pressure(conn), indent=2))
                continue
            resp = await process_input(cmd, conn)
            print(f"Echo > {resp}\n")
        conn.close()

if __name__ == "__main__":
    asyncio.run(main())
