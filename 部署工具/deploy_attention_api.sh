#!/bin/bash
set -euo pipefail

cat > /etc/systemd/system/crypto-attention-api.service << 'EOF'
[Unit]
Description=Crypto Attention API Server
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/crypto-shadow-lab
ExecStart=/root/miniconda3/bin/python3 /opt/crypto-shadow-lab/attention_api_server.py --port 8090 --db /opt/crypto-shadow-lab/server_logs_tencent/runtime/event_store.sqlite3
Restart=always
RestartSec=5
Environment=PYTHONIOENCODING=utf-8

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable crypto-attention-api.service
systemctl start crypto-attention-api.service
echo "Attention API service deployed and started"
systemctl status crypto-attention-api.service --no-pager
