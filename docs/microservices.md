# Microservicios ASPAVI RRHH

Referencia consolidada de los cuatro microservicios de negocio de la plataforma ASPAVI.

> Para infraestructura (PostgreSQL, Keycloak, Traefik, ArgoCD) ver el resto de `docs/`.
> Para la librería de autenticación compartida ver `aspavi-auth-lib`.

## Tabla de servicios

| Servicio | URL pública | Puerto local | Base de datos | Tablas | Endpoints |
|---|---|---|---|---|---|
| [`employee-service`](#employee-service) | `https://api.aspavi.com/` | 8080 | `employee_db` | 4 | 14 |
| [`organization-service`](#organization-service) | `https://api.aspavi.com/organization` | 8081 | `organization_db` | 10 | 20 |
| [`audit-service`](#audit-service) | `https://api.aspavi.com/audit` | 8083 | `audit_db` | 1 | 4 |
| [`payroll-service`](#payroll-service) | `https://api.aspavi.com/payroll` | 8082 | `payroll_db` | 6 | 19 |

Documentación interactiva: `https://docs.api.aspavi.com` (Swagger UI consolidado, una entrada por servicio).

---

## Convenciones comunes

Todos los servicios siguen las mismas convenciones, definidas y reforzadas por `aspavi-auth-lib`:

| | |
|---|---|
| **Stack** | Java 25, Spring Boot 4.0.3, PostgreSQL 17, Flyway, Hibernate (`ddl-auto: validate`) |
| **Auth** | OAuth2 Resource Server con JWT firmado por Keycloak. `JwtDecoder` multi-tenant resuelve el `iss` del token contra el mapa `aspavi.auth.tenants` |
| **Multi-tenancy** | El claim `tenant_id` del JWT se almacena en `TenantContext` (ThreadLocal) por `TenantFilter`. Los servicios filtran siempre por `tenantId` y la BBDD aplica RLS sobre `app.current_tenant` |
| **Roles** | Extraídos de `realm_access.roles` del JWT. Sin prefijo `ROLE_` — usar `@PreAuthorize("hasAuthority('ADMIN')")` |
| **Endpoints públicos** | `/swagger-ui/**`, `/v3/api-docs/**`, `/actuator/health`, `/actuator/health/**`, `/actuator/info` |
| **CRUD por defecto** | `POST /` · `GET /` · `GET /{id}` · `PUT /{id}` · `DELETE /{id}` (5 endpoints por recurso) |
| **Profiles** | `local` (PostgreSQL local en `localhost:5432`), `aspavi` (cluster, lee credenciales de variables de entorno) |
| **CI** | `mvn test` con PostgreSQL 17 service · `mvn package` + Docker buildx → GHCR · `update-infra` exporta OpenAPI a `aspavi-infra/apps/api-docs/configmap.yaml` |
| **Path** | Las rutas internas siempre empiezan por `/api/v1/`. El strip-prefix middleware de Traefik elimina el prefijo público (`/audit`, `/payroll`, `/organization`) antes de llegar al pod. `employee-service` no lleva prefijo (raíz) |

---

## employee-service

CRUD de empleados, sus contratos laborales y categorías de documentos personalizadas.

- **Repo**: `aspavi/aspavi-employee-service`
- **URL pública**: `https://api.aspavi.com/api/v1/employees/...` (sin prefijo)
- **Puerto local**: 8080 · **Container**: 8080
- **DB**: `employee_db` · **Tablas**: `employees`, `contracts`, `contract_salary_concepts`, `document_categories`

### Entidades

| Entidad | Tabla | Notas |
|---|---|---|
| `Employee` | `employees` | Datos personales y profesionales del empleado (UUID, NIF, nombre, apellidos, email, fecha alta, etc.) |
| `Contract` | `contracts` | Contrato laboral. Histórico — un empleado puede tener varios; siempre hay como máximo uno "vigente" (sin fecha fin) |
| `ContractSalaryConcept` | `contract_salary_concepts` | Conceptos salariales (sueldo base, complementos, etc.) asociados a un contrato concreto |
| `DocumentCategory` | `document_categories` | Categorías personalizadas por empleado para organizar sus documentos (DNI, contrato, nómina, etc.) |

### Endpoints

**Empleados** — `/api/v1/employees`

| Método | Ruta | Resumen |
|---|---|---|
| POST | `/` | Crear empleado |
| GET | `/` | Listar todos |
| GET | `/paginated` | Listar paginado |
| GET | `/{id}` | Obtener por ID |
| PUT | `/{id}` | Actualizar |
| DELETE | `/{id}` | Eliminar |

**Contratos** — `/api/v1/employees/{employeeId}/contracts`

| Método | Ruta | Resumen |
|---|---|---|
| GET | `/` | Listar todos los contratos del empleado |
| GET | `/current` | Contrato vigente del empleado |
| POST | `/` | Crear contrato |
| PUT | `/{contractId}` | Actualizar contrato |

**Categorías de documentos** — `/api/v1/employees/{employeeId}/document-categories`

| Método | Ruta | Resumen |
|---|---|---|
| GET | `/` | Listar categorías del empleado |
| POST | `/` | Crear categoría personalizada |
| PUT | `/{categoryId}` | Actualizar categoría |
| DELETE | `/{categoryId}` | Eliminar categoría |

---

## organization-service

Configuración organizativa: estructura de la empresa, convenios colectivos, categorías profesionales, tipos de contrato, calendarios laborales, conceptos salariales.

- **Repo**: `aspavi/aspavi-organization-service`
- **URL pública**: `https://api.aspavi.com/organization/api/v1/...`
- **Puerto local**: 8081 · **Container**: 8080
- **DB**: `organization_db` · **Tablas**: 10

### Entidades

| Entidad | Tabla | Propósito |
|---|---|---|
| `Tenant` | `tenants` | Organizaciones (slug, dominio, estado). El propio servicio se autoadministra: cada inquilino del sistema tiene aquí su ficha |
| `Department` | `departments` | Departamentos de la organización |
| `Position` | `positions` | Puestos de trabajo |
| `WorkCenter` | `work_centers` | Centros de trabajo físicos (dirección, código postal, etc.) |
| `WorkCalendar` | `work_calendars` | Calendarios laborales por año, con días totales y festivos |
| `ContractType` | `contract_types` | Catálogo de tipos de contrato (indefinido, temporal, formación, etc.) |
| `CollectiveAgreement` | `collective_agreements` | Convenios colectivos aplicables |
| `ProfessionalCategory` | `professional_categories` | Categorías profesionales del convenio |
| `SalaryConcept` | `salary_concepts` | Conceptos salariales catalogados (base, complementos, deducciones) |
| `AgreementPermit` | `agreement_permits` | Tipos de permisos / ausencias contemplados en convenio |
| `ContractCategory` *(enum)* | — | Enum auxiliar |

### Endpoints

Todos los recursos siguen el patrón CRUD estándar bajo `/api/v1/{recurso}`:

| Recurso | Ruta base |
|---|---|
| Tenants | `/api/v1/tenants` |
| Departments | `/api/v1/departments` |
| Positions | `/api/v1/positions` |
| Work Centers | `/api/v1/work-centers` |
| Work Calendars | `/api/v1/work-calendars` |
| Contract Types | `/api/v1/contract-types` |
| Collective Agreements | `/api/v1/collective-agreements` |
| Professional Categories | `/api/v1/professional-categories` |
| Salary Concepts | `/api/v1/salary-concepts` |
| Agreement Permits | `/api/v1/agreement-permits` |

Cada recurso expone:

| Método | Ruta | Resumen |
|---|---|---|
| POST | `/` | Crear |
| GET | `/` | Listar todos |
| GET | `/{id}` | Obtener por ID |
| PUT | `/{id}` | Actualizar |
| DELETE | `/{id}` | Eliminar |

**Total: 10 recursos × 5 operaciones = 50 endpoints lógicos (20 path/method pairs en OpenAPI).**

---

## audit-service

Servicio centralizado de auditoría. Registra eventos `CREATE / UPDATE / DELETE / VIEW` que generan los demás microservicios sobre sus entidades, y permite consultar el histórico completo de cambios de cualquier entidad.

- **Repo**: `aspavi/aspavi-audit-service`
- **URL pública**: `https://api.aspavi.com/audit/api/v1/...`
- **Puerto local**: 8083 · **Container**: 8080
- **DB**: `audit_db` · **Tablas**: `audit_log`

### Entidades

| Entidad | Tabla | Notas |
|---|---|---|
| `AuditLog` | `audit_log` | Una fila por evento auditable. Campos: `id`, `tenantId`, `entityType`, `entityId`, `action`, `userId`, `username`, `oldValue`, `newValue`, `metadata`, `createdAt` |

### Endpoints — `/api/v1/audit-logs`

| Método | Ruta | Resumen |
|---|---|---|
| GET | `/` | Buscar logs con filtros opcionales (paginado): `entityType`, `entityId`, `userId`, `action`, rango de fechas |
| GET | `/{id}` | Obtener evento por ID |
| GET | `/entity/{entityId}` | Histórico completo de cambios de una entidad concreta |
| POST | `/` | Registrar evento (uso interno: llamado por los demás microservicios) |

> El endpoint POST está pensado como API interna entre microservicios. Los servicios de negocio escriben aquí cuando ocurre algo auditable; ningún cliente externo debería llamarlo.

---

## payroll-service

Cálculo y gestión de nóminas y cotizaciones. Conoce las tablas de IRPF, las bases de cotización a la Seguridad Social y las variaciones por categoría de contrato. Genera líneas de nómina mensuales por empleado.

- **Repo**: `aspavi/aspavi-payroll-service`
- **URL pública**: `https://api.aspavi.com/payroll/api/v1/...`
- **Puerto local**: 8082 · **Container**: 8080
- **DB**: `payroll_db` · **Tablas**: 6

### Entidades

| Entidad | Tabla | Propósito |
|---|---|---|
| `PayrollPeriod` | `payroll_periods` | Periodo de nómina (año + mes). Estados: `DRAFT`, `CLOSED` |
| `PayrollLine` | `payroll_lines` | Línea de nómina de un empleado para un periodo: salario bruto, neto, deducciones totales, coste empresarial, IRPF |
| `PayrollLineConcept` | `payroll_line_concepts` | Detalle de cada concepto que compone una línea (devengo, deducción, etc.) — código, importe, base, tipo |
| `IrpfTable` | `irpf_tables` | Tablas oficiales de retención IRPF por año fiscal y tramo de renta |
| `ContributionBase` | `contribution_bases` | Bases mín/máx de cotización a la Seguridad Social y tipos asociados (CC, desempleo, FP, FOGASA, MEI, solidaridad) por año fiscal y grupo de cotización |
| `ContributionRateVariation` | `contribution_rate_variations` | Variaciones de los tipos de cotización según categoría/duración del contrato (por ej. contratos temporales) |
| `ConceptType` *(enum)* | — | Devengo, deducción, aportación empresa, etc. |
| `ContractCategory` *(enum)* | — | Indefinido, temporal, etc. |
| `PayrollStatus`, `PayrollLineStatus` *(enums)* | — | Estados |

> El servicio se entrega con `V2__Seed_contribution_data_2025.sql` precargada con las bases y tipos vigentes para el ejercicio 2025.

### Endpoints

**Periodos de nómina** — `/api/v1/payroll-periods`

| Método | Ruta | Resumen |
|---|---|---|
| POST | `/` | Crear periodo |
| GET | `/` | Listar todos |
| GET | `/{id}` | Obtener por ID |
| GET | `/year/{year}` | Listar por año |
| PUT | `/{id}` | Actualizar |
| POST | `/{id}/close` | Cerrar periodo (lo bloquea para edición) |
| POST | `/{id}/reopen` | Reabrir periodo |
| DELETE | `/{id}` | Eliminar |

**Líneas de nómina** — `/api/v1/payroll-periods/{periodId}/lines`

| Método | Ruta | Resumen |
|---|---|---|
| POST | `/` | Crear línea para el empleado |
| GET | `/` | Listar líneas del periodo |
| GET | `/{id}` | Obtener por ID |
| PUT | `/{id}` | Actualizar |
| DELETE | `/{id}` | Eliminar |

**Conceptos de línea** — `/api/v1/payroll-periods/{periodId}/lines/{lineId}/concepts`

| Método | Ruta | Resumen |
|---|---|---|
| POST | `/` | Añadir concepto a la línea |
| GET | `/` | Listar conceptos de la línea |
| GET | `/{id}` | Obtener por ID |
| PUT | `/{id}` | Actualizar |
| DELETE | `/{id}` | Eliminar |

**Tablas IRPF** — `/api/v1/irpf-tables`

| Método | Ruta | Resumen |
|---|---|---|
| POST · GET · GET `/{id}` · PUT `/{id}` · DELETE `/{id}` | | CRUD estándar |
| GET | `/fiscal-year/{fiscalYear}` | Filtrar por año fiscal |

**Bases de cotización** — `/api/v1/contribution-bases`

| Método | Ruta | Resumen |
|---|---|---|
| POST · GET · GET `/{id}` · PUT `/{id}` · DELETE `/{id}` | | CRUD estándar |
| GET | `/fiscal-year/{fiscalYear}` | Filtrar por año fiscal |

**Variaciones de tipos de cotización** — `/api/v1/contribution-rate-variations`

| Método | Ruta | Resumen |
|---|---|---|
| POST · GET · GET `/{id}` · PUT `/{id}` · DELETE `/{id}` | | CRUD estándar |
| GET | `/search?fiscalYear=&contractCategory=` | Filtrar por año y categoría |
| GET | `/applicable?fiscalYear=&contributionGroup=&contractCategory=&durationMonths=` | Resolver la variación aplicable a un caso concreto |

---

## Mapa de dependencias

```
        ┌──────────────────┐
        │ organization-svc │  catálogos: convenios, contratos, conceptos
        └────────▲─────────┘
                 │ referencias por código (no llamada HTTP)
                 │
   ┌─────────────┴─────────────┐
   │                           │
┌──┴──────────┐         ┌──────┴───────┐
│ employee-svc│         │ payroll-svc  │
│  empleados  │         │   nóminas    │
│  contratos  │         └──────┬───────┘
└──────┬──────┘                │
       │                       │
       │ POST audit events     │
       └────────►┬◄────────────┘
                 │
        ┌────────┴────────┐
        │   audit-svc     │
        │  histórico      │
        └─────────────────┘
```

- **organization-service** es el catálogo maestro: define convenios, tipos de contrato, conceptos salariales, etc. Los demás servicios se refieren a estos por **código** (string), no por llamada HTTP — cada servicio mantiene la integridad referencial dentro de su propia BBDD.
- **employee-service** y **payroll-service** son consumidores de catálogo y productores de eventos de auditoría.
- **audit-service** es el destinatario común de eventos auditables. Los servicios escriben mediante `POST /api/v1/audit-logs`.
- **No hay llamadas síncronas service-to-service** salvo el POST a auditoría. La integridad cruzada se mantiene a nivel de aplicación.
