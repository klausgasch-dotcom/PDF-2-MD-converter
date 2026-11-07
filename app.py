"""Web-Oberfläche und HTTP-API für pdf2md.

Start lokal:   uvicorn app:app --host 0.0.0.0 --port 8000
Im Container:  siehe Dockerfile / docker-compose.yml

Konfiguration über Umgebungsvariablen:
  PDF2MD_USER / PDF2MD_PASSWORD   Basic-Auth aktivieren (empfohlen bei öffentlichem Hosting)
  PDF2MD_MAX_UPLOAD_MB            max. Größe pro Datei in MB (Standard 50)
  PDF2MD_MAX_FILES                max. Anzahl Dateien pro Batch (Standard 100)
  PDF2MD_MAX_PARALLEL             gleichzeitige Konvertierungen (Standard 1, OCR ist CPU-lastig)
  PDF2MD_DEFAULT_LANG             Standard-OCR-Sprache (Standard deu+eng)
  PDF2MD_JOB_TTL_MIN              Ergebnisse eines Batches so lange aufbewahren (Standard 60)
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import secrets
import shutil
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, Form, HTTPException, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials

import pdf2md

logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
pdf2md.log.setLevel(logging.INFO)

AUTH_USER = os.environ.get("PDF2MD_USER", "")
AUTH_PASSWORD = os.environ.get("PDF2MD_PASSWORD", "")
MAX_UPLOAD_BYTES = int(os.environ.get("PDF2MD_MAX_UPLOAD_MB", "50")) * 1024 * 1024
MAX_FILES = int(os.environ.get("PDF2MD_MAX_FILES", "100"))
MAX_PARALLEL = int(os.environ.get("PDF2MD_MAX_PARALLEL", "1"))
DEFAULT_LANG = os.environ.get("PDF2MD_DEFAULT_LANG", "deu+eng")
JOB_TTL = int(os.environ.get("PDF2MD_JOB_TTL_MIN", "60")) * 60

LANG_RE = re.compile(r"^[a-z_]{3,10}(\+[a-z_]{3,10})*$")
OCR_MODES = {"auto", "force", "off"}
ERROR_FILE = "_FEHLER.txt"

app = FastAPI(title="pdf2md", docs_url="/api/docs", redoc_url=None)
_basic = HTTPBasic(auto_error=False)
_semaphore: asyncio.Semaphore | None = None


def _get_semaphore() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(MAX_PARALLEL)
    return _semaphore


def require_auth(credentials: HTTPBasicCredentials | None = Depends(_basic)) -> None:
    """Basic-Auth, aber nur wenn PDF2MD_USER und PDF2MD_PASSWORD gesetzt sind."""
    if not (AUTH_USER and AUTH_PASSWORD):
        return
    ok = credentials is not None and (
        secrets.compare_digest(credentials.username.encode(), AUTH_USER.encode())
        & secrets.compare_digest(credentials.password.encode(), AUTH_PASSWORD.encode())
    )
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Anmeldung erforderlich",
            headers={"WWW-Authenticate": 'Basic realm="pdf2md"'},
        )


def _safe_stem(filename: str | None) -> str:
    """Dateiname ohne Pfad und Endung, ohne gefährliche Zeichen."""
    name = Path((filename or "dokument.pdf").replace("\\", "/")).name
    stem = Path(name).stem if name.lower().endswith(".pdf") else name
    stem = re.sub(r'[\x00-\x1f<>:"/\\|?*]', "_", stem).strip(" .")
    return stem or "dokument"


def _content_disposition(filename: str) -> str:
    ascii_name = filename.encode("ascii", "replace").decode().replace("?", "_").replace('"', "_")
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"


def _check_params(n_files: int, lang: str, ocr: str) -> None:
    if not n_files:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Keine Datei übergeben")
    if n_files > MAX_FILES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Maximal {MAX_FILES} Dateien pro Anfrage")
    if not LANG_RE.match(lang):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Ungültige Sprachangabe (z. B. 'deu+eng')")
    if ocr not in OCR_MODES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "ocr muss 'auto', 'force' oder 'off' sein")


# --------------------------------------------------------------------------- Batch-Logik

@dataclass
class Item:
    """Eine Datei innerhalb eines Batches."""

    filename: str           # Originalname des PDFs
    md_name: str            # Name der Markdown-Datei (eindeutig im Batch)
    pdf_path: Path
    status: str = "wartend"  # wartend | läuft | fertig | fehler
    error: str | None = None
    md_path: Path | None = None

    def as_dict(self, index: int) -> dict:
        return {
            "index": index, "filename": self.filename, "md_name": self.md_name,
            "status": self.status, "error": self.error,
        }


@dataclass
class Job:
    id: str
    workdir: Path
    lang: str
    ocr: str
    items: list[Item] = field(default_factory=list)
    created: float = field(default_factory=time.time)
    finished: float | None = None
    task: asyncio.Task | None = None

    @property
    def done(self) -> bool:
        return self.finished is not None

    def as_dict(self) -> dict:
        counts = {s: 0 for s in ("wartend", "läuft", "fertig", "fehler")}
        for it in self.items:
            counts[it.status] += 1
        return {
            "id": self.id, "done": self.done, "total": len(self.items), "counts": counts,
            "files": [it.as_dict(i) for i, it in enumerate(self.items)],
        }


JOBS: dict[str, Job] = {}


async def _store_uploads(files: list[UploadFile], workdir: Path) -> list[Item]:
    """Speichert Uploads; ungültige Dateien werden als Fehler markiert statt den Batch abzubrechen."""
    items: list[Item] = []
    used: set[str] = set()
    for n, upload in enumerate(files):
        stem = _safe_stem(upload.filename)
        md_name, k = f"{stem}.md", 1
        while md_name.lower() in used:  # gleichnamige PDFs im selben Batch
            k += 1
            md_name = f"{stem} ({k}).md"
        used.add(md_name.lower())

        job_dir = workdir / f"{n:04d}"
        job_dir.mkdir()
        pdf_path = job_dir / f"{Path(md_name).stem}.pdf"
        item = Item(filename=upload.filename or f"{stem}.pdf", md_name=md_name, pdf_path=pdf_path)
        items.append(item)

        size = 0
        with pdf_path.open("wb") as fh:
            while chunk := await upload.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    item.status = "fehler"
                    item.error = f"Datei größer als {MAX_UPLOAD_BYTES // (1024 * 1024)} MB"
                    break
                fh.write(chunk)
        if item.status != "fehler":
            with pdf_path.open("rb") as fh:
                if fh.read(5) != b"%PDF-":
                    item.status, item.error = "fehler", "keine PDF-Datei"
        if item.status == "fehler":
            pdf_path.unlink(missing_ok=True)
    return items


async def _process_item(item: Item, lang: str, ocr: str) -> None:
    async with _get_semaphore():
        item.status = "läuft"
        try:
            item.md_path = await run_in_threadpool(
                pdf2md.convert, item.pdf_path, item.pdf_path.parent / "out",
                language=lang, ocr=ocr, overwrite=True,
            )
            item.status = "fertig"
        except Exception as exc:  # noqa: BLE001 - nächste Datei trotzdem verarbeiten
            logging.exception("Konvertierung fehlgeschlagen: %s", item.filename)
            msg = pdf2md.describe_error(exc, item.pdf_path, item.filename)
            item.status, item.error = "fehler", f"Konvertierung fehlgeschlagen: {msg}"
        finally:
            item.pdf_path.unlink(missing_ok=True)


async def _process_all(items: list[Item], lang: str, ocr: str) -> None:
    """Dateien nacheinander (bzw. bis MAX_PARALLEL gleichzeitig) verarbeiten."""
    await asyncio.gather(*(_process_item(it, lang, ocr) for it in items if it.status == "wartend"))


def _zip_results(items: list[Item]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for it in items:
            if it.status == "fertig" and it.md_path:
                zf.write(it.md_path, it.md_name)
        errors = [f"{it.filename}: {it.error}" for it in items if it.status == "fehler"]
        if errors:
            zf.writestr(ERROR_FILE, "Nicht umgewandelte Dateien:\n\n" + "\n".join(errors) + "\n")
    return buf.getvalue()


def _cleanup_jobs() -> None:
    now = time.time()
    for job_id, job in list(JOBS.items()):
        if job.done and now - job.finished > JOB_TTL:
            _delete_job(job_id)


def _delete_job(job_id: str) -> None:
    job = JOBS.pop(job_id, None)
    if job is None:
        return
    if job.task and not job.task.done():
        job.task.cancel()
    shutil.rmtree(job.workdir, ignore_errors=True)


def _get_job(job_id: str) -> Job:
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Batch nicht gefunden oder abgelaufen")
    return job


# --------------------------------------------------------------------------- Routen

@app.get("/healthz", include_in_schema=False)
def healthz() -> dict:
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
def index() -> str:
    return (
        INDEX_HTML.replace("{{DEFAULT_LANG}}", DEFAULT_LANG)
        .replace("{{MAX_MB}}", str(MAX_UPLOAD_BYTES // (1024 * 1024)))
        .replace("{{MAX_FILES}}", str(MAX_FILES))
    )


@app.post("/api/jobs", status_code=202, dependencies=[Depends(require_auth)])
async def create_job(
    files: list[UploadFile],
    lang: str = Form(DEFAULT_LANG),
    ocr: str = Form("auto"),
) -> dict:
    """Batch anlegen: PDFs hochladen, Verarbeitung läuft im Hintergrund.

    Status über ``GET /api/jobs/{id}``, Ergebnis über ``GET /api/jobs/{id}/download`` (ZIP)
    oder ``GET /api/jobs/{id}/files/{index}`` (einzelne .md).
    """
    _check_params(len(files), lang, ocr)
    _cleanup_jobs()
    job = Job(id=secrets.token_urlsafe(16), workdir=Path(tempfile.mkdtemp(prefix="pdf2md-job-")),
              lang=lang, ocr=ocr)
    job.items = await _store_uploads(files, job.workdir)
    JOBS[job.id] = job

    async def runner() -> None:
        try:
            await _process_all(job.items, lang, ocr)
        finally:
            for it in job.items:  # z. B. bei Abbruch/Neustart
                if it.status in ("wartend", "läuft"):
                    it.status, it.error = "fehler", "Verarbeitung abgebrochen"
            job.finished = time.time()

    job.task = asyncio.create_task(runner())
    return job.as_dict()


@app.get("/api/jobs/{job_id}", dependencies=[Depends(require_auth)])
def job_status(job_id: str) -> dict:
    return _get_job(job_id).as_dict()


@app.get("/api/jobs/{job_id}/download", dependencies=[Depends(require_auth)])
def job_download(job_id: str) -> Response:
    """Alle fertigen .md-Dateien als ZIP (plus _FEHLER.txt, falls Dateien fehlschlugen)."""
    job = _get_job(job_id)
    if not job.done:
        raise HTTPException(status.HTTP_409_CONFLICT, "Batch läuft noch")
    return Response(
        _zip_results(job.items), media_type="application/zip",
        headers={"Content-Disposition": _content_disposition("markdown.zip")},
    )


@app.get("/api/jobs/{job_id}/files/{index}", dependencies=[Depends(require_auth)])
def job_file(job_id: str, index: int) -> Response:
    job = _get_job(job_id)
    if not 0 <= index < len(job.items):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Datei nicht gefunden")
    item = job.items[index]
    if item.status != "fertig" or not item.md_path:
        raise HTTPException(status.HTTP_409_CONFLICT, item.error or f"Status: {item.status}")
    return Response(
        item.md_path.read_bytes(), media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": _content_disposition(item.md_name)},
    )


@app.delete("/api/jobs/{job_id}", status_code=204, dependencies=[Depends(require_auth)])
def job_delete(job_id: str) -> Response:
    _get_job(job_id)
    _delete_job(job_id)
    return Response(status_code=204)


@app.post("/api/convert", dependencies=[Depends(require_auth)])
async def convert_endpoint(
    files: list[UploadFile],
    lang: str = Form(DEFAULT_LANG),
    ocr: str = Form("auto"),
) -> Response:
    """Synchron umwandeln (für Skripte). Eine Datei -> .md, mehrere -> .zip.

    Bei mehreren Dateien werden fehlerhafte übersprungen und in ``_FEHLER.txt`` im ZIP
    aufgeführt. Für große Batches besser ``/api/jobs`` verwenden (keine Timeouts).
    """
    _check_params(len(files), lang, ocr)
    with tempfile.TemporaryDirectory(prefix="pdf2md-web-") as tmp:
        items = await _store_uploads(files, Path(tmp))
        await _process_all(items, lang, ocr)

        if len(items) == 1:
            item = items[0]
            if item.status != "fertig":
                code = (status.HTTP_415_UNSUPPORTED_MEDIA_TYPE if item.error == "keine PDF-Datei"
                        else status.HTTP_413_REQUEST_ENTITY_TOO_LARGE if "größer" in (item.error or "")
                        else status.HTTP_422_UNPROCESSABLE_ENTITY)
                raise HTTPException(code, f"{item.filename}: {item.error}")
            return Response(
                item.md_path.read_bytes(), media_type="text/markdown; charset=utf-8",
                headers={"Content-Disposition": _content_disposition(item.md_name)},
            )

        if not any(it.status == "fertig" for it in items):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "; ".join(f"{it.filename}: {it.error}" for it in items),
            )
        return Response(
            _zip_results(items), media_type="application/zip",
            headers={"Content-Disposition": _content_disposition("markdown.zip")},
        )


INDEX_HTML = r"""<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PDF zu Markdown</title>
<style>
  :root { --bg:#f6f7f9; --card:#fff; --fg:#1d2330; --muted:#6b7280; --accent:#2563eb; --border:#d6dae1; --err:#b91c1c; --ok:#15803d; --run:#b45309; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#111418; --card:#1a1f26; --fg:#e6e8eb; --muted:#9aa3ae; --accent:#60a5fa; --border:#2e3540; --err:#f87171; --ok:#4ade80; --run:#fbbf24; }
  }
  * { box-sizing: border-box; }
  body { margin:0; font:16px/1.5 system-ui, -apple-system, Segoe UI, Roboto, sans-serif; background:var(--bg); color:var(--fg); }
  main { max-width:720px; margin:0 auto; padding:32px 16px; }
  h1 { font-size:1.6rem; margin:0 0 4px; }
  p.sub { color:var(--muted); margin:0 0 24px; }
  .card { background:var(--card); border:1px solid var(--border); border-radius:12px; padding:20px; }
  #drop { border:2px dashed var(--border); border-radius:10px; padding:32px 16px; text-align:center; cursor:pointer; transition:.15s; }
  #drop.over { border-color:var(--accent); background:color-mix(in srgb, var(--accent) 8%, transparent); }
  #drop strong { color:var(--accent); }
  #summary { margin:12px 0 0; font-size:.92rem; color:var(--muted); display:flex; justify-content:space-between; gap:8px; flex-wrap:wrap; }
  #summary button { all:unset; color:var(--accent); cursor:pointer; }
  #list { list-style:none; padding:0; margin:8px 0 0; font-size:.92rem; max-height:360px; overflow:auto; }
  #list li { display:grid; grid-template-columns:1fr auto; gap:2px 12px; padding:6px 0; border-bottom:1px solid var(--border); }
  #list .name { overflow-wrap:anywhere; }
  #list .st { white-space:nowrap; color:var(--muted); }
  #list .st.fertig { color:var(--ok); } #list .st.fehler { color:var(--err); } #list .st.läuft { color:var(--run); }
  #list .msg { grid-column:1 / -1; color:var(--err); font-size:.85rem; }
  #list a { color:var(--accent); }
  .row { display:flex; gap:12px; flex-wrap:wrap; margin-top:16px; }
  .row label { flex:1 1 180px; font-size:.9rem; color:var(--muted); }
  select { width:100%; margin-top:4px; padding:8px; border:1px solid var(--border); border-radius:8px; background:var(--bg); color:var(--fg); font:inherit; }
  .btn { display:block; margin-top:20px; width:100%; padding:12px; border:0; border-radius:8px; background:var(--accent); color:#fff; font:inherit; font-weight:600; cursor:pointer; text-align:center; text-decoration:none; }
  .btn:disabled { opacity:.5; cursor:default; }
  .btn.secondary { background:transparent; color:var(--accent); border:1px solid var(--accent); margin-top:10px; }
  progress { width:100%; height:8px; margin-top:16px; accent-color:var(--accent); }
  #status { margin-top:8px; min-height:1.5em; }
  .err { color:var(--err); } .ok { color:var(--ok); }
  [hidden] { display:none !important; }
  footer { margin-top:16px; color:var(--muted); font-size:.85rem; }
</style>
</head>
<body>
<main>
  <h1>PDF zu Markdown</h1>
  <p class="sub">Eine oder viele PDFs hochladen und als gleichnamige .md-Dateien herunterladen. Eingescannte Seiten werden automatisch per OCR erkannt.</p>
  <form class="card" id="form">
    <div id="drop" tabindex="0">
      <strong>PDF-Dateien auswählen</strong> oder hierher ziehen (auch ganze Ordner)<br>
      <small>bis zu {{MAX_FILES}} Dateien, max. {{MAX_MB}} MB pro Datei</small>
      <input id="file" type="file" accept="application/pdf,.pdf" multiple hidden>
    </div>
    <div id="summary" hidden><span id="sumtext"></span><button type="button" id="clear">Liste leeren</button></div>
    <ul id="list"></ul>
    <div class="row">
      <label>OCR-Sprache
        <select id="lang">
          <option value="deu+eng">Deutsch + Englisch</option>
          <option value="deu">Deutsch</option>
          <option value="eng">Englisch</option>
        </select>
      </label>
      <label>OCR
        <select id="ocr">
          <option value="auto">Automatisch (nur Scans)</option>
          <option value="force">Immer (alle Seiten)</option>
          <option value="off">Nie</option>
        </select>
      </label>
    </div>
    <button class="btn" id="go" type="submit" disabled>Umwandeln</button>
    <progress id="bar" max="100" value="0" hidden></progress>
    <div id="status" role="status" aria-live="polite"></div>
    <a class="btn secondary" id="zip" hidden>Alle als ZIP herunterladen</a>
  </form>
  <footer>Ergebnisse werden nach Ablauf automatisch gelöscht. API: <a href="api/docs">Doku</a></footer>
</main>
<script>
const $ = (id) => document.getElementById(id);
const drop = $("drop"), input = $("file"), list = $("list"), go = $("go"), statusEl = $("status"),
      bar = $("bar"), zip = $("zip"), summary = $("summary"), sumtext = $("sumtext");
const MAX_FILES = {{MAX_FILES}}, MAX_BYTES = {{MAX_MB}} * 1048576;
const LABEL = { "wartend": "wartet", "läuft": "wird umgewandelt …", "fertig": "fertig", "fehler": "Fehler" };
let files = [], busy = false;

const langSel = $("lang");
if (![...langSel.options].some(o => o.value === "{{DEFAULT_LANG}}")) langSel.add(new Option("{{DEFAULT_LANG}}", "{{DEFAULT_LANG}}"), 0);
langSel.value = "{{DEFAULT_LANG}}";

const isPdf = (f) => f.name.toLowerCase().endsWith(".pdf") || f.type === "application/pdf";
const mb = (n) => (n / 1048576).toFixed(1) + " MB";

function addFiles(fl) {
  if (busy) return;
  const known = new Set(files.map(f => f.name + "|" + f.size + "|" + f.lastModified));
  for (const f of fl) {
    const key = f.name + "|" + f.size + "|" + f.lastModified;
    if (isPdf(f) && !known.has(key)) { files.push(f); known.add(key); }
  }
  renderPending();
}

function renderPending() {
  list.innerHTML = "";
  zip.hidden = true; bar.hidden = true;
  for (const f of files) {
    const li = row(f.name, f.size > MAX_BYTES ? "zu groß" : mb(f.size), f.size > MAX_BYTES ? "fehler" : "");
    list.appendChild(li);
  }
  const total = files.reduce((s, f) => s + f.size, 0);
  summary.hidden = files.length === 0;
  sumtext.textContent = files.length + " Datei(en), " + mb(total);
  let msg = "";
  if (files.length > MAX_FILES) msg = "Maximal " + MAX_FILES + " Dateien pro Durchgang.";
  statusEl.className = msg ? "err" : ""; statusEl.textContent = msg;
  go.disabled = files.length === 0 || files.length > MAX_FILES;
}

function row(name, stText, stClass, href, msg) {
  const li = document.createElement("li");
  const n = document.createElement("span"); n.className = "name";
  if (href) { const a = document.createElement("a"); a.href = href; a.textContent = name; n.appendChild(a); }
  else n.textContent = name;
  const st = document.createElement("span"); st.className = "st " + (stClass || ""); st.textContent = stText;
  li.append(n, st);
  if (msg) { const m = document.createElement("span"); m.className = "msg"; m.textContent = msg; li.appendChild(m); }
  return li;
}

// Ordner per Drag & Drop rekursiv einlesen
async function entriesToFiles(items) {
  const out = [];
  const walk = async (entry) => {
    if (!entry) return;
    if (entry.isFile) out.push(await new Promise((res, rej) => entry.file(res, rej)));
    else if (entry.isDirectory) {
      const reader = entry.createReader();
      let batch;
      do {
        batch = await new Promise((res, rej) => reader.readEntries(res, rej));
        for (const e of batch) await walk(e);
      } while (batch.length);
    }
  };
  await Promise.all([...items].map(it => walk(it.webkitGetAsEntry && it.webkitGetAsEntry())));
  return out;
}

drop.addEventListener("click", () => input.click());
drop.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); input.click(); } });
input.addEventListener("change", () => { addFiles(input.files); input.value = ""; });
["dragenter", "dragover"].forEach(ev => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
["dragleave", "drop"].forEach(ev => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
drop.addEventListener("drop", async (e) => {
  const items = e.dataTransfer.items;
  if (items && items.length && items[0].webkitGetAsEntry) addFiles(await entriesToFiles(items));
  else addFiles(e.dataTransfer.files);
});
$("clear").addEventListener("click", () => { if (!busy) { files = []; renderPending(); } });

function upload(fd) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "api/jobs");
    xhr.responseType = "json";
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) {
        bar.value = Math.round(e.loaded / e.total * 100);
        statusEl.textContent = "Hochladen … " + bar.value + " %";
      }
    };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) resolve(xhr.response);
      else reject(new Error((xhr.response && xhr.response.detail) || (xhr.status + " " + xhr.statusText)));
    };
    xhr.onerror = () => reject(new Error("Verbindung fehlgeschlagen"));
    xhr.send(fd);
  });
}

