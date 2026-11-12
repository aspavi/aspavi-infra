#!/bin/sh
# Copia de seguridad puntual de TODA la instancia de Postgres (ASP-4), con
# prueba de restauración. La ejecuta backup-job-*.yaml; ver README.md.
#
# 1. globals.sql: roles (con sus hashes de contraseña) y permisos globales,
#    con pg_dumpall --globals-only
# 2. <base>.dump: cada base que admite conexiones, con pg_dump -Fc
# 3. restaura todo en un Postgres temporal dentro de este mismo pod (en un
#    emptyDir, sin red) y compara, tabla a tabla, el número de filas del
#    origen con el de la copia restaurada
# 4. solo si todo coincide, la copia pasa de <nombre>.tmp a <nombre> y se
#    escribe <nombre>/OK con el resumen
#
# Entra como el superusuario (PGHOST/PGUSER/PGPASSWORD), que salta el RLS: la
# copia tiene las filas de todos los tenants. No imprime contraseñas ni datos,
# solo nombres de base y de tabla, tamaños y recuentos de filas.
set -eu

: "${BACKUP_NAME:?}" "${BACKUP_DIR:=/backup}" "${SCRATCH_DIR:=/scratch}"
OUT="$BACKUP_DIR/$BACKUP_NAME"
TMP="$OUT.tmp"

if [ -e "$OUT/OK" ]; then
  echo "La copia $OUT ya existe y está verificada; no hago nada."
  cat "$OUT/OK"
  exit 0
fi

server=$(psql -d postgres -Atc "SHOW server_version_num")
client=$(pg_dump --version | sed -E 's/.* ([0-9]+)\.([0-9]+).*/\1/')
echo "Servidor $(psql -d postgres -Atc 'SHOW server_version'), pg_dump $(pg_dump --version | awk '{print $3}')"
if [ "$client" -lt $((server / 10000)) ]; then
  echo "ERROR: pg_dump $client es más antiguo que el servidor; usa una imagen de la misma versión o más nueva" >&2
  exit 1
fi

rm -rf "$TMP"
mkdir -p "$TMP"

# Recuento EXACTO de filas de cada tabla de usuario, ordenado: es lo que se
# compara entre el origen y la copia restaurada.
count_rows() { # $1 = base, resto = opciones de conexión
  db=$1; shift
  psql "$@" -d "$db" -X -At -v ON_ERROR_STOP=1 <<'SQL' | sort
SELECT format('SELECT %L || ''|'' || count(*) FROM %I.%I', n.nspname || '.' || c.relname, n.nspname, c.relname)
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p') AND n.nspname NOT IN ('pg_catalog', 'information_schema')
  AND n.nspname NOT LIKE 'pg\_%'
\gexec
SQL
}

echo "== Copia"
pg_dumpall --globals-only -f "$TMP/globals.sql"
dbs=$(psql -d postgres -Atc "SELECT datname FROM pg_database WHERE datallowconn AND NOT datistemplate ORDER BY 1")
for db in $dbs; do
  pg_dump -Fc -d "$db" -f "$TMP/$db.dump"
  pg_restore --list "$TMP/$db.dump" > /dev/null
  count_rows "$db" > "$TMP/$db.rows"
  printf '  %-28s %8s  %s tablas, %s filas\n' "$db" "$(du -h "$TMP/$db.dump" | cut -f1)" \
    "$(wc -l < "$TMP/$db.rows" | tr -d ' ')" "$(awk -F'|' '{s += $2} END {print s + 0}' "$TMP/$db.rows")"
done

echo "== Prueba de restauración en un Postgres temporal"
PGTMP="$SCRATCH_DIR/pgdata"
SOCK="$SCRATCH_DIR/sock"
rm -rf "$PGTMP" "$SOCK"
mkdir -p "$SOCK"
initdb -D "$PGTMP" -U postgres --auth=trust --no-locale -E UTF8 > "$SCRATCH_DIR/initdb.log" 2>&1 || {
  cat "$SCRATCH_DIR/initdb.log" >&2; exit 1; }
pg_ctl -D "$PGTMP" -o "-c listen_addresses='' -k $SOCK -c fsync=off" -w -l "$SCRATCH_DIR/pg.log" start > /dev/null
trap 'pg_ctl -D "$PGTMP" -m fast -w stop > /dev/null 2>&1 || true' EXIT
local_pg() { env -u PGHOST -u PGPASSWORD -u PGDATABASE "$@" -h "$SOCK" -U postgres; }

# globals.sql intenta crear el rol postgres, que en el temporal ya existe: ese
# error se espera. Cualquier otro problema saldría luego en pg_restore.
local_pg psql -X -q -d postgres -f "$TMP/globals.sql" > /dev/null 2> "$SCRATCH_DIR/globals.err" || true
grep -v 'role "postgres" already exists' "$SCRATCH_DIR/globals.err" | grep ERROR && {
  echo "ERROR: globals.sql no se restaura limpio" >&2; exit 1; } || true

ok=1
for db in $dbs; do
  [ "$db" = postgres ] || local_pg createdb -T template0 "$db"
  if ! local_pg pg_restore -d "$db" --exit-on-error "$TMP/$db.dump" 2> "$SCRATCH_DIR/restore.err"; then
    echo "  $db: ERROR al restaurar"; sed 's/^/    /' "$SCRATCH_DIR/restore.err" | head -20; ok=0; continue
  fi
  count_rows "$db" -h "$SOCK" -U postgres > "$SCRATCH_DIR/$db.restored"
  if cmp -s "$TMP/$db.rows" "$SCRATCH_DIR/$db.restored"; then
    echo "  $db: restaurada, mismas filas en las $(wc -l < "$TMP/$db.rows" | tr -d ' ') tablas"
  else
    echo "  $db: ERROR, las filas no coinciden"; diff "$TMP/$db.rows" "$SCRATCH_DIR/$db.restored" | head -20; ok=0
  fi
done
[ "$ok" = 1 ] || { echo "La copia NO es válida; se queda en $TMP para mirarla" >&2; exit 1; }

mv "$TMP" "$OUT"
{
  echo "Copia de $(date -u +%Y-%m-%dT%H:%M:%SZ), servidor $(psql -d postgres -Atc 'SHOW server_version')"
  echo "Bases: $(echo $dbs)"
  echo "Tamaño total: $(du -sh "$OUT" | cut -f1)"
  echo "Restauración probada: todas las bases, mismas filas por tabla"
} > "$OUT/OK"
echo "== Hecho: $OUT"
cat "$OUT/OK"
ls -la "$OUT"
