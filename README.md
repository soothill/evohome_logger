# evohome_logger

Containerized Honeywell Evohome telemetry collector for InfluxDB. Designed for Podman on openSUSE MicroOS (works with any OCI runtime) and meant to be triggered every five minutes via cron/systemd timers or Kubernetes CronJobs. Resilient to DNS failures, InfluxDB outages, and network hiccups; writes warnings to syslog where available and persists caches in a bind-mounted data directory.

**Copyright (c) 2025 Darren Soothill (darren [at] soothill [dot] com).**

## Overview
- Authenticates to Evohome and retrieves per-zone temperatures, setpoints, heat demand, and DHW status.
- Writes metrics to InfluxDB using bucket/org/token auth.
- Uses DNS/IP caching so writes can continue if name resolution is down; keeps retrying every run.
- Buffers writes locally when InfluxDB is unreachable and flushes the backlog automatically on the next successful write.
- Logs to syslog (`/dev/log`) when present; otherwise logs to stdout/stderr for container log collection.

## Resilience highlights
- **DNS cache:** `influx_ip_cache.json` stores the last-known IP for the InfluxDB host and is reused when DNS lookup fails.
- **Offline buffer:** `offline_buffer.json` keeps Influx line protocol payloads if writes fail; flushed on next success.
- **Health checks:** Optional connectivity mode (`--check`) verifies Evohome login and InfluxDB readiness without writing data.
- **Timeout control:** `HTTP_TIMEOUT_MS` environment variable tunes HTTP timeouts to avoid hanging jobs.

## Runtime flow
1. Load configuration from environment (see below).
2. Resolve InfluxDB host; fall back to cached IP if DNS is down.
3. Login to Evohome; fetch temperatures and installation metadata.
4. Build Influx line protocol points for zones and DHW.
5. Attempt to write (including any buffered payload); on failure, cache locally and exit non-zero.

## Configuration
Provide these environment variables:
- `EVOHOME_USERNAME` / `EVOHOME_PASSWORD`: Evohome account credentials.
- `EVOHOME_LOCATION_INDEX` (optional): Installation index when multiple exist. Default: `0`.
- `INFLUX_URL`: Base InfluxDB URL (e.g., `http://influxdb:8086`).
- `INFLUX_BUCKET`: Target bucket.
- `INFLUX_ORG`: Org name/ID.
- `INFLUX_TOKEN`: Token with write permission to the bucket.
- `INFLUX_VERIFY_TLS` (optional): Set to `false` to skip TLS verification.
- `DATA_DIR` (optional): Directory for DNS/IP cache and offline buffer. Default: `/data`.
- `HTTP_TIMEOUT_MS` (optional): HTTP timeout in milliseconds for InfluxDB writes. Default: `10000`.

Use `config.env.example` as a template. `make config` copies it to `config.env` (ignored by git) so secrets stay local.

## Podman workflows (MicroOS-friendly)
1) Prepare config: `make config` then edit `config.env` with your credentials and endpoints.  
2) Build image: `make build` (podman build).  
3) Run once (good for cron/timers): `make run-once` (uses `config.env`, mounts `/var/lib/evohome-logger` to `/data`).  
4) Run detached for log inspection: `make run-detached`, view logs with `make logs`, stop/remove with `make stop` / `make rm`.  
5) Connectivity-only test (no writes): `make test-connect` (runs container with `--check`, exercises Evohome login + InfluxDB health).

`DATA_DIR` is bind-mounted so caches and offline buffers survive across runs. Adjust `DATA_DIR` in the Makefile or environment if you prefer a different host path.

### Cron example with Podman (every 5 minutes)
```cron
*/5 * * * * cd /path/to/evohome_logger && make run-once >> /var/log/evohome-logger.log 2>&1
```
The cron-driven container is short-lived; persisted `/data` ensures DNS cache and offline payloads survive between runs.

### Kubernetes CronJob example
```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: evohome-logger
spec:
  schedule: "*/5 * * * *"
  jobTemplate:
    spec:
      template:
        spec:
          restartPolicy: OnFailure
          containers:
            - name: evohome-logger
              image: your-registry/evohome-logger:latest
              env:
                - name: EVOHOME_USERNAME
                  valueFrom:
                    secretKeyRef:
                      name: evohome-credentials
                      key: username
                - name: EVOHOME_PASSWORD
                  valueFrom:
                    secretKeyRef:
                      name: evohome-credentials
                      key: password
                - name: INFLUX_URL
                  value: http://influxdb:8086
                - name: INFLUX_BUCKET
                  value: evohome
                - name: INFLUX_ORG
                  value: my-org
                - name: INFLUX_TOKEN
                  valueFrom:
                    secretKeyRef:
                      name: influx-credentials
                      key: token
              volumeMounts:
                - name: data
                  mountPath: /data
          volumes:
            - name: data
              emptyDir: {}
```
Swap `emptyDir` for a `PersistentVolume` if you want offline buffers and DNS cache to survive pod restarts.

## Connectivity checks
- `make test-connect` runs the container in check-only mode (`--check`) to validate Evohome credentials and InfluxDB readiness without writing points.
- Non-zero exit means either login failed, DNS/IP resolution failed, or InfluxDB reported unhealthy/unreachable. Inspect `make logs` for details after a detached run or view cron output.

## Data model and buffering
- **Measurements**
  - `evohome_zone`: tags `zone_id`, `zone`, `system_id`, `zone_type`; fields `temperature`, `setpoint`, `heat_demand`, `status`, `fault_count`.
  - `evohome_dhw`: tags `system_id`; fields `temperature`, `status`, `mode`, `is_available`.
- **Offline buffer:** `${DATA_DIR}/offline_buffer.json` stores pending line protocol when writes fail; flushed on the next successful write.
- **DNS/IP cache:** `${DATA_DIR}/influx_ip_cache.json` keeps the last-resolved IP for the InfluxDB host, reused when DNS is unavailable.

## Logging and troubleshooting
- Logs go to syslog if `/dev/log` exists; otherwise to stdout/stderr (visible via Podman/Kubernetes logs).
- Increase verbosity by tailing container logs frequently: `make logs` (with a detached run) or capture cron output.
- If DNS is flaky, ensure at least one successful resolution so the IP cache is primed. The job retries resolution every run and uses the cached IP on failures.
- If InfluxDB returns auth or TLS errors, recheck `INFLUX_TOKEN`, `INFLUX_URL`, and `INFLUX_VERIFY_TLS`.

## Security and operations
- Secrets live in `config.env`, which is ignored by git. For Kubernetes, store secrets in Secret objects; for cron/systemd, use root-readable files with restrictive permissions.
- Limit `config.env` permissions on MicroOS: `chmod 600 config.env`.
- Place `/data` on persistent storage if you want to retain offline buffers and DNS cache across reboots.

## Maintenance
- Update dependencies by editing `requirements.txt` and rebuilding: `make build`.
- To clear cached data, remove files under `${DATA_DIR}` (buffer + DNS cache) before the next run.

---
Copyright (c) 2025 Darren Soothill (darren [at] soothill [dot] com). All rights reserved.
