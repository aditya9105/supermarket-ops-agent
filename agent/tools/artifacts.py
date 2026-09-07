"""
Artifacts tools — PDF invoice generation and PPTX analysis deck.

PDF: reportlab canvas with GST-correct table (HSN, taxable, CGST, SGST, total).
PPTX: four real matplotlib charts embedded as images in slides via python-pptx.

Both tools return bytes that the bot layer sends as Telegram documents.
"""
import io
import logging
import os
from datetime import date, timedelta

logger = logging.getLogger(__name__)

# ─── PDF Invoice ───────────────────────────────────────────────────────────────

def generate_invoice_pdf(bill_id: int) -> dict:
    """
    Generate a GST-compliant PDF invoice for a PAID bill.
    Returns {"status": "ok", "filename": str, "bytes": bytes}
    or {"error": ...}.
    """
    from db.connection import get_conn
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        SimpleDocTemplate, Table, TableStyle, Paragraph,
        Spacer, HRFlowable,
    )
    from reportlab.lib.enums import TA_CENTER, TA_RIGHT, TA_LEFT

    conn = get_conn()
    bill = conn.execute("SELECT * FROM bills WHERE bill_id = ?", (bill_id,)).fetchone()
    if not bill:
        return {"error": "bill_not_found", "bill_id": bill_id}
    if bill["status"] != "PAID":
        return {"error": "bill_not_paid", "status": bill["status"]}

    lines = conn.execute(
        """SELECT bi.line_id, bi.qty, bi.unit_price, bi.tax_slab, bi.hsn_code,
                  bi.taxable_amt, bi.cgst_amt, bi.sgst_amt, bi.line_total,
                  p.name, p.brand, p.unit
           FROM bill_items bi
           JOIN products p ON p.sku_id = bi.sku_id
           WHERE bi.bill_id = ?
           ORDER BY bi.line_id""",
        (bill_id,),
    ).fetchall()

    # Shop details from env / preferences
    shop_name    = os.getenv("SHOP_NAME", "My Kirana Store")
    shop_gstin   = os.getenv("SHOP_GSTIN", "")
    shop_address = os.getenv("SHOP_ADDRESS", "")
    shop_phone   = os.getenv("SHOP_PHONE", "")

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        rightMargin=15*mm, leftMargin=15*mm,
        topMargin=15*mm, bottomMargin=15*mm,
    )
    styles = getSampleStyleSheet()
    W = A4[0] - 30*mm  # usable width

    center  = ParagraphStyle("center",  parent=styles["Normal"], alignment=TA_CENTER, fontSize=10)
    right   = ParagraphStyle("right",   parent=styles["Normal"], alignment=TA_RIGHT,  fontSize=9)
    bold    = ParagraphStyle("bold",    parent=styles["Normal"], fontName="Helvetica-Bold", fontSize=11)
    title   = ParagraphStyle("title",   parent=styles["Normal"], fontName="Helvetica-Bold",
                              fontSize=16, alignment=TA_CENTER, spaceAfter=2)
    small   = ParagraphStyle("small",   parent=styles["Normal"], fontSize=8)

    story = []

    # ── Header ────────────────────────────────────────────────────────────────
    story.append(Paragraph(shop_name, title))
    if shop_address:
        story.append(Paragraph(shop_address, center))
    if shop_phone:
        story.append(Paragraph(f"Ph: {shop_phone}", center))
    if shop_gstin:
        story.append(Paragraph(f"GSTIN: {shop_gstin}", center))
    story.append(Spacer(1, 4*mm))
    story.append(HRFlowable(width="100%", thickness=1, color=colors.black))

    # ── Invoice meta ──────────────────────────────────────────────────────────
    story.append(Spacer(1, 2*mm))
    meta_data = [
        ["TAX INVOICE", "", f"Invoice #: {bill_id}", f"Date: {bill['bill_date'] or date.today()}"],
    ]
    if bill["customer_name"]:
        meta_data.append(["To:", bill["customer_name"], "", ""])
    meta_table = Table(meta_data, colWidths=[W*0.25, W*0.35, W*0.2, W*0.2])
    meta_table.setStyle(TableStyle([
        ("FONTNAME",  (0,0),(0,-1), "Helvetica-Bold"),
        ("FONTSIZE",  (0,0),(-1,-1), 9),
        ("ALIGN",     (2,0),(3,-1), "RIGHT"),
        ("BOTTOMPADDING", (0,0),(-1,-1), 2),
    ]))
    story.append(meta_table)
    story.append(Spacer(1, 3*mm))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.grey))
    story.append(Spacer(1, 2*mm))

    # ── Line items table ──────────────────────────────────────────────────────
    col_hdrs = ["#", "Item", "HSN", "Qty", "Rate\n(₹)", "Taxable\n(₹)",
                f"CGST", f"SGST", "Total\n(₹)"]
    # sub-headers for GST columns
    gst_sub  = ["", "", "", "", "", "", "Rate  Amt", "Rate  Amt", ""]

    table_data = [col_hdrs]
    for i, ln in enumerate(lines, 1):
        slab    = ln["tax_slab"]
        half    = slab / 2
        cgst_lbl = f"{half}%  ₹{ln['cgst_amt']:.2f}"
        sgst_lbl = f"{half}%  ₹{ln['sgst_amt']:.2f}"
        name    = f"{ln['brand']} {ln['name']}".strip()
        table_data.append([
            str(i),
            f"{name}\n({ln['unit']})",
            ln["hsn_code"],
            f"{ln['qty']}",
            f"{ln['unit_price']:.2f}",
            f"{ln['taxable_amt']:.2f}",
            cgst_lbl,
            sgst_lbl,
            f"{ln['line_total']:.2f}",
        ])

    col_widths = [
        W*0.04, W*0.22, W*0.07, W*0.07, W*0.09,
        W*0.10, W*0.15, W*0.15, W*0.11,
    ]
    item_table = Table(table_data, colWidths=col_widths, repeatRows=1)
    item_table.setStyle(TableStyle([
        ("BACKGROUND",    (0,0), (-1,0),  colors.HexColor("#2C3E50")),
        ("TEXTCOLOR",     (0,0), (-1,0),  colors.white),
        ("FONTNAME",      (0,0), (-1,0),  "Helvetica-Bold"),
        ("FONTSIZE",      (0,0), (-1,-1), 8),
        ("ALIGN",         (3,0), (-1,-1), "RIGHT"),
        ("ALIGN",         (0,0), (0,-1),  "CENTER"),
        ("ROWBACKGROUNDS",(0,1), (-1,-1), [colors.white, colors.HexColor("#F5F6FA")]),
        ("GRID",          (0,0), (-1,-1), 0.25, colors.HexColor("#BDC3C7")),
        ("TOPPADDING",    (0,0), (-1,-1), 2),
        ("BOTTOMPADDING", (0,0), (-1,-1), 2),
        ("WORDWRAP",      (1,1), (1,-1),  "CJK"),
    ]))
    story.append(item_table)
    story.append(Spacer(1, 3*mm))

    # ── Totals section ────────────────────────────────────────────────────────
    totals_data = [
        ["", "", "Subtotal (Taxable):", f"₹{bill['subtotal']:.2f}"],
        ["", "", "CGST:", f"₹{bill['cgst_total']:.2f}"],
        ["", "", "SGST:", f"₹{bill['sgst_total']:.2f}"],
        ["", "", "Grand Total:", f"₹{bill['grand_total']:.0f}"],
    ]
    # Build per-slab GST summary
    slab_summary: dict[int, dict] = {}
    for ln in lines:
        s = ln["tax_slab"]
        if s not in slab_summary:
            slab_summary[s] = {"taxable": 0, "cgst": 0, "sgst": 0}
        slab_summary[s]["taxable"] += ln["taxable_amt"]
        slab_summary[s]["cgst"]   += ln["cgst_amt"]
        slab_summary[s]["sgst"]   += ln["sgst_amt"]

    gst_rows = [["GST Slab", "Taxable (₹)", "CGST (₹)", "SGST (₹)", "Total GST (₹)"]]
    for slab, vals in sorted(slab_summary.items()):
        if slab == 0:
            continue
        gst_rows.append([
            f"{slab}%",
            f"{vals['taxable']:.2f}",
            f"{vals['cgst']:.2f}",
            f"{vals['sgst']:.2f}",
            f"{vals['cgst'] + vals['sgst']:.2f}",
        ])

    if len(gst_rows) > 1:
        gst_table = Table(gst_rows, colWidths=[W*0.12, W*0.22, W*0.22, W*0.22, W*0.22])
        gst_table.setStyle(TableStyle([
            ("BACKGROUND",   (0,0),(-1,0),  colors.HexColor("#ECF0F1")),
            ("FONTNAME",     (0,0),(-1,0),  "Helvetica-Bold"),
            ("FONTSIZE",     (0,0),(-1,-1), 8),
            ("ALIGN",        (1,0),(-1,-1), "RIGHT"),
            ("GRID",         (0,0),(-1,-1), 0.25, colors.HexColor("#BDC3C7")),
            ("TOPPADDING",   (0,0),(-1,-1), 2),
            ("BOTTOMPADDING",(0,0),(-1,-1), 2),
        ]))
        story.append(Paragraph("GST Breakup", bold))
        story.append(Spacer(1, 1*mm))
        story.append(gst_table)
        story.append(Spacer(1, 3*mm))

    # Grand total box
    grand_data = [
        ["Payment Mode:", bill["payment_mode"] or "", "GRAND TOTAL:", f"₹{bill['grand_total']:.0f}"],
    ]
    if bill["payment_ref"]:
        grand_data.append(["Ref:", bill["payment_ref"], "", ""])
    grand_table = Table(grand_data, colWidths=[W*0.20, W*0.30, W*0.25, W*0.25])
    grand_table.setStyle(TableStyle([
        ("FONTNAME",   (2,0),(3,0), "Helvetica-Bold"),
        ("FONTSIZE",   (2,0),(3,0), 12),
        ("FONTSIZE",   (0,0),(1,-1), 9),
        ("ALIGN",      (2,0),(3,-1), "RIGHT"),
        ("BACKGROUND", (2,0),(3,0),  colors.HexColor("#2ECC71")),
        ("TEXTCOLOR",  (2,0),(3,0),  colors.white),
        ("TOPPADDING", (0,0),(-1,-1), 3),
        ("BOTTOMPADDING", (0,0),(-1,-1), 3),
    ]))
    story.append(grand_table)

    story.append(Spacer(1, 5*mm))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.grey))
    story.append(Paragraph("Thank you for your purchase!", center))
    story.append(Paragraph("This is a computer-generated invoice.", small))

    doc.build(story)
    pdf_bytes = buf.getvalue()
    filename  = f"invoice_{bill_id}_{bill['bill_date'] or date.today()}.pdf"
    logger.info(f"Generated PDF invoice for bill_id={bill_id} ({len(pdf_bytes)} bytes)")
    return {"status": "ok", "filename": filename, "bytes": pdf_bytes}


