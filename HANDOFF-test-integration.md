# Test LogSight Customer Integration — Dogfood on Homelab

## Context
Files SCP'd to ~/logsight-customer-integration/ on mgmt-01.
LogSight backend: logsight-api.home.arpa
This tests the agent + integration flow as if we're a customer.

## Steps

### 1. Create a test customer account
```bash
curl -sk -X POST https://logsight-api.home.arpa/api/v1/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email": "test-customer@dk-infraedge.com", "username": "test-customer", "password": "***REMOVED-CREDENTIAL***"}'
```

### 2. Login and create sources
```bash
TOKEN=$(curl -sk -X POST https://logsight-api.home.arpa/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username": "test-customer", "password": "***REMOVED-CREDENTIAL***"}' | jq -r '.access_token')

# Create source for mgmt-01
SOURCE_MGMT=$(curl -sk -X POST https://logsight-api.home.arpa/api/v1/sources/ \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name": "mgmt-01", "source_type": "syslog", "host": "192.168.1.222"}' | jq -r '.id')

echo "mgmt-01 source: $SOURCE_MGMT"
```

### 3. Install and run the agent on mgmt-01
```bash
sudo mkdir -p /opt/logsight /etc/logsight
sudo cp ~/logsight-customer-integration/logsight-agent.py /opt/logsight/

# Write config with actual credentials
sudo tee /etc/logsight/agent.yaml <<EOF
endpoint: https://logsight-api.home.arpa
username: test-customer
password: ***REMOVED-CREDENTIAL***
source_id: $SOURCE_MGMT
verify_ssl: false

batch_size: 50
flush_interval: 5

watch:
  - path: /var/log/syslog
    format: syslog_bsd
  - path: /var/log/auth.log
    format: syslog_bsd
EOF

# Test run (foreground, verbose)
python3 /opt/logsight/logsight-agent.py --config /etc/logsight/agent.yaml -v &
AGENT_PID=$!
sleep 15

# Verify logs are flowing
curl -sk -X GET "https://logsight-api.home.arpa/api/v1/logs/?source_id=$SOURCE_MGMT&limit=5" \
  -H "Authorization: Bearer $TOKEN" | jq '.[] | {timestamp, level, host, message}' | head -20

kill $AGENT_PID
```

### 4. Install as systemd service
```bash
sudo cp ~/logsight-customer-integration/logsight-agent.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now logsight-agent
sudo systemctl status logsight-agent
sleep 10
sudo journalctl -u logsight-agent --no-pager -n 20
```

### 5. Verify in LogSight UI
```bash
# Check log count
curl -sk "https://logsight-api.home.arpa/api/v1/logs/?source_id=$SOURCE_MGMT&limit=1" \
  -H "Authorization: Bearer $TOKEN" | jq 'length'
```

### 6. Test on a k3s node too (optional)
```bash
scp /opt/logsight/logsight-agent.py k3s-wk-01:/tmp/

# Create source for k3s-wk-01
SOURCE_WK01=$(curl -sk -X POST https://logsight-api.home.arpa/api/v1/sources/ \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name": "k3s-wk-01", "source_type": "syslog", "host": "192.168.1.221"}' | jq -r '.id')

ssh k3s-wk-01 "python3 /tmp/logsight-agent.py \
  --endpoint https://logsight-api.home.arpa \
  --username test-customer --password ***REMOVED-CREDENTIAL*** \
  --source-id $SOURCE_WK01 \
  --watch /var/log/syslog:syslog_bsd \
  --no-verify-ssl -v &"
sleep 15
curl -sk "https://logsight-api.home.arpa/api/v1/logs/?source_id=$SOURCE_WK01&limit=3" \
  -H "Authorization: Bearer $TOKEN" | jq '.[] | {host, level, message}'
```

### 7. Push agent to GitHub
```bash
cd ~
mkdir -p logsight-agent && cp ~/logsight-customer-integration/* logsight-agent/
cd logsight-agent
git init && git add -A
git commit -m "feat: LogSight agent — lightweight log shipper for customer machines"
gh repo create eched1/logsight-agent --public --source . --push
```

## Expected results
- Agent authenticates and ships logs every 5 seconds
- Logs appear in LogSight UI under the test-customer sources
- systemd service survives restart
- GitHub repo created at github.com/eched1/logsight-agent
