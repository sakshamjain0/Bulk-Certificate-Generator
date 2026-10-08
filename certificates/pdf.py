"""The single, predefined certificate template, rendered to PDF with ReportLab.

All layout lives in this module so the design can be changed without touching
the API or the job-processing code.
"""
import io
from dataclasses import dataclass
from datetime import date

from reportlab.lib.colors import HexColor, white
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.units import mm
from reportlab.lib.utils import simpleSplit
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas

PAGE_WIDTH, PAGE_HEIGHT = landscape(A4)
CENTER_X = PAGE_WIDTH / 2
CONTENT_WIDTH = PAGE_WIDTH - 2 * 35 * mm

NAVY = HexColor("#1F2A44")
GOLD = HexColor("#B8860B")
CREAM = HexColor("#FBF8F1")
TEXT = HexColor("#333333")
MUTED = HexColor("#7A7A7A")

# The built-in PDF fonts (Helvetica/Times) only cover the Windows-1252
# character set. Text outside it would silently render as garbage, so we
# reject it explicitly. To support any script, register a Unicode TTF font
# (e.g. DejaVuSans) and remove this check.
TEMPLATE_ENCODING = "cp1252"

# Set to 0 to emit uncompressed PDF content streams (handy for debugging/tests).
PAGE_COMPRESSION = 1


class CertificateRenderError(Exception):
    """Raised when the data cannot be rendered on the template."""


@dataclass(frozen=True)
class CertificateData:
    certificate_id: str
    recipient_name: str
    course_name: str
    issuer_name: str
    issue_date: date
    description: str = ""


def unsupported_characters(text: str) -> str:
    """Return the characters in ``text`` the template font cannot render."""
    bad = []
    for ch in text:
        try:
            ch.encode(TEMPLATE_ENCODING)
        except UnicodeEncodeError:
            if ch not in bad:
                bad.append(ch)
    return "".join(bad)


def _ensure_renderable(label: str, text: str) -> None:
    bad = unsupported_characters(text)
    if bad:
        raise CertificateRenderError(f"{label} contains characters not supported by the certificate font: {bad!r}")


def _draw_fitted_text(c, text, *, font, max_size, min_size, y, max_width, color,
                      center_x=CENTER_X, max_lines=2, anchor="center"):
    """Draw centred text, shrinking it to fit and wrapping if still too wide.

    anchor="center": the text block is vertically centred on ``y``.
    anchor="bottom": the last line sits on ``y`` (block grows upwards).
    """
    size = max_size
    while size > min_size and stringWidth(text, font, size) > max_width:
        size -= 1

    lines = simpleSplit(text, font, size, max_width) if stringWidth(text, font, size) > max_width else [text]
    if len(lines) > max_lines:
        raise CertificateRenderError(f"Text is too long to fit on the certificate: {text[:40]!r}...")

    leading = size * 1.2
    if anchor == "bottom":
        first_y = y + (len(lines) - 1) * leading
    else:
        first_y = y + (len(lines) - 1) * leading / 2

    c.setFillColor(color)
    c.setFont(font, size)
    for i, line in enumerate(lines):
        c.drawCentredString(center_x, first_y - i * leading, line)


def _draw_frame(c):
    c.setFillColor(CREAM)
    c.rect(0, 0, PAGE_WIDTH, PAGE_HEIGHT, fill=1, stroke=0)

    c.setStrokeColor(NAVY)
    c.setLineWidth(4)
    c.rect(12 * mm, 12 * mm, PAGE_WIDTH - 24 * mm, PAGE_HEIGHT - 24 * mm)

    c.setStrokeColor(GOLD)
    c.setLineWidth(1)
    c.rect(17 * mm, 17 * mm, PAGE_WIDTH - 34 * mm, PAGE_HEIGHT - 34 * mm)

    # Small gold corner ornaments.
    c.setFillColor(GOLD)
    for x, y in ((17 * mm, 17 * mm), (PAGE_WIDTH - 17 * mm, 17 * mm),
                 (17 * mm, PAGE_HEIGHT - 17 * mm), (PAGE_WIDTH - 17 * mm, PAGE_HEIGHT - 17 * mm)):
        c.circle(x, y, 4, fill=1, stroke=0)