# ─── PPTX Analysis Deck ────────────────────────────────────────────────────────

def generate_analysis_deck(from_date: str, to_date: str) -> dict:
    """
    Generate a PPTX analysis deck with 4 real matplotlib charts:
    1. Daily sales trend (line chart)
    2. Top 10 items by revenue (horizontal bar)
    3. Payment mode split (pie chart)
    4. Stock health: in-stock vs low-stock (bar)

    Returns {"status": "ok", "filename": str, "bytes": bytes}
    """
    import matplotlib
    matplotlib.use("Agg")  # non-interactive backend — safe for server
    import matplotlib.pyplot as plt
    import matplotlib.ticker as mticker
    import numpy as np
    from pptx import Presentation
    from pptx.util import Inches, Pt, Emu
    from pptx.dml.color import RGBColor
    from pptx.enum.text import PP_ALIGN
    from agent.tools.reports import get_sales_report
    from agent.tools.inventory import get_stock_report

    BRAND_DARK  = RGBColor(0x2C, 0x3E, 0x50)
    BRAND_GREEN = RGBColor(0x2E, 0xCC, 0x71)
    ACCENT      = "#2C3E50"

    prs = Presentation()
    prs.slide_width  = Inches(13.33)
    prs.slide_height = Inches(7.5)
    blank_layout = prs.slide_layouts[6]  # completely blank

    def _add_title_slide():
        slide = prs.slides.add_slide(blank_layout)
        # Background rectangle
        bg = slide.shapes.add_shape(
            1, Inches(0), Inches(0), prs.slide_width, prs.slide_height
        )
        bg.fill.solid(); bg.fill.fore_color.rgb = BRAND_DARK
        bg.line.fill.background()
        # Title text
        txBox = slide.shapes.add_textbox(Inches(1), Inches(2.5), Inches(11), Inches(1.5))
        tf = txBox.text_frame
        tf.word_wrap = True
        p = tf.paragraphs[0]
        p.alignment = PP_ALIGN.CENTER
        run = p.add_run()
        run.text = f"Sales & Operations Analysis"
        run.font.size = Pt(36)
        run.font.bold = True
        run.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
        # Sub-title
        txBox2 = slide.shapes.add_textbox(Inches(1), Inches(4), Inches(11), Inches(1))
        tf2 = txBox2.text_frame
        p2 = tf2.paragraphs[0]
        p2.alignment = PP_ALIGN.CENTER
        run2 = p2.add_run()
        run2.text = f"{from_date}  →  {to_date}"
        run2.font.size = Pt(18)
        run2.font.color.rgb = BRAND_GREEN

    def _chart_to_image(fig) -> io.BytesIO:
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=150, bbox_inches="tight",
                    facecolor=fig.get_facecolor())
        buf.seek(0)
        plt.close(fig)
        return buf

    def _add_chart_slide(title_text: str, img_buf: io.BytesIO):
        slide = prs.slides.add_slide(blank_layout)
        # Header bar
        hdr = slide.shapes.add_shape(
            1, Inches(0), Inches(0), prs.slide_width, Inches(0.7)
        )
        hdr.fill.solid(); hdr.fill.fore_color.rgb = BRAND_DARK
        hdr.line.fill.background()
        # Title
        txBox = slide.shapes.add_textbox(Inches(0.2), Inches(0.1), Inches(12), Inches(0.5))
        tf = txBox.text_frame
        p = tf.paragraphs[0]
        run = p.add_run()
        run.text = title_text
        run.font.size = Pt(18)
        run.font.bold = True
        run.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
        # Chart image
        slide.shapes.add_picture(img_buf, Inches(0.5), Inches(0.8), Inches(12.3), Inches(6.4))

    # ── Slide 1: Title ─────────────────────────────────────────────────────────
    _add_title_slide()

    # ── Slide 2: Daily sales trend ────────────────────────────────────────────
    daily = get_sales_report(from_date, to_date, group_by="day")
    if daily.get("data"):
        labels = [r["label"] for r in daily["data"]]
        values = [r["total_sales"] or 0 for r in daily["data"]]
        fig, ax = plt.subplots(figsize=(12, 5), facecolor="#F8F9FA")
        ax.plot(labels, values, marker="o", color=ACCENT, linewidth=2, markersize=5)
        ax.fill_between(range(len(labels)), values, alpha=0.15, color=ACCENT)
        ax.set_xticks(range(len(labels)))
        ax.set_xticklabels(labels, rotation=45, ha="right", fontsize=8)
        ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"₹{x:,.0f}"))
        ax.set_title("Daily Sales Revenue", fontsize=14, fontweight="bold")
        ax.set_ylabel("Revenue (₹)")
        ax.grid(axis="y", alpha=0.3)
        ax.set_facecolor("#F8F9FA")
        fig.tight_layout()
        _add_chart_slide("📈 Daily Sales Trend", _chart_to_image(fig))
    else:
        # No sales data — add a placeholder slide
        slide = prs.slides.add_slide(blank_layout)
        txBox = slide.shapes.add_textbox(Inches(2), Inches(3), Inches(9), Inches(1))
        txBox.text_frame.text = f"No sales data for {from_date} – {to_date}"

    # ── Slide 3: Top 10 items ─────────────────────────────────────────────────
    products = get_sales_report(from_date, to_date, group_by="product")
    if products.get("data"):
        top10 = products["data"][:10]
        names  = [r["label"][:25] for r in top10]
        revs   = [r["total_revenue"] or 0 for r in top10]
        fig, ax = plt.subplots(figsize=(12, 5), facecolor="#F8F9FA")
        bars = ax.barh(names[::-1], revs[::-1], color=ACCENT)
        ax.bar_label(bars, labels=[f"₹{v:,.0f}" for v in revs[::-1]],
                     padding=4, fontsize=8)
        ax.set_title("Top 10 Products by Revenue", fontsize=14, fontweight="bold")
        ax.set_xlabel("Revenue (₹)")
        ax.xaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"₹{x:,.0f}"))
        ax.set_facecolor("#F8F9FA")
        ax.grid(axis="x", alpha=0.3)
        fig.tight_layout()
        _add_chart_slide("🏆 Top 10 Products by Revenue", _chart_to_image(fig))

    # ── Slide 4: Payment mode split ───────────────────────────────────────────
    modes = get_sales_report(from_date, to_date, group_by="payment_mode")
    if modes.get("data"):
        mode_labels = [r["label"] for r in modes["data"]]
        mode_values = [r["total_sales"] or 0 for r in modes["data"]]
        COLORS = ["#2ECC71", "#3498DB", "#E74C3C", "#F39C12"]
        fig, ax = plt.subplots(figsize=(7, 5), facecolor="#F8F9FA")
        wedges, texts, autotexts = ax.pie(
            mode_values, labels=mode_labels, autopct="%1.1f%%",
            colors=COLORS[:len(mode_labels)], startangle=140,
            textprops={"fontsize": 11},
        )
        for at in autotexts:
            at.set_fontweight("bold")
        ax.set_title("Payment Mode Split", fontsize=14, fontweight="bold")
        ax.set_facecolor("#F8F9FA")
        fig.tight_layout()
        _add_chart_slide("💳 Payment Mode Split", _chart_to_image(fig))

    # ── Slide 5: Stock health ─────────────────────────────────────────────────
    stock = get_stock_report(low_stock_only=False)
    if stock.get("items"):
        items = stock["items"]
        names_s = [f"{i['brand']} {i['name']}".strip()[:20] for i in items[:15]]
        qty_s   = [i["stock_qty"] for i in items[:15]]
        reorder = [i["reorder_level"] for i in items[:15]]
        colors_s = [
            "#E74C3C" if q <= r else "#2ECC71"
            for q, r in zip(qty_s, reorder)
        ]
        fig, ax = plt.subplots(figsize=(12, 5.5), facecolor="#F8F9FA")
        x = range(len(names_s))
        bars = ax.bar(x, qty_s, color=colors_s, zorder=3)
        ax.plot(x, reorder, "r--", linewidth=1.5, label="Reorder Level", zorder=4)
        ax.set_xticks(list(x))
        ax.set_xticklabels(names_s, rotation=45, ha="right", fontsize=8)
        ax.set_title("Stock Health (🔴 below reorder, 🟢 healthy)", fontsize=13, fontweight="bold")
        ax.set_ylabel("Quantity")
        ax.legend()
        ax.grid(axis="y", alpha=0.3, zorder=0)
        ax.set_facecolor("#F8F9FA")
        fig.tight_layout()
        _add_chart_slide("📦 Stock Health", _chart_to_image(fig))

    # ── Save PPTX ─────────────────────────────────────────────────────────────
    buf = io.BytesIO()
    prs.save(buf)
    pptx_bytes = buf.getvalue()
    filename   = f"analysis_{from_date}_{to_date}.pptx"
    logger.info(f"Generated analysis deck ({len(pptx_bytes)} bytes) for {from_date}–{to_date}")
    return {"status": "ok", "filename": filename, "bytes": pptx_bytes}


