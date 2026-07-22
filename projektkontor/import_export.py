from __future__ import annotations

import io
import re
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from openpyxl import Workbook, load_workbook
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Image, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .db import Database


NAVY = colors.HexColor("#0F2E5D")
SAGE = colors.HexColor("#6F8B74")
LIGHT = colors.HexColor("#E6E9ED")


def safe_paragraph(value: Any) -> str:
    return escape(str(value or "")).replace("\n", "<br/>")


def report_date(value: str, include_time: bool) -> str:
    parsed = datetime.fromisoformat(value)
    return parsed.strftime("%d.%m.%Y, %H:%M Uhr") if include_time else parsed.strftime("%d.%m.%Y")


def normalize_username(first_name: str) -> str:
    value = first_name.strip().lower()
    value = value.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    value = re.sub(r"[^a-z0-9]", "", value)
    return value or "konto"


def next_username(connection, first_name: str) -> str:
    base = normalize_username(first_name)
    rows = connection.execute(
        "SELECT username FROM users WHERE username LIKE ? COLLATE NOCASE",
        (base + "__",),
    ).fetchall()
    used = {str(row[0]).lower() for row in rows}
    for number in range(1, 100):
        candidate = f"{base}{number:02d}"
        if candidate not in used:
            return candidate
    raise ValueError(f"Für {first_name} können keine weiteren Benutzernamen mit zwei Ziffern erzeugt werden.")


def account_template(default_class: str = "") -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "ProjektKontor Import"
    sheet.append(["Vorname", "Klasse"])
    sheet.append(["Lena", default_class or "GH24"])
    sheet.append(["Yusuf", default_class or "GH24"])
    sheet.freeze_panes = "A2"
    sheet.column_dimensions["A"].width = 28
    sheet.column_dimensions["B"].width = 18
    for cell in sheet[1]:
        cell.font = cell.font.copy(bold=True, color="FFFFFF")
        cell.fill = cell.fill.copy(fill_type="solid", fgColor="0F2E5D")
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def parse_account_workbook(data: bytes) -> list[dict[str, Any]]:
    try:
        workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:
        raise ValueError("Die Excel-Datei konnte nicht gelesen werden.") from exc
    sheet = workbook.active
    header = [str(cell.value or "").strip().lower() for cell in next(sheet.iter_rows(max_row=1))]
    if "vorname" not in header:
        raise ValueError("Die Spalte „Vorname“ fehlt.")
    column = header.index("vorname")
    class_column = header.index("klasse") if "klasse" in header else None
    result: list[dict[str, Any]] = []
    for row_number, row in enumerate(sheet.iter_rows(min_row=2, values_only=True), start=2):
        value = str(row[column] or "").strip()
        class_name = str(row[class_column] or "").strip() if class_column is not None and class_column < len(row) else ""
        if not value:
            continue
        if len(value) > 80:
            result.append({"row": row_number, "first_name": value[:80], "class_name": class_name, "error": "Vorname ist zu lang."})
        else:
            result.append({"row": row_number, "first_name": value, "class_name": class_name, "error": ""})
    if not result:
        raise ValueError("Die Excel-Datei enthält keine Vornamen.")
    return result


