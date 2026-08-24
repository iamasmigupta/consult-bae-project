"""
ConsultBae – Flask backend for the audio collection app + merged people API.

Flask is wrapped with WsgiToAsgi so the entrypoint
`uvicorn server:app` can serve it without any config changes.

All endpoints are under /api/*.
"""

import io
import json
import os
import re
import sqlite3
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import soundfile as sf
from asgiref.wsgi import WsgiToAsgi
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from mutagen import File as MutagenFile

ROOT = Path(__file__).parent
DB_PATH = ROOT / "consultbae.db"
UPLOAD_DIR = ROOT / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

flask_app = Flask(__name__)
CORS(flask_app)


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def norm_phone(p: str) -> str:
    d = re.sub(r"\D", "", p or "")
    if len(d) > 10 and d.startswith("91"):
        d = d[-10:]
    if len(d) == 11 and d.startswith("0"):
        d = d[1:]
    return d if len(d) == 10 else ""


# ----------------------------- Audio analysis ------------------------------

def probe_audio(path: Path) -> dict:
    """Extract duration, sample_rate, bitrate, loudness (dBFS), noise_estimate."""
    out = {
        "duration_sec": 0.0,
        "sample_rate_hz": 0,
        "bitrate_kbps": 0,
        "loudness_db": 0.0,
        "noise_estimate": "unknown",
    }
    # 1) Bitrate + duration + sr via ffprobe (works for mp3/webm/ogg/wav/m4a).
    try:
        proc = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration,bit_rate",
                "-show_entries", "stream=sample_rate,bit_rate,codec_type",
                "-of", "json", str(path),
            ],
            capture_output=True, text=True, timeout=30,
        )
        meta = json.loads(proc.stdout or "{}")
        fmt = meta.get("format", {})
        streams = [s for s in meta.get("streams", []) if s.get("codec_type") == "audio"]
        if fmt.get("duration"):
            out["duration_sec"] = round(float(fmt["duration"]), 3)
        if fmt.get("bit_rate"):
            out["bitrate_kbps"] = int(int(fmt["bit_rate"]) / 1000)
        if streams:
            sr = streams[0].get("sample_rate")
            br = streams[0].get("bit_rate")
            if sr:
                out["sample_rate_hz"] = int(sr)
            if br and not out["bitrate_kbps"]:
                out["bitrate_kbps"] = int(int(br) / 1000)
    except Exception as e:
        print("ffprobe failed:", e)

    # 2) Loudness + noise via soundfile + numpy. Convert to wav first if needed.
    wav_path = path
    try:
        info = sf.info(str(path))
        if not out["sample_rate_hz"]:
            out["sample_rate_hz"] = info.samplerate
        if not out["duration_sec"]:
            out["duration_sec"] = round(info.frames / max(info.samplerate, 1), 3)
    except Exception:
        # decode to wav via ffmpeg
        wav_path = path.with_suffix(".decoded.wav")
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-i", str(path), "-ac", "1", "-ar", "22050",
                 str(wav_path)],
                capture_output=True, timeout=60, check=True,
            )
        except Exception as e:
            print("ffmpeg decode failed:", e)

    try:
        data, sr = sf.read(str(wav_path))
        if data.ndim > 1:
            data = data.mean(axis=1)
        # loudness as dBFS (peak-normalized RMS)
        rms = float(np.sqrt(np.mean(np.square(data))) + 1e-12)
        out["loudness_db"] = round(20 * np.log10(rms), 2)
        # noise estimate: compare RMS of quietest 10% frames to overall RMS
        frame = max(int(sr * 0.05), 1)  # 50 ms
        n_frames = len(data) // frame
        if n_frames > 4:
            frames = data[: n_frames * frame].reshape(n_frames, frame)
            frame_rms = np.sqrt(np.mean(frames ** 2, axis=1) + 1e-12)
            noise_floor = float(np.percentile(frame_rms, 10))
            snr = 20 * np.log10((rms + 1e-12) / (noise_floor + 1e-12))
            if snr > 25:
                out["noise_estimate"] = "clean"
            elif snr > 15:
                out["noise_estimate"] = "moderate"
            else:
                out["noise_estimate"] = "noisy"
        if not out["sample_rate_hz"]:
            out["sample_rate_hz"] = sr
        if not out["duration_sec"]:
            out["duration_sec"] = round(len(data) / max(sr, 1), 3)
    except Exception as e:
        print("soundfile analysis failed:", e)

    # bitrate fallback via mutagen for containers ffprobe missed
    if not out["bitrate_kbps"]:
        try:
            mf = MutagenFile(str(path))
            if mf and getattr(mf.info, "bitrate", None):
                out["bitrate_kbps"] = int(mf.info.bitrate / 1000)
        except Exception:
            pass
    # cleanup temp decoded file
    if wav_path != path and wav_path.exists():
        try:
            wav_path.unlink()
        except OSError:
            pass
    return out


# ----------------------------- Routes --------------------------------------

@flask_app.get("/api/health")
def health():
    return jsonify({"ok": True, "db": DB_PATH.exists()})


