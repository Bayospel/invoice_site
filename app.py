"""
Flask front end for the invoice sender.

The browser uploads three files (contacts list, invoice PDF template,
email message) and clicking "Send Invoices" processes the contacts,
looks up addresses, populates the PDF invoice, and sends the email with
the invoice attached via Resend's API.
"""
import io
import os
import re
import time
from pathlib import Path

import pdfplumber
import requests
import resend
from flask import Flask, render_template, request
from reportlab.pdfbase.pdfmetrics import stringWidth
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas

app = Flask(__name__)

# Resend API Setup
resend.api_key = os.environ.get("RESEND_API_KEY", "")
FROM_ADDRESS = os.environ.get("FROM_ADDRESS", "accounting@alphaprotectionspecialoperation.com")

# ---- Settings --------------------------------------------------------------
PLACEHOLDERS = {"name": "{{NAME}}", "company": "{{COMPANY}}", "address": "{{ADDRESS}}", "email": "{{EMAIL}}"}
FONT_REGULAR = "Helvetica"
FONT_BOLD = "Helvetica-Bold"
DEFAULT_SUBJECT = "Your invoice, {name}"
PAUSE_SECONDS = 1   # wait between emails

COVER_PAD_X = 3
COVER_PAD_TOP = 0.5
COVER_PAD_BOTTOM = 5

FALLBACK_POSITIONS = {
    "name": (0, 120, 650),
    "company": (0, 120, 630),
    "address": (0, 120, 610),
    "email": (0, 120, 590),
}
FALLBACK_SIZE = 12
# -----------------------------------------------------------------------------

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY")
GEOAPIFY_API_KEY = os.environ.get("GEOAPIFY_API_KEY")
GEOAPIFY_URL = "https://api.geoapify.com/v1/geocode/search"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
_last_nominatim_call = [0.0]
PLACES_URL = "https://places.googleapis.com/v1/places:searchText"
ADDRESS_CACHE = {}
ADDRESS_MAX_WIDTH = 280


def read_text_bytes(data, is_pdf):
    if is_pdf:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            return "\n".join(p.extract_text() or "" for p in pdf.pages)
    return data.decode("utf-8", errors="ignore")


def parse_contacts(data, is_pdf):
    contacts, seen = [], set()
    for line in read_text_bytes(data, is_pdf).splitlines():
        m = EMAIL_RE.search(line)
        if not m:
            continue
        email = m.group(0)
        
        # Split line cleanly by pipes, commas, or tabs
        if "|" in line:
            parts = [p.strip() for p in line.split("|")]
        elif "\t" in line:
            parts = [p.strip() for p in line.split("\t")]
        else:
            parts = [p.strip() for p in re.split(r"[,;]+", line)]
        
        parts = [p for p in parts if p]
        
        if len(parts) < 3 or email.lower() in seen:
            continue
        
        seen.add(email.lower())
        # Map fields correctly based on your format: email | name | company
        contacts.append({
            "email": parts[0],
            "name": parts[1],       # Middle name / president (e.g. Martin Jojo)
            "company": parts[2]     # Company name (e.g. martin company)
        })
    return contacts


def provider_name():
    if GOOGLE_API_KEY:
        return "Google Places"
    if GEOAPIFY_API_KEY:
        return "Geoapify"
    return "OpenStreetMap"


def _google_address(company):
    r = requests.post(
        PLACES_URL,
        headers={"X-Goog-Api-Key": GOOGLE_API_KEY, "X-Goog-FieldMask": "places.displayName,places.formattedAddress"},
        json={"textQuery": company, "pageSize": 1}, timeout=10,
    )
    r.raise_for_status()
    places = r.json().get("places", [])
    return places[0]["formattedAddress"] if places else ""


def _geoapify_address(company):
    r = requests.get(GEOAPIFY_URL, params={"text": company, "limit": 1, "format": "json", "apiKey": GEOAPIFY_API_KEY}, timeout=10)
    r.raise_for_status()
    results = r.json().get("results", [])
    if not results:
        return ""
    hit = results[0]
    if hit.get("name") and hit.get("address_line2"):
        return hit["address_line2"]
    return hit.get("formatted", "")


