"""
ConsultBae – Data Merge Pipeline (Task 1)

Ingests three CSVs (Naukri applicants, Gig workers, CBNexus contacts) into a
single SQLite database. Same person appearing across files becomes ONE record
using a union-find on normalized email + normalized phone.

Also produces a data_issues report (Task 4).

Run:  python scripts/merge_data.py
"""

import csv
import json
import os
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DB_PATH = ROOT / "backend" / "consultbae.db"

SOURCE1 = DATA_DIR / "source1.csv"  # Naukri applicants
SOURCE2 = DATA_DIR / "source2.csv"  # Gig workers
SOURCE3 = DATA_DIR / "source3.csv"  # CBNexus contacts


# ----------------------------- Normalizers ---------------------------------

def norm_email(email: str) -> str:
    if not email:
        return ""
    return email.strip().lower()


def norm_phone(phone: str) -> str:
    """Return the canonical last-10-digit Indian mobile number, or ''."""
    if not phone:
        return ""
    digits = re.sub(r"\D", "", phone)
    # strip country code 91 or leading 0
    if len(digits) > 10 and digits.startswith("91"):
        digits = digits[-10:]
    if len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) != 10:
        return ""
    return digits


def norm_city(city: str) -> str:
    if not city:
        return ""
    c = city.strip().lower()
    # canonicalize the NCR cluster loosely
    mapping = {
        "bangalore": "Bengaluru",
        "bengaluru": "Bengaluru",
        "gurgaon": "Gurugram",
        "gurugram": "Gurugram",
        "new delhi": "Delhi",
        "delhi ncr": "Delhi",
        "delhi": "Delhi",
        "noida": "Noida",
        "pune": "Pune",
    }
    return mapping.get(c, city.strip().title())


def title_name(name: str) -> str:
    if not name:
        return ""
    n = " ".join(name.strip().split())
    return n.title()


def parse_date(raw: str) -> str:
    """Try many formats, return ISO YYYY-MM-DD or empty."""
    if not raw:
        return ""
    raw = raw.strip()
    fmts = [
        "%Y-%m-%d", "%d-%m-%Y", "%m/%d/%Y", "%d/%m/%Y",
        "%d %b %Y", "%d %B %Y",
    ]
    for f in fmts:
        try:
            return datetime.strptime(raw, f).date().isoformat()
        except ValueError:
            continue
    return ""  # unparseable


def parse_ctc(raw) -> float:
    """Return CTC in LPA (lakhs per annum).

    Source1 mixes raw rupees (e.g. 417964) and LPA (e.g. 4.2). Heuristic:
    values >= 10000 are rupees; values < 100 are already LPA.
    """
    if raw is None or raw == "":
        return 0.0
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return 0.0
    if v >= 10000:
        return round(v / 100000.0, 2)
    return round(v, 2)


def parse_rate_inr_hr(raw: str) -> float:
    """Source2 rate is '1415/hr' or '15k/month'. Return normalized INR/hour."""
    if not raw:
        return 0.0
    raw = raw.strip().lower()
    m = re.match(r"([\d.]+)\s*k?/(hr|month)", raw)
    if not m:
        return 0.0
    val = float(m.group(1))
    if "k" in raw:
        val *= 1000
    if m.group(2) == "month":
        # assume ~22 working days * 8 hours
        val = val / (22 * 8)
    return round(val, 2)


def parse_verified(raw: str) -> int:
    if not raw:
        return 0
    return 1 if raw.strip().lower() in {"y", "yes", "true", "1"} else 0


def parse_status(raw: str) -> str:
    return (raw or "").strip().lower()


def parse_skills(raw: str) -> list:
    if not raw:
        return []
    return [s.strip().lower() for s in raw.split(",") if s.strip()]


# ----------------------------- Union-find -----------------------------------

class UF:
    def __init__(self):
        self.p = {}

    def find(self, x):
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra


# ----------------------------- Load rows -----------------------------------

