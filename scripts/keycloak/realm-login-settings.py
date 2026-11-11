#!/usr/bin/env python3
"""Ajustes de login de un realm de Keycloak que YA existe: el email es el usuario.

Lo deja como lo crearía hoy apps/keycloak/realm-import-<tenant>.yaml, que el
operador no vuelve a aplicar (import --override=false):

  1. realm: registrationEmailAsUsername=true, loginWithEmailAllowed=true,
     editUsernameAllowed=false
  2. user profile: el atributo email solo lo edita un admin
     (permissions.edit = ["admin"]); el resto del JSON se conserva tal cual
  3. con --rename-users: los usuarios con email cuyo username no es ese email
     (en minúsculas) pasan a tenerlo como username. Se quedan como están los
     service-account-* y los usuarios sin email. Solo se renombra si los
     ajustes 1 y 2 han quedado aplicados: el renombrado es un PUT del usuario
     con su propia representación, y es Keycloak quien pone el email como
     username porque registrationEmailAsUsername está activo.

Por defecto NO escribe: imprime, por realm, cada ajuste con su valor actual y
el que tendría, y cada renombrado como `username: viejo -> nuevo`. Con --apply
escribe solo lo que difiere, primero los ajustes y después los renombrados, y
vuelve a leer para comprobarlo. Idempotente.

Uso:
  python3 scripts/keycloak/realm-login-settings.py <tenant> [<tenant> ...] [--rename-users] [--apply]

Credenciales: ver kcadmin.py (admin de master + Llavero, solo en memoria).
No lee ni imprime secretos. Termina con 1 si algo no ha quedado aplicado.
"""
import argparse
import json

from kcadmin import Admin, KcError, q, realm_of, say

REALM_SETTINGS = {
    "registrationEmailAsUsername": True,
    "loginWithEmailAllowed": True,
    "editUsernameAllowed": False,
}
EMAIL_EDIT = ["admin"]
PAGE = 100


def show(value):
    return json.dumps(value, ensure_ascii=False)


def settle(kc, realm, apply):
    """Ajustes 1 y 2. Devuelve True si el realm queda (o, en dry-run, quedaría) bien."""
    rep = kc.get(realm)

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
    say("comprobado" if not wrong else "ERROR", "ajustes aplicados" if not wrong else f"no ha quedado: {', '.join(wrong)}")
    return not wrong


def users(kc, realm):
    first = 0
    while True:
        page = kc.get(f"{realm}/users?first={first}&max={PAGE}&briefRepresentation=true")
        yield from page
        if len(page) < PAGE:
            return
        first += PAGE


def rename_users(kc, realm, apply):
    """Ajuste 3. Devuelve True si no ha fallado ningún renombrado."""
    todo, ok = [], True
    for u in users(kc, realm):
        name, email = u["username"], (u.get("email") or "").strip().lower()
        if name.startswith("service-account-"):
            continue
        if not email:
            say("se queda", f"{name}: sin email")
        elif name == email:
            say("ok", f"username = email ({name})")
        else:
            say("renombra", f"username: {name} -> {email}")
            todo.append((u["id"], name, email))
    if not apply:
        return True
    for uid, name, email in todo:
        try:
            kc.call("PUT", f"{realm}/users/{uid}", kc.get(f"{realm}/users/{uid}"), raise_errors=True)
        except KcError as e:
            # Keycloak puede dejar el username cambiado aunque el PUT falle: lo digo.
            say("ERROR", f"{name}: {e} (username guardado ahora: {stored_username(kc, realm, uid, name, email)})")
            ok = False
            continue
        now = stored_username(kc, realm, uid, name, email)
        if now == email:
            say("comprobado", f"{name} -> {now}")
        else:
            say("ERROR", f"{name}: el username guardado sigue siendo {now}")
            ok = False
    return ok


def stored_username(kc, realm, uid, old, new):
    """El username que Keycloak tiene guardado. No vale GET users/{id}: con
    registrationEmailAsUsername activo devuelve ya el email como username aunque
    el guardado (con el que se entra) siga siendo el viejo. La búsqueda exacta
    sí lee el valor guardado."""
    for name in (new, old):
        if any(u["id"] == uid for u in kc.get(f"{realm}/users?username={q(name)}&exact=true")):
            return name
    return "?"


def main():
    ap = argparse.ArgumentParser(description="Email como nombre de usuario en realms existentes (ver la cabecera).")
    ap.add_argument("tenants", nargs="+", help="test | acme | ...  (realm rrhh-tenant-<tenant>)")
    ap.add_argument("--rename-users", action="store_true",
                    help="renombra a su email a los usuarios cuyo username no lo es")
    ap.add_argument("--apply", action="store_true", help="escribe los cambios; sin él solo los muestra")
    opts = ap.parse_args()
    kc = Admin()
    ok = True
    for realm in map(realm_of, opts.tenants):
        print(f"== {realm}{'' if opts.apply else ' (dry-run)'}")
        if kc.get(realm, ok404=True) is None:
            say("ERROR", "el realm no existe")
            ok = False
            continue
        settled = settle(kc, realm, opts.apply)
        ok &= settled
        if opts.rename_users:
            if settled:
                ok &= rename_users(kc, realm, opts.apply)
            else:
                say("ERROR", "los ajustes no han quedado aplicados; no renombro a nadie")
    if not opts.apply:
        print("== dry-run: nada escrito; repite con --apply para aplicarlo")
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
