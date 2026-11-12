#!/bin/sh
# Paso 2 del Job de usuarios de un servicio (ASP-4): roles, dueños y permisos
# en Postgres, como el superusuario. Idempotente. Ver README.md.
#
#   <svc>_owner  dueño de la base y de todos sus objetos; solo para Flyway
#   <svc>_app    la aplicación: LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB
#                NOCREATEROLE; DML en las tablas, USAGE/SELECT en las
#                secuencias, sin DDL; solo entra en su base
#
# 1. crea los roles si faltan, fija sus atributos y les pone la contraseña del
#    Secret (la deja el paso 1 en $CREDS_DIR)
# 2. crea la base si falta
# 3. dentro de la base, cada objeto de app_user pasa a <svc>_owner con su
#    ALTER ... OWNER. No se usa REASSIGN OWNED: reasigna también los objetos
#    compartidos, entre ellos las OTRAS bases de app_user
# 4. permisos de <svc>_app (DML en tablas, USAGE/SELECT en secuencias,
#    EXECUTE en funciones) y privilegios por defecto para lo que cree
#    <svc>_owner en el futuro (migraciones)
#    3 y 4 van en UNA transacción: si algo falla, la base queda como estaba
#    y app_user, que es con quien sigue corriendo el servicio, no pierde nada.
#    Las extensiones (pg_trgm, uuid-ossp) no tienen ALTER ... OWNER: siguen
#    siendo de app_user hasta la limpieza final de app_user (README)
# 2b. solo entonces la base pasa a ser de <svc>_owner; REVOKE CONNECT FROM
#    PUBLIC, CONNECT solo para los dos roles
# 5. comprueba y resume, sin secretos: dueños de las bases, que las demás
#    no han cambiado, atributos de los roles, que <svc>_app no puede hacer DDL
#    y en qué otras bases puede entrar
#
# La contraseña se lee en psql con \set y comillas invertidas (no va en la
# línea de comandos), con log_statement=none y log_min_error_statement=panic
# en la sesión para que no acabe en el log del servidor, y VERBOSITY terse
# para que un error no repita la sentencia.
set -eu

: "${SVC:?}" "${CREDS_DIR:=/creds}" "${LEGACY_OWNER:=app_user}"
prefix=$(echo "$SVC" | tr - _)
DB="${DB:-${prefix}_db}"  # collective-agreements -> collective_agreements_db
OWNER="${prefix}_owner"
APP="${prefix}_app"
# Solo la base del servicio que se le pasa, nunca otras: ni keycloak (en la
# misma instancia), ni postgres, ni las plantillas.
echo "$DB" | grep -Eq '^[a-z][a-z0-9_]*_db$' || { echo "ERROR: DB tiene que ser <servicio>_db: $DB" >&2; exit 1; }
for f in owner app; do [ -s "$CREDS_DIR/$f.password" ] || { echo "ERROR: falta $CREDS_DIR/$f.password" >&2; exit 1; }; done

PSQL="psql -X -q -v ON_ERROR_STOP=1 -v VERBOSITY=terse -v SHOW_CONTEXT=never -v owner=$OWNER -v app=$APP -v db=$DB -v legacy=$LEGACY_OWNER"
echo "== $SVC: base $DB, roles $OWNER y $APP"

# Dueño y permisos (datacl: quién tiene CONNECT) de todas las demás bases,
# keycloak incluida: al final tienen que estar igual que al principio.
other_owners() {
  psql -X -At -d postgres -c "SELECT datname || ' ' || pg_get_userbyid(datdba) || ' ' || coalesce(datacl::text, '(por defecto)') FROM pg_database WHERE datname <> '$DB' ORDER BY 1"
}
before=$(other_owners)

