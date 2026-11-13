# apps/postgresql

Lo que acompaña a la instancia de Postgres de la Helm (`postgresql.rrhh-data`).
La sincroniza la app `postgresql` de ArgoCD como tercera fuente
(`argocd/applications/postgresql.yaml`), con kustomize. Esa app no poda
(`prune: false`): si se quita un fichero de aquí, su objeto se queda en el
clúster.

| Fichero | Qué es |
|---|---|
| `backup.sh` | Script de la copia, con prueba de restauración (ConfigMap `postgresql-backup-script`) |
| `backup-pvc.yaml` | PVC `postgresql-backup`, 5Gi, `local-path`, aparte del de la base |
| `backup-job-20261112.yaml` | Copia puntual de ASP-4, antes de cambiar los dueños de las bases |
| `backup-job-20261113.yaml` | Copia puntual de ASP-4 fase 2, antes de la primera migración de RLS estricto |
| `restore-pod.example.yaml` | Pod para restaurar. ArgoCD **no** lo crea; se lanza a mano |
| `db-users-rbac.yaml` | ServiceAccount `postgresql-db-users` (rrhh-data) y su Role en rrhh-system |
| `db-users-secrets.sh`, `db-users.sh` | Los dos pasos del Job de usuarios (ConfigMap `postgresql-db-users-scripts`) |
| `db-users-job.template.yaml` | Plantilla del Job de cada servicio. No se sincroniza: se genera en `apps/<svc>-service/` |
| `app-user-cleanup.sh`, `app-user-cleanup-job.yaml` | Limpieza final de `app_user` (ConfigMap `postgresql-app-user-cleanup-script`) |
| `legacy-roles-lockdown.sh`, `legacy-roles-lockdown-job.yaml` | Desactiva los roles sueltos (ConfigMap `postgresql-legacy-roles-lockdown-script`) |
| `asp4-fase2-precheck.sh`, `asp4-fase2-precheck-job.yaml` | Comprobación previa de ASP-4 fase 2, solo lectura (ConfigMap `postgresql-asp4-fase2-precheck-script`) |
| `tenant-backup/` | Copias diarias por tenant, cifradas, al bucket (ASP-43): CronJob `postgresql-tenant-backup`, rol `backup_reader` y su Job, `tenants.conf` y `settings.env`. Ver "Copias por tenant" |

## Copia de seguridad puntual

El Job `postgresql-backup-<fecha>` copia **toda la instancia**, como el
superusuario `postgres`, que salta el RLS: la copia lleva las filas de todos
los tenants.

- `globals.sql`: roles con sus hashes de contraseña y permisos globales
  (`pg_dumpall --globals-only`).
- `<base>.dump`: una por base que admite conexiones (`pg_dump -Fc`), incluida
  `postgres`.
- `<base>.rows`: el número de filas de cada tabla al copiarla.
- `OK`: resumen (fecha, versión, bases, tamaño). Solo existe si la prueba de
  restauración ha ido bien.

Todo queda en `/backup/<BACKUP_NAME>/` dentro del PVC `postgresql-backup`. El
Job arranca en un nodo distinto del de Postgres (`podAntiAffinity`), y
`local-path` crea el volumen en ese nodo: la copia no comparte disco con la
base.

**Prueba de restauración:** antes de dar la copia por buena, el Job levanta un
Postgres temporal dentro del propio pod (en un `emptyDir`, sin red) y restaura
en él `globals.sql` y cada base. Después compara, tabla a tabla, el número de
filas del origen con el de la copia restaurada. Si algo falla o no coincide, la
copia se queda en `<BACKUP_NAME>.tmp`, sin `OK`, y el Job falla (la app
`postgresql` sale `Degraded`). Los logs solo llevan nombres de base y tabla,
tamaños y recuentos; ni contraseñas ni datos.

```bash
export KUBECONFIG=~/.kube/aspavi.kubeconfig
kubectl -n rrhh-data get job postgresql-backup-20261112      # COMPLETIONS 1/1
kubectl -n rrhh-data logs job/postgresql-backup-20261112     # bases, tamaños y "restaurada, mismas filas"
```

Copias hechas, todas en el mismo PVC (en el nodo aspavi-k3s-worker-2):

| Job | `BACKUP_NAME` | Para qué |
|---|---|---|
| `postgresql-backup-20261112` | `20261112-asp4` | Antes de los usuarios por servicio (ASP-4 fase 1). 980K |
| `postgresql-backup-20261113` | `20261113-asp4-fase2` | Antes de la primera migración de RLS estricto (ASP-4 fase 2) |

**Otra copia:** un fichero `backup-job-<fecha>.yaml` nuevo, con otro nombre
de Job y otro `BACKUP_NAME`, y añadirlo a `kustomization.yaml`. Un Job
terminado no se vuelve a ejecutar, y uno con el mismo `BACKUP_NAME` ve el
`OK` y no hace nada. Las copias no se borran solas. El PVC pide 5Gi, pero
`local-path` no lo limita: el espacio real es el libre en el disco del nodo
(`kubectl get --raw /api/v1/nodes/aspavi-k3s-worker-2/proxy/stats/summary`,
`node.fs.availableBytes`). El Postgres temporal de la prueba va en un
`emptyDir` de hasta 5Gi en el mismo nodo.

Esto no son backups programados; eso va en una tarea aparte.

## Restaurar

Siempre desde el pod de restauración, que monta el PVC de las copias en
`/backup` (solo lectura) y entra como el superusuario:

```bash
export KUBECONFIG=~/.kube/aspavi.kubeconfig
kubectl apply -f apps/postgresql/restore-pod.example.yaml
kubectl -n rrhh-data exec -it postgresql-restore -- sh
ls /backup/                                   # copias disponibles
cat /backup/20261112-asp4/OK
```

### Una base (p. ej., `contract_db` tras una migración que ha ido mal)

