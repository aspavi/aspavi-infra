#!/bin/sh
# Copias por tenant (ASP-43), paso de la base de datos. Lo ejecuta el
# initContainer `dump` del CronJob postgresql-tenant-backup; ver README.md,
# "Copias por tenant".
#
# Entra como backup_reader (solo lectura, BYPASSRLS) y deja en $WORK/out/<tenant>/,
# por cada tenant con copia:
#   pg/<base>/schema.dump      pg_dump --schema-only -Fc de la base
#   pg/<base>/<esq>.<tabla>.copy  COPY en texto: las filas del tenant de cada
#                              tabla con tenant_id, y entera cada tabla sin él
#   pg/<base>/sequences.tsv    valor de cada secuencia
#   manifest.tsv               base, tabla, tipo (tenant|global), filas, sha256, columnas
#   backup.info                tenant, fecha, versiones, migración de Flyway de cada base
# y restaura la base keycloak en el Postgres temporal del pod (sidecar
# scratch-pg) para que el paso kc-export saque de ella el realm de cada tenant.
# Al final, restore-check.sh restaura la copia de cada tenant en ese mismo
# Postgres temporal y compara filas con el manifiesto: si algo no cuadra, el
# pod falla y no se sube nada.
#
# Tenants: los realms rrhh-tenant-* de Keycloak más los de tenants.conf. Un
# realm que no esté en tenants.conf tiene copia (es el valor por defecto) y el
# log lo avisa. No imprime contraseñas ni datos: solo nombres de base, tabla y
# tenant, y recuentos.
set -eu

: "${WORK:=/work}" "${TENANTS_CONF:=/config/tenants.conf}" "${SCRATCH_HOST:=127.0.0.1}"
: "${KEEP_DAILY:=7}" "${KEEP_WEEKLY:=4}" "${KEEP_MONTHLY:=6}"
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
ID_RE='^[a-z][a-z0-9-]{1,29}$'
NAME_RE='^[a-z_][a-z0-9_]*$'
umask 077

die() { echo "ERROR: $*" >&2; exit 1; }
q() { psql -X -At -v ON_ERROR_STOP=1 "$@"; }
scratch() { env -u PGPASSWORD -u PGDATABASE PGHOST="$SCRATCH_HOST" PGUSER=postgres "$@"; }

echo "== Conexión"
server=$(q -d postgres -c "SHOW server_version_num")
client=$(pg_dump --version | sed -E 's/.* ([0-9]+)\.([0-9]+).*/\1/')
[ "$client" -ge $((server / 10000)) ] || die "pg_dump $client es más antiguo que el servidor"
# Sin BYPASSRLS el RLS filtraría las filas sin dar error y las copias saldrían
# vacías: se comprueba antes de copiar nada.
attrs=$(q -d postgres -c "SELECT rolbypassrls::text || ' ' || rolsuper::text FROM pg_roles WHERE rolname = current_user")
[ "$attrs" = "true false" ] || die "$(q -d postgres -c 'SELECT current_user') tiene que tener BYPASSRLS y no ser superusuario (tiene: bypassrls/super = $attrs)"
echo "  $(q -d postgres -c "SELECT current_user || ' en ' || version()" | cut -d, -f1); pg_dump $(pg_dump --version | awk '{print $3}')"

echo "== Tenants"
realms=$(q -d keycloak -c "SELECT substr(name, 13) FROM realm WHERE name LIKE 'rrhh-tenant-%' ORDER BY 1")
# tenants.conf: "<tenant> on|off [diarias/semanales/mensuales]"; # comenta
conf=$(sed -e 's/#.*//' -e 's/[[:space:]]\{1,\}/ /g' -e 's/^ //' -e 's/ $//' "$TENANTS_CONF" | grep -v '^$' || true)
printf '%s\n' "$conf" | while read -r t state keep extra; do
  [ -n "$t" ] || continue
  echo "$t" | grep -Eq "$ID_RE" || die "tenants.conf: id de tenant no válido: '$t'"
  case "$state" in on|off) ;; *) die "tenants.conf: '$t' tiene '$state'; tiene que ser on u off" ;; esac
  [ -z "$keep" ] || echo "$keep" | grep -Eq '^[0-9]{1,3}/[0-9]{1,3}/[0-9]{1,3}$' || die "tenants.conf: retención de '$t' no válida: '$keep' (diarias/semanales/mensuales)"
  [ -z "$extra" ] || die "tenants.conf: sobra '$extra' en la línea de '$t'"