def _draw_seal(c, x, y):
    c.setFillColor(GOLD)
    c.circle(x, y, 32, fill=1, stroke=0)
    c.setStrokeColor(white)
    c.setLineWidth(1)
    c.circle(x, y, 27, fill=0, stroke=1)
    c.setFillColor(white)
    c.setFont("Helvetica-Bold", 8)
    c.drawCentredString(x, y + 2, "CERTIFIED")
    c.setFont("Helvetica", 6)
    c.drawCentredString(x, y - 8, "AUTHENTIC")


def _draw_signature_block(c, x, y, value, label, font):
    _draw_fitted_text(c, value, font=font, max_size=15, min_size=9, y=y + 8,
                      max_width=200, color=TEXT, center_x=x, max_lines=3, anchor="bottom")
    c.setStrokeColor(NAVY)
    c.setLineWidth(0.8)
    c.line(x - 100, y, x + 100, y)
    c.setFillColor(MUTED)
    c.setFont("Helvetica", 10)
    c.drawCentredString(x, y - 15, label)


def render_certificate_pdf(data: CertificateData) -> bytes:
    """Render one certificate and return the PDF bytes."""
    _ensure_renderable("Recipient name", data.recipient_name)
    _ensure_renderable("Course name", data.course_name)
    _ensure_renderable("Issuer name", data.issuer_name)
    _ensure_renderable("Description", data.description)

    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=(PAGE_WIDTH, PAGE_HEIGHT), pageCompression=PAGE_COMPRESSION)
    c.setTitle(f"Certificate - {data.recipient_name}")
    c.setAuthor(data.issuer_name)
    c.setSubject(data.course_name)

    _draw_frame(c)

    # Heading
    c.setFillColor(NAVY)
    c.setFont("Helvetica-Bold", 38)
    c.drawCentredString(CENTER_X, PAGE_HEIGHT - 95, "CERTIFICATE")
    c.setFillColor(GOLD)
    c.setFont("Helvetica", 16)
    c.drawCentredString(CENTER_X, PAGE_HEIGHT - 122, "OF COMPLETION")
    c.setStrokeColor(GOLD)
    c.setLineWidth(1.2)
    c.line(CENTER_X - 60, PAGE_HEIGHT - 135, CENTER_X + 60, PAGE_HEIGHT - 135)

    c.setFillColor(TEXT)
    c.setFont("Helvetica-Oblique", 14)
    c.drawCentredString(CENTER_X, 415, "This is to certify that")

    # Recipient name (the main recipient-specific field)
    _draw_fitted_text(c, data.recipient_name, font="Times-BoldItalic", max_size=40, min_size=14,
                      y=362, max_width=CONTENT_WIDTH, color=NAVY)
    c.setStrokeColor(GOLD)
    c.setLineWidth(0.8)
    c.line(CENTER_X - 200, 335, CENTER_X + 200, 335)

    c.setFillColor(TEXT)
    c.setFont("Helvetica-Oblique", 14)
    c.drawCentredString(CENTER_X, 302, "has successfully completed")

    _draw_fitted_text(c, data.course_name, font="Helvetica-Bold", max_size=24, min_size=12,
                      y=265, max_width=CONTENT_WIDTH, color=NAVY)

    if data.description:
        _draw_fitted_text(c, data.description, font="Helvetica-Oblique", max_size=11, min_size=9,
                          y=222, max_width=CONTENT_WIDTH, color=MUTED, max_lines=3)

    # Footer: date | seal | issuer
    _draw_signature_block(c, PAGE_WIDTH * 0.25, 130, data.issue_date.strftime("%d %B %Y"),
                          "Date of Issue", "Helvetica")
    _draw_seal(c, CENTER_X, 132)
    _draw_signature_block(c, PAGE_WIDTH * 0.75, 130, data.issuer_name, "Issued by", "Times-Italic")

    c.setFillColor(MUTED)
    c.setFont("Helvetica", 8)
    c.drawCentredString(CENTER_X, 62, f"Certificate ID: {data.certificate_id}")

    c.showPage()
    c.save()
    return buffer.getvalue()
