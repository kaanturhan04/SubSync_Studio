import sqlite3
from datetime import datetime

DB_PATH = "subsync_history.db"

# ── ROI Varsayimlari — net etiketli tahminler, gercek olcum degil ────────────
MANUAL_MINUTES_PER_MEDIA_MINUTE = 6      # 1 dk medya icin manuel is suresi (dil basina)
MANUAL_RATE_TRY_PER_MINUTE = 20          # freelance transkripsiyon/ceviri tahmini TL/dk

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL,
            filename TEXT NOT NULL,
            detected_lang TEXT,
            target_langs TEXT,
            is_video INTEGER DEFAULT 0,
            burned_langs TEXT,
            status TEXT DEFAULT 'ok',
            created_at TEXT NOT NULL,
            media_duration_seconds REAL DEFAULT 0,
            processing_seconds REAL DEFAULT 0,
            lang_count INTEGER DEFAULT 1
        )
    """)
    conn.commit()

    # Eski db'de yeni kolonlar yoksa migration yap
    c.execute("PRAGMA table_info(jobs)")
    existing_cols = [row[1] for row in c.fetchall()]
    migrations = {
        "media_duration_seconds": "REAL DEFAULT 0",
        "processing_seconds": "REAL DEFAULT 0",
        "lang_count": "INTEGER DEFAULT 1"
    }
    for col, col_type in migrations.items():
        if col not in existing_cols:
            c.execute(f"ALTER TABLE jobs ADD COLUMN {col} {col_type}")
            print(f"[SubSync] DB migration: '{col}' kolonu eklendi")
    conn.commit()
    conn.close()

def save_job(job_id, filename, detected_lang, target_langs, is_video, burned_langs,
             status="ok", media_duration_seconds=0, processing_seconds=0):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    lang_count = 1 + (len(target_langs) if target_langs else 0)
    c.execute("""
        INSERT INTO jobs (job_id, filename, detected_lang, target_langs, is_video, burned_langs,
                          status, created_at, media_duration_seconds, processing_seconds, lang_count)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        job_id, filename, detected_lang,
        ",".join(target_langs) if target_langs else "",
        1 if is_video else 0,
        ",".join(burned_langs) if burned_langs else "",
        status,
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        media_duration_seconds,
        processing_seconds,
        lang_count
    ))
    conn.commit()
    conn.close()

def get_history(limit=50):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,))
    rows = c.fetchall()
    conn.close()
    return [dict(row) for row in rows]

def get_stats():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM jobs")
    total = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM jobs WHERE is_video = 1")
    videos = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM jobs WHERE status = 'ok'")
    success = c.fetchone()[0]
    conn.close()
    return {"total": total, "videos": videos, "success": success}

def get_roi_stats():
    """
    A) Zaman tasarrufu -> gercek olculen verilere dayanir
    B) Maliyet tasarrufu -> yukaridaki MANUAL_* sabitlerine dayanan TAHMIN, dashboard'da etiketlenecek
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM jobs WHERE status = 'ok'")
    rows = c.fetchall()
    conn.close()

    total_media_seconds = 0
    total_processing_seconds = 0
    total_manual_seconds_estimate = 0

    for row in rows:
        media_s = row["media_duration_seconds"] or 0
        proc_s = row["processing_seconds"] or 0
        lang_count = row["lang_count"] or 1
        total_media_seconds += media_s
        total_processing_seconds += proc_s
        manual_minutes = (media_s / 60) * lang_count * MANUAL_MINUTES_PER_MEDIA_MINUTE
        total_manual_seconds_estimate += manual_minutes * 60

    time_saved_seconds = max(0, total_manual_seconds_estimate - total_processing_seconds)
    cost_saved_try = (total_manual_seconds_estimate / 60) * MANUAL_RATE_TRY_PER_MINUTE

    return {
        "job_count": len(rows),
        "total_media_minutes": round(total_media_seconds / 60, 1),
        "total_processing_minutes": round(total_processing_seconds / 60, 2),
        "estimated_manual_minutes": round(total_manual_seconds_estimate / 60, 1),
        "time_saved_minutes": round(time_saved_seconds / 60, 1),
        "estimated_cost_saved_try": round(cost_saved_try, 2),
        "assumptions": {
            "manual_minutes_per_media_minute_per_lang": MANUAL_MINUTES_PER_MEDIA_MINUTE,
            "manual_rate_try_per_minute": MANUAL_RATE_TRY_PER_MINUTE
        }
    }

init_db()