#!/usr/bin/env python3
"""Paso único para un realm de Keycloak que YA existe.

Lo deja como lo crearía hoy apps/keycloak/realm-import-<tenant>.yaml, que el
operador no vuelve a aplicar (import --override=false):

  1. employee_id declarado en el user profile (lo ve el usuario, solo lo edita un admin)
  2. mapper employee_id en el client rrhh-frontend
  3. client confidencial rrhh-iam-service, con service account y mapper tenant_id
  4. roles de realm-management del service account: view-realm, manage-realm,
     view-users, query-users, manage-users
  5. su secret en rrhh-system/keycloak-client-secrets, clave iam-service-secret-<tenant>

Idempotente: solo crea lo que falta. No borra ni sobrescribe nada que ya exista;
si el service account tiene roles de más, avisa y no los toca.

Uso:
  python3 scripts/keycloak/bootstrap-iam-realm.py <tenant> [--dry-run] [--production] [--kubeconfig RUTA]

Credenciales: ver kcadmin.py (admin de master + Llavero, solo en memoria).
El client secret tampoco se imprime: va de Keycloak a `kubectl patch` por stdin.
"""
import argparse
import base64
import json
import os
import subprocess

from kcadmin import Admin, die, q, realm_of, say

APP_NS = "rrhh-system"
SECRET_NAME = "keycloak-client-secrets"
IAM_CLIENT = "rrhh-iam-service"
FRONT_CLIENT = "rrhh-frontend"
SA_ROLES = ["view-realm", "manage-realm", "view-users", "query-users", "manage-users"]
UUID_PATTERN = "^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
EMPLOYEE_ID_ATTRIBUTE = {
    "name": "employee_id",
    "displayName": "Employee ID",
    "validations": {"pattern": {"pattern": UUID_PATTERN, "error-message": "employee_id debe ser un UUID"}},
    "permissions": {"view": ["admin", "user"], "edit": ["admin"]},
    "multivalued": False,
}
EMPLOYEE_ID_MAPPER = {
    "name": "employee_id",
    "protocol": "openid-connect",
    "protocolMapper": "oidc-usermodel-attribute-mapper",
    "config": {
        "user.attribute": "employee_id",
        "claim.name": "employee_id",
        "jsonType.label": "String",
        "id.token.claim": "true",
        "access.token.claim": "true",
        "userinfo.token.claim": "true",
    },
}


def iam_client_rep(tenant):
    return {
        "clientId": IAM_CLIENT,
        "name": "IAM Service",
        "enabled": True,
        "publicClient": False,
        "standardFlowEnabled": False,
        "directAccessGrantsEnabled": False,
        "serviceAccountsEnabled": True,
        "clientAuthenticatorType": "client-secret",
        "protocol": "openid-connect",
        "protocolMappers": [{
            "name": "tenant_id",
            "protocol": "openid-connect",
            "protocolMapper": "oidc-hardcoded-claim-mapper",
            "config": {
                "claim.name": "tenant_id",
                "claim.value": tenant,
                "jsonType.label": "String",
                "id.token.claim": "true",
                "access.token.claim": "true",
            },
        }],
    }


def kubectl(kubeconfig, *args, stdin=None):
    return subprocess.run(["kubectl", "--kubeconfig", kubeconfig, *args],
                          input=stdin, capture_output=True, text=True)


