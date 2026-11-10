#!/usr/bin/env bash
# Paso único para un realm que YA existe: lo deja como lo crearía hoy
# apps/keycloak/realm-import-<tenant>.yaml, que el operador no reaplica
# (import --override=false).
#
#   - atributo employee_id declarado en el user profile (editable solo por admin)
#   - mapper employee_id en el client rrhh-frontend
#   - client confidencial rrhh-iam-service con service account y mapper tenant_id
#   - roles de realm-management del service account: view-realm, manage-realm,
#     view-users, query-users, manage-users
#   - su secret en rrhh-system/keycloak-client-secrets, clave iam-service-secret-<tenant>
#
# Idempotente: solo crea lo que falta; no borra ni sobrescribe nada que ya exista.
#
# Uso:
#   scripts/keycloak/bootstrap-iam-realm.sh <tenant> [--dry-run] [--production]
#
#   <tenant>      test | acme | ...   (realm rrhh-tenant-<tenant>)
#   --dry-run     solo dice qué haría
#   --production  obligatorio para cualquier tenant que no sea test
#
# Necesita KUBECONFIG apuntando al clúster (~/.kube/aspavi.kubeconfig) y python3.
# kcadm se ejecuta dentro del pod de Keycloak con el admin de arranque del
# operador; las credenciales de admin no salen del pod. El secret del client
# pasa por memoria hasta kubectl y no se imprime.

set -euo pipefail

TENANT=""
DRY_RUN=false
PRODUCTION=false
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=true ;;
    --production) PRODUCTION=true ;;
    -*) echo "Opción desconocida: $arg" >&2; exit 2 ;;
    *) TENANT="$arg" ;;
  esac
done
[[ -n "$TENANT" ]] || { sed -n '2,24p' "$0"; exit 2; }
if [[ "$TENANT" != "test" && "$PRODUCTION" != true && "$DRY_RUN" != true ]]; then
  echo "El tenant '$TENANT' no es test: añade --production para aplicarlo (o --dry-run para verlo)." >&2
  exit 2
fi

REALM="rrhh-tenant-$TENANT"
KC_NS=rrhh-auth
KC_POD=keycloak-0
APP_NS=rrhh-system
SECRET_NAME=keycloak-client-secrets
SECRET_KEY="iam-service-secret-$TENANT"
IAM_CLIENT=rrhh-iam-service
FRONT_CLIENT=rrhh-frontend
SA_ROLES=(view-realm manage-realm view-users query-users manage-users)
CFG="/tmp/kcadm-iam-$$.config"

kc() { kubectl -n "$KC_NS" exec -i "$KC_POD" -- /opt/keycloak/bin/kcadm.sh "$@" --config "$CFG"; }
say() { printf '  %-12s %s\n' "$1" "$2"; }
change() { if $DRY_RUN; then say "[dry-run]" "$1"; return 1; fi; say "aplicando" "$1"; }

cleanup() { kubectl -n "$KC_NS" exec "$KC_POD" -- rm -f "$CFG" >/dev/null 2>&1 || true; }
trap cleanup EXIT

echo "== $REALM $($DRY_RUN && echo '(dry-run)')"

kubectl -n "$KC_NS" exec "$KC_POD" -- bash -c \
  '/opt/keycloak/bin/kcadm.sh config credentials --config "$1" --server http://localhost:8080 \
     --realm master --user "$KC_BOOTSTRAP_ADMIN_USERNAME" --password "$KC_BOOTSTRAP_ADMIN_PASSWORD"' \
  _ "$CFG" >/dev/null
kc get "realms/$REALM" --fields realm >/dev/null

# --- 1. user profile ---------------------------------------------------------
PROFILE="$(kc get users/profile -r "$REALM")"
if python3 -c 'import json,sys; sys.exit(0 if any(a["name"]=="employee_id" for a in json.load(sys.stdin)["attributes"]) else 1)' <<<"$PROFILE"; then
  say "ok" "user profile ya declara employee_id"
elif change "declarar employee_id en el user profile"; then
  python3 -c '
import json,sys
p=json.load(sys.stdin)
p["attributes"].append({"name":"employee_id","displayName":"Employee ID",
  "validations":{"pattern":{"pattern":"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$","error-message":"employee_id debe ser un UUID"}},
  "permissions":{"view":["admin","user"],"edit":["admin"]},"multivalued":False})
print(json.dumps(p))' <<<"$PROFILE" | kc update users/profile -r "$REALM" -f - >/dev/null
fi

# --- 2. mapper employee_id en rrhh-frontend ---------------------------------
FRONT_ID="$(kc get clients -r "$REALM" -q clientId="$FRONT_CLIENT" --fields id --format csv --noquotes)"
[[ -n "$FRONT_ID" ]] || { echo "No existe el client $FRONT_CLIENT en $REALM" >&2; exit 1; }
if kc get "clients/$FRONT_ID/protocol-mappers/models" -r "$REALM" --fields name --format csv --noquotes | grep -qx employee_id; then
  say "ok" "$FRONT_CLIENT ya tiene el mapper employee_id"
