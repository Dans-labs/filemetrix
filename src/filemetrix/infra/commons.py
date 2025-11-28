import logging
import os

import tomli
from dynaconf import Dynaconf
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import time

# compute repository root (three levels up from infra/commons.py)
_repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# only set BASE_DIR if not already set in the environment
if not os.environ.get("BASE_DIR"):
    os.environ["BASE_DIR"] = _repo_root

_raw_app_settings = Dynaconf(root_path=f'{os.environ["BASE_DIR"]}/conf', settings_files=["*.toml"],
                    environments=True)

def get_project_details(base_dir: str, keys: list):
    with open(os.path.join(base_dir, 'pyproject.toml'), 'rb') as file:
        package_details = tomli.load(file)
    poetry = package_details['project']
    return {key: poetry[key] for key in keys}

class SettingsWrapper:
    """Wrap dynaconf object and provide attribute-style access with fallbacks.

    - getattr -> try attribute, then .get(), then env var uppercase
    - get(name, default) -> dynaconf.get then env var fallback
    """

    def __init__(self, wrapped):
        self._wrapped = wrapped

    def __getattr__(self, name: str):
        # Try attribute access first
        try:
            return getattr(self._wrapped, name)
        except Exception:
            pass
        # Try .get() fallback
        try:
            v = self._wrapped.get(name)
            if v is not None:
                return v
        except Exception:
            pass
        # Fallback to environment variable (uppercase)
        env_key = name.upper()
        if env_key in os.environ:
            return os.environ[env_key]
        # If setting is missing, return None (don't raise) so code importing module at import-time won't fail
        return None

    def get(self, name, default=None):
        try:
            v = self._wrapped.get(name)
            if v is not None:
                return v
        except Exception:
            pass
        return os.environ.get(name.upper(), default)


# Export a wrapped settings object used across the project
app_settings = SettingsWrapper(_raw_app_settings)


def _normalize_prefix(raw: str | None, default: str = "/api/v1") -> str:
    if not raw:
        return default
    p = raw.strip()
    if not p:
        return default
    # ensure single leading slash and no trailing slash (root stays "/")
    return "/" + p.strip("/")

# safe lookup from dynaconf settings, falling back to env var and default
API_PREFIX = getattr(app_settings, "API_PREFIX", None) or app_settings.get("API_PREFIX", None) or os.environ.get("API_PREFIX", None)
API_PREFIX = _normalize_prefix(API_PREFIX)


def _as_bool(val, default=False):
    if isinstance(val, bool):
        return val
    if val is None:
        return default
    s = str(val).strip().lower()
    if s in ("1", "true", "yes", "y", "on"):
        return True
    if s in ("0", "false", "no", "n", "off"):
        return False
    return default


def _normalize_mail_to(raw):
    # Accept list or comma-separated string
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        return [r for r in raw if r]
    if isinstance(raw, str):
        # support JSON-like list strings? best-effort: split on commas
        parts = [p.strip() for p in raw.split(",")]
        return [p for p in parts if p]
    return []


def send_mail(subject: str, body: str, to: list | None = None, from_addr: str | None = None) -> bool:
    mail_host = app_settings.get("mail_host") or os.environ.get("MAIL_HOST") or "smtp.gmail.com"
    try:
        mail_port = int(app_settings.get("mail_port", os.environ.get("MAIL_PORT", 587)))
    except Exception:
        mail_port = 587

    mail_use_tls = _as_bool(app_settings.get("mail_use_tls", os.environ.get("MAIL_USE_TLS", True)))
    mail_use_ssl = _as_bool(app_settings.get("mail_use_ssl", os.environ.get("MAIL_USE_SSL", False)))
    mail_use_auth = _as_bool(app_settings.get("mail_use_auth", os.environ.get("MAIL_USE_AUTH", False)))

    mail_usr = app_settings.get("mail_usr") or os.environ.get("MAIL_USR")
    mail_pass = app_settings.get("mail_pass") or os.environ.get("MAIL_PASS")

    mail_to_raw = to if to is not None else app_settings.get("mail_to") or os.environ.get("MAIL_TO")
    mail_to = _normalize_mail_to(mail_to_raw)

    if not mail_to:
        logging.error("No recipient specified for email. mail_to=%s", mail_to_raw)
        return False

    from_addr = from_addr or app_settings.get("mail_from") or os.environ.get("MAIL_FROM") or mail_usr or "no-reply@example.com"

    # retry configuration
    try:
        retries = int(os.environ.get("MAIL_SEND_RETRIES", app_settings.get("mail_send_retries") or 3))
    except Exception:
        retries = 3
    try:
        interval = int(os.environ.get("MAIL_SEND_INTERVAL", app_settings.get("mail_send_interval") or 2))
    except Exception:
        interval = 2

    msg = MIMEMultipart()
    msg["From"] = from_addr
    msg["To"] = ", ".join(mail_to)
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    attempt = 1
    while attempt <= retries:
        try:
            if mail_use_ssl:
                logging.debug("[mail attempt %d/%d] Connecting to SMTP (SSL) %s:%s", attempt, retries, mail_host, mail_port)
                with smtplib.SMTP_SSL(mail_host, mail_port, timeout=10) as server:
                    if mail_use_auth and mail_usr and mail_pass:
                        server.login(mail_usr, mail_pass)
                    server.sendmail(from_addr, mail_to, msg.as_string())
            else:
                logging.debug("[mail attempt %d/%d] Connecting to SMTP %s:%s (tls=%s)", attempt, retries, mail_host, mail_port, mail_use_tls)
                with smtplib.SMTP(mail_host, mail_port, timeout=10) as server:
                    server.ehlo()
                    if mail_use_tls:
                        # Only attempt STARTTLS if the server advertises it
                        if server.has_extn("starttls"):
                            server.starttls()
                            server.ehlo()
                        else:
                            logging.warning("STARTTLS extension not supported by server; continuing without TLS.")
                    # Only attempt login if auth is requested and server supports AUTH
                    if mail_use_auth and mail_usr and mail_pass:
                        if server.has_extn("auth"):
                            server.login(mail_usr, mail_pass)
                        else:
                            logging.warning("SMTP server does not advertise AUTH extension; skipping login.")
                    server.sendmail(from_addr, mail_to, msg.as_string())

            logging.info("Email sent successfully to %s", mail_to)
            return True
        except smtplib.SMTPAuthenticationError as e:
            logging.error("Authentication failed when sending email: %s", e)
            return False
        except Exception as e:
            logging.warning("Failed to send email on attempt %d/%d: %s", attempt, retries, e)
            if attempt < retries:
                logging.info("Retrying email send in %s seconds...", interval)
                try:
                    time.sleep(interval)
                except Exception:
                    pass
            attempt += 1

    logging.error("All attempts to send email failed (%d attempts)", retries)
    return False
