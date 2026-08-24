# ConsultBae — AI Automation Take-Home

> **Working software over polish** — merge three messy CSVs into one clean SQLite DB, wire up an n8n duplicate-alert flow, and collect audio submissions with automatic quality metrics.

**Stack:** Python 3.11+ · Flask (served through `asgiref.WsgiToAsgi` so `uvicorn` can run it) · SQLite · React 19 (thin HTML/JS shell) · FFmpeg / ffprobe · `soundfile` + `numpy` + `mutagen` · n8n.

---

## 0. What's in the repo

```
consultbae-takehome/
├── data/                            # the 3 raw CSVs
│   ├── source1.csv                  # Naukri applicants
│   ├── source2.csv                  # Gig workers
│   └── source3.csv                  # CBNexus contacts
├── scripts/merge_data.py            # Task 1 — merge pipeline
├── backend/
│   ├── server.py                    # Flask API (submissions, people, dedup)
│   ├── requirements.txt
│   ├── .env.example
│   └── uploads/                     # stored audio files (created on first run)
├── frontend/
│   ├── src/App.js                   # 4-tab UI
│   ├── src/App.css
│   ├── package.json
│   └── .env.example
├── n8n/duplicate_alert_flow.json    # Task 2 — n8n workflow export
├── render.yaml                      # Task 3 stretch — Render deploy blueprint
├── .gitignore
└── README.md                        # ← you are here
```

---

## 1. Setup — Windows (PowerShell)

Prereqs (install once):

```powershell
# Python 3.11+
winget install Python.Python.3.11

# Node.js 18+ and Yarn
winget install OpenJS.NodeJS.LTS
npm install -g yarn

# FFmpeg (required — the app calls ffprobe for duration/bitrate/sample-rate)
winget install Gyan.FFmpeg
```

**Close and reopen your terminal** so the new PATH picks up `python`, `yarn`, `ffmpeg`. Verify:

```powershell
python --version    # 3.11+
yarn --version
ffmpeg -version
```

### 1a. Backend

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
# If PowerShell blocks the activation script, run once:
#   Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
pip install -r requirements.txt
```

### 1b. Build the merged SQLite

```powershell
cd ..
python scripts\merge_data.py
# Expected:
#   Merged 60 unique people into backend\consultbae.db
#   Logged 25 data issues
#   People coverage per source: {'cbnexus': 30, 'gig': 30, 'naukri': 40}
```

### 1c. Run the backend

```powershell
cd backend
uvicorn server:app --host 0.0.0.0 --port 8001 --reload
```

Leave it running. Test in a browser: <http://localhost:8001/api/health> → `{"ok":true,"db":true}`.

### 1d. Frontend (new terminal tab)

```powershell
cd frontend
copy .env.example .env
yarn install
yarn start
```

Browser opens at <http://localhost:3000> — you should see the 4-tab app.

---

## 1'. Setup — macOS / Linux (bash)

```bash
# ffmpeg
brew install ffmpeg              # macOS
sudo apt-get install -y ffmpeg   # Ubuntu/Debian

# backend
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cd ..
python3 scripts/merge_data.py
cd backend && uvicorn server:app --host 0.0.0.0 --port 8001 --reload

