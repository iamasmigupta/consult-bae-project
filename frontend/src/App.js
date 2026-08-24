import { useEffect, useRef, useState } from "react";
import "./App.css";

const API = `${process.env.REACT_APP_BACKEND_URL}/api`;

function Tabs({ tab, setTab }) {
  const tabs = [
    { k: "record", label: "1. Record audio" },
    { k: "list", label: "2. All submissions" },
    { k: "people", label: "3. Merged people" },
    { k: "issues", label: "4. Data issues" },
  ];
  return (
    <nav className="tabs" data-testid="main-tabs">
      {tabs.map((t) => (
        <button
          key={t.k}
          className={tab === t.k ? "tab active" : "tab"}
          onClick={() => setTab(t.k)}
          data-testid={`tab-${t.k}`}
        >
          {t.label}
        </button>
      ))}
    </nav>
  );
}

function RecordView() {
  const [name, setName] = useState("");
  const [phone, setPhone] = useState("");
  const [mode, setMode] = useState("record");
  const [recording, setRecording] = useState(false);
  const [blob, setBlob] = useState(null);
  const [file, setFile] = useState(null);
  const [status, setStatus] = useState("");
  const [result, setResult] = useState(null);
  const mediaRef = useRef(null);
  const chunksRef = useRef([]);

  async function startRec() {
    setStatus("");
    setResult(null);
    setBlob(null);
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const mr = new MediaRecorder(stream);
      chunksRef.current = [];
      mr.ondataavailable = (e) => chunksRef.current.push(e.data);
      mr.onstop = () => {
        const b = new Blob(chunksRef.current, { type: mr.mimeType || "audio/webm" });
        setBlob(b);
        stream.getTracks().forEach((t) => t.stop());
      };
      mr.start();
      mediaRef.current = mr;
      setRecording(true);
    } catch (err) {
      setStatus("Microphone access denied — switch to upload mode.");
    }
  }

  function stopRec() {
    mediaRef.current?.stop();
    setRecording(false);
  }

  async function submit(e) {
    e.preventDefault();
    if (!name || !phone) {
      setStatus("Enter name and phone first.");
      return;
    }
    const payload = mode === "record" ? blob : file;
    if (!payload) {
      setStatus("Record or upload some audio first.");
      return;
    }
    setStatus("Uploading & analyzing…");
    const fd = new FormData();
    fd.append("name", name);
    fd.append("phone", phone);
    const filename =
      mode === "record"
        ? `rec-${Date.now()}.webm`
        : file.name || "upload.audio";
    fd.append("audio", payload, filename);
    const res = await fetch(`${API}/submissions`, { method: "POST", body: fd });
    if (!res.ok) {
      setStatus("Upload failed.");
      return;
    }
    const data = await res.json();
    setResult(data);
    setStatus("Saved.");
    setBlob(null);
    setFile(null);
  }

  const audioUrl = blob
    ? URL.createObjectURL(blob)
    : file
    ? URL.createObjectURL(file)
    : "";

  return (
    <section className="card" data-testid="record-view">
      <h2>Submit an audio recording</h2>
      <p className="muted">
        We store the file and auto-extract duration, sample rate, bitrate, loudness (dBFS)
        and a rough noise estimate. If your phone matches someone in the merged DB, the
        submission attaches to that person.
      </p>
      <form onSubmit={submit} className="form">
        <label>
          Full name
          <input
            data-testid="name-input"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Priya Singh"
          />
        </label>
        <label>
          Phone number
          <input
            data-testid="phone-input"
            value={phone}
            onChange={(e) => setPhone(e.target.value)}
            placeholder="+91-9000000287"
          />
        </label>

        <div className="mode-switch" data-testid="mode-switch">
          <button
            type="button"
            className={mode === "record" ? "chip active" : "chip"}
            onClick={() => setMode("record")}
            data-testid="mode-record"
          >
            Record in browser
          </button>
          <button
            type="button"
            className={mode === "upload" ? "chip active" : "chip"}
            onClick={() => setMode("upload")}
            data-testid="mode-upload"
          >
            Upload a file
          </button>
        </div>

        {mode === "record" ? (
          <div className="rec-controls">
            {!recording ? (
              <button
                type="button"
                className="btn primary"
                onClick={startRec}
                data-testid="start-recording-btn"
              >
                ● Start recording
              </button>
            ) : (
              <button
                type="button"
                className="btn danger"
                onClick={stopRec}
                data-testid="stop-recording-btn"
              >
                ■ Stop
              </button>
            )}
          </div>
        ) : (
          <input
            type="file"
            accept="audio/*"
            onChange={(e) => setFile(e.target.files?.[0] || null)}
            data-testid="file-input"
          />
        )}

        {audioUrl && (
          <audio controls src={audioUrl} data-testid="preview-audio" />
        )}

        <button type="submit" className="btn primary" data-testid="submit-btn">
          Submit
        </button>
        {status && <div className="status" data-testid="status-msg">{status}</div>}
      </form>

      {result && (
        <div className="result" data-testid="submission-result">
          <h3>Extracted audio properties</h3>
          <dl>
            <dt>Duration</dt><dd>{result.duration_sec} s</dd>
            <dt>Sample rate</dt><dd>{(result.sample_rate_hz / 1000).toFixed(1)} kHz</dd>
            <dt>Bitrate</dt><dd>{result.bitrate_kbps} kbps</dd>
            <dt>Loudness</dt><dd>{result.loudness_db} dBFS</dd>
            <dt>Noise estimate</dt><dd>{result.noise_estimate}</dd>
          </dl>
        </div>
      )}
    </section>
  );
}

