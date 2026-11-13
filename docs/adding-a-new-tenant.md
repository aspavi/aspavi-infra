# Añadir un nuevo tenant

Desde ASP-21, dar de alta un tenant **no toca ni reinicia ningún servicio** y el
front no se recompila. Hacen falta tres cosas:

1. `scripts/tenant/onboard.py`: el realm de Keycloak, su correo, el client de
   iam y el primer admin de RRHH;
2. el registro DNS;
3. una PR a este repo que solo añade el host al Ingress del front.

En el ejemplo, el tenant es `globex` y su dominio `globex.aspavi.com`.

> Esto funciona cuando está desplegado todo ASP-21: los servicios con auth-lib
> 1.2.0 y `aspavi.auth.issuer-pattern`, iam leyendo los secrets en caliente,
> absence con los tipos de ausencia por defecto y el front sacando el tenant
> del host (contrato: `aspavi-board/docs/ASP-21-contrato.md`).

---

## Requisitos

- El id del tenant: `^[a-z][a-z0-9-]{1,29}$` (minúsculas, cifras y guiones;
  empieza por letra; de 2 a 30 caracteres). Es el subdominio, el sufijo del
  realm (`rrhh-tenant-<id>`) y el `tenant_id` de los datos. No se puede
  cambiar después. No valen los que ya son otra cosa en `aspavi.com` (`api`,
  `app`, `keycloak`, `argocd`, `tapete`, `www`, `mail`, `admin`, `master`,
  `aspavi`).
- El port-forward a Keycloak abierto (ver `scripts/keycloak/README.md`):
  ```bash
  kubectl --kubeconfig ~/.kube/aspavi.kubeconfig -n rrhh-auth port-forward svc/keycloak-service 18080:8080
  ```
- La contraseña del admin de master en el Llavero (`aspavi` /
  `KEYCLOAK_ADMIN_PASSWORD`) y la del SMTP (`aspavi` / `SMTP_BREVO_KEY`); ver
  `scripts/keycloak/README.md`.
- `kubectl` con `~/.kube/aspavi.kubeconfig` (o el de `--kubeconfig`).
- Acceso al DNS de `aspavi.com` (Hetzner DNS) y push a este repo.

---

## Paso 1 — `onboard.py`

Primero en seco, para ver qué haría:

```bash
python3 scripts/tenant/onboard.py globex --name "Globex S.A." --admin-email rrhh@globex.example --dry-run
```

Después, de verdad:

```bash
python3 scripts/tenant/onboard.py globex --name "Globex S.A." --admin-email rrhh@globex.example
```

Es idempotente: si algo falla a medias, se arregla la causa y se vuelve a
lanzar, y cada paso solo hace lo que falta. Al empezar imprime contra qué
Keycloak y qué clúster trabaja. Hace esto:

1. **Realm** `rrhh-tenant-globex`, desde `scripts/tenant/realm-template.json`
   y por la Admin API (no con un `KeycloakRealmImport`). Si ya existe, no lo
   toca.
2. **Ajustes** con los scripts de `scripts/keycloak/`:
   - `realm-login-settings.py`: email como usuario, fuerza bruta e idioma;
   - `realm-smtp.py`: el correo saliente, desde `smtp.json`;
   - `bootstrap-iam-realm.py`: comprueba el client `rrhh-iam-service` y guarda
     su secret en `rrhh-system/keycloak-client-secrets[iam-service-secret-globex]`.
     iam lo lee en caliente del volumen (Kubernetes lo actualiza en un minuto
     más o menos), descubre el tenant y le carga el catálogo de permisos y
     roles. No hay que reiniciar iam.

   `onboard.py` **no** siembra los permisos (`<recurso>:read|write`) ni los
   roles compuestos: lo hace iam (`CatalogBootstrap`). Busca tenants nuevos
   cada `aspavi.iam.bootstrap.scan-interval` (5 min), así que el catálogo
   llega como mucho unos 5 min después de que el secret aparezca en el
   volumen, o antes si alguien usa el tenant en iam (el primer uso lo carga
   antes de seguir). Si falla, lo reintenta cada 30 s y el componente
   `catalog` del health de iam lo indica. Se comprueba en el paso 4.
