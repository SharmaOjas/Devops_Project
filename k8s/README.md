# Dark Ship Watch on Kubernetes (DevOps Group 17: Model Deployment using Kubernetes)

The "model" is the drift-polygon (possible-area) engine: wind and wave-adjusted reach areas for ships that went dark.

| Object | File | Why |
|---|---|---|
| Secret `dsw-db-secret` | db-statefulset.yaml | DB passwords kept out of the image |
| StatefulSet `postgis` + PVC `pgdata` | db-statefulset.yaml | PostGIS with persistent storage (2Gi) |
| ConfigMap `dsw-config` | app-deployment.yaml | DB host/port as env vars (read by `db.py`) |
| Job `dsw-db-init` | app-deployment.yaml | Runs `setup_db.py` (schema, triggers, roles) and loads the land map once |
| Deployment `dark-ship-app` (x2) | app-deployment.yaml | Web UI + API + model; readiness `/api/health`, liveness `/` |
| Deployment `dark-ship-collector` (x1) | app-deployment.yaml | Live AIS/weather polling; one writer only |
| Service `postgis` (ClusterIP) | service.yaml | Internal DB at `postgis:5432` |
| Service `dark-ship-app` (LoadBalancer, port/nodePort 30000) | service.yaml | Map UI at http://localhost:30000, balanced across both pods |

Why LoadBalancer and not plain NodePort: Docker Desktop's kind-based cluster doesn't publish NodePorts to Windows,
only LoadBalancer ports. `kubectl port-forward` is also a bad fit, since it stays on one pod and drops during a rollout.

The collector logs a few `password authentication failed for dsw_ingest` warnings at first start. That's expected:
it starts before the init Job has created the role, then retries and connects.

## Run it (Docker Desktop → Settings → Kubernetes → Enable)

```bash
docker build -t dark-ship-watch:1.0 .
kubectl apply -f k8s/
kubectl get pods,svc,pvc -w
kubectl logs job/dsw-db-init
```

Open http://localhost:30000.

Minikube instead: `minikube image load dark-ship-watch:1.0`, then `minikube tunnel`.

Tested on Docker Desktop (Kubernetes v1.36.1, kind provisioner): 56/56 health checks returned 200 during
`rollout restart`, and the `location_events` row count was unchanged after deleting `postgis-0`.

## Viva demo

```bash
# zero-downtime rolling update (maxUnavailable: 0)
kubectl rollout restart deployment dark-ship-app
kubectl rollout status deployment dark-ship-app

# self-healing: delete a pod, a new one replaces it
kubectl delete pod -l app=dark-ship-app --wait=false
kubectl get pods -w

# scaling
kubectl scale deployment dark-ship-app --replicas=4

# persistence: kill the DB pod, data is still there after it comes back
kubectl delete pod postgis-0
kubectl exec postgis-0 -- psql -U postgres -d darkshipwatch -c "SELECT count(*) FROM location_events;"

# rollback
kubectl rollout undo deployment dark-ship-app
```

## CI pipeline (GitHub Actions)

[.github/workflows/ci.yml](../.github/workflows/ci.yml) runs on every push to `main`:
compile check → `docker build` → throwaway kind cluster → `kubectl apply -f k8s/` → API smoke test →
rolling update keeps 2 ready pods → data survives a `postgis-0` restart. On failure it prints the pod state and logs.

## CD pipeline (Jenkins)

[Jenkinsfile](../Jenkinsfile) deploys to the local Docker Desktop cluster (Jenkins runs as a Windows service on
http://localhost:8081). Each build tags the image with the build number (`dark-ship-watch:<N>`), so every run is a
real rolling update: build image → deploy database → re-run the setup Job (schema migration) and apply the app with
the new tag → wait for rollout → smoke test `/api/health`. If any stage fails, it runs `kubectl rollout undo`.

So: GitHub Actions = CI (tests every push on a throwaway cluster), Jenkins = CD (deploys to our cluster).

Clean up: `kubectl delete -f k8s/` (the PVC stays; `kubectl delete pvc pgdata-postgis-0` removes the data).
