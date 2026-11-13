#!/bin/bash
# Copias por tenant (ASP-43), paso de Keycloak. Lo ejecuta el initContainer
# `kc-export` del CronJob postgresql-tenant-backup, con la imagen de Keycloak de
# la MISMA versión que el servidor; ver README.md, "Copias por tenant".
#
# Exporta el realm rrhh-tenant-<tenant> de cada tenant con copia, con sus
# usuarios (y sus credenciales) en el mismo fichero, desde la COPIA de la base
# keycloak que dump.sh ha restaurado en el Postgres temporal del pod. No toca el
# Keycloak de verdad ni su base: kc.sh export no está soportado con el servidor
# arrancado contra la misma base, y aquí no hay ningún servidor usando la copia.
set -euo pipefail

WORK=${WORK:-/work}
[ -s "$WORK/plan.tsv" ] || { echo "Ningún tenant con copia."; exit 0; }

# La imagen tiene que ser la misma versión que el Keycloak que escribió la base:
# más vieja no entiende el esquema, y más nueva migraría la copia antes de exportar.
db_version=$(cat "$WORK/keycloak.version")
image_version=$(/opt/keycloak/bin/kc.sh --version 2>/dev/null | sed -n 's/^Keycloak //p')
if [ "$db_version" != "$image_version" ]; then
  echo "ERROR: la base keycloak es de Keycloak $db_version y esta imagen es $image_version;" \
       "pon quay.io/keycloak/keycloak:$db_version en cronjob.yaml (kc-export) y en scripts/backup/restore-test.py" >&2
  exit 1
fi
echo "  Keycloak $image_version, la misma versión que la base"

while IFS=$'\t' read -r t keep realm; do
  if [ "$realm" != si ]; then echo "  $t: sin realm, no hay nada que exportar"; continue; fi
  out="$WORK/out/$t/keycloak"
  mkdir -p "$out"
  if ! /opt/keycloak/bin/kc.sh export --dir "$out" --realm "rrhh-tenant-$t" --users realm_file \
      --db postgres --db-url-host 127.0.0.1 --db-url-database keycloak --db-username postgres --db-password unused \
      --cache local > "$WORK/kc-export.log" 2>&1; then
    grep -E 'ERROR|Exception' "$WORK/kc-export.log" | head -20
    echo "ERROR: kc.sh export de rrhh-tenant-$t ha fallado" >&2
    exit 1
  fi
  grep -q "Realm 'rrhh-tenant-$t' - data exported" "$WORK/kc-export.log" ||
    { tail -20 "$WORK/kc-export.log"; echo "ERROR: kc.sh export no dice que haya exportado rrhh-tenant-$t" >&2; exit 1; }
  [ -s "$out/rrhh-tenant-$t-realm.json" ] || { echo "ERROR: falta $out/rrhh-tenant-$t-realm.json" >&2; exit 1; }
  echo "  $t: rrhh-tenant-$t exportado ($(wc -c < "$out/rrhh-tenant-$t-realm.json") bytes)"
done < "$WORK/plan.tsv"
rm -f "$WORK/kc-export.log"
