"""Configuración del correo saliente (ASP-42), compartida por realm-smtp.py y
smtp-test.py. No se ejecuta sola.

Lee smtp.json (o el fichero que se pase con --config), que es la única fuente de
verdad del SMTP y no sabe de proveedores:

  smtpServer        lo que se guarda en el realm, con los nombres de campo de
                    Keycloak: host, port, from (obligatorios), starttls, ssl,
                    auth, user, fromDisplayName, replyTo, replyToDisplayName,
                    envelopeFrom. Números y booleanos se pasan a texto, que es
                    como los guarda Keycloak.
  passwordKeychain  {account, service}: la entrada del Llavero de macOS con la
                    contraseña. Obligatorio si auth es true.

La contraseña se lee con `security find-generic-password -a <account> -s
<service> -w`, o de la variable de entorno que se llama como <service> si está
definida. Vive solo en memoria: no se imprime, ni se escribe en disco ni en los
mensajes de error.
"""
import json
import os
import subprocess
from pathlib import Path

from kcadmin import die

CONFIG = Path(__file__).with_name("smtp.json")
REQUIRED = ("host", "port", "from")
OPTIONAL = ("starttls", "ssl", "auth", "user", "fromDisplayName", "replyTo", "replyToDisplayName", "envelopeFrom")
MASK = "**********"  # lo que devuelve Keycloak en lugar de la contraseña


def as_keycloak(value):
    """Keycloak guarda el smtpServer como un mapa de cadenas."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, str)):
        return str(value)
    die(f"valor no admitido en smtpServer: {value!r} (texto, número o booleano)")


def load_config(path=CONFIG):
    """Devuelve (smtpServer como mapa de cadenas, passwordKeychain o None si auth no es true)."""
    try:
        cfg = json.loads(Path(path).read_text())
    except (OSError, ValueError) as e:
        die(f"no puedo leer la configuración {path}: {e}")
    smtp = cfg.get("smtpServer")
    if not isinstance(smtp, dict):
        die(f"{path}: falta el objeto smtpServer")
    if "password" in smtp:
        die(f"{path}: la contraseña no va en el fichero, sino en el Llavero (passwordKeychain)")
    unknown = sorted(set(smtp) - set(REQUIRED) - set(OPTIONAL))
    if unknown:
        die(f"{path}: campos de smtpServer que no conozco: {', '.join(unknown)}")
    missing = [k for k in REQUIRED if smtp.get(k) in (None, "")]
    if missing:
        die(f"{path}: faltan en smtpServer: {', '.join(missing)}")
    want = {k: as_keycloak(v) for k, v in smtp.items() if v is not None}
    keychain = None
    if want.get("auth") == "true":
        keychain = cfg.get("passwordKeychain") or {}
        if not (keychain.get("account") and keychain.get("service")):
            die(f"{path}: con auth=true hace falta passwordKeychain con account y service")
        if not want.get("user"):
            die(f"{path}: con auth=true hace falta smtpServer.user")
    return want, keychain


def keychain_label(keychain):
    return f"Llavero {keychain['account']} / {keychain['service']}"


def password_available(keychain):
    """Sin leer la contraseña: ¿hay de dónde sacarla? (para los dry-run)."""
    if os.environ.get(keychain["service"]):
        return True
    try:
        # sin -w, `security` no saca la contraseña; solo dice si la entrada existe
        subprocess.run(["security", "find-generic-password", "-a", keychain["account"], "-s", keychain["service"]],
                       capture_output=True, check=True)
        return True
    except (OSError, subprocess.CalledProcessError):
        return False


def read_password(keychain):
    pwd = os.environ.get(keychain["service"])
    if pwd:
        return pwd
    try:
        pwd = subprocess.run(
            ["security", "find-generic-password", "-a", keychain["account"], "-s", keychain["service"], "-w"],
            capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        pwd = ""
    if not pwd:
        die(f"no encuentro la contraseña SMTP: ni la variable {keychain['service']} ni el {keychain_label(keychain)}. "
            f"Guárdala con: security add-generic-password -a {keychain['account']} -s {keychain['service']} -w")
    return pwd


def scrub(text, password):
    """Por si un error devolviera lo enviado: la contraseña nunca sale."""
    return text.replace(password, MASK) if password else text
