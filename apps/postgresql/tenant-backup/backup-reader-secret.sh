#!/bin/sh
# Paso 1 del Job del rol backup_reader (ASP-43): el Secret con su contraseña.
# Corre en un initContainer con kubectl; ver README.md, "Copias por tenant".
#
#   - si el Secret postgresql-backup-reader no existe en rrhh-data, lo crea con
#     username=backup_reader y una contraseña aleatoria nueva;
#   - si existe, no lo toca;
#   - deja la contraseña en $CREDS_DIR (emptyDir en memoria) para el paso 2,
#     que la pone en el rol.
# La contraseña no se imprime ni se pasa como argumento a ningún comando.
set -eu

: "${SECRETS_NS:=rrhh-data}" "${CREDS_DIR:=/creds}"
name=postgresql-backup-reader
user=backup_reader
umask 077

# --ignore-not-found: vacío si no existe; cualquier otro error (permisos, API
# caída) para el script en vez de llevarlo a crear uno nuevo
if [ -z "$(kubectl -n "$SECRETS_NS" get secret "$name" --ignore-not-found -o name)" ]; then
  printf %s "$user" > "$CREDS_DIR/new-username"
  # printf %s: sin salto de línea al final, que acabaría dentro del Secret
  printf %s "$(head -c 48 /dev/urandom | base64 | tr -d '/+=\n' | cut -c1-40)" > "$CREDS_DIR/new-password"
  kubectl -n "$SECRETS_NS" create secret generic "$name" \
    --from-file=username="$CREDS_DIR/new-username" --from-file=password="$CREDS_DIR/new-password" > /dev/null
  kubectl -n "$SECRETS_NS" label secret "$name" app.kubernetes.io/managed-by=postgresql-backup-reader > /dev/null
  rm -f "$CREDS_DIR/new-username" "$CREDS_DIR/new-password"
  echo "  $name: creado"
else
  echo "  $name: ya existe"
fi

have_user=$(kubectl -n "$SECRETS_NS" get secret "$name" -o jsonpath='{.data.username}' | base64 -d)
[ "$have_user" = "$user" ] || { echo "ERROR: $name tiene username '$have_user', esperaba '$user'" >&2; exit 1; }
kubectl -n "$SECRETS_NS" get secret "$name" -o jsonpath='{.data.password}' | base64 -d > "$CREDS_DIR/backup_reader.password"
# Solo letras y números, 32 o más, sin salto de línea (psql quitaría uno final
# al leerla y el rol y el Secret no coincidirían)
if ! grep -Eq '^[A-Za-z0-9]{32,}$' "$CREDS_DIR/backup_reader.password" || [ "$(wc -l < "$CREDS_DIR/backup_reader.password")" -ne 0 ]; then
  echo "ERROR: la contraseña de $name no tiene el formato esperado (32+ letras y números, sin salto de línea)" >&2
  exit 1
fi
