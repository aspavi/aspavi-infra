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