1. Para el servicio, para que nadie escriba durante la restauración. ArgoCD
   vuelve a subir las réplicas (selfHeal), así que primero se quita la
   sincronización automática de su app:
   ```bash
   KUBECONFIG=~/.kube/aspavi-argocd.kubeconfig ARGOCD_OPTS='--core' argocd app set contract-service --sync-policy none
   kubectl -n rrhh-system scale deploy contract-service --replicas=0
   ```
2. Dentro del pod de restauración, borra la base y recréala desde la copia
   (`--create` la crea con su dueño, sus permisos y sus ajustes):
   ```sh
   dropdb --force --if-exists contract_db
   pg_restore -d postgres --create --exit-on-error /backup/20261112-asp4/contract_db.dump
   psql -d contract_db -c '\dt'
   ```
3. Devuelve la app a la sincronización automática, que repone las réplicas:
   ```bash
   KUBECONFIG=~/.kube/aspavi-argocd.kubeconfig ARGOCD_OPTS='--core' argocd app set contract-service --sync-policy automated --self-heal --auto-prune
   ```

La copia del 2026-11-12 es de **antes** de ASP-4: sus objetos pertenecen a
`app_user`. Si la base ya tenía sus usuarios `<svc>_owner`/`<svc>_app`,
después de restaurarla hay que volver a lanzar el Job de usuarios de ese
servicio, que es idempotente.

### La instancia entera (Postgres nuevo y vacío)

1. Para todos los servicios, como en el paso 1 de arriba.
2. Dentro del pod de restauración, restaura primero los roles y después cada
   base:
   ```sh
   D=/backup/20261112-asp4
   psql -X -q -d postgres -f $D/globals.sql      # 'role "postgres" already exists' es normal
   for f in $D/*.dump; do
     db=$(basename $f .dump); [ $db = postgres ] && continue
     pg_restore -d postgres --create --exit-on-error $f
   done
   ```
   `globals.sql` también pone las contraseñas de los roles de la copia,
   incluida la de `postgres`. Si el Postgres nuevo arrancó con otra, a partir
   de ahí vale la de la copia: la de `postgresql-helm-secrets` en el momento
   de copiar. Lo mismo para `app_user` y los demás roles.
3. Comprueba los recuentos contra `<base>.rows` y vuelve a activar las apps.

Las dos restauraciones están probadas en local con Postgres 18.3 y una copia
hecha por este mismo script. Se conservan los dueños, el RLS (`ENABLE` y
`FORCE`), las políticas y las filas.

Al terminar: `kubectl -n rrhh-data delete pod postgresql-restore`.

## Usuarios de base de datos por servicio (ASP-4)

Cada servicio con base de datos pasa de entrar como `app_user` (dueño de todas
las bases) a tener dos usuarios propios:

| Rol | Para qué | Atributos y permisos |
|---|---|---|
| `<svc>_owner` | Flyway (`FLYWAY_USER`) | Dueño de `<svc>_db` y de todos sus objetos. Como dueño, el RLS no le aplica salvo en tablas con `FORCE` |
| `<svc>_app` | La aplicación (`DB_USERNAME`) | `LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE`; `SELECT/INSERT/UPDATE/DELETE` en las tablas y `USAGE/SELECT` en las secuencias, también en las que creen las migraciones futuras (privilegios por defecto); sin DDL y sin tocar `flyway_schema_history` |

`REVOKE CONNECT ON DATABASE <svc>_db FROM PUBLIC`, y `CONNECT` solo para los
dos roles. Los servicios usan `<svc>_db` (`collective-agreements` →
`collective_agreements_db`, `collective_agreements_owner`...).

### Cómo funciona

`apps/<svc>-service/db-users-job.yaml` es un Job hook **PreSync** de la app del
servicio: corre en cada sync, antes de que ArgoCD aplique el deployment, y si
falla el deployment no cambia. Es idempotente. Corre en rrhh-data, donde está
el superusuario (`postgresql-helm-secrets/postgres-password`).

1. **initContainer `secrets`** (`db-users-secrets.sh`, kubectl): si no existen
   `<svc>-db-owner` y `<svc>-db-app` en rrhh-system, los crea con `username` y
   una contraseña aleatoria de 40 caracteres. Si existen, no los toca. Deja las
   contraseñas en un `emptyDir` en memoria para el paso 2.
2. **`postgres`** (`db-users.sh`, como superusuario, solo en `<svc>_db`):
   - crea los roles si faltan, fija sus atributos y les pone la contraseña del
     Secret;
   - crea la base si falta (sustituye al antiguo `db-bootstrap-job.yaml`, que
     volvía a dársela a `app_user` en cada sync), le cambia el dueño y aplica
     los `REVOKE`/`GRANT CONNECT`;
   - dentro de la base, pasa a `<svc>_owner` cada objeto de `app_user`
     (esquemas, tablas, vistas, secuencias sueltas, funciones, tipos) con su
     `ALTER … OWNER`, y falla si queda alguno;
   - permisos de `<svc>_app` y privilegios por defecto;
   - comprueba y resume, sin secretos: dueño de cada base, que **las demás
     bases (keycloak incluida) conservan dueño y permisos**, atributos de los
     roles, que `<svc>_app` entra en su base, que no puede crear tablas, que
     tiene DML en todas, y en qué otras bases puede conectarse.

```bash
kubectl -n rrhh-data logs job/contract-db-users --all-containers
```

Decisiones que no se ven en el código:

- **No `REASSIGN OWNED BY app_user`.** Además de los objetos de la base actual,
  reasigna los objetos *compartidos* de `app_user`, entre ellos **sus otras
  bases**: lanzado en `contract_db`, deja `employee_db` y las demás en manos de
  `contract_owner` (comprobado en Postgres 18.3).
- **Contraseñas:** no se imprimen ni van en la línea de comandos. psql las lee
  con ``\set pw `cat …` ``, con `log_statement=none` y
  `log_min_error_statement=panic` en la sesión (probado con el servidor en
  `log_statement=all`: ni la contraseña ni el `ALTER ROLE` llegan al log). Son
  de letras y números, sin salto de línea; un Secret con otro formato hace
  fallar el Job, porque psql y el deployment lo leerían distinto.
