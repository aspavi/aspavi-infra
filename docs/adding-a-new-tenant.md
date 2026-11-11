# Añadir un nuevo tenant

Guía paso a paso para incorporar un nuevo cliente/empresa al sistema multi-tenant ASPAVI RRHH.

En este ejemplo usaremos el slug `globex` y el dominio `globex.aspavi.com`.

---

## Requisitos previos

- Acceso a la consola de Keycloak (`https://keycloak.aspavi.com`) con rol admin
- Acceso al cluster K3s con `kubectl`
- Acceso al repo `aspavi-infra` con permisos de push
- DNS del nuevo subdominio apuntando al cluster (mismo IP que los demás subdominios)

---

## Paso 1 — Crear el realm en Keycloak (YAML)

Copia el fichero de un tenant existente y adáptalo:

```bash
cp apps/keycloak/realm-import-acme.yaml apps/keycloak/realm-import-globex.yaml
```

Edita `realm-import-globex.yaml` y cambia los siguientes valores:

| Campo | Valor anterior (acme) | Valor nuevo (globex) |
|---|---|---|
| `metadata.name` | `realm-import-acme` | `realm-import-globex` |
| `spec.realm.realm` | `rrhh-tenant-acme` | `rrhh-tenant-globex` |
| `spec.realm.displayName` | `RRHH - Acme Corporation` | `RRHH - Globex` |
| `redirectUris` | `https://acme.aspavi.com/*` | `https://globex.aspavi.com/*` |
| `webOrigins` | `https://acme.aspavi.com` | `https://globex.aspavi.com` |
| `tenant_id` claim (×3: `rrhh-frontend`, `rrhh-employee-service`, `rrhh-iam-service`) | `acme` | `globex` |

> **IMPORTANTE:** `KeycloakRealmImport` solo funciona en la **creación inicial**.
> Si el realm ya existe en Keycloak, el operator lo ignora.
> Para reimportar: borra el CR (`kubectl delete keycloakrealmimport realm-import-globex -n rrhh-auth`)
> y el realm en la consola de Keycloak, luego vuelve a aplicar.

El import ya trae lo que necesita la gestión de roles (aspavi-iam-service):

- el atributo `employee_id` declarado en el user profile (solo lo edita un admin)
  y su mapper en `rrhh-frontend`, que lo pone en el claim `employee_id`;
- el client confidencial `rrhh-iam-service`, con service account y los roles de
  `realm-management` `view-realm`, `manage-realm`, `view-users`, `query-users`
  y `manage-users`. No le des ninguno más.

Los permisos (`<recurso>:read|write`) y los roles compuestos **no** van en el
import: los siembra iam-service al arrancar.

Para un realm que **ya existe** (creado antes de que el import llevara todo
esto), aplica el mismo paso con el script, que es idempotente:

```bash
python3 scripts/keycloak/bootstrap-iam-realm.py <slug> --dry-run
python3 scripts/keycloak/bootstrap-iam-realm.py <slug> --production
```

Los scripts de `scripts/keycloak/` usan la Admin REST API pública como el
usuario `admin` del realm master. La contraseña la leen del Llavero de macOS
(`security find-generic-password -a aspavi -s KEYCLOAK_ADMIN_PASSWORD -w`), o de
`KEYCLOAK_ADMIN_PASSWORD` si está definida; nunca la imprimen. El kubeconfig es
`~/.kube/aspavi.kubeconfig`, o el que se pase con `--kubeconfig`. El admin de
arranque del operador (`temp-admin`) ya no sirve, así que no usan `kcadm` dentro
del pod.

El import también hace que **el email sea el nombre de usuario**
(`registrationEmailAsUsername: true`, con `loginWithEmailAllowed: true` y
`editUsernameAllowed: false`) y que el email solo lo cambie un admin (en el user
profile, `email.permissions.edit = ["admin"]`): ni la consola de cuenta ni
UPDATE_PROFILE dejan al usuario tocarlo. iam-service da de alta los usuarios
con username = email. Para un realm que ya existe:

```bash
python3 scripts/keycloak/realm-login-settings.py <slug> [<slug> ...]           # dry-run: qué cambiaría
python3 scripts/keycloak/realm-login-settings.py <slug> [<slug> ...] --apply
```

> Con ese ajuste activo, Keycloak cambia el username de un usuario existente
> por su email **en cuanto se actualiza el usuario por cualquier vía**: un PUT
> de admin aunque solo cambie el nombre o `employee_id`, o el propio usuario
> editando su nombre en la consola de cuenta. Desde ese momento entra con el
> email y no con el username antiguo, y el claim `preferred_username` pasa a
> ser el email. Un alta con un username distinto del email también se guarda
> con el email.
>
> Y el email pasa a ser obligatorio también para el admin: a un usuario **sin
> email** no se le puede actualizar nada (ni siquiera `employee_id`) ni dar de
> alta sin email; Keycloak responde `400 error-user-attribute-required` con
> `field: email`. Los usuarios sin email deben tenerlo antes de aplicar el
> ajuste (`inventory.py` muestra `email=-` en esos casos).

Para ver cómo ha quedado un realm (solo lectura: roles, composites, clients y
usuarios sin rol base):

```bash
python3 scripts/keycloak/inventory.py <slug>      # sin argumento: todos los realms
```

---

## Paso 2 — Actualizar el CORS middleware (Traefik)

Añade el nuevo origen en `apps/employee-service/cors-middleware.yaml`:

```yaml
accessControlAllowOriginList:
  - "https://acme.aspavi.com"
  - "https://test.aspavi.com"
  - "https://globex.aspavi.com"    # ← añadir
```

---

## Paso 3 — Actualizar el Ingress del frontend para incluir el nuevo dominio

El frontend es una única SPA que sirve todos los tenants, detectando cuál usar desde el hostname.
Actualiza `apps/rrhh-frontend/deployment.yaml`:

**En la sección `spec.tls.hosts`:**
```yaml
tls:
  - hosts:
      - test.aspavi.com
      - acme.aspavi.com
      - globex.aspavi.com    # ← añadir
    secretName: rrhh-frontend-tls
```

**En la sección `spec.rules`:**
```yaml
rules:
  - host: test.aspavi.com
    http:
      paths:
        - path: /
          pathType: Prefix
          backend:
            service:
              name: rrhh-frontend
              port:
                number: 80
  - host: acme.aspavi.com
    http:
      paths:
        - path: /
          pathType: Prefix
          backend:
            service:
              name: rrhh-frontend
              port:
                number: 80
  - host: globex.aspavi.com    # ← añadir
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

cert-manager generará automáticamente un certificado SAN que cubra todos los hosts.

---

## Paso 4 — Añadir el client secret al deployment del backend

En `apps/employee-service/deployment.yaml`, añade la nueva variable de entorno:

```yaml
- name: KEYCLOAK_CLIENT_SECRET_GLOBEX
  valueFrom:
    secretKeyRef:
      name: keycloak-client-secrets
      key: employee-service-secret-globex
```

---

## Paso 5 — Commit y push → ArgoCD sincroniza

```bash
git add apps/keycloak/realm-import-globex.yaml \
        apps/employee-service/cors-middleware.yaml \
        apps/employee-service/deployment.yaml
git commit -m "feat: add globex tenant"
git push
```

ArgoCD detecta el cambio y aplica automáticamente:
- El nuevo `KeycloakRealmImport` (crea el realm en Keycloak)
- El CORS middleware actualizado
- El deployment del employee-service (quedará en estado `Pending` hasta el paso 6)

---

## Paso 6 — Obtener el client secret del nuevo realm

Una vez ArgoCD haya sincronizado y el realm esté creado en Keycloak:

1. Entra en `https://keycloak.aspavi.com`
2. Cambia al realm **`rrhh-tenant-globex`**
3. **Clients** → `rrhh-employee-service` → pestaña **Credentials**
4. Copia el **Client secret**

---

## Paso 7 — Crear el Kubernetes Secret manualmente