done
dups=$(printf '%s\n' "$conf" | awk 'NF {print $1}' | sort | uniq -d)
[ -z "$dups" ] || die "tenants.conf: tenants repetidos: $dups"

mkdir -p "$WORK/out"
: > "$WORK/plan.tsv"
for t in $(printf '%s\n%s\n' "$realms" "$(printf '%s\n' "$conf" | awk 'NF {print $1}')" | grep -v '^$' | sort -u); do
  echo "$t" | grep -Eq "$ID_RE" || { echo "  AVISO: realm rrhh-tenant-$t: el id no es de tenant, no se copia"; continue; }
  line=$(printf '%s\n' "$conf" | awk -v t="$t" '$1 == t')
  state=$(echo "$line" | awk '{print $2}')
  keep=$(echo "$line" | awk '{print $3}')
  [ -n "$keep" ] || keep="$KEEP_DAILY/$KEEP_WEEKLY/$KEEP_MONTHLY"
  realm=no; printf '%s\n' "$realms" | grep -qx "$t" && realm=si
  if [ -z "$state" ]; then
    state=on
    echo "  AVISO: $t no está en tenants.conf: copia activada (valor por defecto). Añádelo a tenants.conf con on u off."
  fi
  if [ "$state" = off ]; then echo "  $t: sin copia (off)"; continue; fi
  [ "$realm" = si ] || echo "  AVISO: $t está activado pero no tiene realm rrhh-tenant-$t: se copian sus datos, sin Keycloak"
  printf '%s\t%s\t%s\n' "$t" "$keep" "$realm" >> "$WORK/plan.tsv"
  echo "  $t: copia, retención $keep, realm: $realm"
done
tenants=$(cut -f1 "$WORK/plan.tsv")
[ -n "$tenants" ] || { echo "Ningún tenant con copia: no hay nada que hacer."; exit 0; }

