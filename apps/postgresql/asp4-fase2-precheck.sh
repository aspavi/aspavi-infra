#!/bin/sh
# Comprobación previa de ASP-4 fase 2, SOLO LECTURA. La ejecuta
# asp4-fase2-precheck-job.yaml; ver README.md, "Comprobación previa de la
# fase 2".
#
# Lanza, antes de desplegar cada servicio, las consultas que harían parar el
# bloque DO de su migración nueva. Son las de la sección "Pre-check" /
# "Comprobación previa" de la PR de cada servicio, tal cual; organization aún no
# tiene PR y lleva las dos que pasó el orquestador. audit y events no tienen.
#
# - Entra como el superusuario (PGHOST/PGUSER/PGPASSWORD): salta el RLS, también
#   el FORCE de employees, así que cuenta las filas de todos los tenants. Como
#   <svc>_app el RLS las filtraría y todo saldría 0 sin decir nada.
# - Cada base va en una transacción READ ONLY, y la sesión además con
#   default_transaction_read_only=on: una escritura o un DDL daría error.
# - El log solo lleva servicio, etiqueta y número de filas; ningún dato.
#
# Sale con 0 si todas dan 0. Sale con 1 si alguna no da 0 o alguna consulta
# falla (la app `postgresql` sale Degraded; el log dice cuál).
set -eu

export PGOPTIONS="-c default_transaction_read_only=on"
WORK=$(mktemp -d)
RESULTS="$WORK/results"
: > "$RESULTS"

# check <servicio> <base> <etiqueta>, con la consulta por la entrada estándar.
# Una consulta de un solo count(*) da una fila con el número; una con varias
# filas (UNION ALL) da "etiqueta|número" en cada una.
check() {
  svc=$1 db=$2 label=$3
  sql=$(cat)
  if out=$(printf 'BEGIN TRANSACTION READ ONLY;\n%s\nCOMMIT;\n' "$sql" |
      psql -X -q -At -F'|' -v ON_ERROR_STOP=1 -d "$db" 2> "$WORK/err"); then
    printf '%s\n' "$out" | while IFS='|' read -r a b; do
      [ -n "$a" ] || continue
      if [ -n "$b" ]; then l="$label: $a"; n=$b; else l=$label; n=$a; fi
      printf '  %-14s %-58s %s\n' "$svc" "$l" "$n"
      printf '%s|%s|%s\n' "$svc" "$l" "$n" >> "$RESULTS"
    done
  else
    printf '  %-14s %-58s ERROR\n' "$svc" "$label"
    sed 's/^/      /' "$WORK/err" | head -5
    printf '%s|%s|ERROR\n' "$svc" "$label" >> "$RESULTS"
  fi
}

echo "Servidor $(psql -X -d postgres -Atc 'SHOW server_version'), como $(psql -X -d postgres -Atc 'SELECT current_user')," \
  "default_transaction_read_only=$(psql -X -d postgres -Atc 'SHOW default_transaction_read_only')"
for db in contract_db payroll_db absence_db attendance_db collective_agreements_db employee_db organization_db; do
  v=$(psql -X -d "$db" -Atc "SELECT coalesce(max(version::int)::text, '-') FROM flyway_schema_history WHERE success AND version IS NOT NULL" 2>/dev/null || echo '?')
  echo "  $db: última migración V$v"
done
echo "== Comprobaciones (servicio, consulta, filas que harían parar la migración)"

# --- contract (aspavi-contract-service#7, V4) ---
check contract contract_db "V4 conceptos con tenant distinto del contrato" <<'SQL'
SELECT count(*)
  FROM contract_salary_concepts csc
  JOIN contracts c ON c.id = csc.contract_id
 WHERE csc.tenant_id <> c.tenant_id;
SQL

# --- payroll (aspavi-payroll-service#7, V3) ---
check payroll payroll_db "V3 payroll_lines sin periodo de su tenant" <<'SQL'
SELECT count(*) FROM payroll_lines l LEFT JOIN payroll_periods p ON p.id=l.period_id AND p.tenant_id=l.tenant_id WHERE p.id IS NULL;
SQL
check payroll payroll_db "V3 payroll_line_concepts sin línea de su tenant" <<'SQL'
SELECT count(*) FROM payroll_line_concepts c LEFT JOIN payroll_lines l ON l.id=c.line_id AND l.tenant_id=c.tenant_id WHERE l.id IS NULL;
SQL

# --- absence (aspavi-absence-service#7, V7) ---
check absence absence_db "V7 solicitudes con tipo de otro tenant" <<'SQL'
SELECT count(*) FROM absence_request r JOIN absence_type t ON t.id = r.absence_type_id WHERE t.tenant_id <> r.tenant_id;
SQL

