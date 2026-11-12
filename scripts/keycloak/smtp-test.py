#!/usr/bin/env python3
"""Envía un correo de prueba con la configuración de smtp.json (ASP-42).

Sirve para comprobar la configuración, la clave y la autenticación del dominio
(SPF/DKIM/DMARC) sin pasar por Keycloak. Habla directamente con el servidor
SMTP del fichero, con smtplib: no necesita el port-forward ni toca ningún realm.
El testSMTPConnection de Keycloak no sirve para esto, porque envía al email del
admin conectado y el admin de master no tiene.

Usa los mismos campos que realm-smtp.py guarda en los realms: host, port, ssl
(TLS desde el principio) o starttls, auth, user, from, fromDisplayName, replyTo
y envelopeFrom. La contraseña sale del Llavero (ver smtpconfig.py), vive solo en
memoria y no se imprime nunca, tampoco en los errores. El certificado del
servidor se verifica.

El destinatario se pasa solo por argumento. El correo lleva el asunto
"ASPAVI - prueba de envío" y un cuerpo corto con la fecha, sin datos de nadie.
Imprime la respuesta del servidor: aceptado (con su código y texto, donde
suele ir el id de la cola) o el error.

Uso:
  python3 scripts/keycloak/smtp-test.py --to someone@example.com [--config FICHERO]

Termina con 1 si el servidor no lo acepta.
"""
import argparse
import smtplib
import ssl
import sys
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import format_datetime, formataddr, make_msgid

from smtpconfig import CONFIG, load_config, read_password, scrub

SUBJECT = "ASPAVI - prueba de envío"
TIMEOUT = 30


def message(smtp, to):
    now = datetime.now(timezone.utc)
    msg = EmailMessage()
    msg["From"] = formataddr((smtp.get("fromDisplayName", ""), smtp["from"]))
    msg["To"] = to
    msg["Subject"] = SUBJECT
    msg["Date"] = format_datetime(now)
    msg["Message-ID"] = make_msgid(domain=smtp["from"].rpartition("@")[2] or None)
    if smtp.get("replyTo"):
        msg["Reply-To"] = formataddr((smtp.get("replyToDisplayName", ""), smtp["replyTo"]))
    msg.set_content(
        "Correo de prueba de la configuración SMTP de ASPAVI "
        "(scripts/keycloak/smtp-test.py en aspavi-infra).\n\n"
        f"Enviado el {now:%Y-%m-%d %H:%M:%S} UTC.\n\n"
        "Si lo has recibido, el servidor, la clave y el remitente funcionan.\n")
    return msg


def connect(smtp):
    host, port = smtp["host"], int(smtp["port"])
    ctx = ssl.create_default_context()
    if smtp.get("ssl") == "true":
        return smtplib.SMTP_SSL(host, port, timeout=TIMEOUT, context=ctx)
    conn = smtplib.SMTP(host, port, timeout=TIMEOUT)
    conn.ehlo()
    if smtp.get("starttls") == "true":
        conn.starttls(context=ctx)
        conn.ehlo()
    return conn


def send(smtp, password, to):
    """Devuelve (código, texto) de la respuesta del servidor al DATA."""
    msg = message(smtp, to)
    with connect(smtp) as conn:
        if smtp.get("auth") == "true":
            conn.login(smtp["user"], password)
        conn.ehlo_or_helo_if_needed()
        code, resp = conn.mail(smtp.get("envelopeFrom") or smtp["from"])
        if code != 250:
            raise smtplib.SMTPSenderRefused(code, resp, smtp.get("envelopeFrom") or smtp["from"])
        code, resp = conn.rcpt(to)
        if code not in (250, 251):
            raise smtplib.SMTPRecipientsRefused({to: (code, resp)})
        code, resp = conn.data(msg.as_bytes())
        if code != 250:
            raise smtplib.SMTPDataError(code, resp)
        return code, resp.decode(errors="replace")


def main():
    ap = argparse.ArgumentParser(description="Correo de prueba con la configuración de smtp.json (ver la cabecera).")
    ap.add_argument("--to", required=True, metavar="EMAIL", help="destinatario, p. ej. someone@example.com")
    ap.add_argument("--config", default=CONFIG, help=f"fichero de configuración (por defecto {CONFIG.name})")
    opts = ap.parse_args()
    to = opts.to.strip()
    if "@" not in to or any(c in to for c in "\r\n,; <>"):
        print("ERROR: --to tiene que ser una sola dirección de correo", file=sys.stderr)
        raise SystemExit(1)
    smtp, keychain = load_config(opts.config)
    password = read_password(keychain) if keychain else None
    mode = "ssl" if smtp.get("ssl") == "true" else "starttls" if smtp.get("starttls") == "true" else "sin TLS"
    print(f"== {smtp['host']}:{smtp['port']} ({mode}), "
          f"usuario {smtp.get('user') if keychain else '(sin auth)'}, remitente {smtp['from']}")
    try:
        code, resp = send(smtp, password, to)
    except (smtplib.SMTPException, OSError) as e:
        print(f"  {'ERROR':<12} {type(e).__name__}: {scrub(str(e), password)}")
        raise SystemExit(1)
    print(f"  {'aceptado':<12} {code} {scrub(resp, password)}")
    print("  (aceptado = el servidor lo ha encolado; que llegue a la bandeja y pase SPF/DKIM/DMARC se mira en el correo)")


if __name__ == "__main__":
    main()