def main():
    ap = argparse.ArgumentParser(description="Paso único de IAM en un realm existente (ver la cabecera del script).")
    ap.add_argument("tenant", help="test | acme | ...  (realm rrhh-tenant-<tenant>)")
    ap.add_argument("--dry-run", action="store_true", help="solo dice qué haría")
    ap.add_argument("--production", action="store_true",
                    help="obligatorio para aplicar en cualquier tenant que no sea test")
    ap.add_argument("--kubeconfig", default=os.path.expanduser("~/.kube/aspavi.kubeconfig"),
                    help="kubeconfig del clúster (por defecto ~/.kube/aspavi.kubeconfig)")
    opts = ap.parse_args()

    tenant = opts.tenant.removeprefix("rrhh-tenant-")
    if tenant != "test" and not opts.dry_run and not opts.production:
        ap.exit(2, f"El tenant '{tenant}' no es test: añade --production para aplicarlo (o --dry-run para verlo).\n")
    realm = realm_of(tenant)
    secret_key = f"iam-service-secret-{tenant}"
    dry_run = opts.dry_run

    def change(msg):
        say("[dry-run]" if dry_run else "aplicando", msg)
        return not dry_run

    print(f"== {realm}{' (dry-run)' if dry_run else ''}")
    kc = Admin()
    if kc.get(realm, ok404=True) is None:
        die(f"el realm {realm} no existe")

    # 1. user profile
    profile = kc.get(f"{realm}/users/profile")
    if any(a["name"] == "employee_id" for a in profile.get("attributes", [])):
        say("ok", "el user profile ya declara employee_id")
    elif change("declarar employee_id en el user profile"):
        profile["attributes"].append(EMPLOYEE_ID_ATTRIBUTE)
        kc.call("PUT", f"{realm}/users/profile", profile)

    # 2. mapper employee_id en rrhh-frontend
    front_id = kc.client_uuid(realm, FRONT_CLIENT) or die(f"no existe el client {FRONT_CLIENT} en {realm}")
    if any(m["name"] == "employee_id" for m in kc.get(f"{realm}/clients/{front_id}/protocol-mappers/models")):
        say("ok", f"{FRONT_CLIENT} ya tiene el mapper employee_id")
    elif change(f"crear el mapper employee_id en {FRONT_CLIENT}"):
        kc.call("POST", f"{realm}/clients/{front_id}/protocol-mappers/models", EMPLOYEE_ID_MAPPER)

    # 3. client rrhh-iam-service
    iam_id = kc.client_uuid(realm, IAM_CLIENT)
    if iam_id:
        say("ok", f"el client {IAM_CLIENT} ya existe")
    elif change(f"crear el client {IAM_CLIENT}"):
        kc.call("POST", f"{realm}/clients", iam_client_rep(tenant))
        iam_id = kc.client_uuid(realm, IAM_CLIENT) or die(f"no aparece {IAM_CLIENT} tras crearlo")

    # 4. roles de realm-management del service account
    if not iam_id:
        say("[dry-run]", f"dar al service account: {' '.join(SA_ROLES)}")
    else:
        sa = kc.get(f"{realm}/clients/{iam_id}/service-account-user", ok404=True)
        if sa is None:
            die(f"{IAM_CLIENT} existe pero no tiene service account: actívalo a mano (serviceAccountsEnabled)")
        rm_id = kc.client_uuid(realm, "realm-management") or die("no existe el client realm-management")
        current = {r["name"] for r in kc.get(f"{realm}/users/{sa['id']}/role-mappings/clients/{rm_id}")}
        missing = [r for r in SA_ROLES if r not in current]
        if not missing:
            say("ok", f"el service account ya tiene {' '.join(SA_ROLES)}")
        elif change(f"dar al service account: {' '.join(missing)}"):
            reps = [kc.get(f"{realm}/clients/{rm_id}/roles/{q(r)}") for r in missing]
            kc.call("POST", f"{realm}/users/{sa['id']}/role-mappings/clients/{rm_id}",
                    [{"id": r["id"], "name": r["name"]} for r in reps])
        extra = sorted(current - set(SA_ROLES))
        if extra:
            say("AVISO", f"el service account tiene roles de más (no se tocan): {' '.join(extra)}")

    # 5. secret en Kubernetes
    if not iam_id:
        say("[dry-run]", f"guardar el secret en {APP_NS}/{SECRET_NAME}[{secret_key}]")
    else:
        got = kubectl(opts.kubeconfig, "-n", APP_NS, "get", "secret", SECRET_NAME, "-o", "json")
        if got.returncode != 0:
            die(f"no puedo leer {APP_NS}/{SECRET_NAME}: {got.stderr.strip()}")
        stored_b64 = json.loads(got.stdout).get("data", {}).get(secret_key)
        stored = base64.b64decode(stored_b64).decode() if stored_b64 else None
        client_secret = (kc.get(f"{realm}/clients/{iam_id}/client-secret") or {}).get("value")
        if not client_secret:
            die(f"el client {IAM_CLIENT} no tiene secret")
        if stored == client_secret:
            say("ok", f"{APP_NS}/{SECRET_NAME}[{secret_key}] ya está al día")
        elif change(f"{'actualizar' if stored else 'guardar'} el secret en {APP_NS}/{SECRET_NAME}[{secret_key}]"):
            patch = json.dumps({"stringData": {secret_key: client_secret}})
            res = kubectl(opts.kubeconfig, "-n", APP_NS, "patch", "secret", SECRET_NAME,
                          "--type=merge", "--patch-file=/dev/stdin", stdin=patch)
            if res.returncode != 0:
                die(f"kubectl patch falló: {res.stderr.strip()}")
        del client_secret, stored

    print("== hecho")


if __name__ == "__main__":
    main()
