"""
Bulk invoice sender (v7: automatic company address lookup, label-safe cover boxes, no leftover brace marks).

Inputs:
  1. contacts file (.txt or .pdf): one customer per line -> Name, Email, Company
  2. invoice PDF with placeholders {{NAME}}, {{COMPANY}} and {{ADDRESS}} typed
     where the customer's name, company and company address should appear
  3. email template (.txt) using {name}, {first_name}, {company} and {address}; optional first
     line "Subject: ..." sets the subject

For each customer the script looks up the company's address, replaces the
placeholders in the invoice, fills the email template, and sends the email with
the invoice attached. Each company is looked up once per run.

Address lookup uses the first of these that is set up:
  1. Google Places   (needs GOOGLE_API_KEY and a Google Cloud billing account)
  2. Geoapify        (needs GEOAPIFY_API_KEY; free signup, no card)
  3. OpenStreetMap   (no key at all; free, about 1 lookup per second)

Setup:
  pip install pdfplumber pypdf reportlab requests
  export GEOAPIFY_API_KEY=your-key      # optional; or GOOGLE_API_KEY. With neither, OpenStreetMap is used
  export SMTP_HOST=smtp.gmail.com SMTP_PORT=587   # use 465 if 587 is blocked
  export SMTP_USER=you@gmail.com SMTP_PASS=your-app-password

Usage:
  python send_invoices_v9.py contacts.txt invoice.pdf message.txt          # dry run
  python send_invoices_v9.py contacts.txt invoice.pdf message.txt --send   # send

Contacts file lines (comma, tab, semicolon or | between fields; any order of
the email is fine, but name comes before company):
  John Smith, john@example.com, Acme Inc
"""
import io
import os
import re
import smtplib
import sys
import time
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path

import pdfplumber
import requests
from reportlab.pdfbase.pdfmetrics import stringWidth
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas

# ---- Settings --------------------------------------------------------------
PLACEHOLDERS = {
    "name": "{{NAME}}",
    "company": "{{COMPANY}}",
    "address": "{{ADDRESS}}",
    "email": "{{EMAIL}}",
}
OPTIONAL_PLACEHOLDERS = {"email"}   # fine if missing from the invoice; nothing gets drawn
FONT_REGULAR = "Helvetica"
FONT_BOLD = "Helvetica-Bold"
DEFAULT_SUBJECT = "Your invoice, {name}"
FIXED_SUBJECT = "Invoice Delivery - INV-0038-2616"   # always used as the email subject
PAUSE_SECONDS = 2   # wait between emails

# White box drawn over each placeholder, in points.
COVER_PAD_X = 3
COVER_PAD_TOP = 0.5
COVER_PAD_BOTTOM = 5

# Used only if a placeholder is NOT found in the invoice:
# (page index, x, y) in points from the bottom-left of the page.
FALLBACK_POSITIONS = {
    "name": (0, 120, 650),
    "company": (0, 120, 630),
    "address": (0, 120, 610),
    "email": (0, 120, 590),
}
FALLBACK_SIZE = 12
# ---------------------------------------------------------------------------

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY")
GEOAPIFY_API_KEY = os.environ.get("GEOAPIFY_API_KEY")
GEOAPIFY_URL = "https://api.geoapify.com/v1/geocode/search"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
_last_nominatim_call = [0.0]
PLACES_URL = "https://places.googleapis.com/v1/places:searchText"
ADDRESS_CACHE = {}
ADDRESS_MAX_WIDTH = 280  # points; long addresses shrink to fit so they don't hit the right-hand column


def read_text(path):
    if Path(path).suffix.lower() == ".pdf":
        with pdfplumber.open(path) as pdf:
            return "\n".join(p.extract_text() or "" for p in pdf.pages)
    return Path(path).read_text(encoding="utf-8", errors="ignore")


def parse_contacts(path):
    contacts, seen = [], set()
    for line in read_text(path).splitlines():
        m = EMAIL_RE.search(line)
        if not m:
            continue
        email = m.group(0)
        rest = line[:m.start()] + "," + line[m.end():]
        if "|" in line:
            splitter = r"\|+"
        elif "\t" in line:
            splitter = r"\t+"
        else:
            splitter = r"[,;]+"
        rest = rest.replace(",", " ", 1) if splitter != r"[,;]+" else rest
        fields = [f.strip(" ,") for f in re.split(splitter, rest) if f.strip(" ,")]
        if len(fields) < 2 or email.lower() in seen:
            print(f"SKIPPED (need name + company, or duplicate): {line.strip()}")
            continue
        seen.add(email.lower())
        contacts.append({"name": fields[0], "company": ", ".join(fields[1:]) if splitter == r"[,;]+" else fields[1], "email": email})
    return contacts


def provider_name():
    if GOOGLE_API_KEY:
        return "Google Places"
    if GEOAPIFY_API_KEY:
        return "Geoapify"
    return "OpenStreetMap (free, no key; small or unlisted companies may not be found)"


