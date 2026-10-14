"""
Invoice sender website. Wraps send_invoices_v4.py in a web page.

Run locally:  pip install -r requirements.txt && python app.py   (opens on http://localhost:5000)
Environment:  GOOGLE_API_KEY or GEOAPIFY_API_KEY  (optional; address lookup falls back to OpenStreetMap)
              APP_PASSWORD    (login password for the site; SET THIS if the site is online)
"""
import contextlib
import io
import os
import re
import shutil
import smtplib
import tempfile
import threading
import time
import uuid
from email.message import EmailMessage
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request, send_file

import send_invoices_v4 as inv

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024
APP_PASSWORD = os.environ.get("APP_PASSWORD")
JOBS = {}


@app.before_request
def require_login():
    if APP_PASSWORD:
        a = request.authorization
        if not a or a.password != APP_PASSWORD:
            return Response("Login required", 401, {"WWW-Authenticate": 'Basic realm="Invoices"'})


@contextlib.contextmanager
def captured(log):
    """The v4 functions print() their warnings; show those in the web log instead."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        yield
    for line in buf.getvalue().splitlines():
        if line.strip():
            log(line.strip())


def connect(cfg):
    port = int(cfg["port"])
    if port == 465:
        server = smtplib.SMTP_SSL(cfg["host"], port, timeout=30)
    else:
        server = smtplib.SMTP(cfg["host"], port, timeout=30)
        server.starttls()
    server.login(cfg["user"], cfg["pass"])
    return server


def run_job(job, d, cfg, send):
    log = job["log"].append
    server = None
    try:
        with captured(log):
            contacts = inv.parse_contacts(job["contacts"])
            locations = inv.find_placeholders(job["invoice"])
        subject_t, body_t = inv.load_email_template(job["message"])
        for key, ph in inv.PLACEHOLDERS.items():
            status = f"found {len(locations[key])}x" if locations[key] else "NOT FOUND, using fallback position"
            log(f"Invoice placeholder {ph}: {status}")
        log(f"Contacts found: {len(contacts)}")
        log(f"Address lookup: {inv.provider_name()}")
        if send:
            server = connect(cfg)
            log("Mail server connected")

        out = d / "generated"
        out.mkdir()
        for i, c in enumerate(contacts, 1):
            tag = f"[{i}/{len(contacts)}] {c['name']} ({c['company']}) <{c['email']}>"
            try:
                with captured(log):
                    address = inv.lookup_address(c["company"])
                if not address:
                    log(f"  WARNING no address found for {c['company']}")
                values = {"name": c["name"], "company": c["company"], "address": address}
                pdf_bytes = inv.make_invoice(job["invoice"], values, locations)
                safe = re.sub(r"[^A-Za-z0-9]+", "_", c["name"]).strip("_")
                fname = f"Invoice_{safe}.pdf"
                if (out / fname).exists():
                    fname = f"Invoice_{safe}_{i}.pdf"
                (out / fname).write_bytes(pdf_bytes)
                if send:
                    msg = EmailMessage()
                    msg["From"] = cfg["user"]
                    msg["To"] = c["email"]
                    msg["Subject"] = inv.fill(subject_t, values)
                    msg.set_content(inv.fill(body_t, values))
                    msg.add_attachment(pdf_bytes, maintype="application", subtype="pdf", filename=fname)
                    server.send_message(msg)
                    log(f"SENT    {tag}")
                    time.sleep(inv.PAUSE_SECONDS)
                else:
                    log(f"PREVIEW {tag}")
            except Exception as e:
                log(f"ERROR   {tag}: {e}")
    except Exception as e:
        log(f"FAILED: {e}")
    finally:
        if server:
            try:
                server.quit()
            except Exception:
                pass
        # Don't keep customer data on the server after a real send.
        if send:
            shutil.rmtree(d, ignore_errors=True)
        else:
            Path(job["contacts"]).unlink(missing_ok=True)
        job["running"] = False
        log("DONE")


@app.get("/")
def index():
    return render_template("index.html")


@app.post("/start")
def start():
    if any(j["running"] for j in JOBS.values()):
        return jsonify(error="A job is already running."), 409
    files = request.files
    if not all(k in files and files[k].filename for k in ("contacts", "invoice")):
        return jsonify(error="Please upload the contacts list and the invoice template."), 400
    send = request.form.get("mode") == "send"
    cfg = {k: request.form.get(k, "").strip() for k in ("host", "port", "user")}
    cfg["pass"] = request.form.get("pass", "")
    if send:
        if not all(cfg.values()) or not cfg["port"].isdigit():
            return jsonify(error="Fill in the mail server host, port, username and password."), 400

    d = Path(tempfile.mkdtemp())
    paths = {
        "contacts": d / ("contacts" + (Path(files["contacts"].filename).suffix.lower() or ".txt")),
        "invoice": d / "invoice.pdf",
        "message": d / "message.txt",
    }
    files["contacts"].save(paths["contacts"])
    files["invoice"].save(paths["invoice"])
    paths["message"].write_text(request.form.get("message", ""), encoding="utf-8")

    job_id = uuid.uuid4().hex
    job = {"log": [], "running": True, "send": send, "dir": d, **{k: str(v) for k, v in paths.items()}}
    JOBS[job_id] = job
    threading.Thread(target=run_job, args=(job, d, cfg, send), daemon=True).start()
    return jsonify(id=job_id)


@app.get("/status/<job_id>")
def status(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify(error="Unknown job"), 404
    since = int(request.args.get("since", 0))
    return jsonify(log=job["log"][since:], next=len(job["log"]), running=job["running"], send=job["send"])


@app.get("/download/<job_id>")
def download(job_id):
    job = JOBS.get(job_id)
    if not job or job["send"] or job["running"]:
        return "Not available", 404
    zip_base = str(Path(job["dir"]) / "preview")
    shutil.make_archive(zip_base, "zip", Path(job["dir"]) / "generated")
    return send_file(zip_base + ".zip", as_attachment=True, download_name="preview_invoices.zip")


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000)