3. **Admin de RRHH**: el usuario `rrhh@globex.example` (el email es el
   usuario) con el rol `ADMIN_RRHH`, una **contraseña temporal** y la acción
   `UPDATE_PASSWORD`:
   - la contraseña se guarda en el Secret `rrhh-auth/tenant-globex-initial-admin`
     (claves `username` y `password`) antes de crear el usuario, y **no se
     imprime nunca**;
   - **no se envía ningún correo**;
   - si el usuario ya existe, no se toca su contraseña.

   Opcionales: `--admin-first-name` y `--admin-last-name`. Si faltan, Keycloak
   se los pide en el primer login.
4. **Lo que falta fuera**: imprime los registros DNS y el bloque del Ingress
   del front de los pasos 2 y 3. Si ya están, dice `ok`.

---

## Paso 2 — DNS

En Hetzner DNS, zona `aspavi.com`, los mismos registros A que `test` (uno por
nodo). `onboard.py` los imprime, por ejemplo:

```
globex  A  116.203.154.181
globex  A  46.225.141.208
globex  A  91.98.195.135
```

No hay comodín `*.aspavi.com`; quitar este paso necesita un certificado
comodín con DNS01 (tarea aparte).

---

## Paso 3 — Ingress del front

Una PR a `apps/rrhh-frontend/deployment.yaml` con una **entrada de `tls`
propia** para el host (secreto `rrhh-frontend-<id>-tls`) y su regla en
`spec.rules`, que es lo que imprime `onboard.py`:

```yaml
  tls:
    - hosts:
        - test.aspavi.com
        - acme.aspavi.com
      secretName: rrhh-frontend-tls
    - hosts:                         # ← añadir
        - globex.aspavi.com
      secretName: rrhh-frontend-globex-tls
  rules:
    # ...
    - host: globex.aspavi.com        # ← añadir
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: rrhh-frontend
                port:
                  number: 80
```

cert-manager emite `rrhh-frontend-globex-tls` solo para el host nuevo, lo que
necesita que el DNS del paso 2 resuelva (reto HTTP-01). Si aún no resuelve,
falla solo ese certificado y cert-manager lo reintenta; cuando el DNS exista,
lo emite.

**No metas el host en la entrada de test y acme.** Ese certificado pediría
todos los nombres a la vez: mientras el host nuevo no resuelva, fallaría
también la renovación de `rrhh-frontend-tls`, y test y acme se quedarían sin
certificado al caducar.

Nada más en infra:

- el CORS (`apps/employee-service/cors-middleware.yaml`) acepta cualquier
  `https://<id>.aspavi.com`;
- el Ingress de Keycloak (`apps/keycloak/ingress.yaml`) publica cualquier
  `/realms/rrhh-tenant-<id>`;
- los servicios aceptan cualquier realm `rrhh-tenant-<id>` por
  `aspavi.auth.issuer-pattern`.

---

## Paso 4 — Comprobar

1. Abre `https://globex.aspavi.com` y entra con el admin. La contraseña
   temporal la lee quien tenga acceso al clúster:
   ```bash
   kubectl -n rrhh-auth get secret tenant-globex-initial-admin -o jsonpath='{.data.password}' | base64 -d
   ```
   Keycloak pide una contraseña nueva en el primer login. A partir de ahí la
   temporal ya no vale y el Secret se puede borrar:
   ```bash
   kubectl -n rrhh-auth delete secret tenant-globex-initial-admin
   ```
2. El admin ve los menús de `ADMIN_RRHH`, y en ausencias aparecen los tipos por
   defecto (los crea absence en el primer uso).
3. globex no ve nada de los demás tenants, ni al revés.
4. iam ha cargado el catálogo (como mucho unos 5 min tras el paso 1; ver
   arriba). En solo lectura:
   ```bash
   python3 scripts/keycloak/inventory.py globex
   ```
   Tienen que salir los roles con forma `recurso:acción` y los 4 roles base
   como compuestos con sus hijos. Si no, mira los logs de iam y el
   componente `catalog` de su health.

---

## Qué deja configurado el realm

Todo sale de `realm-template.json`, que tiene la forma de
`apps/keycloak/realm-import-acme.yaml`. Si cambias uno, cambia el otro y
comprueba que coinciden:

```bash
python3 scripts/tenant/check-template.py      # necesita yq
```

La plantilla no lleva el client `rrhh-employee-service` del import de acme: no
lo usa nada en el clúster.

Contiene:

- los roles de realm `ADMIN_RRHH`, `MANAGER`, `EMPLOYEE` y `AUDITOR`. Los
  permisos (`<recurso>:read|write`) y los roles compuestos **no** van en la
  plantilla: los siembra iam;
