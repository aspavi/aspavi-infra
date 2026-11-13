#!/usr/bin/env python3
"""Copias por tenant (ASP-43), último paso: cifrar, subir y rotar.

Lo ejecuta el contenedor `upload` del CronJob postgresql-tenant-backup; ver
apps/postgresql/README.md, "Copias por tenant". Antes de este paso, dump.sh ha
dejado en $WORK/out/<tenant>/ la copia de cada tenant (ya restaurada y
comprobada en el Postgres temporal del pod) y kc-export.sh su realm.

Por cada tenant de $WORK/plan.tsv:
  1. comprueba el realm exportado (que es JSON, que es su realm) y anota cuántos
     usuarios lleva en backup.info;
  2. empaqueta la copia en un .tar.gz y la cifra con age para la clave PÚBLICA
     del Secret (la privada no está en el clúster): en el bucket no hay nada en
     claro, ni el manifiesto;
  3. la sube a s3://<bucket>/<prefijo>/<tenant>/<tenant>-<fecha>.tar.gz.age y
     comprueba con HEAD que ha llegado entera;
  4. rotación: de los objetos de ese tenant con ese patrón de nombre, deja N
     diarias, N semanales y N mensuales sin solaparse (ver keep_set;
     KEEP_DAILY/WEEKLY/MONTHLY, o la retención de su línea en tenants.conf), y
     borra el resto. Nunca toca otro nombre ni otro prefijo (el bucket es
     compartido con otros ficheros), y solo rota si la subida ha ido bien.

Credenciales: ficheros del Secret postgresql-tenant-backup (lo crea el usuario
con scripts/backup/create-secret.py). No se imprimen. Sale con 1 si algún
tenant falla; los demás siguen.
"""
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import tarfile

import botocore.session
from botocore.config import Config

WORK = os.environ.get("WORK", "/work")
SECRET_DIR = os.environ.get("SECRET_DIR", "/secret")
AGE = os.environ.get("AGE", "/tools/age")
NAME_RE = r"(?P<stamp>\d{8}T\d{6}Z)\.tar\.gz\.age"


def say(msg):
    print(msg, flush=True)


def setting(name):
    value = os.environ.get(name, "").strip()
    if not value:
        sys.exit(f"ERROR: falta {name} en la configuración (settings.env)")
    return value


def secret(name):
    path = os.path.join(SECRET_DIR, name)
    try:
        with open(path) as f:
            value = f.read().strip()
    except FileNotFoundError:
        sys.exit(f"ERROR: el Secret postgresql-tenant-backup no tiene la clave {name}")
    if not value:
        sys.exit(f"ERROR: la clave {name} del Secret postgresql-tenant-backup está vacía")
    return value


def recipient():
    value = secret("age-recipient")
    if value.upper().startswith("AGE-SECRET-KEY-"):
        # No se imprime: es la clave privada, y no debería estar en el clúster
        sys.exit("ERROR: age-recipient tiene una clave PRIVADA de age. Tiene que ser la pública (age1...);"
                 " la privada se guarda fuera del clúster. Borra el Secret y créalo de nuevo.")
    if not re.fullmatch(r"age1[02-9ac-hj-np-z]{58}", value):
        sys.exit("ERROR: age-recipient no es una clave pública de age (age1 + 58 caracteres)")
    return value


def s3_client():
    return botocore.session.get_session().create_client(
        "s3",
        endpoint_url=setting("S3_ENDPOINT"),
        region_name=setting("S3_REGION"),
        aws_access_key_id=secret("s3-access-key"),
        aws_secret_access_key=secret("s3-secret-key"),
        config=Config(
            s3={"addressing_style": os.environ.get("S3_ADDRESSING", "virtual")},
            # Solo las sumas que pide el API: los servicios compatibles con S3 no
            # siempre aceptan las CRC que botocore añade por defecto
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
            retries={"max_attempts": 5, "mode": "standard"},
            connect_timeout=20,
            read_timeout=120,
        ),
    )


def check_realm(tenant, tdir):
    """Valida el JSON del realm y añade a backup.info cuántos usuarios lleva."""
    path = os.path.join(tdir, "keycloak", f"rrhh-tenant-{tenant}-realm.json")
    with open(path) as f:
        realm = json.load(f)
    if realm.get("realm") != f"rrhh-tenant-{tenant}":
        raise RuntimeError(f"{path} es del realm {realm.get('realm')!r}")
    users = realm.get("users") or []
    service = sum(1 for u in users if u.get("serviceAccountClientId"))
    with_password = sum(1 for u in users if any(c.get("type") == "password" for c in u.get("credentials") or []))
    with open(os.path.join(tdir, "backup.info"), "a") as f:
        f.write(f"keycloak_users={len(users) - service}\nkeycloak_service_accounts={service}\n"
                f"keycloak_users_with_password={with_password}\n")
    return f"{len(users) - service} usuarios ({with_password} con contraseña), {service} cuentas de servicio"