def load_source1():
    rows, issues = [], []
    with open(SOURCE1, newline="", encoding="utf-8") as f:
        for i, r in enumerate(csv.DictReader(f), start=2):
            if not any(r.values()):
                issues.append(("source1", i, "empty_row",
                    "Row is entirely blank (all columns empty)",
                    "Skipped — no data to ingest"))
                continue
            email = norm_email(r.get("Email", ""))
            phone = norm_phone(r.get("Phone", ""))
            if not email and not phone:
                issues.append(("source1", i, "no_identifier",
                    f"Row has neither email nor phone — cannot match to any other record. Raw row: {r}",
                    "Skipped — cannot be deduplicated without an identifier"))
                continue
            # detect alt email prefix
            if email.startswith("alt."):
                issues.append(("source1", i, "alt_email_prefix",
                    f'Email "{email}" starts with "alt." — likely an alternate address for an existing person',
                    "Kept as a separate email; person is merged via shared phone number if present"))
            date_iso = parse_date(r.get("Applied Date", ""))
            if not date_iso and r.get("Applied Date"):
                issues.append(("source1", i, "bad_date",
                    f'Applied Date "{r["Applied Date"]}" does not match any of 6 expected formats '
                    f'(YYYY-MM-DD, DD-MM-YYYY, MM/DD/YYYY, DD/MM/YYYY, DD Mon YYYY, DD Month YYYY)',
                    "Stored as empty; other row data preserved"))
            ctc_raw = r.get("Current CTC", "")
            ctc_lpa = parse_ctc(ctc_raw)
            try:
                if float(ctc_raw) >= 10000:
                    issues.append(("source1", i, "ctc_in_rupees",
                        f'CTC value "{ctc_raw}" was stored in raw rupees instead of Lakhs Per Annum (LPA). '
                        f'Other rows in the same file use LPA (e.g. "4.2"), so units are mixed within one column.',
                        f"Divided by 100,000 → stored as {ctc_lpa} LPA"))
            except (TypeError, ValueError):
                pass
            rows.append({
                "src": "naukri",
                "name": title_name(r.get("Full Name", "")),
                "email": email,
                "phone": phone,
                "city": norm_city(r.get("City", "")),
                "experience_years": float(r.get("Experience (Years)") or 0),
                "current_ctc_lpa": ctc_lpa,
                "applied_date": date_iso,
                "skills": parse_skills(r.get("Skills", "")),
            })
    return rows, issues


def load_source2():
    rows, issues = [], []
    with open(SOURCE2, newline="", encoding="utf-8") as f:
        for i, r in enumerate(csv.DictReader(f), start=2):
            if not any((v or "").strip() for v in r.values()):
                issues.append(("source2", i, "empty_row",
                    "Row is entirely blank — likely an accidental delimiter-only line in the CSV export",
                    "Skipped — no data to ingest"))
                continue
            # Row 20 in source2 has columns misaligned (skills in email column).
            raw_email = r.get("email_id", "")
            if raw_email and "@" not in raw_email:
                issues.append(("source2", i, "column_shift",
                    f'Value in email_id column is not an email — looks like skills leaked into the wrong column: "{raw_email}". '
                    f"Whole row is misaligned and cannot be trusted.",
                    "Row rejected outright — cannot safely realign columns"))
                continue
            email = norm_email(raw_email)
            rows.append({
                "src": "gig",
                "name": title_name(r.get("worker_name", "")),
                "email": email,
                "phone": "",
                "city": norm_city(r.get("location", "")),
                "rate_inr_hr": parse_rate_inr_hr(r.get("rate", "")),
                "worker_status": parse_status(r.get("status", "")),
                "skills": parse_skills(r.get("skill_tags", "")),
            })
    return rows, issues


def load_source3():
    rows, issues = [], []
    with open(SOURCE3, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader)
        for i, r in enumerate(reader, start=2):
            if not any(c.strip() for c in r):
                issues.append(("source3", i, "empty_row",
                    "Row is entirely blank",
                    "Skipped — no data to ingest"))
                continue
            if r == header:
                issues.append(("source3", i, "duplicate_header",
                    f"The header row ({','.join(header)}) appears again inside the data section — "
                    f"suggests the file was concatenated from two exports without stripping the second header.",
                    "Skipped — treated as a stray header, not a data row"))
                continue
            name, phone, city, verified, projects = r
            rows.append({
                "src": "cbnexus",
                "name": title_name(name),
                "email": "",
                "phone": norm_phone(phone),
                "city": norm_city(city),
                "verified": parse_verified(verified),
                "projects_completed": int(projects) if projects.strip().isdigit() else 0,
            })
    return rows, issues


# ----------------------------- Merge ----------------------------------------

