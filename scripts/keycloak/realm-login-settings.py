#!/usr/bin/env python3
"""Ajustes de login de un realm de Keycloak que YA existe: el email es el usuario.

Lo deja como lo crearía hoy apps/keycloak/realm-import-<tenant>.yaml, que el
operador no vuelve a aplicar (import --override=false):

  1. realm: registrationEmailAsUsername=true, loginWithEmailAllowed=true,
     editUsernameAllowed=false
  2. user profile: el atributo email solo lo edita un admin
     (permissions.edit = ["admin"]); el resto del JSON se conserva tal cual

Por defecto NO escribe: imprime, por realm, cada ajuste con su valor actual y
el que tendría. Con --apply escribe solo lo que difiere. Idempotente.

Uso:
  python3 scripts/keycloak/realm-login-settings.py <tenant> [<tenant> ...] [--apply]

Credenciales: ver kcadmin.py (admin de master + Llavero, solo en memoria).
No lee ni imprime secretos.
"""
import argparse
import json

from kcadmin import Admin, realm_of, say

REALM_SETTINGS = {
    "registrationEmailAsUsername": True,
    "loginWithEmailAllowed": True,
    "editUsernameAllowed": False,
}
EMAIL_EDIT = ["admin"]


def show(value):
    return json.dumps(value, ensure_ascii=False)


def settle(kc, realm, apply):
    print(f"== {realm}{'' if apply else ' (dry-run)'}")
    rep = kc.get(realm, ok404=True)
    if rep is None:
        say("ERROR", "el realm no existe")
        return False

    # 1. ajustes del realm. El PUT parcial solo cambia los campos que lleva.
    patch = {}
    for key, want in REALM_SETTINGS.items():
        have = rep.get(key)
        if have == want:
            say("ok", f"{key} = {show(want)}")
        else:
            say("cambia", f"{key}: {show(have)} -> {show(want)}")
            patch[key] = want
    if patch and apply:
        kc.call("PUT", realm, patch)

    # 2. permisos de edición del email en el user profile
    profile = kc.get(f"{realm}/users/profile")
    email = next((a for a in profile.get("attributes", []) if a["name"] == "email"), None)
    if email is None:
        say("ERROR", "el user profile no declara el atributo email; no lo toco")
        return False
    perms = email.setdefault("permissions", {})
    have = perms.get("edit")
    if have == EMAIL_EDIT:
        say("ok", f"email.permissions.edit = {show(EMAIL_EDIT)}")
    else:
        say("cambia", f"email.permissions.edit: {show(have)} -> {show(EMAIL_EDIT)}")
        if apply:
            perms["edit"] = EMAIL_EDIT
            kc.call("PUT", f"{realm}/users/profile", profile)

    if apply and (patch or have != EMAIL_EDIT):
        return check(kc, realm)
    return True


def check(kc, realm):
    """Vuelve a leer lo escrito para no dar por bueno un PUT que Keycloak ignorase."""
    rep = kc.get(realm)
    email = next(a for a in kc.get(f"{realm}/users/profile")["attributes"] if a["name"] == "email")
    wrong = [k for k, v in REALM_SETTINGS.items() if rep.get(k) != v]
    if email.get("permissions", {}).get("edit") != EMAIL_EDIT:
        wrong.append("email.permissions.edit")
    say("comprobado" if not wrong else "ERROR", "todo aplicado" if not wrong else f"no ha quedado: {', '.join(wrong)}")
    return not wrong


def main():
    ap = argparse.ArgumentParser(description="Email como nombre de usuario en realms existentes (ver la cabecera).")
    ap.add_argument("tenants", nargs="+", help="test | acme | ...  (realm rrhh-tenant-<tenant>)")
    ap.add_argument("--apply", action="store_true", help="escribe los cambios; sin él solo los muestra")
    opts = ap.parse_args()
    kc = Admin()
    ok = all([settle(kc, realm_of(t), opts.apply) for t in opts.tenants])
    if not opts.apply:
        print("== dry-run: nada escrito; repite con --apply para aplicarlo")
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