def encrypt(tenant, tdir, rcpt, dest):
    """tar.gz del directorio del tenant, cifrado con age por una tubería: el
    .tar.gz en claro no llega a escribirse."""
    with open(dest, "wb") as out:
        age = subprocess.Popen([AGE, "--encrypt", "--recipient", rcpt], stdin=subprocess.PIPE, stdout=out)
        try:
            with tarfile.open(fileobj=age.stdin, mode="w|gz") as tar:
                for root, dirs, files in os.walk(tdir):
                    dirs.sort()
                    for name in sorted(files):
                        full = os.path.join(root, name)
                        tar.add(full, arcname=os.path.join(tenant, os.path.relpath(full, tdir)), recursive=False)
        finally:
            age.stdin.close()
        if age.wait() != 0:
            raise RuntimeError("age ha fallado al cifrar")
    with open(dest, "rb") as f:
        if not f.read(32).startswith(b"age-encryption.org/v1\n"):
            raise RuntimeError("el fichero cifrado no tiene la cabecera de age")
        f.seek(0)
        return hashlib.file_digest(f, "sha256").hexdigest()


def keep_set(stamps, daily, weekly, monthly):
    """Diarias, semanales y mensuales sin solaparse (abuelo-padre-hijo): las
    `daily` copias más recientes, una por día; luego, de las más antiguas que
    la última diaria, la más reciente de cada una de `weekly` semanas ISO; y de
    las más antiguas que la última semanal, la más reciente de cada uno de
    `monthly` meses. Con 7/4/6 y una copia al día quedan 17, que cubren unos
    siete meses y medio."""
    ordered = sorted(stamps, reverse=True)
    keep = set()
    older_than = None
    for count, period in ((daily, lambda t: t.date()),
                          (weekly, lambda t: t.isocalendar()[:2]),
                          (monthly, lambda t: (t.year, t.month))):
        seen = set()
        for t in ordered:
            if older_than is not None and t >= older_than:
                continue
            p = period(t)
            if p in seen:
                continue
            if len(seen) >= count:
                break
            seen.add(p)
            keep.add(t)
        if keep:
            older_than = min(keep)
    return keep


def rotate(s3, bucket, prefix, tenant, keep_rule, just_uploaded):
    daily, weekly, monthly = (int(x) for x in keep_rule.split("/"))
    pattern = re.compile(re.escape(f"{prefix}/{tenant}/{tenant}-") + NAME_RE + r"$")
    objects = {}
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=f"{prefix}/{tenant}/"):
        for obj in page.get("Contents", []):
            m = pattern.match(obj["Key"])
            if m:  # cualquier otro nombre bajo el prefijo no se toca
                objects[dt.datetime.strptime(m["stamp"], "%Y%m%dT%H%M%SZ")] = obj["Key"]
    if just_uploaded not in objects.values():
        raise RuntimeError("la copia recién subida no aparece al listar; no roto nada")
    keep = keep_set(objects, daily, weekly, monthly)
    gone = sorted(k for t, k in objects.items() if t not in keep and k != just_uploaded)
    for key in gone:
        s3.delete_object(Bucket=bucket, Key=key)
    say(f"    rotación {keep_rule}: {len(objects)} copias, se quedan {len(objects) - len(gone)}, se borran {len(gone)}"
        + (": " + ", ".join(k.rsplit("/", 1)[1] for k in gone) if gone else ""))


def main():
    plan_path = os.path.join(WORK, "plan.tsv")
    plan = []
    if os.path.exists(plan_path):
        with open(plan_path) as f:
            plan = [line.rstrip("\n").split("\t") for line in f if line.strip()]
    if not plan:
        say("Ningún tenant con copia: no hay nada que subir.")
        return 0
    bucket, prefix = setting("S3_BUCKET"), setting("S3_PREFIX").strip("/")
    rcpt = recipient()
    s3 = s3_client()
    os.makedirs(os.path.join(WORK, "enc"), exist_ok=True)

    say(f"== Subida a s3://{bucket}/{prefix}/<tenant>/ ({os.environ['S3_ENDPOINT']}), cifrado con age")
    failed = []
    for tenant, keep_rule, realm in plan:
        tdir = os.path.join(WORK, "out", tenant)
        try:
            with open(os.path.join(tdir, "backup.info")) as f:
                stamp = re.search(r"^created=(\S+)$", f.read(), re.M)[1]
            kc = check_realm(tenant, tdir) if realm == "si" else "sin realm"
            key = f"{prefix}/{tenant}/{tenant}-{stamp}.tar.gz.age"
            enc = os.path.join(WORK, "enc", f"{tenant}.tar.gz.age")
            digest = encrypt(tenant, tdir, rcpt, enc)
            size = os.path.getsize(enc)
            with open(enc, "rb") as body:
                s3.put_object(Bucket=bucket, Key=key, Body=body, ContentLength=size,
                              ContentType="application/octet-stream", Metadata={"sha256": digest})
            head = s3.head_object(Bucket=bucket, Key=key)
            if head["ContentLength"] != size or head.get("Metadata", {}).get("sha256") != digest:
                raise RuntimeError(f"el objeto subido no coincide (tamaño {head['ContentLength']} de {size})")
            os.remove(enc)
            say(f"  {tenant}: {key} ({size} bytes, sha256 {digest[:12]}…); Keycloak: {kc}")
            rotate(s3, bucket, prefix, tenant, keep_rule, key)
        except Exception as e:  # sigue con los demás tenants; el pod acaba en error
            failed.append(tenant)
            say(f"  {tenant}: ERROR: {type(e).__name__}: {e}")
    if failed:
        say(f"== Fallan: {', '.join(failed)}")
        return 1
    say("== Hecho")
    return 0


if __name__ == "__main__":
    sys.exit(main())