function renderJob(job) {
  list.innerHTML = "";
  for (const f of job.files) {
    const href = f.status === "fertig" ? "api/jobs/" + job.id + "/files/" + f.index : null;
    list.appendChild(row(f.status === "fertig" ? f.md_name : f.filename, LABEL[f.status] || f.status, f.status, href, f.error));
  }
  const c = job.counts, finished = c.fertig + c.fehler;
  bar.value = Math.round(finished / job.total * 100);
  statusEl.className = "";
  statusEl.textContent = job.done
    ? "Fertig: " + c.fertig + " von " + job.total + " umgewandelt" + (c.fehler ? ", " + c.fehler + " mit Fehler" : "")
    : "Umwandeln … " + finished + " / " + job.total + " (OCR kann bei Scans etwas dauern)";
  if (job.done && c.fehler) statusEl.className = c.fertig ? "" : "err";
}

function download(href) {
  const a = document.createElement("a"); a.href = href; a.download = "";
  document.body.appendChild(a); a.click(); a.remove();
}

$("form").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (!files.length || busy) return;
  busy = true; go.disabled = true; zip.hidden = true; bar.hidden = false; bar.value = 0;
  summary.hidden = true;
  const fd = new FormData();
  files.forEach(f => fd.append("files", f, f.name));
  fd.append("lang", langSel.value);
  fd.append("ocr", $("ocr").value);
  try {
    let job = await upload(fd);
    renderJob(job);
    while (!job.done) {
      await new Promise(r => setTimeout(r, 1000));
      const res = await fetch("api/jobs/" + job.id);
      if (!res.ok) throw new Error("Status nicht abrufbar (" + res.status + ")");
      job = await res.json();
      renderJob(job);
    }
    const ok = job.files.filter(f => f.status === "fertig");
    if (job.total > 1 && ok.length) {
      zip.href = "api/jobs/" + job.id + "/download"; zip.hidden = false;
      download(zip.href);
    } else if (ok.length === 1) {
      download("api/jobs/" + job.id + "/files/" + ok[0].index);
    }
    files = [];
  } catch (err) {
    statusEl.className = "err";
    statusEl.textContent = "Fehler: " + err.message;
  } finally {
    busy = false;
    go.disabled = files.length === 0;
  }
});
</script>
</body>
</html>
"""