elif change "crear el mapper employee_id en $FRONT_CLIENT"; then
  kc create "clients/$FRONT_ID/protocol-mappers/models" -r "$REALM" -f - >/dev/null <<'JSON'
{"name":"employee_id","protocol":"openid-connect","protocolMapper":"oidc-usermodel-attribute-mapper",
 "config":{"user.attribute":"employee_id","claim.name":"employee_id","jsonType.label":"String",
           "id.token.claim":"true","access.token.claim":"true","userinfo.token.claim":"true"}}
JSON
fi

# --- 3. client rrhh-iam-service ---------------------------------------------
IAM_ID="$(kc get clients -r "$REALM" -q clientId="$IAM_CLIENT" --fields id --format csv --noquotes)"
if [[ -n "$IAM_ID" ]]; then
  say "ok" "el client $IAM_CLIENT ya existe"
elif change "crear el client $IAM_CLIENT"; then
  kc create clients -r "$REALM" -f - >/dev/null <<JSON
{"clientId":"$IAM_CLIENT","name":"IAM Service","enabled":true,"publicClient":false,
 "standardFlowEnabled":false,"directAccessGrantsEnabled":false,"serviceAccountsEnabled":true,
 "clientAuthenticatorType":"client-secret","protocol":"openid-connect",
 "protocolMappers":[{"name":"tenant_id","protocol":"openid-connect","protocolMapper":"oidc-hardcoded-claim-mapper",
   "config":{"claim.name":"tenant_id","claim.value":"$TENANT","jsonType.label":"String",
             "id.token.claim":"true","access.token.claim":"true"}}]}
JSON
  IAM_ID="$(kc get clients -r "$REALM" -q clientId="$IAM_CLIENT" --fields id --format csv --noquotes)"
fi

# --- 4. roles del service account -------------------------------------------
if [[ -n "$IAM_ID" ]]; then
  CURRENT="$(kc get-roles -r "$REALM" --uusername "service-account-$IAM_CLIENT" --cclientid realm-management --fields name --format csv --noquotes || true)"
  MISSING=()
  for r in "${SA_ROLES[@]}"; do grep -qx "$r" <<<"$CURRENT" || MISSING+=("$r"); done
  if [[ ${#MISSING[@]} -eq 0 ]]; then
    say "ok" "el service account ya tiene ${SA_ROLES[*]}"
  elif change "dar al service account: ${MISSING[*]}"; then
    args=(); for r in "${MISSING[@]}"; do args+=(--rolename "$r"); done
    kc add-roles -r "$REALM" --uusername "service-account-$IAM_CLIENT" --cclientid realm-management "${args[@]}"
  fi
  EXTRA="$(grep -vxF -f <(printf '%s\n' "${SA_ROLES[@]}") <<<"$CURRENT" || true)"
  [[ -z "$EXTRA" ]] || say "AVISO" "el service account tiene roles de más (no se tocan): $(tr '\n' ' ' <<<"$EXTRA")"
else
  say "[dry-run]" "dar al service account: ${SA_ROLES[*]}"
fi

# --- 5. secret en Kubernetes -------------------------------------------------
if [[ -z "$IAM_ID" ]]; then
  say "[dry-run]" "guardar el secret en $APP_NS/$SECRET_NAME[$SECRET_KEY]"
else
  CLIENT_SECRET="$(kc get "clients/$IAM_ID/client-secret" -r "$REALM" --fields value --format csv --noquotes)"
  [[ -n "$CLIENT_SECRET" ]] || { echo "El client $IAM_CLIENT no tiene secret" >&2; exit 1; }
  STORED="$(kubectl -n "$APP_NS" get secret "$SECRET_NAME" -o "jsonpath={.data.$SECRET_KEY}" | base64 -d 2>/dev/null || true)"
  if [[ "$STORED" == "$CLIENT_SECRET" ]]; then
    say "ok" "$APP_NS/$SECRET_NAME[$SECRET_KEY] ya está al día"
  elif change "guardar el secret en $APP_NS/$SECRET_NAME[$SECRET_KEY]"; then
    printf '%s' "$CLIENT_SECRET" \
      | python3 -c 'import json,sys; print(json.dumps({"stringData":{sys.argv[1]:sys.stdin.read()}}))' "$SECRET_KEY" \
      | kubectl -n "$APP_NS" patch secret "$SECRET_NAME" --type=merge --patch-file=/dev/stdin >/dev/null
  fi
  unset CLIENT_SECRET STORED
fi

echo "== hecho"