- el client público `rrhh-frontend` (PKCE S256), con `redirectUris` y
  `webOrigins` en `https://globex.aspavi.com`, y los mappers `tenant_id`
  (fijo) y `employee_id`;
- el atributo `employee_id` en el user profile: solo lo edita un admin y el
  usuario lo ve;
- el client confidencial `rrhh-iam-service`, con service account y los roles de
  `realm-management` `view-realm`, `manage-realm`, `view-users`, `query-users`
  y `manage-users`. No le des ninguno más.

### Email como usuario

`registrationEmailAsUsername: true`, con `loginWithEmailAllowed: true` y
`editUsernameAllowed: false`. El email solo lo cambia un admin (en el user
profile, `email.permissions.edit = ["admin"]`); iam da de alta los usuarios
con username = email.

> Con ese ajuste, Keycloak cambia el username de un usuario existente por su
> email **en cuanto se actualiza el usuario por cualquier vía**. Desde ese
> momento entra con el email, y `preferred_username` pasa a ser el email.
> Además, a un usuario sin email no se le puede actualizar nada (`400
> error-user-attribute-required`, `field: email`).
>
> Con el ajuste activo, el campo `username` de las respuestas de la Admin API
> ya trae el email aunque el guardado siga siendo otro. Para saber con qué
> username entra un usuario, mira `guardado_es_email` en `inventory.py`. En un
> realm antiguo, `realm-login-settings.py <id> --rename-users --apply` los
> renombra de una vez.

### Fuerza bruta

`bruteForceProtected: true`, `failureFactor: 10`, `waitIncrementSeconds: 900`,
`maxFailureWaitSeconds: 900`, `maxDeltaTimeSeconds: 43200` y
`permanentLockout: false`. Comprobado en Keycloak 26.5.5:

- El 10º fallo seguido bloquea **15 min**. Los intentos durante el bloqueo no
  cuentan ni lo alargan.
- Después, cada fallo vuelve a bloquear 15 min, hasta un acierto o 12 h sin
  fallos.
- Dos fallos en menos de 1 s bloquean 60 s.
- Durante el bloqueo, Keycloak responde lo mismo que con una contraseña mala
  (`401 invalid_grant`). El estado real está en
  `GET /admin/realms/{realm}/attack-detection/brute-force/users/{id}`. Un
  `DELETE` sobre esa URL desbloquea al usuario.

### Idioma (ASP-41)

`internationalizationEnabled: true`, `supportedLocales: [es, en, pt]` y
`defaultLocale: es`:

- **Correos:** en el `locale` del usuario y, si no tiene, en español.
- **Páginas:** en el `locale` del usuario; si no tiene, en el idioma del
  navegador si es `es`, `en` o `pt`; si no, en español.

### Roles en el token

Los roles van en `realm_access.roles` del access token (client scope `roles`),
que es lo que leen el front y auth-lib. No hay claim plano `roles`.

### Correo saliente

Lo pone `realm-smtp.py` desde `scripts/keycloak/smtp.json`; ni la plantilla ni
los imports llevan `smtpServer`. Ver "Correo saliente (SMTP)" en
`scripts/keycloak/README.md`.

---

## test y acme

Siguen creados por sus `KeycloakRealmImport` (`apps/keycloak/realm-import-*.yaml`),
que el operador solo aplica al crear el realm. Los cambios en realms que ya
existen se llevan con los scripts de `scripts/keycloak/`, todos en seco por
defecto:

```bash
python3 scripts/keycloak/inventory.py <id>                   # solo lectura
python3 scripts/keycloak/realm-login-settings.py <id>         # --apply para escribir
python3 scripts/keycloak/realm-smtp.py <id>                   # --apply para escribir
python3 scripts/keycloak/bootstrap-iam-realm.py <id> --dry-run
```

---

## Resumen

| Qué | Dónde | Quién |
|---|---|---|
| `scripts/tenant/onboard.py <id> --name ... --admin-email ...` | Keycloak y clúster | quien tenga el port-forward y el Llavero |
| Registros A `<id>` | Hetzner DNS | el usuario |
| Host en `apps/rrhh-frontend/deployment.yaml` | `aspavi-infra` | PR |

Ya **no** hacen falta: `KeycloakRealmImport` nuevo, rutas en el Ingress de
Keycloak, CORS, variables o secrets de employee-service, variables de iam,
reinicios, la lista de tenants de los servicios (`aspavi.auth.tenants`) ni
`TENANT_MAP` en el front.
