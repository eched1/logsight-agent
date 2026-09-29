# LogSight — Customer Integration Guide

## Quick Start (5 minutes)

### Step 1: Get your credentials

```bash
# Register (or your LogSight admin creates an account for you)
curl -X POST https://logsight-api.example.internal/api/v1/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email": "ops@customer.com", "username": "customer-ops", "password": "CHANGE_ME"}'

# Login to get your token
TOKEN=$(curl -s -X POST https://logsight-api.example.internal/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username": "customer-ops", "password": "CHANGE_ME"}' | jq -r '.access_token')
```

### Step 2: Create a log source

```bash
SOURCE_ID=$(curl -s -X POST https://logsight-api.example.internal/api/v1/sources/ \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "web-server-01",
    "source_type": "syslog",
    "host": "10.0.0.50"
  }' | jq -r '.id')

echo "Source ID: $SOURCE_ID"
```

### Step 3: Choose your integration method

| Method | Best For | Setup Time |
|--------|----------|------------|
| [LogSight Agent](#option-a-logsight-agent) | Simplest — single binary, works everywhere | 2 min |
| [Fluent Bit](#option-b-fluent-bit) | Already using Fluent Bit/Fluentd | 5 min |
| [rsyslog](#option-c-rsyslog) | Linux servers with rsyslog | 5 min |
| [Direct API](#option-d-direct-api) | Custom apps, scripts, CI/CD | 1 min |

---

## Option A: LogSight Agent

The lightest option — a single Python script that tails log files and ships them to LogSight.

```bash
# Download the agent
curl -O https://logsight-api.example.internal/downloads/logsight-agent.py

# Configure
cat > /etc/logsight/agent.yaml <<EOF
endpoint: https://logsight-api.example.internal
username: customer-ops
password: CHANGE_ME
source_id: YOUR_SOURCE_ID

watch:
  - path: /var/log/syslog
    format: syslog_bsd
  - path: /var/log/nginx/access.log
    format: nginx
  - path: /var/log/nginx/error.log
    format: plain
  - path: /var/log/app/*.log
    format: json
EOF

# Run
python3 logsight-agent.py --config /etc/logsight/agent.yaml

# Or install as systemd service
sudo cp logsight-agent.service /etc/systemd/system/
sudo systemctl enable --now logsight-agent
```

---

## Option B: Fluent Bit

Add this output to your existing Fluent Bit config:

```ini
# /etc/fluent-bit/fluent-bit.conf

[INPUT]
    Name        tail
    Path        /var/log/syslog
    Tag         syslog

[INPUT]
    Name        tail
    Path        /var/log/nginx/*.log
    Tag         nginx

[FILTER]
    Name        modify
    Match       *
    Add         host ${HOSTNAME}

[OUTPUT]
    Name        http
    Match       *
    Host        logsight-api.example.internal
    Port        443
    URI         /api/v1/logs/ingest/YOUR_SOURCE_ID
    Format      json_lines
    Header      Authorization Bearer YOUR_JWT_TOKEN
    Header      Content-Type application/json
    tls         On
    Json_date_key  timestamp
    Json_date_format iso8601
```

```bash
sudo systemctl restart fluent-bit
```

---

## Option C: rsyslog (Linux)

Add HTTP forwarding to rsyslog using the omhttp module:

```bash
# Install the HTTP output module
sudo apt install rsyslog-omhttp   # Debian/Ubuntu
sudo yum install rsyslog-omhttp   # RHEL/CentOS
```

```conf
# /etc/rsyslog.d/99-logsight.conf

module(load="omhttp")

template(name="LogSightJSON" type="list") {
    constant(value="[{")
    constant(value="\"timestamp\":\"")     property(name="timereported" dateFormat="rfc3339")
    constant(value="\",\"level\":\"")      property(name="syslogseverity-text")
    constant(value="\",\"message\":\"")    property(name="msg" format="jsonf")
    constant(value="\",\"host\":\"")       property(name="hostname")
    constant(value="\",\"service\":\"")    property(name="programname")
    constant(value="\",\"raw\":\"")        property(name="rawmsg" format="jsonf")
    constant(value="\"}]")
}

action(
    type="omhttp"
    server="logsight-api.example.internal"
    serverport="443"
    restpath="api/v1/logs/ingest/YOUR_SOURCE_ID"
    template="LogSightJSON"
    httpcontenttype="application/json"
    httpheaderkey="Authorization"
    httpheadervalue="Bearer YOUR_JWT_TOKEN"
    usehttps="on"
    batch="on"
    batch.maxsize="50"
    action.resumeRetryCount="-1"
)
```

```bash
sudo systemctl restart rsyslog
```

---

## Option D: Direct API

Send logs directly from your application code:

### Python
```python
import httpx, datetime

client = httpx.Client(
    base_url="https://logsight-api.example.internal",
    headers={"Authorization": f"Bearer {TOKEN}"}
)

client.post(f"/api/v1/logs/ingest/{SOURCE_ID}", json=[
    {
        "timestamp": datetime.datetime.now().isoformat(),
        "level": "ERROR",
        "message": "Database connection timeout after 30s",
        "host": "db-01",
        "service": "postgres",
    }
])
```

### Bash (one-liner)
```bash
curl -X POST "https://logsight-api.example.internal/api/v1/logs/ingest/$SOURCE_ID" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '[{"level":"ERROR","message":"disk usage at 95%","host":"web-01","service":"nginx"}]'
```

### Docker container logs
```bash
# Pipe docker logs through jq to LogSight
docker logs -f mycontainer 2>&1 | while read line; do
  curl -s -X POST "https://logsight-api.example.internal/api/v1/logs/ingest/$SOURCE_ID" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d "[{\"message\":\"$line\",\"host\":\"$(hostname)\",\"service\":\"mycontainer\"}]"
done
```

---

## Architecture Diagram

```
Customer Infrastructure                    DK InfraEdge (LogSight)
┌─────────────────────────┐               ┌──────────────────────────┐
│                         │               │                          │
│  ┌───────┐ ┌─────────┐ │    HTTPS      │  ┌────────────────────┐  │
│  │ App   │ │ Syslog  │ │  ────────►    │  │ LogSight API       │  │
│  │ Logs  │ │ /var/log│ │  JSON POST    │  │ /api/v1/logs/ingest│  │
│  └───┬───┘ └────┬────┘ │               │  └─────────┬──────────┘  │
│      │          │       │               │            │             │
│  ┌───▼──────────▼────┐  │               │  ┌─────────▼──────────┐  │
│  │  LogSight Agent   │  │               │  │  Batch Writer      │  │
│  │  or Fluent Bit    │  │               │  │  → PostgreSQL      │  │
│  │  or rsyslog       │  │               │  │  → Rule Engine     │  │
│  └───────────────────┘  │               │  │  → AI Analysis     │  │
│                         │               │  └─────────┬──────────┘  │
│  ┌───────────────────┐  │  WebSocket    │            │             │
│  │  LogSight Web UI  │◄─┼──────────────┼─ ┌─────────▼──────────┐  │
│  │  (dashboard)      │  │  Real-time    │  │  Alerts + Anomaly  │  │
│  └───────────────────┘  │  streaming    │  │  Detection         │  │
│                         │               │  └────────────────────┘  │
└─────────────────────────┘               └──────────────────────────┘
```

---

## What You Get

Once logs are flowing:
- **Live dashboard** at logsight.example.internal — real-time log viewer with search/filter
- **Alert rules** — threshold, pattern, absence, and rate-change detection (<100ms)
- **AI analysis** — on-demand OpenAI-powered anomaly detection with root cause + recommendations
- **Network diagnostics** — ping, DNS, traceroute, port check from LogSight's infrastructure
- **Grafana dashboards** — ingestion rates, alert history, system health at grafana.example.internal
