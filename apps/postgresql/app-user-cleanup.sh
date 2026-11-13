#!/bin/sh
# Limpieza final de app_user (ASP-4 fase 1, paso 4), como el superusuario.
# Idempotente: si no queda nada por hacer, solo inventaría y comprueba. Ver
# README.md, "Limpieza de app_user".
#
#   1. inventario (solo lectura) y paradas: sesiones abiertas como app_user,
#      algo suyo que no sea la base postgres, su esquema public o las dos
#      extensiones, bases o roles que no están como deberían. Inventaría
#      también los roles que no son de ningún servicio (no los toca)
#   2. en la base postgres, en UNA transacción:
#      - la base postgres pasa de app_user a postgres, y su esquema public
#        (de app_user por Bitnami) a pg_database_owner, como viene por defecto
#      - REVOKE CONNECT FROM PUBLIC en keycloak, postgres y las 9 bases que
#        aún lo tengan; keycloak_user y el superusuario tienen que seguir
#        pudiendo conectarse
#      - app_user NOCREATEDB (no se borra ni se le cambia la contraseña)
#      - compara dueños y ACL de todas las bases con los de antes: solo
#        pueden cambiar el dueño de postgres y el CONNECT de PUBLIC
#   3. en payroll_db y collective_agreements_db, una transacción cada una:
#      REASSIGN OWNED BY app_user TO <svc>_owner, solo si app_user ya no es
#      dueño de ninguna base ni tablespace (si no, también se las llevaría),
#      y solo si lo único suyo en la base es la extensión esperada. Después,
#      ninguna base ha cambiado
#   3b. en cada base, una transacción: los permisos de app_user en el esquema
#      public se revocan (en employee_db tenía un GRANT). El ACL del esquema
#      tiene que quedar igual que antes salvo las entradas de app_user
#   4. comprobaciones (solo lectura): dueños, la matriz de CONNECT, que
#      app_user no es dueño de nada ni tiene permisos en ningún esquema
#      public, y el diff de bases de antes y después
#
# No hay ALTER EXTENSION ... OWNER en Postgres 18, y DROP/CREATE de la
# extensión se llevaría el DEFAULT de uuid-ossp y el índice GIN de pg_trgm:
# REASSIGN es la única vía soportada, y por eso va después del paso 2.
# Las bases son distintas y no caben en una sola transacción: si el paso 3
# falla, el 2 queda hecho, y volver a lanzar el Job sigue desde ahí.
set -eu

L=app_user                   # el usuario que se limpia
KC_DB=keycloak KC_USER=keycloak_user
SUPER=postgres
SVCS="absence attendance audit collective_agreements contract employee events organization payroll"
# base -> extensión de app_user que se espera en ella (y que pasa a <svc>_owner)
EXPECTED_EXT="payroll_db:uuid-ossp collective_agreements_db:pg_trgm"
# base:objeto (con + en vez de espacio) que puede ser de app_user al empezar;
# cualquier otra cosa para el Job en el paso 1
EXPECTED_OWNED="payroll_db:extension+uuid-ossp collective_agreements_db:extension+pg_trgm postgres:schema+public"

# Ninguna sentencia de esta sesión al log del servidor (no hay secretos, pero
# así queda igual que db-users.sh)
export PGOPTIONS="-c log_statement=none -c log_min_error_statement=panic"
PSQL="psql -X -q -v ON_ERROR_STOP=1 -v VERBOSITY=terse -v SHOW_CONTEXT=never -v legacy=$L"
Q="psql -X -At -v ON_ERROR_STOP=1 -v VERBOSITY=terse"
RO="env PGOPTIONS=-cdefault_transaction_read_only=on $Q"   # consultas de solo lectura
fail() { echo "ERROR: $*" >&2; exit 1; }
# roles conocidos: el superusuario, app_user, keycloak_user y los de los 9
# servicios. Los demás se inventarían aparte
known="'$SUPER','$L','$KC_USER'$(for s in $SVCS; do printf ",'%s_owner','%s_app'" "$s" "$s"; done)"
svc_dbs=$(for s in $SVCS; do printf '%s_db,' "$s"; done)
targets="${svc_dbs}$KC_DB,postgres"   # bases en las que PUBLIC pierde CONNECT