@flask_app.get("/api/people")
def list_people():
    q = (request.args.get("q") or "").strip().lower()
    conn = db()
    if q:
        like = f"%{q}%"
        rows = conn.execute(
            """SELECT * FROM people
               WHERE lower(full_name) LIKE ? OR primary_email LIKE ?
                     OR primary_phone LIKE ? OR lower(city) LIKE ?
                     OR lower(skills) LIKE ?
               ORDER BY full_name""",
            (like, like, like, like, like),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM people ORDER BY full_name").fetchall()
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        for k in ("aliases", "emails", "phones", "skills", "sources"):
            try:
                d[k] = json.loads(d[k] or "[]")
            except (TypeError, ValueError):
                d[k] = []
        result.append(d)
    return jsonify(result)


@flask_app.get("/api/people/stats")
def people_stats():
    conn = db()
    total = conn.execute("SELECT COUNT(*) c FROM people").fetchone()["c"]
    multi = conn.execute(
        "SELECT COUNT(*) c FROM people WHERE json_array_length(sources) > 1"
    ).fetchone()["c"]
    src_counts = {"naukri": 0, "gig": 0, "cbnexus": 0}
    for r in conn.execute("SELECT sources FROM people").fetchall():
        for s in json.loads(r["sources"] or "[]"):
            src_counts[s] = src_counts.get(s, 0) + 1
    issues = conn.execute(
        "SELECT issue_type, COUNT(*) c FROM data_issues GROUP BY issue_type"
    ).fetchall()
    conn.close()
    return jsonify({
        "total_people": total,
        "in_multiple_sources": multi,
        "per_source": src_counts,
        "issues_by_type": {r["issue_type"]: r["c"] for r in issues},
    })


@flask_app.get("/api/data-issues")
def data_issues():
    conn = db()
    rows = conn.execute("SELECT * FROM data_issues ORDER BY source, row_num").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@flask_app.post("/api/submissions")
def create_submission():
    name = (request.form.get("name") or "").strip()
    phone = (request.form.get("phone") or "").strip()
    if not name or not phone:
        return jsonify({"error": "name and phone are required"}), 400
    if "audio" not in request.files:
        return jsonify({"error": "audio file missing"}), 400
    f = request.files["audio"]
    if not f.filename:
        return jsonify({"error": "empty audio filename"}), 400

    sub_id = str(uuid.uuid4())
    # Preserve extension (needed for ffprobe container detection)
    ext = os.path.splitext(f.filename)[1].lower() or ".webm"
    stored_path = UPLOAD_DIR / f"{sub_id}{ext}"
    f.save(stored_path)

    props = probe_audio(stored_path)
    normalized = norm_phone(phone)

    conn = db()
    # try match to existing person via normalized phone
    person = None
    if normalized:
        person = conn.execute(
            "SELECT id FROM people WHERE primary_phone = ? OR phones LIKE ?",
            (normalized, f'%"{normalized}"%'),
        ).fetchone()

    person_id = person["id"] if person else None
    # If no match, create a lightweight person record for the submitter
    if not person_id:
        person_id = str(uuid.uuid4())
        conn.execute(
            """INSERT INTO people (id, full_name, aliases, primary_email, emails,
                primary_phone, phones, city, skills, sources, experience_years,
                current_ctc_lpa, applied_date, rate_inr_hr, worker_status,
                verified, projects_completed, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, '', 0, '', 0, 0, ?)""",
            (person_id, name, json.dumps([name]), "", json.dumps([]),
             normalized, json.dumps([normalized] if normalized else []),
             "", json.dumps([]), json.dumps(["audio_app"]),
             datetime.now(timezone.utc).isoformat()),
        )

    conn.execute(
        """INSERT INTO submissions (id, person_id, name, phone, audio_path, mime,
             duration_sec, sample_rate_hz, bitrate_kbps, loudness_db,
             noise_estimate, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (sub_id, person_id, name, phone, stored_path.name, f.mimetype,
         props["duration_sec"], props["sample_rate_hz"], props["bitrate_kbps"],
         props["loudness_db"], props["noise_estimate"],
         datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()
    return jsonify({"id": sub_id, "person_id": person_id, **props})


@flask_app.get("/api/submissions")
def list_submissions():
    conn = db()
    rows = conn.execute(
        """SELECT s.*, p.full_name AS person_name, p.sources AS person_sources
           FROM submissions s LEFT JOIN people p ON p.id = s.person_id
           ORDER BY s.created_at DESC"""
    ).fetchall()
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        try:
            d["person_sources"] = json.loads(d["person_sources"] or "[]")
        except (TypeError, ValueError):
            d["person_sources"] = []
        result.append(d)
    return jsonify(result)


@flask_app.get("/api/audio/<path:filename>")
def get_audio(filename):
    return send_from_directory(UPLOAD_DIR, filename)


# ----------------------------- Task 2 – dedup webhook ---------------------

@flask_app.post("/api/dedup/check")
def dedup_check():
    """Endpoint the n8n flow calls. Accepts either JSON list of contacts
    ({name, email, phone}) or a single object. Returns duplicates found in
    the merged people DB. Used by the n8n duplicate-alert workflow.
    """
    payload = request.get_json(silent=True) or {}
    items = payload if isinstance(payload, list) else payload.get("contacts") or [payload]
    conn = db()
    duplicates = []
    for it in items:
        email = (it.get("email") or "").strip().lower()
        phone = norm_phone(it.get("phone") or "")
        if not email and not phone:
            continue
        match = conn.execute(
            """SELECT id, full_name, primary_email, primary_phone, sources
               FROM people
               WHERE (? != '' AND (primary_email = ? OR emails LIKE ?))
                  OR (? != '' AND (primary_phone = ? OR phones LIKE ?))
               LIMIT 1""",
            (email, email, f'%"{email}"%', phone, phone, f'%"{phone}"%'),
        ).fetchone()
        if match:
            duplicates.append({
                "input": it,
                "matched": {
                    "id": match["id"],
                    "name": match["full_name"],
                    "email": match["primary_email"],
                    "phone": match["primary_phone"],
                    "sources": json.loads(match["sources"] or "[]"),
                },
            })
    conn.close()
    return jsonify({"count": len(duplicates), "duplicates": duplicates})


# ----------------------------- ASGI wrapper --------------------------------

app = WsgiToAsgi(flask_app)
