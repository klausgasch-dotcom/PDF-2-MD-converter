import io
import shutil
import zipfile
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

import app as webapp
from test_pdf2md import make_digital_pdf, make_scanned_pdf

client = TestClient(webapp.app)


@pytest.fixture(scope="module", autouse=True)
def _running_client():
    # Event-Loop über alle Requests am Leben halten (Hintergrund-Batches)
    with client:
        yield


def pdf_bytes(tmp_path, maker, name="x.pdf"):
    p = tmp_path / name
    maker(p)
    return p.read_bytes()


def test_index_and_health():
    assert client.get("/healthz").json() == {"status": "ok"}
    r = client.get("/")
    assert r.status_code == 200 and "PDF zu Markdown" in r.text


def test_single_file_keeps_name(tmp_path):
    data = pdf_bytes(tmp_path, make_digital_pdf)
    r = client.post("/api/convert", files={"files": ("Bericht Ä 2026.pdf", data, "application/pdf")})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/markdown")
    cd = r.headers["content-disposition"]
    assert unquote(cd.split("filename*=UTF-8''")[1]) == "Bericht Ä 2026.md"
    assert "Quartalsbericht 2026" in r.text


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="Tesseract nicht installiert")
def test_multiple_files_zip_with_ocr(tmp_path):
    files = [
        ("files", ("a.pdf", pdf_bytes(tmp_path, make_digital_pdf, "a.pdf"), "application/pdf")),
        ("files", ("Scan.pdf", pdf_bytes(tmp_path, make_scanned_pdf, "s.pdf"), "application/pdf")),
    ]
    r = client.post("/api/convert", files=files, data={"lang": "deu"})
    assert r.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    assert sorted(zf.namelist()) == ["Scan.md", "a.md"]
    assert "Eingescanntes Dokument" in zf.read("Scan.md").decode()


def test_rejects_non_pdf_and_bad_lang(tmp_path):
    r = client.post("/api/convert", files={"files": ("x.pdf", b"hello", "application/pdf")})
    assert r.status_code == 415
    data = pdf_bytes(tmp_path, make_digital_pdf)
    r = client.post("/api/convert", files={"files": ("x.pdf", data)}, data={"lang": "deu; rm -rf"})
    assert r.status_code == 400


def test_path_traversal_name(tmp_path):
    data = pdf_bytes(tmp_path, make_digital_pdf)
    r = client.post("/api/convert", files={"files": ("../../etc/passwd.pdf", data)})
    assert r.status_code == 200
    assert 'filename="passwd.md"' in r.headers["content-disposition"]


def test_basic_auth(monkeypatch):
    monkeypatch.setattr(webapp, "AUTH_USER", "u")
    monkeypatch.setattr(webapp, "AUTH_PASSWORD", "p")
    assert client.get("/").status_code == 401
    assert client.get("/", auth=("u", "wrong")).status_code == 401
    assert client.get("/", auth=("u", "p")).status_code == 200
    assert client.get("/healthz").status_code == 200


def _wait(job_id, timeout=120):
    import time
    end = time.time() + timeout
    while time.time() < end:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["done"]:
            return job
        time.sleep(0.2)
    raise AssertionError("Batch nicht fertig geworden")


def test_batch_job_continues_after_errors(tmp_path):
    digital = pdf_bytes(tmp_path, make_digital_pdf)
    files = [
        ("files", ("Bericht.pdf", digital, "application/pdf")),
        ("files", ("kaputt.pdf", b"kein pdf", "application/pdf")),
        ("files", ("ordner2/Bericht.pdf", digital, "application/pdf")),  # gleicher Name
        ("files", ("defekt.pdf", b"%PDF-1.7 abgeschnitten", "application/pdf")),
    ]
    r = client.post("/api/jobs", files=files)
    assert r.status_code == 202
    job = _wait(r.json()["id"])
    status = {f["filename"]: f["status"] for f in job["files"]}
    assert status == {"Bericht.pdf": "fertig", "kaputt.pdf": "fehler",
                      "ordner2/Bericht.pdf": "fertig", "defekt.pdf": "fehler"}
    assert [f["md_name"] for f in job["files"]][:3] == ["Bericht.md", "kaputt.md", "Bericht (2).md"]
    assert job["counts"] == {"wartend": 0, "läuft": 0, "fertig": 2, "fehler": 2}

    r = client.get(f"/api/jobs/{job['id']}/files/0")
    assert r.status_code == 200 and "Quartalsbericht" in r.text
    assert client.get(f"/api/jobs/{job['id']}/files/1").status_code == 409

    zf = zipfile.ZipFile(io.BytesIO(client.get(f"/api/jobs/{job['id']}/download").content))
    assert sorted(zf.namelist()) == ["Bericht (2).md", "Bericht.md", "_FEHLER.txt"]
    assert "kaputt.pdf: keine PDF-Datei" in zf.read("_FEHLER.txt").decode()

    assert client.delete(f"/api/jobs/{job['id']}").status_code == 204
    assert client.get(f"/api/jobs/{job['id']}").status_code == 404


def test_sync_convert_zip_skips_broken(tmp_path):
    digital = pdf_bytes(tmp_path, make_digital_pdf)
    files = [("files", ("a.pdf", digital)), ("files", ("b.pdf", b"nope"))]
    r = client.post("/api/convert", files=files)
    assert r.status_code == 200
    assert sorted(zipfile.ZipFile(io.BytesIO(r.content)).namelist()) == ["_FEHLER.txt", "a.md"]


def test_too_many_files(tmp_path, monkeypatch):
    monkeypatch.setattr(webapp, "MAX_FILES", 2)
    data = pdf_bytes(tmp_path, make_digital_pdf)
    r = client.post("/api/jobs", files=[("files", (f"{i}.pdf", data)) for i in range(3)])
    assert r.status_code == 400