# Dueño y ACL de cada base, para el diff del log
db_state() {
  $RO -d postgres -F ' ' -c "SELECT datname, pg_get_userbyid(datdba), coalesce(datacl::text, '(por defecto: PUBLIC=Tc)')
    FROM pg_database WHERE NOT datistemplate ORDER BY 1"
}

echo "== 1. inventario de $L (solo lectura)"
if [ "$($RO -d postgres -c "SELECT count(*) FROM pg_roles WHERE rolname = '$L'")" = 0 ]; then
  echo "  $L no existe: no hay nada que limpiar"; exit 0
fi
echo "-- atributos"
$RO -d postgres -c "SELECT format('  login=%s superuser=%s createdb=%s createrole=%s replication=%s bypassrls=%s connlimit=%s',
  rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls, rolconnlimit) FROM pg_roles WHERE rolname = '$L'"
[ "$($RO -d postgres -c "SELECT rolsuper FROM pg_roles WHERE rolname = '$L'")" = f ] || fail "$L es superusuario: no se esperaba"
echo "-- pertenencia a roles y ajustes por rol"
$RO -d postgres -c "SELECT '  miembro de ' || roleid::regrole FROM pg_auth_members WHERE member = '$L'::regrole
  UNION ALL SELECT '  tiene como miembro a ' || member::regrole FROM pg_auth_members WHERE roleid = '$L'::regrole
  UNION ALL SELECT '  ajuste en ' || coalesce((SELECT datname FROM pg_database WHERE oid = setdatabase), '(todas)') || ': '
               || array_to_string(setconfig, ', ') FROM pg_db_role_setting WHERE setrole = '$L'::regrole"
echo "-- sesiones abiertas como $L"
# Tres muestras: las sondas de la StatefulSet (pg_isready -U app_user) abren
# una conexión de milisegundos sin autenticarse; una sesión de verdad sigue ahí
n=0
for i in 1 2 3; do
  s=$($RO -d postgres -c "SELECT count(*) FROM pg_stat_activity WHERE usename = '$L' AND pid <> pg_backend_pid()")
  [ "$s" = 0 ] && break
  n=$((n + 1)); [ "$i" = 3 ] || sleep 2
done
if [ "$n" = 3 ]; then
  $RO -d postgres -F ' ' -c "SELECT '  ', datname, coalesce(nullif(application_name, ''), '-'), coalesce(client_addr::text, 'local'), state
    FROM pg_stat_activity WHERE usename = '$L'"
  fail "hay sesiones abiertas como $L: algo lo sigue usando"
fi
echo "  ninguna"
echo "-- bases: dueño, ACL y si PUBLIC puede conectarse"
$RO -d postgres -F ' ' -c "SELECT '  ' || datname, pg_get_userbyid(datdba),
  CASE WHEN has_database_privilege('public', oid, 'CONNECT') THEN 'PUBLIC:CONNECT' ELSE 'PUBLIC:-' END, coalesce(datacl::text, '(por defecto)')
  FROM pg_database WHERE NOT datistemplate ORDER BY 1"
before=$(db_state)
echo "-- objetos compartidos de $L (bases, tablespaces)"
shared=$($RO -d postgres -c "SELECT pg_describe_object(classid, objid, objsubid) FROM pg_shdepend
  WHERE dbid = 0 AND refclassid = 'pg_authid'::regclass AND refobjid = '$L'::regrole AND deptype = 'o' ORDER BY 1")
printf '  %s\n' "${shared:-ninguno}"
case "$shared" in ''|'database postgres') ;; *) fail "$L es dueño de objetos compartidos que no son la base postgres" ;; esac
$RO -d postgres -c "SELECT '  permiso en ' || pg_describe_object(classid, objid, objsubid) FROM pg_shdepend
  WHERE dbid = 0 AND refclassid = 'pg_authid'::regclass AND refobjid = '$L'::regrole AND deptype <> 'o'"
