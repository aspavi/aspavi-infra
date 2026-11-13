#!/usr/bin/env python3
"""Secret de las copias por tenant (ASP-43), desde el Llavero de macOS.

Crea (o sustituye) el Secret rrhh-data/postgresql-tenant-backup que lee el
CronJob postgresql-tenant-backup, con tres claves que salen del Llavero, cuenta
`aspavi` (o de una variable de entorno con el mismo nombre):

  clave del Secret   entrada del Llavero      qué es
  s3-access-key      BACKUP_S3_ACCESS_KEY     access key de Hetzner Object Storage
  s3-secret-key      BACKUP_S3_SECRET_KEY     su secret key
  age-recipient      BACKUP_AGE_RECIPIENT     la clave PÚBLICA de age (age1...)

La clave PRIVADA de age (BACKUP_AGE_IDENTITY) no la lee este script ni va al
clúster: solo la usa restore-test.py, en local. Ver apps/postgresql/README.md,
"Copias por tenant", para crear las claves y guardarlas.

Por defecto NO escribe: dice qué encuentra (las claves de S3 enmascaradas; la
pública de age entera, porque es pública) y si el Secret ya existe. Con --apply
lo escribe con create o replace por la entrada estándar: los valores no van en
la línea de comandos ni en la anotación last-applied-configuration. No imprime
nunca las claves de S3.

Uso:
  python3 scripts/backup/create-secret.py            # dry-run
  python3 scripts/backup/create-secret.py --apply
"""
import argparse
import base64
import json
import os
import re
import subprocess
import sys

ACCOUNT = "aspavi"
NAMESPACE, NAME = "rrhh-data", "postgresql-tenant-backup"
KEYS = {  # clave del Secret -> entrada del Llavero
    "s3-access-key": "BACKUP_S3_ACCESS_KEY",
    "s3-secret-key": "BACKUP_S3_SECRET_KEY",
    "age-recipient": "BACKUP_AGE_RECIPIENT",
}
RECIPIENT_RE = re.compile(r"age1[02-9ac-hj-np-z]{58}")


def die(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def keychain(service):
    value = os.environ.get(service)
    if value:
        return value.strip(), f"variable {service}"
    try:
        value = subprocess.run(["security", "find-generic-password", "-a", ACCOUNT, "-s", service, "-w"],
                               capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        value = ""
    return value, f"Llavero {ACCOUNT} / {service}"


def kubectl(kubeconfig, *args, stdin=None):
    return subprocess.run(["kubectl", "--kubeconfig", kubeconfig, *args], input=stdin, capture_output=True, text=True)


def main():
    ap = argparse.ArgumentParser(description="Secret de las copias por tenant desde el Llavero (ver la cabecera).")
    ap.add_argument("--apply", action="store_true", help="escribe el Secret (sin esto, solo dice qué haría)")
    ap.add_argument("--kubeconfig", default=os.path.expanduser("~/.kube/aspavi.kubeconfig"))
    opts = ap.parse_args()

    values, missing = {}, []
    for key, service in KEYS.items():
        value, source = keychain(service)
        if not value:
            missing.append(service)
            print(f"  {key:14} FALTA: ni la variable {service} ni el Llavero {ACCOUNT} / {service}")
            continue
        if key == "age-recipient":
            if value.upper().startswith("AGE-SECRET-KEY-"):
                die(f"{source} tiene una clave PRIVADA de age; ahí va la pública (age1...). No la he impreso.")
            if not RECIPIENT_RE.fullmatch(value):
                die(f"{source} no es una clave pública de age (age1 + 58 caracteres)")
            shown = value
        elif key == "s3-access-key":
            shown = f"{value[:4]}… ({len(value)} caracteres)"
        else:
            shown = f"puesta ({len(value)} caracteres)"
        values[key] = value
        print(f"  {key:14} {shown}  ({source})")
    if missing:
        die("faltan entradas en el Llavero; guárdalas con: security add-generic-password -a aspavi -s <ENTRADA> -w")

    r = kubectl(opts.kubeconfig, "-n", NAMESPACE, "get", "secret", NAME, "-o", "name", "--ignore-not-found")
    if r.returncode != 0:
        die(f"no puedo leer {NAMESPACE}/{NAME}: {r.stderr.strip()}")
    exists = bool(r.stdout.strip())
    ctx = kubectl(opts.kubeconfig, "config", "current-context").stdout.strip() or "?"
    verb = "replace" if exists else "create"
    print(f"== {NAMESPACE}/{NAME} en {ctx}: {'existe, se sustituye' if exists else 'no existe, se crea'}")
    if not opts.apply:
        print("== dry-run: no se ha escrito nada (añade --apply)")
        return

    body = {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
            "metadata": {"name": NAME, "namespace": NAMESPACE,
                         "labels": {"app.kubernetes.io/managed-by": "scripts-backup-create-secret"}},
            "data": {k: base64.b64encode(v.encode()).decode() for k, v in values.items()}}
    r = kubectl(opts.kubeconfig, verb, "-f", "-", stdin=json.dumps(body))
    if r.returncode != 0:
        die(f"no puedo escribir {NAMESPACE}/{NAME}: {r.stderr.strip()}")
    r = kubectl(opts.kubeconfig, "-n", NAMESPACE, "get", "secret", NAME, "-o", "json")
    have = sorted(json.loads(r.stdout).get("data", {})) if r.returncode == 0 else []
    if have != sorted(KEYS):
        die(f"el Secret ha quedado con las claves {have}, esperaba {sorted(KEYS)}")
    print(f"== hecho ({verb}): claves {', '.join(have)}")


if __name__ == "__main__":
    main()
