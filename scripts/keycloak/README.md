# scripts/keycloak

Scripts de administración de Keycloak (Admin REST API) para los realms que ya
existen. Los imports de `apps/keycloak/realm-import-*.yaml` solo se aplican al
crear un realm (`--override=false`); estos scripts llevan los mismos cambios a
los realms creados antes.

| Script | Qué hace |
|---|---|
| `inventory.py [<tenant> ...]` | Solo lectura: roles, composites, clients y usuarios (email, `guardado_es_email`, `employee_id`, roles) |
| `bootstrap-iam-realm.py <tenant> [--dry-run] [--production]` | Client `rrhh-iam-service`, mapper `employee_id` y su secret en `rrhh-system/keycloak-client-secrets` |
| `realm-login-settings.py <tenant\|master> ... [--apply]` | Email como usuario, fuerza bruta, `--rename-users`, `--drop-roles-claim`; en `master`, solo fuerza bruta y `frontendUrl` |
| `kcadmin.py` | Login y llamadas compartidas; no se ejecuta solo |

Todos son dry-run por defecto, salvo `inventory.py`, que solo lee.

## Cómo se llega a Keycloak: port-forward

La administración no se publica (ASP-5). `https://keycloak.aspavi.com` solo
sirve `/realms/rrhh-tenant-*` y `/resources`; `/admin` y `/realms/master` dan
404. Para administrar, abre un port-forward en otra terminal y déjalo abierto
mientras trabajas:

```bash
kubectl --kubeconfig ~/.kube/aspavi.kubeconfig -n rrhh-auth port-forward svc/keycloak-service 18080:8080
```

El puerto local es el 18080 porque el 8180 lo usa el Keycloak del entorno local
(aspavi-local). Tiene que ser **exactamente** `http://localhost:18080`: es el
`spec.hostname.admin` del CR y el `frontendUrl` del realm master. Con otro
puerto local, la consola redirige a `localhost:18080`, donde no hay nada, y
con `http://127.0.0.1:18080/admin/` da 404 (solo atiende en `localhost`). Los
scripts sí funcionan con `127.0.0.1`.

- **Consola:** <http://localhost:18080/admin/>, con el usuario `admin` del
  realm master.
- **Scripts:** usan `http://localhost:18080` por defecto. Si el port-forward no
  está abierto, fallan con "¿Está abierto el port-forward?" y el comando.

```bash
python3 scripts/keycloak/inventory.py test acme
python3 scripts/keycloak/realm-login-settings.py test acme            # dry-run
```

### Credenciales

`kcadmin.py` entra como `admin` del realm master (client `admin-cli`, grant
password). La contraseña sale de `KEYCLOAK_ADMIN_PASSWORD` o, si no está, del
Llavero de macOS (`security find-generic-password -a aspavi -s KEYCLOAK_ADMIN_PASSWORD -w`).
Vive solo en memoria y no se imprime.

Variables opcionales: `KEYCLOAK_URL` (por defecto `http://localhost:18080`) y
`KEYCLOAK_ADMIN_USER` (por defecto `admin`).

El realm master tiene protección contra fuerza bruta: 10 fallos seguidos
bloquean al admin 15 minutos. Para desbloquearlo hace falta otro admin de
master (`DELETE /admin/realms/master/attack-detection/brute-force/users/<id>`)
o esperar.

## El realm master

Master no está en los imports. Se ajusta con:

```bash
python3 scripts/keycloak/realm-login-settings.py master            # dry-run
python3 scripts/keycloak/realm-login-settings.py master --apply
```

Aplica la misma fuerza bruta que en los tenants (`failureFactor` 10,
`waitIncrementSeconds` 900, `maxFailureWaitSeconds` 900, `maxDeltaTimeSeconds`
43200, sin bloqueo permanente). También fija el atributo
`frontendUrl=http://localhost:18080` (`--admin-url` para otro valor).

Sin ese `frontendUrl`, la consola carga por el port-forward, pero su login va
al hostname público (`https://keycloak.aspavi.com/realms/master/...`), que ya
no lo sirve. Con él, el login y el `iss` de los tokens de master usan
`http://localhost:18080`. No afecta a los tenants: su `iss` sigue siendo
`https://keycloak.aspavi.com/realms/rrhh-tenant-<tenant>`.

El script conserva el resto de atributos de master: Keycloak sustituye el mapa
`attributes` entero en cada PUT, así que los manda todos.

Para deshacerlo: en la consola, *Realm settings* de master → *Frontend URL*
vacío.

## URLs de Keycloak por quién las usa

| Quién | URL |
|---|---|
| Navegador, front y JWKS de los servicios | `https://keycloak.aspavi.com/realms/rrhh-tenant-*` |
| iam-service (Admin API, client_credentials, direct grant, logout) | `http://keycloak-service.rrhh-auth.svc.cluster.local:8080` |
| Personas y scripts de administración | `http://localhost:18080` (port-forward) |

El `iss` de los tokens de los tenants es siempre
`https://keycloak.aspavi.com/realms/<realm>`, se entre por donde se entre:
`spec.hostname.hostname` es una URL completa.
