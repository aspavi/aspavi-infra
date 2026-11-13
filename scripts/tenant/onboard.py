#!/usr/bin/env python3
"""Alta de un tenant sin desplegar ningún servicio (ASP-21).

  python3 scripts/tenant/onboard.py <id> --name "<Nombre>" --admin-email <email> [--dry-run]

Idempotente: se puede repetir tantas veces como haga falta, y cada paso solo
hace lo que falta.

  1. realm rrhh-tenant-<id>: si no existe, lo crea por la Admin API desde
     realm-template.json (la forma de apps/keycloak/realm-import-acme.yaml, con
     <id> y <Nombre> sustituidos). Si ya existe, no lo toca.
  2. scripts/keycloak/realm-login-settings.py, realm-smtp.py y
     bootstrap-iam-realm.py (este guarda el secret del client rrhh-iam-service
     en rrhh-system/keycloak-client-secrets[iam-service-secret-<id>], que iam
     lee en caliente). Este script NO siembra los permisos ni los roles
     compuestos: los carga iam (CatalogBootstrap) al ver el secret nuevo.
  3. el admin de RRHH: usuario <email> con el rol ADMIN_RRHH, una contraseña
     temporal y la acción UPDATE_PASSWORD. La contraseña se guarda en el Secret
     rrhh-auth/tenant-<id>-initial-admin (claves username y password) ANTES de
     crear el usuario, y no se imprime nunca. NO se envía ningún correo. Si el
     usuario ya existe, no se toca su contraseña; solo se le asegura el rol.
  4. imprime lo que queda por hacer fuera: el registro DNS y el bloque del
     Ingress del front (apps/rrhh-frontend/deployment.yaml), con una entrada
     de tls propia (secretName rrhh-frontend-<id>-tls). Sin secretos.

Con --dry-run no escribe nada: lee el estado y dice qué haría.

Destinos: Keycloak en KEYCLOAK_URL (por defecto http://localhost:18080, el
port-forward de scripts/keycloak/README.md; credenciales en kcadmin.py) y el
clúster de --kubeconfig (por defecto ~/.kube/aspavi.kubeconfig). Los imprime al
empezar.

Requisitos: python3 (solo biblioteca estándar) y kubectl.
"""
import argparse
import base64
import json
import os
import re
import secrets
import socket
import string
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
KEYCLOAK_SCRIPTS = REPO / "scripts" / "keycloak"
sys.path.insert(0, str(KEYCLOAK_SCRIPTS))

from kcadmin import KC, Admin, die, q, say  # noqa: E402

TEMPLATE = HERE / "realm-template.json"
FRONT_INGRESS = REPO / "apps" / "rrhh-frontend" / "deployment.yaml"
TENANT_RE = re.compile(r"^[a-z][a-z0-9-]{1,29}$")  # la misma que auth-lib, el front, el CORS y Keycloak
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# <id>.aspavi.com no puede pisar un host que ya es de otra cosa
RESERVED = {"api", "app", "keycloak", "argocd", "tapete", "www", "mail", "admin", "master", "aspavi"}
AUTH_NS = "rrhh-auth"
ADMIN_ROLE = "ADMIN_RRHH"
DNS_REFERENCE = "test.aspavi.com"  # sus registros A son los de los nodos del clúster
PASSWORD_ALPHABET = string.ascii_letters + string.digits


def render_realm(tenant, name):
    """La plantilla con el id y el nombre sustituidos (el nombre, escapado para JSON)."""
    raw = TEMPLATE.read_text()
    raw = raw.replace("__TENANT__", tenant).replace("__NAME__", json.dumps(name)[1:-1])
    rep = json.loads(raw)
    return {k: v for k, v in rep.items() if not k.startswith("_")}


class Kube:
    def __init__(self, kubeconfig):
        self.kubeconfig = kubeconfig

    def run(self, *args, stdin=None):
        return subprocess.run(["kubectl", "--kubeconfig", self.kubeconfig, *args],
                              input=stdin, capture_output=True, text=True)

    def context(self):
        r = self.run("config", "current-context")
        return r.stdout.strip() if r.returncode == 0 else "?"

    def get_secret(self, ns, name):
        r = self.run("-n", ns, "get", "secret", name, "-o", "json", "--ignore-not-found")
        if r.returncode != 0:
            die(f"no puedo leer {ns}/{name}: {r.stderr.strip()}")
        if not r.stdout.strip():
            return None
        data = json.loads(r.stdout).get("data", {})
        return {k: base64.b64decode(v).decode() for k, v in data.items()}

    def write_secret(self, ns, name, values, labels, exists):
        """create (o replace si ya existe) desde stdin: el valor no va en la línea
        de comandos ni en last-applied-configuration (no se usa apply)."""
        body = {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
                "metadata": {"name": name, "namespace": ns, "labels": labels},
                "data": {k: base64.b64encode(v.encode()).decode() for k, v in values.items()}}
        r = self.run("replace" if exists else "create", "-f", "-", stdin=json.dumps(body))
        if r.returncode != 0:
            die(f"no puedo escribir {ns}/{name}: {r.stderr.strip()}")


