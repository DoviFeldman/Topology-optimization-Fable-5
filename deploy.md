# Deploying to a VPS (Ubuntu 22.04+)

This guide takes you from a fresh Ubuntu server to a running topology
optimizer, step by step. **Do not skip steps.** Every command goes in the
server's terminal (SSH in first: `ssh youruser@your-server-ip`).

> **Sizing note:** optimizations are CPU/RAM hungry. Recommended: 4+ cores and
> 8 GB RAM for the default resolution (96). On a small 1–2 GB VPS, keep
> resolution at 48–64.

## 1. Install system packages

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git
```

## 2. Get the code

```bash
cd /opt
sudo git clone https://github.com/DoviFeldman/Topology-optimization-Fable-5.git topology-optimizer
sudo chown -R $USER: /opt/topology-optimizer
cd /opt/topology-optimizer
```

## 3. Create a virtual environment and install dependencies

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## 4. Test it manually first

```bash
.venv/bin/uvicorn main:app --host 127.0.0.1 --port 8000
```

From your own computer, check it responds (in a second terminal on the server):

```bash
curl http://127.0.0.1:8000/api/meta
```

You should see `{"faces": [...], "directions": [...]}`. Press `Ctrl+C` to stop.

## 5. Run it as a systemd service (starts on boot, restarts on crash)

Create the service file:

```bash
sudo tee /etc/systemd/system/topopt.service > /dev/null <<'EOF'
[Unit]
Description=Topology Optimizer web app
After=network.target

[Service]
WorkingDirectory=/opt/topology-optimizer
ExecStart=/opt/topology-optimizer/.venv/bin/uvicorn main:app --host 0.0.0.0 --port 8000
Restart=on-failure
# Optional hardening: cap memory so a huge job can't take down the box
MemoryMax=7G

[Install]
WantedBy=multi-user.target
EOF
```

Enable and start it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now topopt
sudo systemctl status topopt        # should say "active (running)"
```

Now open `http://your-server-ip:8000` in a browser (open port 8000 in your
firewall/cloud panel first: `sudo ufw allow 8000/tcp` if using ufw).

## 6. Optional: nginx reverse proxy (serve on port 80 / with a domain)

```bash
sudo apt install -y nginx
sudo tee /etc/nginx/sites-available/topopt > /dev/null <<'EOF'
server {
    listen 80;
    server_name your-domain.com;          # or the server IP

    client_max_body_size 210M;            # big STL uploads

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_read_timeout 600s;          # jobs are polled, but be generous
    }
}
EOF
sudo ln -s /etc/nginx/sites-available/topopt /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

If you use nginx, change the systemd `ExecStart` to bind `--host 127.0.0.1`
so the app is only reachable through the proxy, then
`sudo systemctl restart topopt`.

## 7. Updating to a new version

```bash
cd /opt/topology-optimizer
git pull
.venv/bin/pip install -r requirements.txt
sudo systemctl restart topopt
```

## Docker alternative

If you prefer Docker over systemd:

```bash
docker build -t topopt .
docker run -d --name topopt -p 8000:8000 --restart unless-stopped --memory 7g topopt
```

## Troubleshooting

- **`systemctl status topopt` shows failed** — read the log:
  `journalctl -u topopt -n 50 --no-pager`
- **Optimization killed / job errors on a small VPS** — lower the resolution
  slider (48–64) or add swap:
  `sudo fallocate -l 4G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile`
- **Upload fails behind nginx** — make sure `client_max_body_size` is set
  (step 6).