# frontend (new terminal)
cd frontend
cp .env.example .env
yarn install
yarn start
```

---

## 2. Deploy to Render (Task 3 stretch)

`render.yaml` is a blueprint. In your Render dashboard:
`New → Blueprint → connect this GitHub repo → Apply`.

It creates:
- A Python service (installs ffmpeg + runs `merge_data.py` on build, serves `server:app` with `gunicorn -k uvicorn.workers.UvicornWorker`)
- A static site for the React frontend (set `REACT_APP_BACKEND_URL` to the backend URL after first deploy)

> **Note on free tier**: Render's free plan doesn't persist disk, so `consultbae.db` and uploaded audio get wiped on redeploy. For a real launch see Task 5 below (S3 + Postgres).

---

## 3. Using the app

Four tabs:

1. **Record audio** — enter name + phone, then record in-browser (`MediaRecorder`) or upload a file. On submit, ffprobe + soundfile extract duration, sample rate, bitrate, loudness (dBFS), and a noise estimate.
2. **All submissions** — list with play buttons.
3. **Merged people** — searchable table of the 60 unique people, with source badges (naukri / gig / cbnexus).
4. **Data issues** — every quality problem detected during the merge, grouped by type.

**Phone auto-link:** if the phone you enter (in any format) matches a person in the merged DB, the submission attaches to that person. Otherwise a new lightweight person row is created with source `audio_app`.

---

## 4. Task 1 — Merge pipeline

**Matching strategy:** union-find on two normalized keys per row.

| Key | Normalizer |
|-----|------------|
| email  | `strip().lower()` |
| phone  | strip everything non-digit → drop leading `91` if length > 10 → drop leading `0` if length 11 → keep the last 10 digits |

Rows that share **any** normalized email OR normalized phone collapse into one person. Names are never used as a join key (too fuzzy: `R. Verma` vs `Rohit Verma`, `RITU SHARMA` vs `Ritu Sharma`) — but every observed spelling is kept in an `aliases` JSON list on the person row. Cities and skills are unioned; conflicting values (like two spellings of "Bengaluru") are canonicalised through a small mapping table.

**Result on the supplied data:** 42 + 31 + 30 = **103 raw rows → 60 unique people**, with **25 people appearing in ≥2 sources**.

> **Note on the "60 vs 61" count you may see in the UI:** the merge pipeline itself produces exactly 60 unique people. The number grows past 60 as workers submit audio — every audio submission from a phone that doesn't match any of the 60 merged people creates a new "audio_app" person record (per Task 3's requirement that *"a record goes into your database from Task 1"*). This is intentional, not a merge inconsistency.

---

## 5. Task 2 — n8n automation

`n8n/duplicate_alert_flow.json` is a five-node workflow:

`Webhook (POST /consultbae-new-contacts)` → `HTTP POST /api/dedup/check` → `IF count > 0` → `Email` + `Slack` alert → `Respond`.

**To import it into n8n cloud:**
1. Sign in at <https://n8n.cloud> (free trial).
2. `Workflows → Import from File` → pick `n8n/duplicate_alert_flow.json`.
3. Set these env vars on the n8n side: `CONSULTBAE_API` (your backend URL), `ALERT_EMAIL`, `SLACK_CHANNEL`.
4. Wire up your own Email/Slack credentials on the two alert nodes.
5. **Execute Workflow** → copy the Test Webhook URL n8n gives you.

**Test it with curl:**
```powershell
curl.exe -X POST "<n8n test webhook URL>" -H "Content-Type: application/json" `
  -d '{\"contacts\":[{\"name\":\"Priya Singh\",\"phone\":\"+91-9000000287\"}]}'
```

The dedup endpoint reuses the exact same phone/email normalisation as the merge pipeline, so any phone format (`+91-9000000287`, `09000000287`, `919000000287`) still matches.

You can also hit the dedup endpoint directly (no n8n needed):
```powershell
curl.exe -X POST http://localhost:8001/api/dedup/check -H "Content-Type: application/json" `
  -d '{\"contacts\":[{\"phone\":\"+91-9000000287\",\"name\":\"Priya Singh\"}]}'
