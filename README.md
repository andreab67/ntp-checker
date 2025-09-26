# NTP Checker

A Kubernetes-based system to monitor NTP/GPS health, store results in PostgreSQL, and visualize metrics through a web dashboard.

---

## Components

### 1. `monitor.py`
- Continuously checks NTP and GPS status on a remote host via SSH.
- Runs `chronyc tracking` and `chronyc sources`, plus GPS data collection via `gpspipe`.
- Validates:
  - Leap status  
  - Stratum within acceptable bounds  
  - Time offset  
  - NTP source availability  
  - GPS fix availability  
- Alerts:
  - Sends email via Amazon SES when problems are detected.
- Database:
  - Inserts health samples into PostgreSQL (`metrics.ntp_parent`).
  - Supports daily partitioning for efficient storage.

### 2. `app.py`
- A Flask web dashboard serving:
  - `/api/latest` → most recent metrics  
  - `/api/offset` → aggregated offset stats over time (24h, 14d, 90d)  
  - `/` → dashboard UI with charts (Plotly.js).
- Reads data from PostgreSQL and renders offset trends.

### 3. `schema.sql`
- Defines schema `metrics` with parent table `ntp_parent` partitioned by day.
- Includes helper functions:
  - `metrics.create_daily_partition(date)`
  - `metrics.maintain_partitions(retention_days, premake_days)`
- Automates creation and cleanup of daily partitions for rolling data retention.

---

## Dockerfiles
- `Dockerfile.checker`: builds container for `monitor.py`.
- `Dockerfile.dashboard`: builds container for `app.py`.

---

## Kubernetes Deployment

This project uses three main manifests to run inside Kubernetes:

### `deployment.yaml`
- Defines Deployments for:
  - **Checker**: runs `monitor.py` as a long-running pod.
  - **Dashboard**: runs `app.py` Flask app.
- Includes environment variables for DB connection and SES credentials.
- Can be scaled horizontally if needed.

### `service.yaml`
- Exposes the dashboard deployment internally via a ClusterIP or NodePort.
- Provides stable DNS name for Ingress routing.
- Example:
  ```yaml
  kind: Service
  apiVersion: v1
  metadata:
    name: ntp-dashboard
  spec:
    selector:
      app: dashboard
    ports:
      - port: 80
        targetPort: 8080
  ```

### `ingress.yaml`
- Configures external access to the dashboard via Ingress controller (e.g., NGINX).
- Handles host-based routing and TLS termination.
- Example:
  ```yaml
  apiVersion: networking.k8s.io/v1
  kind: Ingress
  metadata:
    name: ntp-dashboard
  spec:
    rules:
      - host: dashboard.example.com
        http:
          paths:
            - path: /
              pathType: Prefix
              backend:
                service:
                  name: ntp-dashboard
                  port:
                    number: 80
    tls:
      - hosts:
          - dashboard.example.com
        secretName: dashboard-tls
  ```

---

## CI/CD

- `.gitlab-ci.yml` handles build and deploy pipelines:
  - Lint, build Docker images, push to GitLab Container Registry.
  - Deploy to Kubernetes via GitLab CI/CD stages.

---

## Setup

### Database
1. Deploy PostgreSQL (RDS or on-cluster).
2. Run `schema.sql` to initialize schema and functions.

### Local development
```bash
export DATABASE_URL=postgresql://user:pass@host:5432/ntp-checker
python3 monitor.py   # to run checker
python3 app.py       # to run dashboard
```

### Kubernetes deployment
```bash
kubectl apply -f deployment.yaml
kubectl apply -f service.yaml
kubectl apply -f ingress.yaml
```

---

## Roadmap
- Add Prometheus metrics exporter.
- Expand dashboard with alert history.
- Automate TLS with cert-manager.

---

## Architecture Diagram

![Architecture](architecture.png)



### Explanation
- **monitor.py** collects NTP/GPS health from the remote host and writes metrics to PostgreSQL.  
- **app.py** provides APIs and dashboard views, querying PostgreSQL.  
- **Service** exposes the dashboard inside the cluster.  
- **Ingress** publishes the dashboard externally with TLS.  
- **User Browser** accesses the dashboard securely via HTTPS.  
