from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


TEAM_COLOR_PALETTE = [
    "#2563EB", "#16A34A", "#DC2626", "#F59E0B", "#7C3AED",
    "#06B6D4", "#EC4899", "#0F2E5D", "#14B8A6", "#64748B",
]


SCHEMA = """
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS classes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    class_id INTEGER REFERENCES classes(id) ON DELETE SET NULL,
    first_name TEXT NOT NULL,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    email TEXT NOT NULL DEFAULT '',
    credential TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('teacher','student')),
    is_owner INTEGER NOT NULL DEFAULT 0,
    must_change_password INTEGER NOT NULL DEFAULT 0,
    license_managed INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    last_login_at TEXT,
    initial_credentials_emailed_at TEXT,
    archived_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS teacher_classes (
    teacher_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    class_id INTEGER NOT NULL REFERENCES classes(id) ON DELETE CASCADE,
    assigned_at TEXT NOT NULL,
    PRIMARY KEY(teacher_id, class_id)
);

CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    class_id INTEGER NOT NULL REFERENCES classes(id) ON DELETE RESTRICT,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    start_at TEXT NOT NULL,
    end_at TEXT NOT NULL,
    has_start INTEGER NOT NULL DEFAULT 1,
    has_end INTEGER NOT NULL DEFAULT 1,
    start_has_time INTEGER NOT NULL DEFAULT 1,
    end_has_time INTEGER NOT NULL DEFAULT 1,
    project_lead_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('draft','active','completed','archived')),
    team_min_size INTEGER,
    team_max_size INTEGER,
    allow_team_proposals INTEGER NOT NULL DEFAULT 1,
    allow_student_organization INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS project_members (
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    joined_at TEXT NOT NULL,
    PRIMARY KEY(project_id, user_id)
);

CREATE TABLE IF NOT EXISTS teams (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    responsibility TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('proposed','active','rejected')),
    color TEXT NOT NULL DEFAULT '#6F8B74',
    created_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL,
    UNIQUE(project_id, name COLLATE NOCASE)
);

CREATE TABLE IF NOT EXISTS team_members (
    team_id INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    is_lead INTEGER NOT NULL DEFAULT 0,
    business_role TEXT NOT NULL DEFAULT '',
    joined_at TEXT NOT NULL,
    PRIMARY KEY(team_id, user_id)
);

CREATE TABLE IF NOT EXISTS phases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    expected_result TEXT NOT NULL DEFAULT '',
    start_at TEXT NOT NULL,
    end_at TEXT NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 0,
    locked INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_statuses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    key TEXT NOT NULL,
    label TEXT NOT NULL,
    color TEXT NOT NULL,
    sort_order INTEGER NOT NULL,
    system_kind TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    UNIQUE(project_id, key)
);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    parent_id INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
    team_id INTEGER REFERENCES teams(id) ON DELETE SET NULL,
    phase_id INTEGER REFERENCES phases(id) ON DELETE SET NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    result_text TEXT NOT NULL DEFAULT '',
    status_key TEXT NOT NULL DEFAULT 'open',
    weight INTEGER NOT NULL DEFAULT 2 CHECK(weight IN (1,2,3,5)),
    start_at TEXT,
    due_at TEXT,
    required INTEGER NOT NULL DEFAULT 1,
    requires_final_approval INTEGER NOT NULL DEFAULT 1,
    is_shared_parent INTEGER NOT NULL DEFAULT 0,
    blocked_reason TEXT NOT NULL DEFAULT '',
    created_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_assignees (
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    assigned_at TEXT NOT NULL,
    PRIMARY KEY(task_id, user_id)
);

CREATE TABLE IF NOT EXISTS task_dependencies (
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    depends_on_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    relation TEXT NOT NULL DEFAULT 'finish_to_start',
    PRIMARY KEY(task_id, depends_on_id)
);

CREATE TABLE IF NOT EXISTS deadline_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    requested_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    requested_due_at TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','rejected')),
    decided_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    decided_at TEXT,
    decision_note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    author_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    body TEXT NOT NULL,
    edited_at TEXT,
    hidden_at TEXT,
    hidden_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS comment_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    comment_id INTEGER NOT NULL REFERENCES comments(id) ON DELETE CASCADE,
    body TEXT NOT NULL,
    saved_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS uploads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    task_id INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
    team_id INTEGER REFERENCES teams(id) ON DELETE CASCADE,
    uploaded_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    original_name TEXT NOT NULL,
    stored_name TEXT NOT NULL,
    media_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
    task_id INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
    message TEXT NOT NULL,
    read_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS project_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    actor_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    event_type TEXT NOT NULL,
    message TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    source_project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
    snapshot_json TEXT NOT NULL,
    created_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    stored_name TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    created_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL UNIQUE,
    csrf_token TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS login_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL,
    remote_addr TEXT NOT NULL,
    attempted_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS contact_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    remote_addr TEXT NOT NULL,
    attempted_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS license_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    email TEXT NOT NULL,
    subject TEXT NOT NULL,
    message TEXT NOT NULL,
    request_type TEXT NOT NULL DEFAULT 'contact' CHECK(request_type IN ('beta','license','contact')),
    status TEXT NOT NULL DEFAULT 'new' CHECK(status IN ('new','in_progress','converted','closed')),
    email_status TEXT NOT NULL DEFAULT 'pending' CHECK(email_status IN ('pending','sent','failed')),
    email_error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS license_products (
    product_key TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    plan TEXT NOT NULL CHECK(plan IN ('beta','single','department','school')),
    billing_cycle TEXT NOT NULL CHECK(billing_cycle IN ('none','monthly','annual')),
    amount_cents INTEGER NOT NULL CHECK(amount_cents >= 0),
    seat_limit INTEGER NOT NULL CHECK(seat_limit BETWEEN 1 AND 500),
    duration_unit TEXT NOT NULL CHECK(duration_unit IN ('days','months','years')),
    duration_value INTEGER NOT NULL CHECK(duration_value BETWEEN 1 AND 1200),
    active INTEGER NOT NULL DEFAULT 1,
    sort_order INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS privacy_acceptances (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    privacy_version TEXT NOT NULL,
    accepted_at TEXT NOT NULL,
    PRIMARY KEY(user_id, privacy_version)
);

CREATE TABLE IF NOT EXISTS pending_logins (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS support_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    teacher_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    code_hash TEXT NOT NULL UNIQUE,
    phone TEXT NOT NULL,
    message TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','active','revoked','expired')),
    code_expires_at TEXT NOT NULL,
    granted_admin_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    granted_at TEXT,
    access_expires_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS license_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_name TEXT NOT NULL,
    organization TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL DEFAULT '',
    billing_address TEXT NOT NULL DEFAULT '',
    invoice_reference TEXT NOT NULL DEFAULT '',
    plan TEXT NOT NULL CHECK(plan IN ('beta','single','department','school')),
    billing_cycle TEXT NOT NULL DEFAULT 'annual' CHECK(billing_cycle IN ('none','monthly','annual')),
    amount_cents INTEGER NOT NULL DEFAULT 0 CHECK(amount_cents >= 0),
    payment_status TEXT NOT NULL DEFAULT 'open' CHECK(payment_status IN ('not_required','open','paid','overdue','refunded','cancelled')),
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS licenses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL UNIQUE REFERENCES license_orders(id) ON DELETE RESTRICT,
    seat_limit INTEGER NOT NULL CHECK(seat_limit BETWEEN 1 AND 500),
    starts_on TEXT NOT NULL,
    ends_on TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','active','suspended','expired','cancelled')),
    archived_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS license_teachers (
    license_id INTEGER NOT NULL REFERENCES licenses(id) ON DELETE CASCADE,
    teacher_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    assigned_at TEXT NOT NULL,
    PRIMARY KEY(license_id, teacher_id)
);

CREATE TABLE IF NOT EXISTS license_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    license_id INTEGER NOT NULL REFERENCES licenses(id) ON DELETE CASCADE,
    changed_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    event_type TEXT NOT NULL CHECK(event_type IN ('created','updated','converted')),
    from_plan TEXT,
    to_plan TEXT NOT NULL,
    from_billing_cycle TEXT,
    to_billing_cycle TEXT NOT NULL,
    from_amount_cents INTEGER,
    to_amount_cents INTEGER NOT NULL,
    from_status TEXT,
    to_status TEXT NOT NULL,
    changed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS invoice_settings (
    id INTEGER PRIMARY KEY CHECK(id=1),
    business_name TEXT NOT NULL,
    proprietor_name TEXT NOT NULL DEFAULT '',
    address TEXT NOT NULL,
    email TEXT NOT NULL DEFAULT '',
    tax_identifier TEXT NOT NULL,
    tax_mode TEXT NOT NULL DEFAULT 'small_business' CHECK(tax_mode IN ('small_business','standard')),
    vat_rate_basis_points INTEGER NOT NULL DEFAULT 1900 CHECK(vat_rate_basis_points BETWEEN 0 AND 10000),
    invoice_prefix TEXT NOT NULL DEFAULT 'PK',
    next_invoice_number INTEGER NOT NULL DEFAULT 1 CHECK(next_invoice_number >= 1),
    payment_terms_days INTEGER NOT NULL DEFAULT 0 CHECK(payment_terms_days IN (0,7,14,30)),
    iban TEXT NOT NULL DEFAULT '',
    bic TEXT NOT NULL DEFAULT '',
    bank_name TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS invoices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    license_id INTEGER NOT NULL REFERENCES licenses(id) ON DELETE RESTRICT,
    invoice_number TEXT NOT NULL UNIQUE,
    issued_on TEXT NOT NULL,
    service_on TEXT NOT NULL,
    due_on TEXT NOT NULL,
    customer_name TEXT NOT NULL,
    organization TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL DEFAULT '',
    billing_address TEXT NOT NULL,
    invoice_reference TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL,
    net_cents INTEGER NOT NULL,
    vat_rate_basis_points INTEGER NOT NULL DEFAULT 0,
    vat_cents INTEGER NOT NULL DEFAULT 0,
    gross_cents INTEGER NOT NULL,
    issuer_name TEXT NOT NULL,
    issuer_proprietor TEXT NOT NULL DEFAULT '',
    issuer_address TEXT NOT NULL,
    issuer_email TEXT NOT NULL DEFAULT '',
    issuer_tax_identifier TEXT NOT NULL,
    tax_note TEXT NOT NULL DEFAULT '',
    iban TEXT NOT NULL DEFAULT '',
    bic TEXT NOT NULL DEFAULT '',
    bank_name TEXT NOT NULL DEFAULT '',
    stored_name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','paid','cancelled')),
    emailed_at TEXT,
    downloaded_at TEXT,
    archived_at TEXT,
    created_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS invoice_number_registry (
    invoice_number TEXT PRIMARY KEY,
    invoice_id INTEGER UNIQUE,
    reserved_at TEXT NOT NULL,
    retired_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_id);
CREATE INDEX IF NOT EXISTS idx_tasks_team ON tasks(team_id);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(project_id, status_key);
CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications(user_id, read_at);
CREATE INDEX IF NOT EXISTS idx_sessions_token ON sessions(token_hash);
CREATE INDEX IF NOT EXISTS idx_login_attempts ON login_attempts(username,remote_addr,attempted_at);
CREATE INDEX IF NOT EXISTS idx_contact_attempts ON contact_attempts(remote_addr,attempted_at);
CREATE INDEX IF NOT EXISTS idx_license_requests_status ON license_requests(status,created_at);
CREATE INDEX IF NOT EXISTS idx_pending_logins_user ON pending_logins(user_id,expires_at);
CREATE INDEX IF NOT EXISTS idx_deadline_requests_task ON deadline_requests(task_id,status);
CREATE INDEX IF NOT EXISTS idx_teacher_classes_class ON teacher_classes(class_id,teacher_id);
CREATE INDEX IF NOT EXISTS idx_support_requests_teacher ON support_requests(teacher_id,status,access_expires_at);
CREATE INDEX IF NOT EXISTS idx_license_orders_status ON license_orders(payment_status,plan);
CREATE INDEX IF NOT EXISTS idx_licenses_status_dates ON licenses(status,starts_on,ends_on);
CREATE INDEX IF NOT EXISTS idx_license_teachers_teacher ON license_teachers(teacher_id,license_id);
CREATE INDEX IF NOT EXISTS idx_license_history_license ON license_history(license_id,changed_at);
CREATE INDEX IF NOT EXISTS idx_invoices_license ON invoices(license_id,issued_on,id);
CREATE INDEX IF NOT EXISTS idx_invoices_issued_on ON invoices(issued_on,invoice_number);
"""


