# Deployment Guide — DigitalOcean (Droplet + CloudPanel + Multi-Worker + Managed Redis)

**Architecture:** Backend (FastAPI, multiple uvicorn workers) + Frontend (Next.js) — dono alag CloudPanel sites, ek Droplet par. Shared call-state (active-call dedup, outbound-call registry, extraction dedup) DigitalOcean Managed Redis mein — taake multiple backend workers concurrent calls ko safely handle kar sakein.

**Backend domain:** `api.yourdomain.com`
**Frontend domain:** `app.yourdomain.com`

---

## Step 0 — Supabase Migration (pehle yeh)

Supabase Dashboard → SQL Editor mein run karo:
1. `supabase_schema.sql` (agar fresh DB hai)
2. `migrations/005_multi_tenancy.sql`

---

## Step 1 — Droplet Banao

1. DigitalOcean → **Create → Droplets**
2. Image: **Ubuntu 22.04 LTS**
3. Plan: **4GB RAM / 2 vCPU minimum** (multiple workers + Next.js build dono RAM/CPU leते hain — worker count Droplet ke vCPU count se match karega, isliye jitne workers chahiye utna vCPU lo)
4. Region: apne users ke qareeb, **aur Managed Redis wahi region mein banega** (latency ke liye same region zaroori hai)
5. Auth: SSH Key
6. Create → IP note karo (e.g. `164.90.x.x`)

## Step 2 — DNS + DO Firewall

DNS A records:
```
api.yourdomain.com  →  droplet IP
app.yourdomain.com  →  droplet IP
```

**Networking → Firewalls** — inbound allow: 22 (SSH), 80, 443, 8443 (CloudPanel admin — apne IP tak restrict kar sakte ho).

---

## Step 3 — CloudPanel Install

