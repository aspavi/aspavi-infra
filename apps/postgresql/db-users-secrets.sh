#!/bin/sh
# Paso 1 del Job de usuarios de un servicio (ASP-4): sus dos Secrets en
# rrhh-system. Corre en un initContainer con kubectl; ver README.md.
#
# Por cada uno de <svc>-db-owner y <svc>-db-app:
#   - si no existe, lo crea con username=<svc>_<owner|app> y una contraseña
#     aleatoria nueva;
#   - si existe, no lo toca;
#   - deja su contraseña en $CREDS_DIR (emptyDir en memoria) para el paso 2,
#     que la pone en el rol de Postgres.
# Las contraseñas no se imprimen ni se pasan como argumento a ningún comando.
set -eu

: "${SVC:?}" "${SECRETS_NS:=rrhh-system}" "${CREDS_DIR:=/creds}"

# El ServiceAccount puede crear cualquier Secret en rrhh-system: el verbo
# create no se puede limitar por nombre en RBAC. Este script se limita a los
# dos nombres de su servicio.
echo "$SVC" | grep -Eq '^[a-z][a-z0-9-]*$' || { echo "ERROR: SVC no válido: $SVC" >&2; exit 1; }
user_prefix=$(echo "$SVC" | tr - _)
umask 077

for kind in owner app; do
  name="$SVC-db-$kind"
  user="${user_prefix}_$kind"
  echo "$name" | grep -Eq '^[a-z][a-z0-9-]*-db-(owner|app)$' || { echo "ERROR: no escribo el Secret $name" >&2; exit 1; }

  # --ignore-not-found: vacío si no existe; cualquier otro error (permisos,
  # API caída) para el script en vez de llevarlo a crear uno nuevo
  if [ -z "$(kubectl -n "$SECRETS_NS" get secret "$name" --ignore-not-found -o name)" ]; then
    printf %s "$user" > "$CREDS_DIR/new-username"
    # printf %s: sin salto de línea al final, que acabaría dentro del Secret
    printf %s "$(head -c 48 /dev/urandom | base64 | tr -d '/+=\n' | cut -c1-40)" > "$CREDS_DIR/new-password"
    kubectl -n "$SECRETS_NS" create secret generic "$name" \
      --from-file=username="$CREDS_DIR/new-username" --from-file=password="$CREDS_DIR/new-password" > /dev/null
    kubectl -n "$SECRETS_NS" label secret "$name" app.kubernetes.io/managed-by=postgresql-db-users \
      "aspavi.com/db-service=$SVC" > /dev/null
    rm -f "$CREDS_DIR/new-username" "$CREDS_DIR/new-password"
    echo "  $name: creado"
  else
    echo "  $name: ya existe"
  fi

  have_user=$(kubectl -n "$SECRETS_NS" get secret "$name" -o jsonpath='{.data.username}' | base64 -d)
  [ "$have_user" = "$user" ] || { echo "ERROR: $name tiene username '$have_user', esperaba '$user'" >&2; exit 1; }
  kubectl -n "$SECRETS_NS" get secret "$name" -o jsonpath='{.data.password}' | base64 -d > "$CREDS_DIR/$kind.password"
  # Solo letras y números, 32 o más, y nada más en el fichero (ni un salto de
  # línea): el deployment usa el valor tal cual y psql quitaría un salto
  # final, con lo que el rol y el Secret no coincidirían.
  if ! grep -Eq '^[A-Za-z0-9]{32,}$' "$CREDS_DIR/$kind.password" || [ "$(wc -l < "$CREDS_DIR/$kind.password")" -ne 0 ]; then
    echo "ERROR: la contraseña de $name no tiene el formato esperado (32+ letras y números, sin salto de línea)" >&2
    exit 1
  fi
done
