#!/usr/bin/env python3
"""Inventario de SOLO LECTURA de uno o varios realms de ASPAVI (Admin REST API).

Por cada realm:
  - roles de realm: cuántos tienen la forma recurso:acción y cuáles no
  - si los 4 roles base son compuestos y cuántos hijos tienen
  - clients
  - usuarios: enabled, email, emailVerified, employee_id y roles de realm, marcando a los que no
    tienen ningún rol base; los service accounts muestran además sus roles
    de realm-management

Solo hace GET, aparte de pedir el token. No imprime secretos.

Uso:
  python3 scripts/keycloak/inventory.py [<tenant|realm> ...]

  sin argumentos   todos los realms salvo master
  test, acme       se traducen a rrhh-tenant-test, rrhh-tenant-acme

Credenciales: ver kcadmin.py (admin de master + Llavero, solo en memoria).
"""
import argparse

from kcadmin import BASE_ROLES, Admin, q, realm_of


def inventory(kc, realm):
    print(f"\n===== {realm} =====")
    if kc.get(realm, ok404=True) is None:
        print("  (no existe)")
        return
    roles = kc.get(f"{realm}/roles?briefRepresentation=false&max=1000")
    names = sorted(r["name"] for r in roles)
    perms = [n for n in names if ":" in n]
    others = [n for n in names if ":" not in n]
    print(f"Roles de realm: {len(names)} ({len(perms)} con forma recurso:acción)")
    print("  sin ':' ->", ", ".join(others) or "-")
    by_name = {r["name"]: r for r in roles}
    for base in BASE_ROLES:
        r = by_name.get(base)
        if r is None:
            print(f"  {base}: NO EXISTE")
            continue
        children = kc.get(f"{realm}/roles/{q(base)}/composites") if r.get("composite") else []
        print(f"  {base}: compuesto={bool(r.get('composite'))} hijos={len(children)}")

    clients = sorted(c["clientId"] for c in kc.get(f"{realm}/clients?max=500"))
    print("Clients:", ", ".join(clients))
    rm_id = kc.client_uuid(realm, "realm-management")

    users = kc.get(f"{realm}/users?max=1000&briefRepresentation=false")
    print(f"Usuarios: {len(users)}")
    for u in sorted(users, key=lambda x: x["username"]):
        realm_roles = sorted(x["name"] for x in kc.get(f"{realm}/users/{u['id']}/role-mappings/realm"))
        emp = (u.get("attributes") or {}).get("employee_id", ["-"])[0]
        line = (f"  {u['username']} enabled={u.get('enabled')} email={u.get('email') or '-'}"
                f" emailVerified={bool(u.get('emailVerified'))} employee_id={emp} roles={realm_roles}")
        if u.get("serviceAccountClientId") or u["username"].startswith("service-account-"):
            mgmt = sorted(x["name"] for x in kc.get(f"{realm}/users/{u['id']}/role-mappings/clients/{rm_id}")) \
                if rm_id else []
            line += f" realm-management={mgmt} (service account)"
        elif not any(x in BASE_ROLES for x in realm_roles):
            line += "   <-- SIN ROL BASE"
        print(line)


def main():
    ap = argparse.ArgumentParser(description="Inventario de solo lectura de realms de Keycloak.")
    ap.add_argument("realms", nargs="*", help="tenant (test, acme) o nombre de realm; vacío = todos salvo master")
    opts = ap.parse_args()
    kc = Admin()
    realms = [realm_of(r) for r in opts.realms] or \
        sorted(r["realm"] for r in kc.get("") if r["realm"] != "master")
    for realm in realms:
        inventory(kc, realm)


if __name__ == "__main__":
    main()