def generate_project_report(db: Database, project_id: int, destination: Path) -> None:
    project = db.one(
        """SELECT p.*, c.name class_name, u.first_name lead_name
           FROM projects p JOIN classes c ON c.id=p.class_id
           JOIN users u ON u.id=p.project_lead_id WHERE p.id=?""",
        (project_id,),
    )
    if not project:
        raise ValueError("Projekt nicht gefunden")
    teams = db.all("SELECT * FROM teams WHERE project_id=? AND status='active' ORDER BY name", (project_id,))
    phases = db.all("SELECT * FROM phases WHERE project_id=? ORDER BY sort_order,id", (project_id,))
    tasks = db.all(
        """SELECT t.*, tm.name team_name, ph.name phase_name, s.label status_label
           FROM tasks t LEFT JOIN teams tm ON tm.id=t.team_id
           LEFT JOIN phases ph ON ph.id=t.phase_id
           LEFT JOIN task_statuses s ON s.project_id=t.project_id AND s.key=t.status_key
           WHERE t.project_id=? ORDER BY t.created_at""",
        (project_id,),
    )
    events = db.all(
        """SELECT e.*, u.first_name actor_name FROM project_events e
           LEFT JOIN users u ON u.id=e.actor_id WHERE e.project_id=? ORDER BY e.created_at""",
        (project_id,),
    )
    comments = db.all(
        """SELECT c.*, u.first_name author_name, t.title task_title FROM comments c
           JOIN users u ON u.id=c.author_id JOIN tasks t ON t.id=c.task_id
           WHERE t.project_id=? AND c.hidden_at IS NULL ORDER BY c.created_at""",
        (project_id,),
    )
    uploads = db.all("SELECT * FROM uploads WHERE project_id=? ORDER BY created_at", (project_id,))

    destination.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(str(destination), pagesize=A4, rightMargin=18*mm, leftMargin=18*mm, topMargin=18*mm, bottomMargin=18*mm)
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="PKTitle", parent=styles["Title"], textColor=NAVY, alignment=TA_CENTER, spaceAfter=10*mm))
    styles.add(ParagraphStyle(name="PKH1", parent=styles["Heading1"], textColor=NAVY, spaceBefore=5*mm))
    styles.add(ParagraphStyle(name="PKSmall", parent=styles["BodyText"], fontSize=8, leading=10))
    story: list[Any] = [Paragraph("ProjektKontor", styles["PKTitle"]), Paragraph(safe_paragraph(project["title"]), styles["Title"])]
    story += [Spacer(1, 8*mm), Paragraph(safe_paragraph(project["description"] or "Ohne Projektbeschreibung"), styles["BodyText"])]
    story += [Spacer(1, 8*mm), Table([
        ["Klasse", project["class_name"]], ["Gesamtprojektleitung", project["lead_name"]],
        ["Zeitraum", (
            f"{report_date(project['start_at'], bool(project.get('start_has_time', 1)))} – {report_date(project['end_at'], bool(project.get('end_has_time', 1)))}" if project.get("has_start", 1) and project.get("has_end", 1)
            else f"ab {report_date(project['start_at'], bool(project.get('start_has_time', 1)))}" if project.get("has_start", 1)
            else f"bis {report_date(project['end_at'], bool(project.get('end_has_time', 1)))}" if project.get("has_end", 1)
            else "Nicht festgelegt"
        )], ["Bericht erstellt", datetime.now().strftime("%d.%m.%Y %H:%M")]
    ], colWidths=[50*mm, 110*mm], style=TableStyle([("BACKGROUND",(0,0),(0,-1),LIGHT),("TEXTCOLOR",(0,0),(0,-1),NAVY),("GRID",(0,0),(-1,-1),0.4,LIGHT),("VALIGN",(0,0),(-1,-1),"TOP")]))]

    story += [PageBreak(), Paragraph("Projektorganisation", styles["PKH1"])]
    for team in teams:
        members = db.all(
            """SELECT u.first_name, tm.is_lead, tm.business_role FROM team_members tm
               JOIN users u ON u.id=tm.user_id WHERE tm.team_id=? ORDER BY tm.is_lead DESC,u.first_name""",
            (team["id"],),
        )
        parts = []
        for member in members:
            label = member["first_name"]
            if member["is_lead"]:
                label += " (Teamleitung)"
            if member["business_role"]:
                label += f" – {member['business_role']}"
            parts.append(label)
        member_text = ", ".join(parts) or "Noch unbesetzt"
        story += [Paragraph(safe_paragraph(team["name"]), styles["Heading2"]), Paragraph(safe_paragraph(team["responsibility"] or "Keine Zuständigkeit beschrieben"), styles["BodyText"]), Paragraph(safe_paragraph(member_text), styles["PKSmall"])]

    story += [Paragraph("Projektphasen", styles["PKH1"])]
    if phases:
        phase_data = [["Phase", "Zeitraum", "Erwartetes Ergebnis"]] + [[p["name"], f"{p['start_at']} – {p['end_at']}", p["expected_result"]] for p in phases]
        story.append(Table(phase_data, repeatRows=1, colWidths=[42*mm,55*mm,63*mm], style=TableStyle([("BACKGROUND",(0,0),(-1,0),NAVY),("TEXTCOLOR",(0,0),(-1,0),colors.white),("GRID",(0,0),(-1,-1),0.4,LIGHT),("VALIGN",(0,0),(-1,-1),"TOP"),("FONTSIZE",(0,0),(-1,-1),8)])))

    story += [Paragraph("Aufgaben und Ergebnisse", styles["PKH1"])]
    for task in tasks:
        assignees = db.all("SELECT u.first_name FROM task_assignees a JOIN users u ON u.id=a.user_id WHERE a.task_id=? ORDER BY u.first_name", (task["id"],))
        story += [Paragraph(safe_paragraph(task["title"]), styles["Heading2"]), Paragraph(safe_paragraph(
            f"Team: {task['team_name'] or 'ohne Team'} · Verantwortlich: {', '.join(a['first_name'] for a in assignees) or 'noch zu verteilen'} · Status: {task['status_label'] or task['status_key']}"), styles["PKSmall"])]
        if task["description"]:
            story.append(Paragraph(safe_paragraph(task["description"]), styles["BodyText"]))
        if task["result_text"]:
            story += [Paragraph("Ergebnis", styles["Heading3"]), Paragraph(safe_paragraph(task["result_text"]), styles["BodyText"])]

    story += [Paragraph("Kommentare", styles["PKH1"])]
    for comment in comments:
        edited = " · bearbeitet" if comment["edited_at"] else ""
        story.append(Paragraph(f"<b>{safe_paragraph(comment['author_name'])}</b> zu „{safe_paragraph(comment['task_title'])}“ · {safe_paragraph(comment['created_at'])}{edited}<br/>{safe_paragraph(comment['body'])}", styles["BodyText"]))

    story += [Paragraph("Projektverlauf", styles["PKH1"])]
    for event in events:
        story.append(Paragraph(safe_paragraph(f"{event['created_at']} · {event['actor_name'] or 'System'}: {event['message']}"), styles["PKSmall"]))

    story += [Paragraph("Dateiverzeichnis", styles["PKH1"])]
    if uploads:
        story.append(Table([["Datei","Typ","Größe"]] + [[u["original_name"],u["media_type"],f"{u['size_bytes']/1024:.0f} KB"] for u in uploads], repeatRows=1, colWidths=[90*mm,45*mm,25*mm], style=TableStyle([("BACKGROUND",(0,0),(-1,0),NAVY),("TEXTCOLOR",(0,0),(-1,0),colors.white),("GRID",(0,0),(-1,-1),0.4,LIGHT),("FONTSIZE",(0,0),(-1,-1),8)])))
        image_uploads=[upload for upload in uploads if str(upload["media_type"]).startswith("image/")]
        if image_uploads:
            story.append(Paragraph("Bildanhänge", styles["PKH1"]))
            for upload in image_uploads:
                image_path=db.path.parent/"uploads"/upload["stored_name"]
                if image_path.is_file():
                    try:
                        preview=Image(str(image_path));preview._restrictSize(155*mm,95*mm)
                        story += [Paragraph(safe_paragraph(upload["original_name"]),styles["Heading3"]),preview,Spacer(1,4*mm)]
                    except Exception:
                        story.append(Paragraph(safe_paragraph(f"{upload['original_name']} (Vorschau nicht verfügbar)"),styles["BodyText"]))
    else:
        story.append(Paragraph("Keine Uploads vorhanden.", styles["BodyText"]))

    doc.build(story)