function SubmissionsView() {
  const [items, setItems] = useState([]);
  useEffect(() => {
    fetch(`${API}/submissions`).then((r) => r.json()).then(setItems);
  }, []);
  return (
    <section className="card" data-testid="submissions-view">
      <h2>All submissions ({items.length})</h2>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>When</th><th>Name</th><th>Phone</th><th>Duration</th>
              <th>Sample rate</th><th>Bitrate</th><th>Loudness</th>
              <th>Noise</th><th>Play</th>
            </tr>
          </thead>
          <tbody>
            {items.map((s) => (
              <tr key={s.id} data-testid={`sub-row-${s.id}`}>
                <td>{s.created_at?.slice(0, 19).replace("T", " ")}</td>
                <td>{s.name}</td>
                <td>{s.phone}</td>
                <td>{s.duration_sec} s</td>
                <td>{(s.sample_rate_hz / 1000).toFixed(1)} kHz</td>
                <td>{s.bitrate_kbps} kbps</td>
                <td>{s.loudness_db} dB</td>
                <td>
                  <span className={`pill pill-${s.noise_estimate}`}>{s.noise_estimate}</span>
                </td>
                <td>
                  <audio
                    controls
                    src={`${API}/audio/${s.audio_path}`}
                    data-testid={`play-${s.id}`}
                  />
                </td>
              </tr>
            ))}
            {items.length === 0 && (
              <tr><td colSpan={9} className="muted">No submissions yet.</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function PeopleView() {
  const [people, setPeople] = useState([]);
  const [q, setQ] = useState("");
  const [stats, setStats] = useState(null);
  useEffect(() => {
    fetch(`${API}/people/stats`).then((r) => r.json()).then(setStats);
  }, []);
  useEffect(() => {
    const url = q ? `${API}/people?q=${encodeURIComponent(q)}` : `${API}/people`;
    fetch(url).then((r) => r.json()).then(setPeople);
  }, [q]);
  return (
    <section className="card" data-testid="people-view">
      <h2>Merged people ({people.length})</h2>
      {stats && (
        <div className="stats" data-testid="stats-row">
          <span><b>{stats.total_people}</b> unique people</span>
          <span><b>{stats.in_multiple_sources}</b> appear in ≥2 sources</span>
          <span>naukri: <b>{stats.per_source.naukri}</b></span>
          <span>gig: <b>{stats.per_source.gig}</b></span>
          <span>cbnexus: <b>{stats.per_source.cbnexus}</b></span>
        </div>
      )}
      <input
        className="search"
        placeholder="Search name, email, phone, city, skill…"
        value={q}
        onChange={(e) => setQ(e.target.value)}
        data-testid="people-search"
      />
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Name</th><th>Email(s)</th><th>Phone(s)</th><th>City</th>
              <th>Sources</th><th>Skills</th><th>Exp</th><th>CTC (LPA)</th>
            </tr>
          </thead>
          <tbody>
            {people.map((p) => (
              <tr key={p.id} data-testid={`person-row-${p.id}`}>
                <td>{p.full_name}</td>
                <td className="wrap">{p.emails.join(", ")}</td>
                <td className="wrap">{p.phones.join(", ")}</td>
                <td>{p.city}</td>
                <td>
                  {p.sources.map((s) => (
                    <span key={s} className={`pill pill-src-${s}`}>{s}</span>
                  ))}
                </td>
                <td className="wrap">{p.skills.slice(0, 6).join(", ")}</td>
                <td>{p.experience_years || "-"}</td>
                <td>{p.current_ctc_lpa || "-"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function IssuesView() {
  const [items, setItems] = useState([]);
  useEffect(() => {
    fetch(`${API}/data-issues`).then((r) => r.json()).then(setItems);
  }, []);
  const groups = items.reduce((acc, it) => {
    acc[it.issue_type] = (acc[it.issue_type] || []).concat(it);
    return acc;
  }, {});
  return (
    <section className="card" data-testid="issues-view">
      <h2>Data quality issues ({items.length})</h2>
      <p className="muted">
        Detected automatically during the merge pipeline. Each row shows the source
        file, row number, what was wrong, and what the pipeline did about it.
      </p>
      {Object.entries(groups).map(([type, rows]) => (
        <details key={type} open>
          <summary>{type} <span className="count">({rows.length})</span></summary>
          <table>
            <thead>
              <tr><th>Source</th><th>Row</th><th>Description</th><th>Action</th></tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.id} data-testid={`issue-row-${r.id}`}>
                  <td>{r.source}</td>
                  <td>{r.row_num}</td>
                  <td className="wrap">{r.description}</td>
                  <td>{r.action}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </details>
      ))}
    </section>
  );
}

export default function App() {
  const [tab, setTab] = useState("record");
  return (
    <div className="page">
      <header className="hero">
        <div className="logo">CB</div>
        <div>
          <h1>ConsultBae · Merge, Automate, Collect</h1>
          <p className="sub">
            Three messy CSVs → one SQLite of unique people · n8n duplicate-alert flow ·
            in-browser audio capture with automatic quality metrics.
          </p>
        </div>
      </header>
      <Tabs tab={tab} setTab={setTab} />
      <main>
        {tab === "record" && <RecordView />}
        {tab === "list" && <SubmissionsView />}
        {tab === "people" && <PeopleView />}
        {tab === "issues" && <IssuesView />}
      </main>
      <footer className="foot">
        Backend: Flask + SQLite · Audio: ffprobe + soundfile · Deploy target: Render
      </footer>
    </div>
  );
}