echo "-- otros roles (ni del superusuario, ni de app_user, ni de keycloak_user, ni de los 9 servicios): solo se inventarían"
# contraseña=t/f dice si tiene una puesta, nunca cuál
$RO -d postgres -c "SELECT format('  %s: login=%s contraseña=%s superuser=%s createdb=%s createrole=%s replication=%s bypassrls=%s caduca=%s miembro_de=%s sesiones=%s',
    a.rolname, a.rolcanlogin, a.rolpassword IS NOT NULL, a.rolsuper, a.rolcreatedb, a.rolcreaterole, a.rolreplication, a.rolbypassrls,
    coalesce(a.rolvaliduntil::text, '-'),
    coalesce((SELECT string_agg(m.roleid::regrole::text, ',') FROM pg_auth_members m WHERE m.member = a.oid), '-'),
    (SELECT count(*) FROM pg_stat_activity s WHERE s.usename = a.rolname))
  || coalesce(' entra_en=' || (SELECT string_agg(d.datname, ',' ORDER BY d.datname) FROM pg_database d
                               WHERE d.datallowconn AND NOT d.datistemplate AND has_database_privilege(a.oid, d.oid, 'CONNECT')), ' entra_en=-')
  || coalesce(' crea_esquemas_en=' || (SELECT string_agg(d.datname, ',' ORDER BY d.datname) FROM pg_database d
                               WHERE d.datallowconn AND NOT d.datistemplate AND has_database_privilege(a.oid, d.oid, 'CREATE')), '')
  || coalesce(' dueño_de=' || (SELECT string_agg(pg_describe_object(classid, objid, objsubid), ',') FROM pg_shdepend
                               WHERE dbid = 0 AND refclassid = 'pg_authid'::regclass AND refobjid = a.oid AND deptype = 'o'), '')
  FROM pg_authid a WHERE a.rolname !~ '^pg_' AND a.rolname NOT IN ($known) ORDER BY a.rolname"

