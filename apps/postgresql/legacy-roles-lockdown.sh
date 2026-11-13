#!/bin/sh
# Desactiva los roles sueltos que no son de ningún servicio (ASP-4), como el
# superusuario. Idempotente. Ver README.md, "Roles sueltos".
#
#   audit_user, organization_user, payroll_user y keycloak: LOGIN con
#   contraseña, con CONNECT+CREATE+TEMP en una base (audit_db,
#   organization_db, payroll_db, keycloak) y, los tres primeros, permisos en
#   el esquema public de la base postgres. Nadie los usa (inventario del
#   2026-11-13). No se borran ni se les cambia la contraseña: se quedan
#   desactivados una temporada, y el DROP va en otra PR.
#
#   1. inventario (solo lectura) y paradas: sesiones abiertas de alguno de
#      los 4, que sean dueños de algo, que tengan permisos en algo que no sea
#      una base o el esquema public (tablas, secuencias, funciones...), que
#      sean miembros de otro rol o tengan miembros, o que Keycloak no esté
#      conectado como keycloak_user
#   2. una transacción por base: REVOKE ALL ON DATABASE y ON SCHEMA public
#      de los 4 (solo lo que concedió el dueño). Después, en el ACL de la base
#      y del esquema solo pueden faltar entradas de los 4; en keycloak,
#      keycloak_user tiene que seguir pudiendo conectarse
#   3. ALTER ROLE ... NOLOGIN de los 4, en una transacción
#   4. comprobaciones: ninguno de los 4 puede entrar ni tiene CONNECT en
#      ninguna base ni permisos en nada, y la matriz de CONNECT de los 9
#      servicios y keycloak_user es la misma de antes
set -eu

ROLES="audit_user organization_user payroll_user keycloak"
KC_DB=keycloak KC_USER=keycloak_user
SVCS="absence attendance audit collective_agreements contract employee events organization payroll"

export PGOPTIONS="-c log_statement=none -c log_min_error_statement=panic"
PSQL="psql -X -q -v ON_ERROR_STOP=1 -v VERBOSITY=terse -v SHOW_CONTEXT=never"
Q="psql -X -At -v ON_ERROR_STOP=1 -v VERBOSITY=terse"
RO="env PGOPTIONS=-cdefault_transaction_read_only=on $Q"
fail() { echo "ERROR: $*" >&2; exit 1; }

# solo los que existen (si alguno ya se ha borrado, no cuenta)
roles=""
for r in $ROLES; do
  [ "$($RO -d postgres -c "SELECT count(*) FROM pg_roles WHERE rolname = '$r'")" = 1 ] && roles="$roles $r"
done
roles=${roles# }
echo "== 1. inventario de los roles sueltos (solo lectura)"
[ -n "$roles" ] || { echo "  no queda ninguno: nada que hacer"; exit 0; }
list=$(for r in $roles; do printf "'%s'," "$r"; done); list=${list%,}   # para IN (...)
alldbs() { $RO -d postgres -c "SELECT datname FROM pg_database WHERE datallowconn AND NOT datistemplate ORDER BY 1"; }

# En qué bases entra cada rol de los servicios y keycloak_user: tiene que
# quedar igual al final
svc_matrix() {
  for r in $(for s in $SVCS; do printf '%s_app %s_owner ' "$s" "$s"; done) $KC_USER; do
    printf '  %-30s %s\n' "$r" "$($RO -d postgres -c "SELECT coalesce(string_agg(datname, ' ' ORDER BY datname), '-') FROM pg_database
      WHERE datallowconn AND NOT datistemplate AND has_database_privilege('$r', oid, 'CONNECT')")"
  done
}
matrix_before=$(svc_matrix)

echo "-- atributos (contraseña=t/f dice si tiene una, nunca cuál)"
$RO -d postgres -c "SELECT format('  %s: login=%s contraseña=%s superuser=%s createdb=%s createrole=%s replication=%s bypassrls=%s caduca=%s',
    rolname, rolcanlogin, rolpassword IS NOT NULL, rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls, coalesce(rolvaliduntil::text, '-'))
  FROM pg_authid WHERE rolname IN ($list) ORDER BY 1"
[ "$($RO -d postgres -c "SELECT count(*) FROM pg_roles WHERE rolname IN ($list) AND (rolsuper OR rolreplication OR rolbypassrls OR rolcreaterole)")" = 0 ] \
  || fail "alguno de los roles tiene atributos de administrador: no se esperaba"