# ─── Tool schemas ─────────────────────────────────────────────────────────────

ARTIFACT_TOOLS = [
    {
        "name": "generate_invoice_pdf",
        "description": (
            "Generate a GST-compliant PDF invoice for a finalized (PAID) bill. "
            "Returns the PDF as bytes to be sent as a Telegram document. "
            "Shows: item lines with HSN, taxable amount, CGST%, CGST amount, "
            "SGST%, SGST amount, line total; plus per-slab GST summary and grand total."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "bill_id": {"type": "integer", "description": "ID of a PAID bill."},
            },
            "required": ["bill_id"],
        },
    },
    {
        "name": "generate_analysis_deck",
        "description": (
            "Generate a PPTX analysis deck with 4 real matplotlib charts: "
            "daily sales trend, top-10 products by revenue, payment mode split, stock health. "
            "Returns the PPTX as bytes. Specify a date range."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "from_date": {"type": "string", "description": "Start date YYYY-MM-DD."},
                "to_date":   {"type": "string", "description": "End date YYYY-MM-DD."},
            },
            "required": ["from_date", "to_date"],
        },
    },
]

ARTIFACT_HANDLERS: dict[str, callable] = {
    "generate_invoice_pdf":    lambda args: generate_invoice_pdf(**args),
    "generate_analysis_deck":  lambda args: generate_analysis_deck(**args),
}
