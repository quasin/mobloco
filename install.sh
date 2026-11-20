#!/usr/bin/env bash
set -e

cd "$(dirname "${BASH_SOURCE[0]}")"
APP_DIR="$(pwd)"
RUN_USER="${SUDO_USER:-$(id -un)}"

sudo apt update
sudo DEBIAN_FRONTEND=noninteractive apt install -y \
	build-essential python3-dev python3-pip python3-venv \
	unzip pkg-config

python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install --upgrade pip
pip3 install -r requirements.txt

mkdir data
echo -e "name\turl\tdescription" > data/feeds.tsv

# feed mirror: reads data/feeds.tsv on :00 and :30 UTC, writes data/ + data/logs.txt
sudo tee /etc/systemd/system/mobloco-feeds.service >/dev/null <<EOF
[Unit]
Description=Mobloco feed mirror
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/.venv/bin/python $APP_DIR/feedcopy.py
Restart=always
RestartSec=15

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable mobloco-feeds.service

sleep 9
sudo reboot
