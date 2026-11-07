"""Web-Oberfläche und HTTP-API für pdf2md.

Start lokal:   uvicorn app:app --host 0.0.0.0 --port 8000
Im Container:  siehe Dockerfile / docker-compose.yml

Konfiguration über Umgebungsvariablen:
  PDF2MD_USER / PDF2MD_PASSWORD   Basic-Auth aktivieren (empfohlen bei öffentlichem Hosting)
  PDF2MD_MAX_UPLOAD_MB            max. Größe pro Datei in MB (Standard 50)
  PDF2MD_MAX_FILES                max. Anzahl Dateien pro Anfrage (Standard 20)
  PDF2MD_MAX_PARALLEL             gleichzeitige Konvertierungen (Standard 1, OCR ist CPU-lastig)
  PDF2MD_DEFAULT_LANG             Standard-OCR-Sprache (Standard deu+eng)
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import secrets
import tempfile
import zipfile
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
MAX_FILES = int(os.environ.get("PDF2MD_MAX_FILES", "20"))
MAX_PARALLEL = int(os.environ.get("PDF2MD_MAX_PARALLEL", "1"))
DEFAULT_LANG = os.environ.get("PDF2MD_DEFAULT_LANG", "deu+eng")

LANG_RE = re.compile(r"^[a-z_]{3,10}(\+[a-z_]{3,10})*$")
OCR_MODES = {"auto", "force", "off"}

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


async def _save_upload(upload: UploadFile, dst: Path) -> None:
    size = 0
    with dst.open("wb") as fh:
        while chunk := await upload.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES:
                raise HTTPException(
                    status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    f"{upload.filename}: Datei größer als {MAX_UPLOAD_BYTES // (1024 * 1024)} MB",
                )
            fh.write(chunk)
    with dst.open("rb") as fh:
        if fh.read(5) != b"%PDF-":
            raise HTTPException(
                status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, f"{upload.filename}: keine PDF-Datei"
            )


async def _convert_one(upload: UploadFile, workdir: Path, lang: str, ocr: str) -> tuple[str, bytes]:
    stem = _safe_stem(upload.filename)
    job_dir = Path(tempfile.mkdtemp(dir=workdir))
    pdf_path = job_dir / f"{stem}.pdf"
    await _save_upload(upload, pdf_path)
    async with _get_semaphore():
        try:
            md_path = await run_in_threadpool(
                pdf2md.convert, pdf_path, job_dir / "out", language=lang, ocr=ocr, overwrite=True
            )
        except Exception as exc:  # noqa: BLE001
            logging.exception("Konvertierung fehlgeschlagen: %s", upload.filename)
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"{upload.filename}: Konvertierung fehlgeschlagen ({type(exc).__name__}: {exc})",
            ) from exc
    return md_path.name, md_path.read_bytes()


@app.get("/healthz", include_in_schema=False)
def healthz() -> dict:
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
def index() -> str:
    return INDEX_HTML.replace("{{DEFAULT_LANG}}", DEFAULT_LANG).replace(
        "{{MAX_MB}}", str(MAX_UPLOAD_BYTES // (1024 * 1024))
    )


@app.post("/api/convert", dependencies=[Depends(require_auth)])
async def convert_endpoint(
    files: list[UploadFile],
    lang: str = Form(DEFAULT_LANG),
    ocr: str = Form("auto"),
) -> Response:
    """PDF(s) hochladen. Eine Datei -> .md, mehrere Dateien -> .zip mit gleichnamigen .md-Dateien."""
    if not files:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Keine Datei übergeben")
    if len(files) > MAX_FILES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Maximal {MAX_FILES} Dateien pro Anfrage")
    if not LANG_RE.match(lang):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Ungültige Sprachangabe (z. B. 'deu+eng')")
    if ocr not in OCR_MODES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "ocr muss 'auto', 'force' oder 'off' sein")

    with tempfile.TemporaryDirectory(prefix="pdf2md-web-") as tmp:
        results = [await _convert_one(f, Path(tmp), lang, ocr) for f in files]

    if len(results) == 1:
        name, data = results[0]
        return Response(
            data,
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": _content_disposition(name)},
        )

    buf = io.BytesIO()
    used: set[str] = set()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in results:
            unique, n = name, 1
            while unique in used:  # gleiche Namen aus verschiedenen Ordnern
                n += 1
                unique = f"{Path(name).stem} ({n}).md"
            used.add(unique)
            zf.writestr(unique, data)
    return Response(
        buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": _content_disposition("markdown.zip")},
    )


INDEX_HTML = """<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PDF zu Markdown</title>
<style>
  :root { --bg:#f6f7f9; --card:#fff; --fg:#1d2330; --muted:#6b7280; --accent:#2563eb; --border:#d6dae1; --err:#b91c1c; --ok:#15803d; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#111418; --card:#1a1f26; --fg:#e6e8eb; --muted:#9aa3ae; --accent:#60a5fa; --border:#2e3540; --err:#f87171; --ok:#4ade80; }
  }
  * { box-sizing: border-box; }
  body { margin:0; font:16px/1.5 system-ui, -apple-system, Segoe UI, Roboto, sans-serif; background:var(--bg); color:var(--fg); }
  main { max-width:640px; margin:0 auto; padding:32px 16px; }
  h1 { font-size:1.6rem; margin:0 0 4px; }
  p.sub { color:var(--muted); margin:0 0 24px; }
  .card { background:var(--card); border:1px solid var(--border); border-radius:12px; padding:20px; }
  #drop { border:2px dashed var(--border); border-radius:10px; padding:32px 16px; text-align:center; cursor:pointer; transition:.15s; }
  #drop.over { border-color:var(--accent); background:color-mix(in srgb, var(--accent) 8%, transparent); }
  #drop strong { color:var(--accent); }
  #list { list-style:none; padding:0; margin:12px 0 0; font-size:.92rem; }
  #list li { display:flex; justify-content:space-between; gap:8px; padding:4px 0; border-bottom:1px solid var(--border); overflow-wrap:anywhere; }
  .row { display:flex; gap:12px; flex-wrap:wrap; margin-top:16px; }
  .row label { flex:1 1 180px; font-size:.9rem; color:var(--muted); }
  select, input[type=text] { width:100%; margin-top:4px; padding:8px; border:1px solid var(--border); border-radius:8px; background:var(--bg); color:var(--fg); font:inherit; }
  button { margin-top:20px; width:100%; padding:12px; border:0; border-radius:8px; background:var(--accent); color:#fff; font:inherit; font-weight:600; cursor:pointer; }
  button:disabled { opacity:.5; cursor:default; }
  #status { margin-top:16px; min-height:1.5em; }
  .err { color:var(--err); } .ok { color:var(--ok); }
  footer { margin-top:16px; color:var(--muted); font-size:.85rem; }
</style>
</head>
<body>
<main>
  <h1>PDF zu Markdown</h1>
  <p class="sub">PDFs hochladen und als gleichnamige .md-Datei herunterladen. Eingescannte Seiten werden automatisch per OCR erkannt.</p>
  <form class="card" id="form">
    <div id="drop" tabindex="0">
      <strong>PDF-Dateien auswählen</strong> oder hierher ziehen<br>
      <small>max. {{MAX_MB}} MB pro Datei</small>
      <input id="file" type="file" accept="application/pdf,.pdf" multiple hidden>
    </div>
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
    <button id="go" type="submit" disabled>Umwandeln</button>
    <div id="status" role="status"></div>
  </form>
  <footer>Mehrere Dateien werden als ZIP geliefert. API: <code>POST /api/convert</code> – <a href="/api/docs">Doku</a></footer>
</main>
<script>
const $ = (id) => document.getElementById(id);
const drop = $("drop"), input = $("file"), list = $("list"), go = $("go"), statusEl = $("status");
let files = [];
const langSel = $("lang");
if (![...langSel.options].some(o => o.value === "{{DEFAULT_LANG}}")) langSel.add(new Option("{{DEFAULT_LANG}}", "{{DEFAULT_LANG}}"), 0);
langSel.value = "{{DEFAULT_LANG}}";

function setFiles(fl) {
  files = [...fl].filter(f => f.name.toLowerCase().endsWith(".pdf") || f.type === "application/pdf");
  list.innerHTML = "";
  for (const f of files) {
    const li = document.createElement("li");
    li.innerHTML = "<span></span><span></span>";
    li.children[0].textContent = f.name;
    li.children[1].textContent = (f.size / 1048576).toFixed(1) + " MB";
    list.appendChild(li);
  }
  go.disabled = files.length === 0;
  statusEl.textContent = "";
}
drop.addEventListener("click", () => input.click());
drop.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); input.click(); } });
input.addEventListener("change", () => setFiles(input.files));
["dragenter", "dragover"].forEach(ev => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
["dragleave", "drop"].forEach(ev => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
drop.addEventListener("drop", (e) => setFiles(e.dataTransfer.files));

function filenameFrom(header, fallback) {
  const star = /filename\\*=UTF-8''([^;]+)/i.exec(header || "");
  if (star) return decodeURIComponent(star[1]);
  const plain = /filename="([^"]+)"/i.exec(header || "");
  return plain ? plain[1] : fallback;
}

$("form").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (!files.length) return;
  const fd = new FormData();
  files.forEach(f => fd.append("files", f, f.name));
  fd.append("lang", langSel.value);
  fd.append("ocr", $("ocr").value);
  go.disabled = true;
  statusEl.className = "";
  statusEl.textContent = "Wird umgewandelt … (OCR kann bei Scans etwas dauern)";
  try {
    const res = await fetch("api/convert", { method: "POST", body: fd });
    if (!res.ok) {
      let msg = res.status + " " + res.statusText;
      try { const j = await res.json(); if (j.detail) msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail); } catch {}
      throw new Error(msg);
    }
    const blob = await res.blob();
    const fallback = files.length === 1 ? files[0].name.replace(/\\.pdf$/i, "") + ".md" : "markdown.zip";
    const name = filenameFrom(res.headers.get("Content-Disposition"), fallback);
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = name;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 10000);
    statusEl.className = "ok";
    statusEl.textContent = "Fertig: " + name;
  } catch (err) {
    statusEl.className = "err";
    statusEl.textContent = "Fehler: " + err.message;
  } finally {
    go.disabled = files.length === 0;
  }
});
</script>
</body>
</html>
"""