- **RBAC:** el ServiceAccount puede **crear cualquier Secret** en rrhh-system:
  `create` no se puede limitar por nombre (`resourceNames` no aplica). Solo
  puede leer y etiquetar (`get`, `patch`) los `<svc>-db-(owner|app)` de los 9
  servicios; ni `list`, ni `update`, ni `delete`, ni ningún otro Secret. El
  script se niega a escribir un Secret con otro nombre.
- **Solo la base del servicio:** `db-users.sh` exige un `DB` de la forma
  `<algo>_db`; nunca recorre todas las bases, y nunca toca `keycloak` ni
  `postgres`.
- **Conexión a otras bases:** mientras una base no ha migrado, sigue dando
  `CONNECT` a `PUBLIC`, y `<svc>_app` puede *entrar* en ella (no leer sus
  tablas, que no son suyas). Lo mismo con `postgres` y `keycloak` hasta la
  limpieza de `app_user`, que se lo quita. El resumen del Job lo muestra.

### Pasar un servicio a sus propios usuarios

En una PR por servicio (contract fue el primero):

1. Genera su Job desde la plantilla (solo cambia el nombre del servicio):
   ```bash
   sed -e '/^#T /d' -e '/^#T$/d' -e 's/__SVC__/<svc>/g' \
     apps/postgresql/db-users-job.template.yaml > apps/<svc>-service/db-users-job.yaml
   ```
2. Borra `apps/<svc>-service/db-bootstrap-job.yaml`.
3. En `apps/<svc>-service/deployment.yaml`, `DB_USERNAME`/`DB_PASSWORD` desde
   `<svc>-db-app` y `FLYWAY_USER`/`FLYWAY_PASSWORD` desde `<svc>-db-owner`
   (como en contract-service).
4. El servicio tiene que leer `spring.flyway.user: ${FLYWAY_USER:${DB_USERNAME}}`
   y `spring.flyway.password: ${FLYWAY_PASSWORD:${DB_PASSWORD}}` **antes** de
   desplegar esto.

El RBAC ya incluye los 9 servicios.

### Volver atrás (un servicio)

Revertir la PR no basta: `app_user` ya no es dueño de la base ni tiene
`CONNECT`, y el antiguo `db-bootstrap-job.yaml` (que corre como `app_user`) no
puede devolvérsela. Antes, como superusuario (pod de restauración), en la base
del servicio:

```sql
-- \c contract_db
REASSIGN OWNED BY contract_owner TO app_user;   -- aquí sí: contract_owner solo tiene esta base
GRANT CONNECT ON DATABASE contract_db TO app_user;
```

Y después revertir la PR. Los roles y los Secrets pueden quedarse.

## Limpieza de `app_user` (ASP-4, paso 4)

Cuando los 9 servicios usan ya sus propios usuarios, el Job
`postgresql-app-user-cleanup-2` (`app-user-cleanup.sh`, como el superusuario)
deja a `app_user` sin nada: ni bases, ni objetos, ni `CREATEDB`. **No** lo
borra ni le cambia la contraseña: se queda sin usar una temporada antes de
borrarlo.

Lo que le quedaba (inventario del 2026-11-13):

- la base **`postgres`** y su **esquema `public`**: la imagen de Bitnami se
  los dio en la primera instalación (`POSTGRES_USER=app_user`,
  `POSTGRES_DATABASE=postgres`);
- un `GRANT` en el esquema `public` de `employee_db` (`UC`, concedido por su
  dueño; no bloquea nada hoy, pero sí un `DROP ROLE app_user`);
- las extensiones que creó Flyway cuando corría como él: `uuid-ossp` en
  `payroll_db` y `pg_trgm` en `collective_agreements_db`. Sus funciones son
  del superusuario (son extensiones *trusted*), pero la extensión es suya;
- `CREATEDB`.

`keycloak` y `postgres` daban además `CONNECT` a `PUBLIC`, y cualquier
`<svc>_app` podía entrar en ellas.

Lo que hace, en este orden:

1. **Inventario**, solo lectura, en el log: atributos, pertenencia a roles,
   sesiones, dueño y ACL de cada base, y lo que es de `app_user` en cada
   una. **Para** sin tocar nada si hay una sesión abierta como `app_user`
   (tres muestras, para no confundirse con las sondas), si es dueño de algo
   que no sea la base `postgres`, su esquema `public` o esas dos extensiones,
   si alguna de las 9 bases no es de su `<svc>_owner` o si `keycloak` no es
   de `keycloak_user`.
   También inventaría, sin tocarlos, los **otros roles** (ni el superusuario,
   ni `app_user`, ni `keycloak_user`, ni los de los 9 servicios): atributos,
   si tienen contraseña (sí/no), sesiones, en qué bases entran y crean
   esquemas, y de qué son dueños o tienen permisos en cada base.
2. **En una transacción**, en la base `postgres`:
   - la base `postgres` pasa a ser del superusuario `postgres`, y su esquema
     `public` a `pg_database_owner` (lo de por defecto desde Postgres 15: el
     dueño de la base);
   - `REVOKE CONNECT … FROM PUBLIC` en `keycloak`, en `postgres` y en las 9
     bases que aún lo tengan (ninguna, si todas pasaron por su Job);
   - `ALTER ROLE app_user NOCREATEDB`;
   - comprueba que `keycloak_user` sigue pudiendo entrar en `keycloak` (el
     login de test y acme) y el superusuario en `postgres`, y compara dueño y
     ACL de todas las bases con los de antes: solo pueden cambiar el dueño de
     `postgres` y el `CONNECT` de `PUBLIC`. Si algo no cuadra, se deshace todo.
3. **Extensiones**, una transacción por base: `REASSIGN OWNED BY app_user TO
   <svc>_owner`, solo si `app_user` ya no es dueño de ninguna base ni
   tablespace y lo único suyo en esa base es la extensión esperada. Después
   comprueba que ninguna base ha cambiado.
