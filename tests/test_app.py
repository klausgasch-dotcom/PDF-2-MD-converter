import io
import shutil
import zipfile
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

import app as webapp
from test_pdf2md import make_digital_pdf, make_scanned_pdf

client = TestClient(webapp.app)


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
