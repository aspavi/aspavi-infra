#!/bin/sh
# Prueba de restauración de la copia de UN tenant (ASP-43), en un Postgres
# TEMPORAL. Uso: restore-check.sh <directorio de la copia, ya descifrada>
#
# La usan dump.sh (cada noche, en el Postgres temporal del pod, antes de subir
# nada) y scripts/backup/restore-test.py (en local, sobre una copia bajada del
# bucket y descifrada). Ver apps/postgresql/README.md, "Copias por tenant".
#
# Por cada base de la copia: crea <DB_PREFIX><base>, restaura el esquema
# (schema.dump), carga cada tabla del manifiesto con COPY, pone las secuencias,
# y comprueba:
#   - que cada tabla tiene las filas que dice el manifiesto (y su sha256);
#   - que no hay tablas en el esquema que falten en el manifiesto;
#   - que todas las FKs se cumplen: la copia de un tenant no apunta fuera de
#     sí misma ni de las tablas globales.
# Después borra esas bases (KEEP=1 las deja para mirarlas).
#
# Entra como superusuario (PGHOST/PGUSER) porque carga con
# session_replication_role=replica: sin triggers ni FKs durante la carga, que
# es lo que permite cargar en cualquier orden y con FKs circulares
# (time_entry <-> time_correction). Las FKs se comprueban después, todas.
# Se niega a correr contra otro Postgres que no sea local.
set -eu

DIR=${1:?uso: restore-check.sh <directorio de la copia>}
: "${DB_PREFIX:=restore_}" "${KEEP:=0}"
die() { echo "ERROR: $*" >&2; exit 1; }
case "${PGHOST:-}" in
  127.0.0.1|localhost|/*) ;;
  *) die "PGHOST='${PGHOST:-}': solo restauro en un Postgres local o temporal (127.0.0.1, localhost o un socket)" ;;
esac
[ -f "$DIR/manifest.tsv" ] || die "no encuentro $DIR/manifest.tsv"
echo "$DB_PREFIX" | grep -Eq '^[a-z][a-z0-9_]*$' || die "DB_PREFIX no válido: $DB_PREFIX"
q() { psql -X -At -v ON_ERROR_STOP=1 "$@"; }
TAB=$(printf '\t')

fail=0
for db in $(cut -f1 "$DIR/manifest.tsv" | sort -u); do
  target="$DB_PREFIX$db"
  src="$DIR/pg/$db"
  [ -f "$src/schema.dump" ] || die "$db: falta schema.dump"
  [ "$(q -d postgres -c "SELECT count(*) FROM pg_database WHERE datname = '$target'")" = 0 ] ||
    die "$target ya existe en este Postgres; no lo piso"
  q -d postgres -c "CREATE DATABASE \"$target\"" > /dev/null
  pg_restore -d "$target" --no-owner --no-privileges --exit-on-error "$src/schema.dump"

  # sha256 de cada fichero antes de cargarlo
  awk -F'\t' -v d="$db" '$1 == d {print $5 "  " $2 ".copy"}' "$DIR/manifest.tsv" > "$src/.sha256"
  (cd "$src" && sha256sum -c -s .sha256) || { echo "$db: ERROR, algún fichero no coincide con su sha256 del manifiesto"; fail=1; }
  rm -f "$src/.sha256"

  {
    echo "BEGIN;"
    echo "SET session_replication_role = replica;"
    awk -F'\t' -v d="$db" -v src="$src" '$1 == d {printf "\\copy %s (%s) FROM '\''%s/%s.copy'\''\n", $2, $6, src, $2}' "$DIR/manifest.tsv"
    while IFS="$TAB" read -r s seq v; do
      [ -z "$v" ] || echo "SELECT setval('$s.$seq', $v);"
    done < "$src/sequences.tsv"
    echo "COMMIT;"
  } | psql -X -q -v ON_ERROR_STOP=1 -d "$target" > /dev/null

  # Filas: manifiesto contra lo restaurado
  awk -F'\t' -v d="$db" '$1 == d {print $2 "|" $4}' "$DIR/manifest.tsv" | sort > "$src/.want"
  q -d "$target" <<'SQL' | sort > "$src/.got"
SELECT format('SELECT %L || ''|'' || count(*) FROM %I.%I', n.nspname || '.' || c.relname, n.nspname, c.relname)
  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE c.relkind IN ('r', 'p') AND NOT c.relispartition
   AND n.nspname NOT IN ('pg_catalog', 'information_schema') AND n.nspname NOT LIKE 'pg\_%'
\gexec
SQL
  if ! cmp -s "$src/.want" "$src/.got"; then
    echo "$db: ERROR, las filas no coinciden con el manifiesto (< manifiesto, > restaurado):"
    diff "$src/.want" "$src/.got" | grep '^[<>]' | head -20
    fail=1
  fi

  # FKs: filas cuya referencia no está (con todas las columnas de la FK no nulas)
  q -d "$target" -F'|' <<'SQL' > "$src/.fk"
SELECT format('SELECT %L, count(*) FROM %s c WHERE %s AND NOT EXISTS (SELECT 1 FROM %s p WHERE %s)',
              k.conrelid::regclass::text || '.' || k.conname, k.conrelid::regclass,
              (SELECT string_agg(format('c.%I IS NOT NULL', a.attname), ' AND ')
                 FROM unnest(k.conkey) u(n) JOIN pg_attribute a ON a.attrelid = k.conrelid AND a.attnum = u.n),
              k.confrelid::regclass,
              (SELECT string_agg(format('p.%I = c.%I', pa.attname, ca.attname), ' AND ')
                 FROM unnest(k.conkey, k.confkey) u(c, p)
                 JOIN pg_attribute ca ON ca.attrelid = k.conrelid AND ca.attnum = u.c
                 JOIN pg_attribute pa ON pa.attrelid = k.confrelid AND pa.attnum = u.p))
  FROM pg_constraint k WHERE k.contype = 'f' ORDER BY 1
\gexec
SQL
  nfk=$(grep -c . "$src/.fk" || true)
  broken=$(awk -F'|' '$2 != 0' "$src/.fk")
  if [ -n "$broken" ]; then
    echo "$db: ERROR, FKs que no se cumplen (FK|filas):"; printf '%s\n' "$broken" | head -20
    fail=1
  fi

  [ "$KEEP" = 1 ] || q -d postgres -c "DROP DATABASE \"$target\"" > /dev/null
  printf '%-26s %2s tablas, %6s filas, %2s FKs; ' "$db" "$(wc -l < "$src/.want" | tr -d ' ')" \
    "$(awk -F'|' '{s += $2} END {print s + 0}' "$src/.want")" "$nfk"
  if cmp -s "$src/.want" "$src/.got" && [ -z "$broken" ]; then echo "restaurada: mismas filas, FKs bien"; else echo "MAL"; fi
  rm -f "$src/.want" "$src/.got" "$src/.fk"
done
[ "$fail" = 0 ] || die "la copia de $(sed -n 's/^tenant=//p' "$DIR/backup.info") NO se restaura bien"
echo "copia de $(sed -n 's/^tenant=//p' "$DIR/backup.info"): restaurada y comprobada"