3b. **Permisos en el esquema `public`**, una transacción por base: si
   `app_user` tiene alguno (hoy, en `employee_db`), `REVOKE ALL ON SCHEMA
   public FROM app_user`. Solo si los concedió el dueño del esquema (un
   superusuario revoca como el dueño); si los concedió otro rol, para y hay
   que mirarlo a mano. Sin `CASCADE`. Después, el ACL del esquema tiene que
   ser el de antes menos las entradas de `app_user`.
4. **Comprobaciones:** dueños, en qué bases puede entrar cada rol (cada
   `<svc>_app` y `<svc>_owner` solo en la suya, `keycloak_user` solo en
   `keycloak`, `app_user` en ninguna; los otros roles se listan sin
   comprobar), que `app_user` no es dueño de nada ni tiene permisos en ningún
   esquema `public`, y el diff de dueños y ACL de las bases de antes y
   después.

```bash
kubectl -n rrhh-data logs job/postgresql-app-user-cleanup-2
```

Es idempotente: una segunda vez inventaría, no cambia nada ("nada que
cambiar", "sin cambios") y termina bien. Si para, el Job falla sin
reintentos y la app `postgresql` sale `Degraded`. Para repetirlo, se le
cambia el nombre (`-3`…) o se borra el Job y ArgoCD lo vuelve a crear.

**Un Job que ya no está en git se queda en el clúster:** la app `postgresql`
no poda (`prune: false`). El primero, `postgresql-app-user-cleanup`, paró en
el paso 1 sin tocar nada (el esquema `public` de la base `postgres` no
estaba previsto) y sigue ahí, `Failed`. Mientras exista, la app sale
`OutOfSync` (un recurso por podar) y `Degraded` (ArgoCD cuenta también la
salud de los recursos por podar). Se borra a mano, **después** de mergear
el `-2` (antes, ArgoCD lo volvería a crear, porque seguiría en git):

```bash
KUBECONFIG=~/.kube/aspavi.kubeconfig kubectl -n rrhh-data delete job postgresql-app-user-cleanup
```

Borrar el Job borra también su pod y su log.

Las sondas de la StatefulSet (`pg_isready -U app_user -d postgres`) no se
autentican: el `REVOKE` y `NOCREATEDB` no les afectan (probado).

**Por qué así, y no de otra forma:**

- No existe `ALTER EXTENSION … OWNER` (Postgres 18). Borrar y crear la
  extensión no vale: `uuid-ossp` está en el `DEFAULT` de una columna y
  `pg_trgm` en un índice GIN, y `CASCADE` se los llevaría. Tocar
  `pg_extension` a mano deja `pg_shdepend` inconsistente. `REASSIGN OWNED` es
  la única vía soportada.
- **La trampa de `REASSIGN OWNED BY app_user`:** también reasigna los objetos
  compartidos de `app_user`, es decir, sus **bases**. Lanzado en `payroll_db`
  mientras `app_user` es dueño de `postgres`, deja `postgres` en manos de
  `payroll_owner` (comprobado en 18.3). Por eso la base `postgres` cambia de
  dueño antes, en el paso 2, y el paso 3 lo comprueba otra vez dentro de su
  transacción.
- Son bases distintas y no caben en una sola transacción: si el paso 3
  falla, el 2 queda hecho, y volver a lanzar el Job sigue desde ahí.

**Volver atrás**, como superusuario (pod de restauración):

```sql
ALTER DATABASE postgres OWNER TO app_user;
\c postgres
ALTER SCHEMA public OWNER TO app_user;
GRANT CONNECT ON DATABASE keycloak TO PUBLIC;
GRANT CONNECT ON DATABASE postgres TO PUBLIC;
ALTER ROLE app_user CREATEDB;
-- las extensiones pueden quedarse en <svc>_owner, y el GRANT de employee_db
-- no hace falta (nada usa app_user)
```

**Para borrar `app_user`**, más adelante: no le queda nada propio, así que
`DROP OWNED BY app_user` en cada base (solo quita sus permisos) y `DROP ROLE
app_user`. Antes hay que quitarlo de la Helm (`auth.username`; las sondas
usan ese nombre) y del `initdb`.

## Roles sueltos (ASP-4)

El inventario de la limpieza de `app_user` encontró cuatro roles que no son de
ningún servicio: `audit_user`, `organization_user`, `payroll_user` y
`keycloak`. Los cuatro eran `LOGIN` con contraseña, sin sesiones y sin nada
propio, con `CONNECT`, `CREATE` y `TEMPORARY` (`CTc`) en una base, y los tres
primeros con permisos en el esquema `public` de la base `postgres`.

- No los crea nada del repo (ni la Helm, ni `postgresql-init-secret`, ni los
  antiguos `db-bootstrap-job.yaml`), y ningún workload ni Secret del clúster
  los usa. Los tres `*_user` se llaman como el usuario del docker-compose y
  de la CI de cada servicio; seguramente se crearon a mano al principio.
  Keycloak entra como `keycloak_user`, no como `keycloak`.
- Riesgo: con `LOGIN` y `CREATE` en la base de un servicio, quien tenga la
  contraseña puede crear esquemas en ella desde cualquier pod. Algunas
  contraseñas de local están publicadas en los README de los servicios.

El Job `postgresql-legacy-roles-lockdown` (`legacy-roles-lockdown.sh`, como
el superusuario) los deja sin permisos y `NOLOGIN`. **No** los borra ni les
cambia la contraseña: se quedan desactivados una temporada, y el `DROP ROLE`
va en otra PR.

1. **Inventario**, solo lectura: atributos, pertenencia a roles, sesiones,
   permisos y objetos por base, el ACL de cada base y esquema donde aparecen,
   y las sentencias exactas para volver atrás. **Para** sin tocar nada si
   alguno tiene sesiones abiertas (tres muestras), es dueño de algo, tiene
   permisos en algo que no sea una base o el esquema `public` (tablas,
   secuencias, funciones...), es miembro de otro rol o tiene miembros, tiene
   atributos de administrador, o si no hay ninguna sesión de `keycloak_user`
   en `keycloak` (no se podría confirmar que Keycloak no entra como
   `keycloak`).
2. **Una transacción por base:** `REVOKE ALL ON DATABASE` y `REVOKE ALL ON
   SCHEMA public` de los cuatro, solo de lo que concedió el dueño (si lo
   concedió otro rol, para y se mira a mano). Después, en el ACL de la base y
   del esquema solo pueden faltar entradas de los cuatro, y en `keycloak`,
   `keycloak_user` tiene que seguir pudiendo conectarse. Si no, ROLLBACK de
   esa base. Si una base falla, las anteriores quedan hechas y el Job se
   puede repetir.
3. **`ALTER ROLE … NOLOGIN`** de los cuatro, en una transacción.
4. **Comprobaciones:** ninguno puede iniciar sesión ni tiene `CONNECT` en
   ninguna base, no les queda ningún permiso ni objeto (un `DROP ROLE` ya no
   encontraría nada), y la matriz de `CONNECT` de los 9 servicios y
   `keycloak_user` es la misma que antes.

```bash
kubectl -n rrhh-data logs job/postgresql-legacy-roles-lockdown
```

Es idempotente: una segunda vez no cambia nada ("nada que cambiar") y termina
bien.

**Volver atrás**, como superusuario (pod de restauración). Los permisos de
las bases son los del log del 2026-11-13 (`CTc`, concedidos por el dueño);
los del esquema `public` de `postgres`, los del bloque "para volver atrás"
del log del Job (el inventario anterior solo decía que había un permiso;
`GRANT ALL ON SCHEMA` da `USAGE, CREATE`):

```sql
GRANT CONNECT, CREATE, TEMPORARY ON DATABASE audit_db TO audit_user;
GRANT CONNECT, CREATE, TEMPORARY ON DATABASE organization_db TO organization_user;
GRANT CONNECT, CREATE, TEMPORARY ON DATABASE payroll_db TO payroll_user;
GRANT CONNECT, CREATE, TEMPORARY ON DATABASE keycloak TO keycloak;
\c postgres
GRANT USAGE, CREATE ON SCHEMA public TO audit_user;        -- según el log del Job
GRANT USAGE, CREATE ON SCHEMA public TO organization_user;
GRANT USAGE, CREATE ON SCHEMA public TO payroll_user;
ALTER ROLE audit_user LOGIN;
ALTER ROLE organization_user LOGIN;
ALTER ROLE payroll_user LOGIN;
ALTER ROLE keycloak LOGIN;
```

Ojo: desde la limpieza de `app_user`, `postgres` y `keycloak` ya no dan
`CONNECT` a `PUBLIC`, así que con esto vuelven a entrar en su base, no en
`postgres` ni en `keycloak` (los tres `*_user`).

**Para borrarlos**, más adelante: `DROP ROLE audit_user, organization_user,
payroll_user, keycloak;` (no les queda nada, el Job lo comprueba).

### Pendiente

- El `initdb` de la Helm (`helm/postgresql/values.yaml`) sigue dando las bases
  nuevas y `CREATEDB` a `app_user`, y Bitnami le da la base `postgres`; solo
  afecta a una instalación desde cero. Va en una PR aparte, revisando con
  `helm template` que no reinicia la StatefulSet.
- El servidor usa `bitnami/postgresql:latest` (hoy 18.3.0). Fijar la versión,
  en una tarea aparte.

## Comprobación previa de la fase 2 (ASP-4)

Las migraciones de RLS estricto de la fase 2 llevan un bloque `DO` que para la
migración si hay filas hijas con otro tenant que su padre (o sin padre). El Job
`postgresql-asp4-fase2-precheck` (`asp4-fase2-precheck.sh`) lanza esas mismas
consultas **antes** de desplegar, para saberlo sin que pare ningún servicio.

| Servicio | Migración | De dónde salen las consultas |
|---|---|---|
| contract | V4 | aspavi-contract-service#7, "Comprobación previa" |
| payroll | V3 | aspavi-payroll-service#7, "Pre-check" |
| absence | V7 | aspavi-absence-service#7, "Pre-check" |
| attendance | V2 | aspavi-attendance-service#7, "Pre-check" |
| collective-agreements | V16 | aspavi-collective-agreements-service#10, "Pre-check" |
| employee | V20 | aspavi-employee-service#15, "Pre-check" |
| organization | (sin PR) | las dos del contrato: funciones y categorías sin puesto |

audit y events no tienen.

- **Solo lectura.** Como el superusuario, que salta el RLS (también el `FORCE`
  de `employees`): cuenta las filas de todos los tenants. Como `<svc>_app` el
  RLS las filtraría y todo saldría 0 sin avisar. Cada base va en una
  transacción `READ ONLY`, y la sesión con `default_transaction_read_only=on`.
- **El log** dice, por base, la última migración aplicada (de
  `flyway_schema_history`), y por servicio y consulta, la etiqueta y el número
  de filas. Al final, `Todo 0` o la lista de las que no dan 0. Ningún dato de
  las filas.
- **Sale con 1** si alguna no da 0 o alguna consulta falla (p. ej., porque la
  migración nueva ya está aplicada y cambió el esquema). La app `postgresql`
  sale `Degraded` hasta que se borre el Job o se lance otro.
- Las consultas están escritas contra el esquema **actual** (antes de cada
  migración nueva): probadas sobre las migraciones de la imagen desplegada de
  cada servicio.
- **Valida los datos, no la migración.** Corre como superusuario, y la
  migración la corre Flyway como `<svc>_owner`. Donde una tabla tiene
  `FORCE ROW LEVEL SECURITY`, el RLS también se aplica a su dueño. Hoy solo
  `employees`, en employee: en las migraciones de las PRs de la fase 2 nadie
  más lo usa.
  - Un paso de la migración puede fallar como el dueño aunque aquí todo dé 0.
  - Le pasó a employee V20: la validación de una FK nueva hacia `employees`
    la hace el dueño, y V20 ya había cambiado las políticas a estrictas.
    Como el dueño no veía ninguna fila de `employees`, la FK falló con datos
    correctos.
  - Para esos servicios, la migración se prueba además ejecutándola como
    `<svc>_owner` sobre las migraciones anteriores (en local o en la CI del
    servicio). Este Job no sustituye esa prueba.

```bash
export KUBECONFIG=~/.kube/aspavi.kubeconfig
kubectl -n rrhh-data logs job/postgresql-asp4-fase2-precheck
```

**Repetirla** (p. ej., antes del siguiente servicio): cambiar el nombre del
Job (`-2`, `-3`...). Cuando un servicio ya tenga su migración nueva, su consulta
puede fallar o dejar de tener sentido: se quita del script en esa misma PR.


## Copias por tenant (ASP-43)

Cada noche (03:30, hora de Madrid) el CronJob `postgresql-tenant-backup` hace,
por cada tenant con copia, **un objeto cifrado** en Hetzner Object Storage:

```
s3://<bucket>/backups/<tenant>/<tenant>-<AAAAMMDDTHHMMSSZ>.tar.gz.age
```

El bucket es compartido (también guarda los ficheros de DUD-7): las copias van
solo bajo `backups/`, y el CronJob no lee, escribe ni borra nada fuera de
`backups/<tenant>/` con ese patrón de nombre.

| Fichero (en `tenant-backup/`) | Qué es |
|---|---|
| `tenants.conf` | Qué tenants tienen copia (`on`/`off`) y, si no es la de por defecto, su retención. ConfigMap `postgresql-tenant-backup-config` |
| `settings.env` | Bucket, endpoint, región, prefijo y retención por defecto. ConfigMap `postgresql-tenant-backup-settings` |
| `cronjob.yaml` | El CronJob, con `suspend: true` hasta la puesta en marcha (abajo) |
| `backup-reader-job.yaml`, `backup-reader-rbac.yaml` | El rol `backup_reader` (hook Sync de la app) y sus permisos para crear su Secret |
| `backup-reader-secret.sh`, `backup-reader.sh` | Los dos pasos de ese Job |
| `fetch-age.sh`, `dump.sh`, `restore-check.sh`, `kc-export.sh`, `upload.py` | Los pasos del CronJob. ConfigMap `postgresql-tenant-backup-scripts` |

Y en `scripts/backup/`, para el usuario: `create-secret.py` (el Secret, desde
el Llavero) y `restore-test.py` (prueba de restauración en local, con la clave
privada).

### Por qué por tenant y no un `pg_dump`

test y acme comparten las mismas bases (RLS por `tenant_id`): un `pg_dump` de
una base lleva los dos tenants mezclados, y el de la base `keycloak` todos los
realms, `master` incluido. Así que cada copia lleva solo lo de su tenant:

| En el `.tar.gz` | Qué es |
|---|---|
| `pg/<base>/schema.dump` | `pg_dump --schema-only -Fc` de cada base de servicio: el esquema para restaurar sobre una base vacía |
| `pg/<base>/<esquema>.<tabla>.copy` | `COPY` en texto. Las tablas con `tenant_id`, solo las filas del tenant; las que no lo tienen, enteras |
| `pg/<base>/sequences.tsv` | El valor de cada secuencia |
| `manifest.tsv` | Por tabla: base, tipo (`tenant` o `global`), filas, sha256 del fichero y columnas |
| `backup.info` | Tenant, fecha, versiones, la última migración de Flyway de cada base y cuántos usuarios lleva el realm |
| `keycloak/rrhh-tenant-<id>-realm.json` | El realm, con sus usuarios y sus credenciales (`kc.sh export --users realm_file`) |

- **Tablas globales** (sin `tenant_id`: hoy `flyway_schema_history` de cada
  base y el catálogo `ccaa`/`province`/`locality` de collective-agreements)
  van **dentro de cada copia**, no aparte en `backups/_global/`. Así cada
  copia se restaura sola sobre un esquema vacío (`work_calendar` apunta a
  `locality`), con las globales de esa misma noche y la misma retención. Una
  carpeta `_global` con su propia rotación podría quedarse sin la versión con
  la que se hizo una copia vieja de acme. Cuestan unos cientos de KB
  comprimidos por copia.
- **Ninguna tabla global lleva datos de un tenant**: si una tabla sin
  `tenant_id` tiene una FK hacia una con `tenant_id` (una tabla hija sin
  `tenant_id`, lo que quita la fase 2 de ASP-4), `dump.sh` para la copia en vez
  de copiarla entera y mezclar tenants.
- **Coherencia**: todas las tablas de una base salen de la misma transacción
  `REPEATABLE READ`; las bases, una tras otra (son servicios distintos).
- **Keycloak**: `kc.sh export` no está soportado con el servidor arrancado
  contra la misma base ("Your Keycloak server instance must not be started
  when invoking this command", <https://www.keycloak.org/server/importExport>).
  Sin parar Keycloak: `pg_dump` de la base `keycloak` (una foto consistente),
  restaurada en el Postgres temporal del pod, y `kc.sh export` de cada realm
  desde esa copia, en la que no hay ningún servidor. Ni el volcado ni los
  realms de otros tenants salen del pod.

### Cómo funciona

Un pod por noche (`cronjob.yaml`). Lo que está en claro vive en `emptyDir` en
memoria y desaparece con el pod. Si un paso falla, los siguientes no corren y
esa noche no se sube nada (el Job sale en error):

1. `tools`: baja age (versión y sha256 fijados en `fetch-age.sh`; si el sha256
   no coincide, para).
2. `scratch-pg` (sidecar): un Postgres temporal que solo escucha en
   `127.0.0.1` del pod. Nunca es la base real.
3. `dump` (`dump.sh`), como `backup_reader`:
   - decide los tenants: los realms `rrhh-tenant-*` más los de `tenants.conf`.
     Un realm que no está en `tenants.conf` **tiene copia** (el valor por
     defecto es `on`), y el log lo avisa;
   - copia cada base de servicio como arriba, y avisa si hay filas con un
     `tenant_id` que no es de ningún tenant conocido (no van en ninguna copia);
   - restaura la base `keycloak` en el Postgres temporal;
   - **prueba de restauración**: restaura la copia de cada tenant en el
     Postgres temporal (`restore-check.sh`): esquema, `COPY` de cada tabla y
     secuencias. Después comprueba el sha256 de cada fichero, que cada tabla
     tenga las filas del manifiesto, que no falte ninguna tabla del esquema y
     que se cumplan **todas** las FKs (la copia de un tenant no apunta fuera de
     sí misma ni de las globales). Si algo no cuadra, para.
4. `kc-export` (`kc-export.sh`): el realm de cada tenant, desde la copia de
   keycloak. La imagen tiene que ser la misma versión que el Keycloak que
   escribió la base, y lo comprueba: **al subir Keycloak, sube también la de
   `cronjob.yaml`** (y la de `scripts/backup/restore-test.py`).
5. `upload` (`upload.py`):
   - comprueba el JSON del realm;
   - empaqueta y cifra con age, por una tubería, para la clave **pública** del
     Secret: el `.tar.gz` en claro no llega a escribirse, y en el bucket no hay
     nada en claro, tampoco el manifiesto;
   - sube y comprueba con HEAD el tamaño y el sha256;
   - rota. Cada tenant va por separado: si uno falla, los demás se suben igual
     y el pod acaba en error.

El log solo lleva nombres de base, tabla y tenant, recuentos, tamaños y
nombres de objeto: ni datos ni claves.

### El rol `backup_reader`

Lo crea el Job `postgresql-backup-reader`, hook `Sync` de la app `postgresql`:
corre en cada sync y es idempotente. Su contraseña está en el Secret
`rrhh-data/postgresql-backup-reader`, que el Job crea si falta. El rol tiene:

- `LOGIN`, `BYPASSRLS` y nada más: ni superusuario, ni `CREATEDB`, ni
  `CREATEROLE`, ni `REPLICATION`. Como mucho 2 conexiones;
- miembro de `pg_read_all_data`, el rol predefinido de Postgres que da
  `SELECT` en todas las tablas y secuencias de todas las bases, también las
  que se creen después. **No cambia el ACL de ninguna tabla**, tampoco las de
  Keycloak;
- `default_transaction_read_only = on`;
- `CONNECT` en cada base `*_db`. `keycloak` y `postgres` ya dan `CONNECT` a
  `PUBLIC`, y su ACL no se toca.

Al final, el Job entra como `backup_reader` y comprueba que:

- ve todas las filas de `employees`, que tiene FORCE RLS;
- un `DELETE` le da `permission denied`.

`dump.sh` también comprueba, antes de copiar nada, que el rol tiene `BYPASSRLS`
y no es superusuario. Sin `BYPASSRLS`, el RLS filtraría las filas sin dar error
y las copias saldrían vacías.

Una base de servicio nueva recibe su `CONNECT` en el siguiente sync de la app
`postgresql`. Hasta entonces, `dump.sh` para con "backup_reader no entra en
<base>".

### Qué tenants: `tenants.conf`

```
acme   on
test   off   # entorno de pruebas
demo   off   # solo la prueba de alta de ASP-21
```

- `<tenant> on|off [diarias/semanales/mensuales]`. Sin retención, la de
  `settings.env` (7/4/6).
- Un tenant con realm y sin línea tiene copia (el valor por defecto) y el log
  lo avisa cada noche. Para que un tenant **no** tenga copia hace falta su
  línea con `off`. `onboard.py` imprime la línea (`--backups on|off`; ver
  `docs/adding-a-new-tenant.md`).
- Activar, desactivar o cambiar la retención es una PR a este fichero: el pod
  de cada noche lee el ConfigMap, así que no se reinicia ni se redespliega nada.
- Pasar un tenant a `off` deja sus copias en el bucket: ya no se rotan. Si hay
  que borrarlas, se borran a mano.

### Retención

Por tenant, N diarias, N semanales y N mensuales, **sin solaparse**:

- las N copias más recientes, una por día;
- de las anteriores a la última diaria, la más reciente de cada una de N
  semanas ISO;
- de las anteriores a la última semanal, la más reciente de cada uno de N
  meses.

Con 7/4/6 y una copia al día quedan **17 copias**, que cubren unos siete meses
y medio. Una simulación de un año de rotación diaria nunca pasa de 17.

- Dos copias el mismo día (una ejecución a mano): se queda la más reciente.
- Solo se rota un tenant cuya subida de esa noche ha ido bien.
- Solo se borran objetos de `backups/<tenant>/` con el patrón de nombre de
  arriba.

### Puesta en marcha (el usuario)

1. **Claves S3**: en <https://console.hetzner.com>, en el proyecto del bucket,
   Security → S3 credentials → Generate credentials. Se guardan en el Llavero:
   ```bash
   security add-generic-password -a aspavi -s BACKUP_S3_ACCESS_KEY -w   # pide el valor
   security add-generic-password -a aspavi -s BACKUP_S3_SECRET_KEY -w
   ```
2. **Clave de age** (`brew install age`). La privada no va nunca al clúster:
   ```bash
   age-keygen -o ~/aspavi-backup-age.key        # imprime la pública: "Public key: age1..."
   security add-generic-password -a aspavi -s BACKUP_AGE_RECIPIENT -w "age1...la pública..."
   security add-generic-password -a aspavi -s BACKUP_AGE_IDENTITY -w "$(grep AGE-SECRET-KEY ~/aspavi-backup-age.key)"
   ```
   Guarda **otra copia de la privada fuera de este Mac** (gestor de
   contraseñas o papel) y borra `~/aspavi-backup-age.key`. **Sin la clave
   privada, las copias no se pueden descifrar.**
3. **El Secret** `rrhh-data/postgresql-tenant-backup`:
   ```bash
   python3 scripts/backup/create-secret.py           # dry-run: qué encuentra
   python3 scripts/backup/create-secret.py --apply
   ```
4. **Una PR** que rellena `S3_BUCKET`, `S3_ENDPOINT`
   (`https://<ubicación>.your-objectstorage.com`, p. ej. `fsn1`) y `S3_REGION`
   (la ubicación) en `settings.env`, y pone `suspend: false` en `cronjob.yaml`.
   Con `suspend: true` ya se puede lanzar a mano (paso 5) antes de esa PR.
5. **La primera, a mano**, y la prueba de restauración desde el bucket:
   ```bash
   export KUBECONFIG=~/.kube/aspavi.kubeconfig
   kubectl -n rrhh-data create job --from=cronjob/postgresql-tenant-backup tenant-backup-primera
   kubectl -n rrhh-data logs job/tenant-backup-primera --all-containers --prefix
   python3 scripts/backup/restore-test.py acme --keycloak
   ```

### Comprobar

```bash
export KUBECONFIG=~/.kube/aspavi.kubeconfig
kubectl -n rrhh-data get cronjob postgresql-tenant-backup          # LAST SCHEDULE
kubectl -n rrhh-data get jobs -l app=postgresql-tenant-backup      # o los de la plantilla
kubectl -n rrhh-data logs job/<job> --all-containers --prefix
python3 scripts/backup/restore-test.py acme --keycloak             # de punta a punta, en local
```

`restore-test.py` hace en local lo mismo que el pod pero con lo que hay de
verdad en el bucket:

- baja la copia más reciente (o `--key`, o `--file`);
- la descifra con la clave privada (`BACKUP_AGE_IDENTITY` o `--identity`);
- la restaura en un Postgres de Docker sin red, con `restore-check.sh`;
- con `--keycloak`, importa el realm en un Keycloak de Docker (solo
  `127.0.0.1`) y compara usuarios y clients.

La copia descifrada son datos personales en claro: va a un directorio temporal
que se borra al acabar.

### Restaurar un tenant en la base real (documentado, no ejecutado)

Sustituye **solo** las filas de un tenant y no toca las de los demás. Lo decide
el usuario y lo ejecuta quien tenga el superusuario. Antes:

1. `restore-test.py <tenant> --key <objeto> --keep` sobre la copia elegida:
   tiene que dar BIEN, y deja la copia descifrada en un directorio.
2. **Flyway**: la `flyway.<base>` de `backup.info` tiene que ser la versión
   actual de cada base. Si no lo es, el esquema ha cambiado desde la copia y
   **no se restaura así**: hay que restaurarla en un Postgres temporal,
   migrarla a la versión actual y sacar de ahí los datos.
3. Una copia completa puntual antes (`backup-job-<fecha>.yaml`, como las de
   arriba).

Por cada base, como superusuario y en **una sola transacción**:

```sql
BEGIN;
-- Sin triggers ni comprobación de FKs durante la carga: así da igual el orden y
-- entran las FKs circulares (time_entry <-> time_correction). LOCAL: solo
-- esta transacción.
SET LOCAL session_replication_role = replica;
-- 1. borrar las filas del tenant de cada tabla de tipo "tenant" del manifiesto
--    (en replica no hay ON DELETE CASCADE: se borran todas a mano)
DELETE FROM public.<tabla> WHERE tenant_id = '<tenant>';         -- una por tabla
-- 2. cargarlas de la copia: solo las de tipo "tenant", NUNCA las globales
\copy public.<tabla> (<columnas del manifiesto>) FROM '<dir>/pg/<base>/public.<tabla>.copy'
-- 3. comprobar antes del COMMIT: filas de cada tabla del tenant = manifiesto,
--    y las FKs (la consulta de restore-check.sh) dan 0
COMMIT;
```

- **Las FKs compuestas** `(x_id, tenant_id) → padre (id, tenant_id)` de la
  fase 2 de ASP-4 garantizan que las filas de un tenant solo apuntan a filas
  del mismo tenant o a tablas globales. Por eso borrar y cargar solo ese
  tenant deja el conjunto cerrado, y la comprobación de FKs del paso 3 lo
  confirma antes del `COMMIT`.
- **Sin `replica`** (si no se puede ser superusuario): cargar en orden de FKs,
  padres antes que hijos, y borrar en el orden inverso. El orden sale de
  `pg_constraint` (`contype = 'f'`, `conrelid` depende de `confrelid`). Las
  FKs circulares de attendance obligan a cargar con la columna a `NULL` y
  rellenarla después con un `UPDATE`.
- **No se tocan las secuencias**: ya van por delante. `sequences.tsv` solo
  sirve para una base vacía.
- **Las tablas `outbox`** (employee, organization, collective-agreements) **no
  se restauran**: cargar sus filas viejas volvería a publicar eventos ya
  enviados. Se quedan como están.
- Los servicios siguen arrancados. Si el tenant está usando la aplicación
  mientras tanto, lo que escriba entre la copia y la restauración se pierde:
  avisar antes.

**Keycloak** (por el port-forward de `scripts/keycloak/README.md`, como admin
de master): borrar el realm `rrhh-tenant-<tenant>` y crearlo de nuevo con el
JSON de la copia (consola: Create realm → Resource file, o
`POST /admin/realms` con el JSON). Entran sus usuarios con sus contraseñas;
probado en local con Keycloak 26.5.5. Se cierran todas las sesiones del realm.
El secret del client `rrhh-iam-service` vuelve al de la copia: si se ha
cambiado después, hay que volver a ponerlo
(`scripts/keycloak/bootstrap-iam-realm.py`).

### Límites

- **Las claves S3 de Hetzner son del proyecto**: valen para todos sus buckets,
  también para los ficheros de DUD-7. El CronJob solo borra objetos de
  `backups/<tenant>/` con su patrón de nombre, pero quien tenga la clave puede
  borrar cualquier cosa del bucket. Contra eso (o contra un clúster
  comprometido), Object Lock o versionado en el bucket; tarea aparte.
- **age se baja de GitHub cada noche**, con el sha256 fijado: si GitHub no
  responde, esa noche falla (y lo dice el Job).
- **La imagen de Keycloak** de `kc-export` tiene que ser la misma versión que
  el Keycloak del clúster. `kc-export.sh` compara la de su imagen con la de la
  base (`migration_model`) y, si no coinciden, para y dice qué imagen poner:
  al subir Keycloak, la primera noche falla hasta que se sube también aquí.
- Este CronJob no tiene alertas propias: un Job fallido se ve en
  `kubectl get jobs`.