echo "-- pertenencia a roles"
m=$($RO -d postgres -c "SELECT format('  %s es miembro de %s', member::regrole, roleid::regrole) FROM pg_auth_members
  WHERE member IN (SELECT oid FROM pg_roles WHERE rolname IN ($list)) OR roleid IN (SELECT oid FROM pg_roles WHERE rolname IN ($list))")
printf '%s\n' "${m:-  ninguna}"
[ -z "$m" ] || fail "alguno de los roles es miembro de otro o tiene miembros"
$RO -d postgres -c "SELECT format('  ajuste de %s en %s: %s', setrole::regrole, coalesce((SELECT datname FROM pg_database WHERE oid = setdatabase), '(todas)'),
  array_to_string(setconfig, ', ')) FROM pg_db_role_setting WHERE setrole IN (SELECT oid FROM pg_roles WHERE rolname IN ($list))"

echo "-- sesiones abiertas (tres muestras)"
n=0
for i in 1 2 3; do
  s=$($RO -d postgres -c "SELECT count(*) FROM pg_stat_activity WHERE usename IN ($list)")
  [ "$s" = 0 ] && break
  n=$((n + 1)); [ "$i" = 3 ] || sleep 2
done
if [ "$n" = 3 ]; then
  $RO -d postgres -F ' ' -c "SELECT '  ', usename, datname, coalesce(nullif(application_name, ''), '-'), coalesce(client_addr::text, 'local'), state
    FROM pg_stat_activity WHERE usename IN ($list)"
  fail "hay sesiones abiertas de alguno de los roles: algo los sigue usando"
fi
echo "  ninguna"

echo "-- Keycloak"
kc=$($RO -d postgres -c "SELECT count(*) FROM pg_stat_activity WHERE usename = '$KC_USER' AND datname = '$KC_DB'")
echo "  sesiones de $KC_USER en $KC_DB: $kc"
[ "$kc" -gt 0 ] || fail "Keycloak no tiene ninguna sesión como $KC_USER en $KC_DB: no se puede confirmar con qué usuario entra (¿está arrancando?)"

echo "-- permisos y objetos, por base"
# o = dueño, a = en un ACL, r = en una política; dbid 0 = compartido (bases)
shared=$($RO -d postgres -c "SELECT format('  compartido: %s %s %s', refobjid::regrole, CASE deptype WHEN 'o' THEN 'es dueño de' WHEN 'a' THEN 'tiene permisos en' ELSE deptype::text END,
    pg_describe_object(classid, objid, objsubid)) FROM pg_shdepend
  WHERE dbid = 0 AND refclassid = 'pg_authid'::regclass AND refobjid IN (SELECT oid FROM pg_roles WHERE rolname IN ($list)) ORDER BY 1")
