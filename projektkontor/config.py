from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent

DEFAULT_PRIVACY_CONTROLLER_NAME = "PRIMEAdvisory · Inhaber Jeroen L. Jochem"
DEFAULT_PRIVACY_CONTROLLER_ADDRESS = "Nieberdingstr. 41, 45147 Essen, Deutschland"
DEFAULT_PRIVACY_CONTROLLER_EMAIL = "info@prime-advisory.de"
DEFAULT_PRIVACY_DPO_CONTACT = "Datenschutzanfragen über info@prime-advisory.de"
DEFAULT_PRIVACY_LEGAL_BASIS = (
    "Für den Unterrichtseinsatz gilt die von der verantwortlichen Schule beziehungsweise dem "
    "zuständigen Schulträger festgelegte schulrechtliche Rechtsgrundlage. Für den technischen "
    "Betrieb verarbeitet PRIMEAdvisory personenbezogene Daten nach Maßgabe der vertraglichen "
    "Vereinbarungen und dokumentierten Weisungen der verantwortlichen Stelle."
)


def load_local_env() -> None:
    """Load a private development .env without overriding real environment variables."""
    path = ROOT / ".env"
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key.startswith("PK_"):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


@dataclass(frozen=True)
class Config:
    host: str
    port: int
    data_dir: Path
    max_upload_bytes: int
    cookie_secure: bool = False
    trust_proxy: bool = False
    privacy_controller_name: str = DEFAULT_PRIVACY_CONTROLLER_NAME
    privacy_controller_address: str = DEFAULT_PRIVACY_CONTROLLER_ADDRESS
    privacy_controller_email: str = DEFAULT_PRIVACY_CONTROLLER_EMAIL
    privacy_dpo_contact: str = DEFAULT_PRIVACY_DPO_CONTACT
    privacy_legal_basis: str = DEFAULT_PRIVACY_LEGAL_BASIS
    contact_recipient: str = ""
    smtp_host: str = "mail.gmx.net"
    smtp_port: int = 465
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_sender: str = ""
    smtp_use_ssl: bool = True
    turnstile_sitekey: str = ""
    turnstile_secret: str = ""
    admin_setup_token: str = ""

    @property
    def db_path(self) -> Path:
        return self.data_dir / "projektkontor.sqlite3"

    @property
    def upload_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def report_dir(self) -> Path:
        return self.data_dir / "reports"

    @property
    def secret_path(self) -> Path:
        return self.data_dir / ".secret-key"


def load_config() -> Config:
    load_local_env()
    data_dir = Path(os.getenv("PK_DATA_DIR", ROOT / "data")).expanduser().resolve()
    cookie_secure = os.getenv("PK_COOKIE_SECURE", "0").lower() in {"1", "true", "yes"}
    test_sitekey = "" if cookie_secure else "1x00000000000000000000AA"
    test_secret = "" if cookie_secure else "1x0000000000000000000000000000000AA"
    config = Config(
        host=os.getenv("PK_HOST", "127.0.0.1"),
        port=int(os.getenv("PK_PORT", "8080")),
        data_dir=data_dir,
        max_upload_bytes=int(os.getenv("PK_MAX_UPLOAD_MB", "25")) * 1024 * 1024,
        cookie_secure=cookie_secure,
        trust_proxy=os.getenv("PK_TRUST_PROXY", "0").lower() in {"1", "true", "yes"},
        privacy_controller_name=os.getenv("PK_PRIVACY_CONTROLLER_NAME", DEFAULT_PRIVACY_CONTROLLER_NAME).strip(),
        privacy_controller_address=os.getenv("PK_PRIVACY_CONTROLLER_ADDRESS", DEFAULT_PRIVACY_CONTROLLER_ADDRESS).strip(),
        privacy_controller_email=os.getenv("PK_PRIVACY_CONTROLLER_EMAIL", DEFAULT_PRIVACY_CONTROLLER_EMAIL).strip(),
        privacy_dpo_contact=os.getenv("PK_PRIVACY_DPO_CONTACT", DEFAULT_PRIVACY_DPO_CONTACT).strip(),
        privacy_legal_basis=os.getenv("PK_PRIVACY_LEGAL_BASIS", DEFAULT_PRIVACY_LEGAL_BASIS).strip(),
        contact_recipient=os.getenv("PK_CONTACT_RECIPIENT", "").strip(),
        smtp_host=os.getenv("PK_SMTP_HOST", "mail.gmx.net").strip(),
        smtp_port=int(os.getenv("PK_SMTP_PORT", "465")),
        smtp_username=os.getenv("PK_SMTP_USERNAME", "").strip(),
        smtp_password=os.getenv("PK_SMTP_PASSWORD", ""),
        smtp_sender=os.getenv("PK_SMTP_SENDER", "").strip(),
        smtp_use_ssl=os.getenv("PK_SMTP_USE_SSL", "1").lower() in {"1", "true", "yes"},
        turnstile_sitekey=os.getenv("PK_TURNSTILE_SITEKEY", test_sitekey).strip(),
        turnstile_secret=os.getenv("PK_TURNSTILE_SECRET", test_secret).strip(),
        admin_setup_token=os.getenv("PK_ADMIN_SETUP_TOKEN", "").strip(),
    )
    config.data_dir.mkdir(parents=True, exist_ok=True)
    config.upload_dir.mkdir(parents=True, exist_ok=True)
    config.report_dir.mkdir(parents=True, exist_ok=True)
    for directory in (config.data_dir, config.upload_dir, config.report_dir):
        try:
            directory.chmod(0o700)
        except OSError:
            pass
    return config
