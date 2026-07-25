from flask import Flask, request, jsonify, send_file, render_template_string
from functools import wraps
import os
import uuid
import threading
import time
import subprocess
import glob
import re
import requests as req_lib
from subsync import get_model, translate_segments, save_srt, burn_subtitles, is_video, LANGUAGES, WHISPER_TO_GOOGLE
from api_keys import is_valid_key
from history import save_job, get_history, get_stats, get_roi_stats

app = Flask(__name__)

UPLOAD_FOLDER = "uploads"
OUTPUT_FOLDER = "outputs"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

# ── Model ─────────────────────────────────────────────────────────────────────
MODEL_PATH = os.environ.get("SUBSYNC_MODEL_PATH", "large-v3")

# ── Zero-Retention (islem sonrasi otomatik dosya silme) ───────────────────────
CLEANUP_DELAY_SECONDS = int(os.environ.get("SUBSYNC_CLEANUP_DELAY", 3600))

# job_id -> {"timer": Timer, "paths": [...]} - iptal edilebilir zamanlayicilar
active_timers = {}

def cleanup_job_files(job_id, paths):
    for p in paths:
        try:
            if os.path.exists(p):
                os.remove(p)
                print(f"[SubSync] Zero-retention: silindi -> {p}")
        except Exception as e:
            print(f"[SubSync] Zero-retention silme hatasi: {p} -> {e}")
    active_timers.pop(job_id, None)

def schedule_cleanup(job_id, paths, delay=CLEANUP_DELAY_SECONDS):
    # Ayni job icin eski bir zamanlayici varsa once iptal et
    cancel_cleanup(job_id)
    timer = threading.Timer(delay, cleanup_job_files, args=(job_id, paths))
    timer.daemon = True
    timer.start()
    active_timers[job_id] = {"timer": timer, "paths": paths}
    print(f"[SubSync] Zero-retention planlandi: job {job_id}, {len(paths)} dosya, {delay}s sonra silinecek")

def cancel_cleanup(job_id):
    entry = active_timers.get(job_id)
    if entry:
        entry["timer"].cancel()
        active_timers.pop(job_id, None)
        print(f"[SubSync] Zero-retention iptal edildi: job {job_id} (duzenleme modu)")
        return True
    return False

# ── ROI: medya suresi olcumu (ffprobe uzerinden) ──────────────────────────────
def get_media_duration(file_path):
    try:
        cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration",
               "-of", "default=noprint_wrappers=1:nokey=1", file_path]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return float(result.stdout.strip())
    except Exception as e:
        print(f"[SubSync] ffprobe hatasi: {e}")
        return 0

# ── Human-in-the-Loop: SRT parse / yazma ──────────────────────────────────────
def parse_srt(path):
    """SRT dosyasini [{index, start, end, text}, ...] listesine cevirir."""
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    blocks = re.split(r"\n\s*\n", content.strip())
    cues = []
    for block in blocks:
        lines = block.strip().split("\n")
        if len(lines) < 3:
            continue
        idx = lines[0].strip()
        time_line = lines[1].strip()
        text = "\n".join(lines[2:]).strip()
        m = re.match(r"(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2},\d{3})", time_line)
        if not m:
            continue
        cues.append({
            "index": idx,
            "start": m.group(1),
            "end": m.group(2),
            "start_seconds": srt_time_to_seconds(m.group(1)),
            "text": text
        })
    return cues

def srt_time_to_seconds(t):
    h, m, rest = t.split(":")
    s, ms = rest.split(",")
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000

def write_srt_from_cues(cues, path):
    with open(path, "w", encoding="utf-8") as f:
        for i, cue in enumerate(cues, 1):
            f.write(f"{i}\n{cue['start']} --> {cue['end']}\n{cue['text'].strip()}\n\n")

def get_job_media(job_id, lang):
    """job_id+lang icin gercek video ve srt dosya yollarini bulur."""
    srt_path = os.path.join(OUTPUT_FOLDER, f"{job_id}_{lang}.srt")
    burned_path = os.path.join(OUTPUT_FOLDER, f"{job_id}_{lang}_burned.mp4")
    if os.path.exists(burned_path):
        video_path = burned_path
    else:
        matches = glob.glob(os.path.join(UPLOAD_FOLDER, f"{job_id}_*"))
        video_path = matches[0] if matches else None
    return {
        "srt_path": srt_path if os.path.exists(srt_path) else None,
        "video_path": video_path if video_path and os.path.exists(video_path) else None
    }

# ── Webhook ──────────────────────────────────────────────────────────────────
def send_webhook(url, payload):
    try:
        r = req_lib.post(url, json=payload, timeout=10)
        print(f"[SubSync] Webhook -> {url} : {r.status_code}")
    except Exception as e:
        print(f"[SubSync] Webhook hatasi: {e}")

