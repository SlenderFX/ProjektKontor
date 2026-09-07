from __future__ import annotations

import base64
import calendar
import hashlib
import hmac
import io
import ipaddress
import json
import mimetypes
import os
import re
import secrets
import smtplib
import sqlite3
import sys
import threading
import traceback
import unicodedata
import warnings
from datetime import date, datetime, timedelta, timezone
from http import cookies
from email.message import EmailMessage
from email.utils import formataddr, parseaddr
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import parse_qs, quote, urlencode
from urllib.request import Request, urlopen
from wsgiref.simple_server import make_server

from PIL import Image as PillowImage, UnidentifiedImageError

from . import __version__
from .config import Config, load_config
from .db import Database, TEAM_COLOR_PALETTE, seed_project_statuses, utcnow
from .import_export import (
    account_template,
    generate_invoice_pdf,
    generate_project_report,
    next_username,
    parse_account_workbook,
)
from .security import CodeVault, generate_access_code, hash_password, hash_session_token, load_or_create_secret, new_session_token, password_needs_rehash, verify_password


STATIC_DIR = Path(__file__).resolve().parent / "static"
ALLOWED_MEDIA_TYPES = {"application/pdf": ".pdf", "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
TEAM_COLORS = set(TEAM_COLOR_PALETTE)
LICENSE_PLANS = {
    "beta": {"amount_cents": 0, "seat_limit": 1},
    "single": {"amount_cents": 7900, "seat_limit": 1},
    "department": {"amount_cents": 29900, "seat_limit": 5},
    "school": {"amount_cents": 59900, "seat_limit": 15},
}
LICENSE_STATUSES = {"draft", "active", "suspended", "expired", "cancelled"}
PAYMENT_STATUSES = {"not_required", "open", "paid", "overdue", "refunded", "cancelled"}
BILLING_CYCLES = {"none", "monthly", "annual"}
PRIVACY_VERSION = "2026-09-04.2"
SECURITY_HEADERS = [
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Referrer-Policy", "strict-origin-when-cross-origin"),
    ("Permissions-Policy", "camera=(), microphone=(), geolocation=()"),
    ("Cross-Origin-Opener-Policy", "same-origin"),
    ("Cross-Origin-Resource-Policy", "same-origin"),
    ("X-Permitted-Cross-Domain-Policies", "none"),
    ("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self' https://challenges.cloudflare.com 'sha256-l4xERraYJGTAs+NV2Zvk1GpNQGWjQE2ABVdPYAQaJFU='; frame-src https://challenges.cloudflare.com; connect-src 'self' https://challenges.cloudflare.com; object-src 'none'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"),
]


class HttpError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def safe_download_name(filename: Any) -> str:
    value = str(filename or "download").replace("\r", "").replace("\n", "")
    value = value.replace("\\", "/").rsplit("/", 1)[-1].strip() or "download"
    ascii_name = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    ascii_name = re.sub(r"[^A-Za-z0-9._ -]", "_", ascii_name).strip(" .")[:120] or "download"
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(value, safe='')}"


def parse_datetime(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise HttpError(400, f"{field} fehlt.")
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).isoformat(timespec="minutes")
    except ValueError as exc:
        raise HttpError(400, f"{field} ist ungültig.") from exc


def parse_project_date(value: Any, field: str, include_time: bool) -> str:
    text = str(value or "").strip()
    if not text:
        raise HttpError(400, f"{field} fehlt.")
    try:
        if include_time:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).isoformat(timespec="minutes")
        return datetime.fromisoformat(text[:10]).date().isoformat()
    except ValueError as exc:
        raise HttpError(400, f"{field} ist ungültig.") from exc


def parse_date(value: Any, field: str) -> str:
    text = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(text[:10]).date()
    except (TypeError, ValueError) as exc:
        raise HttpError(400, f"{field} ist ungültig.") from exc
    if not text or parsed.isoformat() != text:
        raise HttpError(400, f"{field} ist ungültig.")
    return text


def standard_license_dates(kind: str) -> tuple[str, str]:
    starts = date.today()
    if kind == "beta":
        ends = starts + timedelta(days=27)
    elif kind == "monthly":
        year = starts.year + (1 if starts.month == 12 else 0)
        month = 1 if starts.month == 12 else starts.month + 1
        next_month = date(year, month, min(starts.day, calendar.monthrange(year, month)[1]))
        ends = next_month - timedelta(days=1)
    else:
        try:
            next_year = starts.replace(year=starts.year + 1)
        except ValueError:
            next_year = starts.replace(year=starts.year + 1, day=28)
        ends = next_year - timedelta(days=1)
    return starts.isoformat(), ends.isoformat()


def product_license_dates(product: dict[str, Any], starts: date | None = None) -> tuple[str, str]:
    starts = starts or date.today()
    value = int(product["duration_value"])
    unit = product["duration_unit"]
    if unit == "days":
        end_exclusive = starts + timedelta(days=value)
    elif unit == "months":
        month_index = starts.month - 1 + value
        year = starts.year + month_index // 12
        month = month_index % 12 + 1
        end_exclusive = date(year, month, min(starts.day, calendar.monthrange(year, month)[1]))
    else:
        year = starts.year + value
        end_exclusive = date(year, starts.month, min(starts.day, calendar.monthrange(year, starts.month)[1]))
    return starts.isoformat(), (end_exclusive - timedelta(days=1)).isoformat()


