#!/usr/bin/env python3
"""Prueba de restauración de una copia por tenant (ASP-43), en LOCAL.

El CronJob ya prueba cada copia antes de cifrarla. Esto prueba lo que de verdad
hay en el bucket, de punta a punta, con la clave privada que solo tiene el
usuario: baja la copia, la descifra, la restaura en un Postgres TEMPORAL (un
contenedor de Docker sin red, que se borra al acabar; nunca la base real) y
compara, tabla a tabla, las filas con el manifiesto de la copia, y que todas
las FKs se cumplan (apps/postgresql/tenant-backup/restore-check.sh). Con
--keycloak, además, importa el realm en un Keycloak temporal (contenedor, solo
en 127.0.0.1) y compara sus usuarios con los que dice la copia.

Uso:
  python3 scripts/backup/restore-test.py acme                  # la copia más reciente
  python3 scripts/backup/restore-test.py acme --key backups/acme/acme-20261114T023000Z.tar.gz.age
  python3 scripts/backup/restore-test.py acme --file copia.tar.gz.age --identity clave.txt
  python3 scripts/backup/restore-test.py acme --keycloak

Necesita Docker. age, si está (brew install age); si no, descifra en un
contenedor alpine con el age de sus paquetes.

Credenciales (del Llavero de macOS, cuenta aspavi, o de una variable de entorno
con el mismo nombre); ninguna se imprime ni va en la línea de comandos:
  BACKUP_S3_ACCESS_KEY, BACKUP_S3_SECRET_KEY  para bajar del bucket
  BACKUP_AGE_IDENTITY                         la clave PRIVADA de age (o --identity FICHERO)
Bucket, endpoint, región y prefijo: los de apps/postgresql/tenant-backup/settings.conf
(o las variables S3_BUCKET, S3_ENDPOINT, S3_REGION, S3_PREFIX, S3_ADDRESSING).

La copia descifrada son datos personales en claro: va a un directorio temporal
que se borra al acabar (--keep lo deja y dice dónde).
"""
import argparse
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
TB = os.path.join(REPO, "apps", "postgresql", "tenant-backup")
PG_IMAGE = "postgres:18.3-alpine"
KC_IMAGE = "quay.io/keycloak/keycloak:26.5.5"  # la del clúster (apps/postgresql/tenant-backup/cronjob.yaml)
S3_IMAGE = "alpine/k8s:1.34.1"  # python3 + botocore, la misma del paso upload
AGE_IMAGE = "alpine:3.22"
ACCOUNT = "aspavi"


def say(msg=""):
    print(msg, flush=True)


