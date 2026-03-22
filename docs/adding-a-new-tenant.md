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
| `tenant_id` claim (×2) | `acme` | `globex` |

> **IMPORTANTE:** `KeycloakRealmImport` solo funciona en la **creación inicial**.
> Si el realm ya existe en Keycloak, el operator lo ignora.
> Para reimportar: borra el CR (`kubectl delete keycloakrealmimport realm-import-globex -n rrhh-auth`)
> y el realm en la consola de Keycloak, luego vuelve a aplicar.

---

## Paso 2 — Actualizar el CORS middleware (Traefik)

Añade el nuevo origen en `apps/employee-service/cors-middleware.yaml`:

```yaml
accessControlAllowOriginList:
  - "https://app.aspavi.com"
  - "https://acme.aspavi.com"
  - "https://test.aspavi.com"
  - "https://globex.aspavi.com"    # ← añadir
```

---

## Paso 3 — Añadir el client secret al deployment del backend

En `apps/employee-service/deployment.yaml`, añade la nueva variable de entorno:

```yaml
- name: KEYCLOAK_CLIENT_SECRET_GLOBEX
  valueFrom:
    secretKeyRef:
      name: keycloak-client-secrets
      key: employee-service-secret-globex
```

---

## Paso 4 — Commit y push → ArgoCD sincroniza

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

## Paso 5 — Obtener el client secret del nuevo realm

Una vez ArgoCD haya sincronizado y el realm esté creado en Keycloak:

1. Entra en `https://keycloak.aspavi.com`
2. Cambia al realm **`rrhh-tenant-globex`**
3. **Clients** → `rrhh-employee-service` → pestaña **Credentials**
4. Copia el **Client secret**

---

## Paso 6 — Crear el Kubernetes Secret manualmente

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

---

## Paso 7 — Actualizar el frontend

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

## Paso 8 — Verificación

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
```

---

## Resumen de archivos tocados

| Fichero | Repo | Acción |
|---|---|---|
| `apps/keycloak/realm-import-{slug}.yaml` | `aspavi-infra` | Crear (copiar de acme) |
| `apps/employee-service/cors-middleware.yaml` | `aspavi-infra` | Añadir origen |
| `apps/employee-service/deployment.yaml` | `aspavi-infra` | Añadir env var del secret |
| `kubectl patch secret keycloak-client-secrets` | cluster | Ejecutar manualmente |
| `src/utils/tenant.ts` | `aspavi-rrhh-management-site` | Añadir entrada al mapa |

El único paso manual que no se puede automatizar (por seguridad) es el **Paso 6** — crear/actualizar el Kubernetes Secret con el client secret real de Keycloak.
