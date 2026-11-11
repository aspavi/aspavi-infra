"""Cliente mínimo de la Admin REST API de Keycloak para los scripts de scripts/keycloak/.

Entra como el usuario `admin` del realm master (client admin-cli, grant password).
La administración de Keycloak no se publica (ASP-5): se llega por port-forward,

    kubectl --kubeconfig ~/.kube/aspavi.kubeconfig -n rrhh-auth port-forward svc/keycloak-service 18080:8080

y esta es la URL por defecto (http://localhost:18080). La contraseña sale de la variable KEYCLOAK_ADMIN_PASSWORD
o, si no está, del Llavero de macOS (cuenta `aspavi`, servicio
KEYCLOAK_ADMIN_PASSWORD). Vive solo en memoria y no se imprime nunca.

Variables opcionales: KEYCLOAK_URL (http://localhost:18080) y
KEYCLOAK_ADMIN_USER (admin). Solo usa la biblioteca estándar.
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

KC = os.environ.get("KEYCLOAK_URL", "http://localhost:18080").rstrip("/")
PORT_FORWARD = "kubectl --kubeconfig ~/.kube/aspavi.kubeconfig -n rrhh-auth port-forward svc/keycloak-service 18080:8080"
ADMIN_USER = os.environ.get("KEYCLOAK_ADMIN_USER", "admin")
BASE_ROLES = ("ADMIN_RRHH", "MANAGER", "EMPLOYEE", "AUDITOR")


class KcError(Exception):
    """Error HTTP de la Admin API, para quien quiera seguir en vez de salir."""


def die(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def say(tag, msg):
    print(f"  {tag:<12} {msg}")


def realm_of(name):
    """`test` -> `rrhh-tenant-test`; un nombre de realm completo se deja tal cual."""
    return name if name.startswith("rrhh-tenant-") or name == "master" else f"rrhh-tenant-{name}"


def q(value):
    return urllib.parse.quote(value, safe="")


class Admin:
    """Renueva el token antes de que caduque (el de admin-cli en master dura 60 s
    por defecto). Si una llamada da 401, vuelve a entrar una vez y la repite."""

    def __init__(self):
        self._password = self._read_password()
        self._token = None
        self._expires = 0.0

    @staticmethod
    def _read_password():
        pwd = os.environ.get("KEYCLOAK_ADMIN_PASSWORD")
        if pwd:
            return pwd
        try:
            return subprocess.run(
                ["security", "find-generic-password", "-a", "aspavi", "-s", "KEYCLOAK_ADMIN_PASSWORD", "-w"],
                capture_output=True, text=True, check=True).stdout.strip()
        except (OSError, subprocess.CalledProcessError):
            die("no encuentro la contraseña: ni KEYCLOAK_ADMIN_PASSWORD ni el Llavero (aspavi / KEYCLOAK_ADMIN_PASSWORD)")

    def _login(self):
        data = urllib.parse.urlencode({"grant_type": "password", "client_id": "admin-cli",
                                       "username": ADMIN_USER, "password": self._password}).encode()
        try:
            with urllib.request.urlopen(f"{KC}/realms/master/protocol/openid-connect/token", data) as r:
                tok = json.load(r)
        except urllib.error.HTTPError as e:
            die(f"login de {ADMIN_USER} en master: HTTP {e.code} {e.read()[:200].decode(errors='replace')}")
        except urllib.error.URLError as e:
            hint = f"\n¿Está abierto el port-forward?  {PORT_FORWARD}" if "localhost" in KC or "127.0.0.1" in KC else ""
            die(f"no llego a {KC}: {e.reason}{hint}")
        self._token = tok["access_token"]
        self._expires = time.monotonic() + max(int(tok.get("expires_in", 60)) - 15, 5)

    def call(self, method, path, body=None, ok404=False, _retry=True, raise_errors=False):
        """`path` va relativo a /admin/realms/ ('' lista los realms). Un error
        HTTP termina el script, o lanza KcError si raise_errors=True."""
        if self._token is None or time.monotonic() >= self._expires:
            self._login()
        url = f"{KC}/admin/realms/{path}" if path else f"{KC}/admin/realms"
        req = urllib.request.Request(
            url, method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as r:
                raw = r.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            if e.code == 401 and _retry:
                self._token = None
                return self.call(method, path, body, ok404, _retry=False, raise_errors=raise_errors)
            if ok404 and e.code == 404:
                return None
            msg = f"{method} /admin/realms/{path}: HTTP {e.code} {e.read()[:300].decode(errors='replace')}"
            if raise_errors:
                raise KcError(msg) from None
            die(msg)

    def get(self, path, ok404=False):
        return self.call("GET", path, ok404=ok404)

    def stored_username_is(self, realm, user_id, name):
        """True si el username GUARDADO del usuario es `name`.

        Con registrationEmailAsUsername activo, Keycloak devuelve ya el email
        en el campo username de todas sus respuestas (GET users/{id} en 26.2;
        también la lista y las búsquedas en 26.5), aunque el guardado, con el
        que se entra, siga siendo el anterior. El filtro de búsqueda sí compara
        con el valor guardado: por eso se mira si el id aparece en la búsqueda
        exacta, sin leer el campo username de la respuesta."""
        found = self.get(f"{realm}/users?username={q(name)}&exact=true&briefRepresentation=true")
        return any(u["id"] == user_id for u in found)

    def client_uuid(self, realm, client_id):
        found = self.get(f"{realm}/clients?clientId={q(client_id)}")
        return found[0]["id"] if found else None