for db in $($RO -d postgres -c "SELECT datname FROM pg_database WHERE datallowconn AND NOT datistemplate ORDER BY 1"); do
  echo "-- base $db"
  owned=$($RO -d "$db" -c "SELECT pg_describe_object(classid, objid, objsubid) FROM pg_shdepend
    WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
      AND refclassid = 'pg_authid'::regclass AND refobjid = '$L'::regrole AND deptype = 'o' ORDER BY 1")
  printf '  dueño de: %s\n' "${owned:-nada}"
  $RO -d "$db" -c "SELECT CASE deptype WHEN 'a' THEN '  permiso en ' WHEN 'r' THEN '  en política ' ELSE '  (' || deptype::text || ') ' END
      || pg_describe_object(classid, objid, objsubid)
    FROM pg_shdepend WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
      AND refclassid = 'pg_authid'::regclass AND refobjid = '$L'::regrole AND deptype <> 'o' ORDER BY 1"
  $RO -d "$db" -c "SELECT '  extensión ' || extname || ' (dueño ' || pg_get_userbyid(extowner) || ')' FROM pg_extension WHERE extname <> 'plpgsql' ORDER BY 1"
  $RO -d "$db" -c "SELECT '  privilegios por defecto de $L: ' || count(*) FROM pg_default_acl WHERE defaclrole = '$L'::regrole"
  # los otros roles en esta base: de qué son dueños y en qué tienen permisos
  $RO -d "$db" -c "SELECT format('  otro rol %s: dueño de %s, permisos en %s%s', r.rolname,
        count(*) FILTER (WHERE d.deptype = 'o'), count(*) FILTER (WHERE d.deptype = 'a'),
        ' (' || left(string_agg(pg_describe_object(d.classid, d.objid, d.objsubid), ', ' ORDER BY d.deptype, d.objid), 300) || ')')
    FROM pg_shdepend d JOIN pg_roles r ON r.oid = d.refobjid
    WHERE d.dbid = (SELECT oid FROM pg_database WHERE datname = current_database()) AND d.refclassid = 'pg_authid'::regclass
      AND r.rolname !~ '^pg_' AND r.rolname NOT IN ($known)
    GROUP BY r.rolname ORDER BY 1"
  unexpected=$(printf '%s\n' "$owned" | while IFS= read -r o; do
    [ -n "$o" ] || continue
    ok=0
    for e in $EXPECTED_OWNED; do
      [ "${e%%:*}" = "$db" ] && [ "$(printf '%s' "${e#*:}" | tr + ' ')" = "$o" ] && ok=1
    done
    [ "$ok" = 1 ] || printf '%s; ' "$o"
  done)
  [ -z "$unexpected" ] || fail "$L es dueño de algo inesperado en $db: $unexpected"
done

echo "-- comprobaciones previas"
for s in $SVCS; do
  o=$($RO -d postgres -c "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = '${s}_db'")
  [ "$o" = "${s}_owner" ] || fail "${s}_db no es de ${s}_owner (es de '${o:-no existe}'): ese servicio no ha migrado"
  [ "$($RO -d postgres -c "SELECT count(*) FROM pg_roles WHERE rolname = '${s}_app'")" = 1 ] || fail "no existe ${s}_app"
done
o=$($RO -d postgres -c "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = '$KC_DB'")
[ "$o" = "$KC_USER" ] || fail "la base $KC_DB no es de $KC_USER (es de '${o:-no existe}')"
o=$($RO -d postgres -c "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = 'postgres'")
[ "$o" = "$L" ] || [ "$o" = "$SUPER" ] || fail "la base postgres es de $o: se esperaba $L o $SUPER"
echo "  las 9 bases son de su <svc>_owner, $KC_DB de $KC_USER y postgres de $o"

echo "== 2. base postgres, CONNECT de PUBLIC y CREATEDB (una transacción)"
$PSQL -1 -d postgres -v targets="$targets" -v kc_db=$KC_DB -v kc_user=$KC_USER -v super=$SUPER <<'SQL'
-- dueño y ACL de antes, normalizados: el dueño de cada base sale como OWNER,
-- PUBLIC como PUBLIC, y una ACL nula como la que tendría por defecto
CREATE TEMP VIEW db_acl AS
  SELECT d.datname, d.datdba,
         CASE e.grantee WHEN 0 THEN 'PUBLIC' WHEN d.datdba THEN 'OWNER' ELSE pg_get_userbyid(e.grantee) END AS grantee,
         e.privilege_type || CASE WHEN e.is_grantable THEN '*' ELSE '' END AS priv
  FROM pg_database d, aclexplode(coalesce(d.datacl, acldefault('d', d.datdba))) e;
CREATE TEMP TABLE before_db AS SELECT oid, datname, datdba FROM pg_database;
CREATE TEMP TABLE before_acl AS SELECT datname, grantee, priv FROM db_acl;

SELECT format('ALTER DATABASE postgres OWNER TO %I', :'super')
FROM pg_database WHERE datname = 'postgres' AND datdba = :'legacy'::regrole \gexec
-- su esquema public, al dueño por defecto desde PG 15 (pg_database_owner: el
-- dueño de la base, que ahora es el superusuario)
SELECT 'ALTER SCHEMA public OWNER TO pg_database_owner'
FROM pg_namespace WHERE nspname = 'public' AND nspowner = :'legacy'::regrole \gexec
SELECT format('REVOKE CONNECT ON DATABASE %I FROM PUBLIC', datname)
FROM pg_database WHERE datname = ANY (string_to_array(:'targets', ',')) AND has_database_privilege('public', oid, 'CONNECT')
ORDER BY 1 \gexec
SELECT format('ALTER ROLE %I NOCREATEDB', rolname) FROM pg_roles WHERE rolname = :'legacy' AND rolcreatedb \gexec

-- Keycloak (el login de test y acme) tiene que seguir entrando en su base
SELECT has_database_privilege(:'kc_user', :'kc_db', 'CONNECT') AS kc_ok \gset
\if :kc_ok
\echo '  ' :kc_user 'sigue pudiendo conectarse a' :kc_db
\else
\echo ERROR: :kc_user ya no podría conectarse a :kc_db: se deshace todo
DO $$ BEGIN RAISE EXCEPTION 'keycloak perdería CONNECT'; END $$;
\endif
SELECT has_database_privilege(:'super', 'postgres', 'CONNECT') AS su_ok \gset
\if :su_ok
\echo '  ' :super 'sigue pudiendo conectarse a postgres'
\else
\echo ERROR: :super ya no podría conectarse a postgres: se deshace todo
DO $$ BEGIN RAISE EXCEPTION 'el superusuario perdería CONNECT'; END $$;
\endif

-- Lo único que puede haber cambiado: el dueño de postgres (de app_user al
-- superusuario) y el CONNECT de PUBLIC en las bases de la lista
SELECT coalesce(string_agg(x, '; '), '') AS unexpected FROM (
  SELECT format('base %s: aparece o desaparece', coalesce(a.datname, b.datname)) x
  FROM pg_database a FULL JOIN before_db b ON a.oid = b.oid WHERE a.oid IS NULL OR b.oid IS NULL
  UNION ALL
  SELECT format('base %s: dueño %s -> %s', a.datname, pg_get_userbyid(b.datdba), pg_get_userbyid(a.datdba))
  FROM pg_database a JOIN before_db b ON a.oid = b.oid
  WHERE a.datdba <> b.datdba AND NOT (a.datname = 'postgres' AND b.datdba = :'legacy'::regrole AND a.datdba = :'super'::regrole)
  UNION ALL
  (SELECT format('base %s: ACL %s %s', datname, grantee, priv) FROM (
     (SELECT datname, grantee, priv FROM before_acl
      WHERE NOT (grantee = 'PUBLIC' AND priv = 'CONNECT' AND datname = ANY (string_to_array(:'targets', ','))))
     EXCEPT SELECT datname, grantee, priv FROM db_acl) gone
   UNION ALL
   SELECT format('base %s: ACL nueva %s %s', datname, grantee, priv) FROM (
     SELECT datname, grantee, priv FROM db_acl EXCEPT SELECT datname, grantee, priv FROM before_acl) added)
) u \gset
SELECT :'unexpected' <> '' AS has_unexpected \gset
\if :has_unexpected
\echo ERROR: cambios no esperados: :unexpected
DO $$ BEGIN RAISE EXCEPTION 'cambios no esperados en las bases'; END $$;
\endif
SELECT CASE WHEN count(*) = 0 THEN 'nada que cambiar' ELSE 'hecho' END AS status
FROM (SELECT 1 FROM before_db b JOIN pg_database a ON a.oid = b.oid WHERE a.datdba <> b.datdba
      UNION ALL SELECT 1 FROM (SELECT datname, grantee, priv FROM before_acl
                               EXCEPT SELECT datname, grantee, priv FROM db_acl) g) c \gset
\echo '  ' :status
SQL
# después de la transacción: en la base postgres, app_user ya no es dueño de nada
left=$($RO -d postgres -c "SELECT coalesce(string_agg(pg_describe_object(classid, objid, objsubid), ', '), '') FROM pg_shdepend
  WHERE dbid IN (0, (SELECT oid FROM pg_database WHERE datname = 'postgres')) AND refclassid = 'pg_authid'::regclass
    AND refobjid = '$L'::regrole AND deptype = 'o'")
[ -z "$left" ] || fail "en la base postgres (o compartido), $L sigue siendo dueño de: $left"
echo "  base postgres: $L ya no es dueño de nada"

echo "== 3. extensiones de $L (REASSIGN, una transacción por base)"
for e in $EXPECTED_EXT; do
  db=${e%%:*} ext=${e#*:}
  owner="${db%_db}_owner"
  $PSQL -1 -d "$db" -v owner="$owner" -v ext="$ext" -v db="$db" <<'SQL'
-- REASSIGN OWNED también reasigna los objetos compartidos de app_user (sus
-- bases y tablespaces): solo si ya no tiene ninguno
SELECT (SELECT count(*) FROM pg_database WHERE datdba = :'legacy'::regrole)
     + (SELECT count(*) FROM pg_tablespace WHERE spcowner = :'legacy'::regrole) AS shared \gset
SELECT :shared > 0 AS has_shared \gset
\if :has_shared
\echo ERROR: :legacy aún es dueño de alguna base o tablespace: REASSIGN se la llevaría
DO $$ BEGIN RAISE EXCEPTION 'app_user aún tiene objetos compartidos'; END $$;
\endif
SELECT coalesce(string_agg(pg_describe_object(classid, objid, objsubid), ', '), '') AS owned FROM pg_shdepend
WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
  AND refclassid = 'pg_authid'::regclass AND refobjid = :'legacy'::regrole AND deptype = 'o' \gset
SELECT :'owned' = '' AS nothing, :'owned' = 'extension ' || :'ext' AS expected \gset
\if :nothing
\echo '  ' :db ': nada de' :legacy
\elif :expected
CREATE TEMP TABLE before_db AS SELECT oid, datname, datdba, datacl::text AS acl FROM pg_database;
REASSIGN OWNED BY :"legacy" TO :"owner";
SELECT coalesce(string_agg(datname, ', '), '') AS changed FROM (
  SELECT datname, datdba, datacl::text FROM pg_database
  EXCEPT SELECT datname, datdba, acl FROM before_db) c \gset
SELECT :'changed' <> '' AS has_changed \gset
\if :has_changed
\echo ERROR: REASSIGN ha cambiado otras bases: :changed
DO $$ BEGIN RAISE EXCEPTION 'REASSIGN ha tocado otras bases'; END $$;
\endif
SELECT pg_get_userbyid(extowner) = :'owner' AS ext_ok FROM pg_extension WHERE extname = :'ext' \gset
SELECT NOT EXISTS (SELECT FROM pg_shdepend WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
  AND refclassid = 'pg_authid'::regclass AND refobjid = :'legacy'::regrole AND deptype = 'o') AS clean \gset
\if :ext_ok
\else
DO $$ BEGIN RAISE EXCEPTION 'la extensión no ha cambiado de dueño'; END $$;
\endif
\if :clean
\else
DO $$ BEGIN RAISE EXCEPTION 'quedan objetos de app_user'; END $$;
\endif
\echo '  ' :db ': extensión' :ext 'pasa a' :owner
\else
\echo ERROR: en :db :legacy es dueño de: :owned
DO $$ BEGIN RAISE EXCEPTION 'objetos inesperados de app_user'; END $$;
\endif
SQL
done

echo "== 3b. permisos de $L en el esquema public (una transacción por base)"
for db in $($RO -d postgres -c "SELECT datname FROM pg_database WHERE datallowconn AND NOT datistemplate ORDER BY 1"); do
  $PSQL -1 -d "$db" -v db="$db" <<'SQL'
SELECT EXISTS (SELECT FROM pg_namespace n, aclexplode(n.nspacl) e
               WHERE n.nspname = 'public' AND e.grantee = :'legacy'::regrole) AS has_grant \gset
\if :has_grant
-- ACL del esquema de antes, sin las entradas de app_user: tiene que quedar así
CREATE TEMP TABLE before_acl AS
  SELECT e.grantor, e.grantee, e.privilege_type, e.is_grantable
  FROM pg_namespace n, aclexplode(n.nspacl) e WHERE n.nspname = 'public' AND e.grantee <> :'legacy'::regrole;
-- Un superusuario revoca como el dueño del esquema, así que solo quita lo que
-- concedió el dueño. Si lo concedió otro rol, se para y se mira a mano (SET
-- ROLE a ese rol no es fiable: si es dueño de la base, también es miembro de
-- pg_database_owner, y Postgres puede revocar como cualquiera de los dos).
-- Sin CASCADE: si app_user lo había vuelto a conceder a otros, falla
SELECT coalesce(string_agg(DISTINCT e.grantor::regrole::text, ', '), '') AS other_grantors
FROM pg_namespace n, aclexplode(n.nspacl) e
WHERE n.nspname = 'public' AND e.grantee = :'legacy'::regrole AND e.grantor <> n.nspowner \gset
SELECT :'other_grantors' <> '' AS has_other \gset
\if :has_other
\echo ERROR: :db ', esquema public: permisos de' :legacy 'concedidos por' :other_grantors '(no por el dueño): revisar a mano'
DO $$ BEGIN RAISE EXCEPTION 'permisos concedidos por otro rol'; END $$;
\endif
SELECT format('REVOKE ALL ON SCHEMA public FROM %I', :'legacy') \gexec
SELECT coalesce(string_agg(x, '; '), '') AS unexpected FROM (
  SELECT 'sigue: ' || e.privilege_type x FROM pg_namespace n, aclexplode(n.nspacl) e
  WHERE n.nspname = 'public' AND e.grantee = :'legacy'::regrole
  UNION ALL
  SELECT format('cambia: %s -> %s %s', grantor::regrole, grantee::regrole, privilege_type) FROM (
    (SELECT * FROM before_acl
     EXCEPT SELECT e.grantor, e.grantee, e.privilege_type, e.is_grantable
            FROM pg_namespace n, aclexplode(n.nspacl) e WHERE n.nspname = 'public')
    UNION ALL
    (SELECT e.grantor, e.grantee, e.privilege_type, e.is_grantable
     FROM pg_namespace n, aclexplode(n.nspacl) e WHERE n.nspname = 'public'
     EXCEPT SELECT * FROM before_acl)) c
) u \gset
SELECT :'unexpected' <> '' AS has_unexpected \gset
\if :has_unexpected
\echo ERROR: :db ', esquema public:' :unexpected
DO $$ BEGIN RAISE EXCEPTION 'el ACL del esquema public no queda como debe'; END $$;
\endif
\echo '  ' :db ': revocados los permisos de' :legacy 'en el esquema public'
\endif
SQL
done
echo "  (en las demás bases no tenía ninguno)"

echo "== 4. comprobaciones (solo lectura)"
echo "-- dueños"
$RO -d postgres -F ' ' -c "SELECT '  ' || datname, pg_get_userbyid(datdba) FROM pg_database WHERE NOT datistemplate ORDER BY 1"
for s in $SVCS; do
  [ "$($RO -d postgres -c "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = '${s}_db'")" = "${s}_owner" ] || fail "${s}_db ha cambiado de dueño"
done
[ "$($RO -d postgres -c "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = '$KC_DB'")" = "$KC_USER" ] || fail "$KC_DB ha cambiado de dueño"
[ "$($RO -d postgres -c "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = 'postgres'")" = "$SUPER" ] || fail "postgres no es de $SUPER"

echo "-- CONNECT: en qué bases puede entrar cada rol"
bad=0
for r in $(for s in $SVCS; do printf '%s_app %s_owner ' "$s" "$s"; done) $KC_USER $L; do
  can=$($RO -d postgres -c "SELECT coalesce(string_agg(datname, ' ' ORDER BY datname), '-') FROM pg_database
    WHERE datallowconn AND NOT datistemplate AND has_database_privilege('$r', oid, 'CONNECT')")
  case "$r" in
    *_app|*_owner) want="${r%_*}_db" ;;
    "$KC_USER") want=$KC_DB ;;
    *) want=- ;;
  esac
  if [ "$can" = "$want" ]; then mark=ok; else mark="MAL (se esperaba $want)"; bad=1; fi
  printf '  %-30s %-28s %s\n' "$r" "$can" "$mark"
done
[ "$bad" = 0 ] || fail "algún rol puede conectarse donde no debe"
$RO -d postgres -c "SELECT format('  %-30s %-28s (otro rol, sin comprobar)', a.rolname,
    coalesce((SELECT string_agg(d.datname, ' ' ORDER BY d.datname) FROM pg_database d
              WHERE d.datallowconn AND NOT d.datistemplate AND has_database_privilege(a.oid, d.oid, 'CONNECT')), '-'))
  FROM pg_roles a WHERE a.rolname !~ '^pg_' AND a.rolname NOT IN ($known) ORDER BY 1"

echo "-- $L"
$RO -d postgres -c "SELECT format('  createdb=%s superuser=%s login=%s', rolcreatedb, rolsuper, rolcanlogin) FROM pg_roles WHERE rolname = '$L'"
[ "$($RO -d postgres -c "SELECT rolcreatedb FROM pg_roles WHERE rolname = '$L'")" = f ] || fail "$L sigue con CREATEDB"
left=0
for db in $($RO -d postgres -c "SELECT datname FROM pg_database WHERE datallowconn ORDER BY 1"); do
  c=$($RO -d "$db" -c "SELECT count(*) FROM pg_shdepend WHERE dbid IN (0, (SELECT oid FROM pg_database WHERE datname = current_database()))
    AND refclassid = 'pg_authid'::regclass AND refobjid = '$L'::regrole AND deptype = 'o'")
  [ "$c" = 0 ] || { echo "  $db: $L es dueño de $c objetos"; left=1; }
done
[ "$left" = 0 ] || fail "$L sigue siendo dueño de algo"
echo "  no es dueño de nada"
for db in $($RO -d postgres -c "SELECT datname FROM pg_database WHERE datallowconn ORDER BY 1"); do
  c=$($RO -d "$db" -c "SELECT count(*) FROM pg_namespace n, aclexplode(n.nspacl) e WHERE n.nspname = 'public' AND e.grantee = '$L'::regrole")
  [ "$c" = 0 ] || fail "$db: $L sigue con permisos en el esquema public"
done
echo "  sin permisos en ningún esquema public"
$RO -d payroll_db -c "SELECT '  payroll_db: ' || extname || ' de ' || pg_get_userbyid(extowner) FROM pg_extension WHERE extname = 'uuid-ossp'"
$RO -d collective_agreements_db -c "SELECT '  collective_agreements_db: ' || extname || ' de ' || pg_get_userbyid(extowner) FROM pg_extension WHERE extname = 'pg_trgm'"

echo "-- bases antes y después (dueño, ACL)"
after=$(db_state)
if [ "$before" = "$after" ]; then
  echo "  sin cambios"
else
  printf '%s\n' "$before" > /tmp/before; printf '%s\n' "$after" > /tmp/after
  diff /tmp/before /tmp/after | sed 's/^/  /' || true
fi
echo "== hecho"