def die(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def keychain(service, required=True):
    value = os.environ.get(service, "").strip()
    if not value:
        try:
            value = subprocess.run(["security", "find-generic-password", "-a", ACCOUNT, "-s", service, "-w"],
                                   capture_output=True, text=True, check=True).stdout.strip()
        except (OSError, subprocess.CalledProcessError):
            value = ""
    if not value and required:
        die(f"falta {service}: ni la variable ni el Llavero {ACCOUNT} / {service}")
    return value


def settings():
    conf = {}
    with open(os.path.join(TB, "settings.conf")) as f:
        for line in f:
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.rstrip("\n").split("=", 1)
                conf[k.strip()] = v.strip()
    for k in ("S3_BUCKET", "S3_ENDPOINT", "S3_REGION", "S3_PREFIX", "S3_ADDRESSING"):
        if os.environ.get(k):
            conf[k] = os.environ[k]
    for k in ("S3_BUCKET", "S3_ENDPOINT", "S3_REGION", "S3_PREFIX"):
        if not conf.get(k):
            die(f"falta {k} en settings.conf (o en la variable {k})")
    return conf


def docker(*args, env=None, stdin=None, check=True, capture=True):
    r = subprocess.run(["docker", *args], input=stdin, env=env, capture_output=capture, text=isinstance(stdin, str) or stdin is None)
    if check and r.returncode != 0:
        die(f"docker {args[0]} ha fallado:\n{(r.stderr or '')[-2000:]}")
    return r


DOWNLOAD = r'''
import os, re, sys, botocore.session
from botocore.config import Config
c = botocore.session.get_session().create_client("s3", endpoint_url=os.environ["S3_ENDPOINT"], region_name=os.environ["S3_REGION"],
    aws_access_key_id=os.environ["BACKUP_S3_ACCESS_KEY"], aws_secret_access_key=os.environ["BACKUP_S3_SECRET_KEY"],
    config=Config(s3={"addressing_style": os.environ.get("S3_ADDRESSING") or "virtual"},
                  request_checksum_calculation="when_required", response_checksum_validation="when_required"))
bucket, prefix, tenant, key = os.environ["S3_BUCKET"], os.environ["S3_PREFIX"].strip("/"), os.environ["TENANT"], os.environ.get("KEY")
if not key:
    pat = re.compile(re.escape(f"{prefix}/{tenant}/{tenant}-") + r"\d{8}T\d{6}Z\.tar\.gz\.age$")
    keys = [o["Key"] for p in c.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=f"{prefix}/{tenant}/")
            for o in p.get("Contents", []) if pat.match(o["Key"])]
    if not keys:
        sys.exit(f"no hay copias de {tenant} en s3://{bucket}/{prefix}/{tenant}/")
    key = max(keys)
    print(f"{len(keys)} copias de {tenant}; la más reciente: {key}")
obj = c.get_object(Bucket=bucket, Key=key)
with open("/out/copy.tar.gz.age", "wb") as f:
    for chunk in iter(lambda: obj["Body"].read(1 << 20), b""):
        f.write(chunk)
print(f"bajada: {key} ({os.path.getsize('/out/copy.tar.gz.age')} bytes)")
'''


def download(conf, tenant, key, tmp):
    env = dict(os.environ, BACKUP_S3_ACCESS_KEY=keychain("BACKUP_S3_ACCESS_KEY"),
               BACKUP_S3_SECRET_KEY=keychain("BACKUP_S3_SECRET_KEY"), TENANT=tenant, KEY=key or "",
               **{k: conf.get(k, "") for k in ("S3_BUCKET", "S3_ENDPOINT", "S3_REGION", "S3_PREFIX", "S3_ADDRESSING")})
    # -e NOMBRE sin valor: Docker lo toma del entorno de este proceso; las
    # claves no aparecen en la línea de comandos
    names = ["BACKUP_S3_ACCESS_KEY", "BACKUP_S3_SECRET_KEY", "TENANT", "KEY",
             "S3_BUCKET", "S3_ENDPOINT", "S3_REGION", "S3_PREFIX", "S3_ADDRESSING"]
    args = ["run", "--rm", "-i", "--add-host", "host.docker.internal:host-gateway", "-v", f"{tmp}:/out"]
    for n in names:
        args += ["-e", n]
    r = docker(*args, "--entrypoint", "python3", S3_IMAGE, "-", env=env, stdin=DOWNLOAD, check=False)
    if r.returncode != 0:
        die(f"no he podido bajar la copia:\n{r.stderr.strip()[-1500:]}")
    say("  " + r.stdout.strip().replace("\n", "\n  "))
    return os.path.join(tmp, "copy.tar.gz.age")


def decrypt(src, dest, identity_file):
    if identity_file:
        with open(identity_file) as f:
            identity = f.read()
    else:
        identity = keychain("BACKUP_AGE_IDENTITY")
    if "AGE-SECRET-KEY-" not in identity.upper():
        die("la identidad de age no es una clave privada (AGE-SECRET-KEY-...)")
    # La clave va por la entrada estándar (age -i -), nunca a un fichero
    if shutil.which("age"):
        r = subprocess.run(["age", "--decrypt", "-i", "-", "-o", dest, src], input=identity.encode(), capture_output=True)
        tool = "age local"
    else:
        d, name = os.path.dirname(src), os.path.basename(src)
        r = subprocess.run(["docker", "run", "--rm", "-i", "-v", f"{d}:/d", AGE_IMAGE, "sh", "-c",
                            f"apk add -q age >/dev/null && age --decrypt -i - -o /d/{os.path.basename(dest)} /d/{name}"],
                           input=identity.encode(), capture_output=True)
        tool = f"age de {AGE_IMAGE} (no hay age local)"
    if r.returncode != 0:
        die(f"no se ha podido descifrar ({tool}): {r.stderr.decode(errors='replace').strip()[-500:]}")
    say(f"  descifrada con {tool}")


def restore(tenant, tdir):
    name = f"restore-test-pg-{secrets.token_hex(4)}"
    docker("run", "-d", "--name", name, "--network", "none", "-u", "postgres",
           "-v", f"{os.path.dirname(tdir)}:/backup", "-v", f"{TB}:/script:ro",
           "--tmpfs", "/scratch:uid=70", "--entrypoint", "sleep", PG_IMAGE, "infinity")
    try:
        docker("exec", name, "sh", "-c",
               "initdb -D /scratch/pg -U postgres --auth=trust --no-locale -E UTF8 >/scratch/initdb.log 2>&1 && "
               "pg_ctl -D /scratch/pg -o '-c listen_addresses= -k /scratch -c fsync=off' -l /scratch/pg.log -w start >/dev/null")
        r = docker("exec", "-e", "PGHOST=/scratch", "-e", "PGUSER=postgres", "-e", "DB_PREFIX=restore_", name,
                   "sh", "/script/restore-check.sh", f"/backup/{tenant}", check=False)
        say("  " + (r.stdout + r.stderr).strip().replace("\n", "\n  "))
        return r.returncode == 0
    finally:
        docker("rm", "-f", name, check=False)


def keycloak(tenant, tdir, info):
    realm = f"rrhh-tenant-{tenant}"
    path = os.path.join(tdir, "keycloak", f"{realm}-realm.json")
    if not os.path.exists(path):
        say("  la copia no lleva realm")
        return info.get("realm") != "si"
    with open(path) as f:
        rep = json.load(f)
    want_users = int(info.get("keycloak_users", "-1"))
    want_clients = len(rep.get("clients") or [])
    name, pwd = f"restore-test-kc-{secrets.token_hex(4)}", secrets.token_urlsafe(24)
    env = dict(os.environ, KC_BOOTSTRAP_ADMIN_USERNAME="admin", KC_BOOTSTRAP_ADMIN_PASSWORD=pwd)
    docker("run", "-d", "--name", name, "-p", "127.0.0.1::8080", "-e", "KC_BOOTSTRAP_ADMIN_USERNAME",
           "-e", "KC_BOOTSTRAP_ADMIN_PASSWORD", "-v", f"{os.path.dirname(path)}:/opt/keycloak/data/import:ro",
           KC_IMAGE, "start-dev", "--import-realm", env=env)
    try:
        port = docker("port", name, "8080").stdout.strip().splitlines()[0].rsplit(":", 1)[1]
        base = f"http://127.0.0.1:{port}"
        for _ in range(90):
            try:
                urllib.request.urlopen(f"{base}/realms/{realm}", timeout=3)
                break
            except (urllib.error.URLError, ConnectionError, OSError):
                time.sleep(2)
        else:
            logs = docker("logs", name, check=False)
            die(f"el Keycloak temporal no ha levantado el realm:\n{(logs.stdout + logs.stderr)[-1500:]}")
        body = urllib.parse.urlencode({"grant_type": "password", "client_id": "admin-cli",
                                       "username": "admin", "password": pwd}).encode()
        token = json.load(urllib.request.urlopen(f"{base}/realms/master/protocol/openid-connect/token", body))["access_token"]

        def get(p):
            return json.load(urllib.request.urlopen(urllib.request.Request(
                f"{base}/admin/realms/{realm}{p}", headers={"Authorization": f"Bearer {token}"})))
        users, clients = get("/users/count"), len(get("/clients"))
        ok = users == want_users and clients == want_clients
        say(f"  {realm} importado: {users} usuarios (la copia dice {want_users}), "
            f"{clients} clients (el JSON trae {want_clients}): {'bien' if ok else 'NO COINCIDE'}")
        return ok
    finally:
        docker("rm", "-f", name, check=False)


def main():
    ap = argparse.ArgumentParser(description="Prueba de restauración de una copia por tenant (ver la cabecera).")
    ap.add_argument("tenant")
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--key", help="objeto del bucket (por defecto, la copia más reciente del tenant)")
    src.add_argument("--file", help="copia .tar.gz.age ya bajada")
    ap.add_argument("--identity", help="fichero con la clave privada de age (por defecto, BACKUP_AGE_IDENTITY del Llavero)")
    ap.add_argument("--keycloak", action="store_true", help="importa también el realm en un Keycloak temporal")
    ap.add_argument("--keep", action="store_true", help="no borra la copia descifrada al acabar (¡datos en claro!)")
    opts = ap.parse_args()
    if not re.fullmatch(r"[a-z][a-z0-9-]{1,29}", opts.tenant):
        die(f"id de tenant no válido: {opts.tenant}")

    tmp = tempfile.mkdtemp(prefix=f"restore-test-{opts.tenant}-")
    ok = False
    try:
        say(f"== 1. copia de {opts.tenant}")
        if opts.file:
            enc = os.path.abspath(opts.file)
            shutil.copy(enc, os.path.join(tmp, "copy.tar.gz.age"))
            enc = os.path.join(tmp, "copy.tar.gz.age")
            say(f"  fichero local: {opts.file}")
        else:
            enc = download(settings(), opts.tenant, opts.key, tmp)
        say("== 2. descifrar")
        plain = os.path.join(tmp, "copy.tar.gz")
        decrypt(enc, plain, opts.identity)
        out = os.path.join(tmp, "x")
        with tarfile.open(plain) as tar:
            tar.extractall(out, filter="data")
        os.remove(plain)
        tdir = os.path.join(out, opts.tenant)
        if not os.path.isdir(tdir):
            die(f"la copia no es de {opts.tenant}: trae {os.listdir(out)}")
        with open(os.path.join(tdir, "backup.info")) as f:
            info = dict(line.rstrip("\n").split("=", 1) for line in f if "=" in line)
        say("  " + ", ".join(f"{k}={v}" for k, v in info.items() if not k.startswith("flyway.")))
        say("  Flyway: " + ", ".join(f"{k[7:]} V{v}" for k, v in info.items() if k.startswith("flyway.")))
        say("== 3. restauración en un Postgres temporal (Docker, sin red)")
        ok = restore(opts.tenant, tdir)
        if opts.keycloak:
            say("== 4. realm en un Keycloak temporal (Docker, en 127.0.0.1)")
            ok = keycloak(opts.tenant, tdir, info) and ok
    finally:
        if opts.keep:
            say(f"== la copia descifrada se queda en {tmp} (datos personales en claro: bórrala al acabar)")
        else:
            shutil.rmtree(tmp, ignore_errors=True)
    say("== BIEN: la copia se restaura y coincide" if ok else "== MAL: la copia NO se restaura bien (arriba)")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
