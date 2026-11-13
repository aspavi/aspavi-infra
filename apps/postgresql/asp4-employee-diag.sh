#!/bin/sh
# Diagnóstico de employee_db, SOLO LECTURA (ASP-4 fase 2: V20 de employee
# falla con document_categories_employee_fkey). Lo ejecuta
# asp4-employee-diag-job.yaml.
#
# Como el superusuario (salta el RLS y el FORCE de employees), en una
# transacción READ ONLY y con default_transaction_read_only=on. Solo imprime
# recuentos, ids de tenant, tipos de columna, RLS y dueños de tabla, y el
# historial de Flyway; ningún dato personal ni id de empleado.
set -eu

export PGOPTIONS="-c default_transaction_read_only=on"
export PGDATABASE=employee_db

psql -X -v ON_ERROR_STOP=1 -P pager=off <<'SQL'
BEGIN TRANSACTION READ ONLY;
SELECT current_user AS usuario, current_setting('transaction_read_only') AS solo_lectura,
       current_setting('server_version') AS servidor;

\echo '== 1. document_categories'
SELECT count(*)                                                AS total,
       count(*) FILTER (WHERE employee_id IS NULL)             AS employee_id_null,
       count(*) FILTER (WHERE tenant_id IS NULL)               AS tenant_id_null,
       count(*) FILTER (WHERE tenant_id = '')                  AS tenant_id_vacio
  FROM document_categories;

SELECT count(*) FILTER (WHERE e.id IS NULL)                                         AS sin_empleado,
       count(*) FILTER (WHERE e.id IS NOT NULL AND e.tenant_id IS DISTINCT FROM x.tenant_id) AS tenant_distinto,
       count(*) FILTER (WHERE e.id IS NOT NULL
                          AND lower(trim(e.tenant_id)) IS DISTINCT FROM lower(trim(x.tenant_id))) AS tenant_distinto_trim_lower
  FROM document_categories x LEFT JOIN employees e ON e.id = x.employee_id
 WHERE x.employee_id IS NOT NULL;

\echo '-- tenant_id de document_categories (con longitud, para ver espacios)'
SELECT tenant_id, length(tenant_id) AS long, count(*) AS filas,
       count(*) FILTER (WHERE employee_id IS NOT NULL) AS con_employee_id
  FROM document_categories GROUP BY tenant_id ORDER BY tenant_id NULLS FIRST;

\echo '-- parejas (tenant de la categoría, tenant del empleado) con employee_id'
SELECT x.tenant_id AS tenant_categoria, e.tenant_id AS tenant_empleado, count(*) AS filas
  FROM document_categories x LEFT JOIN employees e ON e.id = x.employee_id
 WHERE x.employee_id IS NOT NULL
 GROUP BY 1, 2 ORDER BY 1 NULLS FIRST, 2 NULLS FIRST;

\echo '-- tenant_id de employees'
SELECT tenant_id, length(tenant_id) AS long, count(*) AS filas
  FROM employees GROUP BY tenant_id ORDER BY tenant_id NULLS FIRST;

\echo '-- tipos de employee_id / id / tenant_id'
SELECT table_name, column_name, data_type, character_maximum_length AS max_long,
       is_nullable, collation_name
  FROM information_schema.columns
 WHERE table_schema = 'public'
   AND table_name IN ('employees', 'document_categories', 'work_calendar_assignment', 'employee_reporting')
   AND column_name IN ('id', 'employee_id', 'manager_id', 'tenant_id')
 ORDER BY table_name, column_name;

\echo '== 2. las otras referencias a employees'
SELECT ref,
       count(*) FILTER (WHERE e.id IS NULL)                                        AS sin_empleado,
       count(*) FILTER (WHERE e.id IS NOT NULL AND e.tenant_id IS DISTINCT FROM r.tenant_id) AS tenant_distinto,
       count(*) FILTER (WHERE e.id IS NOT NULL
                          AND lower(trim(e.tenant_id)) IS DISTINCT FROM lower(trim(r.tenant_id))) AS tenant_distinto_trim_lower,
       count(*) FILTER (WHERE r.tenant_id IS NULL OR r.tenant_id = '')             AS tenant_null_o_vacio,
       count(*)                                                                    AS total_con_ref
  FROM (
        SELECT 'work_calendar_assignment.employee_id' AS ref, employee_id AS emp, tenant_id FROM work_calendar_assignment WHERE employee_id IS NOT NULL
        UNION ALL
        SELECT 'employee_reporting.employee_id', employee_id, tenant_id FROM employee_reporting WHERE employee_id IS NOT NULL
        UNION ALL
        SELECT 'employee_reporting.manager_id', manager_id, tenant_id FROM employee_reporting WHERE manager_id IS NOT NULL
       ) r
  LEFT JOIN employees e ON e.id = r.emp
 GROUP BY ref ORDER BY ref;

\echo '== 3. RLS y dueños'
SELECT c.relname AS tabla, pg_get_userbyid(c.relowner) AS dueno,
       c.relrowsecurity AS rls, c.relforcerowsecurity AS force_rls
  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = 'public'
   AND c.relname IN ('employees', 'document_categories', 'work_calendar_assignment', 'employee_reporting')
 ORDER BY 1;

\echo '-- políticas actuales de esas tablas'
SELECT tablename AS tabla, policyname AS politica, cmd, roles::text AS roles,
       qual AS using_expr, with_check AS check_expr
  FROM pg_policies
 WHERE schemaname = 'public'
   AND tablename IN ('employees', 'document_categories', 'work_calendar_assignment', 'employee_reporting')
 ORDER BY 1, 2;

\echo '-- quién corre Flyway en el pod (dueño de la base) y sus atributos'
SELECT d.datname AS base, pg_get_userbyid(d.datdba) AS dueno_base,
       r.rolsuper AS superuser, r.rolbypassrls AS bypassrls
  FROM pg_database d JOIN pg_roles r ON r.oid = d.datdba
 WHERE d.datname = current_database();

\echo '== 4. flyway_schema_history (últimas 3)'
SELECT installed_rank, version, description, success, installed_on, installed_by
  FROM flyway_schema_history ORDER BY installed_rank DESC LIMIT 3;

COMMIT;
SQL
echo "== Hecho (solo lectura)"