# 1 y 2: roles y base
$PSQL -d postgres <<SQL
SET log_statement = 'none';
SET log_min_error_statement = 'panic';
SELECT format('CREATE ROLE %I', r) FROM unnest(ARRAY[:'owner', :'app']) r
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = r) \gexec
ALTER ROLE :"owner" LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION;
ALTER ROLE :"app"   LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION;
\set pw \`cat $CREDS_DIR/owner.password\`
ALTER ROLE :"owner" PASSWORD :'pw';
\set pw \`cat $CREDS_DIR/app.password\`
ALTER ROLE :"app" PASSWORD :'pw';
\unset pw

SELECT format('CREATE DATABASE %I OWNER %I', :'db', :'owner')
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = :'db') \gexec
SQL

# 3 y 4: dentro de la base, todo o nada (-1: una sola transacción)
$PSQL -1 -d "$DB" <<'SQL'
SELECT EXISTS (SELECT FROM pg_roles WHERE rolname = :'legacy') AS has_legacy \gset

\if :has_legacy
-- esquemas
SELECT format('ALTER SCHEMA %I OWNER TO %I', nspname, :'owner')
FROM pg_namespace WHERE nspowner = :'legacy'::regrole \gexec
-- tablas, vistas, vistas materializadas, tablas foráneas; después las
-- secuencias sueltas (las de serial/identity van con su tabla)
SELECT format('ALTER %s %I.%I OWNER TO %I',
         CASE c.relkind WHEN 'v' THEN 'VIEW' WHEN 'm' THEN 'MATERIALIZED VIEW'
                        WHEN 'f' THEN 'FOREIGN TABLE' WHEN 'S' THEN 'SEQUENCE' ELSE 'TABLE' END,
         n.nspname, c.relname, :'owner')
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relowner = :'legacy'::regrole AND c.relkind IN ('r', 'p', 'v', 'm', 'f', 'S')
  AND NOT (c.relkind = 'S' AND EXISTS (
        SELECT FROM pg_depend d WHERE d.classid = 'pg_class'::regclass AND d.objid = c.oid AND d.deptype IN ('a', 'i')))
ORDER BY c.relkind = 'S', n.nspname, c.relname \gexec
-- funciones, procedimientos y agregados (no los de extensiones)
SELECT format('ALTER %s %s OWNER TO %I',
         CASE p.prokind WHEN 'a' THEN 'AGGREGATE' WHEN 'p' THEN 'PROCEDURE' ELSE 'FUNCTION' END,
         p.oid::regprocedure, :'owner')
FROM pg_proc p
WHERE p.proowner = :'legacy'::regrole
  AND NOT EXISTS (SELECT FROM pg_depend d WHERE d.classid = 'pg_proc'::regclass AND d.objid = p.oid AND d.deptype = 'e') \gexec
-- tipos propios: enum, dominio, rango y compuesto suelto
SELECT format('ALTER %s %s OWNER TO %I', CASE t.typtype WHEN 'd' THEN 'DOMAIN' ELSE 'TYPE' END, t.oid::regtype, :'owner')
FROM pg_type t
WHERE t.typowner = :'legacy'::regrole
  AND (t.typtype IN ('e', 'd', 'r') OR (t.typtype = 'c' AND (SELECT relkind FROM pg_class WHERE oid = t.typrelid) = 'c'))
  AND NOT EXISTS (SELECT FROM pg_depend d WHERE d.classid = 'pg_type'::regclass AND d.objid = t.oid AND d.deptype = 'e') \gexec

-- lo que siga siendo de app_user en esta base (las bases y los roles son
-- compartidos y no salen aquí): si queda algo, el Job falla y lo lista.
-- Las extensiones se avisan pero no cuentan: no hay ALTER EXTENSION ...
-- OWNER, y su dueño no da acceso a los datos
SELECT coalesce(string_agg(extname, ', '), '') AS legacy_ext FROM pg_extension WHERE extowner = :'legacy'::regrole \gset
SELECT :'legacy_ext' <> '' AS has_legacy_ext \gset
\if :has_legacy_ext
\echo AVISO: extensiones que siguen siendo de :legacy (se pasan al limpiar app_user, ver README): :legacy_ext
\endif
SELECT coalesce(string_agg(pg_describe_object(classid, objid, objsubid), ', '), '') AS leftover
FROM pg_shdepend
WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
  AND refclassid = 'pg_authid'::regclass AND refobjid = :'legacy'::regrole AND deptype = 'o'
  AND classid <> 'pg_extension'::regclass \gset
SELECT :'leftover' <> '' AS has_leftover \gset
\if :has_leftover
\echo ERROR: siguen siendo de :legacy en esta base: :leftover
\echo (no se ha cambiado nada en la base: la transacción se deshace)
DO $$ BEGIN RAISE EXCEPTION 'quedan objetos de app_user en la base'; END $$;
\endif
\endif

-- 4. permisos de la aplicación, en cada esquema que no es del sistema
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
SELECT format('GRANT USAGE ON SCHEMA %1$I TO %2$I;'
           || 'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA %1$I TO %2$I;'
           || 'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA %1$I TO %2$I;'
           || 'GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA %1$I TO %2$I;'
           || 'ALTER DEFAULT PRIVILEGES FOR ROLE %3$I IN SCHEMA %1$I GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO %2$I;'
           || 'ALTER DEFAULT PRIVILEGES FOR ROLE %3$I IN SCHEMA %1$I GRANT USAGE, SELECT ON SEQUENCES TO %2$I;'
           || 'ALTER DEFAULT PRIVILEGES FOR ROLE %3$I IN SCHEMA %1$I GRANT EXECUTE ON FUNCTIONS TO %2$I',
              nspname, :'app', :'owner')
FROM pg_namespace
WHERE nspname NOT IN ('information_schema', 'pg_catalog', 'pg_toast') AND nspname NOT LIKE 'pg\_temp\_%'
  AND nspname NOT LIKE 'pg\_toast\_temp\_%' \gexec
-- el historial de Flyway es cosa del owner
SELECT format('REVOKE ALL ON TABLE %s FROM %I', c.oid::regclass, :'app')
FROM pg_class c WHERE c.relname = 'flyway_schema_history' AND c.relkind IN ('r', 'p') \gexec
SQL

# 2b. la base, ahora sí, pasa a <svc>_owner y solo entran sus dos roles
$PSQL -d postgres <<'SQL'
ALTER DATABASE :"db" OWNER TO :"owner";
REVOKE CONNECT ON DATABASE :"db" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"db" TO :"owner", :"app";
SQL

# 5. comprobaciones y resumen
echo "-- dueños de las bases"
psql -X -At -F ' ' -d postgres -c "SELECT '  ' || datname, pg_get_userbyid(datdba) FROM pg_database WHERE NOT datistemplate ORDER BY 1"
after=$(other_owners)
if [ "$before" != "$after" ]; then
  echo "ERROR: ha cambiado el dueño o los permisos de otra base" >&2
  printf '%s\n' "$before" > /tmp/before; printf '%s\n' "$after" > /tmp/after; diff /tmp/before /tmp/after >&2 || true
  exit 1
fi
echo "  (las demás bases, keycloak incluida, conservan su dueño y sus permisos)"

echo "-- roles"
psql -X -At -d postgres -v owner="$OWNER" -v app="$APP" <<'SQL'
SELECT format('  %s: login=%s superuser=%s bypassrls=%s createdb=%s createrole=%s replication=%s',
              rolname, rolcanlogin, rolsuper, rolbypassrls, rolcreatedb, rolcreaterole, rolreplication)
FROM pg_roles WHERE rolname IN (:'owner', :'app') ORDER BY 1;
SQL
bad=$(psql -X -At -d postgres -c "SELECT count(*) FROM pg_roles WHERE rolname IN ('$OWNER', '$APP')
  AND (rolsuper OR rolbypassrls OR rolcreatedb OR rolcreaterole OR rolreplication OR NOT rolcanlogin)")
[ "$bad" = 0 ] || { echo "ERROR: algún rol tiene atributos de más" >&2; exit 1; }

as_app() { # psql como <svc>_app, con su contraseña en el entorno del proceso, no en la línea
  PGUSER="$APP" PGPASSWORD="$(cat "$CREDS_DIR/app.password")" psql -X -At -v ON_ERROR_STOP=1 -v VERBOSITY=terse "$@"
}
echo "-- $APP"
as_app -d "$DB" -c "SELECT '  entra en $DB como ' || current_user" || { echo "ERROR: $APP no entra en $DB" >&2; exit 1; }
if as_app -d "$DB" -c "CREATE TABLE asp4_ddl_probe (id int)" 2>/dev/null; then
  as_app -d "$DB" -c "DROP TABLE asp4_ddl_probe" > /dev/null 2>&1 || true
  echo "ERROR: $APP ha podido crear una tabla en $DB" >&2; exit 1
fi
echo "  DDL en $DB: denegado (CREATE TABLE falla)"
tables=$(psql -X -At -d "$DB" -c "SELECT count(*) FROM pg_tables WHERE schemaname NOT IN ('pg_catalog', 'information_schema') AND tablename <> 'flyway_schema_history'")
# has_table_privilege con varios privilegios separados por comas da true si
# tiene CUALQUIERA de ellos: hay que preguntar uno a uno y unirlos con AND.
granted=$(psql -X -At -d "$DB" -c "SELECT count(*) FROM pg_tables WHERE schemaname NOT IN ('pg_catalog', 'information_schema') AND tablename <> 'flyway_schema_history'
  AND has_table_privilege('$APP', format('%I.%I', schemaname, tablename), 'SELECT')
  AND has_table_privilege('$APP', format('%I.%I', schemaname, tablename), 'INSERT')
  AND has_table_privilege('$APP', format('%I.%I', schemaname, tablename), 'UPDATE')
  AND has_table_privilege('$APP', format('%I.%I', schemaname, tablename), 'DELETE')")
echo "  DML: en $granted de $tables tablas (sin flyway_schema_history)"
[ "$granted" = "$tables" ] || { echo "ERROR: faltan permisos en alguna tabla" >&2; exit 1; }
# EXECUTE en las funciones de la base (las de los relays del outbox, como
# outbox_pending_tenants(), son SECURITY DEFINER y la aplicación tiene que
# poder llamarlas); no cuentan las de extensiones
routines="FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
  WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
  AND NOT EXISTS (SELECT FROM pg_depend d WHERE d.classid = 'pg_proc'::regclass AND d.objid = p.oid AND d.deptype = 'e')"
funcs=$(psql -X -At -d "$DB" -c "SELECT count(*) $routines")
missing=$(psql -X -At -d "$DB" -c "SELECT coalesce(string_agg(p.oid::regprocedure::text, ', '), '') $routines
  AND NOT has_function_privilege('$APP', p.oid, 'EXECUTE')")
definer=$(psql -X -At -d "$DB" -c "SELECT coalesce(string_agg(p.oid::regprocedure::text, ', '), '-') $routines AND p.prosecdef")
echo "  EXECUTE: en $funcs funciones (SECURITY DEFINER: $definer)"
[ -z "$missing" ] || { echo "ERROR: $APP no puede ejecutar: $missing" >&2; exit 1; }
for other in $(psql -X -At -d postgres -c "SELECT datname FROM pg_database WHERE datallowconn AND NOT datistemplate AND datname <> '$DB' ORDER BY 1"); do
  if as_app -d "$other" -c "SELECT 1" > /dev/null 2>&1; then
    echo "  $other: PUEDE conectarse (la base aún da CONNECT a PUBLIC)"
  else
    echo "  $other: no puede conectarse"
  fi
done
echo "== $SVC: hecho"
