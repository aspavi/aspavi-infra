#!/bin/sh
# Paso 2 del Job del rol backup_reader (ASP-43), como el superusuario: el rol
# con el que leen las copias por tenant. Idempotente. Ver README.md, "Copias
# por tenant".
#
# backup_reader:
#   - LOGIN, BYPASSRLS (lee las filas de todos los tenants, también con el FORCE
#     de employees), y nada más: ni superusuario, ni CREATEDB, ni CREATEROLE, ni
#     REPLICATION;
#   - miembro de pg_read_all_data: SELECT en todas las tablas y secuencias de
#     todas las bases, también las que se creen después, sin tocar el ACL de
#     ninguna tabla (tampoco de las de Keycloak);
#   - default_transaction_read_only=on, y 2 conexiones como mucho;
#   - CONNECT en cada base de servicio (*_db). keycloak y postgres ya dan
#     CONNECT a PUBLIC: no se toca su ACL.
# Al final entra como backup_reader y comprueba que lee todas las filas de
# employees (FORCE RLS) y que no puede escribir.
set -eu

: "${CREDS_DIR:=/creds}"
[ -s "$CREDS_DIR/backup_reader.password" ] || { echo "ERROR: falta $CREDS_DIR/backup_reader.password" >&2; exit 1; }

echo "== backup_reader"
psql -X -q -v ON_ERROR_STOP=1 -d postgres <<SQL
\set pw \`cat $CREDS_DIR/backup_reader.password\`
SELECT NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'backup_reader') AS crear \gset
\if :crear
  CREATE ROLE backup_reader;
  \echo '  rol creado'
\else
  \echo '  el rol ya existe'
\endif
ALTER ROLE backup_reader WITH LOGIN BYPASSRLS INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION
  CONNECTION LIMIT 2 PASSWORD :'pw';
ALTER ROLE backup_reader SET default_transaction_read_only = on;
GRANT pg_read_all_data TO backup_reader;
SQL

# Ningún otro rol: si alguien le ha dado otro, se para (no se quita a ciegas)
other=$(psql -X -At -v ON_ERROR_STOP=1 -d postgres -c "
  SELECT string_agg(r.rolname, ', ') FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.roleid
   WHERE m.member = 'backup_reader'::regrole AND r.rolname <> 'pg_read_all_data'")
[ -z "$other" ] || { echo "ERROR: backup_reader es miembro de otros roles: $other" >&2; exit 1; }

echo "== CONNECT en las bases de servicio"
for db in $(psql -X -At -v ON_ERROR_STOP=1 -d postgres -c "
    SELECT datname FROM pg_database WHERE datallowconn AND NOT datistemplate AND datname LIKE '%\_db' ORDER BY 1"); do
  if [ "$(psql -X -At -d postgres -c "SELECT has_database_privilege('backup_reader', '$db', 'CONNECT')")" = t ]; then
    echo "  $db: ya tenía CONNECT"
  else
    psql -X -q -v ON_ERROR_STOP=1 -d postgres -c "GRANT CONNECT ON DATABASE \"$db\" TO backup_reader"
    echo "  $db: GRANT CONNECT"
  fi
done

echo "== Comprobación como backup_reader"
as_reader() { PGUSER=backup_reader PGPASSWORD="$(cat "$CREDS_DIR/backup_reader.password")" psql -X -At -v ON_ERROR_STOP=1 "$@"; }
for db in $(psql -X -At -d postgres -c "SELECT datname FROM pg_database WHERE datallowconn AND NOT datistemplate AND datname <> 'postgres' ORDER BY 1"); do
  as_reader -d "$db" -c "SELECT 1" > /dev/null || { echo "ERROR: backup_reader no entra en $db" >&2; exit 1; }
done
echo "  entra en todas las bases"
super=$(psql -X -At -d employee_db -c "SELECT count(*) FROM employees")
reader=$(as_reader -d employee_db -c "SELECT count(*) FROM employees")
[ "$super" = "$reader" ] || { echo "ERROR: backup_reader ve $reader filas de employees y el superusuario $super" >&2; exit 1; }
echo "  employees (FORCE RLS): ve las $reader filas, como el superusuario"
# Lo que impide escribir es que solo tiene SELECT; read only es una segunda
# barrera, que la sesión podría quitarse. Se prueban las dos: el DELETE ... WHERE
# false no borra nada, pero el permiso se comprueba igual.
[ "$(as_reader -d employee_db -c "SHOW default_transaction_read_only")" = on ] ||
  { echo "ERROR: default_transaction_read_only no está a on para backup_reader" >&2; exit 1; }
if as_reader -d employee_db -c "BEGIN READ WRITE; DELETE FROM employees WHERE false; ROLLBACK" > /tmp/w.out 2>&1 ||
   ! grep -q 'permission denied for table employees' /tmp/w.out; then
  echo "ERROR: el DELETE de prueba no ha dado 'permission denied':" >&2; cat /tmp/w.out >&2; exit 1
fi
echo "  no puede escribir: read only por defecto y, aun quitándolo, sin permiso"
echo "== Hecho"
