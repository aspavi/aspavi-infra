# scripts/keycloak

Scripts de administración de Keycloak (Admin REST API) para los realms que ya
existen. Los imports de `apps/keycloak/realm-import-*.yaml` solo se aplican al
crear un realm (`--override=false`); estos scripts llevan los mismos cambios a
los realms creados antes.

| Script | Qué hace |
|---|---|
| `inventory.py [<tenant> ...]` | Solo lectura: roles, composites, clients y usuarios (email, `guardado_es_email`, `employee_id`, roles) |
| `bootstrap-iam-realm.py <tenant> [--dry-run] [--production]` | Client `rrhh-iam-service`, mapper `employee_id` y su secret en `rrhh-system/keycloak-client-secrets` |
| `realm-login-settings.py <tenant\|master> ... [--apply]` | Email como usuario, fuerza bruta, idioma (`es` por defecto), `--rename-users`, `--drop-roles-claim`; en `master`, solo fuerza bruta y `frontendUrl` |
| `realm-smtp.py <tenant\|realm> ... [--apply]` | Correo saliente del realm (`smtpServer`) desde `smtp.json` |
| `smtp-test.py --to <email>` | Envía un correo de prueba con `smtp.json`, sin pasar por Keycloak |
| `smtp.json` | Configuración del correo saliente: la única fuente de verdad |
| `kcadmin.py`, `smtpconfig.py` | Código compartido; no se ejecutan solos |

Todos son dry-run por defecto, salvo `inventory.py`, que solo lee, y
`smtp-test.py`, que solo envía un correo.

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

## Correo saliente (SMTP)

Keycloak envía los correos de los realms (restablecer la contraseña, acciones
pendientes, verificar el email) con el `smtpServer` de cada realm.

`smtp.json` es la **única fuente de verdad** de esa configuración: servidor,
puerto, `starttls`/`ssl`, `auth`, usuario, remitente (`from`,
`fromDisplayName`) y, si hace falta, `replyTo`. Usa los mismos nombres de campo
que Keycloak. La contraseña no va en el fichero: `passwordKeychain` dice en qué
entrada del Llavero de macOS está (`-a <account> -s <service>`). Si existe una
variable de entorno con el nombre del `service`, se usa esa en su lugar.

Ningún script, import ni documento repite estos valores; todos leen o citan
`smtp.json`. Los scripts no conocen ningún proveedor.

```bash
python3 scripts/keycloak/realm-smtp.py test acme                      # dry-run: actual -> nuevo, con la contraseña enmascarada
python3 scripts/keycloak/realm-smtp.py test --apply
python3 scripts/keycloak/smtp-test.py --to someone@example.com        # correo de prueba, sin Keycloak
```

- **`realm-smtp.py`** hace un PUT del realm que solo lleva `smtpServer` y
  después vuelve a leerlo para comprobarlo. Keycloak sustituye el
  `smtpServer` entero, así que se quita cualquier campo que esté en el realm y
  no en el fichero; el dry-run lo marca como `se quita`. Keycloak no devuelve
  la contraseña (la enmascara como `**********`), así que no se puede
  comparar. Si todo lo demás coincide, el script lo dice, y `--apply` vuelve
  a escribirla igualmente. Solo lee la contraseña con `--apply`. Necesita el
  port-forward.
- **`smtp-test.py`** habla directamente con el servidor SMTP (smtplib) y
  verifica su certificado. Envía un correo con el asunto "ASPAVI - prueba de
  envío" y la fecha. Imprime la respuesta del servidor (`aceptado 250 …` o el
  error). Que el servidor acepte el correo no garantiza que llegue: la entrega
  y el resultado de SPF/DKIM/DMARC se comprueban en las cabeceras del correo
  recibido. El destinatario se pasa solo por argumento. No se usa el
  `testSMTPConnection` de Keycloak porque envía al email del admin conectado,
  y el admin de master no tiene.

Los `realm-import` **no** llevan `smtpServer`, para no duplicar los valores de
`smtp.json`. Un realm nuevo nace sin correo saliente. Hay que lanzar
`realm-smtp.py <tenant> --apply` después de crearlo (paso 1 de
`docs/adding-a-new-tenant.md`). Editar un `KeycloakRealmImport` no vuelve a
importar el realm con el operador 26.5.5: los Jobs de import de test y acme
son del día en que se crearon los realms, aunque los CR se han editado
después. Aun así, el SMTP se queda fuera de los imports por la fuente única.

### Cambiar de proveedor de correo

Solo cambia la configuración, no el código:

1. **DNS** (Hetzner): publica lo que pida el proveedor nuevo para el dominio
   del remitente: verificación, DKIM e `include:` en el SPF. Comprueba con
   `dig` que todo resuelve. Revisa el DMARC. **No** retires aún los registros
   del proveedor anterior.
2. **Clave**: guarda la clave SMTP nueva en el Llavero, en una entrada nueva:
   `security add-generic-password -a aspavi -s <SERVICE> -w`.
3. **Fichero**: edita `smtp.json`: `smtpServer` con los datos nuevos y
   `passwordKeychain.service` con esa entrada. Hazlo en una PR.
4. **Test**: `smtp-test.py --to someone@example.com` y después
   `realm-smtp.py test --apply`. Prueba un correo real del realm (por ejemplo,
   "¿Has olvidado tu contraseña?" en test.aspavi.com) y mira en las cabeceras
   que pasan SPF, DKIM y DMARC.
5. **Acme y limpieza**: `realm-smtp.py acme --apply`. Cuando ya no salga
   correo por el proveedor anterior, retira del DNS sus registros (DKIM,
   `include:` del SPF, verificación) y borra su entrada del Llavero.

### Si un servicio envía correo

Hoy solo envía correo Keycloak. Si algún servicio Java llega a enviar correo
(Spring Mail), la dirección prevista es que lea los mismos valores desde un
Secret/ConfigMap común `smtp` del clúster, generado a partir de `smtp.json`, y
no una copia propia. Así, cambiar de proveedor seguiría siendo editar
`smtp.json` y la clave. No está implementado.