def run_script(script, *args):
    """Los scripts de scripts/keycloak/, con el mismo KEYCLOAK_URL y credenciales."""
    print(f"  -- {script} {' '.join(args)}")
    r = subprocess.run([sys.executable, str(KEYCLOAK_SCRIPTS / script), *args], cwd=KEYCLOAK_SCRIPTS)
    if r.returncode != 0:
        die(f"{script} ha fallado (código {r.returncode})")


def ensure_realm(kc, realm, tenant, name, dry_run):
    print(f"== 1. realm {realm}")
    current = kc.get(realm, ok404=True)
    if current is not None:
        say("ok", f"ya existe (displayName: {current.get('displayName')!r}); no se toca")
        return True
    if dry_run:
        say("[dry-run]", f"crearlo desde {TEMPLATE.relative_to(REPO)} con displayName 'RRHH - {name}'")
        return False
    say("aplicando", f"crearlo desde {TEMPLATE.relative_to(REPO)}")
    kc.call("POST", "", render_realm(tenant, name))
    if kc.get(realm, ok404=True) is None:
        die(f"{realm} no aparece tras crearlo")
    return True


def realm_scripts(tenant, realm_exists, dry_run, kubeconfig, smtp_config):
    print("== 2. ajustes del realm, correo saliente y client de iam")
    if dry_run and not realm_exists:
        for s in ("realm-login-settings.py", "realm-smtp.py", "bootstrap-iam-realm.py"):
            say("[dry-run]", f"{s} {tenant} (el realm aún no existe: no se puede ver más)")
        return
    apply = [] if dry_run else ["--apply"]
    smtp = ["--config", str(Path(smtp_config).resolve())] if smtp_config else []
    run_script("realm-login-settings.py", tenant, *apply)
    run_script("realm-smtp.py", tenant, *smtp, *apply)
    run_script("bootstrap-iam-realm.py", tenant, "--production", "--kubeconfig", kubeconfig,
               *(["--dry-run"] if dry_run else []))


def ensure_admin(kc, kube, realm, tenant, email, first, last, realm_exists, dry_run):
    secret_name = f"tenant-{tenant}-initial-admin"
    print(f"== 3. admin de RRHH {email}")
    stored = kube.get_secret(AUTH_NS, secret_name)
    user = None
    if realm_exists:
        found = kc.get(f"{realm}/users?email={q(email)}&exact=true")
        user = found[0] if found else None

    if user:
        say("ok", f"el usuario ya existe (id {user['id']}); no se toca su contraseña")
    else:
        if stored and stored.get("username") == email and stored.get("password"):
            password = stored["password"]
            say("ok", f"reutilizo la contraseña temporal de {AUTH_NS}/{secret_name} (una ejecución anterior no llegó a crear el usuario)")
        else:
            password = "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(24))
            if dry_run:
                say("[dry-run]", f"guardar una contraseña temporal nueva en {AUTH_NS}/{secret_name}")
            else:
                say("aplicando", f"guardar la contraseña temporal en {AUTH_NS}/{secret_name}")
                kube.write_secret(AUTH_NS, secret_name, {"username": email, "password": password},
                                  {"app.kubernetes.io/managed-by": "onboard.py", "aspavi.com/tenant": tenant},
                                  exists=stored is not None)
        if dry_run:
            say("[dry-run]", f"crear el usuario {email} con contraseña temporal y UPDATE_PASSWORD, sin correo")
        else:
            say("aplicando", f"crear el usuario {email} con contraseña temporal y UPDATE_PASSWORD, sin correo")
            rep = {"username": email, "email": email, "enabled": True, "emailVerified": True,
                   "requiredActions": ["UPDATE_PASSWORD"],
                   "credentials": [{"type": "password", "value": password, "temporary": True}]}
            if first:
                rep["firstName"] = first
            if last:
                rep["lastName"] = last
            kc.call("POST", f"{realm}/users", rep)
            found = kc.get(f"{realm}/users?email={q(email)}&exact=true")
            user = found[0] if found else die(f"el usuario {email} no aparece tras crearlo")
        del password

    if not user:
        say("[dry-run]", f"darle el rol {ADMIN_ROLE}")
        return
    roles = {r["name"] for r in kc.get(f"{realm}/users/{user['id']}/role-mappings/realm")}
    if ADMIN_ROLE in roles:
        say("ok", f"tiene el rol {ADMIN_ROLE}")
    elif dry_run:
        say("[dry-run]", f"darle el rol {ADMIN_ROLE}")
    else:
        say("aplicando", f"darle el rol {ADMIN_ROLE}")
        role = kc.get(f"{realm}/roles/{q(ADMIN_ROLE)}")
        kc.call("POST", f"{realm}/users/{user['id']}/role-mappings/realm", [{"id": role["id"], "name": role["name"]}])
    if not user.get("enabled", True):
        say("AVISO", "el usuario está deshabilitado; no se cambia")


