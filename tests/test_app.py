from __future__ import annotations

import base64
import io
import json
import os
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openpyxl import Workbook, load_workbook

from projektkontor.backup import create_backup
from projektkontor.config import Config
from projektkontor.server import App


class Client:
    def __init__(self, app: App, auto_privacy: bool = True):
        self.app = app
        self.auto_privacy = auto_privacy
        self.cookie = ""
        self.csrf = ""

    def request(self, method: str, path: str, body=None):
        raw = json.dumps(body or {}).encode() if body is not None else b""
        captured = {}

        def start_response(status, headers):
            captured["status"] = int(status.split()[0])
            captured["headers"] = dict(headers)

        environ = {
            "REQUEST_METHOD": method,
            "PATH_INFO": path,
            "QUERY_STRING": "",
            "CONTENT_LENGTH": str(len(raw)),
            "CONTENT_TYPE": "application/json",
            "HTTP_COOKIE": self.cookie,
            "HTTP_X_CSRF_TOKEN": self.csrf,
            "wsgi.input": io.BytesIO(raw),
        }
        payload = b"".join(self.app(environ, start_response))
        if "Set-Cookie" in captured["headers"]:
            self.cookie = captured["headers"]["Set-Cookie"].split(";", 1)[0]
        data = json.loads(payload) if captured["headers"].get("Content-Type", "").startswith("application/json") else payload
        if isinstance(data, dict) and data.get("csrf"):
            self.csrf = data["csrf"]
        if self.auto_privacy and isinstance(data, dict) and data.get("privacy_required"):
            return self.request("POST", "/api/privacy/accept", {
                "privacy_token": data["privacy_token"], "privacy_version": data["privacy_version"]
            })
        return captured["status"], data, captured["headers"]


class AppFlowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        root.joinpath("uploads").mkdir()
        root.joinpath("reports").mkdir()
        self.app = App(Config("127.0.0.1", 8080, root, 25 * 1024 * 1024))
        self.client = Client(self.app)

    def tearDown(self):
        self.temp.cleanup()

    def test_teacher_class_students_project_and_task(self):
        status, setup, _ = self.client.request("POST", "/api/setup", {
            "first_name": "Jeroen", "username": "lehrkraft", "password": "sicheres-testkennwort"
        })
        self.assertEqual(status, 200)

        status, klass, _ = self.client.request("POST", "/api/classes", {"name": "GH24"})
        self.assertEqual(status, 200)
        class_id = klass["id"]

        status, imported, _ = self.client.request("POST", f"/api/classes/{class_id}/import", {
            "rows": [{"first_name": "Lena"}, {"first_name": "Lena"}, {"first_name": "Yusuf"}]
        })
        self.assertEqual(status, 200)
        self.assertEqual([row["username"] for row in imported["created"]], ["lena01", "lena02", "yusuf01"])
        lead_id = imported["created"][0]["id"]
        member_ids = [row["id"] for row in imported["created"]]

        status, project, _ = self.client.request("POST", "/api/projects", {
            "title": "Messeprojekt", "class_id": class_id, "description": "Eine Messe planen",
            "start_at": "2026-07-16T08:00", "end_at": "2026-07-23T12:00",
            "project_lead_id": lead_id, "member_ids": member_ids,
        })
        self.assertEqual(status, 200)
        project_id = project["id"]

        _, later_import, _ = self.client.request("POST", f"/api/classes/{class_id}/import", {
            "rows": [{"first_name": "Nora"}]
        })
        nora_id = later_import["created"][0]["id"]
        _, before_team, _ = self.client.request("GET", f"/api/projects/{project_id}")
        self.assertIn(nora_id, [student["id"] for student in before_team["class_students"]])
        self.assertNotIn(nora_id, [member["id"] for member in before_team["members"]])
        nora_before = next(student for student in before_team["class_students"] if student["id"] == nora_id)
        self.assertIsNone(nora_before["team_id"])

        status, team, _ = self.client.request("POST", f"/api/projects/{project_id}/teams", {
            "name": "Marketing", "responsibility": "Kommunikation", "color": "#2563EB",
            "member_ids": [lead_id], "lead_ids": [lead_id],
        })
        self.assertEqual(status, 200)
        team_id = team["id"]
        stored_member = self.app.db.one("SELECT is_lead FROM team_members WHERE team_id=? AND user_id=?", (team_id, lead_id))
        self.assertEqual(stored_member["is_lead"], 1)

        status, second_team, _ = self.client.request("POST", f"/api/projects/{project_id}/teams", {
            "name": "Vertrieb", "responsibility": "Kundschaft", "color": "#16A34A",
            "member_ids": [nora_id], "lead_ids": [nora_id],
        })
        self.assertEqual(status, 200)
        self.assertIsNotNone(self.app.db.one("SELECT 1 ok FROM project_members WHERE project_id=? AND user_id=?", (project_id, nora_id)))
        _, after_team, _ = self.client.request("GET", f"/api/projects/{project_id}")
        nora_after = next(student for student in after_team["class_students"] if student["id"] == nora_id)
        self.assertEqual(nora_after["team_id"], second_team["id"])
        self.assertEqual(nora_after["is_lead"], 1)

        status, task, _ = self.client.request("POST", f"/api/projects/{project_id}/tasks", {
            "title": "Flyer abstimmen", "team_ids": [team_id], "weight": 2,
            "start_at": "2026-07-16T09:00", "due_at": "2026-07-16T11:30"
        })
        self.assertEqual(status, 200)
        task_id = task["ids"][0]
        self.client.request("POST", f"/api/tasks/{task_id}/assignees", {"user_ids": [lead_id]})

        status, all_users, _ = self.client.request("GET", "/api/users")
        self.assertEqual(status, 200)
        lead_context = next(account for account in all_users if account["id"] == lead_id)
        self.assertEqual(lead_context["class_name"], "GH24")
        self.assertEqual(lead_context["projects"][0]["title"], "Messeprojekt")
        self.assertEqual(lead_context["teams"][0]["name"], "Marketing")
        self.assertEqual(lead_context["teams"][0]["is_lead"], 1)
        self.assertEqual(lead_context["open_task_count"], 1)
        self.assertTrue(lead_context["access_code"])

        status, detail, _ = self.client.request("GET", f"/api/projects/{project_id}")
        self.assertEqual(status, 200)
        self.assertEqual(detail["tasks"][0]["assignees"][0]["first_name"], "Lena")
        self.assertEqual(detail["tasks"][0]["due_at"], "2026-07-16T11:30")
        self.assertEqual(detail["progress"]["overall"], 0)

        status, duplicate_color, _ = self.client.request("POST", f"/api/projects/{project_id}/teams", {
            "name": "Einkauf", "responsibility": "Beschaffung", "color": "#2563EB"
        })
        self.assertEqual(status, 409)
        self.assertIn("bereits verwendet", duplicate_color["error"])

        status, duplicate_name, _ = self.client.request("POST", f"/api/projects/{project_id}/teams", {
            "name": "marketing", "responsibility": "Doppelt", "color": "#DC2626"
        })
        self.assertEqual(status, 409)
        self.assertIn("Teamname", duplicate_name["error"])

        status, comment, _ = self.client.request("POST", f"/api/tasks/{task_id}/comments", {"body": "Der Entwurf ist bereit."})
        self.assertEqual(status, 200)
        status, report, _ = self.client.request("POST", f"/api/projects/{project_id}/report", {})
        self.assertEqual(status, 200)
        report_row = self.app.db.one("SELECT * FROM reports WHERE id=?", (report["id"],))
        self.assertTrue((self.app.config.report_dir / report_row["stored_name"]).is_file())

        status, template, _ = self.client.request("POST", f"/api/projects/{project_id}/template", {"name": "Messevorlage"})
        self.assertEqual(status, 200)
        status, copied, _ = self.client.request("POST", "/api/projects", {
            "title": "Messeprojekt 2", "class_id": class_id, "description": "Kopie",
            "start_at": "2026-08-01T08:00", "end_at": "2026-08-08T12:00",
            "project_lead_id": lead_id, "member_ids": member_ids, "template_id": template["id"],
        })
        self.assertEqual(status, 200)
        copied_marketing = self.app.db.one("SELECT id,responsibility,color FROM teams WHERE project_id=? AND name='Marketing'", (copied["id"],))
        self.assertIsNotNone(copied_marketing)
        self.assertEqual(copied_marketing["responsibility"], "Kommunikation")
        self.assertEqual(copied_marketing["color"], "#2563EB")
        self.assertEqual(self.app.db.one("SELECT COUNT(*) count FROM team_members WHERE team_id=?", (copied_marketing["id"],))["count"], 0)
        self.assertIsNotNone(self.app.db.one("SELECT id FROM teams WHERE project_id=? AND name='Vertrieb'", (copied["id"],)))
        self.assertIsNotNone(self.app.db.one("SELECT id FROM tasks WHERE project_id=? AND title='Flyer abstimmen'", (copied["id"],)))

        status, archived, _ = self.client.request("POST", f"/api/projects/{project_id}/archive", {"confirm_cleanup": True})
        self.assertEqual(status, 200)
        self.assertIsNone(self.app.db.one("SELECT id FROM tasks WHERE project_id=?", (project_id,)))
        self.assertIsNotNone(self.app.db.one("SELECT id FROM reports WHERE project_id=?", (project_id,)))

    def test_student_codes_are_readable_to_teacher(self):
        self.client.request("POST", "/api/setup", {
            "first_name": "Jeroen", "username": "lehrkraft", "password": "sicheres-testkennwort"
        })
        _, klass, _ = self.client.request("POST", "/api/classes", {"name": "KB25"})
        _, imported, _ = self.client.request("POST", f"/api/classes/{klass['id']}/import", {"rows": [{"first_name": "Mia"}]})
        _, users, _ = self.client.request("GET", f"/api/classes/{klass['id']}/users")
        self.assertEqual(users[0]["access_code"], imported["created"][0]["access_code"])

    def test_teacher_can_create_student_account_manually(self):
        self.client.request("POST", "/api/setup", {
            "first_name": "Jeroen", "username": "lehrkraft", "password": "sicheres-testkennwort"
        })
        _, klass, _ = self.client.request("POST", "/api/classes", {"name": "KB26"})
        status, first, _ = self.client.request("POST", f"/api/classes/{klass['id']}/users", {"first_name": "Lena"})
        self.assertEqual(status, 200)
        self.assertEqual(first["username"], "lena01")
        self.assertEqual(len(first["access_code"]), 8)
        status, second, _ = self.client.request("POST", f"/api/classes/{klass['id']}/users", {
            "first_name": "Lena", "username": "einkauf01", "access_code": "KONTOR-2026"
        })
        self.assertEqual(status, 200)
        self.assertEqual(second["username"], "einkauf01")
        self.assertEqual(second["access_code"], "KONTOR-2026")
        _, users, _ = self.client.request("GET", f"/api/classes/{klass['id']}/users")
        self.assertEqual(len(users), 2)

        status, duplicate_class, _ = self.client.request("POST", "/api/classes", {"name": "kb26"})
        self.assertEqual(status, 409)
        self.assertEqual(duplicate_class["error"], "Diese Klasse existiert bereits.")

        _, target_class, _ = self.client.request("POST", "/api/classes", {"name": "KB27"})
        status, renamed_class, _ = self.client.request("PATCH", f"/api/classes/{target_class['id']}", {"name": "KB28"})
        self.assertEqual(status, 200)
        self.assertEqual(renamed_class["name"], "KB28")
        status, _, _ = self.client.request("PATCH", f"/api/users/{first['id']}", {"class_id": target_class["id"]})
        self.assertEqual(status, 200)
        _, original_users, _ = self.client.request("GET", f"/api/classes/{klass['id']}/users")
        _, target_users, _ = self.client.request("GET", f"/api/classes/{target_class['id']}/users")
        self.assertNotIn(first["id"], [account["id"] for account in original_users])
        self.assertIn(first["id"], [account["id"] for account in target_users])

    def test_student_can_login_with_generated_access_code(self):
        self.client.request("POST", "/api/setup", {
            "first_name": "Jeroen", "username": "lehrkraft", "password": "sicheres-testkennwort"
        })
        _, klass, _ = self.client.request("POST", "/api/classes", {"name": "KB29"})
        _, account, _ = self.client.request("POST", f"/api/classes/{klass['id']}/users", {"first_name": "Markus"})
        status, _, _ = self.client.request("POST", "/api/logout", {})
        self.assertEqual(status, 200)
        status, login, login_headers = self.client.request("POST", "/api/login", {
            "username": account["username"].upper(),
            "password": f"  {account['access_code'].lower()}  ",
        })
        self.assertEqual(status, 200)
        self.assertTrue(login["ok"])
        self.assertIn("Max-Age=21600", login_headers["Set-Cookie"])
        status, bootstrap, _ = self.client.request("GET", "/api/bootstrap")
        self.assertEqual(status, 200)
        self.assertEqual(bootstrap["user"]["id"], account["id"])
        self.assertEqual(bootstrap["user"]["role"], "student")
        session = self.app.db.one("SELECT created_at,expires_at FROM sessions WHERE user_id=? ORDER BY id DESC LIMIT 1", (account["id"],))
        session_duration = __import__("datetime").datetime.fromisoformat(session["expires_at"]) - __import__("datetime").datetime.fromisoformat(session["created_at"])
        self.assertEqual(session_duration.total_seconds(), 6 * 60 * 60)

        second_device = Client(self.app)
        status, _, _ = second_device.request("POST", "/api/login", {
            "username": account["username"], "password": account["access_code"]
        })
        self.assertEqual(status, 200)
        self.assertEqual(self.app.db.one("SELECT COUNT(*) count FROM sessions WHERE user_id=?", (account["id"],))["count"], 1)
        status, first_device_bootstrap, _ = self.client.request("GET", "/api/bootstrap")
        self.assertEqual(status, 200)
        self.assertIsNone(first_device_bootstrap["user"])
        status, second_device_bootstrap, _ = second_device.request("GET", "/api/bootstrap")
        self.assertEqual(status, 200)
        self.assertEqual(second_device_bootstrap["user"]["id"], account["id"])

    def test_student_roles_deadline_requests_and_self_organization(self):
        teacher = self.client
        teacher.request("POST", "/api/setup", {
            "first_name": "Jeroen", "username": "lehrkraft", "password": "sicheres-testkennwort"
        })
        _, klass, _ = teacher.request("POST", "/api/classes", {"name": "WB30"})
        _, imported, _ = teacher.request("POST", f"/api/classes/{klass['id']}/import", {
            "rows": [{"first_name": "Markus"}, {"first_name": "Lena"}]
        })
        markus, lena = imported["created"]
        _, project, _ = teacher.request("POST", "/api/projects", {
            "title": "Rollenprojekt", "class_id": klass["id"], "has_start": False, "has_end": False,
            "project_lead_id": lena["id"], "member_ids": [markus["id"], lena["id"]],
        })
        _, team, _ = teacher.request("POST", f"/api/projects/{project['id']}/teams", {
            "name": "Einkauf", "color": "#2563EB", "member_ids": [markus["id"]], "lead_ids": []
        })
        _, task, _ = teacher.request("POST", f"/api/projects/{project['id']}/tasks", {
            "title": "Angebote prüfen", "team_ids": [team["id"]], "due_at": "2026-08-01"
        })
        task_id = task["ids"][0]
        teacher.request("POST", f"/api/tasks/{task_id}/assignees", {"user_ids": [markus["id"]]})

        student = Client(self.app)
        status, _, _ = student.request("POST", "/api/login", {
            "username": markus["username"], "password": markus["access_code"]
        })
        self.assertEqual(status, 200)
        status, forbidden_team, _ = student.request("POST", f"/api/projects/{project['id']}/teams", {
            "name": "Logistik", "color": "#16A34A", "member_ids": [], "lead_ids": []
        })
        self.assertEqual(status, 403)
        self.assertIn("deaktiviert", forbidden_team["error"])
        status, _, _ = student.request("POST", f"/api/tasks/{task_id}/assignees", {"user_ids": [markus["id"]]})
        self.assertEqual(status, 403)

        teacher.request("PATCH", f"/api/projects/{project['id']}", {"allow_student_organization": True})
        status, proposed_team, _ = student.request("POST", f"/api/projects/{project['id']}/teams", {
            "name": "Logistik", "color": "#16A34A", "member_ids": [], "lead_ids": []
        })
        self.assertEqual(status, 200)
        self.assertEqual(proposed_team["status"], "proposed")

        status, request, _ = student.request("POST", f"/api/tasks/{task_id}/deadline-requests", {
            "requested_due_at": "2026-08-05T12:00", "reason": "Lieferant antwortet später"
        })
        self.assertEqual(status, 200)
        status, duplicate_request, _ = student.request("POST", f"/api/tasks/{task_id}/deadline-requests", {
            "requested_due_at": "2026-08-06T12:00", "reason": "Noch später"
        })
        self.assertEqual(status, 409)
        status, _, _ = teacher.request("PATCH", f"/api/deadline-requests/{request['id']}", {"decision": "approved"})
        self.assertEqual(status, 200)
        self.assertEqual(self.app.db.one("SELECT due_at FROM tasks WHERE id=?", (task_id,))["due_at"], "2026-08-05T12:00")

        _, comment, _ = student.request("POST", f"/api/tasks/{task_id}/comments", {"body": "Erste Fassung"})
        status, _, _ = student.request("PATCH", f"/api/comments/{comment['id']}", {"body": "Überarbeitete Fassung"})
        self.assertEqual(status, 200)
        stored = self.app.db.one("SELECT body,edited_at FROM comments WHERE id=?", (comment["id"],))
        self.assertEqual(stored["body"], "Überarbeitete Fassung")
        self.assertTrue(stored["edited_at"])
        status, _, _ = teacher.request("DELETE", f"/api/comments/{comment['id']}", {})
        self.assertEqual(status, 200)

        pdf_data = base64.b64encode(b"%PDF-test-project-file").decode()
        status, project_upload, _ = teacher.request("POST", f"/api/projects/{project['id']}/uploads", {
            "name": "Projektauftrag.pdf", "media_type": "application/pdf", "file_base64": pdf_data
        })
        self.assertEqual(status, 200)
        status, team_upload, _ = student.request("POST", f"/api/projects/{project['id']}/uploads", {
            "name": "Teamdatei.pdf", "media_type": "application/pdf", "file_base64": pdf_data,
            "team_id": team["id"],
        })
        self.assertEqual(status, 200)
        self.assertNotEqual(project_upload["id"], team_upload["id"])

        _, outsider, _ = teacher.request("POST", f"/api/classes/{klass['id']}/users", {"first_name": "Nora"})
        outsider_client = Client(self.app)
        outsider_client.request("POST", "/api/login", {"username": outsider["username"], "password": outsider["access_code"]})
        status, _, _ = outsider_client.request("GET", f"/api/projects/{project['id']}")
        self.assertEqual(status, 403)

    def test_excel_duplicate_rows_require_confirmation(self):
        self.client.request("POST", "/api/setup", {
            "first_name": "Jeroen", "username": "lehrkraft", "password": "sicheres-testkennwort"
        })
        _, klass, _ = self.client.request("POST", "/api/classes", {"name": "WB31"})
        rows = [{"first_name": "Markus", "duplicate": True}]
        status, result, _ = self.client.request("POST", f"/api/classes/{klass['id']}/import", {"rows": rows})
        self.assertEqual(status, 409)
        self.assertIn("Namensdubletten", result["error"])
        status, result, _ = self.client.request("POST", f"/api/classes/{klass['id']}/import", {
            "rows": rows, "confirm_duplicates": True
        })
        self.assertEqual(status, 200)
        self.assertEqual(len(result["created"]), 1)

    def test_excel_import_can_create_and_assign_multiple_classes(self):
        self.client.request("POST", "/api/setup", {
            "first_name": "Jeroen", "username": "lehrkraft", "password": "sicheres-testkennwort"
        })
        _, klass, _ = self.client.request("POST", "/api/classes", {"name": "WB31"})

        status, template, _ = self.client.request("GET", f"/api/classes/{klass['id']}/template")
        self.assertEqual(status, 200)
        template_book = load_workbook(io.BytesIO(template))
        self.assertEqual([cell.value for cell in template_book.active[1]], ["Vorname", "Klasse"])
        self.assertEqual(template_book.active["B2"].value, "WB31")

        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["Vorname", "Klasse"])
        sheet.append(["Lena", ""])
        sheet.append(["Yusuf", "WB32"])
        buffer = io.BytesIO()
        workbook.save(buffer)
        status, preview, _ = self.client.request("POST", f"/api/classes/{klass['id']}/import-preview", {
            "file_base64": base64.b64encode(buffer.getvalue()).decode()
        })
        self.assertEqual(status, 200)
        self.assertEqual([row["class_name"] for row in preview["rows"]], ["WB31", "WB32"])
        self.assertFalse(preview["rows"][0]["new_class"])
        self.assertTrue(preview["rows"][1]["new_class"])

        status, result, _ = self.client.request("POST", f"/api/classes/{klass['id']}/import", {
            "rows": preview["rows"]
        })
        self.assertEqual(status, 200)
        self.assertEqual(result["created_classes"], ["WB32"])
        self.assertEqual([row["class_name"] for row in result["created"]], ["WB31", "WB32"])
        wb32 = self.app.db.one("SELECT id FROM classes WHERE name='WB32'")
        self.assertIsNotNone(wb32)
        self.assertEqual(self.app.db.one("SELECT first_name FROM users WHERE class_id=?", (wb32["id"],))["first_name"], "Yusuf")

    def test_global_excel_import_works_without_an_existing_class(self):
        self.client.request("POST", "/api/setup", {
            "first_name": "Jeroen", "username": "lehrkraft", "password": "sicheres-testkennwort"
        })
        status, template, _ = self.client.request("GET", "/api/users/import-template")
        self.assertEqual(status, 200)
        self.assertEqual([cell.value for cell in load_workbook(io.BytesIO(template)).active[1]], ["Vorname", "Klasse"])

        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["Vorname", "Klasse"])
        sheet.append(["Mia", "GH24"])
        sheet.append(["Noah", "GH25"])
        buffer = io.BytesIO()
        workbook.save(buffer)
        status, preview, _ = self.client.request("POST", "/api/users/import-preview", {
            "file_base64": base64.b64encode(buffer.getvalue()).decode()
        })
        self.assertEqual(status, 200)
        self.assertTrue(all(row["new_class"] for row in preview["rows"]))
        status, result, _ = self.client.request("POST", "/api/users/import", {"rows": preview["rows"]})
        self.assertEqual(status, 200)
        self.assertEqual(result["created_classes"], ["GH24", "GH25"])

        missing_class_book = Workbook()
        missing_class_sheet = missing_class_book.active
        missing_class_sheet.append(["Vorname", "Klasse"])
        missing_class_sheet.append(["Lena", ""])
        missing_class_buffer = io.BytesIO()
        missing_class_book.save(missing_class_buffer)
        status, preview, _ = self.client.request("POST", "/api/users/import-preview", {
            "file_base64": base64.b64encode(missing_class_buffer.getvalue()).decode()
        })
        self.assertEqual(status, 200)
        self.assertIn("Klasse angegeben", preview["rows"][0]["error"])

    def test_class_name_validation_explains_empty_values_and_spaces(self):
        self.client.request("POST", "/api/setup", {
            "first_name": "Jeroen", "username": "lehrkraft", "password": "sicheres-testkennwort"
        })
        status, result, _ = self.client.request("POST", "/api/classes", {"name": "   "})
        self.assertEqual(status, 400)
        self.assertIn("nicht leer", result["error"])
        status, result, _ = self.client.request("POST", "/api/classes", {"name": "Test Klasse"})
        self.assertEqual(status, 400)
        self.assertIn("keine Leerzeichen", result["error"])

    def test_backup_contains_database_uploads_and_reports(self):
        data_dir = Path(self.temp.name) / "backup-source"
        backup_dir = Path(self.temp.name) / "backup-target"
        data_dir.mkdir(); (data_dir / "uploads").mkdir(); (data_dir / "reports").mkdir()
        app = App(Config("127.0.0.1", 8080, data_dir, 25 * 1024 * 1024))
        app.db.execute("INSERT INTO classes(name,created_at) VALUES(?,?)", ("BK32", "2026-07-16T12:00:00+00:00"))
        (data_dir / "uploads" / "test.pdf").write_bytes(b"%PDF-test")
        (data_dir / "reports" / "bericht.pdf").write_bytes(b"%PDF-report")
        with patch.dict(os.environ, {"PK_DATA_DIR": str(data_dir), "PK_BACKUP_DIR": str(backup_dir)}):
            archive = create_backup()
        self.assertTrue(archive.is_file())
        self.assertEqual(archive.stat().st_mode & 0o777, 0o600)
        with tarfile.open(archive, "r:gz") as saved:
            names = set(saved.getnames())
        self.assertIn("projektkontor/projektkontor.sqlite3", names)
        self.assertIn("projektkontor/uploads/test.pdf", names)
        self.assertIn("projektkontor/reports/bericht.pdf", names)

    def test_project_can_be_created_without_start_and_end(self):
        self.client.request("POST", "/api/setup", {
            "first_name": "Jeroen", "username": "lehrkraft", "password": "sicheres-testkennwort"
        })
        _, klass, _ = self.client.request("POST", "/api/classes", {"name": "GH26"})
        _, imported, _ = self.client.request("POST", f"/api/classes/{klass['id']}/import", {"rows": [{"first_name": "Mia"}]})
        lead_id = imported["created"][0]["id"]
        status, project, _ = self.client.request("POST", "/api/projects", {
            "title": "Projekt ohne Zeitraum", "class_id": klass["id"], "description": "Offen",
            "has_start": False, "has_end": False, "start_at": None, "end_at": None,
            "project_lead_id": lead_id, "member_ids": [lead_id],
        })
        self.assertEqual(status, 200)
        status, detail, _ = self.client.request("GET", f"/api/projects/{project['id']}")
        self.assertEqual(status, 200)
        self.assertIsNone(detail["start_at"])
        self.assertIsNone(detail["end_at"])
        self.assertEqual(detail["has_start"], 0)
        self.assertEqual(detail["has_end"], 0)

        status, dated, _ = self.client.request("POST", "/api/projects", {
            "title": "Projekt nur mit Datum", "class_id": klass["id"], "description": "Ohne Uhrzeit",
            "has_start": True, "has_end": True, "start_has_time": False, "end_has_time": False,
            "start_at": "2026-09-01", "end_at": "2026-09-10",
            "project_lead_id": lead_id, "member_ids": [lead_id],
        })
        self.assertEqual(status, 200)
        _, dated_detail, _ = self.client.request("GET", f"/api/projects/{dated['id']}")
        self.assertEqual(dated_detail["start_at"], "2026-09-01")
        self.assertEqual(dated_detail["end_at"], "2026-09-10")
        self.assertEqual(dated_detail["start_has_time"], 0)

        status, timed, _ = self.client.request("POST", "/api/projects", {
            "title": "Projekt mit Uhrzeit", "class_id": klass["id"], "description": "Mit Uhrzeit",
            "has_start": True, "has_end": True, "start_has_time": True, "end_has_time": True,
            "start_at": "2026-09-01T08:15", "end_at": "2026-09-10T13:30",
            "project_lead_id": lead_id, "member_ids": [lead_id],
        })
        self.assertEqual(status, 200)
        _, timed_detail, _ = self.client.request("GET", f"/api/projects/{timed['id']}")
        self.assertEqual(timed_detail["start_at"], "2026-09-01T08:15")
        self.assertEqual(timed_detail["end_at"], "2026-09-10T13:30")
        self.assertEqual(timed_detail["start_has_time"], 1)

    def test_date_only_upload_deadline_defaults_to_end_of_day(self):
        self.client.request("POST", "/api/setup", {
            "first_name": "Jeroen", "username": "lehrkraft", "password": "sicheres-testkennwort"
        })
        _, klass, _ = self.client.request("POST", "/api/classes", {"name": "GH27"})
        _, imported, _ = self.client.request("POST", f"/api/classes/{klass['id']}/import", {"rows": [{"first_name": "Mia"}]})
        lead_id = imported["created"][0]["id"]
        _, project, _ = self.client.request("POST", "/api/projects", {
            "title": "Fristentest", "class_id": klass["id"], "has_start": False, "has_end": False,
            "project_lead_id": lead_id, "member_ids": [lead_id],
        })
        status, task, _ = self.client.request("POST", f"/api/projects/{project['id']}/tasks", {
            "title": "Upload einreichen", "due_at": "2026-10-05"
        })
        self.assertEqual(status, 200)
        _, detail, _ = self.client.request("GET", f"/api/projects/{project['id']}")
        created = next(item for item in detail["tasks"] if item["id"] == task["ids"][0])
        self.assertEqual(created["due_at"], "2026-10-05T23:59")

    def test_teacher_can_update_own_account_securely(self):
        self.client.request("POST", "/api/setup", {
            "first_name": "Jeroen", "username": "lehrkraft", "password": "sicheres-testkennwort"
        })
        status, result, _ = self.client.request("PATCH", "/api/account", {"first_name": "Herr Müller"})
        self.assertEqual(status, 200)
        self.assertEqual(result["user"]["first_name"], "Herr Müller")
        status, _, _ = self.client.request("PATCH", "/api/account", {"username": "geschaeftsfuehrung"})
        self.assertEqual(status, 403)
        status, result, _ = self.client.request("PATCH", "/api/account", {
            "username": "geschaeftsfuehrung", "current_password": "sicheres-testkennwort"
        })
        self.assertEqual(status, 200)
        self.assertEqual(result["user"]["username"], "geschaeftsfuehrung")
        login = Client(self.app)
        status, _, _ = login.request("POST", "/api/login", {
            "username": "geschaeftsfuehrung", "password": "sicheres-testkennwort"
        })
        self.assertEqual(status, 200)

    def test_security_headers_and_malformed_image_rejection(self):
        status, _, headers = self.client.request("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertIn("object-src 'none'", headers["Content-Security-Policy"])
        self.assertIn("noindex", headers["X-Robots-Tag"])
        status, app_script, _ = self.client.request("GET", "/app.js")
        self.assertEqual(status, 200)
        self.assertNotIn(b"onclick=", app_script)
        self.assertIn(b'href="#/admin/users"', app_script)
        self.assertIn(b'href="#/projects/${id}/${key}"', app_script)
        self.assertIn(b'id="add-user-from-overview"', app_script)
        self.assertIn(b"chooseManualAccountClassDialog", app_script)
        self.assertIn(b'<button class="button primary" id="add-user">Zugang manuell anlegen</button>', app_script)
        with self.assertRaises(Exception) as rejected:
            self.app.validated_upload({
                "name": "scheinbild.png", "media_type": "image/png",
                "file_base64": base64.b64encode(b"\x89PNG\r\n\x1a\nkein-echtes-bild").decode(),
            })
        self.assertIn("beschädigt", str(rejected.exception))
        status, robots, headers = self.client.request("GET", "/robots.txt")
        self.assertEqual(status, 200)
        self.assertIn(b"Allow: /", robots)
        self.assertIn(b"Disallow: /api/", robots)
        self.assertNotIn("X-Robots-Tag", headers)

    def test_public_landing_page_and_protected_login_are_separated(self):
        status, landing, headers = self.client.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"Berufliche Handlungssituationen vollst", landing)
        self.assertIn(b"webbasierte Arbeitsumgebung", landing)
        self.assertIn(b"Kanban-Board", landing)
        self.assertIn(b'id="contact-form"', landing)
        self.assertNotIn(b"mailto:", landing)
        self.assertNotIn(b"gmx.de", landing)
        self.assertIn(b"Praxiserfahrung", landing)
        self.assertIn(b"ERP-Umfeld", landing)
        self.assertIn(b"Pr\xc3\xbcfstatus", landing)
        self.assertIn(b"Der Projektbericht", landing)
        self.assertIn(b"Produkteinblick", landing)
        self.assertIn(b'href="#funktionsweise">Produktprinzip</a>', landing)
        self.assertIn(b"Nachhaltige Sch\xc3\xbclerfirma", landing)
        self.assertIn(b"Vom Projektauftrag bis zum gesicherten Ergebnis", landing)
        self.assertIn(b"VPS in Deutschland", landing)
        self.assertIn(b"Grunds\xc3\xa4tze der DSGVO", landing)
        self.assertIn(b'href="#sicherheit">Datenschutz</a>', landing)
        self.assertIn(b"Testzugang f\xc3\xbcr ProjektKontor anfragen", landing)
        self.assertIn(b"R\xc3\xbcckspr\xc3\xbcnge und \xc3\x9cberarbeitungen", landing)
        self.assertIn(b'class="process-lines"', landing)
        self.assertIn(b"L\xc3\xa4ngerfristiger<br>Unterrichtszusammenhang", landing)
        self.assertIn(b"Ergebnisse einer Einheit", landing)
        self.assertIn(b"\xc3\xbcbergeordnete Handlungsergebnis", landing)
        self.assertNotIn(b"Kein zus\xc3\xa4tzlicher Verwaltungsort", landing)
        self.assertIn(b'href="/login">Sch\xc3\xbclerlogin', landing)
        self.assertIn(b'<link rel="canonical" href="https://projektkontor.org/">', landing)
        self.assertNotIn(b'id="login-form"', landing)
        self.assertNotIn("X-Robots-Tag", headers)
        status, login, _ = self.client.request("GET", "/login")
        self.assertEqual(status, 200)
        self.assertIn(b'id="login-form"', login)
        self.assertIn(b'content="noindex,nofollow,noarchive,nosnippet"', login)
        status, sitemap, _ = self.client.request("GET", "/sitemap.xml")
        self.assertEqual(status, 200)
        self.assertIn(b"https://projektkontor.org/", sitemap)
        status, impressum, _ = self.client.request("GET", "/impressum")
        self.assertEqual(status, 200)
        self.assertIn(b"Noch nicht f", impressum)
        self.assertIn(b'content="noindex,nofollow,noarchive"', impressum)
        self.assertIn(b"Name beziehungsweise Firma des Anbieters", impressum)
        self.assertIn(b"BIBB-Leitbild", landing)
        self.assertIn(b"Berufliche Handlungsf\xc3\xa4higkeit", landing)
        self.assertIn(b"Fachliche, methodische, soziale und pers\xc3\xb6nliche", landing)
        self.assertIn(b"Projektlernen", landing)
        self.assertIn(b"Selbststeuerung", landing)
        self.assertNotIn(b'<p class="card-index">A</p>', landing)
        status, landing_script, _ = self.client.request("GET", "/landing.js")
        self.assertEqual(status, 200)
        self.assertIn(b"target.closest('section')", landing_script)
        self.assertIn(b"availableHeight", landing_script)
        self.assertIn(b"largestVisibleArea", landing_script)
        self.assertIn(b"visibleArea", landing_script)
        self.assertIn(b"history.pushState", landing_script)

    def test_contact_form_is_human_checked_and_recipient_stays_server_side(self):
        payload = {
            "name": "Erika Beispiel", "email": "erika@example.org", "subject": "Testzugang",
            "message": "Ich möchte ProjektKontor gerne kennenlernen.", "privacy_accepted": True,
            "turnstile_token": "verified-token", "website": "",
        }
        with patch.object(self.app, "_verify_turnstile") as verify, patch.object(self.app, "_send_contact_email") as send:
            status, result, _ = self.client.request("POST", "/api/contact", payload)
        self.assertEqual(status, 200)
        self.assertTrue(result["ok"])
        verify.assert_called_once_with("verified-token", "unknown")
        send.assert_called_once_with("Erika Beispiel", "erika@example.org", "Testzugang", "Ich möchte ProjektKontor gerne kennenlernen.")

        config = Config(
            "127.0.0.1", 8080, Path(self.temp.name), 25 * 1024 * 1024,
            smtp_host="smtp.example.org", smtp_username="sender@example.org",
            smtp_password="secret", smtp_sender="sender@example.org", contact_recipient="private@example.org",
        )
        mail_app = App(config)
        with patch("projektkontor.server.smtplib.SMTP_SSL") as smtp:
            smtp.return_value.__enter__.return_value = smtp.return_value
            mail_app._send_contact_email("Erika Beispiel", "erika@example.org", "Testzugang", "Nachricht")
            sent_mail = smtp.return_value.send_message.call_args.args[0]
        self.assertEqual(sent_mail["Subject"], "ProjektKontor Anfrage: Testzugang")
        self.assertEqual(sent_mail["To"], "private@example.org")
        self.assertEqual(sent_mail["Reply-To"], "Erika Beispiel <erika@example.org>")

    def test_first_login_requires_open_privacy_confirmation(self):
        client = Client(self.app, auto_privacy=False)
        status, pending, headers = client.request("POST", "/api/setup", {
            "first_name": "Jeroen", "username": "lehrkraft", "password": "sicheres-testkennwort"
        })
        self.assertEqual(status, 200)
        self.assertTrue(pending["privacy_required"])
        self.assertNotIn("Set-Cookie", headers)
        status, denied, _ = client.request("GET", "/api/classes")
        self.assertEqual(status, 401)
        self.assertIn("melden", denied["error"])
        status, information, _ = client.request("GET", "/api/privacy")
        self.assertEqual(status, 200)
        self.assertEqual(information["version"], pending["privacy_version"])
        self.assertIn("Verarbeitete Daten", [section["heading"] for section in information["sections"]])
        status, accepted, headers = client.request("POST", "/api/privacy/accept", {
            "privacy_token": pending["privacy_token"], "privacy_version": pending["privacy_version"]
        })
        self.assertEqual(status, 200)
        self.assertTrue(accepted["ok"])
        self.assertIn("Set-Cookie", headers)
        self.assertEqual(self.app.db.one("SELECT COUNT(*) count FROM privacy_acceptances")["count"], 1)
        status, _, _ = client.request("POST", "/api/privacy/accept", {
            "privacy_token": pending["privacy_token"], "privacy_version": pending["privacy_version"]
        })
        self.assertEqual(status, 401)
        client.request("POST", "/api/logout", {})
        status, logged_in, _ = client.request("POST", "/api/login", {
            "username": "lehrkraft", "password": "sicheres-testkennwort"
        })
        self.assertEqual(status, 200)
        self.assertTrue(logged_in["ok"])
        self.assertNotIn("privacy_required", logged_in)

    def test_students_cannot_read_other_team_contents(self):
        teacher = self.client
        teacher.request("POST", "/api/setup", {
            "first_name": "Jeroen", "username": "lehrkraft", "password": "sicheres-testkennwort"
        })
        _, bootstrap, _ = teacher.request("GET", "/api/bootstrap")
        teacher_id = bootstrap["user"]["id"]
        _, klass, _ = teacher.request("POST", "/api/classes", {"name": "SI24"})
        _, imported, _ = teacher.request("POST", f"/api/classes/{klass['id']}/import", {
            "rows": [{"first_name": "Lena"}, {"first_name": "Noah"}]
        })
        lena, noah = imported["created"]
        _, project, _ = teacher.request("POST", "/api/projects", {
            "title": "Sicherheitsprojekt", "class_id": klass["id"], "has_start": False, "has_end": False,
            "project_lead_id": teacher_id, "member_ids": [lena["id"], noah["id"], teacher_id],
        })
        teacher.request("POST", f"/api/projects/{project['id']}/teams", {
            "name": "Einkauf", "color": "#2563EB", "member_ids": [lena["id"]], "lead_ids": []
        })
        _, team_b, _ = teacher.request("POST", f"/api/projects/{project['id']}/teams", {
            "name": "Vertrieb", "color": "#16A34A", "member_ids": [noah["id"]], "lead_ids": []
        })
        _, secret_task, _ = teacher.request("POST", f"/api/projects/{project['id']}/tasks", {
            "title": "Vertrauliche Teamaufgabe", "team_ids": [team_b["id"]]
        })
        pdf_data = base64.b64encode(b"%PDF-team-b").decode()
        _, secret_upload, _ = teacher.request("POST", f"/api/projects/{project['id']}/uploads", {
            "name": "vertrieb.pdf", "media_type": "application/pdf", "file_base64": pdf_data,
            "team_id": team_b["id"],
        })
        student = Client(self.app)
        student.request("POST", "/api/login", {"username": lena["username"], "password": lena["access_code"]})
        status, detail, _ = student.request("GET", f"/api/projects/{project['id']}")
        self.assertEqual(status, 200)
        self.assertNotIn(secret_task["ids"][0], [task["id"] for task in detail["tasks"]])
        self.assertNotIn(secret_upload["id"], [upload["id"] for upload in detail["uploads"]])
        status, _, _ = student.request("GET", f"/api/uploads/{secret_upload['id']}")
        self.assertEqual(status, 403)
        status, _, _ = student.request("POST", f"/api/tasks/{secret_task['ids'][0]}/comments", {"body": "Unzulässiger Zugriff"})
        self.assertEqual(status, 403)


if __name__ == "__main__":
    unittest.main()