class Database:
    def __init__(self, path: Path):
        self.path = path

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=15)
        try:
            self.path.chmod(0o600)
        except OSError:
            pass
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            columns = {row[1] for row in connection.execute("PRAGMA table_info(projects)").fetchall()}
            if "has_start" not in columns:
                connection.execute("ALTER TABLE projects ADD COLUMN has_start INTEGER NOT NULL DEFAULT 1")
            if "has_end" not in columns:
                connection.execute("ALTER TABLE projects ADD COLUMN has_end INTEGER NOT NULL DEFAULT 1")
            if "start_has_time" not in columns:
                connection.execute("ALTER TABLE projects ADD COLUMN start_has_time INTEGER NOT NULL DEFAULT 1")
            if "end_has_time" not in columns:
                connection.execute("ALTER TABLE projects ADD COLUMN end_has_time INTEGER NOT NULL DEFAULT 1")
            if "allow_student_organization" not in columns:
                connection.execute("ALTER TABLE projects ADD COLUMN allow_student_organization INTEGER NOT NULL DEFAULT 0")
            user_columns = {row[1] for row in connection.execute("PRAGMA table_info(users)").fetchall()}
            if "is_owner" not in user_columns:
                connection.execute("ALTER TABLE users ADD COLUMN is_owner INTEGER NOT NULL DEFAULT 0")
            if "must_change_password" not in user_columns:
                connection.execute("ALTER TABLE users ADD COLUMN must_change_password INTEGER NOT NULL DEFAULT 0")
            if "license_managed" not in user_columns:
                connection.execute("ALTER TABLE users ADD COLUMN license_managed INTEGER NOT NULL DEFAULT 0")
            if "email" not in user_columns:
                connection.execute("ALTER TABLE users ADD COLUMN email TEXT NOT NULL DEFAULT ''")
            if "initial_credentials_emailed_at" not in user_columns:
                connection.execute("ALTER TABLE users ADD COLUMN initial_credentials_emailed_at TEXT")
            if "archived_at" not in user_columns:
                connection.execute("ALTER TABLE users ADD COLUMN archived_at TEXT")
            order_columns = {row[1] for row in connection.execute("PRAGMA table_info(license_orders)").fetchall()}
            if "billing_cycle" not in order_columns:
                connection.execute(
                    "ALTER TABLE license_orders ADD COLUMN billing_cycle TEXT NOT NULL DEFAULT 'annual' "
                    "CHECK(billing_cycle IN ('none','monthly','annual'))"
                )
            connection.execute("UPDATE license_orders SET billing_cycle='none' WHERE plan='beta'")
            license_columns = {row[1] for row in connection.execute("PRAGMA table_info(licenses)").fetchall()}
            if "archived_at" not in license_columns:
                connection.execute("ALTER TABLE licenses ADD COLUMN archived_at TEXT")
            invoice_columns = {row[1] for row in connection.execute("PRAGMA table_info(invoices)").fetchall()}
            invoice_indexes = connection.execute("PRAGMA index_list(invoices)").fetchall()
            has_unique_license = False
            for index_row in invoice_indexes:
                if not index_row[2]:
                    continue
                indexed_columns = [
                    row[2] for row in connection.execute(f"PRAGMA index_info('{index_row[1]}')").fetchall()
                ]
                if indexed_columns == ["license_id"]:
                    has_unique_license = True
                    break
            if has_unique_license:
                connection.execute("ALTER TABLE invoices RENAME TO invoices_legacy")
                connection.execute(
                    """CREATE TABLE invoices (
                           id INTEGER PRIMARY KEY AUTOINCREMENT,
                           license_id INTEGER NOT NULL REFERENCES licenses(id) ON DELETE RESTRICT,
                           invoice_number TEXT NOT NULL UNIQUE,
                           issued_on TEXT NOT NULL, service_on TEXT NOT NULL, due_on TEXT NOT NULL,
                           customer_name TEXT NOT NULL, organization TEXT NOT NULL DEFAULT '',
                           email TEXT NOT NULL DEFAULT '', billing_address TEXT NOT NULL,
                           invoice_reference TEXT NOT NULL DEFAULT '', description TEXT NOT NULL,
                           net_cents INTEGER NOT NULL, vat_rate_basis_points INTEGER NOT NULL DEFAULT 0,
                           vat_cents INTEGER NOT NULL DEFAULT 0, gross_cents INTEGER NOT NULL,
                           issuer_name TEXT NOT NULL, issuer_proprietor TEXT NOT NULL DEFAULT '',
                           issuer_address TEXT NOT NULL, issuer_email TEXT NOT NULL DEFAULT '',
                           issuer_tax_identifier TEXT NOT NULL, tax_note TEXT NOT NULL DEFAULT '',
                           iban TEXT NOT NULL DEFAULT '', bic TEXT NOT NULL DEFAULT '',
                           bank_name TEXT NOT NULL DEFAULT '', stored_name TEXT NOT NULL,
                           status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','paid','cancelled')),
                           emailed_at TEXT, downloaded_at TEXT, archived_at TEXT,
                           created_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
                           created_at TEXT NOT NULL
                       )"""
                )
                connection.execute(
                    """INSERT INTO invoices(
                           id,license_id,invoice_number,issued_on,service_on,due_on,customer_name,
                           organization,email,billing_address,invoice_reference,description,net_cents,
                           vat_rate_basis_points,vat_cents,gross_cents,issuer_name,issuer_proprietor,
                           issuer_address,issuer_email,issuer_tax_identifier,tax_note,iban,bic,bank_name,
                           stored_name,status,emailed_at,downloaded_at,archived_at,created_by,created_at)
                       SELECT id,license_id,invoice_number,issued_on,service_on,due_on,customer_name,
                           organization,email,billing_address,invoice_reference,description,net_cents,
                           vat_rate_basis_points,vat_cents,gross_cents,issuer_name,issuer_proprietor,
                           issuer_address,issuer_email,issuer_tax_identifier,tax_note,iban,bic,bank_name,
                           stored_name,'open',NULL,NULL,NULL,created_by,created_at
                       FROM invoices_legacy"""
                )
                connection.execute("DROP TABLE invoices_legacy")
                connection.execute("CREATE INDEX IF NOT EXISTS idx_invoices_license ON invoices(license_id,issued_on,id)")
                connection.execute("CREATE INDEX IF NOT EXISTS idx_invoices_issued_on ON invoices(issued_on,invoice_number)")
            else:
                if "status" not in invoice_columns:
                    connection.execute("ALTER TABLE invoices ADD COLUMN status TEXT NOT NULL DEFAULT 'open'")
                if "emailed_at" not in invoice_columns:
                    connection.execute("ALTER TABLE invoices ADD COLUMN emailed_at TEXT")
                if "downloaded_at" not in invoice_columns:
                    connection.execute("ALTER TABLE invoices ADD COLUMN downloaded_at TEXT")
                if "archived_at" not in invoice_columns:
                    connection.execute("ALTER TABLE invoices ADD COLUMN archived_at TEXT")
            connection.execute(
                """CREATE TABLE IF NOT EXISTS invoice_number_registry (
                       invoice_number TEXT PRIMARY KEY,
                       invoice_id INTEGER UNIQUE,
                       reserved_at TEXT NOT NULL,
                       retired_at TEXT
                   )"""
            )
            connection.execute(
                """INSERT OR IGNORE INTO invoice_number_registry(
                       invoice_number,invoice_id,reserved_at,retired_at
                   ) SELECT invoice_number,id,created_at,
                            CASE WHEN archived_at IS NOT NULL THEN archived_at ELSE NULL END
                     FROM invoices"""
            )
            connection.execute(
                """INSERT OR IGNORE INTO invoice_settings(
                       id,business_name,proprietor_name,address,email,tax_identifier,tax_mode,
                       vat_rate_basis_points,invoice_prefix,next_invoice_number,payment_terms_days,
                       iban,bic,bank_name,updated_at
                   ) VALUES(1,?,?,?,?,?,'small_business',1900,'PK',1,0,'','','',?)""",
                (
                    "PRIMEAdvisory",
                    "Jeroen L. Jochem",
                    "Nieberdingstr. 41\n45147 Essen",
                    "info@prime-advisory.de",
                    "DE453188253",
                    utcnow(),
                ),
            )
            # Frühere Versionen behielten trotz Kleinunternehmerregelung den
            # Regelsatz als Nebenwert. § 19 UStG wird nun eindeutig abgebildet.
            connection.execute(
                "UPDATE invoice_settings SET vat_rate_basis_points=0 WHERE tax_mode='small_business'"
            )
            product_defaults = (
                ("beta", "Beta-Test", "beta", "none", 0, 1, "days", 28, 10),
                ("single_monthly", "Einzellizenz monatlich", "single", "monthly", 890, 1, "months", 1, 20),
                ("single_annual", "Einzellizenz jährlich", "single", "annual", 7900, 1, "years", 1, 30),
                ("department", "Fachbereichslizenz", "department", "annual", 29900, 5, "years", 1, 40),
                ("school", "Schullizenz", "school", "annual", 59900, 15, "years", 1, 50),
            )
            for product in product_defaults:
                connection.execute(
                    """INSERT OR IGNORE INTO license_products(
                           product_key,name,plan,billing_cycle,amount_cents,seat_limit,
                           duration_unit,duration_value,active,sort_order,updated_at
                       ) VALUES(?,?,?,?,?,?,?,?,1,?,?)""",
                    (*product, utcnow()),
                )
            # Ab Version 3 benötigen sämtliche Lehrkraftkonten eine aktive
            # Lizenz; alte Bestandskonten werden nicht mehr stillschweigend
            # freigeschaltet.
            if not connection.execute("SELECT 1 FROM schema_migrations WHERE version=3").fetchone():
                connection.execute("UPDATE users SET license_managed=1 WHERE role='teacher' AND is_owner=0")
                connection.execute(
                    "INSERT INTO schema_migrations(version,applied_at) VALUES(3,?)",
                    (utcnow(),),
                )
            # Seit Version 2 sind Plattform-Admin und Lehrkraft getrennte
            # Konten. Frühere globale Lehrkraftkonten bleiben Lehrkraftkonten;
            # anschließend kann einmalig ein eigener Admin eingerichtet werden.
            if not connection.execute("SELECT 1 FROM schema_migrations WHERE version=2").fetchone():
                connection.execute("UPDATE users SET is_owner=0 WHERE role='teacher'")
                connection.execute(
                    "INSERT INTO schema_migrations(version,applied_at) VALUES(2,?)",
                    (utcnow(),),
                )
            project_rows = connection.execute("SELECT DISTINCT project_id FROM teams WHERE status!='rejected'").fetchall()
            for project_row in project_rows:
                used: set[str] = set()
                team_rows = connection.execute(
                    "SELECT id,color FROM teams WHERE project_id=? AND status!='rejected' ORDER BY id",
                    (project_row[0],),
                ).fetchall()
                for team_row in team_rows:
                    current = str(team_row[1] or "").upper()
                    if current in TEAM_COLOR_PALETTE and current not in used:
                        used.add(current)
                        continue
                    replacement = next((color for color in TEAM_COLOR_PALETTE if color not in used), None)
                    if replacement:
                        connection.execute("UPDATE teams SET color=? WHERE id=?", (replacement, team_row[0]))
                        used.add(replacement)
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_teams_unique_project_color ON teams(project_id,color) WHERE status!='rejected'"
            )
            connection.commit()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def one(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(sql, params).fetchone()
            return dict(row) if row else None

    def all(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self.connect() as connection:
            return [dict(row) for row in connection.execute(sql, params).fetchall()]

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        with self.connect() as connection:
            cursor = connection.execute(sql, params)
            connection.commit()
            return int(cursor.lastrowid)

    @staticmethod
    def event(
        connection: sqlite3.Connection,
        project_id: int,
        actor_id: int | None,
        event_type: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        connection.execute(
            "INSERT INTO project_events(project_id,actor_id,event_type,message,details_json,created_at) VALUES(?,?,?,?,?,?)",
            (project_id, actor_id, event_type, message, json.dumps(details or {}, ensure_ascii=False), utcnow()),
        )


DEFAULT_STATUSES = [
    ("open", "Offen", "#E6E9ED", 10, "open"),
    ("in_progress", "In Arbeit", "#3B82F6", 20, "active"),
    ("blocked", "Blockiert", "#EF4444", 30, "blocked"),
    ("team_review", "Zur Teamprüfung", "#F59E0B", 40, "team_review"),
    ("approval", "Zur Freigabe", "#8B5CF6", 50, "approval"),
    ("revision", "Zur Überarbeitung", "#F97316", 60, "revision"),
    ("done", "Freigegeben", "#6F8B74", 70, "done"),
]


def seed_project_statuses(connection: sqlite3.Connection, project_id: int) -> None:
    connection.executemany(
        "INSERT INTO task_statuses(project_id,key,label,color,sort_order,system_kind) VALUES(?,?,?,?,?,?)",
        [(project_id, *row) for row in DEFAULT_STATUSES],
    )
