# aspavi-infra

Repositorio de infraestructura GitOps para el cluster K3s de Aspavi.

## Estructura

```
aspavi-infra/
├── helm/                        # Valores base comunes a todos los entornos
│   ├── traefik/values.yaml
│   ├── cert-manager/values.yaml
│   ├── postgresql/values.yaml
│   └── keycloak/values.yaml     (pendiente)
├── environments/                # Overrides específicos por entorno
│   ├── prod/
│   │   ├── traefik/values.yaml
│   │   ├── cert-manager/values.yaml
│   │   └── postgresql/values.yaml
│   └── dev/
│       ├── traefik/values.yaml
│       ├── cert-manager/values.yaml
│       └── postgresql/values.yaml
├── apps/
│   └── employee-service/        # Manifests K8s por microservicio
├── cluster/
│   ├── namespaces.yaml
│   └── cluster-issuer.yaml      # Let's Encrypt prod + staging
├── argocd/
│   └── applications/            # ArgoCD Application CRDs (pendiente)
└── .github/
    └── workflows/
        └── deploy-infra.yml     # Pipeline — soporta prod y dev
```

## Patrón de values

Helm carga los ficheros en orden — el segundo sobreescribe al primero:
```
helm/postgresql/values.yaml                   ← base común
environments/{env}/postgresql/values.yaml     ← override del entorno
```

## Cluster

| Nodo | IP Pública | IP Privada | Rol |
|------|-----------|------------|-----|
| aspavi-k3s-master-1 | 116.203.154.181 | 10.0.0.2 | Master |
| aspavi-k3s-worker-1 | 91.98.195.135 | 10.0.0.3 | Worker |
| aspavi-k3s-worker-2 | 46.225.141.208 | 10.0.0.4 | Worker |

## Namespaces

| Namespace | Uso |
|-----------|-----|
| rrhh-system | Microservicios |
| rrhh-gateway | API Gateway |
| rrhh-auth | Keycloak |
| rrhh-data | PostgreSQL |
| argocd | GitOps |
| monitoring | Prometheus + Grafana |

## Secrets necesarios en GitHub

| Secret | Descripción |
|--------|-------------|
| `KUBECONFIG` | Kubeconfig en base64: `cat ~/.kube/config-aspavi \| base64` |
| `POSTGRES_ADMIN_PASSWORD` | Password del usuario postgres admin |
| `APP_USER_PASSWORD` | Password del usuario app_user |
| `KEYCLOAK_USER_PASSWORD` | Password del usuario keycloak_user |
| `ARGOCD_GITHUB_TOKEN` | GitHub PAT con acceso de lectura al repo GitOps |

## Despliegue manual

```bash
export KUBECONFIG=~/.kube/config-aspavi

# Namespaces
kubectl apply -f cluster/namespaces.yaml

# Traefik
helm upgrade --install traefik traefik/traefik \
  --namespace kube-system \
  --values helm/traefik/values.yaml \
  --version 39.0.5

# cert-manager
helm upgrade --install cert-manager jetstack/cert-manager \
  --namespace kube-system \
  --values helm/cert-manager/values.yaml \
  --version v1.20.0

# ClusterIssuer
kubectl apply -f cluster/cluster-issuer.yaml

# ArgoCD repo credentials
ARGOCD_GITHUB_TOKEN=$(security find-generic-password -a "aspavi" -s "ARGOCD_GITHUB_TOKEN" -w)
kubectl create secret generic argocd-github-credentials \
  --from-literal=url="https://github.com/vmorera" \
  --from-literal=username="vmorera" \
  --from-literal=password="${ARGOCD_GITHUB_TOKEN}" \
  --namespace argocd \
  --dry-run=client -o yaml | \
kubectl label --local -f - argocd.argoproj.io/secret-type=repo-creds -o yaml | \
kubectl apply -f -

# PostgreSQL
helm upgrade --install postgresql bitnami/postgresql \
  --namespace rrhh-data \
  --values helm/postgresql/values.yaml
```