def merge():
    s1, i1 = load_source1()
    s2, i2 = load_source2()
    s3, i3 = load_source3()
    all_rows = s1 + s2 + s3
    issues = i1 + i2 + i3

    # Build union-find over emails and phones
    uf = UF()
    row_keys = []
    for idx, row in enumerate(all_rows):
        keys = []
        if row.get("email"):
            keys.append(("e", row["email"]))
        if row.get("phone"):
            keys.append(("p", row["phone"]))
        row_keys.append(keys)
        for k in keys:
            uf.find(k)
        if len(keys) >= 2:
            for k in keys[1:]:
                uf.union(keys[0], k)

    # Bucket rows by their component root
    groups = {}
    orphan_counter = 0
    for idx, keys in enumerate(row_keys):
        if keys:
            root = uf.find(keys[0])
        else:
            root = ("orphan", orphan_counter)
            orphan_counter += 1
        groups.setdefault(root, []).append(idx)

    # Also: intra-source1 exact duplicate detection (Rohit Verma appears twice
    # with different name capitalisation)
    seen = {}
    for idx, row in enumerate(all_rows):
        key = (row.get("email"), row.get("phone"), row.get("name", "").lower())
        if key in seen and row.get("email"):
            issues.append((row["src"], idx, "duplicate_row",
                f'Same email + phone + name combination appeared earlier in the same source file (email: {row.get("email")}). '
                f"Likely a data-entry duplicate rather than a distinct person.",
                "Merged with the earlier occurrence — kept as one person record"))
        seen[key] = idx

    people = []
    for root, idxs in groups.items():
        rows = [all_rows[i] for i in idxs]
        srcs = sorted({r["src"] for r in rows})
        # aggregate
        names = [r["name"] for r in rows if r.get("name")]
        emails = sorted({r["email"] for r in rows if r.get("email")})
        phones = sorted({r["phone"] for r in rows if r.get("phone")})
        cities = [r["city"] for r in rows if r.get("city")]
        skills = sorted({s for r in rows for s in r.get("skills", [])})

        p = {
            "id": str(uuid.uuid4()),
            "full_name": max(set(names), key=names.count) if names else "",
            "aliases": json.dumps(sorted(set(n for n in names))),
            "primary_email": emails[0] if emails else "",
            "emails": json.dumps(emails),
            "primary_phone": phones[0] if phones else "",
            "phones": json.dumps(phones),
            "city": max(set(cities), key=cities.count) if cities else "",
            "skills": json.dumps(skills),
            "sources": json.dumps(srcs),
            "experience_years": next((r.get("experience_years") for r in rows if r.get("experience_years")), 0),
            "current_ctc_lpa": next((r.get("current_ctc_lpa") for r in rows if r.get("current_ctc_lpa")), 0),
            "applied_date": next((r.get("applied_date") for r in rows if r.get("applied_date")), ""),
            "rate_inr_hr": next((r.get("rate_inr_hr") for r in rows if r.get("rate_inr_hr")), 0),
            "worker_status": next((r.get("worker_status") for r in rows if r.get("worker_status")), ""),
            "verified": next((r.get("verified") for r in rows if r.get("verified") is not None), 0),
            "projects_completed": next((r.get("projects_completed") for r in rows if r.get("projects_completed")), 0),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        people.append(p)
    return people, issues


# ----------------------------- Write DB ------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS people (
    id TEXT PRIMARY KEY,
    full_name TEXT,
    aliases TEXT,
    primary_email TEXT,
    emails TEXT,
    primary_phone TEXT,
    phones TEXT,
    city TEXT,
    skills TEXT,
    sources TEXT,
    experience_years REAL,
    current_ctc_lpa REAL,
    applied_date TEXT,
    rate_inr_hr REAL,
    worker_status TEXT,
    verified INTEGER,
    projects_completed INTEGER,
    skill_category TEXT,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_email  ON people(primary_email);
CREATE INDEX IF NOT EXISTS idx_phone  ON people(primary_phone);

CREATE TABLE IF NOT EXISTS submissions (
    id TEXT PRIMARY KEY,
    person_id TEXT,
    name TEXT,
    phone TEXT,
    audio_path TEXT,
    mime TEXT,
    duration_sec REAL,
    sample_rate_hz INTEGER,
    bitrate_kbps INTEGER,
    loudness_db REAL,
    noise_estimate TEXT,
    created_at TEXT,
    FOREIGN KEY (person_id) REFERENCES people(id)
);

CREATE TABLE IF NOT EXISTS data_issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT,
    row_num INTEGER,
    issue_type TEXT,
    description TEXT,
    action TEXT
);
"""


def write_db(people, issues):
    if DB_PATH.exists():
        DB_PATH.unlink()
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    conn.executemany(
        """INSERT INTO people (id, full_name, aliases, primary_email, emails,
              primary_phone, phones, city, skills, sources, experience_years,
              current_ctc_lpa, applied_date, rate_inr_hr, worker_status,
              verified, projects_completed, created_at)
           VALUES (:id, :full_name, :aliases, :primary_email, :emails,
              :primary_phone, :phones, :city, :skills, :sources, :experience_years,
              :current_ctc_lpa, :applied_date, :rate_inr_hr, :worker_status,
              :verified, :projects_completed, :created_at)""",
        people,
    )
    conn.executemany(
        "INSERT INTO data_issues (source, row_num, issue_type, description, action) VALUES (?,?,?,?,?)",
        issues,
    )
    conn.commit()
    conn.close()


def main():
    people, issues = merge()
    write_db(people, issues)
    print(f"Merged {len(people)} unique people into {DB_PATH}")
    print(f"Logged {len(issues)} data issues")
    # print per-source counts
    src_counts = {}
    for p in people:
        for s in json.loads(p["sources"]):
            src_counts[s] = src_counts.get(s, 0) + 1
    print("People coverage per source:", src_counts)


if __name__ == "__main__":
    main()