def normalize_due_at(value: Any) -> str | None:
    """A date-only task/upload deadline expires at the end of that day."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            return datetime.fromisoformat(text + "T23:59").isoformat(timespec="minutes")
        return datetime.fromisoformat(text.replace("Z", "+00:00")).isoformat(timespec="minutes")
    except ValueError as exc:
        raise HttpError(400, "Die Fälligkeit ist ungültig.") from exc


def comparable_datetime(value: str) -> datetime:
    parsed=datetime.fromisoformat(value)
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def parse_id_list(value: Any, field: str) -> list[int]:
    if value is None:return []
    if not isinstance(value,list):raise HttpError(400,f"{field} ist ungültig.")
    try:return list(dict.fromkeys(int(item) for item in value if str(item).strip()))
    except (TypeError,ValueError):raise HttpError(400,f"{field} ist ungültig.")


class App:
    def __init__(self, config: Config):
        self.config = config
        self.db = Database(config.db_path)
        self.db.initialize()
        self.secret = load_or_create_secret(config.secret_path)
        self.vault = CodeVault(self.secret)
        self.routes: list[tuple[str, re.Pattern[str], Callable[..., Any]]] = []
        self._register_routes()

    def route(self, method: str, pattern: str):
        def decorator(func: Callable[..., Any]):
            self.routes.append((method, re.compile("^" + pattern + "$"), func))
            return func
        return decorator

    def _register_routes(self) -> None:
        route = self.route
        route("GET", r"/api/bootstrap")(self.bootstrap)
        route("GET", r"/api/health")(self.health)
        route("GET", r"/api/contact/config")(self.contact_config)
        route("POST", r"/api/contact")(self.contact)
        route("POST", r"/api/setup")(self.setup)
        route("POST", r"/api/login")(self.login)
        route("POST", r"/api/initial-password")(self.change_initial_password)
        route("GET", r"/api/privacy")(self.privacy_information)
        route("POST", r"/api/privacy/accept")(self.accept_privacy)
        route("POST", r"/api/logout")(self.logout)
        route("PATCH", r"/api/account")(self.update_own_account)
        route("POST", r"/api/support/requests")(self.create_support_request)
        route("GET", r"/api/support/requests")(self.list_support_requests)
        route("DELETE", r"/api/support/requests/(?P<request_id>\d+)")(self.revoke_support_request)
        route("POST", r"/api/support/redeem")(self.redeem_support_code)
        route("GET", r"/api/support/grants")(self.list_support_grants)
        route("GET", r"/api/teachers")(self.list_teachers)
        route("POST", r"/api/teachers")(self.create_teacher)
        route("PATCH", r"/api/teachers/(?P<teacher_id>\d+)")(self.update_teacher)
        route("POST", r"/api/teachers/(?P<teacher_id>\d+)/archive")(self.archive_teacher)
        route("POST", r"/api/teachers/(?P<teacher_id>\d+)/restore")(self.restore_teacher)
        route("DELETE", r"/api/teachers/(?P<teacher_id>\d+)")(self.delete_teacher)
        route("POST", r"/api/teachers/(?P<teacher_id>\d+)/initial-credentials/email")(self.email_initial_credentials)
        route("GET", r"/api/licenses")(self.list_licenses)
        route("GET", r"/api/license-products")(self.list_license_products)
        route("PATCH", r"/api/license-products/(?P<product_key>[a-z0-9_-]+)")(self.update_license_product)
        route("GET", r"/api/license-requests")(self.list_license_requests)
        route("PATCH", r"/api/license-requests/(?P<request_id>\d+)")(self.update_license_request)
        route("POST", r"/api/licenses")(self.create_license)
        route("PATCH", r"/api/licenses/(?P<license_id>\d+)")(self.update_license)
        route("POST", r"/api/licenses/(?P<license_id>\d+)/courtesy-cancel")(self.courtesy_cancel_license)
        route("POST", r"/api/licenses/(?P<license_id>\d+)/archive")(self.archive_license)
        route("DELETE", r"/api/licenses/(?P<license_id>\d+)")(self.delete_license)
        route("GET", r"/api/invoice-settings")(self.get_invoice_settings)
        route("PATCH", r"/api/invoice-settings")(self.update_invoice_settings)
        route("POST", r"/api/licenses/(?P<license_id>\d+)/invoice")(self.create_invoice)
        route("POST", r"/api/invoices/(?P<invoice_id>\d+)/paid")(self.mark_invoice_paid)
        route("POST", r"/api/invoices/(?P<invoice_id>\d+)/archive")(self.archive_invoice)
        route("POST", r"/api/invoices/(?P<invoice_id>\d+)/email")(self.email_invoice)
        route("GET", r"/api/invoices/(?P<invoice_id>\d+)")(self.download_invoice)
        route("DELETE", r"/api/invoices/(?P<invoice_id>\d+)")(self.delete_invoice)
        route("GET", r"/api/classes")(self.list_classes)
        route("POST", r"/api/classes")(self.create_class)
        route("PATCH", r"/api/classes/(?P<class_id>\d+)")(self.update_class)
        route("DELETE", r"/api/classes/(?P<class_id>\d+)")(self.delete_class)
        route("GET", r"/api/classes/(?P<class_id>\d+)/users")(self.list_class_users)
        route("POST", r"/api/classes/(?P<class_id>\d+)/users")(self.create_class_user)
        route("GET", r"/api/classes/(?P<class_id>\d+)/template")(self.download_account_template)
        route("POST", r"/api/classes/(?P<class_id>\d+)/import-preview")(self.import_preview)
        route("POST", r"/api/classes/(?P<class_id>\d+)/import")(self.import_accounts)
        route("GET", r"/api/users")(self.list_all_users)
        route("GET", r"/api/users/import-template")(self.download_account_template)
        route("POST", r"/api/users/import-preview")(self.import_preview)
        route("POST", r"/api/users/import")(self.import_accounts)
        route("PATCH", r"/api/users/(?P<user_id>\d+)")(self.update_user)
        route("DELETE", r"/api/users/(?P<user_id>\d+)")(self.delete_user)
        route("GET", r"/api/projects")(self.list_projects)
        route("GET", r"/api/dashboard")(self.dashboard)
        route("GET", r"/api/search")(self.search_workspace)
        route("POST", r"/api/projects")(self.create_project)
        route("GET", r"/api/projects/(?P<project_id>\d+)")(self.project_detail)
        route("PATCH", r"/api/projects/(?P<project_id>\d+)")(self.update_project)
        route("PATCH", r"/api/projects/(?P<project_id>\d+)/statuses/(?P<status_id>\d+)")(self.update_status)
        route("POST", r"/api/projects/(?P<project_id>\d+)/members")(self.add_project_member)
        route("DELETE", r"/api/projects/(?P<project_id>\d+)/members/(?P<user_id>\d+)")(self.remove_project_member)
        route("POST", r"/api/projects/(?P<project_id>\d+)/phases")(self.create_phase)
        route("PATCH", r"/api/phases/(?P<phase_id>\d+)")(self.update_phase)
        route("POST", r"/api/projects/(?P<project_id>\d+)/teams")(self.create_team)
        route("PATCH", r"/api/teams/(?P<team_id>\d+)")(self.update_team)
        route("POST", r"/api/teams/(?P<team_id>\d+)/members")(self.set_team_member)
        route("DELETE", r"/api/teams/(?P<team_id>\d+)/members/(?P<user_id>\d+)")(self.remove_team_member)
        route("POST", r"/api/projects/(?P<project_id>\d+)/tasks")(self.create_task)
        route("PATCH", r"/api/tasks/(?P<task_id>\d+)")(self.update_task)
        route("DELETE", r"/api/tasks/(?P<task_id>\d+)")(self.delete_task)
        route("POST", r"/api/tasks/(?P<task_id>\d+)/assignees")(self.set_task_assignees)
        route("POST", r"/api/tasks/(?P<task_id>\d+)/dependencies")(self.set_task_dependencies)
        route("POST", r"/api/tasks/(?P<task_id>\d+)/deadline-requests")(self.create_deadline_request)
        route("PATCH", r"/api/deadline-requests/(?P<request_id>\d+)")(self.decide_deadline_request)
        route("POST", r"/api/tasks/(?P<task_id>\d+)/comments")(self.create_comment)
        route("PATCH", r"/api/comments/(?P<comment_id>\d+)")(self.update_comment)
        route("DELETE", r"/api/comments/(?P<comment_id>\d+)")(self.hide_comment)
        route("POST", r"/api/tasks/(?P<task_id>\d+)/uploads")(self.upload_file)
        route("POST", r"/api/projects/(?P<project_id>\d+)/uploads")(self.upload_project_file)
        route("GET", r"/api/uploads/(?P<upload_id>\d+)")(self.download_upload)
        route("DELETE", r"/api/uploads/(?P<upload_id>\d+)")(self.delete_upload)
        route("GET", r"/api/notifications")(self.list_notifications)
        route("POST", r"/api/notifications/read")(self.read_notifications)
        route("POST", r"/api/projects/(?P<project_id>\d+)/report")(self.create_report)
        route("GET", r"/api/reports/(?P<report_id>\d+)")(self.download_report)
        route("POST", r"/api/projects/(?P<project_id>\d+)/archive")(self.archive_project)
        route("POST", r"/api/projects/(?P<project_id>\d+)/template")(self.create_template)
        route("GET", r"/api/templates")(self.list_templates)

    def __call__(self, environ: dict[str, Any], start_response: Callable[..., Any]) -> Iterable[bytes]:
        method = environ.get("REQUEST_METHOD", "GET").upper()
        path = environ.get("PATH_INFO", "/")
        try:
            if path.startswith("/api/"):
                for route_method, pattern, handler in self.routes:
                    match = pattern.match(path)
                    if route_method == method and match:
                        user, session = self.current_user(environ)
                        if method in {"POST", "PATCH", "DELETE"} and path not in {"/api/setup", "/api/login", "/api/initial-password", "/api/privacy/accept", "/api/contact"}:
                            if not session or not hmac.compare_digest(environ.get("HTTP_X_CSRF_TOKEN", ""), session["csrf_token"]):
                                raise HttpError(403, "Sicherheitsprüfung fehlgeschlagen. Bitte laden Sie die Seite neu.")
                        result = handler(
                            environ, user,
                            **{k: int(v) if str(v).isdigit() else v for k, v in match.groupdict().items()},
                        )
                        return self.respond(start_response, result)
                raise HttpError(404, "Adresse nicht gefunden")
            return self.serve_static(path, start_response)
        except HttpError as exc:
            return self.respond(start_response, {"error": exc.message}, status=exc.status)
        except Exception:
            traceback.print_exc()
            return self.respond(start_response, {"error": "Ein interner Fehler ist aufgetreten."}, status=500)

    def respond(self, start_response, result: Any, status: int = 200) -> list[bytes]:
        if isinstance(result, tuple) and len(result) == 4:
            body, content_type, filename, extra_headers = result
            if not isinstance(body, (bytes, bytearray)):
                body = json_bytes(body)
            headers = [("Content-Type", content_type), ("Content-Length", str(len(body))), ("Cache-Control", "private, no-store")] + list(extra_headers)
            headers.append(("X-Robots-Tag", "noindex, nofollow, noarchive, nosnippet"))
            if filename:
                headers.append(("Content-Disposition", safe_download_name(filename)))
        else:
            body = json_bytes(result)
            headers = [("Content-Type", "application/json; charset=utf-8"), ("Content-Length", str(len(body))), ("Cache-Control", "no-store"), ("X-Robots-Tag", "noindex, nofollow, noarchive, nosnippet")]
        headers += SECURITY_HEADERS
        if self.config.cookie_secure:
            headers.append(("Strict-Transport-Security", "max-age=31536000; includeSubDomains"))
        reason = {200:"OK",201:"Created",204:"No Content",400:"Bad Request",401:"Unauthorized",403:"Forbidden",404:"Not Found",409:"Conflict",413:"Payload Too Large",429:"Too Many Requests",500:"Internal Server Error",503:"Service Unavailable"}.get(status, "OK")
        start_response(f"{status} {reason}", headers)
        return [body]

    def serve_static(self, path: str, start_response) -> list[bytes]:
        if path in {"/", ""}:
            relative = "landing.html"
        elif path.rstrip("/") == "/login":
            relative = "index.html"
        elif path.rstrip("/") == "/impressum":
            relative = "impressum.html"
        elif path.rstrip("/") == "/datenschutz":
            relative = "datenschutz.html"
        else:
            relative = path.lstrip("/")
        target = (STATIC_DIR / relative).resolve()
        if STATIC_DIR.resolve() not in target.parents and target != STATIC_DIR.resolve():
            raise HttpError(404, "Datei nicht gefunden")
        if not target.is_file():
            target = STATIC_DIR / "index.html"
        body = target.read_bytes()
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        headers = [("Content-Type", content_type), ("Content-Length", str(len(body))), ("Cache-Control", "no-cache")] + SECURITY_HEADERS
        if self.config.cookie_secure:
            headers.append(("Strict-Transport-Security", "max-age=31536000; includeSubDomains"))
        start_response("200 OK", headers)
        return [body]

    def body(self, environ: dict[str, Any], max_bytes: int = 1024 * 1024) -> dict[str, Any]:
        try:
            length = int(environ.get("CONTENT_LENGTH") or 0)
        except ValueError:
            raise HttpError(400, "Ungültige Anfrage")
        if length < 0 or length > max_bytes:
            raise HttpError(413, "Die Anfrage ist zu groß.")
        raw = environ["wsgi.input"].read(length)
        if not raw:
            return {}
        try:
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError
            return value
        except (json.JSONDecodeError, ValueError) as exc:
            raise HttpError(400, "Ungültige Daten") from exc

    def query(self, environ: dict[str, Any]) -> dict[str, str]:
        return {key: values[-1] for key, values in parse_qs(environ.get("QUERY_STRING", "")).items()}

    def client_ip(self, environ: dict[str, Any]) -> str:
        candidates: list[str] = []
        if self.config.trust_proxy:
            candidates.extend(part.strip() for part in environ.get("HTTP_X_FORWARDED_FOR", "").split(","))
        candidates.append(str(environ.get("REMOTE_ADDR", "unknown")).strip())
        for candidate in candidates:
            try:
                return str(ipaddress.ip_address(candidate))
            except ValueError:
                continue
        return "unknown"

    def require_user(self, user: dict[str, Any] | None) -> dict[str, Any]:
        if not user:
            raise HttpError(401, "Bitte melden Sie sich an.")
        return user

    def require_teacher(self, user: dict[str, Any] | None) -> dict[str, Any]:
        user = self.require_user(user)
        if user["role"] != "teacher":
            raise HttpError(403, "Diese Funktion ist Lehrkräften vorbehalten.")
        return user

    def require_owner(self, user: dict[str, Any] | None) -> dict[str, Any]:
        user = self.require_teacher(user)
        if not user.get("is_owner"):
            raise HttpError(403, "Diese Funktion ist der Geschäftsführung vorbehalten.")
        return user

    def require_regular_teacher(self, user: dict[str, Any] | None) -> dict[str, Any]:
        user = self.require_teacher(user)
        if user.get("is_owner"):
            raise HttpError(403, "Diese Funktion ist Lehrkraftkonten vorbehalten.")
        return user

    def teacher_has_class(self, teacher_id: int, class_id: int) -> bool:
        return bool(self.db.one(
            "SELECT 1 ok FROM teacher_classes WHERE teacher_id=? AND class_id=?",
            (teacher_id, class_id),
        ))

    def admin_supports_class(self, admin_id: int, class_id: int) -> bool:
        return bool(self.db.one(
            """SELECT 1 ok FROM support_requests sr
               JOIN teacher_classes tc ON tc.teacher_id=sr.teacher_id
               WHERE sr.granted_admin_id=? AND tc.class_id=? AND sr.status='active'
                 AND sr.access_expires_at>? LIMIT 1""",
            (admin_id, class_id, utcnow()),
        ))

    def can_access_class(self, user: dict[str, Any], class_id: int) -> bool:
        return bool(
            user["role"] == "teacher"
            and (
                self.admin_supports_class(user["id"], class_id)
                if user.get("is_owner")
                else self.teacher_has_class(user["id"], class_id)
            )
        )

    def require_class_access(self, user: dict[str, Any] | None, class_id: int) -> dict[str, Any]:
        user = self.require_teacher(user)
        if not self.can_access_class(user, class_id):
            raise HttpError(403, "Diese Klasse wurde Ihrer Lehrkraft nicht freigeschaltet.")
        return user

    def can_manage_project(self, user: dict[str, Any], project: dict[str, Any]) -> bool:
        return bool(
            project["project_lead_id"] == user["id"]
            or (user["role"] == "teacher" and self.can_access_class(user, project["class_id"]))
        )

    def project_access(self, user: dict[str, Any] | None, project_id: int, manage: bool = False) -> tuple[dict[str, Any], dict[str, Any]]:
        user = self.require_user(user)
        project = self.db.one("SELECT * FROM projects WHERE id=?", (project_id,))
        if not project:
            raise HttpError(404, "Projekt nicht gefunden")
        is_member = self.db.one("SELECT 1 ok FROM project_members WHERE project_id=? AND user_id=?", (project_id, user["id"]))
        can_manage = self.can_manage_project(user, project)
        if not is_member and not can_manage:
            raise HttpError(403, "Sie sind diesem Projekt nicht zugeordnet.")
        if manage and not can_manage:
            raise HttpError(403, "Diese Änderung ist der Gesamtprojektleitung oder Geschäftsführung vorbehalten.")
        return user, project

    def team_scope_access(self, user: dict[str, Any] | None, project_id: int, team_id: int | None) -> tuple[dict[str, Any], dict[str, Any]]:
        user, project = self.project_access(user, project_id)
        if team_id and not self.can_manage_project(user, project):
            if not self.db.one("SELECT 1 ok FROM team_members WHERE team_id=? AND user_id=?", (team_id, user["id"])):
                raise HttpError(403, "Auf Inhalte anderer Teams dürfen nur die Gesamtprojektleitung und Geschäftsführung zugreifen.")
        return user, project

    def task_scope_access(self, user: dict[str, Any] | None, task: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        return self.team_scope_access(user, task["project_id"], task.get("team_id"))

    def current_user(self, environ: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        jar = cookies.SimpleCookie(environ.get("HTTP_COOKIE", ""))
        morsel = jar.get("pk_session")
        if not morsel:
            return None, None
        token_hash = hash_session_token(morsel.value, self.secret)
        session = self.db.one("SELECT * FROM sessions WHERE token_hash=? AND expires_at>?", (token_hash, utcnow()))
        if not session:
            return None, None
        user = self.db.one("SELECT id,class_id,first_name,username,role,is_owner,must_change_password,active,last_login_at FROM users WHERE id=? AND active=1", (session["user_id"],))
        if not user:
            return None, None
        if user["role"] == "teacher" and not user.get("is_owner") and not self.teacher_license_valid(user["id"]):
            self.db.execute("DELETE FROM sessions WHERE id=?", (session["id"],))
            return None, None
        return user, session

    def refresh_expired_licenses(self) -> None:
        today = date.today().isoformat()
        expired = self.db.all("SELECT id FROM licenses WHERE status='active' AND ends_on<?", (today,))
        scheduled = self.db.all(
            "SELECT id FROM licenses WHERE status='draft' AND follow_up_of IS NOT NULL AND starts_on<=? AND ends_on>=?",
            (today, today),
        )
        missed = self.db.all(
            "SELECT id FROM licenses WHERE status='draft' AND follow_up_of IS NOT NULL AND ends_on<?", (today,)
        )
        if not expired and not scheduled and not missed:
            return
        with self.db.transaction() as connection:
            now = utcnow()
            for rows, status in ((expired, "expired"), (scheduled, "active"), (missed, "expired")):
                if rows:
                    ids = [row["id"] for row in rows]
                    placeholders = ",".join("?" for _ in ids)
                    connection.execute(
                        f"UPDATE licenses SET status=?,updated_at=? WHERE id IN ({placeholders})",
                        (status, now, *ids),
                    )

    def run_license_automation(self) -> dict[str, int]:
        today_date = date.today()
        today = today_date.isoformat()
        reminder_until = (today_date + timedelta(days=7)).isoformat()
        now = utcnow()
        reminded = self.db.all(
            """SELECT id FROM licenses WHERE status='draft' AND follow_up_of IS NOT NULL
                 AND archived_at IS NULL AND follow_up_reminded_at IS NULL
                 AND starts_on>? AND starts_on<=?""",
            (today, reminder_until),
        )
        if reminded:
            ids = [row["id"] for row in reminded]
            placeholders = ",".join("?" for _ in ids)
            self.db.execute(
                f"UPDATE licenses SET follow_up_reminded_at=?,updated_at=? WHERE id IN ({placeholders})",
                (now, now, *ids),
            )

        self.refresh_expired_licenses()
        self.db.execute(
            "UPDATE invoices SET status='paid' WHERE gross_cents=0 AND status='open'"
        )
        self.db.execute(
            """UPDATE license_orders SET payment_status='not_required',updated_at=?
                 WHERE amount_cents=0 AND payment_status IN ('open','overdue')""",
            (now,),
        )
        self.db.execute(
            """UPDATE license_orders SET payment_status='overdue',updated_at=?
                 WHERE payment_status='open' AND id IN (
                   SELECT l.order_id FROM licenses l JOIN invoices i ON i.license_id=l.id
                    WHERE i.status='open' AND i.gross_cents>0
                      AND i.archived_at IS NULL AND i.due_on<?
                 )""",
            (now, today),
        )
        owner = self.db.one(
            "SELECT * FROM users WHERE role='teacher' AND is_owner=1 AND active=1 ORDER BY id LIMIT 1"
        )
        candidates = self.db.all(
            """SELECT id FROM licenses WHERE status='active' AND follow_up_of IS NOT NULL
                 AND archived_at IS NULL AND starts_on<=? AND invoice_automation_completed_at IS NULL
                 AND COALESCE(invoice_automation_attempted_on,'')<>? ORDER BY starts_on,id""",
            (today, today),
        )
        created = failed = 0
        for row in candidates:
            license_id = row["id"]
            with self.db.transaction() as connection:
                claimed = connection.execute(
                    """UPDATE licenses SET invoice_automation_attempted_on=?,invoice_automation_error='',updated_at=?
                         WHERE id=? AND invoice_automation_completed_at IS NULL
                           AND COALESCE(invoice_automation_attempted_on,'')<>?""",
                    (today, now, license_id, today),
                ).rowcount
            if not claimed:
                continue
            existing = self.db.one(
                """SELECT id FROM invoices WHERE license_id=? AND archived_at IS NULL
                     AND status<>'cancelled' LIMIT 1""",
                (license_id,),
            )
            if existing:
                self.db.execute(
                    "UPDATE licenses SET invoice_automation_completed_at=?,invoice_automation_error='' WHERE id=?",
                    (now, license_id),
                )
                continue
            if not owner:
                error = "Die automatische Rechnung konnte nicht erstellt werden: Administratorkonto fehlt."
                self.db.execute(
                    "UPDATE licenses SET invoice_automation_error=? WHERE id=?", (error, license_id)
                )
                failed += 1
                continue
            payload = json_bytes({"confirm_zero_invoice": True})
            environ = {"CONTENT_LENGTH": str(len(payload)), "wsgi.input": io.BytesIO(payload)}
            try:
                self.create_invoice(environ, owner, license_id)
                self.db.execute(
                    "UPDATE licenses SET invoice_automation_completed_at=?,invoice_automation_error='' WHERE id=?",
                    (utcnow(), license_id),
                )
                created += 1
            except Exception as exc:
                message = exc.message if isinstance(exc, HttpError) else "Unerwarteter Fehler bei der Rechnungserstellung."
                self.db.execute(
                    "UPDATE licenses SET invoice_automation_error=? WHERE id=?",
                    (f"Automatische Rechnung fehlgeschlagen: {message}"[:1000], license_id),
                )
                failed += 1
        return {"reminded": len(reminded), "created": created, "failed": failed}

    def teacher_license_valid(self, teacher_id: int) -> bool:
        self.refresh_expired_licenses()
        today = date.today().isoformat()
        return bool(self.db.one(
            """SELECT 1 ok FROM license_teachers lt
               JOIN licenses l ON l.id=lt.license_id
               WHERE lt.teacher_id=? AND l.status='active'
                 AND l.starts_on<=? AND l.ends_on>=? LIMIT 1""",
            (teacher_id, today, today),
        ))

    def active_teacher_license(self, teacher_id: int) -> dict[str, Any] | None:
        """Return the currently usable license and its commercial plan."""
        self.refresh_expired_licenses()
        today = date.today().isoformat()
        return self.db.one(
            """SELECT l.*,o.plan,o.billing_cycle,o.customer_name,o.organization
                 FROM license_teachers lt
                 JOIN licenses l ON l.id=lt.license_id
                 JOIN license_orders o ON o.id=l.order_id
                WHERE lt.teacher_id=? AND l.status='active' AND l.archived_at IS NULL
                  AND l.starts_on<=? AND l.ends_on>=?
                ORDER BY CASE o.plan WHEN 'beta' THEN 1 ELSE 0 END,l.ends_on DESC LIMIT 1""",
            (teacher_id, today, today),
        )

    def start_session(self, user_id: int) -> tuple[str, str, int]:
        account = self.db.one("SELECT role,is_owner FROM users WHERE id=? AND active=1", (user_id,))
        if not account:
            raise HttpError(401, "Konto nicht gefunden oder deaktiviert.")
        if account["role"] == "teacher" and not account.get("is_owner") and not self.teacher_license_valid(user_id):
            raise HttpError(403, "Dieser Lehrkraftzugang ist noch nicht freigeschaltet. Bitte wenden Sie sich an die ProjektKontor-Verwaltung.")
        duration_hours = 6 if account["role"] == "student" else 12
        max_age = duration_hours * 60 * 60
        token = new_session_token()
        csrf = secrets.token_urlsafe(24)
        expires = (datetime.now(timezone.utc) + timedelta(hours=duration_hours)).isoformat(timespec="seconds")
        with self.db.transaction() as connection:
            if account["role"] == "student":
                connection.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
            connection.execute("INSERT INTO sessions(user_id,token_hash,csrf_token,expires_at,created_at) VALUES(?,?,?,?,?)", (user_id,hash_session_token(token,self.secret),csrf,expires,utcnow()))
        return token, csrf, max_age

    def session_cookie(self, token: str, max_age: int = 43200) -> str:
        secure = "; Secure" if self.config.cookie_secure else ""
        return f"pk_session={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={max_age}{secure}"

    def bootstrap(self, environ, user):
        configured = bool(self.db.one("SELECT 1 ok FROM users WHERE role='teacher' AND is_owner=1 LIMIT 1"))
        has_accounts = bool(self.db.one("SELECT 1 ok FROM users WHERE active=1 LIMIT 1"))
        result: dict[str, Any] = {
            "configured": configured,
            "setup_available": not configured,
            "setup_token_required": bool(self.config.admin_setup_token),
            "has_accounts": has_accounts,
            "user": user,
        }
        if user:
            _, session = self.current_user(environ)
            result["csrf"] = session["csrf_token"] if session else ""
            result["unread_notifications"] = self.db.one("SELECT COUNT(*) count FROM notifications WHERE user_id=? AND read_at IS NULL", (user["id"],))["count"]
        return result

    def privacy_information(self, environ, user):
        return {
            "version": PRIVACY_VERSION,
            "title": "Datenschutzinformation nach Art. 13 und 14 DSGVO",
            "operator": {
                "name": "PRIMEAdvisory",
                "owner": "Inhaber: Jeroen L. Jochem",
                "address": "Nieberdingstr. 41, 45147 Essen, Deutschland",
                "email": "info@prime-advisory.de",
                "phone": "0201 8438179-0",
            },
            "controller": {
                "name": self.config.privacy_controller_name,
                "address": self.config.privacy_controller_address,
                "email": self.config.privacy_controller_email,
                "dpo": self.config.privacy_dpo_contact,
            },
            "sections": [
                {"heading": "Rollen und Verantwortlichkeit", "paragraphs": [
                    "PRIMEAdvisory betreibt und entwickelt ProjektKontor und ist Ansprechpartner für den technischen Betrieb, die öffentliche Website, Vertragsanbahnung und Abrechnung.",
                    "Bestimmt eine Schule beziehungsweise ihr zuständiger Schulträger die Zwecke und Mittel des Unterrichtseinsatzes, ist diese Stelle für die dabei verarbeiteten Unterrichtsdaten verantwortlich. Die für diese Installation hinterlegte verantwortliche Stelle und ihr Datenschutzkontakt werden oben ausgewiesen.",
                ]},
                {"heading": "Zwecke der Verarbeitung", "paragraphs": [
                    "ProjektKontor dient der Organisation schulischer Projekte, der Verteilung und Bearbeitung von Aufgaben, der Zusammenarbeit in Projektteams sowie der Dokumentation des Projektverlaufs. Lehrkräfte können außerdem freiwillig einen befristeten Supportzugriff anfordern.",
                ]},
                {"heading": "Verarbeitete Daten", "items": [
                    "Kontodaten: Vorname, Benutzername, Klasse und Rolle.",
                    "Anmeldedaten: verschlüsselter Zugangscode beziehungsweise Kennworthash, Zeitpunkt der letzten Anmeldung und zeitlich begrenzte Sitzung.",
                    "Projektdaten: Mitgliedschaften, Team- und Leitungsrollen, Zuständigkeiten, Aufgaben, Status, Termine und Fortschritt.",
                    "Inhaltsdaten: Kommentare, Bearbeitungshinweise, Begründungen, Ergebnistexte und hochgeladene PDF- oder Bilddateien.",
                    "Sicherheitsdaten: fehlgeschlagene Anmeldeversuche mit Benutzername, Clientadresse und Zeitpunkt für höchstens 15 Minuten.",
                    "Supportdaten: Telefonnummer, Beschreibung des Anliegens, gehashter Unterstützungscode sowie Beginn, Ende und Widerruf einer zeitlich begrenzten Supportfreigabe.",
                    "Lizenz- und Vertragsdaten bei Lehrkraftkonten: Schule oder Organisation, Ansprechperson, geschäftliche E-Mail-Adresse, Rechnungsanschrift, Bestellreferenz, Lizenzmodell, Laufzeit, Preis sowie Zahlungs- und Freischaltstatus.",
                    "Nachweis dieser Information: Version und Zeitpunkt der Bestätigung.",
                ]},
                {"heading": "Herkunft der Daten", "paragraphs": [
                    "Kontodaten können unmittelbar bei Ihnen erhoben oder durch die Geschäftsführung beziehungsweise Lehrkraft einzeln oder über eine Excel-Importvorlage angelegt werden. Projekt-, Aufgaben- und Inhaltsdaten entstehen anschließend durch die Nutzung von ProjektKontor sowie durch Zuweisungen berechtigter Projekt- und Teamleitungen.",
                ]},
                {"heading": "Lokale Filtereinstellungen", "paragraphs": [
                    "Selbst gespeicherte Aufgabenfilter werden ausschließlich im lokalen Speicher des verwendeten Browsers abgelegt. Sie werden nicht an den ProjektKontor-Server übertragen und können durch das Löschen der Websitedaten im Browser entfernt werden.",
                ]},
                {"heading": "Rechtsgrundlage", "paragraphs": [self.config.privacy_legal_basis]},
                {"heading": "Empfänger und Sichtbarkeit", "paragraphs": [
                    "Lehrkräfte können die Konten und Projektdaten ihrer eigenen Klassen einsehen. Projektmitglieder sehen die für ihr Projekt bestimmten Inhalte; teambezogene Aufgaben, Kommentare und Uploads sind grundsätzlich auf das jeweilige Team sowie die Leitungsrollen beschränkt. Der technische Hosting-Anbieter verarbeitet Daten nur im Rahmen des Serverbetriebs.",
                    "Der Plattform-Admin sieht ohne Supportfreigabe keine Schüler-, Projekt- oder Unterrichtsdaten eines Lehrkraftkontos. Erst wenn die Lehrkraft einen Unterstützungscode erzeugt und persönlich übermittelt, wird ihr Bereich für höchstens zwei Stunden freigeschaltet. Die Lehrkraft kann diese Freigabe jederzeit widerrufen.",
                    "Eine Übermittlung in ein Drittland findet durch ProjektKontor selbst nicht statt. Falls ein Hosting-Anbieter außerhalb der EU oder des EWR eingesetzt wird, muss der Verantwortliche hierüber gesondert informieren.",
                ]},
                {"heading": "Speicherdauer", "items": [
                    "Schülerkonten werden bei einer angeordneten Löschung unmittelbar aus der aktiven Verwaltung entfernt.",
                    "Nach Projektabschluss kann ein dauerhafter PDF-Projektbericht erhalten bleiben; operative Aufgaben, Kommentare und Uploads können anschließend gelöscht werden.",
                    "Fehlgeschlagene Anmeldeversuche werden nach 15 Minuten gelöscht.",
                    "Supportanfragen und die darin angegebene Telefonnummer werden spätestens 30 Tage nach ihrer Erstellung gelöscht; der eigentliche Datenzugriff endet spätestens zwei Stunden nach Einlösung des Codes.",
                    "Lizenz-, Bestell- und Abrechnungsdaten werden entsprechend der vertraglichen und gesetzlichen Aufbewahrungspflichten gespeichert.",
                    "Reguläre Sicherungen werden nach 14 Tagen ersetzt, sofern der Betreiber keine abweichende, dokumentierte Frist festlegt.",
                    "Die Bestätigung dieser Datenschutzinformation bleibt so lange gespeichert, wie das zugehörige Konto besteht.",
                ]},
                {"heading": "Ihre Rechte", "paragraphs": [
                    "Sie können im Rahmen der gesetzlichen Voraussetzungen Auskunft, Berichtigung, Löschung, Einschränkung der Verarbeitung und gegebenenfalls Datenübertragbarkeit verlangen sowie einer Verarbeitung widersprechen. Soweit eine Verarbeitung auf einer Einwilligung beruht, kann diese für die Zukunft widerrufen werden.",
                    "Außerdem besteht ein Beschwerderecht bei der zuständigen Datenschutzaufsichtsbehörde. In Nordrhein-Westfalen ist dies die Landesbeauftragte für Datenschutz und Informationsfreiheit Nordrhein-Westfalen, Kavalleriestraße 2–4, 40213 Düsseldorf, poststelle@ldi.nrw.de, www.ldi.nrw.de.",
                    "ProjektKontor trifft keine ausschließlich automatisierten Entscheidungen und erstellt kein Profiling.",
                ]},
                {"heading": "Hinweis zu Schüler-Zugangscodes", "paragraphs": [
                    "Die Zugangscodes werden verschlüsselt gespeichert und können vereinbarungsgemäß von der Geschäftsführung beziehungsweise Lehrkraft zur Kontoverwaltung ausgelesen werden. Verwenden Sie diesen Zugangscode deshalb nicht für andere Dienste.",
                ]},
                {"heading": "Pflicht zur Bereitstellung", "paragraphs": [
                    "Die für das Konto erforderlichen Angaben werden benötigt, um ProjektKontor im Unterricht zu verwenden. Ohne Bestätigung, dass diese Datenschutzinformation zur Kenntnis genommen wurde, wird keine Sitzung eröffnet.",
                ]},
            ],
        }

    def authenticated_login_result(self, user_id: int) -> Any:
        account = self.db.one("SELECT role,is_owner,must_change_password FROM users WHERE id=? AND active=1", (user_id,))
        if account and account["role"] == "teacher" and not account.get("is_owner") and not self.teacher_license_valid(user_id):
            raise HttpError(403, "Dieser Lehrkraftzugang ist noch nicht freigeschaltet. Bitte wenden Sie sich an die ProjektKontor-Verwaltung.")
        if account and account["role"] == "teacher" and not account.get("is_owner") and account.get("must_change_password"):
            token = new_session_token()
            expires_at = (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat(timespec="seconds")
            with self.db.transaction() as connection:
                connection.execute("DELETE FROM pending_logins WHERE user_id=? OR expires_at<=?", (user_id, utcnow()))
                connection.execute("INSERT INTO pending_logins(token_hash,user_id,expires_at,created_at) VALUES(?,?,?,?)", (hash_session_token(token, self.secret), user_id, expires_at, utcnow()))
            return {"password_change_required": True, "password_change_token": token}
        accepted = self.db.one("SELECT 1 ok FROM privacy_acceptances WHERE user_id=? AND privacy_version=?", (user_id, PRIVACY_VERSION))
        if not accepted:
            token = new_session_token()
            expires_at = (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat(timespec="seconds")
            with self.db.transaction() as connection:
                connection.execute("DELETE FROM pending_logins WHERE user_id=? OR expires_at<=?", (user_id, utcnow()))
                connection.execute("INSERT INTO pending_logins(token_hash,user_id,expires_at,created_at) VALUES(?,?,?,?)", (hash_session_token(token, self.secret), user_id, expires_at, utcnow()))
            return {"privacy_required": True, "privacy_version": PRIVACY_VERSION, "privacy_token": token}
        self.db.execute("UPDATE users SET last_login_at=? WHERE id=?", (utcnow(), user_id))
        token, csrf, max_age = self.start_session(user_id)
        return ({"ok": True, "csrf": csrf}, "application/json; charset=utf-8", None, [("Set-Cookie", self.session_cookie(token, max_age))])

    def accept_privacy(self, environ, user):
        data = self.body(environ)
        token = str(data.get("privacy_token", ""))
        if not token or len(token) > 200 or data.get("privacy_version") != PRIVACY_VERSION:
            raise HttpError(400, "Die Datenschutzbestätigung ist ungültig oder nicht mehr aktuell.")
        token_hash = hash_session_token(token, self.secret)
        with self.db.transaction() as connection:
            pending = connection.execute("SELECT * FROM pending_logins WHERE token_hash=? AND expires_at>?", (token_hash, utcnow())).fetchone()
            if not pending:
                raise HttpError(401, "Die Bestätigung ist abgelaufen. Bitte melden Sie sich erneut an.")
            user_id = int(pending["user_id"])
            connection.execute("INSERT OR REPLACE INTO privacy_acceptances(user_id,privacy_version,accepted_at) VALUES(?,?,?)", (user_id, PRIVACY_VERSION, utcnow()))
            connection.execute("DELETE FROM pending_logins WHERE token_hash=?", (token_hash,))
            connection.execute("UPDATE users SET last_login_at=? WHERE id=?", (utcnow(), user_id))
        session_token, csrf, max_age = self.start_session(user_id)
        return ({"ok": True, "csrf": csrf}, "application/json; charset=utf-8", None, [("Set-Cookie", self.session_cookie(session_token, max_age))])

    def health(self, environ, user):
        return {"status": "ok", "version": __version__}

    def contact_config(self, environ, user):
        return {"turnstile_sitekey": self.config.turnstile_sitekey}

    def contact(self, environ, user):
        if environ.get("CONTENT_TYPE", "").split(";", 1)[0].strip().lower() != "application/json":
            raise HttpError(400, "Ungültige Anfrage")
        if environ.get("HTTP_SEC_FETCH_SITE", "") == "cross-site":
            raise HttpError(403, "Sicherheitsprüfung fehlgeschlagen.")
        data = self.body(environ, max_bytes=16 * 1024)
        # Das unsichtbare Feld fangen einfache Formularbots ab, ohne ihnen einen Hinweis zu geben.
        if str(data.get("website", "")).strip():
            return {"ok": True, "message": "Vielen Dank. Ihre Anfrage wurde versendet."}
        name = re.sub(r"[\r\n]+", " ", str(data.get("name", "")).strip())
        email = re.sub(r"[\r\n]+", "", str(data.get("email", "")).strip())
        subject = re.sub(r"[\r\n]+", " ", str(data.get("subject", "")).strip())
        message = str(data.get("message", "")).strip()
        organization = re.sub(r"[\r\n]+", " ", str(data.get("organization", "")).strip())
        pilot_start = str(data.get("pilot_start", "")).strip()
        usage_outlook = str(data.get("usage_outlook", "")).strip()
        parsed_email = parseaddr(email)[1]
        if not 2 <= len(name) <= 100:
            raise HttpError(400, "Bitte geben Sie Ihren Namen an.")
        if parsed_email != email or len(email) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
            raise HttpError(400, "Bitte geben Sie eine gültige E-Mail-Adresse an.")
        if not 3 <= len(subject) <= 140:
            raise HttpError(400, "Der Betreff muss 3 bis 140 Zeichen lang sein.")
        if not 10 <= len(message) <= 5000:
            raise HttpError(400, "Die Nachricht muss 10 bis 5.000 Zeichen lang sein.")
        if data.get("privacy_accepted") is not True:
            raise HttpError(400, "Bitte bestätigen Sie den Datenschutzhinweis.")
        remote_addr = self.client_ip(environ)
        self._verify_turnstile(str(data.get("turnstile_token", "")), remote_addr)
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds")
        attempts = self.db.one(
            "SELECT COUNT(*) count FROM contact_attempts WHERE remote_addr=? AND attempted_at>=?",
            (remote_addr, cutoff),
        )["count"]
        if attempts >= 5:
            raise HttpError(429, "Es wurden zu viele Anfragen versendet. Bitte versuchen Sie es später erneut.")
        old = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")
        self.db.execute("DELETE FROM contact_attempts WHERE attempted_at<?", (old,))
        now = utcnow()
        request_text = f"{subject} {message}".casefold()
        if any(value in request_text for value in ("beta", "pilot", "testzugang", "kostenfrei testen")):
            request_type = "beta"
        elif any(value in request_text for value in ("lizenz", "preis", "kaufen")):
            request_type = "license"
        else:
            request_type = "contact"
        usage_labels = {
            "recurring": "Mehrere Vorhaben pro Schuljahr",
            "possible": "Ein Vorhaben mit möglicher Folgenutzung",
            "one_time": "Voraussichtlich einmaliges Vorhaben",
            "unsure": "Noch offen",
        }
        if request_type == "beta":
            if not 2 <= len(organization) <= 160:
                raise HttpError(400, "Bitte geben Sie Ihre Schule oder Organisation an.")
            if usage_outlook not in usage_labels:
                raise HttpError(400, "Bitte ordnen Sie die geplante weitere Nutzung ein.")
            if pilot_start:
                parse_date(pilot_start, "Geplanter Pilotstart")
        else:
            organization = organization[:160]
            pilot_start = ""
            usage_outlook = ""
        with self.db.transaction() as connection:
            connection.execute(
                "INSERT INTO contact_attempts(remote_addr,attempted_at) VALUES(?,?)",
                (remote_addr, now),
            )
            cursor = connection.execute(
                """INSERT INTO license_requests(name,email,organization,pilot_start,usage_outlook,
                   subject,message,request_type,status,
                   email_status,email_error,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,'new','pending','',?,?)""",
                (name, email, organization, pilot_start, usage_outlook, subject, message, request_type, now, now),
            )
            request_id = int(cursor.lastrowid)
        delivery_message = message
        if request_type == "beta":
            delivery_message += (
                f"\n\nSchule / Organisation: {organization}"
                f"\nGeplanter Start: {pilot_start or 'noch offen'}"
                f"\nGeplante weitere Nutzung: {usage_labels[usage_outlook]}"
            )
        try:
            self._send_contact_email(name, email, subject, delivery_message)
        except HttpError as exc:
            # Ein SMTP-Ausfall darf eine bereits eingegangene Anfrage nicht verlieren.
            self.db.execute(
                "UPDATE license_requests SET email_status='failed',email_error=?,updated_at=? WHERE id=?",
                (exc.message, utcnow(), request_id),
            )
        else:
            self.db.execute(
                "UPDATE license_requests SET email_status='sent',email_error='',updated_at=? WHERE id=?",
                (utcnow(), request_id),
            )
        return {"ok": True, "message": "Vielen Dank. Ihre Anfrage ist bei uns eingegangen."}

    def _verify_turnstile(self, token: str, remote_addr: str) -> None:
        if not self.config.turnstile_sitekey or not self.config.turnstile_secret:
            raise HttpError(503, "Die Menschprüfung ist noch nicht eingerichtet.")
        if not token or len(token) > 2048:
            raise HttpError(400, "Bitte führen Sie die Menschprüfung durch.")
        payload = urlencode({"secret": self.config.turnstile_secret, "response": token, "remoteip": remote_addr}).encode()
        try:
            request = Request("https://challenges.cloudflare.com/turnstile/v0/siteverify", data=payload, method="POST")
            with urlopen(request, timeout=10) as response:
                result = json.loads(response.read(16 * 1024))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise HttpError(503, "Die Menschprüfung ist gerade nicht erreichbar. Bitte versuchen Sie es erneut.") from exc
        if not result.get("success"):
            raise HttpError(400, "Die Menschprüfung war nicht erfolgreich. Bitte versuchen Sie es erneut.")

    def _send_contact_email(self, name: str, reply_to: str, subject: str, body: str) -> None:
        config = self.config
        sender = config.smtp_sender or config.smtp_username
        if not config.contact_recipient or not config.smtp_host or not config.smtp_username or not config.smtp_password or not sender:
            raise HttpError(503, "Der Kontaktversand ist noch nicht eingerichtet. Bitte versuchen Sie es später erneut.")
        mail = EmailMessage()
        mail["From"] = formataddr(("ProjektKontor Kontaktformular", sender))
        mail["To"] = config.contact_recipient
        mail["Reply-To"] = formataddr((name, reply_to))
        mail["Subject"] = f"ProjektKontor Anfrage: {subject}"
        mail.set_content(f"Neue Anfrage über projektkontor.org\n\nName: {name}\nE-Mail: {reply_to}\n\n{body}\n")
        smtp_class = smtplib.SMTP_SSL if config.smtp_use_ssl else smtplib.SMTP
        try:
            with smtp_class(config.smtp_host, config.smtp_port, timeout=15) as smtp:
                if not config.smtp_use_ssl:
                    smtp.starttls()
                smtp.login(config.smtp_username, config.smtp_password)
                smtp.send_message(mail)
        except (OSError, smtplib.SMTPException) as exc:
            raise HttpError(503, "Die Anfrage konnte gerade nicht versendet werden. Bitte versuchen Sie es später erneut.") from exc

    def setup(self, environ, user):
        data = self.body(environ)
        first_name = str(data.get("first_name", "")).strip()
        username = str(data.get("username", "")).strip().lower()
        password = str(data.get("password", ""))
        if self.config.admin_setup_token and not hmac.compare_digest(str(data.get("setup_token", "")), self.config.admin_setup_token):
            raise HttpError(403, "Der Admin-Einrichtungscode ist nicht korrekt.")
        if not first_name or len(first_name) > 80 or not re.fullmatch(r"[a-zA-Z0-9._-]{3,50}", username) or not 10 <= len(password) <= 256:
            raise HttpError(400, "Bitte geben Sie Vorname, einen gültigen Benutzernamen und 10 bis 256 Kennwortzeichen ein.")
        credential = hash_password(password)
        with self.db.transaction() as connection:
            if connection.execute("SELECT 1 FROM users WHERE role='teacher' AND is_owner=1 LIMIT 1").fetchone():
                raise HttpError(409, "Die Ersteinrichtung wurde bereits abgeschlossen.")
            cursor = connection.execute("INSERT INTO users(first_name,username,credential,role,is_owner,active,created_at) VALUES(?,?,?,?,1,1,?)", (first_name,username,credential,"teacher",utcnow()))
            user_id = int(cursor.lastrowid)
        return self.authenticated_login_result(user_id)

    def login(self, environ, user):
        data = self.body(environ)
        login_type = str(data.get("login_type", "auto")).strip().lower()
        username = str(data.get("username", "")).strip().lower()
        password = str(data.get("password", ""))
        if login_type not in {"auto", "student", "teacher", "admin"} or len(username) > 80 or len(password) > 256:
            raise HttpError(401, "Benutzername oder Zugangsdaten sind nicht korrekt.")
        remote_addr = self.client_ip(environ)
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat(timespec="seconds")
        self.db.execute("DELETE FROM login_attempts WHERE attempted_at<?", (cutoff,))
        attempts = self.db.one("SELECT COUNT(*) count FROM login_attempts WHERE username=? AND remote_addr=? AND attempted_at>=?", (username, remote_addr, cutoff))["count"]
        username_attempts = self.db.one("SELECT COUNT(*) count FROM login_attempts WHERE username=? AND attempted_at>=?", (username, cutoff))["count"]
        address_attempts = self.db.one("SELECT COUNT(*) count FROM login_attempts WHERE remote_addr=? AND attempted_at>=?", (remote_addr, cutoff))["count"]
        if attempts >= 8 or username_attempts >= 24 or address_attempts >= 80:
            raise HttpError(429, "Zu viele fehlgeschlagene Anmeldungen. Bitte warten Sie 15 Minuten.")
        account = self.db.one("SELECT * FROM users WHERE username=? COLLATE NOCASE AND active=1", (username,))
        valid = False
        if account:
            matches_login_type = (
                login_type == "auto"
                or (login_type == "student" and account["role"] == "student")
                or (login_type == "teacher" and account["role"] == "teacher" and not account.get("is_owner"))
                or (login_type == "admin" and account["role"] == "teacher" and bool(account.get("is_owner")))
            )
            if matches_login_type and account["role"] == "teacher":
                valid = verify_password(password, account["credential"])
            elif matches_login_type:
                try:
                    entered_code = password.strip().upper()
                    stored_code = self.vault.decrypt(account["credential"]).strip().upper()
                    valid = hmac.compare_digest(entered_code, stored_code)
                except ValueError:
                    valid = False
        if not account or not valid:
            self.db.execute("INSERT INTO login_attempts(username,remote_addr,attempted_at) VALUES(?,?,?)", (username, remote_addr, utcnow()))
            raise HttpError(401, "Benutzername oder Zugangsdaten sind nicht korrekt.")
        self.db.execute("DELETE FROM login_attempts WHERE username=? AND remote_addr=?", (username, remote_addr))
        if account["role"] == "teacher" and password_needs_rehash(account["credential"]):
            self.db.execute("UPDATE users SET credential=? WHERE id=?", (hash_password(password), account["id"]))
        return self.authenticated_login_result(account["id"])

    def change_initial_password(self, environ, user):
        data = self.body(environ)
        token = str(data.get("password_change_token", ""))
        new_password = str(data.get("new_password", ""))
        if not token or len(token) > 200 or not 10 <= len(new_password) <= 256:
            raise HttpError(400, "Das neue Kennwort muss 10 bis 256 Zeichen lang sein.")
        token_hash = hash_session_token(token, self.secret)
        with self.db.transaction() as connection:
            pending = connection.execute(
                "SELECT * FROM pending_logins WHERE token_hash=? AND expires_at>?",
                (token_hash, utcnow()),
            ).fetchone()
            if not pending:
                raise HttpError(401, "Die Kennwortänderung ist abgelaufen. Bitte melden Sie sich erneut an.")
            account = connection.execute(
                "SELECT * FROM users WHERE id=? AND role='teacher' AND is_owner=0 AND active=1 AND must_change_password=1",
                (pending["user_id"],),
            ).fetchone()
            if not account:
                raise HttpError(403, "Für dieses Konto ist keine Initialkennwortänderung vorgesehen.")
            if verify_password(new_password, account["credential"]):
                raise HttpError(400, "Das neue Kennwort muss sich vom Initialkennwort unterscheiden.")
            connection.execute(
                "UPDATE users SET credential=?,must_change_password=0 WHERE id=?",
                (hash_password(new_password), account["id"]),
            )
            connection.execute("DELETE FROM pending_logins WHERE token_hash=?", (token_hash,))
        return self.authenticated_login_result(int(account["id"]))

    def logout(self, environ, user):
        _, session = self.current_user(environ)
        if session:
            self.db.execute("DELETE FROM sessions WHERE id=?", (session["id"],))
        return ({"ok": True}, "application/json; charset=utf-8", None, [("Set-Cookie", self.session_cookie("", 0))])

    def update_own_account(self, environ, user):
        user = self.require_teacher(user)
        _, session = self.current_user(environ)
        data = self.body(environ)
        account = self.db.one("SELECT * FROM users WHERE id=? AND role='teacher' AND active=1", (user["id"],))
        if not account or not session:
            raise HttpError(401, "Bitte melden Sie sich erneut an.")
        first_name = str(data.get("first_name", account["first_name"])).strip()
        username = str(data.get("username", account["username"])).strip().lower()
        new_password = str(data.get("new_password", ""))
        identity_change = username != account["username"] or bool(new_password)
        if not first_name or len(first_name) > 80:
            raise HttpError(400, "Der Name muss 1 bis 80 Zeichen lang sein.")
        if not re.fullmatch(r"[a-zA-Z0-9._-]{3,50}", username):
            raise HttpError(400, "Der Benutzername muss 3 bis 50 zulässige Zeichen enthalten.")
        if not user.get("is_owner") and username != account["username"] and not username.startswith("lehrkraft_"):
            raise HttpError(400, "Benutzernamen für Lehrkräfte müssen mit „lehrkraft_“ beginnen.")
        if new_password and not 10 <= len(new_password) <= 256:
            raise HttpError(400, "Das neue Kennwort muss 10 bis 256 Zeichen lang sein.")
        if identity_change and not verify_password(str(data.get("current_password", "")), account["credential"]):
            raise HttpError(403, "Das aktuelle Kennwort ist nicht korrekt.")
        credential = hash_password(new_password) if new_password else account["credential"]
        try:
            with self.db.transaction() as connection:
                connection.execute("UPDATE users SET first_name=?,username=?,credential=?,must_change_password=? WHERE id=?", (first_name, username, credential, 0 if new_password else account.get("must_change_password", 0), user["id"]))
                if new_password:
                    connection.execute("DELETE FROM sessions WHERE user_id=? AND id<>?", (user["id"], session["id"]))
        except sqlite3.IntegrityError:
            raise HttpError(409, "Dieser Benutzername ist bereits vergeben.")
        return {"ok": True, "user": {"id": user["id"], "first_name": first_name, "username": username, "role": "teacher", "is_owner": user.get("is_owner", 0)}}

    def create_support_request(self, environ, user):
        teacher = self.require_regular_teacher(user)
        retention_cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat(timespec="seconds")
        self.db.execute("DELETE FROM support_requests WHERE created_at<? AND status!='active'", (retention_cutoff,))
        data = self.body(environ)
        phone = re.sub(r"\s+", " ", str(data.get("phone", "")).strip())
        message = str(data.get("message", "")).strip()
        if not 6 <= len(phone) <= 30 or not re.fullmatch(r"[0-9+()/ .-]+", phone):
            raise HttpError(400, "Bitte geben Sie eine gültige Telefonnummer an.")
        if not 10 <= len(message) <= 2000:
            raise HttpError(400, "Bitte beschreiben Sie das Anliegen mit 10 bis 2.000 Zeichen.")
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        code = "".join(secrets.choice(alphabet) for _ in range(8))
        code_hash = hash_session_token(code, self.secret)
        code_expires_at = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat(timespec="seconds")
        self._send_support_email(teacher["first_name"], teacher["username"], phone, message)
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE support_requests SET status='revoked' WHERE teacher_id=? AND status IN ('pending','active')",
                (teacher["id"],),
            )
            cursor = connection.execute(
                """INSERT INTO support_requests(teacher_id,code_hash,phone,message,status,code_expires_at,created_at)
                   VALUES(?,?,?,?,'pending',?,?)""",
                (teacher["id"], code_hash, phone, message, code_expires_at, utcnow()),
            )
            admins = connection.execute(
                "SELECT id FROM users WHERE role='teacher' AND is_owner=1 AND active=1"
            ).fetchall()
            for admin in admins:
                connection.execute(
                    "INSERT INTO notifications(user_id,message,created_at) VALUES(?,?,?)",
                    (admin["id"], f"Supportanfrage von {teacher['first_name']} ({teacher['username']})", utcnow()),
                )
        return {"id": int(cursor.lastrowid), "support_code": code, "code_expires_at": code_expires_at}

    def list_support_requests(self, environ, user):
        teacher = self.require_regular_teacher(user)
        self.db.execute(
            "UPDATE support_requests SET status='expired' WHERE teacher_id=? AND status='pending' AND code_expires_at<=?",
            (teacher["id"], utcnow()),
        )
        self.db.execute(
            "UPDATE support_requests SET status='expired' WHERE teacher_id=? AND status='active' AND access_expires_at<=?",
            (teacher["id"], utcnow()),
        )
        return self.db.all(
            """SELECT id,phone,message,status,code_expires_at,granted_at,access_expires_at,created_at
               FROM support_requests WHERE teacher_id=? ORDER BY created_at DESC LIMIT 20""",
            (teacher["id"],),
        )

    def revoke_support_request(self, environ, user, request_id):
        teacher = self.require_regular_teacher(user)
        with self.db.transaction() as connection:
            cursor = connection.execute(
                "UPDATE support_requests SET status='revoked' WHERE id=? AND teacher_id=? AND status IN ('pending','active')",
                (request_id, teacher["id"]),
            )
            if cursor.rowcount != 1:
                raise HttpError(404, "Aktive Supportfreigabe nicht gefunden.")
        return {"ok": True}

    def redeem_support_code(self, environ, user):
        admin = self.require_owner(user)
        code = re.sub(r"[^A-Z0-9]", "", str(self.body(environ).get("support_code", "")).upper())
        if len(code) != 8:
            raise HttpError(400, "Der Unterstützungscode besteht aus acht Zeichen.")
        code_hash = hash_session_token(code, self.secret)
        request = self.db.one(
            "SELECT * FROM support_requests WHERE code_hash=? AND status='pending' AND code_expires_at>?",
            (code_hash, utcnow()),
        )
        if not request:
            raise HttpError(404, "Der Unterstützungscode ist ungültig oder abgelaufen.")
        access_expires_at = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(timespec="seconds")
        self.db.execute(
            """UPDATE support_requests SET status='active',granted_admin_id=?,granted_at=?,access_expires_at=?
               WHERE id=?""",
            (admin["id"], utcnow(), access_expires_at, request["id"]),
        )
        teacher = self.db.one("SELECT id,first_name,username FROM users WHERE id=?", (request["teacher_id"],))
        return {"teacher": teacher, "access_expires_at": access_expires_at}

    def list_support_grants(self, environ, user):
        admin = self.require_owner(user)
        self.db.execute(
            "UPDATE support_requests SET status='expired' WHERE status='active' AND access_expires_at<=?",
            (utcnow(),),
        )
        grants = self.db.all(
            """SELECT sr.id,sr.teacher_id,sr.phone,sr.message,sr.granted_at,sr.access_expires_at,
                      u.first_name,u.username
               FROM support_requests sr JOIN users u ON u.id=sr.teacher_id
               WHERE sr.granted_admin_id=? AND sr.status='active' AND sr.access_expires_at>?
               ORDER BY sr.access_expires_at DESC""",
            (admin["id"], utcnow()),
        )
        for grant in grants:
            grant["classes"] = self.db.all(
                """SELECT c.id,c.name FROM teacher_classes tc JOIN classes c ON c.id=tc.class_id
                   WHERE tc.teacher_id=? AND c.active=1 ORDER BY c.name""",
                (grant["teacher_id"],),
            )
        return grants

    def _send_support_email(self, teacher_name: str, username: str, phone: str, message: str) -> None:
        config = self.config
        sender = config.smtp_sender or config.smtp_username
        if not config.contact_recipient or not config.smtp_host or not config.smtp_username or not config.smtp_password or not sender:
            raise HttpError(503, "Die Supportanfrage kann noch nicht versendet werden, weil der Mailversand nicht eingerichtet ist.")
        mail = EmailMessage()
        mail["From"] = formataddr(("ProjektKontor Support", sender))
        mail["To"] = config.contact_recipient
        mail["Subject"] = f"ProjektKontor Supportanfrage: {teacher_name}"
        mail.set_content(
            f"Supportanfrage aus ProjektKontor\n\nLehrkraft: {teacher_name}\n"
            f"Benutzername: {username}\nTelefon: {phone}\n\nAnliegen:\n{message}\n\n"
            "Der Unterstützungscode wird von der Lehrkraft beim Rückruf mitgeteilt."
        )
        smtp_class = smtplib.SMTP_SSL if config.smtp_use_ssl else smtplib.SMTP
        try:
            with smtp_class(config.smtp_host, config.smtp_port, timeout=15) as smtp:
                if not config.smtp_use_ssl:
                    smtp.starttls()
                smtp.login(config.smtp_username, config.smtp_password)
                smtp.send_message(mail)
        except (OSError, smtplib.SMTPException) as exc:
            raise HttpError(503, "Die Supportanfrage konnte gerade nicht versendet werden.") from exc

    def license_record(self, license_id: int) -> dict[str, Any] | None:
        record = self.db.one(
            """SELECT l.id,l.order_id,l.follow_up_of,l.follow_up_reminded_at,
                      l.invoice_automation_attempted_on,l.invoice_automation_completed_at,
                      l.invoice_automation_error,l.seat_limit,l.starts_on,l.ends_on,l.status,l.archived_at,
                      l.created_at,l.updated_at,o.customer_name,o.organization,o.email,
                      o.billing_address,o.invoice_reference,o.plan,o.billing_cycle,o.amount_cents,
                      o.payment_status,o.notes
               FROM licenses l JOIN license_orders o ON o.id=l.order_id
               WHERE l.id=?""",
            (license_id,),
        )
        if not record:
            return None
        record["teachers"] = self.db.all(
            """SELECT u.id,u.first_name,u.username,u.active,u.archived_at
               FROM license_teachers lt JOIN users u ON u.id=lt.teacher_id
               WHERE lt.license_id=? ORDER BY u.first_name,u.username""",
            (license_id,),
        )
        record["used_seats"] = sum(1 for teacher in record["teachers"] if not teacher.get("archived_at"))
        record["invoices"] = self.db.all(
            """SELECT id,invoice_number,issued_on,due_on,gross_cents,stored_name,status,
                      emailed_at,downloaded_at,archived_at,created_at
               FROM invoices WHERE license_id=? ORDER BY issued_on DESC,id DESC""",
            (license_id,),
        )
        record["invoice"] = record["invoices"][0] if record["invoices"] else None
        record["cancellation"] = self.db.one(
            "SELECT * FROM license_cancellations WHERE license_id=?", (license_id,)
        )
        record["history"] = self.db.all(
            """SELECT h.id,h.event_type,h.from_plan,h.to_plan,h.from_billing_cycle,
                      h.to_billing_cycle,h.from_amount_cents,h.to_amount_cents,
                      h.from_status,h.to_status,h.changed_at,u.first_name changed_by_name
               FROM license_history h LEFT JOIN users u ON u.id=h.changed_by
               WHERE h.license_id=? ORDER BY h.changed_at DESC,h.id DESC""",
            (license_id,),
        )
        return record

    def list_licenses(self, environ, user):
        self.require_owner(user)
        self.run_license_automation()
        rows = self.db.all("SELECT id FROM licenses ORDER BY created_at DESC,id DESC")
        return [self.license_record(row["id"]) for row in rows]

    def list_license_products(self, environ, user):
        self.require_owner(user)
        products = self.db.all(
            "SELECT * FROM license_products ORDER BY sort_order,name,product_key"
        )
        for product in products:
            product["starts_on"], product["ends_on"] = product_license_dates(product)
        return products

    def update_license_product(self, environ, user, product_key):
        self.require_owner(user)
        current = self.db.one(
            "SELECT * FROM license_products WHERE product_key=?", (product_key,)
        )
        if not current:
            raise HttpError(404, "Lizenzprodukt nicht gefunden.")
        data = self.body(environ)
        name = str(data.get("name", current["name"])).strip()
        duration_unit = str(data.get("duration_unit", current["duration_unit"])).strip()
        try:
            amount_cents = int(data.get("amount_cents", current["amount_cents"]))
            seat_limit = int(data.get("seat_limit", current["seat_limit"]))
            duration_value = int(data.get("duration_value", current["duration_value"]))
        except (TypeError, ValueError) as exc:
            raise HttpError(400, "Preis, Plätze oder Laufzeit sind ungültig.") from exc
        active = int(bool(data.get("active", current["active"])))
        if not 2 <= len(name) <= 80:
            raise HttpError(400, "Der Produktname muss 2 bis 80 Zeichen lang sein.")
        if not 0 <= amount_cents <= 100_000_000 or not 1 <= seat_limit <= 500:
            raise HttpError(400, "Preis oder Anzahl der Plätze liegt außerhalb des zulässigen Bereichs.")
        if duration_unit not in {"days", "months", "years"} or not 1 <= duration_value <= 1200:
            raise HttpError(400, "Die Laufzeit ist ungültig.")
        if current["plan"] == "beta" and amount_cents != 0:
            raise HttpError(400, "Das Beta-Produkt muss kostenfrei bleiben.")
        self.db.execute(
            """UPDATE license_products SET name=?,amount_cents=?,seat_limit=?,duration_unit=?,
                      duration_value=?,active=?,updated_at=? WHERE product_key=?""",
            (name, amount_cents, seat_limit, duration_unit, duration_value, active, utcnow(), product_key),
        )
        product = self.db.one("SELECT * FROM license_products WHERE product_key=?", (product_key,))
        product["starts_on"], product["ends_on"] = product_license_dates(product)
        return product

    def list_license_requests(self, environ, user):
        self.require_owner(user)
        return self.db.all(
            """SELECT id,name,email,organization,pilot_start,usage_outlook,
                      subject,message,request_type,status,email_status,
                      email_error,created_at,updated_at
               FROM license_requests
               ORDER BY CASE status WHEN 'new' THEN 0 WHEN 'in_progress' THEN 1 ELSE 2 END,
                        created_at DESC,id DESC"""
        )

    def update_license_request(self, environ, user, request_id):
        self.require_owner(user)
        if not self.db.one("SELECT id FROM license_requests WHERE id=?", (request_id,)):
            raise HttpError(404, "Anfrage nicht gefunden.")
        status = str(self.body(environ).get("status", "")).strip()
        if status not in {"new", "in_progress", "converted", "closed"}:
            raise HttpError(400, "Der Bearbeitungsstatus ist ungültig.")
        self.db.execute(
            "UPDATE license_requests SET status=?,updated_at=? WHERE id=?",
            (status, utcnow(), request_id),
        )
        return self.db.one(
            """SELECT id,name,email,organization,pilot_start,usage_outlook,
                      subject,message,request_type,status,email_status,
                      email_error,created_at,updated_at FROM license_requests WHERE id=?""",
            (request_id,),
        )

    def parse_license_payload(self, data: dict[str, Any], current: dict[str, Any] | None = None) -> dict[str, Any]:
        plan = str(data.get("plan", current["plan"] if current else "")).strip()
        if plan not in LICENSE_PLANS:
            raise HttpError(400, "Bitte wählen Sie ein gültiges Lizenzmodell.")
        customer_name = str(data.get("customer_name", current["customer_name"] if current else "")).strip()
        organization = str(data.get("organization", current["organization"] if current else "")).strip()
        email = str(data.get("email", current["email"] if current else "")).strip().lower()
        billing_address = str(data.get("billing_address", current["billing_address"] if current else "")).strip()
        invoice_reference = str(data.get("invoice_reference", current["invoice_reference"] if current else "")).strip()
        notes = str(data.get("notes", current["notes"] if current else "")).strip()
        if not 2 <= len(customer_name) <= 120:
            raise HttpError(400, "Bitte geben Sie eine Ansprechperson mit 2 bis 120 Zeichen an.")
        if len(organization) > 160 or len(billing_address) > 1000 or len(invoice_reference) > 120 or len(notes) > 4000:
            raise HttpError(400, "Eine Angabe überschreitet die zulässige Länge.")
        if email and (parseaddr(email)[1] != email or len(email) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email)):
            raise HttpError(400, "Bitte geben Sie eine gültige E-Mail-Adresse an.")
        try:
            amount_cents = int(data.get("amount_cents", current["amount_cents"] if current else LICENSE_PLANS[plan]["amount_cents"]))
            seat_limit = int(data.get("seat_limit", current["seat_limit"] if current else LICENSE_PLANS[plan]["seat_limit"]))
        except (TypeError, ValueError) as exc:
            raise HttpError(400, "Preis oder Anzahl der Zugänge ist ungültig.") from exc
        if not 0 <= amount_cents <= 100_000_000 or not 1 <= seat_limit <= 500:
            raise HttpError(400, "Preis oder Anzahl der Zugänge liegt außerhalb des zulässigen Bereichs.")
        starts_on = parse_date(data.get("starts_on", current["starts_on"] if current else ""), "Lizenzbeginn")
        ends_on = parse_date(data.get("ends_on", current["ends_on"] if current else ""), "Lizenzende")
        if ends_on < starts_on:
            raise HttpError(400, "Das Lizenzende darf nicht vor dem Beginn liegen.")
        status = str(data.get("status", current["status"] if current else "draft")).strip()
        payment_status = str(data.get("payment_status", current["payment_status"] if current else ("not_required" if plan == "beta" else "open"))).strip()
        if status not in LICENSE_STATUSES or payment_status not in PAYMENT_STATUSES:
            raise HttpError(400, "Lizenz- oder Zahlungsstatus ist ungültig.")
        billing_cycle = str(data.get(
            "billing_cycle",
            current.get("billing_cycle", "annual") if current else ("none" if plan == "beta" else "annual"),
        )).strip()
        if billing_cycle not in BILLING_CYCLES:
            raise HttpError(400, "Der Abrechnungszeitraum ist ungültig.")
        if plan == "beta":
            billing_cycle = "none"
            beta_product = self.db.one(
                "SELECT duration_unit,duration_value FROM license_products WHERE product_key='beta'"
            )
            if beta_product:
                _, configured_end = product_license_dates(
                    beta_product, datetime.fromisoformat(starts_on).date()
                )
                if ends_on > configured_end:
                    raise HttpError(
                        400,
                        f"Ein Pilotzugang darf gemäß Produktvorgabe höchstens bis {configured_end} laufen.",
                    )
            if amount_cents != 0:
                raise HttpError(400, "Ein Pilotzugang muss kostenfrei sein.")
            payment_status = "not_required"
        elif plan == "single":
            if amount_cents <= 0:
                raise HttpError(400, "Eine Bezahl-Lizenz benötigt einen Preis größer als 0 Euro.")
            if billing_cycle not in {"monthly", "annual"}:
                raise HttpError(400, "Für eine Einzellizenz ist ein Monats- oder Jahreszugang erforderlich.")
        else:
            if amount_cents <= 0:
                raise HttpError(400, "Eine Bezahl-Lizenz benötigt einen Preis größer als 0 Euro.")
            billing_cycle = "annual"
        today = date.today().isoformat()
        if status == "active" and ends_on < today:
            status = "expired"
        raw_teacher_ids = data.get("teacher_ids")
        if raw_teacher_ids is None and current:
            teacher_ids = [teacher["id"] for teacher in current["teachers"]]
        else:
            try:
                teacher_ids = list(dict.fromkeys(int(value) for value in (raw_teacher_ids or [])))
            except (TypeError, ValueError) as exc:
                raise HttpError(400, "Die Auswahl der Lehrkraftzugänge ist ungültig.") from exc
        if len(teacher_ids) > seat_limit:
            raise HttpError(400, f"Für diese Lizenz sind höchstens {seat_limit} Lehrkraftzugänge vorgesehen.")
        if teacher_ids:
            placeholders = ",".join("?" for _ in teacher_ids)
            accounts = self.db.all(
                f"""SELECT id FROM users WHERE id IN ({placeholders}) AND role='teacher'
                       AND is_owner=0 AND archived_at IS NULL""",
                tuple(teacher_ids),
            )
            if len(accounts) != len(teacher_ids):
                raise HttpError(400, "Mindestens ein ausgewählter Lehrkraftzugang ist ungültig.")
        return {
            "customer_name": customer_name, "organization": organization, "email": email,
            "billing_address": billing_address, "invoice_reference": invoice_reference,
            "plan": plan, "amount_cents": amount_cents, "payment_status": payment_status,
            "billing_cycle": billing_cycle,
            "notes": notes, "seat_limit": seat_limit, "starts_on": starts_on,
            "ends_on": ends_on, "status": status, "teacher_ids": teacher_ids,
        }

    def ensure_license_assignments_available(self, values: dict[str, Any], license_id: int | None = None) -> None:
        if values["status"] != "active":
            return
        for teacher_id in values["teacher_ids"]:
            params: list[Any] = [teacher_id, values["ends_on"], values["starts_on"]]
            sql = """SELECT l.id FROM license_teachers lt JOIN licenses l ON l.id=lt.license_id
                     WHERE lt.teacher_id=? AND l.status='active'
                       AND l.starts_on<=? AND l.ends_on>=?"""
            if license_id is not None:
                sql += " AND l.id<>?"
                params.append(license_id)
            if self.db.one(sql + " LIMIT 1", tuple(params)):
                raise HttpError(409, "Mindestens ein Lehrkraftzugang ist bereits einer zeitlich überschneidenden aktiven Lizenz zugeordnet.")

    @staticmethod
    def transfer_teacher_assignments(
        connection: sqlite3.Connection, teacher_ids: list[int], target_license_id: int
    ) -> None:
        """Keep one current managed license per teacher and preserve expired history."""
        if not teacher_ids:
            return
        placeholders = ",".join("?" for _ in teacher_ids)
        connection.execute(
            f"""DELETE FROM license_teachers
                WHERE teacher_id IN ({placeholders}) AND license_id<>?
                  AND license_id IN (
                      SELECT id FROM licenses WHERE status IN ('draft','active','suspended')
                  )""",
            (*teacher_ids, target_license_id),
        )
        connection.execute(
            f"DELETE FROM sessions WHERE user_id IN ({placeholders})",
            tuple(teacher_ids),
        )

    def create_license(self, environ, user):
        owner = self.require_owner(user)
        data = self.body(environ)
        values = self.parse_license_payload(data)
        follow_up_of = int(data.get("follow_up_of") or 0) or None
        source = self.license_record(follow_up_of) if follow_up_of else None
        if follow_up_of:
            if not source or source.get("archived_at") or source["status"] != "active" or source["plan"] == "beta":
                raise HttpError(409, "Eine Folgelizenz kann nur für eine aktive Bezahl-Lizenz geplant werden.")
            expected_start = (date.fromisoformat(source["ends_on"]) + timedelta(days=1)).isoformat()
            if values["starts_on"] != expected_start:
                raise HttpError(400, f"Die Folgelizenz muss am {expected_start} beginnen.")
            values["status"] = "draft"
            values["payment_status"] = "not_required" if values["amount_cents"] == 0 else "open"
            for teacher_id in values["teacher_ids"]:
                overlap = self.db.one(
                    """SELECT l.id FROM license_teachers lt JOIN licenses l ON l.id=lt.license_id
                       WHERE lt.teacher_id=? AND l.id<>? AND l.archived_at IS NULL
                         AND (l.status='active' OR (l.status='draft' AND l.follow_up_of IS NOT NULL))
                         AND l.starts_on<=? AND l.ends_on>=? LIMIT 1""",
                    (teacher_id, follow_up_of, values["ends_on"], values["starts_on"]),
                )
                if overlap:
                    raise HttpError(409, "Mindestens ein Lehrkraftzugang hat in diesem Zeitraum bereits eine Lizenz.")
        create_teacher = bool(data.get("create_teacher"))
        if follow_up_of and create_teacher:
            raise HttpError(400, "Bei einer Folgelizenz kann kein neuer Lehrkraftzugang angelegt werden.")
        teacher_name = str(data.get("new_teacher_first_name", "")).strip()
        teacher_username = str(data.get("new_teacher_username", "")).strip().lower()
        teacher_email = str(data.get("new_teacher_email", "")).strip().lower()
        initial_password = generate_access_code(12) if create_teacher else None
        if create_teacher:
            if not teacher_name or len(teacher_name) > 80:
                raise HttpError(400, "Der Name des neuen Lehrkraftzugangs muss 1 bis 80 Zeichen lang sein.")
            if not teacher_username.startswith("lehrkraft_") or not re.fullmatch(
                r"lehrkraft_[a-zA-Z0-9._-]{3,40}", teacher_username
            ):
                raise HttpError(
                    400,
                    "Der Benutzername muss mit „lehrkraft_“ beginnen und danach 3 bis 40 zulässige Zeichen enthalten.",
                )
            if parseaddr(teacher_email)[1] != teacher_email or not re.fullmatch(
                r"[^\s@]+@[^\s@]+\.[^\s@]+", teacher_email
            ):
                raise HttpError(400, "Bitte geben Sie eine gültige E-Mail-Adresse für den neuen Lehrkraftzugang an.")
            if len(values["teacher_ids"]) + 1 > values["seat_limit"]:
                raise HttpError(
                    400,
                    "Für den neuen Lehrkraftzugang ist kein freier Platz in dieser Lizenz vorgesehen.",
                )
        now = utcnow()
        created_teacher_id: int | None = None
        try:
            with self.db.transaction() as connection:
                order = connection.execute(
                    """INSERT INTO license_orders(customer_name,organization,email,billing_address,
                       invoice_reference,plan,billing_cycle,amount_cents,payment_status,notes,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (values["customer_name"], values["organization"], values["email"], values["billing_address"],
                     values["invoice_reference"], values["plan"], values["billing_cycle"], values["amount_cents"],
                     values["payment_status"], values["notes"], now, now),
                )
                license_row = connection.execute(
                    """INSERT INTO licenses(order_id,follow_up_of,seat_limit,starts_on,ends_on,status,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (int(order.lastrowid), follow_up_of, values["seat_limit"], values["starts_on"],
                     values["ends_on"], values["status"], now, now),
                )
                license_id = int(license_row.lastrowid)
                assigned_teacher_ids = list(values["teacher_ids"])
                if create_teacher:
                    teacher_row = connection.execute(
                        """INSERT INTO users(
                               first_name,username,email,credential,role,is_owner,must_change_password,
                               license_managed,active,created_at
                           ) VALUES(?,?,?,?,'teacher',0,1,1,1,?)""",
                        (teacher_name, teacher_username, teacher_email, hash_password(initial_password), now),
                    )
                    created_teacher_id = int(teacher_row.lastrowid)
                    assigned_teacher_ids.append(created_teacher_id)
                if not follow_up_of:
                    self.transfer_teacher_assignments(connection, assigned_teacher_ids, license_id)
                for teacher_id in assigned_teacher_ids:
                    connection.execute(
                        "INSERT INTO license_teachers(license_id,teacher_id,assigned_at) VALUES(?,?,?)",
                        (license_id, teacher_id, now),
                    )
                    connection.execute("UPDATE users SET license_managed=1 WHERE id=?", (teacher_id,))
                connection.execute(
                    """INSERT INTO license_history(
                           license_id,changed_by,event_type,from_plan,to_plan,from_billing_cycle,
                           to_billing_cycle,from_amount_cents,to_amount_cents,from_status,to_status,changed_at
                       ) VALUES(?,?,'created',NULL,?,NULL,?,NULL,?,NULL,?,?)""",
                    (
                        license_id, owner["id"], values["plan"], values["billing_cycle"],
                        values["amount_cents"], values["status"], now,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            if create_teacher:
                raise HttpError(409, "Dieser Benutzername ist bereits vergeben.") from exc
            raise
        result = self.license_record(license_id)
        if created_teacher_id is not None:
            result["created_teacher"] = {
                "id": created_teacher_id,
                "first_name": teacher_name,
                "username": teacher_username,
                "email": teacher_email,
                "initial_password": initial_password,
            }
        return result

    def update_license(self, environ, user, license_id):
        owner = self.require_owner(user)
        current = self.license_record(license_id)
        if not current:
            raise HttpError(404, "Lizenz nicht gefunden.")
        if current.get("archived_at"):
            raise HttpError(409, "Eine archivierte Lizenz kann nicht mehr bearbeitet werden.")
        values = self.parse_license_payload(self.body(environ), current)
        is_follow_up = bool(current.get("follow_up_of"))
        if is_follow_up:
            source = self.license_record(current["follow_up_of"])
            if not source:
                raise HttpError(409, "Die ursprüngliche Lizenz der Vormerkung wurde nicht gefunden.")
            expected_start = (date.fromisoformat(source["ends_on"]) + timedelta(days=1)).isoformat()
            if values["starts_on"] != expected_start:
                raise HttpError(400, f"Die Folgelizenz muss am {expected_start} beginnen.")
            if current["status"] == "draft":
                values["status"] = "draft"
            for teacher_id in values["teacher_ids"]:
                overlap = self.db.one(
                    """SELECT l.id FROM license_teachers lt JOIN licenses l ON l.id=lt.license_id
                       WHERE lt.teacher_id=? AND l.id NOT IN (?,?) AND l.archived_at IS NULL
                         AND (l.status='active' OR (l.status='draft' AND l.follow_up_of IS NOT NULL))
                         AND l.starts_on<=? AND l.ends_on>=? LIMIT 1""",
                    (teacher_id, license_id, current["follow_up_of"], values["ends_on"], values["starts_on"]),
                )
                if overlap:
                    raise HttpError(409, "Mindestens ein Lehrkraftzugang hat in diesem Zeitraum bereits eine Lizenz.")
        commercial_fields = ("plan", "billing_cycle", "amount_cents", "seat_limit", "starts_on", "ends_on")
        if current["status"] == "active" and current["plan"] != "beta":
            if any(current[field] != values[field] for field in commercial_fields):
                raise HttpError(409, "Eine aktive Bezahl-Lizenz kann nicht unmittelbar geändert werden. Planen Sie stattdessen eine Folgelizenz.")
        if current["status"] == "active" and current["payment_status"] == "paid":
            if values["payment_status"] not in {"paid", "refunded", "cancelled"}:
                raise HttpError(409, "Eine bezahlte Lizenz kann nur als erstattet oder storniert gekennzeichnet werden.")
        now = utcnow()
        previous_teacher_ids = {teacher["id"] for teacher in current["teachers"]}
        affected_teacher_ids = previous_teacher_ids | set(values["teacher_ids"])
        access_changed = (
            current["status"] != values["status"]
            or current["starts_on"] != values["starts_on"]
            or current["ends_on"] != values["ends_on"]
            or previous_teacher_ids != set(values["teacher_ids"])
        )
        with self.db.transaction() as connection:
            connection.execute(
                """UPDATE license_orders SET customer_name=?,organization=?,email=?,billing_address=?,
                   invoice_reference=?,plan=?,billing_cycle=?,amount_cents=?,payment_status=?,notes=?,updated_at=?
                   WHERE id=?""",
                (values["customer_name"], values["organization"], values["email"], values["billing_address"],
                 values["invoice_reference"], values["plan"], values["billing_cycle"], values["amount_cents"],
                 values["payment_status"], values["notes"], now, current["order_id"]),
            )
            connection.execute(
                "UPDATE licenses SET seat_limit=?,starts_on=?,ends_on=?,status=?,updated_at=? WHERE id=?",
                (values["seat_limit"], values["starts_on"], values["ends_on"], values["status"], now, license_id),
            )
            connection.execute("DELETE FROM license_teachers WHERE license_id=?", (license_id,))
            if not is_follow_up:
                self.transfer_teacher_assignments(connection, values["teacher_ids"], license_id)
            for teacher_id in values["teacher_ids"]:
                connection.execute(
                    "INSERT INTO license_teachers(license_id,teacher_id,assigned_at) VALUES(?,?,?)",
                    (license_id, teacher_id, now),
                )
                connection.execute("UPDATE users SET license_managed=1 WHERE id=?", (teacher_id,))
            if affected_teacher_ids and access_changed:
                placeholders = ",".join("?" for _ in affected_teacher_ids)
                connection.execute(
                    f"DELETE FROM sessions WHERE user_id IN ({placeholders})",
                    tuple(affected_teacher_ids),
                )
            event_type = "converted" if current["plan"] == "beta" and values["plan"] != "beta" else "updated"
            connection.execute(
                """INSERT INTO license_history(
                       license_id,changed_by,event_type,from_plan,to_plan,from_billing_cycle,
                       to_billing_cycle,from_amount_cents,to_amount_cents,from_status,to_status,changed_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    license_id, owner["id"], event_type, current["plan"], values["plan"],
                    current["billing_cycle"], values["billing_cycle"], current["amount_cents"],
                    values["amount_cents"], current["status"], values["status"], now,
                ),
            )
        return self.license_record(license_id)

    @staticmethod
    def add_months(value: date, months: int) -> date:
        month_index = value.month - 1 + months
        year = value.year + month_index // 12
        month = month_index % 12 + 1
        return date(year, month, min(value.day, calendar.monthrange(year, month)[1]))

    @classmethod
    def completed_contract_months(cls, starts_on: date, ends_on: date, effective_on: date) -> tuple[int, int]:
        contract_end_exclusive = ends_on + timedelta(days=1)
        effective_end_exclusive = min(effective_on, ends_on) + timedelta(days=1)
        total = used = 0
        while cls.add_months(starts_on, total + 1) <= contract_end_exclusive:
            total += 1
        while cls.add_months(starts_on, used + 1) <= effective_end_exclusive:
            used += 1
        return used, max(total, 1)

    def courtesy_cancel_license(self, environ, user, license_id):
        owner = self.require_owner(user)
        current = self.license_record(license_id)
        if not current:
            raise HttpError(404, "Lizenz nicht gefunden.")
        if current["plan"] == "beta" or int(current["amount_cents"]) == 0:
            raise HttpError(409, "Ein kostenfreier Testzugang benötigt keine Kulanzstornierung.")
        if current["status"] != "active" or current.get("archived_at"):
            raise HttpError(409, "Nur eine aktive Bezahl-Lizenz kann aus Kulanz beendet werden.")
        if current.get("cancellation"):
            raise HttpError(409, "Für diese Lizenz wurde bereits eine Kulanzbeendigung erfasst.")
        data = self.body(environ)
        if data.get("confirm_courtesy_cancellation") is not True:
            raise HttpError(400, "Bestätigen Sie die Kulanzbeendigung ausdrücklich.")
        try:
            effective_on = date.fromisoformat(str(data.get("effective_on", "")))
        except ValueError as exc:
            raise HttpError(400, "Der Stichtag ist ungültig.") from exc
        starts_on, ends_on = date.fromisoformat(current["starts_on"]), date.fromisoformat(current["ends_on"])
        if effective_on < starts_on or effective_on > date.today():
            raise HttpError(400, "Der Stichtag muss innerhalb der bisherigen Laufzeit und darf nicht in der Zukunft liegen.")

        monthly = current["billing_cycle"] == "monthly"
        used_months, total_months = self.completed_contract_months(starts_on, ends_on, effective_on)
        retained = 0 if monthly else round(int(current["amount_cents"]) * used_months / total_months)
        credit = int(current["amount_cents"]) - retained
        corrected_end = effective_on if monthly or used_months == 0 else self.add_months(starts_on, used_months) - timedelta(days=1)
        notes = str(data.get("notes", "")).strip()[:1000]
        now = utcnow()
        invoice_states = [dict(row) for row in self.db.all(
            """SELECT i.id,i.status,i.archived_at,r.retired_at
                 FROM invoices i LEFT JOIN invoice_number_registry r ON r.invoice_id=i.id
                WHERE i.license_id=?""", (license_id,)
        )]
        original_paid = any(row["status"] == "paid" and not row["archived_at"] for row in invoice_states)

        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE invoices SET status='cancelled',archived_at=COALESCE(archived_at,?) WHERE license_id=?",
                (now, license_id),
            )
            connection.execute(
                "UPDATE invoice_number_registry SET retired_at=COALESCE(retired_at,?) WHERE invoice_id IN (SELECT id FROM invoices WHERE license_id=?)",
                (now, license_id),
            )
            connection.execute(
                "UPDATE license_orders SET amount_cents=?,payment_status=?,updated_at=? WHERE id=?",
                (retained, "refunded" if original_paid and retained == 0 else "cancelled", now, current["order_id"]),
            )
            connection.execute(
                "UPDATE licenses SET ends_on=?,status='cancelled',updated_at=? WHERE id=?",
                (corrected_end.isoformat(), now, license_id),
            )
            connection.execute(
                """INSERT INTO license_cancellations(
                       license_id,effective_on,mode,original_amount_cents,retained_amount_cents,
                       credit_amount_cents,original_payment_status,notes,created_by,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    license_id, effective_on.isoformat(), "monthly_full" if monthly else "annual_prorated",
                    current["amount_cents"], retained, credit, current["payment_status"], notes,
                    owner["id"], now,
                ),
            )
            connection.execute(
                """INSERT INTO license_history(
                       license_id,changed_by,event_type,from_plan,to_plan,from_billing_cycle,
                       to_billing_cycle,from_amount_cents,to_amount_cents,from_status,to_status,changed_at
                   ) VALUES(?,?,'updated',?,?,?,?,?,?,?,?,?)""",
                (
                    license_id, owner["id"], current["plan"], current["plan"], current["billing_cycle"],
                    current["billing_cycle"], current["amount_cents"], retained, current["status"],
                    "cancelled", now,
                ),
            )

        corrected_invoice = None
        if retained > 0:
            payload = json_bytes({"confirm_zero_invoice": False})
            invoice_environ = {"CONTENT_LENGTH": str(len(payload)), "wsgi.input": io.BytesIO(payload)}
            try:
                corrected_invoice = self.create_invoice(invoice_environ, owner, license_id)
                if original_paid:
                    with self.db.transaction() as connection:
                        connection.execute("UPDATE invoices SET status='paid' WHERE id=?", (corrected_invoice["id"],))
                        self.sync_license_payment_status(connection, license_id)
            except Exception:
                with self.db.transaction() as connection:
                    connection.execute("DELETE FROM license_cancellations WHERE license_id=?", (license_id,))
                    connection.execute(
                        "DELETE FROM license_history WHERE license_id=? AND changed_at=?", (license_id, now)
                    )
                    connection.execute(
                        "UPDATE licenses SET ends_on=?,status=?,updated_at=? WHERE id=?",
                        (current["ends_on"], current["status"], current["updated_at"], license_id),
                    )
                    connection.execute(
                        "UPDATE license_orders SET amount_cents=?,payment_status=?,updated_at=? WHERE id=?",
                        (current["amount_cents"], current["payment_status"], current["updated_at"], current["order_id"]),
                    )
                    for state in invoice_states:
                        connection.execute(
                            "UPDATE invoices SET status=?,archived_at=? WHERE id=?",
                            (state["status"], state["archived_at"], state["id"]),
                        )
                        connection.execute(
                            "UPDATE invoice_number_registry SET retired_at=? WHERE invoice_id=?",
                            (state["retired_at"], state["id"]),
                        )
                raise
        result = self.license_record(license_id)
        result["corrected_invoice"] = corrected_invoice
        result["refund_due_cents"] = credit if original_paid else 0
        return result

    def archive_license(self, environ, user, license_id):
        owner = self.require_owner(user)
        current = self.license_record(license_id)
        if not current:
            raise HttpError(404, "Lizenz nicht gefunden.")
        if current.get("archived_at"):
            return current
        now = utcnow()
        teacher_ids = [teacher["id"] for teacher in current["teachers"]]
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE licenses SET status='cancelled',archived_at=?,updated_at=? WHERE id=?",
                (now, now, license_id),
            )
            connection.execute(
                """INSERT INTO license_history(
                       license_id,changed_by,event_type,from_plan,to_plan,from_billing_cycle,
                       to_billing_cycle,from_amount_cents,to_amount_cents,from_status,to_status,changed_at
                   ) VALUES(?,?,'updated',?,?,?,?,?,?,?,?,?)""",
                (
                    license_id, owner["id"], current["plan"], current["plan"],
                    current["billing_cycle"], current["billing_cycle"], current["amount_cents"],
                    current["amount_cents"], current["status"], "cancelled", now,
                ),
            )
            if teacher_ids:
                placeholders = ",".join("?" for _ in teacher_ids)
                connection.execute(
                    f"DELETE FROM sessions WHERE user_id IN ({placeholders})", tuple(teacher_ids)
                )
        return self.license_record(license_id)

    def delete_license(self, environ, user, license_id):
        self.require_owner(user)
        if self.body(environ).get("confirm_permanent_delete") is not True:
            raise HttpError(400, "Bestätigen Sie das endgültige Löschen der Lizenz ausdrücklich.")
        current = self.license_record(license_id)
        if not current:
            raise HttpError(404, "Lizenz nicht gefunden.")
        dependent = self.db.one(
            "SELECT id FROM licenses WHERE follow_up_of=? ORDER BY id LIMIT 1", (license_id,)
        )
        if dependent:
            raise HttpError(
                409,
                "Für diese Lizenz besteht noch eine Folgelizenz. Löschen Sie zuerst die Vormerkung.",
            )
        invoice_files = [self.config.report_dir / invoice["stored_name"] for invoice in current["invoices"]]
        teacher_ids = [teacher["id"] for teacher in current["teachers"]]
        with self.db.transaction() as connection:
            if teacher_ids:
                placeholders = ",".join("?" for _ in teacher_ids)
                connection.execute(
                    f"DELETE FROM sessions WHERE user_id IN ({placeholders})", tuple(teacher_ids)
                )
            connection.execute(
                """UPDATE invoice_number_registry SET invoice_id=NULL,
                          retired_at=COALESCE(retired_at,?)
                   WHERE invoice_id IN (SELECT id FROM invoices WHERE license_id=?)""",
                (utcnow(), license_id),
            )
            connection.execute("DELETE FROM invoices WHERE license_id=?", (license_id,))
            connection.execute("DELETE FROM license_history WHERE license_id=?", (license_id,))
            connection.execute("DELETE FROM license_teachers WHERE license_id=?", (license_id,))
            connection.execute("DELETE FROM licenses WHERE id=?", (license_id,))
            connection.execute("DELETE FROM license_orders WHERE id=?", (current["order_id"],))
        for path in invoice_files:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                warnings.warn(f"Rechnungsdatei konnte nach dem Löschen nicht entfernt werden: {path}")
        return {"ok": True, "deleted_id": license_id}

    def get_invoice_settings(self, environ, user):
        self.require_owner(user)
        return self.db.one("SELECT * FROM invoice_settings WHERE id=1")

    def update_invoice_settings(self, environ, user):
        self.require_owner(user)
        current = self.db.one("SELECT * FROM invoice_settings WHERE id=1")
        data = self.body(environ)
        business_name = str(data.get("business_name", current["business_name"])).strip()
        proprietor_name = str(data.get("proprietor_name", current["proprietor_name"])).strip()
        address = str(data.get("address", current["address"])).strip()
        email = str(data.get("email", current["email"])).strip().lower()
        tax_identifier = str(data.get("tax_identifier", current["tax_identifier"])).strip()
        tax_mode = str(data.get("tax_mode", current["tax_mode"])).strip()
        invoice_prefix = str(data.get("invoice_prefix", current["invoice_prefix"])).strip().upper()
        iban = re.sub(r"\s+", "", str(data.get("iban", current["iban"]))).upper()
        bic = re.sub(r"\s+", "", str(data.get("bic", current["bic"]))).upper()
        bank_name = str(data.get("bank_name", current["bank_name"])).strip()
        try:
            vat_rate_basis_points = int(data.get("vat_rate_basis_points", current["vat_rate_basis_points"]))
            payment_terms_days = int(data.get("payment_terms_days", current["payment_terms_days"]))
        except (TypeError, ValueError) as exc:
            raise HttpError(400, "Steuersatz oder Zahlungsziel ist ungültig.") from exc
        if not 2 <= len(business_name) <= 160 or not 5 <= len(address) <= 1000:
            raise HttpError(400, "Name und vollständige Anschrift des Rechnungsstellers sind erforderlich.")
        if not 3 <= len(tax_identifier) <= 40:
            raise HttpError(400, "Bitte geben Sie eine gültige steuerliche Kennung an.")
        if email and (parseaddr(email)[1] != email or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email)):
            raise HttpError(400, "Die E-Mail-Adresse des Rechnungsstellers ist ungültig.")
        if tax_mode not in {"small_business", "standard"} or not 0 <= vat_rate_basis_points <= 10000:
            raise HttpError(400, "Die umsatzsteuerliche Einstellung ist ungültig.")
        if tax_mode == "small_business":
            # Bei Anwendung des § 19 UStG darf ein parallel gespeicherter
            # Regelsatz weder in die Berechnung noch in den Beleg gelangen.
            vat_rate_basis_points = 0
        if not re.fullmatch(r"[A-Z0-9-]{1,12}", invoice_prefix):
            raise HttpError(400, "Das Rechnungspräfix darf nur Großbuchstaben, Ziffern und Bindestriche enthalten.")
        if payment_terms_days not in {0, 7, 14, 30}:
            raise HttpError(400, "Bitte wählen Sie als Zahlungsziel Sofort, 7, 14 oder 30 Tage.")
        if iban and not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{10,30}", iban):
            raise HttpError(400, "Die IBAN ist formal ungültig.")
        if bic and not re.fullmatch(r"[A-Z0-9]{8}([A-Z0-9]{3})?", bic):
            raise HttpError(400, "Die BIC ist formal ungültig.")
        self.db.execute(
            """UPDATE invoice_settings SET business_name=?,proprietor_name=?,address=?,email=?,
               tax_identifier=?,tax_mode=?,vat_rate_basis_points=?,invoice_prefix=?,
               payment_terms_days=?,iban=?,bic=?,bank_name=?,updated_at=? WHERE id=1""",
            (
                business_name, proprietor_name, address, email, tax_identifier, tax_mode,
                vat_rate_basis_points, invoice_prefix, payment_terms_days, iban, bic,
                bank_name, utcnow(),
            ),
        )
        return self.db.one("SELECT * FROM invoice_settings WHERE id=1")

    @staticmethod
    def invoice_description(license_record: dict[str, Any]) -> str:
        if license_record.get("cancellation"):
            cancellation = license_record["cancellation"]
            return (
                "Korrigierte ProjektKontor-Lizenz nach Kulanzbeendigung – "
                f"berechnet werden {cancellation['retained_amount_cents'] / 100:.2f} EUR "
                "für vollständig genutzte Vertragsmonate"
            )
        if license_record["plan"] == "beta":
            return "ProjektKontor Pilotzugang - kostenfreier vierwöchiger Praxistest"
        if license_record["plan"] == "single":
            cycle = "Monatszugang" if license_record.get("billing_cycle") == "monthly" else "Jahreszugang"
            return f"ProjektKontor Einzellizenz - {cycle} für eine Lehrkraft"
        if license_record["plan"] == "department":
            return f"ProjektKontor Fachbereichslizenz für bis zu {license_record['seat_limit']} Lehrkräfte"
        if license_record["plan"] == "school":
            return f"ProjektKontor Schullizenz für bis zu {license_record['seat_limit']} Lehrkräfte"
        return "ProjektKontor Lizenz"

    def create_invoice(self, environ, user, license_id):
        owner = self.require_owner(user)
        license_record = self.license_record(license_id)
        if not license_record:
            raise HttpError(404, "Lizenz nicht gefunden.")
        if license_record.get("archived_at"):
            raise HttpError(409, "Für eine archivierte Lizenz kann keine neue Rechnung erstellt werden.")
        data = self.body(environ)
        is_zero_invoice = int(license_record["amount_cents"]) == 0
        if is_zero_invoice and data.get("confirm_zero_invoice") is not True:
            raise HttpError(400, "Bestätigen Sie ausdrücklich, dass wirklich eine Nullrechnung erstellt werden soll.")
        if not license_record["billing_address"].strip():
            raise HttpError(400, "Ergänzen Sie vor der Rechnungserstellung die vollständige Rechnungsanschrift.")
        settings = self.db.one("SELECT * FROM invoice_settings WHERE id=1")
        if not settings or not settings["business_name"] or not settings["address"] or not settings["tax_identifier"]:
            raise HttpError(400, "Vervollständigen Sie zunächst die Angaben zum Rechnungssteller.")
        issued = date.today()
        gross_cents = int(license_record["amount_cents"])
        now = utcnow()
        stored_name = ""
        with self.db.transaction() as connection:
            locked_settings = dict(connection.execute("SELECT * FROM invoice_settings WHERE id=1").fetchone())
            if not is_zero_invoice and not locked_settings["iban"]:
                raise HttpError(400, "Ergänzen Sie vor der Rechnungserstellung die Bankverbindung des Rechnungsstellers.")
            due = issued + timedelta(days=locked_settings["payment_terms_days"])
            if is_zero_invoice:
                net_cents = 0
                vat_cents = 0
                vat_rate = 0
                tax_note = "Kostenfreie Leistung. Es wird kein Entgelt berechnet."
            elif locked_settings["tax_mode"] == "standard" and locked_settings["vat_rate_basis_points"] > 0:
                net_cents = round(
                    gross_cents * 10000 / (10000 + locked_settings["vat_rate_basis_points"])
                )
                vat_cents = gross_cents - net_cents
                vat_rate = locked_settings["vat_rate_basis_points"]
                tax_note = ""
            else:
                net_cents = gross_cents
                vat_cents = 0
                vat_rate = 0
                tax_note = "Gemäß § 19 UStG wird keine Umsatzsteuer berechnet."
            invoice_number = f"{locked_settings['invoice_prefix']}-{issued.year}-{locked_settings['next_invoice_number']:04d}"
            stored_name = f"rechnung-{invoice_number.lower()}.pdf"
            snapshot = {
                "invoice_number": invoice_number,
                "issued_on": issued.isoformat(),
                "service_on": license_record["starts_on"],
                "due_on": due.isoformat(),
                "customer_name": license_record["customer_name"],
                "organization": license_record["organization"],
                "billing_address": license_record["billing_address"],
                "invoice_reference": license_record["invoice_reference"],
                "description": self.invoice_description(license_record),
                "net_cents": net_cents,
                "vat_rate_basis_points": vat_rate,
                "vat_cents": vat_cents,
                "gross_cents": gross_cents,
                "issuer_name": locked_settings["business_name"],
                "issuer_proprietor": locked_settings["proprietor_name"],
                "issuer_address": locked_settings["address"],
                "issuer_email": locked_settings["email"],
                "issuer_tax_identifier": locked_settings["tax_identifier"],
                "tax_note": tax_note,
                "iban": locked_settings["iban"],
                "bic": locked_settings["bic"],
                "bank_name": locked_settings["bank_name"],
                "license_starts_on": license_record["starts_on"],
                "license_ends_on": license_record["ends_on"],
            }
            pdf_bytes = generate_invoice_pdf(snapshot)
            if not pdf_bytes.startswith(b"%PDF-") or b"%%EOF" not in pdf_bytes[-1024:]:
                raise HttpError(500, "Die PDF-Rechnung konnte nicht vollständig erzeugt werden.")
            destination = self.config.report_dir / stored_name
            temporary = destination.with_suffix(".tmp")
            temporary.write_bytes(pdf_bytes)
            temporary.replace(destination)
            cursor = connection.execute(
                """INSERT INTO invoices(
                       license_id,invoice_number,issued_on,service_on,due_on,customer_name,
                       organization,email,billing_address,invoice_reference,description,
                       net_cents,vat_rate_basis_points,vat_cents,gross_cents,issuer_name,
                       issuer_proprietor,issuer_address,issuer_email,issuer_tax_identifier,
                       tax_note,iban,bic,bank_name,stored_name,status,emailed_at,downloaded_at,
                       created_by,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,?,?)""",
                (
                    license_id, invoice_number, issued.isoformat(), license_record["starts_on"],
                    due.isoformat(), license_record["customer_name"], license_record["organization"],
                    license_record["email"], license_record["billing_address"],
                    license_record["invoice_reference"], snapshot["description"], net_cents,
                    vat_rate, vat_cents, gross_cents, snapshot["issuer_name"],
                    snapshot["issuer_proprietor"], snapshot["issuer_address"],
                    snapshot["issuer_email"], snapshot["issuer_tax_identifier"], tax_note,
                    snapshot["iban"], snapshot["bic"], snapshot["bank_name"], stored_name,
                    "paid" if is_zero_invoice else "open", owner["id"], now,
                ),
            )
            connection.execute(
                "UPDATE invoice_settings SET next_invoice_number=next_invoice_number+1,updated_at=? WHERE id=1",
                (now,),
            )
            if is_zero_invoice:
                connection.execute(
                    "UPDATE license_orders SET payment_status='not_required',updated_at=? WHERE id=?",
                    (now, license_record["order_id"]),
                )
            else:
                connection.execute(
                    "UPDATE license_orders SET payment_status='open',updated_at=? WHERE id=?",
                    (now, license_record["order_id"]),
                )
            invoice_id = int(cursor.lastrowid)
            connection.execute(
                """INSERT INTO invoice_number_registry(invoice_number,invoice_id,reserved_at,retired_at)
                   VALUES(?,?,?,NULL)""",
                (invoice_number, invoice_id, now),
            )
        return self.db.one(
            """SELECT id,invoice_number,issued_on,due_on,gross_cents,stored_name,status,
                      emailed_at,downloaded_at,archived_at,created_at FROM invoices WHERE id=?""",
            (invoice_id,),
        )

    def sync_license_payment_status(self, connection: sqlite3.Connection, license_id: int) -> str:
        license_row = connection.execute(
            """SELECT l.order_id,o.amount_cents FROM licenses l JOIN license_orders o ON o.id=l.order_id
                 WHERE l.id=?""",
            (license_id,),
        ).fetchone()
        if not license_row:
            return "cancelled"
        invoices = connection.execute(
            "SELECT status,due_on FROM invoices WHERE license_id=? AND archived_at IS NULL",
            (license_id,),
        ).fetchall()
        if int(license_row["amount_cents"]) == 0:
            status = "not_required"
        elif any(invoice["status"] == "paid" for invoice in invoices):
            status = "paid"
        elif any(invoice["status"] == "open" for invoice in invoices):
            today = date.today().isoformat()
            status = "overdue" if any(
                invoice["status"] == "open" and invoice["due_on"] < today for invoice in invoices
            ) else "open"
        else:
            status = "cancelled"
        connection.execute(
            "UPDATE license_orders SET payment_status=?,updated_at=? WHERE id=?",
            (status, utcnow(), license_row["order_id"]),
        )
        return status

    def mark_invoice_paid(self, environ, user, invoice_id):
        self.require_owner(user)
        invoice = self.db.one("SELECT * FROM invoices WHERE id=?", (invoice_id,))
        if not invoice:
            raise HttpError(404, "Rechnung nicht gefunden.")
        if invoice.get("archived_at") or invoice["status"] == "cancelled":
            raise HttpError(409, "Eine stornierte Rechnung kann nicht als bezahlt markiert werden.")
        with self.db.transaction() as connection:
            connection.execute("UPDATE invoices SET status='paid' WHERE id=?", (invoice_id,))
            self.sync_license_payment_status(connection, invoice["license_id"])
        return self.db.one("SELECT * FROM invoices WHERE id=?", (invoice_id,))

    def archive_invoice(self, environ, user, invoice_id):
        self.require_owner(user)
        invoice = self.db.one("SELECT * FROM invoices WHERE id=?", (invoice_id,))
        if not invoice:
            raise HttpError(404, "Rechnung nicht gefunden.")
        if invoice.get("archived_at"):
            return invoice
        now = utcnow()
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE invoices SET status='cancelled',archived_at=? WHERE id=?",
                (now, invoice_id),
            )
            connection.execute(
                """INSERT OR IGNORE INTO invoice_number_registry(
                       invoice_number,invoice_id,reserved_at,retired_at
                   ) VALUES(?,?,?,?)""",
                (invoice["invoice_number"], invoice_id, invoice["created_at"], now),
            )
            connection.execute(
                "UPDATE invoice_number_registry SET retired_at=COALESCE(retired_at,?) WHERE invoice_number=?",
                (now, invoice["invoice_number"]),
            )
            self.sync_license_payment_status(connection, invoice["license_id"])
        return self.db.one("SELECT * FROM invoices WHERE id=?", (invoice_id,))

    def delete_invoice(self, environ, user, invoice_id):
        self.require_owner(user)
        if self.body(environ).get("confirm_permanent_delete") is not True:
            raise HttpError(400, "Bestätigen Sie das endgültige Löschen der Rechnung ausdrücklich.")
        invoice = self.db.one("SELECT * FROM invoices WHERE id=?", (invoice_id,))
        if not invoice:
            raise HttpError(404, "Rechnung nicht gefunden.")
        now = utcnow()
        with self.db.transaction() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO invoice_number_registry(
                       invoice_number,invoice_id,reserved_at,retired_at
                   ) VALUES(?,?,?,?)""",
                (invoice["invoice_number"], invoice_id, invoice["created_at"], now),
            )
            connection.execute(
                """UPDATE invoice_number_registry SET invoice_id=NULL,
                          retired_at=COALESCE(retired_at,?) WHERE invoice_number=?""",
                (now, invoice["invoice_number"]),
            )
            connection.execute("DELETE FROM invoices WHERE id=?", (invoice_id,))
            self.sync_license_payment_status(connection, invoice["license_id"])
        try:
            (self.config.report_dir / invoice["stored_name"]).unlink(missing_ok=True)
        except OSError:
            warnings.warn(
                f"Rechnungsdatei konnte nach dem Löschen nicht entfernt werden: {invoice['stored_name']}"
            )
        return {
            "ok": True,
            "deleted_id": invoice_id,
            "retained_invoice_number": invoice["invoice_number"],
        }

    def download_invoice(self, environ, user, invoice_id):
        self.require_owner(user)
        invoice = self.db.one("SELECT invoice_number,stored_name FROM invoices WHERE id=?", (invoice_id,))
        if not invoice:
            raise HttpError(404, "Rechnung nicht gefunden.")
        path = self.config.report_dir / invoice["stored_name"]
        if not path.is_file():
            raise HttpError(404, "Die Rechnungsdatei ist nicht mehr vorhanden.")
        pdf_bytes = path.read_bytes()
        if not pdf_bytes.startswith(b"%PDF-") or b"%%EOF" not in pdf_bytes[-1024:]:
            raise HttpError(500, "Die gespeicherte Rechnungsdatei ist beschädigt. Bitte erstellen Sie eine neue Rechnung.")
        self.db.execute(
            "UPDATE invoices SET downloaded_at=COALESCE(downloaded_at,?) WHERE id=?",
            (utcnow(), invoice_id),
        )
        return pdf_bytes, "application/pdf", f"Rechnung-{invoice['invoice_number']}.pdf", []

    def email_invoice(self, environ, user, invoice_id):
        self.require_owner(user)
        invoice = self.db.one("SELECT * FROM invoices WHERE id=?", (invoice_id,))
        if not invoice:
            raise HttpError(404, "Rechnung nicht gefunden.")
        if invoice.get("archived_at") or invoice.get("status") == "cancelled":
            raise HttpError(409, "Eine stornierte oder archivierte Rechnung kann nicht erneut versendet werden.")
        recipient = str(invoice.get("email", "")).strip().lower()
        if not recipient or parseaddr(recipient)[1] != recipient or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", recipient):
            raise HttpError(400, "Für diese Rechnung ist keine gültige Empfängeradresse hinterlegt.")
        path = self.config.report_dir / invoice["stored_name"]
        if not path.is_file():
            raise HttpError(404, "Die Rechnungsdatei ist nicht mehr vorhanden.")
        config = self.config
        sender = config.smtp_username.strip().lower()
        if not config.smtp_host or not sender or not config.smtp_password:
            raise HttpError(503, "Der Rechnungsversand ist noch nicht eingerichtet.")
        if parseaddr(sender)[1] != sender or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", sender):
            raise HttpError(503, "Der SMTP-Benutzername muss eine vollständige E-Mail-Adresse sein.")
        mail = EmailMessage()
        mail["From"] = formataddr(("PRIMEAdvisory - ProjektKontor", sender))
        mail["To"] = recipient
        reply_to = (config.smtp_sender or sender).strip().lower()
        if parseaddr(reply_to)[1] == reply_to and re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", reply_to):
            mail["Reply-To"] = reply_to
        mail["Subject"] = f"ProjektKontor Rechnung {invoice['invoice_number']}"
        mail.set_content(
            f"Guten Tag {invoice['customer_name']},\n\n"
            f"anbei erhalten Sie die Rechnung {invoice['invoice_number']} für ProjektKontor.\n\n"
            "Freundliche Grüße\nPRIMEAdvisory"
        )
        mail.add_attachment(
            path.read_bytes(), maintype="application", subtype="pdf",
            filename=f"Rechnung-{invoice['invoice_number']}.pdf",
        )
        smtp_class = smtplib.SMTP_SSL if config.smtp_use_ssl else smtplib.SMTP
        try:
            with smtp_class(config.smtp_host, config.smtp_port, timeout=15) as smtp:
                if not config.smtp_use_ssl:
                    smtp.starttls()
                smtp.login(config.smtp_username, config.smtp_password)
                smtp.send_message(mail)
        except smtplib.SMTPAuthenticationError as exc:
            raise HttpError(503, "Die SMTP-Anmeldung ist fehlgeschlagen. Prüfen Sie die GMX-Adresse und das Anwendungspasswort.") from exc
        except smtplib.SMTPSenderRefused as exc:
            raise HttpError(503, "GMX hat den Absender abgelehnt. Der Versand erfolgt nun immer über den hinterlegten SMTP-Benutzernamen.") from exc
        except smtplib.SMTPRecipientsRefused as exc:
            raise HttpError(503, "Die Empfängeradresse wurde vom Mailserver abgelehnt.") from exc
        except (OSError, smtplib.SMTPException) as exc:
            raise HttpError(503, "Der Mailserver ist derzeit nicht erreichbar oder hat den Versand abgelehnt.") from exc
        sent_at = utcnow()
        self.db.execute(
            "UPDATE invoices SET status='open',emailed_at=? WHERE id=?",
            (sent_at, invoice_id),
        )
        return {
            "ok": True, "message": "Die Rechnung wurde per E-Mail versendet.",
            "invoice_id": invoice_id, "emailed_at": sent_at,
        }

    def list_teachers(self, environ, user):
        self.require_owner(user)
        teachers = self.db.all(
            """SELECT u.id,u.first_name,u.username,u.email,u.is_owner,u.active,u.license_managed,
                      u.last_login_at,u.initial_credentials_emailed_at,u.archived_at,u.created_at,
                      (SELECT COUNT(*) FROM teacher_classes tc WHERE tc.teacher_id=u.id) class_count,
                      EXISTS(SELECT 1 FROM support_requests sr WHERE sr.teacher_id=u.id AND sr.status='active'
                        AND sr.access_expires_at>?) support_active
               FROM users u WHERE u.role='teacher' AND u.is_owner=0
               ORDER BY (u.archived_at IS NOT NULL),u.first_name,u.username""",
            (utcnow(),),
        )
        self.refresh_expired_licenses()
        for teacher in teachers:
            teacher["licenses"] = self.db.all(
                """SELECT l.id,l.status,l.starts_on,l.ends_on,o.plan,o.billing_cycle,o.organization
                   FROM license_teachers lt JOIN licenses l ON l.id=lt.license_id
                   JOIN license_orders o ON o.id=l.order_id
                   WHERE lt.teacher_id=? ORDER BY l.ends_on DESC,l.id DESC""",
                (teacher["id"],),
            )
            teacher["license_valid"] = self.teacher_license_valid(teacher["id"])
        return teachers

    def assign_teacher_license(
        self,
        connection: sqlite3.Connection,
        teacher_id: int,
        teacher_name: str,
        selection: str,
        data: dict[str, Any],
        now: str,
    ) -> int | None:
        selection = str(selection or "none").strip()
        current_ids = {
            int(row[0]) for row in connection.execute(
                """SELECT l.id FROM license_teachers lt JOIN licenses l ON l.id=lt.license_id
                   WHERE lt.teacher_id=? AND l.status IN ('draft','active','suspended')""",
                (teacher_id,),
            ).fetchall()
        }
        target_id: int | None = None
        if selection.startswith("existing:"):
            try:
                target_id = int(selection.split(":", 1)[1])
            except ValueError as exc:
                raise HttpError(400, "Die ausgewählte Lizenz ist ungültig.") from exc
            target = connection.execute(
                """SELECT l.id,l.seat_limit,l.status,
                          (SELECT COUNT(*) FROM license_teachers lt
                             JOIN users u ON u.id=lt.teacher_id
                            WHERE lt.license_id=l.id AND u.archived_at IS NULL) used_seats
                   FROM licenses l WHERE l.id=?""",
                (target_id,),
            ).fetchone()
            if not target or target["status"] not in {"draft", "active", "suspended"}:
                raise HttpError(400, "Die ausgewählte Lizenz kann nicht mehr zugeordnet werden.")
            already_assigned = target_id in current_ids
            if not already_assigned and target["used_seats"] >= target["seat_limit"]:
                raise HttpError(409, "Die ausgewählte Lizenz hat keinen freien Lehrkraftplatz mehr.")
        elif selection.startswith("new-product:") or selection in {"new:beta", "new:monthly", "new:annual"}:
            legacy_keys = {"new:beta": "beta", "new:monthly": "single_monthly", "new:annual": "single_annual"}
            product_key = legacy_keys.get(selection, selection.split(":", 1)[1])
            product = connection.execute(
                "SELECT * FROM license_products WHERE product_key=? AND active=1", (product_key,)
            ).fetchone()
            if not product:
                raise HttpError(400, "Das ausgewählte Lizenzprodukt ist nicht verfügbar.")
            product = dict(product)
            starts_on, ends_on = product_license_dates(product)
            plan = product["plan"]
            billing_cycle = product["billing_cycle"]
            amount_cents = product["amount_cents"]
            payment_status = "not_required" if plan == "beta" else "open"
            customer_name = str(data.get("license_customer_name") or teacher_name).strip()
            organization = str(data.get("license_organization") or "").strip()
            email = str(data.get("license_email") or "").strip().lower()
            billing_address = str(data.get("license_billing_address") or "").strip()
            if email and (parseaddr(email)[1] != email or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email)):
                raise HttpError(400, "Die E-Mail-Adresse für die Lizenz ist ungültig.")
            order = connection.execute(
                """INSERT INTO license_orders(
                       customer_name,organization,email,billing_address,invoice_reference,
                       plan,billing_cycle,amount_cents,payment_status,notes,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    customer_name, organization, email, billing_address, "", plan,
                    billing_cycle, amount_cents, payment_status,
                    "Automatisch bei der Lehrkraftanlage erzeugt.", now, now,
                ),
            )
            license_row = connection.execute(
                """INSERT INTO licenses(order_id,seat_limit,starts_on,ends_on,status,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (int(order.lastrowid), product["seat_limit"], starts_on, ends_on, "active", now, now),
            )
            target_id = int(license_row.lastrowid)
        elif selection != "none":
            raise HttpError(400, "Die ausgewählte Lizenzoption ist ungültig.")

        removable = current_ids - ({target_id} if target_id else set())
        if removable:
            placeholders = ",".join("?" for _ in removable)
            connection.execute(
                f"DELETE FROM license_teachers WHERE teacher_id=? AND license_id IN ({placeholders})",
                (teacher_id, *removable),
            )
        if target_id:
            connection.execute(
                "INSERT OR IGNORE INTO license_teachers(license_id,teacher_id,assigned_at) VALUES(?,?,?)",
                (target_id, teacher_id, now),
            )
        connection.execute("UPDATE users SET license_managed=1 WHERE id=?", (teacher_id,))
        connection.execute("DELETE FROM sessions WHERE user_id=?", (teacher_id,))
        return target_id

    def create_teacher(self, environ, user):
        self.require_owner(user)
        data = self.body(environ)
        first_name = str(data.get("first_name", "")).strip()
        username = str(data.get("username", "")).strip().lower()
        email = str(data.get("email", "")).strip().lower()
        if data.get("class_ids"):
            raise HttpError(400, "Klassen werden durch die Lehrkraft selbst angelegt und verwaltet.")
        if not first_name or len(first_name) > 80:
            raise HttpError(400, "Der Name muss 1 bis 80 Zeichen lang sein.")
        if not username.startswith("lehrkraft_") or not re.fullmatch(r"lehrkraft_[a-zA-Z0-9._-]{3,40}", username):
            raise HttpError(400, "Der Benutzername muss mit „lehrkraft_“ beginnen und danach 3 bis 40 zulässige Zeichen enthalten.")
        if email and (parseaddr(email)[1] != email or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email)):
            raise HttpError(400, "Bitte geben Sie eine gültige E-Mail-Adresse der Lehrkraft an.")
        initial_password = generate_access_code(12)
        license_selection = str(data.get("license_selection", "none"))
        try:
            with self.db.transaction() as connection:
                cursor = connection.execute(
                    "INSERT INTO users(first_name,username,email,credential,role,is_owner,must_change_password,license_managed,active,created_at) VALUES(?,?,?,?,?,0,1,1,1,?)",
                    (first_name, username, email, hash_password(initial_password), "teacher", utcnow()),
                )
                teacher_id = int(cursor.lastrowid)
                license_id = self.assign_teacher_license(
                    connection, teacher_id, first_name, license_selection, data, utcnow()
                )
        except sqlite3.IntegrityError:
            raise HttpError(409, "Dieser Benutzername ist bereits vergeben.")
        return {
            "id": teacher_id, "first_name": first_name, "username": username, "email": email,
            "initial_password": initial_password, "license_id": license_id,
        }

    def update_teacher(self, environ, user, teacher_id):
        self.require_owner(user)
        teacher = self.db.one("SELECT * FROM users WHERE id=? AND role='teacher'", (teacher_id,))
        if not teacher:
            raise HttpError(404, "Lehrkraft nicht gefunden.")
        if teacher.get("is_owner"):
            raise HttpError(403, "Das Konto der Geschäftsführung wird unter „Eigenes Konto“ verwaltet.")
        if teacher.get("archived_at"):
            raise HttpError(409, "Ein archivierter Lehrkraftzugang muss vor einer Bearbeitung reaktiviert werden.")
        data = self.body(environ)
        first_name = str(data.get("first_name", teacher["first_name"])).strip()
        username = str(data.get("username", teacher["username"])).strip().lower()
        email = str(data.get("email", teacher.get("email", ""))).strip().lower()
        active = int(bool(data.get("active", teacher["active"])))
        if data.get("class_ids"):
            raise HttpError(400, "Klassenzuordnungen werden durch die Lehrkraft selbst verwaltet.")
        if not first_name or len(first_name) > 80 or not re.fullmatch(r"lehrkraft_[a-zA-Z0-9._-]{3,40}", username):
            raise HttpError(400, "Name oder Benutzername ist ungültig.")
        if email and (parseaddr(email)[1] != email or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email)):
            raise HttpError(400, "Bitte geben Sie eine gültige E-Mail-Adresse der Lehrkraft an.")
        initial_password = generate_access_code(12) if data.get("reset_password") else None
        license_selection = data.get("license_selection")
        if initial_password and teacher.get("last_login_at"):
            raise HttpError(403, "Nach der ersten Anmeldung ändert nur die Lehrkraft selbst ihr Kennwort.")
        try:
            with self.db.transaction() as connection:
                credential = hash_password(initial_password) if initial_password else teacher["credential"]
                connection.execute(
                    """UPDATE users SET first_name=?,username=?,email=?,credential=?,must_change_password=?,
                       initial_credentials_emailed_at=CASE WHEN ? THEN NULL ELSE initial_credentials_emailed_at END,
                       active=? WHERE id=?""",
                    (
                        first_name, username, email, credential,
                        1 if initial_password else teacher.get("must_change_password", 0),
                        1 if initial_password else 0, active, teacher_id,
                    ),
                )
                if license_selection is not None:
                    self.assign_teacher_license(
                        connection, teacher_id, first_name, str(license_selection), data, utcnow()
                    )
                if not active or initial_password:
                    connection.execute("DELETE FROM sessions WHERE user_id=?", (teacher_id,))
        except sqlite3.IntegrityError:
            raise HttpError(409, "Dieser Benutzername ist bereits vergeben.")
        result = {"ok": True, "id": teacher_id, "email": email}
        if initial_password:
            result["initial_password"] = initial_password
        return result

    def archive_teacher(self, environ, user, teacher_id):
        self.require_owner(user)
        teacher = self.db.one(
            "SELECT * FROM users WHERE id=? AND role='teacher' AND is_owner=0", (teacher_id,)
        )
        if not teacher:
            raise HttpError(404, "Lehrkraft nicht gefunden.")
        if teacher.get("archived_at"):
            return {"ok": True, "teacher_id": teacher_id, "archived_at": teacher["archived_at"]}
        now = utcnow()
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE users SET active=0,archived_at=? WHERE id=?", (now, teacher_id)
            )
            connection.execute("DELETE FROM sessions WHERE user_id=?", (teacher_id,))
            connection.execute("DELETE FROM pending_logins WHERE user_id=?", (teacher_id,))
            connection.execute(
                """UPDATE support_requests SET status='revoked',access_expires_at=?
                   WHERE teacher_id=? AND status IN ('pending','active')""",
                (now, teacher_id),
            )
        return {"ok": True, "teacher_id": teacher_id, "archived_at": now}

    def restore_teacher(self, environ, user, teacher_id):
        self.require_owner(user)
        teacher = self.db.one(
            "SELECT * FROM users WHERE id=? AND role='teacher' AND is_owner=0", (teacher_id,)
        )
        if not teacher:
            raise HttpError(404, "Lehrkraft nicht gefunden.")
        if not teacher.get("archived_at"):
            return {"ok": True, "teacher_id": teacher_id, "restored": False}
        self.db.execute(
            "UPDATE users SET active=1,archived_at=NULL WHERE id=?", (teacher_id,)
        )
        return {"ok": True, "teacher_id": teacher_id, "restored": True}

    def delete_teacher(self, environ, user, teacher_id):
        self.require_owner(user)
        if self.body(environ).get("confirm_permanent_delete") is not True:
            raise HttpError(400, "Bestätigen Sie das endgültige Löschen des Lehrkraftzugangs ausdrücklich.")
        teacher = self.db.one(
            "SELECT * FROM users WHERE id=? AND role='teacher' AND is_owner=0", (teacher_id,)
        )
        if not teacher:
            raise HttpError(404, "Lehrkraft nicht gefunden.")
        work_references = (
            ("teacher_classes", "teacher_id"),
            ("projects", "project_lead_id"),
            ("project_members", "user_id"),
            ("teams", "created_by"),
            ("team_members", "user_id"),
            ("tasks", "created_by"),
            ("task_assignees", "user_id"),
            ("deadline_requests", "requested_by"),
            ("comments", "author_id"),
            ("uploads", "uploaded_by"),
            ("templates", "created_by"),
            ("reports", "created_by"),
        )
        has_work = bool(teacher.get("last_login_at")) or any(
            self.db.one(f"SELECT 1 present FROM {table} WHERE {column}=? LIMIT 1", (teacher_id,))
            for table, column in work_references
        )
        if has_work:
            raise HttpError(
                409,
                "Dieser Zugang ist bereits mit Unterrichts- oder Verlaufsdaten verbunden und kann deshalb nur archiviert werden.",
            )
        with self.db.transaction() as connection:
            connection.execute("DELETE FROM license_teachers WHERE teacher_id=?", (teacher_id,))
            connection.execute("DELETE FROM users WHERE id=?", (teacher_id,))
        return {"ok": True, "deleted_id": teacher_id}

    def email_initial_credentials(self, environ, user, teacher_id):
        self.require_owner(user)
        teacher = self.db.one(
            """SELECT id,first_name,username,email,credential,must_change_password,last_login_at
               FROM users WHERE id=? AND role='teacher' AND is_owner=0""",
            (teacher_id,),
        )
        if not teacher:
            raise HttpError(404, "Lehrkraftzugang nicht gefunden.")
        data = self.body(environ)
        initial_password = str(data.get("initial_password", ""))
        if not teacher["must_change_password"] or teacher["last_login_at"]:
            raise HttpError(409, "Die Initialzugangsdaten sind nicht mehr gültig.")
        if not initial_password or not verify_password(initial_password, teacher["credential"]):
            raise HttpError(400, "Die Initialzugangsdaten konnten nicht bestätigt werden.")
        recipient = str(teacher["email"]).strip().lower()
        if parseaddr(recipient)[1] != recipient or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", recipient):
            raise HttpError(400, "Für diesen Zugang ist keine gültige E-Mail-Adresse hinterlegt.")
        config = self.config
        sender = config.smtp_username.strip().lower()
        if not config.smtp_host or not sender or not config.smtp_password:
            raise HttpError(503, "Der Versand von Zugangsdaten ist noch nicht eingerichtet.")
        mail = EmailMessage()
        mail["From"] = formataddr(("PRIMEAdvisory - ProjektKontor", sender))
        mail["To"] = recipient
        reply_to = (config.smtp_sender or sender).strip().lower()
        if parseaddr(reply_to)[1] == reply_to and re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", reply_to):
            mail["Reply-To"] = reply_to
        mail["Subject"] = "Ihre Zugangsdaten für ProjektKontor"
        mail.set_content(
            f"Guten Tag {teacher['first_name']},\n\n"
            "für Sie wurde ein Lehrkraftzugang zu ProjektKontor angelegt.\n\n"
            f"Benutzername: {teacher['username']}\n"
            f"Initialkennwort: {initial_password}\n\n"
            "Bei der ersten Anmeldung vergeben Sie ein persönliches Kennwort. "
            "Bitte behandeln Sie diese Nachricht bis dahin vertraulich.\n\n"
            "Freundliche Grüße\nPRIMEAdvisory"
        )
        smtp_class = smtplib.SMTP_SSL if config.smtp_use_ssl else smtplib.SMTP
        try:
            with smtp_class(config.smtp_host, config.smtp_port, timeout=15) as smtp:
                if not config.smtp_use_ssl:
                    smtp.starttls()
                smtp.login(config.smtp_username, config.smtp_password)
                smtp.send_message(mail)
        except smtplib.SMTPAuthenticationError as exc:
            raise HttpError(503, "Die SMTP-Anmeldung ist fehlgeschlagen.") from exc
        except smtplib.SMTPRecipientsRefused as exc:
            raise HttpError(503, "Die Empfängeradresse wurde vom Mailserver abgelehnt.") from exc
        except (OSError, smtplib.SMTPException) as exc:
            raise HttpError(503, "Der Mailserver ist derzeit nicht erreichbar oder hat den Versand abgelehnt.") from exc
        sent_at = utcnow()
        self.db.execute(
            "UPDATE users SET initial_credentials_emailed_at=? WHERE id=?",
            (sent_at, teacher_id),
        )
        return {"ok": True, "recipient": recipient, "sent_at": sent_at}

    def list_classes(self, environ, user):
        user = self.require_teacher(user)
        if user.get("is_owner"):
            return self.db.all(
                """SELECT DISTINCT c.*, (SELECT COUNT(*) FROM users u WHERE u.class_id=c.id) user_count
                   FROM classes c JOIN teacher_classes tc ON tc.class_id=c.id
                   JOIN support_requests sr ON sr.teacher_id=tc.teacher_id
                   WHERE c.active=1 AND sr.granted_admin_id=? AND sr.status='active'
                     AND sr.access_expires_at>? ORDER BY c.name""",
                (user["id"], utcnow()),
            )
        return self.db.all(
            "SELECT c.*, (SELECT COUNT(*) FROM users u WHERE u.class_id=c.id) user_count "
            "FROM classes c JOIN teacher_classes tc ON tc.class_id=c.id "
            "WHERE c.active=1 AND tc.teacher_id=? ORDER BY c.name",
            (user["id"],),
        )

    def create_class(self, environ, user):
        teacher = self.require_regular_teacher(user)
        active_license = self.active_teacher_license(teacher["id"])
        if active_license and active_license["plan"] == "beta" and self.db.one(
            """SELECT 1 ok FROM teacher_classes tc JOIN classes c ON c.id=tc.class_id
                 WHERE tc.teacher_id=? AND c.active=1 LIMIT 1""",
            (teacher["id"],),
        ):
            raise HttpError(
                409,
                "Der vierwöchige Praxistest ist auf eine Klasse begrenzt. "
                "Für weitere Klassen kann der Test begründet verlängert oder in eine Lizenz umgewandelt werden.",
            )
        name = str(self.body(environ).get("name", "")).strip()
        if not name:
            raise HttpError(400, "Die Klassenbezeichnung darf nicht leer sein.")
        if any(character.isspace() for character in name):
            raise HttpError(400, "Klassenbezeichnungen dürfen keine Leerzeichen enthalten.")
        if not re.fullmatch(r"[A-Za-zÄÖÜäöüß0-9._-]{2,30}", name):
            raise HttpError(400, "Die Klassenbezeichnung muss 2 bis 30 Zeichen lang sein. Erlaubt sind Buchstaben, Zahlen, Punkte, Bindestriche und Unterstriche.")
        try:
            with self.db.transaction() as connection:
                cursor = connection.execute("INSERT INTO classes(name,created_at) VALUES(?,?)", (name,utcnow()))
                class_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO teacher_classes(teacher_id,class_id,assigned_at) VALUES(?,?,?)",
                    (teacher["id"], class_id, utcnow()),
                )
        except sqlite3.IntegrityError:
            raise HttpError(409, "Diese Klasse existiert bereits.")
        return {"id": class_id, "name": name}

    def update_class(self, environ, user, class_id):
        self.require_class_access(user, class_id)
        current = self.db.one("SELECT * FROM classes WHERE id=? AND active=1", (class_id,))
        if not current:
            raise HttpError(404, "Klasse nicht gefunden")
        name = str(self.body(environ).get("name", current["name"])).strip()
        if not name:
            raise HttpError(400, "Die Klassenbezeichnung darf nicht leer sein.")
        if any(character.isspace() for character in name):
            raise HttpError(400, "Klassenbezeichnungen dürfen keine Leerzeichen enthalten.")
        if not re.fullmatch(r"[A-Za-zÄÖÜäöüß0-9._-]{2,30}", name):
            raise HttpError(400, "Die Klassenbezeichnung muss 2 bis 30 Zeichen lang sein. Erlaubt sind Buchstaben, Zahlen, Punkte, Bindestriche und Unterstriche.")
        try:
            self.db.execute("UPDATE classes SET name=? WHERE id=?", (name, class_id))
        except sqlite3.IntegrityError:
            raise HttpError(409, "Diese Klasse existiert bereits.")
        return {"id": class_id, "name": name}

    def delete_class(self, environ, user, class_id):
        teacher = self.require_class_access(user, class_id)
        data = self.body(environ)
        active_projects = self.db.one("SELECT COUNT(*) count FROM projects WHERE class_id=? AND status IN ('draft','active')", (class_id,))["count"]
        if active_projects:
            raise HttpError(409, "Die Klasse besitzt noch aktive Projekte.")
        with self.db.transaction() as connection:
            if data.get("delete_users"):
                user_ids = [row[0] for row in connection.execute("SELECT id FROM users WHERE class_id=? AND role='student'", (class_id,)).fetchall()]
                for account_id in user_ids:
                    self._delete_student_account(connection, account_id, teacher["id"])
            else:
                connection.execute("UPDATE users SET class_id=NULL WHERE class_id=?", (class_id,))
            has_history = connection.execute("SELECT 1 FROM projects WHERE class_id=? LIMIT 1", (class_id,)).fetchone()
            if has_history:
                connection.execute("UPDATE classes SET active=0 WHERE id=?", (class_id,))
            else:
                connection.execute("DELETE FROM classes WHERE id=?", (class_id,))
        return {"ok": True}

    def list_class_users(self, environ, user, class_id):
        self.require_class_access(user, class_id)
        rows = self.db.all("SELECT id,first_name,username,active,last_login_at,credential FROM users WHERE class_id=? AND role='student' ORDER BY first_name,username", (class_id,))
        for row in rows:
            row["access_code"] = self.vault.decrypt(row.pop("credential"))
        return rows

    def list_all_users(self, environ, user):
        user = self.require_teacher(user)
        if user.get("is_owner"):
            scope_sql = """ AND u.class_id IN (
                SELECT tc.class_id FROM teacher_classes tc JOIN support_requests sr ON sr.teacher_id=tc.teacher_id
                WHERE sr.granted_admin_id=? AND sr.status='active' AND sr.access_expires_at>?
            )"""
            scope_params = (user["id"], utcnow())
        else:
            scope_sql = " AND u.class_id IN (SELECT class_id FROM teacher_classes WHERE teacher_id=?)"
            scope_params = (user["id"],)
        rows = self.db.all(f"""
            SELECT u.id,u.class_id,u.first_name,u.username,u.active,u.last_login_at,u.credential,
                   COALESCE(c.name,'Ohne Klasse') class_name
            FROM users u LEFT JOIN classes c ON c.id=u.class_id
            WHERE u.role='student' AND u.active=1{scope_sql}
            ORDER BY u.first_name,u.username
        """, scope_params)
        for row in rows:
            row["access_code"] = self.vault.decrypt(row.pop("credential"))
            row["projects"] = self.db.all("""
                SELECT DISTINCT p.id,p.title,p.status
                FROM project_members pm JOIN projects p ON p.id=pm.project_id
                WHERE pm.user_id=? AND p.status IN ('draft','active')
                ORDER BY p.title
            """, (row["id"],))
            row["teams"] = self.db.all("""
                SELECT DISTINCT t.name,p.title project_title,tm.is_lead
                FROM team_members tm JOIN teams t ON t.id=tm.team_id JOIN projects p ON p.id=t.project_id
                WHERE tm.user_id=? AND t.status='active' AND p.status IN ('draft','active')
                ORDER BY p.title,t.name
            """, (row["id"],))
            row["open_task_count"] = self.db.one("""
                SELECT COUNT(DISTINCT a.task_id) count
                FROM task_assignees a JOIN tasks t ON t.id=a.task_id JOIN projects p ON p.id=t.project_id
                WHERE a.user_id=? AND t.status_key!='done' AND p.status IN ('draft','active')
            """, (row["id"],))["count"]
        return rows

    def create_class_user(self, environ, user, class_id):
        self.require_class_access(user, class_id)
        data = self.body(environ)
        first_name = str(data.get("first_name", "")).strip()
        if not first_name:
            raise HttpError(400, "Bitte geben Sie einen Vornamen ein.")
        if len(first_name) > 80:
            raise HttpError(400, "Der Vorname darf höchstens 80 Zeichen lang sein.")
        class_row = self.db.one("SELECT id FROM classes WHERE id=? AND active=1", (class_id,))
        if not class_row:
            raise HttpError(404, "Klasse nicht gefunden")
        with self.db.transaction() as connection:
            proposed_username = str(data.get("username", "")).strip().lower()
            username = proposed_username or next_username(connection, first_name)
            if not re.fullmatch(r"[a-z0-9._-]{3,50}", username):
                raise HttpError(400, "Der Benutzername muss 3 bis 50 Zeichen lang sein und darf nur Kleinbuchstaben, Zahlen, Punkte, Bindestriche und Unterstriche enthalten.")
            if connection.execute("SELECT 1 FROM users WHERE username=? COLLATE NOCASE", (username,)).fetchone():
                raise HttpError(409, "Dieser Benutzername ist bereits vergeben.")
            access_code = str(data.get("access_code", "")).strip() or generate_access_code()
            if len(access_code) < 4 or len(access_code) > 64:
                raise HttpError(400, "Der Zugangscode muss 4 bis 64 Zeichen lang sein.")
            cursor = connection.execute(
                "INSERT INTO users(class_id,first_name,username,credential,role,active,created_at) VALUES(?,?,?,?,?,1,?)",
                (class_id, first_name, username, self.vault.encrypt(access_code), "student", utcnow()),
            )
        return {"id": int(cursor.lastrowid), "first_name": first_name, "username": username, "access_code": access_code}

    def download_account_template(self, environ, user, class_id=None):
        if class_id is None:
            self.require_regular_teacher(user)
            return account_template(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "ProjektKontor-Import-Alle-Klassen.xlsx", []
        self.require_class_access(user, class_id)
        class_row = self.db.one("SELECT name FROM classes WHERE id=?", (class_id,))
        if not class_row: raise HttpError(404, "Klasse nicht gefunden")
        return account_template(class_row["name"]), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", f"ProjektKontor-Import-{class_row['name']}.xlsx", []

    def import_preview(self, environ, user, class_id=None):
        teacher = self.require_regular_teacher(user) if class_id is None else self.require_class_access(user, class_id)
        data = self.body(environ)
        try: raw = base64.b64decode(data.get("file_base64", ""), validate=True)
        except Exception as exc: raise HttpError(400, "Excel-Datei fehlt.") from exc
        rows = parse_account_workbook(raw)
        with self.db.connect() as connection:
            default_class=connection.execute("SELECT id,name FROM classes WHERE id=? AND active=1",(class_id,)).fetchone() if class_id is not None else None
            if class_id is not None and not default_class:raise HttpError(404,"Klasse nicht gefunden")
            class_lookup={str(item["name"]).casefold():dict(item) for item in connection.execute("SELECT id,name,active FROM classes").fetchall()}
            existing_names={(int(item["class_id"]),str(item["first_name"]).strip().casefold()) for item in connection.execute("SELECT class_id,first_name FROM users WHERE role='student' AND active=1 AND class_id IS NOT NULL").fetchall()}
            seen_names=set()
            for row in rows:
                if not row["error"]:
                    target_name=str(row.get("class_name") or (default_class["name"] if default_class else "")).strip()
                    row["class_name"]=target_name
                    if not target_name:
                        row["error"]="In diesem Importmodus muss eine Klasse angegeben sein."
                        continue
                    if any(character.isspace() for character in target_name) or not re.fullmatch(r"[A-Za-zÄÖÜäöüß0-9._-]{2,30}",target_name):
                        row["error"]="Klassenbezeichnung ungültig (2–30 Zeichen, ohne Leerzeichen)."
                        continue
                    target=class_lookup.get(target_name.casefold())
                    if target is not None and not self.can_access_class(teacher, int(target["id"])):
                        row["error"]="Diese Klasse wurde Ihrer Lehrkraft nicht freigeschaltet."
                        continue
                    row["new_class"]=target is None or not target["active"]
                    if target is None:
                        cursor=connection.execute("INSERT INTO classes(name,created_at) VALUES(?,?)",(target_name,utcnow()))
                        target={"id":int(cursor.lastrowid),"name":target_name,"active":1}
                        connection.execute("INSERT INTO teacher_classes(teacher_id,class_id,assigned_at) VALUES(?,?,?)",(teacher["id"],target["id"],utcnow()))
                        class_lookup[target_name.casefold()]=target
                    row["class_id"]=int(target["id"])
                    normalized=row["first_name"].strip().casefold()
                    identity=(row["class_id"],normalized)
                    row["duplicate"]=identity in existing_names or identity in seen_names
                    seen_names.add(identity)
                    row["username"] = next_username(connection, row["first_name"])
                    row["access_code"] = generate_access_code()
                    connection.execute("INSERT INTO users(class_id,first_name,username,credential,role,active,created_at) VALUES(?,?,?,?,?,0,?)", (row["class_id"],row["first_name"],row["username"],self.vault.encrypt(row["access_code"]),"student",utcnow()))
            connection.rollback()
        return {"rows": rows}

    def import_accounts(self, environ, user, class_id=None):
        teacher = self.require_regular_teacher(user) if class_id is None else self.require_class_access(user, class_id)
        data = self.body(environ); rows = data.get("rows", [])
        if not isinstance(rows, list) or not rows: raise HttpError(400, "Keine Importdaten vorhanden.")
        if any(row.get("duplicate") and not row.get("skip") for row in rows) and not data.get("confirm_duplicates"):
            raise HttpError(409,"Bestätigen Sie den Import der markierten Namensdubletten")
        created = [];created_classes=[]
        with self.db.transaction() as connection:
            default_class=connection.execute("SELECT id,name FROM classes WHERE id=? AND active=1",(class_id,)).fetchone() if class_id is not None else None
            if class_id is not None and not default_class:raise HttpError(404,"Klasse nicht gefunden")
            class_lookup={str(item["name"]).casefold():dict(item) for item in connection.execute("SELECT id,name,active FROM classes").fetchall()}
            for row in rows:
                first_name = str(row.get("first_name", "")).strip()
                if not first_name or row.get("skip") or row.get("error"): continue
                if len(first_name) > 80: raise HttpError(400, "Ein Vorname ist länger als 80 Zeichen.")
                target_name=str(row.get("class_name") or (default_class["name"] if default_class else "")).strip()
                if not target_name:
                    raise HttpError(400,"Mindestens eine Zeile enthält keine Klassenangabe.")
                if any(character.isspace() for character in target_name) or not re.fullmatch(r"[A-Za-zÄÖÜäöüß0-9._-]{2,30}",target_name):
                    raise HttpError(400,f"Ungültige Klassenbezeichnung: {target_name or 'leer'}")
                target=class_lookup.get(target_name.casefold())
                if target is not None and not self.can_access_class(teacher, int(target["id"])):
                    raise HttpError(403,"Diese Klasse wurde Ihrer Lehrkraft nicht freigeschaltet.")
                if target is None:
                    cursor=connection.execute("INSERT INTO classes(name,created_at) VALUES(?,?)",(target_name,utcnow()))
                    target={"id":int(cursor.lastrowid),"name":target_name,"active":1};class_lookup[target_name.casefold()]=target;created_classes.append(target_name)
                    connection.execute("INSERT INTO teacher_classes(teacher_id,class_id,assigned_at) VALUES(?,?,?)",(teacher["id"],target["id"],utcnow()))
                elif not target["active"]:
                    connection.execute("UPDATE classes SET active=1 WHERE id=?",(target["id"],));target["active"]=1;created_classes.append(target["name"])
                username = next_username(connection, first_name)
                code = str(row.get("access_code") or generate_access_code())
                if not 4 <= len(code) <= 64: raise HttpError(400, "Ein Zugangscode hat eine ungültige Länge.")
                cursor = connection.execute("INSERT INTO users(class_id,first_name,username,credential,role,active,created_at) VALUES(?,?,?,?,?,1,?)", (target["id"],first_name,username,self.vault.encrypt(code),"student",utcnow()))
                created.append({"id":cursor.lastrowid,"first_name":first_name,"username":username,"access_code":code,"class_name":target["name"]})
        return {"created": created,"created_classes":created_classes}

    def update_user(self, environ, user, user_id):
        teacher = self.require_teacher(user)
        account = self.db.one("SELECT * FROM users WHERE id=? AND role='student'", (user_id,))
        if not account: raise HttpError(404,"Konto nicht gefunden")
        if account["class_id"] is None or not self.can_access_class(teacher, account["class_id"]):
            raise HttpError(403,"Dieser Zugang gehört nicht zu einer freigeschalteten Klasse.")
        data = self.body(environ)
        first_name = str(data.get("first_name",account["first_name"])).strip()
        username = str(data.get("username",account["username"])).strip().lower()
        if not first_name or len(first_name) > 80: raise HttpError(400,"Der Vorname muss 1 bis 80 Zeichen lang sein")
        if not re.fullmatch(r"[a-z0-9._-]{3,50}",username): raise HttpError(400,"Der Benutzername ist ungültig")
        if "access_code" in data and not 4 <= len(str(data["access_code"])) <= 64: raise HttpError(400,"Der Zugangscode muss 4 bis 64 Zeichen lang sein")
        raw_class_id = data.get("class_id", account["class_id"])
        class_id = int(raw_class_id) if raw_class_id not in (None, "") else None
        if class_id is not None and not self.db.one("SELECT 1 ok FROM classes WHERE id=? AND active=1", (class_id,)):
            raise HttpError(404, "Zielklasse nicht gefunden")
        if class_id is not None and not self.can_access_class(teacher, class_id):
            raise HttpError(403,"Die Zielklasse wurde Ihrer Lehrkraft nicht freigeschaltet.")
        credential = self.vault.encrypt(str(data["access_code"])) if "access_code" in data else account["credential"]
        try:
            self.db.execute("UPDATE users SET class_id=?,first_name=?,username=?,credential=?,active=? WHERE id=?", (class_id,first_name,username,credential,int(data.get("active",account["active"])),user_id))
        except sqlite3.IntegrityError: raise HttpError(409,"Benutzername bereits vergeben")
        return {"ok":True}

    def delete_user(self, environ, user, user_id):
        teacher = self.require_teacher(user)
        account = self.db.one("SELECT class_id FROM users WHERE id=? AND role='student'", (user_id,))
        if not account:
            raise HttpError(404,"Konto nicht gefunden")
        if account["class_id"] is None or not self.can_access_class(teacher, account["class_id"]):
            raise HttpError(403,"Dieser Zugang gehört nicht zu einer freigeschalteten Klasse.")
        count = self.db.one("""SELECT COUNT(*) count FROM task_assignees a JOIN tasks t ON t.id=a.task_id JOIN projects p ON p.id=t.project_id WHERE a.user_id=? AND p.status='active'""", (user_id,))["count"]
        if count: raise HttpError(409,"Das Konto besitzt noch Aufgaben in aktiven Projekten. Bitte ordnen Sie diese zuerst neu zu.")
        if self.db.one("SELECT 1 ok FROM projects WHERE project_lead_id=? AND status IN ('draft','active')", (user_id,)):
            raise HttpError(409, "Das Konto leitet noch ein aktives Projekt. Bitte ändern Sie zuerst die Gesamtprojektleitung.")
        with self.db.transaction() as connection:
            self._delete_student_account(connection, user_id, teacher["id"])
        return {"ok":True}

    def _delete_student_account(self, connection: sqlite3.Connection, user_id: int, teacher_id: int) -> None:
        account = connection.execute("SELECT * FROM users WHERE id=? AND role='student'", (user_id,)).fetchone()
        if not account:
            return
        placeholder_username = f"geloescht-{user_id}-{secrets.token_hex(3)}"
        cursor = connection.execute(
            "INSERT INTO users(first_name,username,credential,role,active,created_at) VALUES(?,?,?,?,0,?)",
            ("Gelöschtes Konto", placeholder_username, self.vault.encrypt(generate_access_code()), "student", utcnow()),
        )
        placeholder_id = int(cursor.lastrowid)
        connection.execute("UPDATE projects SET project_lead_id=? WHERE project_lead_id=?", (teacher_id, user_id))
        for table, column in (("tasks","created_by"),("comments","author_id"),("comments","hidden_by"),("uploads","uploaded_by"),("project_events","actor_id"),("templates","created_by"),("reports","created_by")):
            connection.execute(f"UPDATE {table} SET {column}=? WHERE {column}=?", (placeholder_id, user_id))
        connection.execute("DELETE FROM task_assignees WHERE user_id=?", (user_id,))
        connection.execute("DELETE FROM team_members WHERE user_id=?", (user_id,))
        connection.execute("DELETE FROM project_members WHERE user_id=?", (user_id,))
        connection.execute("DELETE FROM notifications WHERE user_id=?", (user_id,))
        connection.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
        connection.execute("DELETE FROM users WHERE id=?", (user_id,))

    def list_projects(self, environ, user):
        user = self.require_user(user)
        rows = self.db.all("""SELECT p.*,c.name class_name,u.first_name lead_name,
            EXISTS(SELECT 1 FROM project_members pm WHERE pm.project_id=p.id AND pm.user_id=?) is_member,
            (SELECT COUNT(*) FROM tasks t WHERE t.project_id=p.id) task_count,
            (SELECT COUNT(*) FROM tasks t WHERE t.project_id=p.id AND t.status_key='done') done_count,
            (SELECT id FROM reports r WHERE r.project_id=p.id ORDER BY r.id DESC LIMIT 1) report_id
            FROM projects p JOIN classes c ON c.id=p.class_id JOIN users u ON u.id=p.project_lead_id
            WHERE (?='student' AND p.status IN ('draft','active'))
               OR EXISTS(SELECT 1 FROM project_members pm WHERE pm.project_id=p.id AND pm.user_id=?)
               OR p.project_lead_id=?
               OR (?='teacher' AND (EXISTS(
                    SELECT 1 FROM teacher_classes tc WHERE tc.teacher_id=? AND tc.class_id=p.class_id
               ) OR EXISTS(
                    SELECT 1 FROM support_requests sr JOIN teacher_classes tc ON tc.teacher_id=sr.teacher_id
                    WHERE sr.granted_admin_id=? AND sr.status='active' AND sr.access_expires_at>?
                      AND tc.class_id=p.class_id
               )))
            ORDER BY CASE p.status WHEN 'active' THEN 0 WHEN 'draft' THEN 1 ELSE 2 END,p.updated_at DESC""",
            (user["id"], user["role"], user["id"], user["id"], user["role"], user["id"], user["id"], utcnow()),
        )
        for row in rows:
            row["can_open"] = bool(row.pop("is_member") or self.can_manage_project(user, row))
            row["progress"] = round(100*row["done_count"]/row["task_count"]) if row["task_count"] else 0
        return rows

    def dashboard(self, environ, user):
        user = self.require_user(user)
        projects = [project for project in self.list_projects(environ, user) if project["can_open"]]
        today = date.today()
        week_end = today + timedelta(days=7)
        compact_tasks: list[dict[str, Any]] = []
        workload: dict[int, dict[str, Any]] = {}
        portfolio: list[dict[str, Any]] = []
        for project in projects:
            detail = self.project_detail(environ, user, project["id"])
            tasks = [task for task in detail["tasks"] if not task["is_shared_parent"]]
            open_tasks = [task for task in tasks if task["status_key"] != "done"]
            overdue = [task for task in open_tasks if task.get("due_at") and date.fromisoformat(str(task["due_at"])[:10]) < today]
            blocked = [task for task in open_tasks if task["status_key"] == "blocked"]
            waiting = [task for task in open_tasks if task["dependency_blocked"]]
            due_soon = [
                task for task in open_tasks
                if task.get("due_at") and today <= date.fromisoformat(str(task["due_at"])[:10]) <= week_end
            ]
            unassigned = [task for task in open_tasks if not task["assignees"]]
            milestones = [task for task in tasks if task["is_milestone"]]
            project_summary = dict(project)
            project_summary.update({
                "open_task_count": len(open_tasks),
                "blocked_task_count": len(blocked),
                "waiting_task_count": len(waiting),
                "overdue_task_count": len(overdue),
                "due_soon_task_count": len(due_soon),
                "unassigned_task_count": len(unassigned),
                "milestone_count": len(milestones),
                "completed_milestone_count": sum(task["status_key"] == "done" for task in milestones),
                "risk": "critical" if overdue else "warning" if blocked or due_soon else "on_track",
            })
            portfolio.append(project_summary)
            for task in tasks:
                task_summary = {
                    key: task.get(key) for key in (
                        "id", "title", "description", "status_key", "status_label", "status_color",
                        "system_kind", "team_id", "team_name", "phase_id", "phase_name", "weight",
                        "start_at", "due_at", "is_milestone", "blocked_reason", "dependency_blocked",
                    )
                }
                task_summary.update({
                    "project_id": project["id"], "project_title": project["title"],
                    "project_status": project["status"], "class_name": project["class_name"], "assignees": task["assignees"],
                    "dependencies": task["dependencies"],
                })
                compact_tasks.append(task_summary)
                if task["status_key"] == "done":
                    continue
                due_on = date.fromisoformat(str(task["due_at"])[:10]) if task.get("due_at") else None
                for assignee in task["assignees"]:
                    row = workload.setdefault(assignee["id"], {
                        "id": assignee["id"], "name": assignee["first_name"], "task_count": 0,
                        "weight": 0, "overdue_count": 0, "due_soon_count": 0,
                    })
                    row["task_count"] += 1
                    row["weight"] += int(task["weight"] or 0)
                    row["overdue_count"] += int(bool(due_on and due_on < today))
                    row["due_soon_count"] += int(bool(due_on and today <= due_on <= week_end))
        compact_tasks.sort(key=lambda task: (task.get("due_at") is None, task.get("due_at") or "", task["title"].casefold()))
        portfolio.sort(key=lambda project: ({"critical": 0, "warning": 1, "on_track": 2}[project["risk"]], -project["open_task_count"], project["title"].casefold()))
        workload_rows = sorted(workload.values(), key=lambda row: (-row["overdue_count"], -row["weight"], row["name"].casefold()))
        return {"projects": portfolio, "tasks": compact_tasks, "workload": workload_rows, "generated_on": today.isoformat()}

    def search_workspace(self, environ, user):
        query = str(self.query(environ).get("q", "")).strip()
        if len(query) < 2:
            return {"query": query, "projects": [], "tasks": [], "people": []}
        if len(query) > 100:
            raise HttpError(400, "Der Suchbegriff darf höchstens 100 Zeichen enthalten.")
        needle = query.casefold()
        data = self.dashboard(environ, user)
        projects = [
            project for project in data["projects"]
            if needle in " ".join(str(project.get(key) or "") for key in ("title", "description", "class_name", "lead_name")).casefold()
        ][:20]
        tasks = [
            task for task in data["tasks"]
            if needle in " ".join([
                str(task.get("title") or ""), str(task.get("description") or ""),
                str(task.get("project_title") or ""), str(task.get("team_name") or ""),
                " ".join(person["first_name"] for person in task["assignees"]),
            ]).casefold()
        ][:40]
        people_by_id: dict[int, dict[str, Any]] = {}
        for task in data["tasks"]:
            for person in task["assignees"]:
                if needle in person["first_name"].casefold():
                    item = people_by_id.setdefault(person["id"], {
                        "id": person["id"], "name": person["first_name"], "projects": {}, "open_task_count": 0,
                    })
                    item["projects"][task["project_id"]] = task["project_title"]
                    item["open_task_count"] += int(task["status_key"] != "done")
        people = [
            {**person, "projects": [{"id": project_id, "title": title} for project_id, title in person["projects"].items()]}
            for person in people_by_id.values()
        ][:20]
        return {"query": query, "projects": projects, "tasks": tasks, "people": people}

    def create_project(self, environ, user):
        user = self.require_teacher(user)
        data = self.body(environ)
        title = str(data.get("title","")).strip()
        class_id = int(data.get("class_id") or 0); lead_id=int(data.get("project_lead_id") or 0)
        self.require_class_access(user, class_id)
        active_license = None if user.get("is_owner") else self.active_teacher_license(user["id"])
        if active_license and active_license["plan"] == "beta" and self.db.one(
            """SELECT 1 ok FROM projects p
                 JOIN teacher_classes tc ON tc.class_id=p.class_id
                WHERE tc.teacher_id=? AND p.status IN ('draft','active') LIMIT 1""",
            (user["id"],),
        ):
            raise HttpError(
                409,
                "Im vierwöchigen Praxistest kann gleichzeitig ein aktives Projekt genutzt werden. "
                "Archivieren Sie das bisherige Projekt oder wechseln Sie in eine reguläre Lizenz.",
            )
        if not title: raise HttpError(400,"Projekttitel fehlt")
        if len(title)>200: raise HttpError(400,"Der Projekttitel darf höchstens 200 Zeichen enthalten")
        if len(str(data.get("description","")))>20_000: raise HttpError(400,"Die Projektbeschreibung darf höchstens 20.000 Zeichen enthalten")
        has_start = int(bool(data.get("has_start", True)))
        has_end = int(bool(data.get("has_end", True)))
        start_has_time = int(bool(data.get("start_has_time", False))) if has_start else 0
        end_has_time = int(bool(data.get("end_has_time", False))) if has_end else 0
        today_value = datetime.now().astimezone().date().isoformat()
        start_at = parse_project_date(data.get("start_at"), "Projektanfang", bool(start_has_time)) if has_start else today_value
        end_at = parse_project_date(data.get("end_at"), "Projektende", bool(end_has_time)) if has_end else start_at
        if has_start and has_end and datetime.fromisoformat(end_at) < datetime.fromisoformat(start_at):
            raise HttpError(400, "Das Projektende darf nicht vor dem Projektstart liegen.")
        member_ids = [int(x) for x in data.get("member_ids",[])]
        if lead_id not in member_ids: member_ids.append(lead_id)
        with self.db.transaction() as connection:
            if not connection.execute("SELECT 1 FROM classes WHERE id=? AND active=1",(class_id,)).fetchone(): raise HttpError(400,"Ungültige Projektklasse")
            if not connection.execute("""SELECT 1 FROM users u WHERE u.id=? AND u.active=1 AND (
                u.class_id=? OR (u.role='teacher' AND EXISTS(
                    SELECT 1 FROM teacher_classes tc WHERE tc.teacher_id=u.id AND tc.class_id=?
                ))
            )""",(lead_id,class_id,class_id)).fetchone(): raise HttpError(400,"Ungültige Gesamtprojektleitung")
            cur=connection.execute("""INSERT INTO projects(class_id,title,description,start_at,end_at,has_start,has_end,start_has_time,end_has_time,project_lead_id,status,team_min_size,team_max_size,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,'active',?,?,?,?)""",(class_id,title,str(data.get("description","")),start_at,end_at,has_start,has_end,start_has_time,end_has_time,lead_id,data.get("team_min_size"),data.get("team_max_size"),utcnow(),utcnow()))
            project_id=cur.lastrowid
            for member_id in set(member_ids):
                if not connection.execute("""SELECT 1 FROM users u WHERE u.id=? AND u.active=1 AND (
                    u.class_id=? OR (u.role='teacher' AND EXISTS(
                        SELECT 1 FROM teacher_classes tc WHERE tc.teacher_id=u.id AND tc.class_id=?
                    ))
                )""",(member_id,class_id,class_id)).fetchone(): raise HttpError(400,"Ein Projektmitglied gehört nicht zur Projektklasse")
                connection.execute("INSERT INTO project_members(project_id,user_id,joined_at) VALUES(?,?,?)",(project_id,member_id,utcnow()))
            if user["id"] not in member_ids: connection.execute("INSERT OR IGNORE INTO project_members(project_id,user_id,joined_at) VALUES(?,?,?)",(project_id,user["id"],utcnow()))
            seed_project_statuses(connection,project_id)
            template_id = int(data.get("template_id") or 0)
            if template_id:
                template = connection.execute(
                    "SELECT t.snapshot_json,p.class_id FROM templates t JOIN projects p ON p.id=t.source_project_id WHERE t.id=?",
                    (template_id,),
                ).fetchone()
                if not template:
                    raise HttpError(400, "Projektvorlage nicht gefunden")
                if not self.can_access_class(user, int(template["class_id"])):
                    raise HttpError(403, "Diese Projektvorlage gehört nicht zu einer für Sie freigeschalteten Klasse.")
                snapshot = json.loads(template["snapshot_json"])
                phase_map: dict[int, int] = {}
                team_map: dict[int, int] = {}
                source_start = datetime.fromisoformat(snapshot.get("source_start", start_at))
                target_start = datetime.fromisoformat(start_at)
                offset = target_start - source_start
                def shifted(value: str | None, fallback: str) -> str:
                    if not value:
                        return fallback
                    return (datetime.fromisoformat(value) + offset).isoformat(timespec="minutes")
                for team in snapshot.get("teams", []):
                    team_cursor = connection.execute(
                        "INSERT INTO teams(project_id,name,responsibility,status,color,created_by,created_at) VALUES(?,?,?,'active',?,?,?)",
                        (project_id, team["name"], team.get("responsibility", ""), team.get("color", "#6F8B74"), user["id"], utcnow()),
                    )
                    team_map[int(team["id"])] = int(team_cursor.lastrowid)
                for phase in snapshot.get("phases", []):
                    phase_cursor = connection.execute(
                        """INSERT INTO phases(project_id,name,description,expected_result,start_at,end_at,sort_order,locked,created_at)
                           VALUES(?,?,?,?,?,?,?,0,?)""",
                        (project_id, phase["name"], phase.get("description", ""), phase.get("expected_result", ""),
                         shifted(phase.get("start_at"), start_at), shifted(phase.get("end_at"), end_at), phase.get("sort_order", 0), utcnow()),
                    )
                    phase_map[int(phase["id"])] = int(phase_cursor.lastrowid)
                for task in snapshot.get("tasks", []):
                    connection.execute(
                        """INSERT INTO tasks(project_id,team_id,phase_id,title,description,status_key,weight,start_at,due_at,requires_final_approval,is_milestone,created_by,created_at,updated_at)
                           VALUES(?,?,?,?,?,'open',?,?,?,?,?,?,?,?)""",
                        (project_id, team_map.get(task.get("team_id")), phase_map.get(task.get("phase_id")), task["title"], task.get("description", ""),
                         task.get("weight", 2), shifted(task.get("start_at"), start_at), shifted(task.get("due_at"), end_at),
                         task.get("requires_final_approval", 1), int(bool(task.get("is_milestone"))), user["id"], utcnow(), utcnow()),
                    )
            Database.event(connection,project_id,user["id"],"project.created",f"Projekt „{title}“ wurde gestartet.")
        return {"id":project_id}

    def project_detail(self, environ, user, project_id):
        user, project = self.project_access(user,project_id)
        result = dict(project)
        if not result.get("has_start", 1):
            result["start_at"] = None
        if not result.get("has_end", 1):
            result["end_at"] = None
        result["class_name"] = self.db.one("SELECT name FROM classes WHERE id=?",(project["class_id"],))["name"]
        result["can_manage"] = self.can_manage_project(user, project)
        result["members"] = self.db.all("""SELECT u.id,u.first_name,u.username,u.role, EXISTS(SELECT 1 FROM team_members tm JOIN teams t ON t.id=tm.team_id WHERE t.project_id=? AND tm.user_id=u.id) has_team FROM project_members pm JOIN users u ON u.id=pm.user_id WHERE pm.project_id=? AND u.active=1 ORDER BY u.first_name""",(project_id,project_id))
        result["class_students"] = self.db.all(
            """SELECT u.id,u.first_name,u.username,
               t.id team_id,t.name team_name,COALESCE(tm.is_lead,0) is_lead
               FROM users u
               LEFT JOIN team_members tm ON tm.user_id=u.id
                 AND tm.team_id IN (SELECT id FROM teams WHERE project_id=? AND status!='rejected')
               LEFT JOIN teams t ON t.id=tm.team_id
               WHERE u.class_id=? AND u.role='student' AND u.active=1
               ORDER BY u.first_name,u.username""",
            (project_id, project["class_id"]),
        )
        result["teams"] = self.db.all("""SELECT t.*, (SELECT COUNT(*) FROM team_members tm WHERE tm.team_id=t.id) member_count FROM teams t WHERE t.project_id=? ORDER BY t.status,name""",(project_id,))
        for team in result["teams"]:
            team["members"] = self.db.all("""SELECT u.id,u.first_name,u.username,tm.is_lead,tm.business_role FROM team_members tm JOIN users u ON u.id=tm.user_id WHERE tm.team_id=? AND u.active=1 ORDER BY tm.is_lead DESC,u.first_name""",(team["id"],))
        result["phases"] = self.db.all("SELECT * FROM phases WHERE project_id=? ORDER BY sort_order,id",(project_id,))
        result["statuses"] = self.db.all("SELECT * FROM task_statuses WHERE project_id=? AND active=1 ORDER BY sort_order",(project_id,))
        visible_team_ids = [row["team_id"] for row in self.db.all("SELECT team_id FROM team_members WHERE user_id=? AND team_id IN (SELECT id FROM teams WHERE project_id=? AND status!='rejected')", (user["id"], project_id))]
        if result["can_manage"]:
            task_scope_sql, task_scope_params = "", []
        elif visible_team_ids:
            task_scope_sql, task_scope_params = f" AND (t.team_id IS NULL OR t.team_id IN ({','.join('?' for _ in visible_team_ids)}))", visible_team_ids
        else:
            task_scope_sql, task_scope_params = " AND t.team_id IS NULL", []
        result["tasks"] = self.db.all(f"""SELECT t.*,tm.name team_name,ph.name phase_name,s.label status_label,s.color status_color,s.system_kind FROM tasks t LEFT JOIN teams tm ON tm.id=t.team_id LEFT JOIN phases ph ON ph.id=t.phase_id LEFT JOIN task_statuses s ON s.project_id=t.project_id AND s.key=t.status_key WHERE t.project_id=?{task_scope_sql} ORDER BY t.created_at""", tuple([project_id] + task_scope_params))
        for task in result["tasks"]:
            task["assignees"]=self.db.all("SELECT u.id,u.first_name FROM task_assignees a JOIN users u ON u.id=a.user_id WHERE a.task_id=? AND u.active=1 ORDER BY u.first_name",(task["id"],))
            task["dependencies"]=self.db.all(
                """SELECT dependency.id depends_on_id,dependency.title,dependency.status_key
                     FROM task_dependencies relation JOIN tasks dependency ON dependency.id=relation.depends_on_id
                     WHERE relation.task_id=? ORDER BY dependency.due_at,dependency.title""",
                (task["id"],),
            )
            task["dependency_blocked"] = any(dependency["status_key"] != "done" for dependency in task["dependencies"])
            task["comments"]=self.db.all("""SELECT c.*,u.first_name author_name FROM comments c JOIN users u ON u.id=c.author_id WHERE c.task_id=? ORDER BY c.created_at""",(task["id"],))
            task["upload_count"]=self.db.one("SELECT COUNT(*) count FROM uploads WHERE task_id=?",(task["id"],))["count"]
            task["deadline_requests"]=self.db.all("""SELECT dr.*,u.first_name requested_by_name,d.first_name decided_by_name
                FROM deadline_requests dr JOIN users u ON u.id=dr.requested_by LEFT JOIN users d ON d.id=dr.decided_by
                WHERE dr.task_id=? ORDER BY dr.created_at DESC""",(task["id"],))
        if result["can_manage"]:
            upload_scope_sql, upload_scope_params = "", []
        elif visible_team_ids:
            upload_scope_sql, upload_scope_params = f" AND (u.team_id IS NULL OR u.team_id IN ({','.join('?' for _ in visible_team_ids)}))", visible_team_ids
        else:
            upload_scope_sql, upload_scope_params = " AND u.team_id IS NULL", []
        result["uploads"] = self.db.all(f"""SELECT u.id,u.task_id,u.team_id,u.original_name,u.media_type,u.size_bytes,u.created_at,t.name team_name
            FROM uploads u LEFT JOIN teams t ON t.id=u.team_id WHERE u.project_id=?{upload_scope_sql} ORDER BY u.created_at DESC""", tuple([project_id] + upload_scope_params))
        result["reports"] = self.db.all("SELECT * FROM reports WHERE project_id=? ORDER BY created_at DESC",(project_id,))
        result["progress"] = self.progress(project_id)
        return result

    def progress(self, project_id:int)->dict[str,Any]:
        rows=self.db.all("SELECT weight,status_key,phase_id,team_id FROM tasks WHERE project_id=? AND is_shared_parent=0",(project_id,))
        total=sum(r["weight"] for r in rows); done=sum(r["weight"] for r in rows if r["status_key"]=="done")
        return {"overall":round(100*done/total) if total else 0,"done_weight":done,"total_weight":total}

    def update_project(self,environ,user,project_id):
        user,project=self.project_access(user,project_id,manage=True); data=self.body(environ)
        if "title" in data and (not str(data["title"]).strip() or len(str(data["title"]).strip()) > 200):
            raise HttpError(400, "Der Projekttitel muss 1 bis 200 Zeichen lang sein")
        if "description" in data and len(str(data["description"])) > 20_000:
            raise HttpError(400, "Die Projektbeschreibung darf höchstens 20.000 Zeichen enthalten")
        if "status" in data and data["status"] not in {"draft", "active", "completed"}:
            raise HttpError(400, "Ungültiger Projektstatus")
        if "project_lead_id" in data:
            lead_id = int(data["project_lead_id"] or 0)
            if not self.db.one("""SELECT 1 ok FROM users u WHERE u.id=? AND u.active=1 AND (
                u.class_id=? OR (u.role='teacher' AND EXISTS(
                    SELECT 1 FROM teacher_classes tc WHERE tc.teacher_id=u.id AND tc.class_id=?
                ))
            )""", (lead_id, project["class_id"], project["class_id"])):
                raise HttpError(400, "Ungültige Gesamtprojektleitung")
            if not self.db.one("SELECT 1 ok FROM project_members WHERE project_id=? AND user_id=?", (project_id, lead_id)):
                raise HttpError(400, "Die Gesamtprojektleitung muss Projektmitglied sein")
        fields=[];params=[]
        for key in ("title","description","start_at","end_at","has_start","has_end","start_has_time","end_has_time","status","team_min_size","team_max_size","project_lead_id","allow_student_organization"):
            if key in data: fields.append(f"{key}=?");params.append(data[key])
        if fields:
            params += [utcnow(),project_id]; self.db.execute(f"UPDATE projects SET {','.join(fields)},updated_at=? WHERE id=?",tuple(params))
        return {"ok":True}

    def update_status(self, environ, user, project_id, status_id):
        self.project_access(user, project_id, manage=True)
        status = self.db.one("SELECT * FROM task_statuses WHERE id=? AND project_id=?", (status_id, project_id))
        if not status:
            raise HttpError(404, "Status nicht gefunden")
        data = self.body(environ)
        label = str(data.get("label", status["label"])).strip()
        color = str(data.get("color", status["color"])).strip()
        if not label or not re.fullmatch(r"#[0-9A-Fa-f]{6}", color):
            raise HttpError(400, "Statusbezeichnung oder Farbe ist ungültig")
        self.db.execute("UPDATE task_statuses SET label=?,color=?,sort_order=?,active=? WHERE id=?",
                        (label, color, int(data.get("sort_order", status["sort_order"])), int(data.get("active", status["active"])), status_id))
        return {"ok": True}

    def add_project_member(self,environ,user,project_id):
        user,project=self.project_access(user,project_id,manage=True); member_id=int(self.body(environ).get("user_id") or 0)
        account=self.db.one("SELECT * FROM users WHERE id=? AND class_id=? AND active=1",(member_id,project["class_id"]))
        if not account: raise HttpError(400,"Person gehört nicht zur Projektklasse")
        self.db.execute("INSERT OR IGNORE INTO project_members(project_id,user_id,joined_at) VALUES(?,?,?)",(project_id,member_id,utcnow()))
        return {"ok":True}

    def remove_project_member(self,environ,user,project_id,user_id):
        user,project=self.project_access(user,project_id,manage=True)
        if user_id in {project["project_lead_id"],user["id"]}:
            raise HttpError(409,"Gesamtprojektleitung und Geschäftsführung können nicht entfernt werden")
        if self.db.one("SELECT 1 ok FROM team_members tm JOIN teams t ON t.id=tm.team_id WHERE t.project_id=? AND tm.user_id=?",(project_id,user_id)):
            raise HttpError(409,"Entfernen Sie die Person zuerst aus ihrem Projektteam")
        with self.db.transaction() as connection:
            connection.execute("DELETE FROM task_assignees WHERE user_id=? AND task_id IN (SELECT id FROM tasks WHERE project_id=?)",(user_id,project_id))
            connection.execute("DELETE FROM project_members WHERE project_id=? AND user_id=?",(project_id,user_id))
            Database.event(connection,project_id,user["id"],"project.member_removed","Ein Projektmitglied wurde entfernt; offene Zuständigkeiten wurden gelöst.")
        return {"ok":True}

    def create_phase(self,environ,user,project_id):
        user,project=self.project_access(user,project_id,manage=True); data=self.body(environ)
        phase_id=self.db.execute("""INSERT INTO phases(project_id,name,description,expected_result,start_at,end_at,sort_order,locked,created_at) VALUES(?,?,?,?,?,?,?,?,?)""",(project_id,str(data.get("name","")).strip(),str(data.get("description","")),str(data.get("expected_result","")),parse_datetime(data.get("start_at"),"Start"),parse_datetime(data.get("end_at"),"Ende"),int(data.get("sort_order",0)),int(bool(data.get("locked"))),utcnow()))
        return {"id":phase_id}

    def update_phase(self,environ,user,phase_id):
        phase=self.db.one("SELECT * FROM phases WHERE id=?",(phase_id,));
        if not phase: raise HttpError(404,"Phase nicht gefunden")
        self.project_access(user,phase["project_id"],manage=True); data=self.body(environ)
        fields=[];params=[]
        for key in ("name","description","expected_result","start_at","end_at","sort_order","locked"):
            if key in data: fields.append(f"{key}=?");params.append(int(bool(data[key])) if key=="locked" else data[key])
        if fields: self.db.execute(f"UPDATE phases SET {','.join(fields)} WHERE id=?",tuple(params+[phase_id]))
        return {"ok":True}

    def create_team(self,environ,user,project_id):
        user,project=self.project_access(user,project_id); data=self.body(environ)
        manager=self.can_manage_project(user, project)
        if not manager and not project.get("allow_student_organization",0):
            raise HttpError(403,"Die selbstständige Teamorganisation ist derzeit deaktiviert")
        name = str(data.get("name", "")).strip()
        if not name:
            raise HttpError(400, "Bitte geben Sie einen Teamnamen ein, zum Beispiel „Einkauf“ oder „Marketing“.")
        if len(name) > 80:
            raise HttpError(400, "Der Teamname darf höchstens 80 Zeichen lang sein.")
        if self.db.one("SELECT 1 ok FROM teams WHERE project_id=? AND name=? COLLATE NOCASE AND status!='rejected'", (project_id, name)):
            raise HttpError(409, "Dieser Teamname wird im Projekt bereits verwendet. Bitte wählen Sie einen eindeutigen Namen.")
        color = str(data.get("color", "")).upper()
        if color not in TEAM_COLORS:
            raise HttpError(400, "Bitte wählen Sie eine der vorgegebenen Teamfarben.")
        if self.db.one("SELECT 1 ok FROM teams WHERE project_id=? AND color=? AND status!='rejected'", (project_id, color)):
            raise HttpError(409, "Diese Teamfarbe wird im Projekt bereits verwendet.")
        member_ids = {int(value) for value in data.get("member_ids", [])}
        lead_ids = {int(value) for value in data.get("lead_ids", [])}
        if not lead_ids.issubset(member_ids):
            raise HttpError(400, "Teamleitungen müssen zugleich Teammitglieder sein.")
        try:
            with self.db.transaction() as connection:
                cursor = connection.execute(
                    "INSERT INTO teams(project_id,name,responsibility,status,color,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                    (project_id,name,str(data.get("responsibility","")),"active" if manager else "proposed",color,user["id"],utcnow()),
                )
                team_id = int(cursor.lastrowid)
                for member_id in member_ids:
                    account = connection.execute(
                        "SELECT id FROM users WHERE id=? AND class_id=? AND role='student' AND active=1",
                        (member_id, project["class_id"]),
                    ).fetchone()
                    if not account:
                        raise HttpError(400, "Eine ausgewählte Person gehört nicht zur Projektklasse.")
                    occupied = connection.execute(
                        """SELECT t.name FROM team_members tm JOIN teams t ON t.id=tm.team_id
                           WHERE tm.user_id=? AND t.project_id=? AND t.status!='rejected'""",
                        (member_id, project_id),
                    ).fetchone()
                    if occupied:
                        raise HttpError(409, f"Eine ausgewählte Person gehört bereits zu „{occupied['name']}“.")
                    connection.execute("INSERT OR IGNORE INTO project_members(project_id,user_id,joined_at) VALUES(?,?,?)", (project_id,member_id,utcnow()))
                    connection.execute(
                        "INSERT INTO team_members(team_id,user_id,is_lead,business_role,joined_at) VALUES(?,?,?,?,?)",
                        (team_id,member_id,int(member_id in lead_ids),"",utcnow()),
                    )
        except sqlite3.IntegrityError: raise HttpError(409,"Teamname oder Teamfarbe bereits vergeben")
        if not manager:self.notify_leads(project_id,None,f"Das Team „{name}“ wartet auf Freigabe.")
        return {"id":team_id,"status":"active" if manager else "proposed"}

    def update_team(self,environ,user,team_id):
        team=self.db.one("SELECT * FROM teams WHERE id=?",(team_id,));
        if not team: raise HttpError(404,"Team nicht gefunden")
        self.project_access(user,team["project_id"],manage=True); data=self.body(environ);fields=[];params=[]
        for key in ("name","responsibility","status","color"):
            if key in data:
                value = str(data[key]).upper() if key == "color" else str(data[key]).strip() if key == "name" else data[key]
                if key == "name":
                    if not value:
                        raise HttpError(400, "Der Teamname darf nicht leer sein.")
                    if self.db.one("SELECT 1 ok FROM teams WHERE project_id=? AND name=? COLLATE NOCASE AND id<>? AND status!='rejected'", (team["project_id"], value, team_id)):
                        raise HttpError(409, "Dieser Teamname wird im Projekt bereits verwendet. Bitte wählen Sie einen eindeutigen Namen.")
                if key == "color":
                    if value not in TEAM_COLORS:
                        raise HttpError(400, "Bitte wählen Sie eine der vorgegebenen Teamfarben.")
                    if self.db.one("SELECT 1 ok FROM teams WHERE project_id=? AND color=? AND id<>? AND status!='rejected'", (team["project_id"], value, team_id)):
                        raise HttpError(409, "Diese Teamfarbe wird im Projekt bereits verwendet.")
                fields.append(f"{key}=?");params.append(value)
        if fields:self.db.execute(f"UPDATE teams SET {','.join(fields)} WHERE id=?",tuple(params+[team_id]))
        return {"ok":True}

    def set_team_member(self,environ,user,team_id):
        team=self.db.one("SELECT * FROM teams WHERE id=?",(team_id,));
        if not team:raise HttpError(404,"Team nicht gefunden")
        user,project=self.project_access(user,team["project_id"]); data=self.body(environ); member_id=int(data.get("user_id") or 0)
        is_manager=self.can_manage_project(user, project)
        is_lead=self.db.one("SELECT 1 ok FROM team_members WHERE team_id=? AND user_id=? AND is_lead=1",(team_id,user["id"]))
        if not is_manager and not is_lead:raise HttpError(403,"Nur Leitungsrollen dürfen Teams besetzen")
        other=self.db.one("SELECT t.name FROM team_members tm JOIN teams t ON t.id=tm.team_id WHERE tm.user_id=? AND t.project_id=? AND tm.team_id<>?",(member_id,team["project_id"],team_id))
        if other:raise HttpError(409,f"Die Person gehört bereits zu „{other['name']}“.")
        account = self.db.one("SELECT 1 ok FROM users WHERE id=? AND class_id=? AND role='student' AND active=1", (member_id, project["class_id"]))
        if not account: raise HttpError(400,"Person gehört nicht zur Projektklasse")
        with self.db.transaction() as connection:
            connection.execute("INSERT OR IGNORE INTO project_members(project_id,user_id,joined_at) VALUES(?,?,?)",(team["project_id"],member_id,utcnow()))
            connection.execute("INSERT INTO team_members(team_id,user_id,is_lead,business_role,joined_at) VALUES(?,?,?,?,?) ON CONFLICT(team_id,user_id) DO UPDATE SET is_lead=excluded.is_lead,business_role=excluded.business_role",(team_id,member_id,int(bool(data.get("is_lead"))),str(data.get("business_role","")),utcnow()))
        return {"ok":True}

    def remove_team_member(self,environ,user,team_id,user_id):
        team=self.db.one("SELECT * FROM teams WHERE id=?",(team_id,));
        if not team:raise HttpError(404,"Team nicht gefunden")
        self.project_access(user,team["project_id"],manage=True)
        self.db.execute("DELETE FROM team_members WHERE team_id=? AND user_id=?",(team_id,user_id));self.db.execute("DELETE FROM task_assignees WHERE user_id=? AND task_id IN (SELECT id FROM tasks WHERE team_id=?)",(user_id,team_id))
        return {"ok":True}

    def can_create_task(self,user,project_id,team_id)->bool:
        _,project=self.project_access(user,project_id)
        if self.can_manage_project(user, project):return True
        return bool(team_id and self.db.one("SELECT 1 ok FROM team_members WHERE team_id=? AND user_id=? AND is_lead=1",(team_id,user["id"])))

    def create_task(self,environ,user,project_id):
        user,project=self.project_access(user,project_id); data=self.body(environ)
        manager=self.can_manage_project(user,project)
        try:
            team_ids=parse_id_list(data.get("team_ids",[]),"Die Teamauswahl")
            if not team_ids and data.get("team_id"):team_ids=[int(data["team_id"])]
            assignee_ids=parse_id_list(data.get("assignee_ids",[]),"Die Auswahl der Verantwortlichen")
            weight=int(data.get("weight",2))
            phase_id=int(data.get("phase_id") or 0) or None
        except (TypeError, ValueError):
            raise HttpError(400,"Teams, Verantwortliche oder Umfang sind ungültig.")
        if weight not in {1,2,3,5}:raise HttpError(400,"Ungültiger Aufgabenumfang.")
        team_ids=list(dict.fromkeys(team_ids))
        if team_ids:
            placeholders=",".join("?" for _ in team_ids)
            valid_teams=self.db.one(
                f"SELECT COUNT(*) count FROM teams WHERE project_id=? AND status='active' AND id IN ({placeholders})",
                (project_id,*team_ids),
            )["count"]
            if valid_teams!=len(team_ids):raise HttpError(400,"Mindestens ein ausgewähltes Team gehört nicht zum aktiven Projekt")
        if not all(self.can_create_task(user,project_id,team_id) for team_id in (team_ids or [None])):raise HttpError(403,"Nur Teamleitungen dürfen Aufgaben für ihr Team erstellen")
        if len(team_ids)>1 and assignee_ids:
            raise HttpError(400,"Bei einer Aufgabe für mehrere Teams werden Verantwortliche anschließend je Teamkopie zugeordnet")
        if phase_id:
            phase=self.db.one("SELECT * FROM phases WHERE id=? AND project_id=?",(phase_id,project_id))
            if not phase:raise HttpError(400,"Ungültige Phase")
            if phase["locked"]:raise HttpError(409,"Diese Projektphase ist gesperrt")
        title=str(data.get("title","")).strip()
        if not title:raise HttpError(400,"Aufgabentitel fehlt")
        if len(title)>200:raise HttpError(400,"Der Aufgabentitel darf höchstens 200 Zeichen enthalten")
        if len(str(data.get("description","")))>20_000:raise HttpError(400,"Die Aufgabenbeschreibung darf höchstens 20.000 Zeichen enthalten")
        due_at = normalize_due_at(data.get("due_at"))
        start_at = parse_datetime(data.get("start_at"), "Aufgabenbeginn") if data.get("start_at") else None
        if start_at and due_at and comparable_datetime(due_at) < comparable_datetime(start_at):
            raise HttpError(400,"Die Aufgabenfrist darf nicht vor dem Aufgabenbeginn liegen.")
        if data.get("is_milestone") and not due_at:
            raise HttpError(400, "Ein Meilenstein benötigt ein Fälligkeitsdatum.")
        with self.db.transaction() as connection:
            shared=len(team_ids)>1
            parent_id=None
            if shared:
                cur=connection.execute("""INSERT INTO tasks(project_id,phase_id,title,description,status_key,weight,start_at,due_at,is_milestone,is_shared_parent,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,1,?,?,?)""",(project_id,phase_id,title,str(data.get("description","")),"open",weight,start_at,due_at,int(bool(data.get("is_milestone"))),user["id"],utcnow(),utcnow()));parent_id=cur.lastrowid
            created=[]
            for team_id in (team_ids or [None]):
                requires_final_approval=bool(data.get("requires_final_approval",1)) if manager else True
                cur=connection.execute("""INSERT INTO tasks(project_id,parent_id,team_id,phase_id,title,description,status_key,weight,start_at,due_at,requires_final_approval,is_milestone,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(project_id,parent_id,team_id,phase_id,title,str(data.get("description","")),"open",weight,start_at,due_at,int(requires_final_approval),int(bool(data.get("is_milestone"))),user["id"],utcnow(),utcnow()));created.append(cur.lastrowid)
                for assignee_id in assignee_ids:
                    if team_id:
                        allowed=connection.execute("SELECT 1 FROM team_members tm JOIN users u ON u.id=tm.user_id WHERE tm.team_id=? AND tm.user_id=? AND u.active=1",(team_id,assignee_id)).fetchone()
                    else:
                        allowed=connection.execute("SELECT 1 FROM project_members pm JOIN users u ON u.id=pm.user_id WHERE pm.project_id=? AND pm.user_id=? AND u.active=1",(project_id,assignee_id)).fetchone()
                    if not allowed:raise HttpError(400,"Verantwortliche müssen dem gewählten Team oder Projekt angehören")
                    connection.execute("INSERT INTO task_assignees(task_id,user_id,assigned_at) VALUES(?,?,?)",(cur.lastrowid,assignee_id,utcnow()))
                    connection.execute("INSERT INTO notifications(user_id,project_id,task_id,message,created_at) VALUES(?,?,?,?,?)",(assignee_id,project_id,cur.lastrowid,f"Ihnen wurde die Aufgabe „{title}“ zugewiesen.",utcnow()))
            Database.event(connection,project_id,user["id"],"task.created",f"Aufgabe „{title}“ wurde angelegt.")
        return {"ids":created,"shared_parent_id":parent_id}

    def update_task(self,environ,user,task_id):
        task=self.db.one("SELECT * FROM tasks WHERE id=?",(task_id,));
        if not task:raise HttpError(404,"Aufgabe nicht gefunden")
        user,project=self.task_scope_access(user,task); data=self.body(environ)
        phase=self.db.one("SELECT locked FROM phases WHERE id=?",(task["phase_id"],)) if task["phase_id"] else None
        if phase and phase["locked"]:raise HttpError(409,"Diese Projektphase ist gesperrt")
        manager=self.can_manage_project(user, project)
        team_lead=task["team_id"] and self.db.one("SELECT 1 ok FROM team_members WHERE team_id=? AND user_id=? AND is_lead=1",(task["team_id"],user["id"]))
        assignee=self.db.one("SELECT 1 ok FROM task_assignees WHERE task_id=? AND user_id=?",(task_id,user["id"]))
        if not (manager or team_lead or assignee):raise HttpError(403,"Sie dürfen diese Aufgabe nicht bearbeiten")
        leadership_fields={"title","description","weight","start_at","due_at","phase_id","is_milestone"}
        if not(manager or team_lead) and leadership_fields.intersection(data):raise HttpError(403,"Nur Team- oder Projektleitungen dürfen die Aufgabenplanung ändern")
        if "requires_final_approval" in data and not manager:raise HttpError(403,"Nur die Gesamtprojektleitung darf den Freigabeweg ändern")
        if task["status_key"]=="done" and not manager:raise HttpError(403,"Eine freigegebene Aufgabe kann nur durch die Gesamtprojektleitung geändert werden")
        status=data.get("status_key")
        if status is not None and not self.db.one("SELECT 1 ok FROM task_statuses WHERE project_id=? AND key=? AND active=1", (task["project_id"], status)):
            raise HttpError(400, "Ungültiger Aufgabenstatus")
        if "phase_id" in data:
            try: data["phase_id"]=int(data["phase_id"]) if data.get("phase_id") else None
            except (TypeError,ValueError):raise HttpError(400,"Ungültige Projektphase")
            target_phase=self.db.one("SELECT * FROM phases WHERE id=? AND project_id=?",(data["phase_id"],task["project_id"])) if data["phase_id"] else None
            if data["phase_id"] and not target_phase:raise HttpError(400,"Ungültige Projektphase")
            if target_phase and target_phase["locked"] and data["phase_id"]!=task["phase_id"]:raise HttpError(409,"Diese Projektphase ist gesperrt")
        if "title" in data and not str(data.get("title") or "").strip():raise HttpError(400,"Aufgabentitel fehlt")
        if "weight" in data:
            try: weight=int(data["weight"])
            except (TypeError,ValueError):raise HttpError(400,"Ungültiger Aufgabenumfang.")
            if weight not in {1,2,3,5}:raise HttpError(400,"Ungültiger Aufgabenumfang.")
            data["weight"]=weight
        if "start_at" in data:data["start_at"]=parse_datetime(data["start_at"],"Aufgabenbeginn") if data["start_at"] else None
        if "due_at" in data:data["due_at"]=normalize_due_at(data["due_at"])
        next_start=data.get("start_at",task.get("start_at"));next_due=data.get("due_at",task.get("due_at"))
        if next_start and next_due and comparable_datetime(next_due)<comparable_datetime(next_start):raise HttpError(400,"Die Aufgabenfrist darf nicht vor dem Aufgabenbeginn liegen.")
        if status=="approval" and status!=task["status_key"] and not team_lead and not manager:raise HttpError(403,"Vor der Freigabe ist eine Teamprüfung erforderlich")
        if status in {"revision","done"} and status!=task["status_key"] and not (manager or team_lead):raise HttpError(403,"Nur Leitungsrollen dürfen diesen Status vergeben")
        if status=="done" and task["requires_final_approval"] and not manager:
            raise HttpError(403,"Diese Aufgabe benötigt die Freigabe der Gesamtprojektleitung oder Geschäftsführung")
        if status == "done" and self.db.one(
            """SELECT 1 ok FROM task_dependencies relation JOIN tasks dependency ON dependency.id=relation.depends_on_id
                 WHERE relation.task_id=? AND dependency.status_key!='done' LIMIT 1""",
            (task_id,),
        ):
            raise HttpError(409, "Diese Aufgabe kann erst abgeschlossen werden, wenn alle Vorgängeraufgaben erledigt sind.")
        if status is not None and status != "done" and task["status_key"] == "done" and self.db.one(
            """SELECT 1 ok FROM task_dependencies relation JOIN tasks successor ON successor.id=relation.task_id
                 WHERE relation.depends_on_id=? AND successor.status_key='done' LIMIT 1""",
            (task_id,),
        ):
            raise HttpError(409, "Die Aufgabe kann nicht wieder geöffnet werden, solange eine abhängige Folgeaufgabe bereits erledigt ist.")
        next_milestone=bool(data.get("is_milestone",task.get("is_milestone")))
        if next_milestone and not next_due:
            raise HttpError(400, "Ein Meilenstein benötigt ein Fälligkeitsdatum.")
        if status in {"blocked","revision"} and not str(data.get("blocked_reason",task["blocked_reason"])).strip():raise HttpError(400,"Bitte geben Sie einen Grund an")
        fields=[];params=[]
        allowed=("title","description","result_text","status_key","weight","start_at","due_at","phase_id","blocked_reason","is_milestone") if (manager or team_lead) else ("result_text","status_key","blocked_reason")
        if manager:allowed+=("requires_final_approval",)
        limits={"title":200,"description":20_000,"result_text":20_000,"blocked_reason":4_000}
        for key in allowed:
            if key in data:
                if key in limits and len(str(data[key] or ""))>limits[key]:raise HttpError(400,"Der eingegebene Text ist zu lang")
                fields.append(f"{key}=?")
                value=str(data[key]).strip() if key=="title" else data[key]
                params.append(int(bool(value)) if key in {"is_milestone","requires_final_approval"} else value or None if key=="phase_id" else value)
        if fields:
            with self.db.transaction() as connection:
                connection.execute(f"UPDATE tasks SET {','.join(fields)},updated_at=? WHERE id=?",tuple(params+[utcnow(),task_id]))
                Database.event(connection,task["project_id"],user["id"],"task.updated",f"Aufgabe „{task['title']}“ wurde aktualisiert.")
        if status in {"blocked","approval","revision","done"} and status!=task["status_key"]:
            message={"blocked":"Aufgabe wurde als blockiert gemeldet.","approval":"Aufgabe wartet auf Freigabe.","revision":"Aufgabe wurde zur Überarbeitung zurückgegeben.","done":"Aufgabe wurde freigegeben."}[status]
            self.notify_leads(task["project_id"],task_id,message)
        return {"ok":True}

    def delete_task(self,environ,user,task_id):
        task=self.db.one("SELECT * FROM tasks WHERE id=?",(task_id,))
        if not task:raise HttpError(404,"Aufgabe nicht gefunden")
        user,project=self.task_scope_access(user,task)
        manager=self.can_manage_project(user,project)
        team_lead=bool(task["team_id"] and self.db.one("SELECT 1 ok FROM team_members WHERE team_id=? AND user_id=? AND is_lead=1",(task["team_id"],user["id"])))
        if not(manager or team_lead):raise HttpError(403,"Nur Team- oder Projektleitungen dürfen Aufgaben löschen.")
        if task["status_key"]=="done" and not manager:raise HttpError(403,"Freigegebene Aufgaben dürfen nur durch die Gesamtprojektleitung gelöscht werden.")
        phase=self.db.one("SELECT locked FROM phases WHERE id=?",(task["phase_id"],)) if task["phase_id"] else None
        if phase and phase["locked"]:raise HttpError(409,"Aufgaben einer gesperrten Projektphase können nicht gelöscht werden.")
        if self.db.one("""SELECT 1 ok FROM task_dependencies relation JOIN tasks successor ON successor.id=relation.task_id
                           WHERE relation.depends_on_id=? AND successor.status_key='done' LIMIT 1""",(task_id,)):
            raise HttpError(409,"Die Aufgabe kann nicht gelöscht werden, weil eine abhängige Folgeaufgabe bereits erledigt ist.")
        parent_id=task.get("parent_id")
        upload_names=[row["stored_name"] for row in self.db.all("SELECT stored_name FROM uploads WHERE task_id=?",(task_id,))]
        with self.db.transaction() as connection:
            connection.execute("DELETE FROM tasks WHERE id=?",(task_id,))
            if parent_id and not connection.execute("SELECT 1 FROM tasks WHERE parent_id=?",(parent_id,)).fetchone():
                connection.execute("DELETE FROM tasks WHERE id=?",(parent_id,))
            Database.event(connection,task["project_id"],user["id"],"task.deleted",f"Aufgabe „{task['title']}“ wurde gelöscht.")
        for stored_name in upload_names:
            try:(self.config.upload_dir/stored_name).unlink(missing_ok=True)
            except OSError as error:warnings.warn(f"Aufgabendatei konnte nach dem Löschen nicht entfernt werden: {stored_name}: {error}")
        return {"ok":True}

    def notify_leads(self,project_id,task_id,message):
        project=self.db.one("SELECT project_lead_id,class_id FROM projects WHERE id=?",(project_id,));teachers=self.db.all("""SELECT DISTINCT u.id FROM users u
            JOIN teacher_classes tc ON tc.teacher_id=u.id AND tc.class_id=?
            WHERE u.role='teacher' AND u.is_owner=0 AND u.active=1""",(project["class_id"],))
        ids={project["project_lead_id"],*(x["id"] for x in teachers)}
        with self.db.transaction() as connection:
            for user_id in ids:connection.execute("INSERT INTO notifications(user_id,project_id,task_id,message,created_at) VALUES(?,?,?,?,?)",(user_id,project_id,task_id,message,utcnow()))

    def set_task_assignees(self,environ,user,task_id):
        task=self.db.one("SELECT * FROM tasks WHERE id=?",(task_id,));
        if not task:raise HttpError(404,"Aufgabe nicht gefunden")
        user,project=self.task_scope_access(user,task); data=self.body(environ)
        ids=parse_id_list(data.get("user_ids",[]),"Die Auswahl der Verantwortlichen")
        manager=self.can_manage_project(user, project)
        team_lead=task["team_id"] and self.db.one("SELECT 1 ok FROM team_members WHERE team_id=? AND user_id=? AND is_lead=1",(task["team_id"],user["id"]))
        if not(manager or team_lead):raise HttpError(403,"Nur Team- oder Projektleitungen dürfen Aufgaben verteilen")
        with self.db.transaction() as connection:
            previous={row[0] for row in connection.execute("SELECT user_id FROM task_assignees WHERE task_id=?",(task_id,)).fetchall()}
            connection.execute("DELETE FROM task_assignees WHERE task_id=?",(task_id,))
            for uid in ids:
                if task["team_id"] and not connection.execute("SELECT 1 FROM team_members tm JOIN users u ON u.id=tm.user_id WHERE tm.team_id=? AND tm.user_id=? AND u.active=1",(task["team_id"],uid)).fetchone():raise HttpError(400,"Verantwortliche müssen dem Team angehören")
                if not task["team_id"] and not connection.execute("SELECT 1 FROM project_members pm JOIN users u ON u.id=pm.user_id WHERE pm.project_id=? AND pm.user_id=? AND u.active=1", (task["project_id"], uid)).fetchone():raise HttpError(400,"Verantwortliche müssen dem Projekt angehören")
                connection.execute("INSERT INTO task_assignees(task_id,user_id,assigned_at) VALUES(?,?,?)",(task_id,uid,utcnow()))
                if uid not in previous:connection.execute("INSERT INTO notifications(user_id,project_id,task_id,message,created_at) VALUES(?,?,?,?,?)",(uid,task["project_id"],task_id,f"Ihnen wurde die Aufgabe „{task['title']}“ zugewiesen." ,utcnow()))
            if set(ids)!=previous:Database.event(connection,task["project_id"],user["id"],"task.assigned",f"Zuständigkeit für „{task['title']}“ wurde aktualisiert.")
        return {"ok":True}

    def set_task_dependencies(self,environ,user,task_id):
        task=self.db.one("SELECT * FROM tasks WHERE id=?",(task_id,));
        if not task:raise HttpError(404,"Aufgabe nicht gefunden")
        user,project=self.task_scope_access(user,task);manager=self.can_manage_project(user,project);team_lead=bool(task["team_id"] and self.db.one("SELECT 1 ok FROM team_members WHERE team_id=? AND user_id=? AND is_lead=1",(task["team_id"],user["id"])))
        if not(manager or team_lead):raise HttpError(403,"Nur Team- oder Projektleitungen dürfen Abhängigkeiten bearbeiten.")
        ids=parse_id_list(self.body(environ).get("depends_on_ids",[]),"Die Auswahl der Vorgängeraufgaben")
        if task_id in ids:raise HttpError(400,"Eine Aufgabe kann nicht von sich selbst abhängen.")
        if ids:
            placeholders = ",".join("?" for _ in ids)
            count = self.db.one(
                f"SELECT COUNT(*) count FROM tasks WHERE project_id=? AND id IN ({placeholders}) AND is_shared_parent=0",
                (task["project_id"], *ids),
            )["count"]
            if count != len(ids):
                raise HttpError(400, "Mindestens eine Vorgängeraufgabe gehört nicht zu diesem Projekt.")
            for dependency_id in ids:
                dependency=self.db.one("SELECT * FROM tasks WHERE id=?",(dependency_id,))
                if not manager:
                    try:self.task_scope_access(user,dependency)
                    except HttpError as error:
                        if error.status in {403,404}:raise HttpError(403,"Eine ausgewählte Vorgängeraufgabe ist für Sie nicht zugänglich.")
                        raise
                cycle = self.db.one(
                    """WITH RECURSIVE predecessors(id) AS (
                           SELECT depends_on_id FROM task_dependencies WHERE task_id=?
                           UNION
                           SELECT relation.depends_on_id FROM task_dependencies relation
                           JOIN predecessors ON relation.task_id=predecessors.id
                       ) SELECT 1 ok FROM predecessors WHERE id=? LIMIT 1""",
                    (dependency_id, task_id),
                )
                if cycle:
                    raise HttpError(409, "Diese Abhängigkeit würde einen Kreis erzeugen.")
            if task["status_key"] == "done":
                placeholders = ",".join("?" for _ in ids)
                incomplete = self.db.one(
                    f"SELECT 1 ok FROM tasks WHERE id IN ({placeholders}) AND status_key!='done' LIMIT 1",
                    tuple(ids),
                )
                if incomplete:
                    raise HttpError(409, "Eine erledigte Aufgabe darf nicht von offenen Vorgängeraufgaben abhängen.")
        with self.db.transaction() as connection:
            previous={row[0] for row in connection.execute("SELECT depends_on_id FROM task_dependencies WHERE task_id=?",(task_id,)).fetchall()}
            connection.execute("DELETE FROM task_dependencies WHERE task_id=?",(task_id,))
            for dep in ids:connection.execute("INSERT INTO task_dependencies(task_id,depends_on_id) VALUES(?,?)",(task_id,dep))
            if set(ids)!=previous:Database.event(connection,task["project_id"],user["id"],"task.dependencies_updated",f"Abhängigkeiten für „{task['title']}“ wurden aktualisiert.")
        return {"ok":True}

    def create_deadline_request(self,environ,user,task_id):
        task=self.db.one("SELECT * FROM tasks WHERE id=?",(task_id,))
        if not task:raise HttpError(404,"Aufgabe nicht gefunden")
        user,project=self.task_scope_access(user,task);data=self.body(environ)
        if not self.db.one("SELECT 1 ok FROM task_assignees WHERE task_id=? AND user_id=?",(task_id,user["id"])):
            raise HttpError(403,"Nur verantwortliche Personen dürfen eine Fristverlängerung beantragen")
        reason=str(data.get("reason","")).strip();requested_due_at=normalize_due_at(data.get("requested_due_at"))
        if not reason or not requested_due_at:raise HttpError(400,"Neue Frist und Begründung sind erforderlich")
        if self.db.one("SELECT 1 ok FROM deadline_requests WHERE task_id=? AND requested_by=? AND status='pending'",(task_id,user["id"])):
            raise HttpError(409,"Für diese Aufgabe liegt bereits ein offener Antrag vor")
        request_id=self.db.execute("INSERT INTO deadline_requests(task_id,requested_by,requested_due_at,reason,created_at) VALUES(?,?,?,?,?)",(task_id,user["id"],requested_due_at,reason,utcnow()))
        self.notify_leads(task["project_id"],task_id,f"{user['first_name']} beantragt eine neue Frist für „{task['title']}“.")
        return {"id":request_id}

    def decide_deadline_request(self,environ,user,request_id):
        request=self.db.one("SELECT dr.*,t.project_id,t.title FROM deadline_requests dr JOIN tasks t ON t.id=dr.task_id WHERE dr.id=?",(request_id,))
        if not request:raise HttpError(404,"Fristantrag nicht gefunden")
        user,project=self.project_access(user,request["project_id"],manage=True);data=self.body(environ);decision=str(data.get("decision","")).lower()
        if request["status"]!="pending":raise HttpError(409,"Über diesen Antrag wurde bereits entschieden")
        if decision not in {"approved","rejected"}:raise HttpError(400,"Ungültige Entscheidung")
        with self.db.transaction() as connection:
            connection.execute("UPDATE deadline_requests SET status=?,decided_by=?,decided_at=?,decision_note=? WHERE id=?",(decision,user["id"],utcnow(),str(data.get("decision_note","")).strip(),request_id))
            if decision=="approved":connection.execute("UPDATE tasks SET due_at=?,updated_at=? WHERE id=?",(request["requested_due_at"],utcnow(),request["task_id"]))
            message=f"Der Fristantrag für „{request['title']}“ wurde {'genehmigt' if decision=='approved' else 'abgelehnt'}."
            connection.execute("INSERT INTO notifications(user_id,project_id,task_id,message,created_at) VALUES(?,?,?,?,?)",(request["requested_by"],request["project_id"],request["task_id"],message,utcnow()))
            Database.event(connection,request["project_id"],user["id"],"deadline.decided",message)
        return {"ok":True}

    def create_comment(self,environ,user,task_id):
        task=self.db.one("SELECT * FROM tasks WHERE id=?",(task_id,));
        if not task:raise HttpError(404,"Aufgabe nicht gefunden")
        user,_=self.task_scope_access(user,task);body=str(self.body(environ).get("body","")).strip()
        if not body:raise HttpError(400,"Kommentar ist leer")
        if len(body)>10_000:raise HttpError(400,"Der Kommentar darf höchstens 10.000 Zeichen enthalten")
        cid=self.db.execute("INSERT INTO comments(task_id,author_id,body,created_at) VALUES(?,?,?,?)",(task_id,user["id"],body,utcnow()));return {"id":cid}

    def update_comment(self,environ,user,comment_id):
        comment=self.db.one("SELECT c.*,t.project_id,t.team_id FROM comments c JOIN tasks t ON t.id=c.task_id WHERE c.id=?",(comment_id,));
        if not comment:raise HttpError(404,"Kommentar nicht gefunden")
        user,_=self.team_scope_access(user,comment["project_id"],comment["team_id"])
        if comment["author_id"]!=user["id"]:raise HttpError(403,"Nur eigene Kommentare können bearbeitet werden")
        body=str(self.body(environ).get("body","")).strip();
        if not body:raise HttpError(400,"Kommentar ist leer")
        if len(body)>10_000:raise HttpError(400,"Der Kommentar darf höchstens 10.000 Zeichen enthalten")
        with self.db.transaction() as connection:
            connection.execute("INSERT INTO comment_revisions(comment_id,body,saved_at) VALUES(?,?,?)",(comment_id,comment["body"],utcnow()));connection.execute("UPDATE comments SET body=?,edited_at=? WHERE id=?",(body,utcnow(),comment_id))
        return {"ok":True}

    def hide_comment(self,environ,user,comment_id):
        comment=self.db.one("SELECT c.*,t.project_id FROM comments c JOIN tasks t ON t.id=c.task_id WHERE c.id=?",(comment_id,));
        if not comment:raise HttpError(404,"Kommentar nicht gefunden")
        user,_=self.project_access(user,comment["project_id"],manage=True);self.db.execute("UPDATE comments SET hidden_at=?,hidden_by=? WHERE id=?",(utcnow(),user["id"],comment_id));return {"ok":True}

    def validated_upload(self, data: dict[str, Any]) -> tuple[str, str, bytes]:
        supplied_name = str(data.get("name", "")).replace("\\", "/")
        name = supplied_name.rsplit("/", 1)[-1].strip()
        if not name or name in {".", ".."} or len(name) > 180 or any(ord(character) < 32 for character in name):
            raise HttpError(400, "Der Dateiname ist ungültig oder zu lang.")
        media_type = str(data.get("media_type", "")).lower().strip()
        if media_type not in ALLOWED_MEDIA_TYPES:
            raise HttpError(400, "Erlaubt sind PDF, JPG, PNG und WebP")
        allowed_suffixes = {"application/pdf": {".pdf"}, "image/jpeg": {".jpg", ".jpeg"}, "image/png": {".png"}, "image/webp": {".webp"}}
        if Path(name).suffix.lower() not in allowed_suffixes[media_type]:
            raise HttpError(400, "Dateiendung und ausgewählter Dateityp passen nicht zusammen.")
        try:
            raw = base64.b64decode(data.get("file_base64", ""), validate=True)
        except Exception as exc:
            raise HttpError(400, "Datei ungültig") from exc
        if not raw:
            raise HttpError(400, "Die Datei ist leer.")
        if len(raw) > self.config.max_upload_bytes:
            raise HttpError(413, f"Datei ist größer als {self.config.max_upload_bytes // (1024 * 1024)} MB")
        signatures = {"application/pdf": b"%PDF-", "image/png": b"\x89PNG\r\n\x1a\n", "image/jpeg": b"\xff\xd8\xff", "image/webp": b"RIFF"}
        if not raw.startswith(signatures[media_type]) or (media_type == "image/webp" and raw[8:12] != b"WEBP"):
            raise HttpError(400, "Dateiinhalt passt nicht zum angegebenen Typ")
        if media_type.startswith("image/"):
            expected_format = {"image/jpeg": "JPEG", "image/png": "PNG", "image/webp": "WEBP"}[media_type]
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("error", PillowImage.DecompressionBombWarning)
                    with PillowImage.open(io.BytesIO(raw), formats=[expected_format]) as image:
                        if image.format != expected_format or image.width * image.height > 40_000_000:
                            raise ValueError("Bildformat oder Bildgröße unzulässig")
                        image.verify()
            except (UnidentifiedImageError, OSError, ValueError, PillowImage.DecompressionBombWarning) as exc:
                raise HttpError(400, "Das Bild ist beschädigt oder hat eine unzulässig hohe Auflösung.") from exc
        return name, media_type, raw

    def upload_file(self,environ,user,task_id):
        task=self.db.one("SELECT * FROM tasks WHERE id=?",(task_id,));
        if not task:raise HttpError(404,"Aufgabe nicht gefunden")
        user,_=self.task_scope_access(user,task);data=self.body(environ,max_bytes=self.config.max_upload_bytes*2)
        name,media_type,raw=self.validated_upload(data)
        stored=secrets.token_hex(20)+ALLOWED_MEDIA_TYPES[media_type];(self.config.upload_dir/stored).write_bytes(raw)
        upload_id=self.db.execute("INSERT INTO uploads(project_id,task_id,team_id,uploaded_by,original_name,stored_name,media_type,size_bytes,created_at) VALUES(?,?,?,?,?,?,?,?,?)",(task["project_id"],task_id,task["team_id"],user["id"],name,stored,media_type,len(raw),utcnow()));return {"id":upload_id}

    def upload_project_file(self,environ,user,project_id):
        user,project=self.project_access(user,project_id);data=self.body(environ,max_bytes=self.config.max_upload_bytes*2)
        team_id=int(data.get("team_id") or 0) or None
        manager=self.can_manage_project(user, project)
        if team_id:
            team=self.db.one("SELECT id FROM teams WHERE id=? AND project_id=? AND status='active'",(team_id,project_id))
            if not team:raise HttpError(400,"Ungültiges Projektteam")
            if not manager and not self.db.one("SELECT 1 ok FROM team_members WHERE team_id=? AND user_id=?",(team_id,user["id"])):
                raise HttpError(403,"Sie gehören diesem Team nicht an")
        name,media_type,raw=self.validated_upload(data)
        stored=secrets.token_hex(20)+ALLOWED_MEDIA_TYPES[media_type];(self.config.upload_dir/stored).write_bytes(raw)
        upload_id=self.db.execute("INSERT INTO uploads(project_id,task_id,team_id,uploaded_by,original_name,stored_name,media_type,size_bytes,created_at) VALUES(?,NULL,?,?,?,?,?,?,?)",(project_id,team_id,user["id"],name,stored,media_type,len(raw),utcnow()))
        return {"id":upload_id}

    def download_upload(self,environ,user,upload_id):
        upload=self.db.one("SELECT * FROM uploads WHERE id=?",(upload_id,));
        if not upload:raise HttpError(404,"Datei nicht gefunden")
        self.team_scope_access(user,upload["project_id"],upload["team_id"]);path=self.config.upload_dir/upload["stored_name"]
        return path.read_bytes(),upload["media_type"],upload["original_name"],[]

    def delete_upload(self,environ,user,upload_id):
        upload=self.db.one("SELECT * FROM uploads WHERE id=?",(upload_id,));
        if not upload:raise HttpError(404,"Datei nicht gefunden")
        self.project_access(user,upload["project_id"],manage=True);path=self.config.upload_dir/upload["stored_name"]
        if path.exists():path.unlink()
        self.db.execute("DELETE FROM uploads WHERE id=?",(upload_id,));return {"ok":True}

    def list_notifications(self,environ,user):
        user=self.require_user(user);return self.db.all("SELECT * FROM notifications WHERE user_id=? ORDER BY created_at DESC LIMIT 100",(user["id"],))

    def read_notifications(self,environ,user):
        user=self.require_user(user);self.db.execute("UPDATE notifications SET read_at=? WHERE user_id=? AND read_at IS NULL",(utcnow(),user["id"]));return {"ok":True}

    def create_report(self,environ,user,project_id):
        user,_=self.project_access(user,project_id,manage=True);stored=f"projekt-{project_id}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.pdf";path=self.config.report_dir/stored
        generate_project_report(self.db,project_id,path);rid=self.db.execute("INSERT INTO reports(project_id,stored_name,size_bytes,created_by,created_at) VALUES(?,?,?,?,?)",(project_id,stored,path.stat().st_size,user["id"],utcnow()));return {"id":rid}

    def download_report(self,environ,user,report_id):
        report=self.db.one("SELECT * FROM reports WHERE id=?",(report_id,));
        if not report:raise HttpError(404,"Bericht nicht gefunden")
        self.project_access(user,report["project_id"]);path=self.config.report_dir/report["stored_name"]
        return path.read_bytes(),"application/pdf","ProjektKontor-Projektbericht.pdf",[]

    def archive_project(self,environ,user,project_id):
        user,project=self.project_access(user,project_id,manage=True);report=self.db.one("SELECT * FROM reports WHERE project_id=? ORDER BY id DESC LIMIT 1",(project_id,))
        if not report:raise HttpError(409,"Erstellen Sie vor der Archivierung einen Projektbericht")
        data=self.body(environ)
        if not data.get("confirm_cleanup"):raise HttpError(400,"Bestätigung zur Datenbereinigung fehlt")
        uploads=self.db.all("SELECT stored_name FROM uploads WHERE project_id=?",(project_id,))
        with self.db.transaction() as connection:
            connection.execute("DELETE FROM notifications WHERE project_id=?",(project_id,));connection.execute("DELETE FROM uploads WHERE project_id=?",(project_id,));connection.execute("DELETE FROM tasks WHERE project_id=?",(project_id,));connection.execute("DELETE FROM phases WHERE project_id=?",(project_id,));connection.execute("DELETE FROM teams WHERE project_id=?",(project_id,));connection.execute("DELETE FROM project_events WHERE project_id=?",(project_id,));connection.execute("UPDATE projects SET status='archived',updated_at=? WHERE id=?",(utcnow(),project_id))
        for upload in uploads:
            path=self.config.upload_dir/upload["stored_name"]
            if path.exists():path.unlink()
        return {"ok":True,"report_id":report["id"]}

    def create_template(self,environ,user,project_id):
        user,project=self.project_access(user,project_id,manage=True);data=self.body(environ);name=str(data.get("name") or project["title"]).strip()
        snapshot={
            "title": project["title"], "description": project["description"], "source_start": project["start_at"],
            "has_start": project.get("has_start", 1), "has_end": project.get("has_end", 1),
            "start_has_time": project.get("start_has_time", 1), "end_has_time": project.get("end_has_time", 1),
            "teams": self.db.all("SELECT id,name,responsibility,color FROM teams WHERE project_id=? AND status='active' ORDER BY name", (project_id,)),
            "phases": self.db.all("SELECT id,name,description,expected_result,start_at,end_at,sort_order FROM phases WHERE project_id=? ORDER BY sort_order",(project_id,)),
            "tasks": self.db.all("SELECT title,description,weight,phase_id,team_id,start_at,due_at,requires_final_approval,is_milestone FROM tasks WHERE project_id=? AND parent_id IS NULL AND is_shared_parent=0",(project_id,))
        }
        try:tid=self.db.execute("INSERT INTO templates(name,source_project_id,snapshot_json,created_by,created_at) VALUES(?,?,?,?,?)",(name,project_id,json.dumps(snapshot,ensure_ascii=False),user["id"],utcnow()))
        except sqlite3.IntegrityError:raise HttpError(409,"Vorlagenname bereits vergeben")
        return {"id":tid}

    def list_templates(self,environ,user):
        user=self.require_teacher(user)
        if user.get("is_owner"):
            return self.db.all(
                """SELECT DISTINCT t.id,t.name,t.source_project_id,t.created_at
                   FROM templates t JOIN projects p ON p.id=t.source_project_id
                   JOIN teacher_classes tc ON tc.class_id=p.class_id
                   JOIN support_requests sr ON sr.teacher_id=tc.teacher_id
                   WHERE sr.granted_admin_id=? AND sr.status='active' AND sr.access_expires_at>?
                   ORDER BY t.name""",
                (user["id"], utcnow()),
            )
        return self.db.all(
            """SELECT t.id,t.name,t.source_project_id,t.created_at
               FROM templates t JOIN projects p ON p.id=t.source_project_id
               JOIN teacher_classes tc ON tc.class_id=p.class_id
               WHERE tc.teacher_id=? ORDER BY t.name""",
            (user["id"],),
        )


def create_application() -> App:
    return App(load_config())


def main() -> None:
    config = load_config()
    app = App(config)
    stop_automation = threading.Event()
    def automation_worker() -> None:
        while not stop_automation.is_set():
            try:
                app.run_license_automation()
            except Exception:
                traceback.print_exc()
            stop_automation.wait(60 * 60)
    automation_thread = threading.Thread(
        target=automation_worker, name="projektkontor-license-automation", daemon=True
    )
    automation_thread.start()
    print(f"ProjektKontor läuft unter http://{config.host}:{config.port}")
    try:
        with make_server(config.host, config.port, app) as server:
            server.serve_forever()
    except KeyboardInterrupt:
        print("\nProjektKontor wurde beendet.")
    finally:
        stop_automation.set()
        automation_thread.join(timeout=2)