def _google_address(company):
    r = requests.post(
        PLACES_URL,
        headers={
            "X-Goog-Api-Key": GOOGLE_API_KEY,
            "X-Goog-FieldMask": "places.displayName,places.formattedAddress",
        },
        json={"textQuery": company, "pageSize": 1},
        timeout=10,
    )
    r.raise_for_status()
    places = r.json().get("places", [])
    return places[0]["formattedAddress"] if places else ""


def _geoapify_address(company):
    r = requests.get(
        GEOAPIFY_URL,
        params={"text": company, "limit": 1, "format": "json", "apiKey": GEOAPIFY_API_KEY},
        timeout=10,
    )
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
        headers={"User-Agent": "invoice-sender-script/1.0"},
        timeout=15,
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
    except Exception as e:
        print(f"  Address lookup failed for {company}: {e}")
    ADDRESS_CACHE[key] = address
    return address


def find_placeholders(template_path):
    found = {k: [] for k in PLACEHOLDERS}
    with pdfplumber.open(template_path) as pdf:
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


def make_invoice(template_path, values, locations):
    reader = PdfReader(template_path)
    ops = {i: [] for i in range(len(reader.pages))}

    for key, value in values.items():
        if key not in PLACEHOLDERS:
            continue  # e.g. first_name is only for the email text, not the invoice
        spots = locations[key]
        if not spots and key in OPTIONAL_PLACEHOLDERS:
            continue  # optional placeholder not in this invoice: draw nothing
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
                    font_name = op[5]
                    color = op[6]
                    c.setFont(font_name, op[4])
                    c.setFillColorRGB(color[0], color[1], color[2])
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


def load_email_template(path):
    text = Path(path).read_text(encoding="utf-8-sig", errors="ignore").lstrip()
    subject = DEFAULT_SUBJECT
    if text.lower().startswith("subject:"):
        first, _, text = text.partition("\n")
        subject = first.split(":", 1)[1].strip()
    return subject, text.lstrip("\n")


def fill(text, values):
    for k, v in values.items():
        text = text.replace("{" + k + "}", v)
    return text


def main():
    args = sys.argv[1:]
    send = "--send" in args
    files = [a for a in args if a != "--send"]
    if len(files) != 3:
        sys.exit(__doc__)
    contacts_file, invoice_file, message_file = files

    contacts = parse_contacts(contacts_file)
    locations = find_placeholders(invoice_file)
    subject_t, body_t = load_email_template(message_file)
    subject_t = FIXED_SUBJECT   # ignore any Subject line in the template

    for key, ph in PLACEHOLDERS.items():
        if locations[key]:
            status = f"found {len(locations[key])}x"
        elif key in OPTIONAL_PLACEHOLDERS:
            status = "not in invoice (optional, skipped)"
        else:
            status = "NOT FOUND, using fallback position"
        print(f"Invoice placeholder {ph}: {status}")
    print(f"Contacts found: {len(contacts)}")
    print(f"Address lookup: {provider_name()}")
    print()

    server = None
    if send:
        host = os.environ["SMTP_HOST"]
        port = int(os.environ.get("SMTP_PORT", 587))
        try:
            if port == 465:
                server = smtplib.SMTP_SSL(host, port, timeout=30)
            else:
                server = smtplib.SMTP(host, port, timeout=30)
                server.starttls()
            server.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
        except OSError as e:
            sys.exit(f"Could not connect to {host}:{port} ({e}).\n"
                     "Turn off any VPN, check your data/Wi-Fi, or try SMTP_PORT=465.")

    out_dir = Path("generated_invoices")
    out_dir.mkdir(exist_ok=True)

    for i, c in enumerate(contacts, 1):
        tag = f"[{i}/{len(contacts)}] {c['name']} ({c['company']}) <{c['email']}>"
        try:
            address = lookup_address(c["company"])
            if not address:
                print(f"  WARNING no address found for {c['company']}")
            
            first_name = c["name"].split()[0] if c["name"] else ""
            customer_contact_field = c["name"]   # name only on the PDF, no email
            
            values = {
                "name": c["name"],
                "first_name": first_name,
                "company": c["company"],
                "address": address,
                "email": c["email"],
            }
            
            pdf_values = {**values, "name": customer_contact_field}
            pdf_bytes = make_invoice(invoice_file, pdf_values, locations)
            
            safe = re.sub(r"[^A-Za-z0-9]+", "_", c["name"]).strip("_")
            fname = f"Invoice_{safe}.pdf"
            (out_dir / fname).write_bytes(pdf_bytes)

            if send:
                msg = EmailMessage()
                from_name = " ".join(c["company"].split())
                msg["From"] = formataddr((from_name, os.environ["SMTP_USER"]))
                msg["To"] = formataddr((c["name"], c["email"]))   # shows as "Name <email>"
                msg["Subject"] = fill(subject_t, values)
                msg.set_content(fill(body_t, values))
                msg.add_attachment(pdf_bytes, maintype="application", subtype="pdf", filename=fname)
                server.send_message(msg)
                print(f"SENT    {tag}")
                time.sleep(PAUSE_SECONDS)
            else:
                print(f"DRY RUN {tag}")
        except Exception as e:
            print(f"ERROR   {tag}: {e}")

    if server:
        server.quit()


if __name__ == "__main__":
    main()