printf '%s\n' "$shared"
bad=$($RO -d postgres -c "SELECT count(*) FROM pg_shdepend WHERE dbid = 0 AND refclassid = 'pg_authid'::regclass
  AND refobjid IN (SELECT oid FROM pg_roles WHERE rolname IN ($list)) AND NOT (deptype = 'a' AND classid = 'pg_database'::regclass)")
[ "$bad" = 0 ] || fail "alguno de los roles es dueño de un objeto compartido o tiene permisos en algo que no es una base"
for db in $(alldbs); do
  rows=$($RO -d "$db" -c "SELECT format('  %s: %s %s %s', current_database(), refobjid::regrole,
      CASE deptype WHEN 'o' THEN 'es dueño de' WHEN 'a' THEN 'tiene permisos en' WHEN 'r' THEN 'está en la política' ELSE deptype::text END,
      pg_describe_object(classid, objid, objsubid))
    FROM pg_shdepend WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
      AND refclassid = 'pg_authid'::regclass AND refobjid IN (SELECT oid FROM pg_roles WHERE rolname IN ($list)) ORDER BY 1")
  [ -z "$rows" ] || printf '%s\n' "$rows"
  # lo único que se admite dentro de una base: permisos en el esquema public
  other=$($RO -d "$db" -c "SELECT count(*) FROM pg_shdepend WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
      AND refclassid = 'pg_authid'::regclass AND refobjid IN (SELECT oid FROM pg_roles WHERE rolname IN ($list))
      AND NOT (deptype = 'a' AND classid = 'pg_namespace'::regclass AND objid = 'public'::regnamespace)")
  [ "$other" = 0 ] || fail "en $db, alguno de los roles es dueño de algo o tiene permisos en algo que no es el esquema public"
done
echo "-- ACL de las bases y del esquema public donde aparecen"
for db in $(alldbs); do
  $RO -d "$db" -c "SELECT format('  %s: base %s', current_database(), coalesce(datacl::text, '(por defecto)')) FROM pg_database
    WHERE datname = current_database() AND EXISTS (SELECT FROM aclexplode(datacl) e JOIN pg_roles r ON r.oid = e.grantee WHERE r.rolname IN ($list))
    UNION ALL
    SELECT format('  %s: esquema public %s', current_database(), nspacl::text) FROM pg_namespace
    WHERE nspname = 'public' AND EXISTS (SELECT FROM aclexplode(nspacl) e JOIN pg_roles r ON r.oid = e.grantee WHERE r.rolname IN ($list))"
done
echo "-- para volver atrás (lo que se va a revocar, tal cual)"
for db in $(alldbs); do
  $RO -d "$db" -c "SELECT line FROM (
      SELECT 1 AS ord, format('  GRANT %s ON DATABASE %I TO %I;', string_agg(e.privilege_type, ', ' ORDER BY e.privilege_type), current_database(), r.rolname) AS line
      FROM pg_database d, aclexplode(d.datacl) e JOIN pg_roles r ON r.oid = e.grantee
      WHERE d.datname = current_database() AND r.rolname IN ($list) GROUP BY r.rolname
    UNION ALL
      SELECT 2, format('  \\c %s', current_database()) WHERE EXISTS (SELECT FROM pg_namespace n, aclexplode(n.nspacl) e JOIN pg_roles r ON r.oid = e.grantee
        WHERE n.nspname = 'public' AND r.rolname IN ($list))
    UNION ALL
      SELECT 3, format('  GRANT %s ON SCHEMA public TO %I;', string_agg(e.privilege_type, ', ' ORDER BY e.privilege_type), r.rolname)
      FROM pg_namespace n, aclexplode(n.nspacl) e JOIN pg_roles r ON r.oid = e.grantee
      WHERE n.nspname = 'public' AND r.rolname IN ($list) GROUP BY r.rolname) x ORDER BY ord, line"
done
$RO -d postgres -c "SELECT format('  ALTER ROLE %I LOGIN;', rolname) FROM pg_roles WHERE rolname IN ($list) AND rolcanlogin ORDER BY 1"

echo "== 2. permisos en bases y esquemas public (una transacción por base)"
for db in $(alldbs); do
  $PSQL -1 -d "$db" -v db="$db" -v roles="$roles" -v kc_db=$KC_DB -v kc_user=$KC_USER <<'SQL'
CREATE TEMP VIEW roles4 AS SELECT oid FROM pg_roles WHERE rolname = ANY (string_to_array(:'roles', ' '));
-- ACL de la base (normalizado: el dueño como OWNER, una ACL nula como la de
-- por defecto) y del esquema public, antes
CREATE TEMP VIEW cur_acl AS
  SELECT 'base' AS obj, CASE e.grantor WHEN d.datdba THEN 'OWNER' ELSE e.grantor::regrole::text END AS grantor,
         CASE e.grantee WHEN 0 THEN 'PUBLIC' WHEN d.datdba THEN 'OWNER' ELSE e.grantee::regrole::text END AS grantee,
         e.privilege_type || CASE WHEN e.is_grantable THEN '*' ELSE '' END AS priv, e.grantee AS grantee_oid
  FROM pg_database d, aclexplode(coalesce(d.datacl, acldefault('d', d.datdba))) e WHERE d.datname = current_database()
  UNION ALL
  SELECT 'esquema', e.grantor::regrole::text, CASE e.grantee WHEN 0 THEN 'PUBLIC' ELSE e.grantee::regrole::text END,
         e.privilege_type || CASE WHEN e.is_grantable THEN '*' ELSE '' END, e.grantee
  FROM pg_namespace n, aclexplode(coalesce(n.nspacl, acldefault('n', n.nspowner))) e WHERE n.nspname = 'public';
CREATE TEMP TABLE before_acl AS SELECT * FROM cur_acl;

SELECT EXISTS (SELECT FROM cur_acl WHERE grantee_oid IN (SELECT oid FROM roles4)) AS todo \gset
\if :todo
-- Un superusuario revoca como el dueño: solo quita lo que concedió el dueño.
-- Lo que concedió otro rol se mira a mano. Sin CASCADE
SELECT coalesce(string_agg(DISTINCT format('%s %s (concedido por %s)', obj, grantee, grantor), '; '), '') AS foreign_grants
FROM cur_acl c WHERE c.grantee_oid IN (SELECT oid FROM roles4)
  AND c.grantor <> 'OWNER' AND NOT (c.obj = 'esquema' AND c.grantor = (SELECT nspowner::regrole::text FROM pg_namespace WHERE nspname = 'public')) \gset
SELECT :'foreign_grants' <> '' AS has_foreign \gset
\if :has_foreign
\echo ERROR: :db ': permisos concedidos por otro rol que no es el dueño, revisar a mano:' :foreign_grants
DO $$ BEGIN RAISE EXCEPTION 'permisos concedidos por otro rol'; END $$;
\endif
SELECT format('REVOKE ALL ON DATABASE %I FROM %I', current_database(), r.rolname)
FROM pg_roles r WHERE r.oid IN (SELECT oid FROM roles4)
  AND EXISTS (SELECT FROM before_acl b WHERE b.obj = 'base' AND b.grantee_oid = r.oid) ORDER BY 1 \gexec
SELECT format('REVOKE ALL ON SCHEMA public FROM %I', r.rolname)
FROM pg_roles r WHERE r.oid IN (SELECT oid FROM roles4)
  AND EXISTS (SELECT FROM before_acl b WHERE b.obj = 'esquema' AND b.grantee_oid = r.oid) ORDER BY 1 \gexec

-- Solo pueden faltar entradas de los 4; nada nuevo
SELECT coalesce(string_agg(x, '; '), '') AS unexpected FROM (
  SELECT format('%s: sigue %s %s', obj, grantee, priv) x FROM cur_acl WHERE grantee_oid IN (SELECT oid FROM roles4)
  UNION ALL
  SELECT format('%s: falta %s %s', obj, grantee, priv) FROM (
    SELECT obj, grantor, grantee, priv FROM before_acl WHERE grantee_oid NOT IN (SELECT oid FROM roles4)
    EXCEPT SELECT obj, grantor, grantee, priv FROM cur_acl) g
  UNION ALL
  SELECT format('%s: nueva %s %s', obj, grantee, priv) FROM (
    SELECT obj, grantor, grantee, priv FROM cur_acl EXCEPT SELECT obj, grantor, grantee, priv FROM before_acl) a
) u \gset
SELECT :'unexpected' <> '' AS has_unexpected \gset
\if :has_unexpected
\echo ERROR: :db ', cambios no esperados:' :unexpected
DO $$ BEGIN RAISE EXCEPTION 'cambios no esperados'; END $$;
\endif
SELECT current_database() <> :'kc_db' OR has_database_privilege(:'kc_user', :'kc_db', 'CONNECT') AS kc_ok \gset
\if :kc_ok
\else
\echo ERROR: :kc_user ya no podría conectarse a :kc_db: se deshace
DO $$ BEGIN RAISE EXCEPTION 'keycloak perdería CONNECT'; END $$;
\endif
\echo '  ' :db ': revocado'
\endif
SQL
done
echo "  (en las demás bases no tenían nada)"

echo "== 3. NOLOGIN (una transacción)"
$PSQL -1 -d postgres -v roles="$roles" <<'SQL'
SELECT coalesce(string_agg(rolname, ', ' ORDER BY rolname), '') AS todo
FROM pg_roles WHERE rolname = ANY (string_to_array(:'roles', ' ')) AND rolcanlogin \gset
SELECT format('ALTER ROLE %I NOLOGIN', rolname)
FROM pg_roles WHERE rolname = ANY (string_to_array(:'roles', ' ')) AND rolcanlogin ORDER BY 1 \gexec
SELECT CASE WHEN :'todo' = '' THEN 'nada que cambiar' ELSE 'NOLOGIN: ' || :'todo' END AS status \gset
\echo '  ' :status
SQL

echo "== 4. comprobaciones (solo lectura)"
echo "-- los roles sueltos"
for r in $roles; do
  login=$($RO -d postgres -c "SELECT rolcanlogin FROM pg_roles WHERE rolname = '$r'")
  can=$($RO -d postgres -c "SELECT coalesce(string_agg(datname, ' ' ORDER BY datname), '-') FROM pg_database
    WHERE datallowconn AND NOT datistemplate AND has_database_privilege('$r', oid, 'CONNECT')")
  printf '  %-20s login=%s entra_en=%s\n' "$r" "$login" "$can"
  [ "$login" = f ] || fail "$r sigue pudiendo iniciar sesión"
  [ "$can" = - ] || fail "$r sigue teniendo CONNECT en: $can"
done
n=$($RO -d postgres -c "SELECT count(*) FROM pg_shdepend WHERE dbid = 0 AND refclassid = 'pg_authid'::regclass AND refobjid IN (SELECT oid FROM pg_roles WHERE rolname IN ($list))")
for db in $(alldbs); do
  n=$((n + $($RO -d "$db" -c "SELECT count(*) FROM pg_shdepend WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
    AND refclassid = 'pg_authid'::regclass AND refobjid IN (SELECT oid FROM pg_roles WHERE rolname IN ($list))")))
done
[ "$n" = 0 ] || fail "a alguno de los roles le quedan $n permisos u objetos"
echo "  sin permisos ni objetos en ninguna base (un DROP ROLE ya no encontraría nada)"
echo "-- CONNECT de los 9 servicios y $KC_USER"
matrix_after=$(svc_matrix)
printf '%s\n' "$matrix_after"
[ "$matrix_before" = "$matrix_after" ] || fail "la matriz de CONNECT de los servicios ha cambiado"
echo "  igual que antes"
echo "  sesiones de $KC_USER en $KC_DB: $($RO -d postgres -c "SELECT count(*) FROM pg_stat_activity WHERE usename = '$KC_USER' AND datname = '$KC_DB'")"
echo "== hecho"