def _nominatim_address(company):
    wait = 1.1 - (time.time() - _last_nominatim_call[0])
    if wait > 0:
        time.sleep(wait)
    _last_nominatim_call[0] = time.time()
    r = requests.get(
        NOMINATIM_URL,
        params={"q": company, "format": "jsonv2", "addressdetails": 1, "limit": 1},
        headers={"User-Agent": "invoice-sender-site/1.0"}, timeout=15,
    )
    r.raise_for_status()
    results = r.json()
    if not results:
        return ""
    a = results[0].get("address", {})
    street = " ".join(x for x in (a.get("house_number"), a.get("road")) if x)
    place = a.get("city") or a.get("town") or a.get("village") or a.get("suburb") or a.get("county")
    parts = [street, place, a.get("state"), a.get("postcode"), a.get("country")]
    return ", ".join(p for p in parts if p) or results[0].get("display_name", "")


def lookup_address(company):
    key = company.strip().lower()
    if key in ADDRESS_CACHE:
        return ADDRESS_CACHE[key]
    address = ""
    try:
        if GOOGLE_API_KEY:
            address = _google_address(company)
        elif GEOAPIFY_API_KEY:
            address = _geoapify_address(company)
        else:
            address = _nominatim_address(company)
    except Exception:
        pass
    ADDRESS_CACHE[key] = address
    return address


def find_placeholders(pdf_bytes):
    found = {k: [] for k in PLACEHOLDERS}
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for pi, page in enumerate(pdf.pages):
            for key, ph in PLACEHOLDERS.items():
                for hit in page.search(re.escape(ph)):
                    size = hit["chars"][0].get("size", FALLBACK_SIZE) if hit.get("chars") else FALLBACK_SIZE
                    found[key].append((pi, hit["x0"], hit["x1"], hit["top"], hit["bottom"], size))
    return found


def fit_size(key, value, size):
    font_to_use = FONT_BOLD if key == "company" else FONT_REGULAR
    if key in ("address", "email") and value:
        width = stringWidth(value, font_to_use, size)
        if width > ADDRESS_MAX_WIDTH:
            return max(6, size * ADDRESS_MAX_WIDTH / width)
    return size


def make_invoice(template_bytes, values, locations):
    reader = PdfReader(io.BytesIO(template_bytes))
    ops = {i: [] for i in range(len(reader.pages))}

    for key, value in values.items():
        if key not in PLACEHOLDERS:
            continue
        spots = locations[key]
        font_style = FONT_BOLD if key == "company" else FONT_REGULAR
        text_color = (0.1, 0.1, 0.1) if key == "company" else (0.2, 0.2, 0.2)

        if spots:
            for pi, x0, x1, top, bottom, size in spots:
                ph = float(reader.pages[pi].mediabox.height)
                ops[pi].append((
                    "cover",
                    x0 - COVER_PAD_X,
                    ph - bottom - COVER_PAD_BOTTOM,
                    (x1 - x0) + 2 * COVER_PAD_X,
                    (bottom - top) + COVER_PAD_TOP + COVER_PAD_BOTTOM,
                ))
                ops[pi].append(("text", x0, ph - bottom + size * 0.2, value, fit_size(key, value, size), font_style, text_color))
        else:
            pi, x, y = FALLBACK_POSITIONS[key]
            ops[pi].append(("text", x, y, value, fit_size(key, value, FALLBACK_SIZE), font_style, text_color))

    writer = PdfWriter()
    for i, src in enumerate(reader.pages):
        page = writer.add_page(src)
        if ops[i]:
            w, h = float(page.mediabox.width), float(page.mediabox.height)
            buf = io.BytesIO()
            c = canvas.Canvas(buf, pagesize=(w, h))
            for op in ops[i]:
                if op[0] == "cover":
                    c.setFillColorRGB(1, 1, 1)
                    c.rect(op[1], op[2], op[3], op[4], stroke=0, fill=1)
            for op in ops[i]:
                if op[0] == "text":
                    c.setFont(op[5], op[4])
                    c.setFillColorRGB(*op[6])
                    c.drawString(op[1], op[2], op[3])
            c.save()
            buf.seek(0)
            page.merge_page(PdfReader(buf).pages[0])

    for page in writer.pages:
        refs = page.get("/Contents")
        refs = refs.get_object() if hasattr(refs, "get_object") else refs
        if not isinstance(refs, list):
            refs = [refs]
        for ref in refs:
            obj = ref.get_object() if hasattr(ref, "get_object") else ref
            if not hasattr(obj, "get_data"):
                continue
            data = obj.get_data()
            new = data
            for ph in PLACEHOLDERS.values():
                for enc in ["utf-8", "latin-1", "utf-16be"]:
                    try:
                        raw = ph.encode(enc)
                        new = new.replace(raw, b" " * len(raw))
                    except Exception:
                        pass
            if new != data:
                obj.set_data(new)

    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def load_email_template(data):
    text = data.decode("utf-8-sig", errors="ignore").lstrip()
    subject = DEFAULT_SUBJECT
    if text.lower().startswith("subject:"):
        first, _, text = text.partition("\n")
        subject = first.split(":", 1)[1].strip()
    return subject, text.lstrip("\n")


