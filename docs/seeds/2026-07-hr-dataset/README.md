# Dataset de RRHH · julio 2026

Rellena el tenant `acme` con una plantilla coherente entre servicios, y completa
los dos empleados de `test`. Los seeds de `../seed_acme_*.sql` crearon en su día
el esqueleto (28 empleados, 8 centros, 10 puestos, 2 convenios vigentes); esto
añade lo que faltaba, que era casi todo lo demás.

## Qué estado esperaba encontrar

Al generarlo, los 28 empleados de `acme` no tenían ningún dato personal, ni
centro ni puesto asignado, y estas tablas estaban **vacías**: `contracts`,
`contract_salary_concepts`, `absence_request`, `vacation_balance`, `time_entry`,
`work_day`, `employee_reporting`. Los cinco centros valencianos no tenían
calendario laboral, así que su convenio no resolvía.

## Qué inserta

| Script | Base de datos | Contenido |
|---|---|---|
| `1_collective_agreements.sql` | `collective_agreements_db` | 5 calendarios 2026 (València ×2, Gandia, Paterna, Castelló) con 13 festivos cada uno |
| `2_organization.sql` | `organization_db` | Enlaza los 8 centros a su calendario y su responsable |
| `3_employee.sql` | `employee_db` | Datos personales, dirección, emergencia y banco; centro y puesto; 27 relaciones de reporte; asignación de calendario |
| `4_contract.sql` | `contract_db` | 30 contratos vigentes con categoría y salario del convenio, 51 pluses |
| `5_absence.sql` | `absence_db` | 30 saldos de vacaciones y 50 solicitudes |
| `6_attendance.sql` | `attendance_db` | Julio 2026 completo: 1.720 fichajes y 619 jornadas |
| `7_ssn.sql` | `employee_db` | Números de la Seguridad Social (requiere la V13 de employee-service) |
| `8_tax_profile.sql` | `employee_db` | Declaraciones del modelo 145 (requiere la V14) |
| `9_fix_sanidad_weekly_hours.sql` | `contract_db` | Corrección puntual: la jornada de Sanidad Privada era 37,5 en vez de 37,0 |

## Cómo aplicarlo

Contra el clúster, en orden:

```bash
for f in 1_collective_agreements 2_organization 3_employee 4_contract 5_absence 6_attendance 7_ssn; do
  db=$(echo "$f" | sed 's/^[0-9]*_//'); case "$db" in collective_agreements) db=collective_agreements_db;; *) db="${db}_db";; esac
  kubectl -n rrhh-data exec -i postgresql-0 -- bash -c "PGPASSWORD=\$(cat \"\$POSTGRES_POSTGRES_PASSWORD_FILE\") psql -U postgres -d $db -v ON_ERROR_STOP=1 -q -f -" < "$f.sql"
done
```

Cada script es transaccional y **re-ejecutable**: borra únicamente lo que él
mismo genera, siempre acotado a los tenants `acme` y `test`.

## Regenerarlo

```bash
python3 generate.py
```

El generador usa una semilla fija, así que dos ejecuciones producen SQL
idéntico. Los ids de referencia (convenios, categorías, centros, puestos, tipos
de ausencia) están codificados arriba del fichero y salen de la base de datos
real — si alguno cambia, hay que actualizarlos ahí.

## Decisiones que conviene conocer

- **Las cifras salen del convenio que aplica**, no de un rango aleatorio: un
  celador cobra los 17.200 € de su categoría en Sanidad Privada, con 14 pagas.
- **Las vacaciones se cuentan como manda cada convenio** — laborables en
  Oficinas y Despachos, naturales en Sanidad Privada — y se escalonan en agosto
  para que ningún centro se quede vacío.
- **Los fichajes tienen hueco los días de ausencia aprobada**, y hay dos
  personas con la jornada abierta a la hora de generarlo.
- **Los NAF llevan dígitos de control reales** (el número de 10 cifras módulo
  97) y la provincia corresponde al centro de trabajo.
- **La jornada sale del convenio, no al revés.** Cada empleado se declara con
  su FTE (1,0 · 0,8 · 0,5) y las horas se derivan de la jornada completa que
  fija su convenio. Así nadie puede acabar contratado por encima del tope,
  que es justo lo que pasó cuando las horas iban a mano: los 16 contratos de
  sanidad estaban a 37,5 h contra un maximo de 37,0, y de ahí salio el
  `9_fix_...`.
- **payroll no se toca.** Nóminas sigue con mocks por decisión de producto, y
  los 13 periodos que ya existían se dejan como están.