# → { "count": 1, "duplicates": [...] }
```

---

## 6. Task 3 — Audio collection app

- Records in-browser via `MediaRecorder` (WebM/Opus by default) **or** file upload
- On submit: file saved under `backend/uploads/<uuid><ext>`; a row is written to `submissions`
- Auto-extracted properties:
  - **duration_sec** — `ffprobe` (fallback: `soundfile.info`)
  - **sample_rate_hz** — `ffprobe`
  - **bitrate_kbps** — `ffprobe` (fallback: `mutagen`)
  - **loudness_db** — `20·log10(rms)` in dBFS via `soundfile` + `numpy`
  - **noise_estimate** — SNR from overall RMS vs the 10th-percentile 50 ms frame RMS; bucketed as `clean` (>25 dB SNR) / `moderate` / `noisy`
- If the submitter's phone (normalised) matches someone in `people`, the submission attaches to that person. Otherwise a lightweight person row is created with source `"audio_app"`.

---

## 7. Task 4 — Data issues report

Detected automatically by `merge_data.py` and stored in the `data_issues` table (Issues tab in the UI). Full breakdown:

| # | Category | Example | Handling |
|---|----------|---------|----------|
| 1 | **Multiple email domains** | `.com`, `.in`, `.org`, `mailtest.example.org` for the same person | Not a bug — kept as multiple emails per person |
| 2 | **`alt.` email prefix** | `alt.nikhil.chopra70@example.com` vs `nikhil.chopra70@example.com` | Different email string → these merge only if a shared phone links them. Flagged as `alt_email_prefix`. |
| 3 | **Phone format chaos** | `+919000000254`, `9000000237`, `09000000287`, `+91-9000000131`, `919000000260` | Normalised to bare 10-digit; leading `0`/`91`/`+91` stripped |
| 4 | **Name capitalisation** | `RITU SHARMA` vs `Ritu Sharma`; `R. Verma` vs `Rohit Verma` | Title-cased; every spelling kept in `aliases` |
| 5 | **Trailing whitespace in city** | `"Noida "`, `"gurugram "` | Trimmed |
| 6 | **City spellings** | Bangalore/Bengaluru, Gurgaon/Gurugram, Delhi/New Delhi/Delhi NCR | Mapped to canonical form |
| 7 | **Mixed CTC units (Naukri)** | `417964` (rupees) vs `4.2` (LPA) side by side | Values ≥10 000 divided by 1e5. Flagged as `ctc_in_rupees` (21 rows). |
| 8 | **Date format zoo (Naukri)** | `24-07-2026`, `2026-08-08`, `7 Jul 2026`, `07/13/2026` | Parsed with 6 formats; unparseable → empty + logged |
| 9 | **Mixed rate units (Gig)** | `1415/hr` vs `15k/month` | Normalised to INR/hour (assuming 22 × 8 = 176 h/month) |
| 10 | **Case in email (Gig)** | `ISHA.CHOPRA95@MAILTEST.EXAMPLE.ORG` | Lower-cased before matching |
| 11 | **Status vocabulary (Gig)** | `Active`, `active`, `ACTIVE`, `Inactive`, `paused` | Lower-cased |
| 12 | **Verified vocabulary (CBNexus)** | `Y`, `y`, `yes`, `Yes`, `N`, `No` | Mapped to `0`/`1` |
| 13 | **Fully blank row (Gig row 12)** | `,,,,,` | Skipped, logged as `empty_row` |
| 14 | **Column-shift row (Gig row 20)** | Skills string ended up in the `email_id` column, real email in the name column | Rejected outright, logged as `column_shift` |
| 15 | **Header printed inline (CBNexus row 16)** | `Name,Phone Number,City,Verified,Projects Completed` repeated inside data | Detected and skipped |
| 16 | **Exact duplicate row (Naukri)** | `Rohit Verma`/`R. Verma` at rows 25 & 31, identical email/phone | Merged into one person |
| 17 | **Exact duplicate row (Naukri)** | `Nikhil Chopra` rows 27 & 37 (one with `alt.` prefix) | Merged via shared phone |
| 18 | **Two people share a first-last name pair** | Two `Arjun Mehta`s in CBNexus (rows 5 & 28) with different phones | Correctly kept as two separate people because names are NOT a match key |

---

## 8. Task 5 — Launch to 5 000 gig workers over one weekend

### What breaks first

1. **Single-node backend.** One uvicorn worker on Render's free tier will queue and time out under bursty upload traffic. Fix: `k=uvicorn workers ≥ 2×CPU`, or move to Fly.io/ECS with autoscaling, and put uploads behind a queue (RQ/Celery) so the request returns fast and the analysis happens async.
2. **Local disk for audio.** Container disk fills quickly and isn't durable across redeploys. Fix: presigned S3/R2 uploads directly from the browser; DB only stores the object key + metadata.
3. **SQLite under concurrent writes.** Locks under bursty submits. Fix: move `submissions` to Postgres; keep the CSV-merge output in SQLite if you want it portable.
4. **Duplicate submissions.** Workers double-tap Submit or retry on flaky connections. Fix: browser-generated idempotency key per recording; upsert on `(person_id, idempotency_key)`.
5. **Phone-number chaos.** 5 000 workers = five thousand phone formats. Normalisation already happens on the submit path, but I'd also add phone-OTP verification so junk numbers don't pollute the DB.
6. **Audio format hostility.** Android Chrome, iOS Safari, WhatsApp exports — different containers each. Move the ffmpeg transcode into an async worker that re-encodes to a canonical `.opus` for storage + a temporary `.wav` for analysis, then discards the wav.
7. **Cost.** 5 000 × 30 s × 128 kbps ≈ 3 GB of audio. On S3 that's cents; the real cost is egress if we let reviewers play the files back — put audio behind signed CloudFront URLs with short TTLs.

### What I'd change before launch

- Presigned S3 PUT direct from browser; Lambda / worker runs `ffprobe` and writes the row.
- Rate-limit + Cloudflare Turnstile on `/api/submissions`.
- OTP-verify the phone before accepting a submission.
- Health-check dashboard: per-hour submission count, % noisy, upload failure rate.

---

## 9. Stuck log

**Stuck #1 — The entrypoint is locked to `uvicorn server:app`, but the assignment requires Flask.**
I first assumed I'd have to rewrite the whole app to FastAPI to keep uvicorn happy. Instead I searched *"flask asgi wsgitoasgi uvicorn"* and found `asgiref.wsgi.WsgiToAsgi`, a one-line wrapper that lets uvicorn serve a plain Flask WSGI app. So `server.py` ends with `app = WsgiToAsgi(flask_app)` and everything just works.
**Rejected AI suggestion:** *"run Flask on a different port and reverse-proxy from FastAPI"* — pure yak-shave, would have broken the URL routing.

**Stuck #2 — ffprobe returns 0 kbps for browser-recorded WebM/Opus blobs.**
The `MediaRecorder`-produced WebM containers written by Chrome sometimes ship without a `bit_rate` field on the `format` element. Bitrate came back as 0 in the first test recording. I fell back to reading `bit_rate` from the *stream* element, and if that's also missing, `mutagen.File(path).info.bitrate` catches it. Confirmed on both a synthetic sinewave WAV (353 kbps) and a real browser recording.

**Stuck #3 — Merging when *no* single ID is common across all three files.**
Source 1 has email + phone, Source 2 has only email, Source 3 has only name + phone. Naïvely joining pairwise misses transitive matches like `A (email=e1) ↔ B (email=e1, phone=p1) ↔ C (phone=p1)`. I searched *"entity resolution transitive matches union find"* and landed on Union-Find as the simplest correct answer.
**Rejected AI suggestion:** *"do Levenshtein / first-name-last-name similarity matching"* — that would have wrongly merged the two different `Arjun Mehta` people in CBNexus (rows 5 and 28) into one, hiding a real distinction.

**Stuck #4 — n8n couldn't reach my Flask backend even though both were on my machine.**
First curl through the n8n test webhook, the **Call dedup API** node failed with `ECONNREFUSED ::1:8001`. Flask was clearly running (I could hit `http://localhost:8001/api/health` in the browser and got `{"ok":true}`), so I initially assumed a n8n networking bug and started reading n8n docs. The real clue was the `::1` in the error — that's IPv6 localhost. `localhost` on Windows resolves to IPv6 first, but Uvicorn was bound to `0.0.0.0` which only listens on IPv4. So n8n's Node runtime asked for `localhost`, got `::1`, and got refused.
I searched *"n8n ECONNREFUSED ::1 localhost"* and *"uvicorn ipv6 windows"*. Two fixes exist: (a) bind uvicorn to `::` instead of `0.0.0.0` so it listens on both stacks, or (b) change the n8n HTTP node's URL from `http://localhost:8001/...` to `http://127.0.0.1:8001/...`. I picked (b) because the fix stays *inside the workflow JSON* — anyone who imports the JSON gets a working demo without having to re-run uvicorn with a different flag.
**Rejected AI suggestion:** *"disable IPv6 on your Windows adapter"* — nuclear option, would have broken unrelated things and given the reviewer no way to reproduce the fix from just the repo.

---

## 10. API cheat-sheet

```
GET  /api/health
GET  /api/people              ?q=...
GET  /api/people/stats
GET  /api/data-issues
POST /api/submissions         multipart: name, phone, audio
GET  /api/submissions
GET  /api/audio/<filename>
POST /api/dedup/check         {contacts:[{name?,email?,phone?}...]}
```

---

