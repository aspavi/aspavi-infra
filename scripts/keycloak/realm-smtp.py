#!/usr/bin/env python3
"""Correo saliente (SMTP) de realms de Keycloak que YA existen (ASP-42).

No sabe de ningún proveedor: toda la configuración sale de smtp.json, junto a
este script (o del fichero que se pase con --config). Ese fichero es la única
fuente de verdad del SMTP; los realm-import no lo llevan. Para cambiar de
proveedor se edita el fichero, se guarda la clave nueva en el Llavero y se
vuelve a lanzar este script (ver README.md, "Cambiar de proveedor de correo").
El formato del fichero y de dónde sale la contraseña: ver smtpconfig.py. La
contraseña solo se lee con --apply.

Por defecto NO escribe: imprime, por realm, cada campo con su valor actual y el
que tendría; la contraseña sale siempre enmascarada. Con --apply hace un PUT del
realm que solo lleva `smtpServer` y vuelve a leer para comprobarlo. Keycloak
sustituye el smtpServer entero: un campo que esté en el realm y no en el
fichero se quita (el dry-run lo marca como "se quita").

Keycloak no devuelve la contraseña (la enmascara como **********), así que no
se puede comparar: si todo lo demás coincide, lo dice, y --apply la vuelve a
escribir igualmente. No envía ningún correo: para eso está smtp-test.py.

Uso:
  python3 scripts/keycloak/realm-smtp.py <tenant|realm> [...] [--config FICHERO] [--apply]

Credenciales de Keycloak: ver kcadmin.py (admin de master, port-forward).
Termina con 1 si algo no ha quedado aplicado.
"""
import argparse
import json

from kcadmin import Admin, KcError, realm_of, say
from smtpconfig import CONFIG, MASK, keychain_label, load_config, password_available, read_password, scrub


def show(key, value):
    if value is None:
        return "(no está)"
    return MASK if key == "password" else json.dumps(value, ensure_ascii=False)


def diff(realm_rep, want, keychain):
    """Imprime campo a campo. Devuelve True si coincide todo salvo la contraseña."""
    have = realm_rep.get("smtpServer") or {}
    same = True
    for key in sorted(set(want) | (set(have) - {"password"})):
        if key not in want:
            say("se quita", f"{key}: {show(key, have[key])}")
            same = False
        elif have.get(key) == want[key]:
            say("ok", f"{key} = {show(key, want[key])}")
        else:
            say("cambia", f"{key}: {show(key, have.get(key))} -> {show(key, want[key])}")
            same = False
    if keychain:
        now = "puesta (Keycloak no la devuelve)" if have.get("password") else "(no está)"
        say("contraseña", f"{now} -> la del {keychain_label(keychain)}; con --apply se escribe siempre")
        same &= bool(have.get("password"))
    elif "password" in have:
        say("se quita", f"password: {MASK} (auth no es true)")
        same = False
    return same


def apply(kc, realm, want, password):
    body = dict(want)
    if password:
        body["password"] = password
    try:
        kc.call("PUT", realm, {"smtpServer": body}, raise_errors=True)
    except KcError as e:
        say("ERROR", scrub(str(e), password))
        return False
    have = kc.get(realm).get("smtpServer") or {}
    wrong = [k for k in set(want) | (set(have) - {"password"}) if have.get(k) != want.get(k)]
    if password and have.get("password") != MASK:
        wrong.append("password")
    if not password and "password" in have:
        wrong.append("password (sobra)")
    say("comprobado" if not wrong else "ERROR",
        "smtpServer aplicado" if not wrong else f"no ha quedado: {', '.join(sorted(wrong))}")
    return not wrong


def main():
    ap = argparse.ArgumentParser(description="SMTP de realms existentes desde smtp.json (ver la cabecera).")
    ap.add_argument("tenants", nargs="+", help="test | acme | ...  (realm rrhh-tenant-<tenant>), o un realm completo")
    ap.add_argument("--config", default=CONFIG, help=f"fichero de configuración (por defecto {CONFIG.name})")
    ap.add_argument("--apply", action="store_true", help="escribe los cambios; sin él solo los muestra")
    opts = ap.parse_args()
    want, keychain = load_config(opts.config)
    if keychain and not opts.apply and not password_available(keychain):
        say("AVISO", f"no está la contraseña en el {keychain_label(keychain)}: --apply fallaría")
    password = read_password(keychain) if keychain and opts.apply else None
    kc = Admin()
    ok = True
    for realm in map(realm_of, opts.tenants):
        print(f"== {realm}{'' if opts.apply else ' (dry-run)'}")
        rep = kc.get(realm, ok404=True)
        if rep is None:
            say("ERROR", "el realm no existe")
            ok = False
            continue
        if diff(rep, want, keychain):
            say("info", "coincide todo salvo la contraseña, que no se puede comparar"
                        + ("; se reaplica" if opts.apply else ""))
        if opts.apply:
            ok &= apply(kc, realm, want, password)
    if not opts.apply:
        print("== dry-run: nada escrito; repite con --apply para aplicarlo")
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