def pending_outside(tenant):
    host = f"{tenant}.aspavi.com"
    print("== 4. lo que falta fuera de este script")
    try:
        ips = sorted({ai[4][0] for ai in socket.getaddrinfo(DNS_REFERENCE, 443, socket.AF_INET)})
    except OSError:
        ips = []
    try:
        current = sorted({ai[4][0] for ai in socket.getaddrinfo(host, 443, socket.AF_INET)})
    except OSError:
        current = []
    if ips and current == ips:
        say("ok", f"DNS: {host} ya apunta a {' '.join(ips)}")
    else:
        print(f"  DNS (Hetzner DNS, zona aspavi.com): registros A de '{tenant}', los mismos que {DNS_REFERENCE}:")
        for ip in ips or ["<no he podido resolver " + DNS_REFERENCE + ">"]:
            print(f"      {tenant}  A  {ip}")
        if current:
            say("AVISO", f"{host} ya resuelve a {' '.join(current)}, que no coincide")

    text = FRONT_INGRESS.read_text() if FRONT_INGRESS.exists() else ""
    if f"host: {host}" in text and f"- {host}" in text:
        say("ok", f"Ingress del front: {host} ya está en {FRONT_INGRESS.relative_to(REPO)}")
    else:
        print(f"  Ingress del front, en una PR a {FRONT_INGRESS.relative_to(REPO)} (lo único por tenant que queda en infra):")
        # Entrada de tls propia, no en la de test y acme: si el DNS del tenant
        # aún no resuelve, su reto HTTP-01 falla solo para él y no bloquea la
        # renovación de rrhh-frontend-tls.
        print(f"    spec.tls, añadir una entrada nueva (no meter el host en la de test y acme):")
        print(f"    - hosts:\n        - {host}\n      secretName: rrhh-frontend-{tenant}-tls")
        print(f"    spec.rules, añadir:")
        print(f"    - host: {host}\n      http:\n        paths:\n          - path: /\n            pathType: Prefix\n"
              f"            backend:\n              service:\n                name: rrhh-frontend\n"
              f"                port:\n                  number: 80")
    print(f"  Contraseña temporal del admin (no se imprime aquí):\n"
          f"      kubectl -n {AUTH_NS} get secret tenant-{tenant}-initial-admin -o jsonpath='{{.data.password}}' | base64 -d")


def main():
    ap = argparse.ArgumentParser(description="Alta de un tenant (ver la cabecera del script).")
    ap.add_argument("tenant", help="id del tenant: ^[a-z][a-z0-9-]{1,29}$; el realm es rrhh-tenant-<id> y el host <id>.aspavi.com")
    ap.add_argument("--name", required=True, help="nombre visible, p. ej. \"Demo S.L.\" (displayName 'RRHH - <nombre>')")
    ap.add_argument("--admin-email", required=True, help="email del primer admin de RRHH; es también su usuario")
    ap.add_argument("--admin-first-name", help="nombre del admin (si falta, Keycloak se lo pide al entrar)")
    ap.add_argument("--admin-last-name", help="apellidos del admin (si faltan, Keycloak se los pide al entrar)")
    ap.add_argument("--dry-run", action="store_true", help="no escribe nada: dice qué haría")
    ap.add_argument("--kubeconfig", default=os.path.expanduser("~/.kube/aspavi.kubeconfig"),
                    help="kubeconfig del clúster (por defecto ~/.kube/aspavi.kubeconfig)")
    ap.add_argument("--smtp-config", help="fichero de correo para realm-smtp.py (por defecto scripts/keycloak/smtp.json)")
    opts = ap.parse_args()
    sys.stdout.reconfigure(line_buffering=True)  # en orden con la salida de los scripts hijos

    tenant = opts.tenant
    if not TENANT_RE.match(tenant):
        ap.exit(2, f"id de tenant no válido: {tenant!r} (^[a-z][a-z0-9-]{{1,29}}$)\n")
    if tenant in RESERVED:
        ap.exit(2, f"'{tenant}' está reservado: {tenant}.aspavi.com es otra cosa\n")
    email = opts.admin_email.strip().lower()
    if not EMAIL_RE.match(email):
        ap.exit(2, f"email no válido: {opts.admin_email!r}\n")
    name = opts.name.strip()
    if not name:
        ap.exit(2, "--name no puede estar vacío\n")

    realm = f"rrhh-tenant-{tenant}"
    kube = Kube(opts.kubeconfig)
    print(f"== alta del tenant {tenant}{' (dry-run)' if opts.dry_run else ''}")
    print(f"   Keycloak: {KC}   clúster: {kube.context()} ({opts.kubeconfig})")
    kc = Admin()

    exists = ensure_realm(kc, realm, tenant, name, opts.dry_run)
    realm_scripts(tenant, exists, opts.dry_run, opts.kubeconfig, opts.smtp_config)
    ensure_admin(kc, kube, realm, tenant, email, opts.admin_first_name, opts.admin_last_name, exists, opts.dry_run)
    pending_outside(tenant)
    print("== hecho" + (" (dry-run: no se ha escrito nada)" if opts.dry_run else ""))


if __name__ == "__main__":
    main()
