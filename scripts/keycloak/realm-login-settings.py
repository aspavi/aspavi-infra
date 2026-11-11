#!/usr/bin/env python3
"""Ajustes de login de un realm de Keycloak que YA existe: el email es el usuario
y la protección contra fuerza bruta.

Lo deja como lo crearía hoy apps/keycloak/realm-import-<tenant>.yaml, que el
operador no vuelve a aplicar (import --override=false):

  1. realm: registrationEmailAsUsername=true, loginWithEmailAllowed=true,
     editUsernameAllowed=false; fuerza bruta: bruteForceProtected=true,
     failureFactor=10, waitIncrementSeconds=900, maxFailureWaitSeconds=900,
     maxDeltaTimeSeconds=43200, permanentLockout=false
  2. user profile: el atributo email solo lo edita un admin
     (permissions.edit = ["admin"]); el resto del JSON se conserva tal cual
  3. con --rename-users: los usuarios con email cuyo username GUARDADO no es
     ese email (en minúsculas) pasan a tenerlo como username. El guardado se
     mira con la búsqueda exacta por email, no con el campo username de las
     respuestas, que con el ajuste activo ya trae el email aunque no lo sea.
     Se quedan como están los service-account-* y los usuarios sin email. Solo se renombra si los
     ajustes 1 y 2 han quedado aplicados: el renombrado es un PUT del usuario
     con su propia representación, y es Keycloak quien pone el email como
     username porque registrationEmailAsUsername está activo.

  4. con --drop-roles-claim: quita de rrhh-frontend el mapper que emite el
     claim plano `roles` (access, id y userinfo), que repite los roles de
     `realm_access.roles`. Solo si el client tiene por defecto el client scope
     `roles` y este emite `realm_access.roles` en el access token, que es el
     claim que leen el front y auth-lib.

Por defecto NO escribe: imprime, por realm, cada ajuste con su valor actual y
el que tendría, y cada renombrado como `username: viejo -> nuevo`. Con --apply
escribe solo lo que difiere, primero los ajustes y después los renombrados, y
vuelve a leer para comprobarlo. Idempotente.

Uso:
  python3 scripts/keycloak/realm-login-settings.py <tenant> [<tenant> ...] [--rename-users] [--drop-roles-claim] [--apply]

Credenciales: ver kcadmin.py (admin de master + Llavero, solo en memoria).
No lee ni imprime secretos. Termina con 1 si algo no ha quedado aplicado.
"""
import argparse
import json

from kcadmin import Admin, KcError, realm_of, say

