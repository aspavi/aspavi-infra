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
#   - CONNECT, uno a uno, en cada base que lee el CronJob: las de servicio
#     (*_db), keycloak (el realm de cada tenant) y postgres (las consultas de
#     catálogo de dump.sh). PUBLIC no tiene CONNECT en ninguna (lo quitó la
#     limpieza de app_user, ASP-4). En keycloak, el GRANT solo AÑADE una
#     entrada al ACL: se imprime antes y después, y el Job para si
#     keycloak_user no tiene CONNECT antes o no lo tiene después.
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

kc_user_connect() { psql -X -At -v ON_ERROR_STOP=1 -d postgres -c "
  SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'keycloak') THEN 'sin-base'
              WHEN NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'keycloak_user') THEN 'sin-rol'
              ELSE has_database_privilege('keycloak_user', 'keycloak', 'CONNECT')::text END"; }
kc_acl() { psql -X -At -v ON_ERROR_STOP=1 -d postgres -c "SELECT coalesce(datacl::text, '(por defecto)') FROM pg_database WHERE datname = 'keycloak'"; }

echo "== CONNECT en las bases que lee el CronJob"
before=$(kc_user_connect)
case "$before" in
  true) echo "  keycloak, ACL antes: $(kc_acl)" ;;
  sin-base) echo "  (no hay base keycloak)" ;;
  *) echo "ERROR: keycloak_user no tiene CONNECT en keycloak ($before) ANTES de tocar nada; no sigo" >&2; exit 1 ;;
esac
for db in $(psql -X -At -v ON_ERROR_STOP=1 -d postgres -c "
    SELECT datname FROM pg_database WHERE datallowconn AND NOT datistemplate ORDER BY 1"); do
  if [ "$(psql -X -At -d postgres -c "SELECT has_database_privilege('backup_reader', '$db', 'CONNECT')")" = t ]; then
    echo "  $db: ya tenía CONNECT"
  else
    psql -X -q -v ON_ERROR_STOP=1 -d postgres -c "GRANT CONNECT ON DATABASE \"$db\" TO backup_reader"
    echo "  $db: GRANT CONNECT"
  fi
done
if [ "$before" = true ]; then
  after=$(kc_user_connect)
  echo "  keycloak, ACL después: $(kc_acl)"
  [ "$after" = true ] || { echo "ERROR: keycloak_user ya NO tiene CONNECT en keycloak ($after): Keycloak no podría conectarse. Mirar el ACL de arriba" >&2; exit 1; }
  echo "  keycloak_user sigue con CONNECT en keycloak"
fi

echo "== Comprobación como backup_reader"
as_reader() { PGUSER=backup_reader PGPASSWORD="$(cat "$CREDS_DIR/backup_reader.password")" psql -X -At -v ON_ERROR_STOP=1 "$@"; }
for db in $(psql -X -At -d postgres -c "SELECT datname FROM pg_database WHERE datallowconn AND NOT datistemplate ORDER BY 1"); do
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