# ── API Key Auth ──────────────────────────────────────────────────────────────
def require_api_key(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        key = request.headers.get("X-API-Key") or request.args.get("api_key")
        if not key or not is_valid_key(key):
            return jsonify({"status": "error", "message": "Gecersiz veya eksik API key."}), 401
        return f(*args, **kwargs)
    return decorated

# ── HTML (ana sayfa) ──────────────────────────────────────────────────────────
HTML = """<!DOCTYPE html>
<html lang="tr">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>SubSync Studio</title>
  <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600;700;800&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg: #07080f;
      --surface: rgba(255,255,255,0.04);
      --border: rgba(255,255,255,0.08);
      --border-active: rgba(99,179,237,0.5);
      --text: #f0f2f8;
      --muted: rgba(240,242,248,0.45);
      --blue: #38bdf8;
      --purple: #a78bfa;
      --grad: linear-gradient(135deg, #38bdf8, #a78bfa, #f43f5e);
    }
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: 'Outfit', sans-serif; background: var(--bg); color: var(--text); min-height: 100vh; overflow-x: hidden; }
    body::before { content: ''; position: fixed; inset: 0; background: radial-gradient(ellipse 80% 60% at 20% 10%, rgba(56,189,248,0.08) 0%, transparent 60%), radial-gradient(ellipse 60% 80% at 80% 80%, rgba(167,139,250,0.08) 0%, transparent 60%); pointer-events: none; z-index: 0; }
    .container { max-width: 860px; margin: 0 auto; padding: 0 24px; position: relative; z-index: 1; }
    nav { display: flex; align-items: center; justify-content: space-between; padding: 24px 0; border-bottom: 1px solid var(--border); margin-bottom: 64px; }
    .logo { font-weight: 800; font-size: 22px; letter-spacing: -0.5px; background: var(--grad); -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text; }
    .nav-links { display: flex; gap: 8px; }
    .nav-link { font-size: 13px; font-weight: 500; color: var(--muted); text-decoration: none; padding: 6px 14px; border-radius: 8px; border: 1px solid transparent; transition: all 0.2s; }
    .nav-link:hover { color: var(--text); border-color: var(--border); background: var(--surface); }
    .nav-badge { display: inline-flex; align-items: center; gap: 6px; font-size: 11px; font-weight: 600; color: var(--blue); background: rgba(56,189,248,0.1); border: 1px solid rgba(56,189,248,0.2); padding: 4px 10px; border-radius: 20px; }
    .hero { text-align: center; margin-bottom: 56px; }
    .hero-eyebrow { display: inline-flex; align-items: center; gap: 8px; font-size: 12px; font-weight: 600; letter-spacing: 1.5px; text-transform: uppercase; color: var(--muted); margin-bottom: 20px; }
    .hero-eyebrow span { width: 6px; height: 6px; border-radius: 50%; background: var(--grad); animation: pulse 2s ease-in-out infinite; }
    @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.4} }
    .hero h1 { font-size: clamp(36px, 6vw, 58px); font-weight: 800; line-height: 1.1; letter-spacing: -2px; margin-bottom: 18px; }
    .hero h1 .grad { background: var(--grad); -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text; }
    .hero p { font-size: 17px; color: var(--muted); font-weight: 300; line-height: 1.6; max-width: 520px; margin: 0 auto; }
    .stats-row { display: flex; justify-content: center; gap: 32px; margin-top: 32px; }
    .stat-num { font-size: 28px; font-weight: 700; background: var(--grad); -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text; }
    .stat-label { font-size: 12px; color: var(--muted); margin-top: 2px; }
    .card { background: var(--surface); border: 1px solid var(--border); border-radius: 20px; padding: 32px; margin-bottom: 16px; backdrop-filter: blur(12px); }
    .section-label { font-size: 11px; font-weight: 700; letter-spacing: 1.5px; text-transform: uppercase; color: var(--muted); margin-bottom: 16px; display: flex; align-items: center; gap: 8px; }
    .section-label::after { content: ''; flex: 1; height: 1px; background: var(--border); }
    .drop-zone { border: 2px dashed var(--border); border-radius: 14px; padding: 48px 24px; text-align: center; cursor: pointer; transition: all 0.25s; position: relative; }
    .drop-zone:hover, .drop-zone.drag-over { border-color: rgba(56,189,248,0.5); background: rgba(56,189,248,0.04); }
    .drop-zone input { position: absolute; inset: 0; opacity: 0; cursor: pointer; width: 100%; }
    .drop-icon { width: 48px; height: 48px; margin: 0 auto 16px; background: var(--grad); border-radius: 14px; display: flex; align-items: center; justify-content: center; font-size: 22px; }
    .drop-title { font-size: 16px; font-weight: 700; margin-bottom: 6px; }
    .drop-sub { font-size: 13px; color: var(--muted); }
    .drop-formats { display: flex; justify-content: center; gap: 6px; flex-wrap: wrap; margin-top: 16px; }
    .fmt-tag { font-size: 10px; font-weight: 600; padding: 3px 8px; border-radius: 4px; background: rgba(255,255,255,0.06); border: 1px solid var(--border); color: var(--muted); }
    .file-list { margin-top: 12px; display: flex; flex-wrap: wrap; gap: 8px; }
    .file-chip { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; font-weight: 500; padding: 5px 12px; border-radius: 20px; background: rgba(56,189,248,0.1); border: 1px solid rgba(56,189,248,0.2); color: var(--blue); }
    .select-wrapper { position: relative; }
    .select-wrapper select { appearance: none; width: 100%; background: rgba(255,255,255,0.04); border: 1px solid var(--border); border-radius: 10px; color: var(--text); font-family: 'Outfit', sans-serif; font-size: 14px; font-weight: 500; padding: 12px 40px 12px 14px; cursor: pointer; transition: border-color 0.2s; outline: none; }
    .select-wrapper select:focus { border-color: var(--border-active); }
    .select-wrapper::after { content: '▾'; position: absolute; right: 14px; top: 50%; transform: translateY(-50%); color: var(--muted); pointer-events: none; }
    .select-wrapper select option { background: #1a1a2e; }
    .lang-controls { display: flex; align-items: center; gap: 8px; margin-bottom: 14px; }
    .lang-btn { font-size: 11px; font-weight: 600; letter-spacing: 0.5px; text-transform: uppercase; padding: 5px 12px; border-radius: 6px; border: 1px solid var(--border); background: transparent; color: var(--muted); cursor: pointer; transition: all 0.2s; }
    .lang-btn:hover { background: var(--surface); color: var(--text); }
    .lang-count { margin-left: auto; font-size: 12px; color: var(--muted); }
    .lang-count span { color: var(--blue); font-weight: 600; }
    .lang-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(110px, 1fr)); gap: 6px; max-height: 200px; overflow-y: auto; padding-right: 4px; }
    .lang-grid::-webkit-scrollbar { width: 4px; }
    .lang-grid::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.1); border-radius: 2px; }
    .lang-chip { position: relative; display: flex; align-items: center; justify-content: center; padding: 7px 10px; border-radius: 8px; border: 1px solid var(--border); background: transparent; cursor: pointer; font-size: 12px; font-weight: 500; color: var(--muted); transition: all 0.15s; user-select: none; }
    .lang-chip:hover { border-color: rgba(255,255,255,0.2); color: var(--text); }
    .lang-chip.active { background: rgba(56,189,248,0.12); border-color: rgba(56,189,248,0.4); color: var(--blue); }
    .lang-chip input { position: absolute; opacity: 0; width: 0; height: 0; }
    .webhook-input { width: 100%; background: rgba(255,255,255,0.04); border: 1px solid var(--border); border-radius: 10px; color: var(--text); font-family: 'Outfit', sans-serif; font-size: 13px; padding: 11px 14px; outline: none; transition: border-color 0.2s; }
    .webhook-input::placeholder { color: var(--muted); }
    .webhook-input:focus { border-color: var(--border-active); }
    .submit-btn { width: 100%; padding: 16px; border: none; border-radius: 14px; background: var(--grad); color: white; font-family: 'Outfit', sans-serif; font-size: 15px; font-weight: 700; cursor: pointer; transition: opacity 0.2s, transform 0.15s; margin-top: 4px; }
    .submit-btn:hover { opacity: 0.9; transform: translateY(-1px); }
    .submit-btn:disabled { opacity: 0.5; cursor: not-allowed; transform: none; }
    .progress-bar { height: 3px; background: rgba(255,255,255,0.07); border-radius: 2px; overflow: hidden; margin-top: 16px; display: none; }
    .progress-fill { height: 100%; background: var(--grad); border-radius: 2px; transition: width 0.4s ease; }
    .status-text { font-size: 13px; color: var(--muted); margin-top: 10px; text-align: center; display: none; }
    .results { margin-top: 24px; display: flex; flex-direction: column; gap: 12px; }
    .result-card { background: var(--surface); border: 1px solid var(--border); border-radius: 16px; padding: 20px 24px; animation: slideUp 0.3s ease; }
    @keyframes slideUp { from{opacity:0;transform:translateY(8px)} to{opacity:1;transform:translateY(0)} }
    .result-card.processing { border-color: rgba(251,191,36,0.2); }
    .result-card.success { border-color: rgba(52,211,153,0.2); }
    .result-card.error { border-color: rgba(248,113,113,0.2); }
    .result-header { display: flex; align-items: center; gap: 10px; margin-bottom: 14px; }
    .result-icon { width: 32px; height: 32px; border-radius: 8px; display: flex; align-items: center; justify-content: center; font-size: 14px; }
    .processing .result-icon { background: rgba(251,191,36,0.15); }
    .success .result-icon { background: rgba(52,211,153,0.15); }
    .error .result-icon { background: rgba(248,113,113,0.15); }
    .result-filename { font-size: 14px; font-weight: 700; }
    .result-meta { font-size: 12px; color: var(--muted); margin-top: 2px; }
    .dl-group { margin-top: 12px; }
    .dl-group-label { font-size: 10px; font-weight: 700; letter-spacing: 1px; text-transform: uppercase; color: var(--muted); margin-bottom: 8px; }
    .dl-links { display: flex; flex-wrap: wrap; gap: 6px; }
    .dl-btn { display: inline-flex; align-items: center; gap: 5px; font-size: 12px; font-weight: 600; padding: 6px 12px; border-radius: 7px; text-decoration: none; transition: all 0.15s; }
    .dl-btn.srt { background: rgba(255,255,255,0.07); border: 1px solid var(--border); color: var(--text); }
    .dl-btn.srt:hover { background: rgba(255,255,255,0.12); }
    .dl-btn.mp4 { background: rgba(232,0,45,0.15); border: 1px solid rgba(232,0,45,0.3); color: #ff4d6d; }
    .dl-btn.mp4:hover { background: rgba(232,0,45,0.25); }
    .dl-btn.edit { background: rgba(167,139,250,0.15); border: 1px solid rgba(167,139,250,0.3); color: #a78bfa; }
    .dl-btn.edit:hover { background: rgba(167,139,250,0.25); }
    .global-status { text-align: center; padding: 12px; border-radius: 10px; font-size: 13px; font-weight: 500; display: none; margin-top: 16px; }
    .global-status.done { background: rgba(52,211,153,0.1); border: 1px solid rgba(52,211,153,0.2); color: #34d399; }
    @keyframes spin { to{transform:rotate(360deg)} }
    .spinner { display: inline-block; width: 14px; height: 14px; border: 2px solid rgba(255,255,255,0.2); border-top-color: var(--blue); border-radius: 50%; animation: spin 0.8s linear infinite; }
  </style>
</head>
<body>
<div class="container">
  <nav>
    <div class="logo">SubSync Studio</div>
    <div class="nav-links">
      <a href="/roi" class="nav-link">ROI</a>
      <a href="/history" class="nav-link">Geçmiş</a>
      <a href="/api-docs" class="nav-link">API</a>
      <span class="nav-badge"><span style="width:6px;height:6px;border-radius:50%;background:#34d399;display:inline-block"></span>{{ lang_count }}+ Dil</span>
    </div>
  </nav>

  <div class="hero">
    <div class="hero-eyebrow"><span></span>Whisper · AI Lokalizasyon · 2026</div>
    <h1>Eğitim videolarınız<br><span class="grad">artık dil tanımıyor.</span></h1>
    <p>Türk şirketleri için kurumsal video lokalizasyonu — saniyeler içinde 52+ dile otomatik çeviri ve altyazı.</p>
    <div class="stats-row">
      <div><div class="stat-num">52+</div><div class="stat-label">Dil Desteği</div></div>
      <div><div class="stat-num">%99</div><div class="stat-label">Whisper Doğruluğu</div></div>
      <div><div class="stat-num">MP4</div><div class="stat-label">Gömülü Altyazı</div></div>
    </div>
  </div>

  <div class="card">
    <div class="section-label">Dosya Yükle</div>
    <div class="drop-zone" id="dropZone">
      <input type="file" id="fileInput" accept=".mp3,.mp4,.m4a,.wav,.ogg,.mov,.avi,.mkv,.webm" multiple>
      <div class="drop-icon">🎬</div>
      <div class="drop-title">Dosyaları buraya sürükle veya seç</div>
      <div class="drop-sub">Video veya ses — birden fazla dosya desteklenir</div>
      <div class="drop-formats">
        <span class="fmt-tag">MP4</span><span class="fmt-tag">MOV</span><span class="fmt-tag">MKV</span>
        <span class="fmt-tag">M4A</span><span class="fmt-tag">MP3</span><span class="fmt-tag">WAV</span>
      </div>
    </div>
    <div class="file-list" id="fileList"></div>
  </div>

  <div class="card">
    <div class="section-label">Kaynak Dil</div>
    <div class="select-wrapper">
      <select id="sourceLang">
        <option value="auto">Otomatik tespit et</option>
        <option value="tr">Türkçe</option>
        <option value="en">İngilizce</option>
        <option value="de">Almanca</option>
        <option value="fr">Fransızca</option>
        <option value="es">İspanyolca</option>
        <option value="it">İtalyanca</option>
        <option value="ar">Arapça</option>
        <option value="ru">Rusça</option>
        <option value="ja">Japonca</option>
        <option value="ko">Korece</option>
        <option value="zh">Çince</option>
      </select>
    </div>
  </div>

  <div class="card">
    <div class="section-label">Hedef Diller</div>
    <div class="lang-controls">
      <button class="lang-btn" type="button" onclick="selectAll()">Tümünü Seç</button>
      <button class="lang-btn" type="button" onclick="clearAll()">Temizle</button>
      <span class="lang-count">Seçili: <span id="selCount">2</span></span>
    </div>
    <div class="lang-grid" id="langGrid">
      {% for code, name in languages.items() %}
      <label class="lang-chip {% if code in ['EN','DE'] %}active{% endif %}">
        <input type="checkbox" value="{{ code }}" {% if code in ['EN','DE'] %}checked{% endif %} onchange="updateCount()">
        {{ code }}
      </label>
      {% endfor %}
    </div>
  </div>

  <div class="card">
    <div class="section-label">Webhook <span style="color:var(--muted);font-size:10px;text-transform:none;letter-spacing:0">— isteğe bağlı</span></div>
    <input type="text" id="webhookUrl" class="webhook-input" placeholder="https://hooks.slack.com/... veya boş bırakın">
  </div>

  <button class="submit-btn" onclick="startProcess()" id="submitBtn">İşlemi Başlat →</button>
  <div class="progress-bar" id="progressBar"><div class="progress-fill" id="progressFill" style="width:0%"></div></div>
  <div class="status-text" id="statusText"></div>
  <div class="global-status" id="globalStatus"></div>
  <div class="results" id="results"></div>
</div>

<script>
  const dropZone = document.getElementById('dropZone');
  const fileInput = document.getElementById('fileInput');
  dropZone.addEventListener('dragover', e => { e.preventDefault(); dropZone.classList.add('drag-over'); });
  dropZone.addEventListener('dragleave', () => dropZone.classList.remove('drag-over'));
  dropZone.addEventListener('drop', e => { e.preventDefault(); dropZone.classList.remove('drag-over'); fileInput.files = e.dataTransfer.files; updateFileList(); });
  fileInput.addEventListener('change', updateFileList);

  function updateFileList() {
    const list = document.getElementById('fileList');
    list.innerHTML = '';
    for (const f of fileInput.files) list.innerHTML += `<span class="file-chip">🎬 ${f.name}</span>`;
  }

  function updateCount() {
    document.getElementById('selCount').textContent = document.querySelectorAll('#langGrid input:checked').length;
  }

  function selectAll() {
    document.querySelectorAll('#langGrid .lang-chip').forEach(c => { c.classList.add('active'); c.querySelector('input').checked = true; });
    updateCount();
  }
  function clearAll() {
    document.querySelectorAll('#langGrid .lang-chip').forEach(c => { c.classList.remove('active'); c.querySelector('input').checked = false; });
    updateCount();
  }

  document.getElementById('langGrid').addEventListener('click', e => {
    const chip = e.target.closest('.lang-chip');
    if (!chip) return;
    const cb = chip.querySelector('input');
    cb.checked = !cb.checked;
    chip.classList.toggle('active', cb.checked);
    updateCount();
  });

  async function startProcess() {
    const files = fileInput.files;
    if (!files || files.length === 0) { alert('Lütfen en az bir dosya seçin.'); return; }
    const selected = Array.from(document.querySelectorAll('#langGrid input:checked')).map(cb => cb.value);
    if (selected.length === 0) { alert('En az bir hedef dil seçin.'); return; }
    const sourceLang = document.getElementById('sourceLang').value;
    const webhookVal = document.getElementById('webhookUrl').value.trim();
    const btn = document.getElementById('submitBtn');
    const progressBar = document.getElementById('progressBar');
    const progressFill = document.getElementById('progressFill');
    const statusText = document.getElementById('statusText');
    const globalStatus = document.getElementById('globalStatus');
    const results = document.getElementById('results');

    btn.disabled = true; btn.textContent = 'İşleniyor...';
    progressBar.style.display = 'block'; statusText.style.display = 'block';
    results.innerHTML = ''; globalStatus.style.display = 'none';

    let completed = 0;
    for (let i = 0; i < files.length; i++) {
      const file = files[i];
      const cardId = 'card_' + i;
      progressFill.style.width = Math.round((i / files.length) * 60) + '%';
      statusText.textContent = `${i+1}/${files.length} — ${file.name} işleniyor...`;

      results.innerHTML += `<div class="result-card processing" id="${cardId}">
        <div class="result-header">
          <div class="result-icon">⏳</div>
          <div><div class="result-filename">${file.name}</div>
          <div class="result-meta"><span class="spinner"></span> İşleniyor — ${selected.length} dil</div></div>
        </div></div>`;

      const fd = new FormData();
      fd.append('file', file); fd.append('langs', selected.join(','));
      fd.append('source_lang', sourceLang);
      if (webhookVal) fd.append('webhook_url', webhookVal);

      try {
        const r = await fetch('/process', { method: 'POST', body: fd });
        const result = await r.json();
        const card = document.getElementById(cardId);

        if (result.status === 'ok') {
          completed++;
          let srtLinks = `<a class="dl-btn srt" href="/download/${result.job_id}/srt/${result.detected_lang.toUpperCase()}">⬇ ${result.detected_lang.toUpperCase()} (kaynak)</a>`;
          result.langs.forEach(l => { srtLinks += `<a class="dl-btn srt" href="/download/${result.job_id}/srt/${l}">⬇ ${l}</a>`; });
          let editLinks = `<a class="dl-btn edit" href="/edit/${result.job_id}/${result.detected_lang.toUpperCase()}">✎ ${result.detected_lang.toUpperCase()} düzenle</a>`;
          result.langs.forEach(l => { editLinks += `<a class="dl-btn edit" href="/edit/${result.job_id}/${l}">✎ ${l} düzenle</a>`; });
          let mp4Links = '';
          if (result.burned_langs && result.burned_langs.length > 0)
            result.burned_langs.forEach(l => { mp4Links += `<a class="dl-btn mp4" href="/download/${result.job_id}/mp4/${l}">▶ ${l}</a>`; });
          card.className = 'result-card success';
          card.innerHTML = `<div class="result-header"><div class="result-icon">✅</div>
            <div><div class="result-filename">${file.name}</div>
            <div class="result-meta">Kaynak: ${result.detected_lang.toUpperCase()} · ${result.langs.length} dil${result.is_video ? ' · Altyazılı video hazır' : ''}</div></div></div>
            <div class="dl-group"><div class="dl-group-label">SRT Altyazı</div><div class="dl-links">${srtLinks}</div></div>
            <div class="dl-group" style="margin-top:10px"><div class="dl-group-label">Düzenle</div><div class="dl-links">${editLinks}</div></div>
            ${mp4Links ? `<div class="dl-group" style="margin-top:10px"><div class="dl-group-label">Altyazılı Video</div><div class="dl-links">${mp4Links}</div></div>` : ''}`;
        } else {
          card.className = 'result-card error';
          card.innerHTML = `<div class="result-header"><div class="result-icon">❌</div>
            <div><div class="result-filename">${file.name}</div>
            <div class="result-meta" style="color:#f87171">${result.message}</div></div></div>`;
        }
      } catch(e) {
        const card = document.getElementById(cardId);
        card.className = 'result-card error';
        card.innerHTML = `<div class="result-header"><div class="result-icon">❌</div><div><div class="result-filename">${file.name}</div><div class="result-meta" style="color:#f87171">Bağlantı hatası</div></div></div>`;
      }
    }

    progressFill.style.width = '100%';
    setTimeout(() => { progressBar.style.display = 'none'; statusText.style.display = 'none'; }, 800);
    globalStatus.style.display = 'block'; globalStatus.className = 'global-status done';
    globalStatus.textContent = `✓ ${completed}/${files.length} dosya başarıyla tamamlandı.`;
    btn.disabled = false; btn.textContent = 'İşlemi Başlat →';
  }
</script>
</body>
</html>"""

# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template_string(HTML, languages=LANGUAGES, lang_count=len(LANGUAGES))

@app.route("/process", methods=["POST"])
def process():
    if "file" not in request.files:
        return jsonify({"status": "error", "message": "Dosya bulunamadi"})
    file = request.files["file"]
    if file.filename == "":
        return jsonify({"status": "error", "message": "Dosya secilmedi"})

    langs_param = request.form.get("langs", "EN,DE")
    source_lang_param = request.form.get("source_lang", "auto")
    webhook_url = request.form.get("webhook_url", "").strip()
    selected_langs = [l.strip() for l in langs_param.split(",") if l.strip() in LANGUAGES]
    if not selected_langs:
        selected_langs = ["EN", "DE"]

    job_id = str(uuid.uuid4())[:8]
    input_path = os.path.join(UPLOAD_FOLDER, job_id + "_" + file.filename)
    file.save(input_path)
    file_is_video = is_video(input_path)

    try:
        start_time = time.time()
        media_duration = get_media_duration(input_path)

        model = get_model(MODEL_PATH)
        lang_param = None if source_lang_param == "auto" else source_lang_param

        segments_gen, info = model.transcribe(
            input_path, language=lang_param, task="transcribe",
            beam_size=5, temperature=0.0, vad_filter=True
        )
        detected_lang = info.language
        segments = [{"start": s.start, "end": s.end, "text": s.text} for s in segments_gen]

        source_lang_code = WHISPER_TO_GOOGLE.get(detected_lang, "auto")
        base = os.path.join(OUTPUT_FOLDER, job_id)

        source_srt = base + "_" + detected_lang.upper() + ".srt"
        save_srt(segments, source_srt)

        filtered_langs = [l for l in selected_langs if l.lower() != detected_lang.lower()]
        srt_map = {detected_lang.upper(): source_srt}

        for code in filtered_langs:
            tgt = code.lower()
            if code == "ZH-CN": tgt = "zh-CN"
            elif code == "ZH-TW": tgt = "zh-TW"
            segs = translate_segments(segments, source_lang_code, tgt)
            srt_path = base + "_" + code.replace("-", "_") + ".srt"
            save_srt(segs, srt_path)
            srt_map[code] = srt_path

        burned_langs = []
        burned_paths = []
        if file_is_video:
            for lang_code, srt_path in srt_map.items():
                out_path = base + "_" + lang_code + "_burned.mp4"
                if burn_subtitles(input_path, srt_path, out_path):
                    burned_langs.append(lang_code)
                    burned_paths.append(out_path)

        processing_seconds = time.time() - start_time

        save_job(job_id, file.filename, detected_lang, filtered_langs, file_is_video, burned_langs,
                  media_duration_seconds=media_duration, processing_seconds=processing_seconds)

        # ── Zero-Retention: input + tum output dosyalarini gecikmeli sil ──────
        all_paths = [input_path] + list(srt_map.values()) + burned_paths
        schedule_cleanup(job_id, all_paths)

        if webhook_url:
            payload = {"job_id": job_id, "status": "ok", "filename": file.filename,
                       "detected_lang": detected_lang, "langs": filtered_langs,
                       "burned_langs": burned_langs, "is_video": file_is_video}
            threading.Thread(target=send_webhook, args=(webhook_url, payload), daemon=True).start()

        return jsonify({"status": "ok", "job_id": job_id, "langs": filtered_langs,
                        "detected_lang": detected_lang, "burned_langs": burned_langs, "is_video": file_is_video})
    except Exception as e:
        save_job(job_id, file.filename, "unknown", [], False, [], status="error")
        return jsonify({"status": "error", "message": str(e)})

@app.route("/download/<job_id>/srt/<lang>")
def download_srt(job_id, lang):
    path = os.path.join(OUTPUT_FOLDER, job_id + "_" + lang + ".srt")
    if not os.path.exists(path):
        return "Dosya bulunamadi", 404
    return send_file(path, as_attachment=True)

@app.route("/download/<job_id>/mp4/<lang>")
def download_mp4(job_id, lang):
    path = os.path.join(OUTPUT_FOLDER, job_id + "_" + lang + "_burned.mp4")
    if not os.path.exists(path):
        return "Dosya bulunamadi", 404
    return send_file(path, as_attachment=True)

# ── Human-in-the-Loop: video stream + editor ──────────────────────────────────
@app.route("/media/<job_id>/<lang>")
def media_stream(job_id, lang):
    media = get_job_media(job_id, lang)
    if not media["video_path"]:
        return "Video bulunamadi (silinmis olabilir)", 404
    return send_file(media["video_path"], conditional=True)

@app.route("/edit/<job_id>/<lang>")
def edit_page(job_id, lang):
    media = get_job_media(job_id, lang)
    if not media["srt_path"]:
        return "<h2 style='font-family:sans-serif;color:#f87171;padding:40px'>Bu isin SRT dosyasi bulunamadi. Silinmis olabilir (zero-retention).</h2>", 404

    cues = parse_srt(media["srt_path"])
    has_video = media["video_path"] is not None

    cues_html = ""
    for i, cue in enumerate(cues):
        cues_html += f"""
        <div class="cue-row" data-start="{cue['start_seconds']}" data-index="{i}">
          <div class="cue-time" onclick="seekTo({cue['start_seconds']})">{cue['start']}</div>
          <textarea class="cue-text" data-index="{i}">{cue['text']}</textarea>
        </div>"""

    video_html = f'<video id="player" controls src="/media/{job_id}/{lang}"></video>' if has_video else '<div class="no-video">Video bulunamadi — sadece metin duzenleme aktif</div>'

    html = f"""<!DOCTYPE html><html lang="tr"><head><title>SubSync — Duzenle</title><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600;700;800&display=swap" rel="stylesheet">
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:'Outfit',sans-serif;background:#07080f;color:#f0f2f8;min-height:100vh;padding:24px}}
.container{{max-width:1000px;margin:0 auto}}
nav{{display:flex;align-items:center;justify-content:space-between;margin-bottom:24px}}
.logo{{font-weight:800;font-size:20px;background:linear-gradient(135deg,#38bdf8,#a78bfa,#f43f5e);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;text-decoration:none}}
.back-btn{{font-size:13px;color:rgba(240,242,248,0.5);text-decoration:none;padding:6px 14px;border-radius:8px;border:1px solid rgba(255,255,255,0.08)}}
#player{{width:100%;border-radius:14px;background:#000;margin-bottom:20px}}
.no-video{{background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.08);border-radius:14px;padding:40px;text-align:center;color:rgba(240,242,248,0.4);margin-bottom:20px}}
.cue-list{{display:flex;flex-direction:column;gap:8px;max-height:50vh;overflow-y:auto;margin-bottom:20px}}
.cue-row{{display:flex;gap:12px;background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.08);border-radius:12px;padding:12px;transition:border-color 0.2s}}
.cue-row.active{{border-color:rgba(56,189,248,0.5);background:rgba(56,189,248,0.06)}}
.cue-time{{flex-shrink:0;width:90px;font-family:monospace;font-size:12px;color:#38bdf8;cursor:pointer;padding-top:8px;user-select:none}}
.cue-time:hover{{text-decoration:underline}}
.cue-text{{flex:1;background:transparent;border:none;color:#f0f2f8;font-family:'Outfit',sans-serif;font-size:14px;resize:vertical;min-height:36px;outline:none}}
.button-row{{display:flex;gap:10px}}
.save-btn{{flex:1;padding:16px;border:none;border-radius:14px;background:linear-gradient(135deg,#38bdf8,#a78bfa,#f43f5e);color:white;font-family:'Outfit',sans-serif;font-size:15px;font-weight:700;cursor:pointer}}
.rebuild-btn{{padding:16px 20px;border:1px solid rgba(167,139,250,0.3);border-radius:14px;background:rgba(167,139,250,0.1);color:#a78bfa;font-family:'Outfit',sans-serif;font-size:14px;font-weight:600;cursor:pointer;white-space:nowrap}}
.rebuild-btn:hover{{background:rgba(167,139,250,0.2)}}
.rebuild-btn:disabled{{opacity:0.5;cursor:not-allowed}}
.save-status{{text-align:center;margin-top:12px;font-size:13px;color:rgba(240,242,248,0.5)}}
.warning-banner{{background:rgba(251,191,36,0.08);border:1px solid rgba(251,191,36,0.2);border-radius:10px;padding:12px 16px;font-size:12px;color:rgba(251,191,36,0.9);margin-bottom:16px}}
.video-stale-warning{{background:rgba(251,191,36,0.08);border:1px solid rgba(251,191,36,0.2);border-radius:10px;padding:12px 16px;font-size:12px;color:rgba(251,191,36,0.9);margin-top:12px}}
</style></head><body><div class="container">
<nav><a href="/" class="logo">SubSync Studio</a><a href="/" class="back-btn">← Ana Sayfa</a></nav>
<div class="warning-banner">✎ Duzenleme modu aktif — bu is icin otomatik silme (zero-retention) gecici olarak durduruldu. Kaydedince yeniden 1 saatlik sayac baslar.</div>
{video_html}
<div class="cue-list" id="cueList">{cues_html}</div>
<div class="button-row">
  <button class="save-btn" onclick="saveEdits()">Kaydet (sadece metin)</button>
  <button class="rebuild-btn" onclick="rebuildVideo()">Videoyu Yeniden Oluştur</button>
</div>
<div class="save-status" id="saveStatus"></div>
<div class="video-stale-warning" id="staleWarning" style="display:none">
  ⚠ SRT güncellendi ama video hâlâ eski altyazıyı gösteriyor. Güncellemek için "Videoyu Yeniden Oluştur"a bas.
</div>
</div>
<script>
const jobId = "{job_id}";
const lang = "{lang}";
const player = document.getElementById('player');

// Sayfa acilinca zero-retention timer'ini iptal et
fetch(`/edit/${{jobId}}/${{lang}}/cancel-cleanup`, {{method: 'POST'}});

function seekTo(seconds) {{
  if (player) {{ player.currentTime = seconds; player.play(); }}
}}

if (player) {{
  player.addEventListener('timeupdate', () => {{
    const rows = document.querySelectorAll('.cue-row');
    let activeRow = null;
    rows.forEach(row => {{
      const start = parseFloat(row.dataset.start);
      if (player.currentTime >= start) activeRow = row;
    }});
    rows.forEach(r => r.classList.remove('active'));
    if (activeRow) activeRow.classList.add('active');
  }});
}}

async function saveEdits() {{
  const statusEl = document.getElementById('saveStatus');
  statusEl.textContent = 'Kaydediliyor...';
  const rows = document.querySelectorAll('.cue-row');
  const cues = Array.from(rows).map(row => ({{
    start: row.querySelector('.cue-time').textContent.trim(),
    text: row.querySelector('.cue-text').value
  }}));
  try {{
    const r = await fetch(`/edit/${{jobId}}/${{lang}}/save`, {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{cues: cues}})
    }});
    const result = await r.json();
    if (result.status === 'ok') {{
      statusEl.textContent = '✓ Kaydedildi. Zero-retention yeniden baslatildi.';
      document.getElementById('staleWarning').style.display = 'block';
    }} else {{
      statusEl.textContent = '✗ Hata: ' + result.message;
    }}
  }} catch(e) {{
    statusEl.textContent = '✗ Baglanti hatasi';
  }}
}}

async function rebuildVideo() {{
  const statusEl = document.getElementById('saveStatus');
  statusEl.textContent = 'Video yeniden olusturuluyor... (biraz surebilir)';
  try {{
    const r = await fetch(`/edit/${{jobId}}/${{lang}}/rebuild-video`, {{method: 'POST'}});
    const result = await r.json();
    if (result.status === 'ok') {{
      statusEl.textContent = '✓ Video guncellendi.';
      document.getElementById('staleWarning').style.display = 'none';
      player.src = `/media/${{jobId}}/${{lang}}?t=${{Date.now()}}`;
    }} else {{
      statusEl.textContent = '✗ Hata: ' + result.message;
    }}
  }} catch(e) {{
    statusEl.textContent = '✗ Baglanti hatasi';
  }}
}}
</script>
</body></html>"""
    return html

@app.route("/edit/<job_id>/<lang>/cancel-cleanup", methods=["POST"])
def edit_cancel_cleanup(job_id, lang):
    cancelled = cancel_cleanup(job_id)
    return jsonify({"status": "ok", "cancelled": cancelled})

@app.route("/edit/<job_id>/<lang>/save", methods=["POST"])
def edit_save(job_id, lang):
    data = request.get_json()
    cues_in = data.get("cues", [])
    media = get_job_media(job_id, lang)
    if not media["srt_path"]:
        return jsonify({"status": "error", "message": "SRT dosyasi bulunamadi"}), 404

    original_cues = parse_srt(media["srt_path"])
    if len(cues_in) != len(original_cues):
        return jsonify({"status": "error", "message": "Cue sayisi uyusmuyor"}), 400

    updated_cues = []
    for i, cue_in in enumerate(cues_in):
        updated_cues.append({
            "start": original_cues[i]["start"],
            "end": original_cues[i]["end"],
            "text": cue_in["text"]
        })

    try:
        write_srt_from_cues(updated_cues, media["srt_path"])
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

    # Duzenleme bitti, tekrar zero-retention'a al
    remaining_paths = [p for p in [media["video_path"], media["srt_path"]] if p]
    schedule_cleanup(job_id, remaining_paths)

    return jsonify({"status": "ok"})

@app.route("/edit/<job_id>/<lang>/rebuild-video", methods=["POST"])
def edit_rebuild_video(job_id, lang):
    media = get_job_media(job_id, lang)
    if not media["srt_path"]:
        return jsonify({"status": "error", "message": "SRT dosyasi bulunamadi"}), 404

    # Orijinal video (burned olmayan) gerekli - uploads altinda job_id ile baslayan dosya
    matches = glob.glob(os.path.join(UPLOAD_FOLDER, f"{job_id}_*"))
    if not matches:
        return jsonify({"status": "error", "message": "Orijinal video bulunamadi (silinmis olabilir)"}), 404
    original_video = matches[0]

    burned_path = os.path.join(OUTPUT_FOLDER, f"{job_id}_{lang}_burned.mp4")

    success = burn_subtitles(original_video, media["srt_path"], burned_path)
    if not success:
        return jsonify({"status": "error", "message": "Video render hatasi"}), 500

    # Yeniden zero-retention'a al (video + srt)
    schedule_cleanup(job_id, [burned_path, media["srt_path"]])

    return jsonify({"status": "ok"})

@app.route("/history")
def history():
    jobs = get_history(50)
    stats = get_stats()
    rows = ""
    for j in jobs:
        tip = '<span style="font-size:11px;font-weight:600;padding:3px 8px;border-radius:5px;background:rgba(56,189,248,0.1);border:1px solid rgba(56,189,248,0.2);color:#38bdf8">VIDEO</span>' if j["is_video"] else '<span style="font-size:11px;font-weight:600;padding:3px 8px;border-radius:5px;background:rgba(167,139,250,0.1);border:1px solid rgba(167,139,250,0.2);color:#a78bfa">SES</span>'
        durum = '<span style="font-size:11px;font-weight:700;padding:3px 10px;border-radius:20px;background:rgba(52,211,153,0.12);border:1px solid rgba(52,211,153,0.25);color:#34d399">OK</span>' if j["status"] == "ok" else '<span style="font-size:11px;font-weight:700;padding:3px 10px;border-radius:20px;background:rgba(248,113,113,0.12);border:1px solid rgba(248,113,113,0.25);color:#f87171">HATA</span>'
        det = j["detected_lang"].upper() if j["detected_lang"] else "-"
        rows += f'<tr><td style="color:rgba(240,242,248,0.45);font-size:12px">{j["created_at"]}</td><td style="font-family:monospace;font-size:12px;color:rgba(240,242,248,0.6)">{j["job_id"]}</td><td style="font-weight:500">{j["filename"]}</td><td>{tip}</td><td style="font-size:12px;font-weight:600;color:#38bdf8">{det}</td><td style="font-size:12px;color:rgba(240,242,248,0.6)">{j["target_langs"] or "-"}</td><td style="font-size:12px;color:rgba(240,242,248,0.6)">{j["burned_langs"] or "-"}</td><td>{durum}</td></tr>'

    html = f"""<!DOCTYPE html><html lang="tr"><head><title>SubSync — Geçmiş</title><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600;700;800&display=swap" rel="stylesheet">
<style>*{{box-sizing:border-box;margin:0;padding:0}}body{{font-family:'Outfit',sans-serif;background:#07080f;color:#f0f2f8;min-height:100vh;padding:0 24px 60px}}body::before{{content:'';position:fixed;inset:0;background:radial-gradient(ellipse 80% 60% at 20% 10%,rgba(56,189,248,0.07) 0%,transparent 60%),radial-gradient(ellipse 60% 80% at 80% 80%,rgba(167,139,250,0.07) 0%,transparent 60%);pointer-events:none;z-index:0}}.container{{max-width:1100px;margin:0 auto;position:relative;z-index:1}}nav{{display:flex;align-items:center;justify-content:space-between;padding:24px 0;border-bottom:1px solid rgba(255,255,255,0.08);margin-bottom:48px}}.logo{{font-weight:800;font-size:20px;letter-spacing:-0.5px;background:linear-gradient(135deg,#38bdf8,#a78bfa,#f43f5e);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;text-decoration:none}}.back-btn{{font-size:13px;font-weight:500;color:rgba(240,242,248,0.5);text-decoration:none;padding:6px 14px;border-radius:8px;border:1px solid rgba(255,255,255,0.08);transition:all 0.2s}}.back-btn:hover{{background:rgba(255,255,255,0.04);color:#f0f2f8}}.page-title{{font-size:28px;font-weight:800;letter-spacing:-1px;margin-bottom:32px}}.stats{{display:flex;gap:16px;margin-bottom:32px;flex-wrap:wrap}}.stat{{background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.08);border-radius:16px;padding:20px 28px;text-align:center;min-width:140px}}.stat-num{{font-size:32px;font-weight:800;background:linear-gradient(135deg,#38bdf8,#a78bfa);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}}.stat-label{{font-size:12px;color:rgba(240,242,248,0.45);margin-top:4px}}.table-wrap{{background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.08);border-radius:20px;overflow:hidden}}table{{width:100%;border-collapse:collapse}}thead tr{{border-bottom:1px solid rgba(255,255,255,0.08)}}th{{padding:14px 18px;text-align:left;font-size:11px;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:rgba(240,242,248,0.4)}}td{{padding:14px 18px;border-bottom:1px solid rgba(255,255,255,0.04);font-size:13px;color:#f0f2f8}}tbody tr:last-child td{{border-bottom:none}}tbody tr:hover td{{background:rgba(255,255,255,0.02)}}</style></head>
<body><div class="container"><nav><a href="/" class="logo">SubSync Studio</a><a href="/" class="back-btn">← Ana Sayfa</a></nav>
<div class="page-title">İşlem Geçmişi</div>
<div class="stats">
  <div class="stat"><div class="stat-num">{stats["total"]}</div><div class="stat-label">Toplam İşlem</div></div>
  <div class="stat"><div class="stat-num">{stats["videos"]}</div><div class="stat-label">Video</div></div>
  <div class="stat"><div class="stat-num">{stats["success"]}</div><div class="stat-label">Başarılı</div></div>
</div>
<div class="table-wrap"><table>
<thead><tr><th>Tarih</th><th>Job ID</th><th>Dosya</th><th>Tip</th><th>Kaynak</th><th>Hedef Diller</th><th>Burn-in</th><th>Durum</th></tr></thead>
<tbody>{rows if rows else '<tr><td colspan="8" style="text-align:center;padding:60px;color:rgba(240,242,248,0.3)">Henüz işlem yapılmadı</td></tr>'}</tbody>
</table></div></div></body></html>"""
    return html

@app.route("/roi")
def roi_dashboard():
    roi = get_roi_stats()
    html = f"""<!DOCTYPE html><html lang="tr"><head><title>SubSync — ROI</title><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600;700;800&display=swap" rel="stylesheet">
<style>*{{box-sizing:border-box;margin:0;padding:0}}body{{font-family:'Outfit',sans-serif;background:#07080f;color:#f0f2f8;min-height:100vh;padding:0 24px 60px}}
.container{{max-width:900px;margin:0 auto;padding-top:24px;position:relative;z-index:1}}
nav{{display:flex;align-items:center;justify-content:space-between;padding:24px 0;border-bottom:1px solid rgba(255,255,255,0.08);margin-bottom:48px}}
.logo{{font-weight:800;font-size:20px;background:linear-gradient(135deg,#38bdf8,#a78bfa,#f43f5e);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;text-decoration:none}}
.back-btn{{font-size:13px;font-weight:500;color:rgba(240,242,248,0.5);text-decoration:none;padding:6px 14px;border-radius:8px;border:1px solid rgba(255,255,255,0.08);transition:all 0.2s}}
.back-btn:hover{{background:rgba(255,255,255,0.04);color:#f0f2f8}}
.page-title{{font-size:28px;font-weight:800;margin-bottom:8px}}
.subtitle{{color:rgba(240,242,248,0.45);font-size:14px;margin-bottom:32px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:16px;margin-bottom:32px}}
.stat{{background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.08);border-radius:16px;padding:24px}}
.stat-num{{font-size:32px;font-weight:800;background:linear-gradient(135deg,#38bdf8,#a78bfa);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}}
.stat-label{{font-size:12px;color:rgba(240,242,248,0.45);margin-top:6px}}
.assumption-box{{background:rgba(251,191,36,0.08);border:1px solid rgba(251,191,36,0.2);border-radius:14px;padding:16px 20px;font-size:12px;color:rgba(251,191,36,0.9);line-height:1.6}}
</style></head><body><div class="container">
<nav><a href="/" class="logo">SubSync Studio</a><a href="/" class="back-btn">← Ana Sayfa</a></nav>
<div class="page-title">ROI Dashboard</div>
<div class="subtitle">{roi['job_count']} basarili islem uzerinden hesaplanmistir</div>
<div class="grid">
  <div class="stat"><div class="stat-num">{roi['total_media_minutes']}</div><div class="stat-label">Toplam Medya (dk)</div></div>
  <div class="stat"><div class="stat-num">{roi['total_processing_minutes']}</div><div class="stat-label">SubSync Isleme Suresi (dk)</div></div>
  <div class="stat"><div class="stat-num">{roi['estimated_manual_minutes']}</div><div class="stat-label">Tahmini Manuel Sure (dk)</div></div>
  <div class="stat"><div class="stat-num">{roi['time_saved_minutes']}</div><div class="stat-label">Kazanilan Zaman (dk)</div></div>
  <div class="stat"><div class="stat-num">₺{roi['estimated_cost_saved_try']}</div><div class="stat-label">Tahmini Maliyet Tasarrufu</div></div>
</div>
<div class="assumption-box">
  <strong>Varsayimlar:</strong> Manuel islem, dil basina medya dakikasi icin ortalama {roi['assumptions']['manual_minutes_per_media_minute_per_lang']} dakika surer varsayilmistir.
  Freelance transkripsiyon/ceviri ucreti dakika basi ₺{roi['assumptions']['manual_rate_try_per_minute']} olarak kabul edilmistir.
  Bu rakamlar gercek piyasa arastirmasina degil, makul bir tahmine dayanir.
</div>
</div></body></html>"""
    return html

# ── REST API ──────────────────────────────────────────────────────────────────
@app.route("/api/process", methods=["POST"])
@require_api_key
def api_process():
    if "file" not in request.files:
        return jsonify({"status": "error", "message": "file alani gerekli"}), 400
    file = request.files["file"]
    if file.filename == "":
        return jsonify({"status": "error", "message": "Dosya secilmedi"}), 400

    langs_param = request.form.get("langs", "EN,DE")
    source_lang_param = request.form.get("source_lang", "auto")
    selected_langs = [l.strip().upper() for l in langs_param.split(",") if l.strip().upper() in LANGUAGES]
    if not selected_langs:
        return jsonify({"status": "error", "message": "Gecerli dil kodu bulunamadi"}), 400

    job_id = str(uuid.uuid4())[:8]
    input_path = os.path.join(UPLOAD_FOLDER, job_id + "_" + file.filename)
    file.save(input_path)
    file_is_video = is_video(input_path)

    try:
        start_time = time.time()
        media_duration = get_media_duration(input_path)

        model = get_model(MODEL_PATH)
        lang_param = None if source_lang_param == "auto" else source_lang_param.lower()

        segments_gen, info = model.transcribe(
            input_path, language=lang_param, task="transcribe",
            beam_size=5, temperature=0.0, vad_filter=True
        )
        detected_lang = info.language
        segments = [{"start": s.start, "end": s.end, "text": s.text} for s in segments_gen]

        source_lang_code = WHISPER_TO_GOOGLE.get(detected_lang, "auto")
        base = os.path.join(OUTPUT_FOLDER, job_id)

        source_srt = base + "_" + detected_lang.upper() + ".srt"
        save_srt(segments, source_srt)

        filtered_langs = [l for l in selected_langs if l.lower() != detected_lang.lower()]
        srt_map = {detected_lang.upper(): source_srt}

        for code in filtered_langs:
            tgt = code.lower()
            if code == "ZH-CN": tgt = "zh-CN"
            elif code == "ZH-TW": tgt = "zh-TW"
            segs = translate_segments(segments, source_lang_code, tgt)
            srt_path = base + "_" + code.replace("-", "_") + ".srt"
            save_srt(segs, srt_path)
            srt_map[code] = srt_path

        burned_langs = []
        burned_paths = []
        if file_is_video:
            for lang_code, srt_path in srt_map.items():
                out_path = base + "_" + lang_code + "_burned.mp4"
                if burn_subtitles(input_path, srt_path, out_path):
                    burned_langs.append(lang_code)
                    burned_paths.append(out_path)

        processing_seconds = time.time() - start_time

        save_job(job_id, file.filename, detected_lang, filtered_langs, file_is_video, burned_langs,
                  media_duration_seconds=media_duration, processing_seconds=processing_seconds)

        # ── Zero-Retention: input + tum output dosyalarini gecikmeli sil ──────
        all_paths = [input_path] + list(srt_map.values()) + burned_paths
        schedule_cleanup(job_id, all_paths)

        files_urls = {}
        for lang_code in srt_map:
            files_urls[lang_code] = f"/api/download/{job_id}/srt/{lang_code}"
        for lang_code in burned_langs:
            files_urls[lang_code + "_video"] = f"/api/download/{job_id}/mp4/{lang_code}"

        return jsonify({"status": "ok", "job_id": job_id, "detected_lang": detected_lang,
                        "is_video": file_is_video, "langs": list(srt_map.keys()),
                        "burned_langs": burned_langs, "files": files_urls})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/api/download/<job_id>/srt/<lang>")
@require_api_key
def api_download_srt(job_id, lang):
    path = os.path.join(OUTPUT_FOLDER, job_id + "_" + lang + ".srt")
    if not os.path.exists(path):
        return jsonify({"status": "error", "message": "Dosya bulunamadi"}), 404
    return send_file(path, as_attachment=True)

@app.route("/api/download/<job_id>/mp4/<lang>")
@require_api_key
def api_download_mp4(job_id, lang):
    path = os.path.join(OUTPUT_FOLDER, job_id + "_" + lang + "_burned.mp4")
    if not os.path.exists(path):
        return jsonify({"status": "error", "message": "Dosya bulunamadi"}), 404
    return send_file(path, as_attachment=True)

@app.route("/api/languages")
def api_languages():
    return jsonify({"status": "ok", "count": len(LANGUAGES), "languages": LANGUAGES})

@app.route("/api/history")
@require_api_key
def api_history():
    return jsonify({"status": "ok", "stats": get_stats(), "jobs": get_history(50)})

@app.route("/api-docs")
def api_docs():
    return """<!DOCTYPE html><html><head><title>SubSync API</title><meta charset="utf-8">
<link href="https://fonts.googleapis.com/css2?family=Outfit:wght@400;600;700;800&display=swap" rel="stylesheet">
<style>*{box-sizing:border-box;margin:0;padding:0}body{font-family:'Outfit',sans-serif;background:#07080f;color:#f0f2f8;padding:40px 24px}
.container{max-width:860px;margin:0 auto}.logo{font-weight:800;font-size:20px;background:linear-gradient(135deg,#38bdf8,#a78bfa,#f43f5e);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;text-decoration:none;display:block;margin-bottom:40px}
h1{font-size:28px;font-weight:800;letter-spacing:-1px;margin-bottom:8px}p{color:rgba(240,242,248,0.5);margin-bottom:32px}
.ep{background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.08);border-radius:16px;margin-bottom:12px;overflow:hidden}
.ep-h{display:flex;align-items:center;gap:12px;padding:18px 24px}.method{font-size:11px;font-weight:800;padding:4px 10px;border-radius:6px}
.post{background:rgba(56,189,248,0.15);border:1px solid rgba(56,189,248,0.3);color:#38bdf8}.get{background:rgba(52,211,153,0.15);border:1px solid rgba(52,211,153,0.3);color:#34d399}
.ep-path{font-family:monospace;font-size:14px}.ep-desc{font-size:13px;color:rgba(240,242,248,0.4);margin-left:auto}
.ep-body{padding:0 24px 24px;font-size:13px;color:rgba(240,242,248,0.6)}
pre{background:rgba(0,0,0,0.4);border:1px solid rgba(255,255,255,0.08);border-radius:10px;padding:14px;font-family:monospace;font-size:12px;overflow-x:auto;margin-top:12px;white-space:pre-wrap}
.key-card{background:rgba(56,189,248,0.08);border:1px solid rgba(56,189,248,0.2);border-radius:14px;padding:16px 20px;margin-bottom:32px;font-size:13px;color:#38bdf8}
</style></head><body><div class="container">
<a href="/" class="logo">SubSync Studio</a>
<h1>API Dokümantasyonu</h1>
<p>SubSync Studio REST API — HTTP istekleriyle video lokalizasyonunu otomatize edin.</p>
<div class="key-card">🔑 Tüm /api/ isteklerine header ekle: <strong>X-API-Key: subsync-demo-key-2026</strong></div>
<div class="ep"><div class="ep-h"><span class="method post">POST</span><span class="ep-path">/api/process</span><span class="ep-desc">Video/ses işle</span></div>
<div class="ep-body">Form-data: <strong>file</strong> (zorunlu), <strong>langs</strong> (EN,DE,FR), <strong>source_lang</strong> (auto/tr/en...)
<pre>curl -X POST http://localhost:5000/api/process \\
  -H "X-API-Key: subsync-demo-key-2026" \\
  -F "file=@video.mp4" \\
  -F "langs=EN,DE" \\
  -F "source_lang=auto"</pre></div></div>
<div class="ep"><div class="ep-h"><span class="method get">GET</span><span class="ep-path">/api/languages</span><span class="ep-desc">52 dil listesi</span></div></div>
<div class="ep"><div class="ep-h"><span class="method get">GET</span><span class="ep-path">/api/history</span><span class="ep-desc">İşlem geçmişi</span></div></div>
<div class="ep"><div class="ep-h"><span class="method get">GET</span><span class="ep-path">/api/download/{job_id}/srt/{lang}</span><span class="ep-desc">SRT indir</span></div></div>
<div class="ep"><div class="ep-h"><span class="method get">GET</span><span class="ep-path">/api/download/{job_id}/mp4/{lang}</span><span class="ep-desc">Altyazılı video indir</span></div></div>
</div></body></html>"""

if __name__ == "__main__":
    print("[SubSync] Sunucu: http://localhost:5000")
    print("[SubSync] Model: " + MODEL_PATH)
    print("[SubSync] Zero-retention: " + str(CLEANUP_DELAY_SECONDS) + " saniye sonra dosyalar silinir")
    print("[SubSync] API: POST /api/process")
    app.run(debug=False, port=5000, host="0.0.0.0")