REALM_SETTINGS = {
    "registrationEmailAsUsername": True,
    "loginWithEmailAllowed": True,
    "editUsernameAllowed": False,
    # protección contra fuerza bruta (también la usa el direct grant de iam
    # en first-password-change): el 10º fallo bloquea 15 min; después, cada
    # fallo vuelve a bloquear 15 min hasta un acierto o 12 h sin fallos
    "bruteForceProtected": True,
    "failureFactor": 10,
    "waitIncrementSeconds": 900,
    "maxFailureWaitSeconds": 900,
    "maxDeltaTimeSeconds": 43200,
    "permanentLockout": False,
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
    """Ajuste 3. Devuelve True si no ha fallado ningún renombrado.

    Qué username tiene guardado cada usuario NO se lee del campo username de
    la respuesta, que con el ajuste activo ya trae el email (ver
    Admin.stored_username_is): se mira si la búsqueda exacta por su email lo
    encuentra."""
    todo, ok = [], True
    for u in users(kc, realm):
        shown, email = u["username"], (u.get("email") or "").strip().lower()
        if shown.startswith("service-account-") or u.get("serviceAccountClientId"):
            continue
        if not email:
            say("se queda", f"{shown}: sin email")
        elif kc.stored_username_is(realm, u["id"], email):
            say("ok", f"username = email ({email})")
        else:
            # Keycloak 26.5 ya no deja ver el username guardado; 26.2 sí, en la lista.
            old = shown if shown != email else "(el guardado, que la API ya no muestra)"
            say("renombra", f"username: {old} -> {email}")
            todo.append((u["id"], old, email))
    if not apply:
        return True
    for uid, old, email in todo:
        try:
            kc.call("PUT", f"{realm}/users/{uid}", kc.get(f"{realm}/users/{uid}"), raise_errors=True)
        except KcError as e:
            # Keycloak puede dejar el username cambiado aunque el PUT falle: lo digo.
            done = kc.stored_username_is(realm, uid, email)
            say("ERROR", f"{email}: {e} (username guardado = email: {done})")
            ok = False
            continue
        if kc.stored_username_is(realm, uid, email):
            say("comprobado", f"{old} -> {email}")
        else:
            say("ERROR", f"{email}: tras el PUT, el username guardado sigue sin ser el email")
            ok = False
    return ok


FRONT_CLIENT = "rrhh-frontend"
ROLE_MAPPER = "oidc-usermodel-realm-role-mapper"
LEGACY_ROLES_CLAIM = "roles"
STANDARD_ROLES_CLAIM = "realm_access.roles"


def drop_roles_claim(kc, realm, apply):
    """Ajuste 4. Quita de rrhh-frontend los mappers de roles de realm que emiten
    el claim plano `roles`, que repite lo que ya va en `realm_access.roles`.
    Solo si el client tiene como scope POR DEFECTO `roles` y ese scope emite
    `realm_access.roles` en el access token (es lo que leen el front y
    auth-lib). Devuelve True si no ha fallado nada."""
    front = kc.client_uuid(realm, FRONT_CLIENT)
    if not front:
        say("ERROR", f"no existe el client {FRONT_CLIENT}")
        return False
    mappers = kc.get(f"{realm}/clients/{front}/protocol-mappers/models")
    legacy = [m for m in mappers if m.get("protocolMapper") == ROLE_MAPPER
              and (m.get("config") or {}).get("claim.name") == LEGACY_ROLES_CLAIM]
    others = [f"{m['name']} -> {(m.get('config') or {}).get('claim.name')}" for m in mappers
              if m.get("protocolMapper") == ROLE_MAPPER and m not in legacy]
    if others:
        say("info", f"otros mappers de roles en {FRONT_CLIENT} (no se tocan): {', '.join(others)}")
    if not legacy:
        say("ok", f"{FRONT_CLIENT} no emite el claim '{LEGACY_ROLES_CLAIM}'")
        return True

    keeper = standard_roles_mapper(kc, realm, front)
    if keeper is None:
        say("ERROR", f"no borro el claim '{LEGACY_ROLES_CLAIM}': {FRONT_CLIENT} no tiene el scope por defecto "
                     f"'roles' con un mapper {ROLE_MAPPER} que emita '{STANDARD_ROLES_CLAIM}' en el access token")
        return False
    say("info", f"se queda {STANDARD_ROLES_CLAIM} (client scope roles, mapper '{keeper}')")
    for m in legacy:
        say("borra", f"mapper '{m['name']}' de {FRONT_CLIENT} (claim '{LEGACY_ROLES_CLAIM}')")
    if not apply:
        return True
    for m in legacy:
        kc.call("DELETE", f"{realm}/clients/{front}/protocol-mappers/models/{m['id']}")
    left = [m for m in kc.get(f"{realm}/clients/{front}/protocol-mappers/models")
            if (m.get("config") or {}).get("claim.name") == LEGACY_ROLES_CLAIM]
    say("comprobado" if not left else "ERROR",
        f"{FRONT_CLIENT} ya no emite '{LEGACY_ROLES_CLAIM}'" if not left else f"quedan mappers con '{LEGACY_ROLES_CLAIM}'")
    return not left


def standard_roles_mapper(kc, realm, front):
    """Nombre del mapper que pone los roles de realm en realm_access.roles del
    access token a través del scope por defecto `roles` del client, o None."""
    if not any(sc["name"] == "roles" for sc in kc.get(f"{realm}/clients/{front}/default-client-scopes")):
        return None
    scope = next((sc for sc in kc.get(f"{realm}/client-scopes") if sc["name"] == "roles"), None)
    for m in (scope or {}).get("protocolMappers", []):
        cfg = m.get("config") or {}
        if (m.get("protocolMapper") == ROLE_MAPPER and cfg.get("claim.name") == STANDARD_ROLES_CLAIM
                and cfg.get("access.token.claim") == "true" and cfg.get("multivalued") == "true"):
            return m["name"]
    return None


def main():
    ap = argparse.ArgumentParser(description="Email como nombre de usuario en realms existentes (ver la cabecera).")
    ap.add_argument("tenants", nargs="+", help="test | acme | ...  (realm rrhh-tenant-<tenant>)")
    ap.add_argument("--rename-users", action="store_true",
                    help="renombra a su email a los usuarios cuyo username no lo es")
    ap.add_argument("--drop-roles-claim", action="store_true",
                    help=f"quita de {FRONT_CLIENT} el claim plano 'roles', duplicado de realm_access.roles")
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
        if opts.drop_roles_claim:
            ok &= drop_roles_claim(kc, realm, opts.apply)
    if not opts.apply:
        print("== dry-run: nada escrito; repite con --apply para aplicarlo")
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