dbs=$(q -d postgres -c "SELECT datname FROM pg_database WHERE datallowconn AND NOT datistemplate
                          AND datname NOT IN ('postgres', 'keycloak') ORDER BY 1")
known=$(printf '%s\n%s\n' "$realms" "$(printf '%s\n' "$conf" | awk 'NF {print $1}')" | grep -v '^$' | sort -u)

for t in $tenants; do
  mkdir -p "$WORK/out/$t/pg"
  {
    echo "tenant=$t"
    echo "created=$STAMP"
    echo "server_version=$(q -d postgres -c 'SHOW server_version')"
    echo "pg_dump=$(pg_dump --version | awk '{print $3}')"
    echo "realm=$(awk -F'\t' -v t="$t" '$1 == t {print $3}' "$WORK/plan.tsv")"
  } > "$WORK/out/$t/backup.info"
  : > "$WORK/out/$t/manifest.tsv"
done

echo "== Bases"
for db in $dbs; do
  echo "$db" | grep -Eq "$NAME_RE" || die "nombre de base no esperado: $db"
  q -d "$db" -c "SELECT 1" > /dev/null || die "backup_reader no entra en $db (¿base nueva? relanza el Job postgresql-backup-reader)"
  mkdir -p "$WORK/pg/$db/global"

  # Tablas: tenant si tiene la columna tenant_id, global si no. Una tabla sin
  # tenant_id con una FK hacia una con tenant_id guarda datos de un tenant sin
  # decir de cuál: copiarla entera mezclaría tenants, así que se para.
  q -d "$db" -F "$(printf '\t')" -c "
    SELECT n.nspname, c.relname,
           CASE WHEN EXISTS (SELECT 1 FROM pg_attribute a WHERE a.attrelid = c.oid AND a.attname = 'tenant_id'
                                                            AND a.attnum > 0 AND NOT a.attisdropped)
                THEN 'tenant' ELSE 'global' END,
           (SELECT string_agg(a.attname, ',' ORDER BY a.attnum) FROM pg_attribute a
             WHERE a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped AND a.attgenerated = '')
      FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE c.relkind IN ('r', 'p') AND NOT c.relispartition
       AND n.nspname NOT IN ('pg_catalog', 'information_schema') AND n.nspname NOT LIKE 'pg\_%'
     ORDER BY 1, 2" > "$WORK/pg/$db/tables.tsv"
  bad=$(q -d "$db" -c "
    SELECT string_agg(DISTINCT conrelid::regclass::text || ' -> ' || confrelid::regclass::text, ', ')
      FROM pg_constraint k
     WHERE k.contype = 'f'
       AND NOT EXISTS (SELECT 1 FROM pg_attribute a WHERE a.attrelid = k.conrelid AND a.attname = 'tenant_id' AND NOT a.attisdropped)
       AND EXISTS (SELECT 1 FROM pg_attribute a WHERE a.attrelid = k.confrelid AND a.attname = 'tenant_id' AND NOT a.attisdropped)")
  [ -z "$bad" ] || die "$db: tablas sin tenant_id que cuelgan de una con tenant_id ($bad); no sé de qué tenant es cada fila"
  awk -F'\t' -v re="$NAME_RE" '$1 !~ re || $2 !~ re {print; bad=1} END {exit bad}' "$WORK/pg/$db/tables.tsv" ||
    die "$db: nombres de esquema o tabla no esperados (arriba)"

  pg_dump -d "$db" --schema-only -Fc -f "$WORK/pg/$db/schema.dump"
  q -d "$db" -F "$(printf '\t')" -c "SELECT schemaname, sequencename, coalesce(last_value::text, '') FROM pg_sequences ORDER BY 1, 2" \
    > "$WORK/pg/$db/sequences.tsv"
  flyway=$(q -d "$db" -c "SELECT coalesce(max(version::int)::text, '-') FROM flyway_schema_history WHERE success AND version ~ '^[0-9]+\$'" 2>/dev/null || echo '-')

  # Todas las tablas de la base en una sola transacción REPEATABLE READ: las de
  # todos los tenants y las globales salen de la misma foto.
  {
    echo "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;"
    while IFS="$(printf '\t')" read -r s tb kind cols; do
      if [ "$kind" = global ]; then
        echo "\\copy (SELECT $cols FROM $s.$tb) TO '$WORK/pg/$db/global/$s.$tb.copy'"
      else
        for t in $tenants; do
          mkdir -p "$WORK/out/$t/pg/$db"
          echo "\\copy (SELECT $cols FROM $s.$tb WHERE tenant_id = '$t') TO '$WORK/out/$t/pg/$db/$s.$tb.copy'"
        done
        # tenant_id de los datos, para avisar de filas que no van en ninguna copia
        echo "\\copy (SELECT coalesce(tenant_id::text, '<NULL>'), count(*) FROM $s.$tb GROUP BY 1) TO '$WORK/pg/$db/tenant-ids.$s.$tb'"
      fi
    done < "$WORK/pg/$db/tables.tsv"
    echo "COMMIT;"
  } > "$WORK/pg/$db/copy.sql"
  psql -X -q -v ON_ERROR_STOP=1 -d "$db" -f "$WORK/pg/$db/copy.sql"

  ntables=0
  for t in $tenants; do
    mkdir -p "$WORK/out/$t/pg/$db"
    cp "$WORK/pg/$db/schema.dump" "$WORK/pg/$db/sequences.tsv" "$WORK/out/$t/pg/$db/"
    echo "flyway.$db=$flyway" >> "$WORK/out/$t/backup.info"
    while IFS="$(printf '\t')" read -r s tb kind cols; do
      f="$WORK/out/$t/pg/$db/$s.$tb.copy"
      [ "$kind" = tenant ] || cp "$WORK/pg/$db/global/$s.$tb.copy" "$f"
      printf '%s\t%s.%s\t%s\t%s\t%s\t%s\n' "$db" "$s" "$tb" "$kind" "$(wc -l < "$f" | tr -d ' ')" \
        "$(sha256sum "$f" | cut -d' ' -f1)" "$cols" >> "$WORK/out/$t/manifest.tsv"
    done < "$WORK/pg/$db/tables.tsv"
    ntables=$(wc -l < "$WORK/pg/$db/tables.tsv" | tr -d ' ')
  done
  summary=""
  for t in $tenants; do
    summary="$summary $t=$(awk -F'\t' -v d="$db" '$1 == d && $3 == "tenant" {s += $4} END {print s + 0}' "$WORK/out/$t/manifest.tsv")"
  done
  globals=$(awk -F'\t' '$3 == "global" {printf "%s%s", sep, $2; sep=", "}' "$WORK/pg/$db/tables.tsv")
  printf '  %-26s V%-4s %2s tablas; filas por tenant:%s; globales (enteras): %s\n' "$db" "$flyway" "$ntables" "$summary" "${globals:--}"

  # Filas que no van en ninguna copia: tenant_id NULL o de un tenant sin realm
  # ni línea en tenants.conf
  cat "$WORK"/pg/"$db"/tenant-ids.* 2>/dev/null | awk -F'\t' '{n[$1] += $2} END {for (t in n) print t "\t" n[t]}' | sort |
    while IFS="$(printf '\t')" read -r tid n; do
      printf '%s\n' "$known" | grep -qx -- "$tid" ||
        echo "  AVISO: $db tiene $n filas con tenant_id '$tid', que no es ningún tenant conocido: no van en ninguna copia"
    done
done

echo "== Keycloak: copia de la base al Postgres temporal del pod"
# pg_dump hace una foto consistente de la base keycloak sin parar Keycloak.
# kc.sh export no está soportado con el servidor arrancado contra la misma base,
# así que el export se hace desde esta copia, que no usa ningún servidor.
pg_dump -d keycloak -Fc -f "$WORK/keycloak.dump"
scratch psql -X -q -v ON_ERROR_STOP=1 -d postgres -c "DROP DATABASE IF EXISTS keycloak" -c "CREATE DATABASE keycloak"
scratch pg_restore -d keycloak --no-owner --no-privileges --exit-on-error "$WORK/keycloak.dump"
rm -f "$WORK/keycloak.dump"
# versión de Keycloak de la base: kc-export.sh la compara con la de su imagen
scratch psql -X -At -v ON_ERROR_STOP=1 -d keycloak -c "SELECT version FROM migration_model ORDER BY update_time DESC, version DESC LIMIT 1" \
  > "$WORK/keycloak.version"
echo "  restaurada: $(scratch psql -X -At -d keycloak -c "SELECT count(*) FROM realm") realms, $(scratch psql -X -At -d keycloak -c "SELECT count(*) FROM user_entity") usuarios, Keycloak $(cat "$WORK/keycloak.version")"

echo "== Prueba de restauración de cada copia en el Postgres temporal"
ok=1
for t in $tenants; do
  if env -u PGPASSWORD -u PGDATABASE PGHOST="$SCRATCH_HOST" PGUSER=postgres DB_PREFIX="chk_" \
      sh "$(dirname "$0")/restore-check.sh" "$WORK/out/$t" > "$WORK/check.log" 2>&1; then
    sed "s/^/  [$t] /" "$WORK/check.log"
  else
    sed "s/^/  [$t] /" "$WORK/check.log"; ok=0
  fi
done
rm -f "$WORK/check.log"
[ "$ok" = 1 ] || die "la prueba de restauración ha fallado: no se sube ninguna copia"
rm -rf "$WORK/pg"
echo "== Hecho: $(echo $tenants)"