```bash
# Si el secret keycloak-client-secrets NO existe aún:
kubectl create secret generic keycloak-client-secrets \
  --from-literal=employee-service-secret-test="SECRET_TEST" \
  --from-literal=employee-service-secret-acme="SECRET_ACME" \
  --from-literal=employee-service-secret-globex="SECRET_GLOBEX" \
  -n rrhh-system

# Si el secret YA EXISTE (patch sin borrar los otros):
kubectl patch secret keycloak-client-secrets -n rrhh-system \
  --type=merge \
  -p '{"stringData":{"employee-service-secret-globex":"SECRET_GLOBEX"}}'

# Reiniciar el pod para que tome el nuevo valor:
kubectl rollout restart deployment/employee-service -n rrhh-system
```

### Paso 7b — Secret de `rrhh-iam-service`

El client secret de `rrhh-iam-service` va en el mismo Secret, con la clave
`iam-service-secret-<slug>`. El script lo lee de Keycloak y lo guarda sin
imprimirlo; sobre un realm recién importado es lo único que tiene que hacer:

```bash
python3 scripts/keycloak/bootstrap-iam-realm.py globex --production
kubectl rollout restart deployment/iam-service -n rrhh-system
```

`--production` es obligatorio para cualquier tenant que no sea `test`.

---

## Paso 8 — Actualizar el frontend

En `aspavi-rrhh-management-site`, edita `src/utils/tenant.ts` y añade la entrada:

```typescript
'globex.aspavi.com': {
  realm: 'rrhh-tenant-globex',
  displayName: 'Globex',
  tenantId: 'globex',
},
```

Commit, push y el pipeline de GitHub Actions construye y publica la nueva imagen.
ArgoCD despliega el frontend actualizado automáticamente.

---

## Paso 9 — Verificación

```bash
# Realm creado en Keycloak
kubectl get keycloakrealmimport -n rrhh-auth

# Employee service corriendo con los nuevos secrets
kubectl get pods -n rrhh-system
kubectl describe pod -n rrhh-system -l app=employee-service | grep -A5 "Environment"

# Comprobar que el login funciona desde el nuevo dominio
curl -s -X POST \
  https://keycloak.aspavi.com/realms/rrhh-tenant-globex/protocol/openid-connect/token \
  -d "grant_type=password&client_id=rrhh-frontend&username=USUARIO&password=PASSWORD&scope=openid" \
  | jq .tenant_id
# Debe devolver: "globex"

# rrhh-iam-service obtiene token con su service account
curl -s -X POST \
  https://keycloak.aspavi.com/realms/rrhh-tenant-globex/protocol/openid-connect/token \
  -d grant_type=client_credentials -d client_id=rrhh-iam-service \
  --data-urlencode "client_secret=$(kubectl -n rrhh-system get secret keycloak-client-secrets \
      -o jsonpath='{.data.iam-service-secret-globex}' | base64 -d)" \
  | jq -r .token_type
# Debe devolver: Bearer
```

---

## Resumen de archivos tocados

| Fichero | Repo | Acción |
|---|---|---|
| `apps/keycloak/realm-import-{slug}.yaml` | `aspavi-infra` | Crear (copiar de acme) |
| `apps/employee-service/cors-middleware.yaml` | `aspavi-infra` | Añadir origen al CORS |
| `apps/rrhh-frontend/deployment.yaml` | `aspavi-infra` | Añadir host a Ingress (TLS + rules) |
| `apps/employee-service/deployment.yaml` | `aspavi-infra` | Añadir env var del secret |
| `kubectl patch secret keycloak-client-secrets` | cluster | Ejecutar manualmente |
| `python3 scripts/keycloak/bootstrap-iam-realm.py {slug} --production` | cluster | Ejecutar (secret de iam-service) |
| `python3 scripts/keycloak/realm-login-settings.py {slug} --apply` | cluster | Solo si el realm ya existía (email como usuario) |
| `src/utils/tenant.ts` | `aspavi-rrhh-management-site` | Añadir entrada al mapa |

El único paso manual que no se puede automatizar (por seguridad) es el **Paso 6** — crear/actualizar el Kubernetes Secret con el client secret real de Keycloak.
