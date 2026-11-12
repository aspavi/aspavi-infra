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
| `restore-pod.example.yaml` | Pod para restaurar. ArgoCD **no** lo crea; se lanza a mano |
| `db-users-rbac.yaml` | ServiceAccount `postgresql-db-users` (rrhh-data) y su Role en rrhh-system |
| `db-users-secrets.sh`, `db-users.sh` | Los dos pasos del Job de usuarios (ConfigMap `postgresql-db-users-scripts`) |
| `db-users-job.template.yaml` | Plantilla del Job de cada servicio. No se sincroniza: se genera en `apps/<svc>-service/` |

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

**Otra copia:** un fichero `backup-job-<fecha>.yaml` nuevo, con otro nombre
de Job y otro `BACKUP_NAME`, y añadirlo a `kustomization.yaml`. Un Job
terminado no se vuelve a ejecutar, y uno con el mismo `BACKUP_NAME` ve el
`OK` y no hace nada. Las copias no se borran solas: el PVC tiene 5Gi.

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
  tablas, que no son suyas). Lo mismo con `postgres` y `keycloak`, que esta
  fase no toca. El resumen del Job lo muestra.

### Pasar un servicio a sus propios usuarios

En una PR por servicio. Ya pasados: contract (#19), attendance.

Pasos:

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

### Pendiente

- El `initdb` de la Helm (`helm/postgresql/values.yaml`) sigue dando las bases
  nuevas a `app_user`; solo afecta a una instalación desde cero. Va en una PR
  aparte, revisando con `helm template` que no reinicia la StatefulSet.
- Cuando hayan migrado los 9: quitar a `app_user` `CREATEDB` y lo que le quede.
  Le quedan las extensiones creadas por Flyway cuando corría como él
  (`uuid-ossp` en `payroll_db`, `pg_trgm` en `collective_agreements_db`; el Job
  las avisa con `AVISO:`). No existe `ALTER EXTENSION ... OWNER`: en cada una
  de esas bases, `REASSIGN OWNED BY app_user TO <svc>_owner`. **Solo entonces**,
  cuando `app_user` ya no es dueño de ninguna base: antes se llevaría también
  las otras bases (ver arriba).
- El servidor usa `bitnami/postgresql:latest` (hoy 18.3.0). Fijar la versión,
  en una tarea aparte.