```bash
ssh root@your-droplet-ip
curl -sS https://installer.cloudpanel.io/ce/v2/install.sh -o install.sh
sudo bash install.sh
```
(Exact one-liner CloudPanel docs se confirm karo, version change ho sakta hai: https://www.cloudpanel.io/docs/v2/getting-started/)

Browser mein `https://your-droplet-ip:8443` khol kar admin account banao.

CloudPanel mein do sites banao:
- `api.yourdomain.com` → Site Type **Python** → Python 3.11 → Port 7860
- `app.yourdomain.com` → Site Type **Node.js** → Node 20 → Port 3000

> Folder domain ke naam se banega: `/home/cloudpanel/htdocs/api.yourdomain.com/` — neeche jahan `invenco` likha hai wahan apna asli folder path use karo.

---

## Step 4 — DigitalOcean Managed Redis/Valkey Banao

1. **Databases → Create Database Cluster**
2. Engine: **Valkey** (Redis-compatible — DigitalOcean ne Redis ko Valkey se replace kar diya hai, same client/protocol)
3. Plan: sabse chota tier kaafi hai (isse sirf 3 chhoti keys/values store hongi — call-dedup state, koi bulk data nahi)
4. Region: **wahi jo Droplet ka hai**
5. Create → cluster ready hone tak wait karo (2-3 min)

### 4.1 — Trusted Sources (zaroori — warna Droplet connect nahi kar payega)
Database → **Settings → Trusted Sources** → apna Droplet add karo (naam se select karo, ya poora VPC add karo agar Droplet usi VPC mein hai)

### 4.2 — Connection String Lo
Database → **Connection Details** → **Connection String** (agar Droplet aur DB same VPC/region mein hain to **Private Network** connection string use karo — faster, public internet se expose nahi hoti)

Format aisa dikhega:
```
rediss://default:AVNS_xxxxxxxxxxxx@private-db-valkey-xxxx.db.ondigitalocean.com:25061
```
(`rediss://` — do `s` — TLS zaroori hai, managed Redis/Valkey plain `redis://` accept nahi karta)

---

## Step 5 — Backend Setup

```bash
cd /home/cloudpanel/htdocs/api.yourdomain.com
git clone https://github.com/yourrepo/your-project.git .
python3.11 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
nano .env
```

`.env` mein (`.env.example` template hai):
```env
SUPABASE_URL=https://xxxx.supabase.co
SUPABASE_ANON_KEY=...
SUPABASE_SERVICE_ROLE_KEY=...
OPENAI_API_KEY=sk-proj-...
UPLIFT_API_KEY=...
PORT=7860
PUBLIC_HOST=https://api.yourdomain.com
ALLOWED_ORIGINS=https://app.yourdomain.com

# ── Redis — multi-worker shared state ──
REDIS_URL=rediss://default:AVNS_xxxxxxxxxxxx@private-db-valkey-xxxx.db.ondigitalocean.com:25061
```

### 5.1 — Redis Connectivity Test (pehle isolate karo)
```bash
source venv/bin/activate
python -c "
import asyncio, redis.asyncio as redis
async def t():
    r = redis.from_url('rediss://default:AVNS_xxxxxxxxxxxx@private-db-valkey-xxxx.db.ondigitalocean.com:25061')
    print(await r.ping())
asyncio.run(t())
"
```
`True` print hona chahiye. Agar timeout ho to Trusted Sources check karo (Step 4.1).

### 5.2 — Worker Count Decide Karo
Droplet ke vCPU count ke barabar workers se shuru karo (2 vCPU Droplet → `--workers 2`). Baad mein CPU usage dekh kar tune karo:
```bash
nproc   # vCPU count dikhata hai
```

### 5.3 — Systemd Service (multiple workers)
```bash
nano /etc/systemd/system/invenco-backend.service
```
```ini
[Unit]
Description=Invenco Backend
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/home/cloudpanel/htdocs/api.yourdomain.com
ExecStart=/home/cloudpanel/htdocs/api.yourdomain.com/venv/bin/uvicorn main:app --host 0.0.0.0 --port 7860 --workers 2
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```
```bash
systemctl daemon-reload
systemctl enable --now invenco-backend
systemctl status invenco-backend

# Verify N worker processes actually came up:
pgrep -af uvicorn
```

> **Nginx/WebSocket ke liye koi change nahi chahiye** — uvicorn ke `--workers` sab worker processes ko ek hi socket par bind karte hain (OS level), Nginx ko sirf port 7860 par proxy karna hai jaisa pehle tha. Ek WebSocket connection jis worker ne accept kiya, poori call ke liye usi worker ke paas rehta hai — ye automatic hai, koi sticky-session config nahi chahiye.

### 5.4 — Startup Logs Check Karo
```bash
journalctl -u invenco-backend -f
```
Har worker apna khud ka log line dega:
```
INFO: Startup: initializing Supabase...
INFO: Redis client initialized.
INFO: Supabase ready — warming models, RAG and greetings in background…
```
Agar `REDIS_URL not set` dikhe to `.env` dobara check karo.

> **Note:** RAG cache aur greeting-prewarm har worker apni memory mein alag se banata hai (ye Redis mein migrate nahi hai) — matlab N workers = N× OpenAI embedding calls at startup aur N× RAM for RAG data. 2-4 workers ke liye ye negligible hai; agar bohot zyada workers chalane ka socho to is cost ko dhyan mein rakhna.

---

## Step 6 — Nginx (Backend) + SSL

CloudPanel → Site → **Vhost** tab mein:
```nginx
location / {
    proxy_pass http://127.0.0.1:7860;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_read_timeout 3600s;
    proxy_send_timeout 3600s;
}
```
CloudPanel → **SSL/TLS → Let's Encrypt**

Verify: `https://api.yourdomain.com/docs` → Swagger UI.

---

## Step 7 — Frontend Setup

```bash
cd /home/cloudpanel/htdocs/app.yourdomain.com
git clone https://github.com/yourrepo/your-project.git .
cd frontend
nano .env.local
```
```env
NEXT_PUBLIC_API_URL=https://api.yourdomain.com
NEXT_PUBLIC_SUPABASE_URL=https://xxxx.supabase.co
NEXT_PUBLIC_SUPABASE_ANON_KEY=...
```
```bash
npm install
npm run build
```

Systemd service:
```bash
nano /etc/systemd/system/invenco-frontend.service
```
```ini
[Unit]
Description=Invenco Frontend
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/home/cloudpanel/htdocs/app.yourdomain.com/frontend
ExecStart=/usr/bin/npm start
Restart=always
RestartSec=5
Environment=NODE_ENV=production
Environment=PORT=3000

[Install]
WantedBy=multi-user.target
```
```bash
systemctl daemon-reload
systemctl enable --now invenco-frontend
```

CloudPanel → Vhost:
```nginx
location / {
    proxy_pass http://127.0.0.1:3000;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```
CloudPanel → **SSL/TLS → Let's Encrypt**

---

## Step 8 — Telnyx Webhook

Telnyx Dashboard → Application → Webhook URL:
```
https://api.yourdomain.com/webhook/{webhook_token}
```
(`webhook_token` user ke settings page se milega, per-user unique.)

---

## Step 9 — Final Checklist

```
[ ] https://api.yourdomain.com/docs  → Swagger dikhta hai
[ ] https://app.yourdomain.com       → login page dikhta hai
[ ] pgrep -af uvicorn                → N worker processes dikhte hain
[ ] Redis ping test (Step 5.1)       → True
[ ] Sign up / sign in kaam karta hai
[ ] Agent create ho jata hai
[ ] Settings mein Telnyx keys save hoti hain, webhook URL dikhti hai
[ ] Ek test inbound call                → recording/transcript save hoti hai
[ ] Ek test outbound call (/dial)       → connect hoti hai (outbound-registry Redis path test karta hai)
[ ] 2 simultaneous calls ek saath        → dono independently kaam karte hain, koi cross-call state leak nahi
[ ] systemctl status invenco-backend/frontend → active (running)
```

---

## Useful Commands

```bash
journalctl -u invenco-backend -f       # backend logs (sab workers interleaved)
journalctl -u invenco-frontend -f      # frontend logs
pgrep -af uvicorn                      # kitne worker processes chal rahe hain

# Redis mein live keys dekhne ke liye (debugging)
redis-cli -u "$REDIS_URL" --tls KEYS '*'

# Code update ke baad:
cd /home/cloudpanel/htdocs/api.yourdomain.com && git pull && systemctl restart invenco-backend
cd /home/cloudpanel/htdocs/app.yourdomain.com/frontend && git pull && npm run build && systemctl restart invenco-frontend

# Worker count badhana/ghatana:
nano /etc/systemd/system/invenco-backend.service   # --workers N badlo
systemctl daemon-reload && systemctl restart invenco-backend
```

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| Backend 502 Bad Gateway | `systemctl status invenco-backend` se logs dekho |
| WebSocket connect nahi ho raha | Nginx vhost mein `Upgrade`/`Connection` headers check karo |
| CORS error browser mein | `.env` mein `ALLOWED_ORIGINS` check karo |
| Redis ping timeout | Trusted Sources mein Droplet add hai check karo (Step 4.1) |
| `REDIS_URL not set` log | `.env` mein `REDIS_URL` line check karo, service restart karo |
| Outbound call inbound jaisi treat ho rahi hai | Redis down/unreachable ho sakta hai — `pgrep -af uvicorn` logs mein "Redis outbound lookup failed" dhundo |
| Telnyx webhook 404 | Backend chal raha hai aur SSL theek hai check karo |
| `pgrep -af uvicorn` mein sirf 1 process | systemd service ka `--workers N` flag check karo, `daemon-reload` kiya tha? |