def fill(text, values):
    for k, v in values.items():
        text = text.replace("{" + k + "}", v)
    return text


ACCOUNT_ISSUE_PATTERNS = [
    "invalid api key", "invalid_api_key", "unauthorized", "forbidden",
    "restricted", "suspended", "not verified", "domain is not verified",
]


def account_issue_message(error_text):
    low = error_text.lower()
    for pattern in ACCOUNT_ISSUE_PATTERNS:
        if pattern in low:
            return (
                "Stopped — this looks like a problem with the Resend account or domain, "
                "not with one customer. Check the Resend dashboard (API key, domain "
                f"verification, account status) before trying again. Detail: {error_text}"
            )
    return None


@app.route("/", methods=["GET"])
def index():
    return render_template("index.html", run=None, from_address=FROM_ADDRESS)


@app.route("/send", methods=["POST"])
def send_invoices():
    contacts_file = request.files["contacts"]
    invoice_file = request.files["invoice"]
    message_file = request.files["message"]
    fixed_subject = request.form.get("fixed_subject", "").strip()

    contacts_bytes = contacts_file.read()
    is_pdf = contacts_file.filename.lower().endswith(".pdf")
    invoice_bytes = invoice_file.read()
    message_bytes = message_file.read()

    contacts = parse_contacts(contacts_bytes, is_pdf)
    locations = find_placeholders(invoice_bytes)
    subject_t, body_t = load_email_template(message_bytes)
    if fixed_subject:
        subject_t = fixed_subject

    run = {"results": [], "sent": 0, "failed": 0, "total": len(contacts), "account_issue": None}

    resend.api_key = os.environ.get("RESEND_API_KEY", "")

    if not resend.api_key:
        run["account_issue"] = "RESEND_API_KEY is not set on the server. Add it under Environment Variables and redeploy."
        return render_template("index.html", run=run, from_address=FROM_ADDRESS)

    for c in contacts:
        tag = f"{c['name']} <{c['email']}> — {c['company']}"
        try:
            address = lookup_address(c["company"])
            first_name = c["name"].split()[0] if c["name"] else ""
            values = {
                "name": c["name"],
                "first_name": first_name,
                "company": c["company"],
                "address": address,
                "email": c["email"],
            }
            
            customer_contact_field = f"{c['name']} ({c['email']})"
            pdf_values = {**values, "name": customer_contact_field}
            pdf_bytes = make_invoice(invoice_bytes, pdf_values, locations)
            
            safe = re.sub(r"[^A-Za-z0-9]+", "_", c["name"]).strip("_")
            fname = f"Invoice_{safe}.pdf"

            # Use the middle name/president field (e.g., Martin Jojo, Derik law) as the sender display name
            sender_name = c["name"].strip()
            resend.Emails.send({
                "from": f"{sender_name} <{FROM_ADDRESS}>",
                "to": [c["email"]],
                "subject": fill(subject_t, values),
                "text": fill(body_t, values),
                "attachments": [{"filename": fname, "content": list(pdf_bytes)}],
            })
            run["results"].append({"tag": tag, "ok": True, "detail": "Sent"})
            run["sent"] += 1
            time.sleep(PAUSE_SECONDS)
        except Exception as e:
            issue = account_issue_message(str(e))
            if issue:
                run["account_issue"] = issue
                break
            run["results"].append({"tag": tag, "ok": False, "detail": str(e)})
            run["failed"] += 1

    return render_template("index.html", run=run, from_address=FROM_ADDRESS)


if __name__ == "__main__":
    app.run(debug=True)