# --- attendance (aspavi-attendance-service#7, V2) ---
check attendance attendance_db "V2 time_entry.replaces_entry_id de otro tenant" <<'SQL'
SELECT count(*) FROM time_entry e JOIN time_entry r ON r.id = e.replaces_entry_id WHERE r.tenant_id <> e.tenant_id;
SQL
check attendance attendance_db "V2 time_correction.target_entry_id de otro tenant" <<'SQL'
SELECT count(*) FROM time_correction c JOIN time_entry t ON t.id = c.target_entry_id WHERE t.tenant_id <> c.tenant_id;
SQL
check attendance attendance_db "V2 time_correction.resulting_entry_id de otro tenant" <<'SQL'
SELECT count(*) FROM time_correction c JOIN time_entry t ON t.id = c.resulting_entry_id WHERE t.tenant_id <> c.tenant_id;
SQL
check attendance attendance_db "V2 time_entry.correction_id de otro tenant" <<'SQL'
SELECT count(*) FROM time_entry e JOIN time_correction c ON c.id = e.correction_id WHERE c.tenant_id <> e.tenant_id;
SQL
check attendance attendance_db "V2 time_entry.correction_id inexistente" <<'SQL'
SELECT count(*) FROM time_entry e WHERE e.correction_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM time_correction c WHERE c.id = e.correction_id);
SQL

# --- collective-agreements (aspavi-collective-agreements-service#10, V16) ---
check collective-agr collective_agreements_db "V16" <<'SQL'
SELECT 'agreement_signatory' AS tabla, count(*) AS filas_mal
  FROM agreement_signatory x LEFT JOIN collective_agreement p ON p.id = x.agreement_id
 WHERE p.tenant_id IS NULL OR p.tenant_id = ''
UNION ALL
SELECT 'agreement_category_complemento', count(*)
  FROM agreement_category_complemento x LEFT JOIN agreement_category p ON p.id = x.category_id
 WHERE p.tenant_id IS NULL OR p.tenant_id = ''
UNION ALL
SELECT 'agreement_remuneration_paga', count(*)
  FROM agreement_remuneration_paga x LEFT JOIN agreement_remuneration p ON p.id = x.remuneration_id
 WHERE p.tenant_id IS NULL OR p.tenant_id = ''
UNION ALL
SELECT 'agreement_category', count(*) FROM agreement_category x
  JOIN collective_agreement p ON p.id = x.agreement_id WHERE p.tenant_id <> x.tenant_id
UNION ALL
SELECT 'agreement_remuneration', count(*) FROM agreement_remuneration x
  JOIN collective_agreement p ON p.id = x.agreement_id WHERE p.tenant_id <> x.tenant_id
UNION ALL
SELECT 'agreement_leave', count(*) FROM agreement_leave x
  JOIN collective_agreement p ON p.id = x.agreement_id WHERE p.tenant_id <> x.tenant_id
UNION ALL
SELECT 'agreement_excedencia', count(*) FROM agreement_excedencia x
  JOIN collective_agreement p ON p.id = x.agreement_id WHERE p.tenant_id <> x.tenant_id
UNION ALL
SELECT 'agreement_version', count(*) FROM agreement_version x
  JOIN collective_agreement p ON p.id = x.agreement_id WHERE p.tenant_id <> x.tenant_id
UNION ALL
SELECT 'holiday', count(*) FROM holiday x
  JOIN work_calendar p ON p.id = x.work_calendar_id WHERE p.tenant_id <> x.tenant_id
UNION ALL
SELECT 'work_calendar_adjustment', count(*) FROM work_calendar_adjustment x
  JOIN work_calendar p ON p.id = x.work_calendar_id WHERE p.tenant_id <> x.tenant_id;
SQL

# --- employee (aspavi-employee-service#15, V20) ---
# Como superusuario: salta el FORCE de employees, no hace falta
# app.tenant_discovery (la PR pide superusuario o un rol BYPASSRLS).
check employee employee_db "V20" <<'SQL'
SELECT 'document_categories.employee_id' AS ref, count(*) FROM document_categories x LEFT JOIN employees e ON e.id = x.employee_id WHERE x.employee_id IS NOT NULL AND e.tenant_id IS DISTINCT FROM x.tenant_id
UNION ALL
SELECT 'work_calendar_assignment.employee_id', count(*) FROM work_calendar_assignment x LEFT JOIN employees e ON e.id = x.employee_id WHERE e.tenant_id IS DISTINCT FROM x.tenant_id
UNION ALL
SELECT 'employee_reporting.employee_id', count(*) FROM employee_reporting x LEFT JOIN employees e ON e.id = x.employee_id WHERE e.tenant_id IS DISTINCT FROM x.tenant_id
UNION ALL
SELECT 'employee_reporting.manager_id', count(*) FROM employee_reporting x LEFT JOIN employees e ON e.id = x.manager_id WHERE e.tenant_id IS DISTINCT FROM x.tenant_id;
SQL

# --- organization (sin PR todavía; las dos del orquestador) ---
check organization organization_db "job_position_functions sin puesto" <<'SQL'
SELECT count(*) FROM job_position_functions f LEFT JOIN job_positions p ON p.id = f.job_position_id WHERE p.id IS NULL;
SQL
check organization organization_db "job_position_categories sin puesto" <<'SQL'
SELECT count(*) FROM job_position_categories c LEFT JOIN job_positions p ON p.id = c.job_position_id WHERE p.id IS NULL;
SQL

echo "== Resumen"
total=$(wc -l < "$RESULTS" | tr -d ' ')
bad=$(awk -F'|' '$3 != "0"' "$RESULTS")
if [ -z "$bad" ]; then
  echo "Todo 0 ($total comprobaciones): ninguna migración nueva parará por los datos."
  exit 0
fi
echo "NO todo es 0 ($total comprobaciones). Antes de desplegar, mirar:"
printf '%s\n' "$bad" | awk -F'|' '{printf "  %-14s %-58s %s\n", $1, $2, $3}'
exit 1
