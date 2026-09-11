# Deployment Guide — Hostinger VPS + CloudPanel

**Architecture:** Backend (FastAPI) aur Frontend (Next.js) — dono alag applications  
**Backend domain:** `api.yourdomain.com`  
**Frontend domain:** `app.yourdomain.com`

---

## Step 0 — Supabase Migration (Pehle Yeh Karo)

Supabase Dashboard → SQL Editor → New query mein ye files ek ek karke run karo:

1. `supabase_schema.sql` — agar fresh database hai (tables exist nahi karte)
2. `migrations/005_multi_tenancy.sql` — user_settings table + RLS policies

> Agar tables pehle se hain sirf `005_multi_tenancy.sql` chalao.

---

## Step 1 — VPS Par Code Upload Karo

### Option A — Git (Recommended)

```bash
# VPS SSH mein:
cd /home/cloudpanel/htdocs
git clone https://github.com/yourrepo/your-project.git invenco
```

### Option B — FTP/SFTP

CloudPanel → File Manager ya FileZilla se upload karo:
- Backend files → `/home/cloudpanel/htdocs/invenco/`
- Frontend files (same repo mein) → `/home/cloudpanel/htdocs/invenco/frontend/`

---

## Step 2 — Backend Setup (FastAPI)

### 2.1 — CloudPanel mein Python App Banao

1. CloudPanel → **Sites** → **Add Site**
2. Domain: `api.yourdomain.com`
3. Site Type: **Python**
4. Python Version: `3.11`
5. App Root: `/home/cloudpanel/htdocs/invenco`
6. App Port: `7860`

### 2.2 — SSH se Backend Directory Mein Jao

```bash
ssh root@your-vps-ip
cd /home/cloudpanel/htdocs/invenco
```

### 2.3 — Virtual Environment Banao

```bash
python3.11 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2.4 — `.env` File Banao

```bash
nano .env
```

Andar ye paste karo (apni values se replace karo):

```env
# ── Supabase ──────────────────────────────────────
SUPABASE_URL=https://xxxxxxxxxxxx.supabase.co
SUPABASE_ANON_KEY=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...
SUPABASE_SERVICE_ROLE_KEY=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...

# ── AI Services ───────────────────────────────────
OPENAI_API_KEY=sk-proj-...
UPLIFT_API_KEY=your-uplift-key

# ── ElevenLabs (agar English TTS use kar rahe ho) ─
ELEVENLABS_API_KEY=
ELEVENLABS_VOICE_ID=

# ── Server ────────────────────────────────────────
PORT=7860
PUBLIC_HOST=https://api.yourdomain.com

# ── CORS — frontend ka domain ─────────────────────
ALLOWED_ORIGINS=https://app.yourdomain.com
```

Ctrl+X → Y → Enter (save karo)

### 2.5 — Backend Test Karo (manually)

```bash
source venv/bin/activate
python main.py
# ya
uvicorn main:app --host 0.0.0.0 --port 7860
```

Output mein ye aana chahiye:
```
INFO: Startup: initializing Supabase...
INFO: Supabase ready — warming models...
INFO: Uvicorn running on http://0.0.0.0:7860
```

Ctrl+C se band karo, agle step par jao.

### 2.6 — Systemd Service Banao (Auto-restart)

```bash
nano /etc/systemd/system/invenco-backend.service
```

Paste karo:

```ini
[Unit]
Description=Invenco Call Agent Backend
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/home/cloudpanel/htdocs/invenco
ExecStart=/home/cloudpanel/htdocs/invenco/venv/bin/uvicorn main:app --host 0.0.0.0 --port 7860
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

Service enable aur start karo:

```bash
systemctl daemon-reload
systemctl enable invenco-backend
systemctl start invenco-backend

# Status check karo:
systemctl status invenco-backend
```

### 2.7 — Nginx WebSocket Config (CRITICAL)

CloudPanel → Sites → `api.yourdomain.com` → **Vhost** tab

Existing config mein `location /` block dhundo aur replace karo:

```nginx
location / {
    proxy_pass http://127.0.0.1:7860;
    proxy_http_version 1.1;

    # WebSocket support (Telnyx audio stream ke liye zaroori)
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";

    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;

    # Long-running WebSocket connections ke liye timeout badhao
    proxy_read_timeout 3600s;
    proxy_send_timeout 3600s;
    proxy_connect_timeout 60s;
}
```

Save karo → Nginx reload hoga automatically.

### 2.8 — SSL Certificate (HTTPS)

CloudPanel → Sites → `api.yourdomain.com` → **SSL/TLS** → **Let's Encrypt**

> Telnyx webhooks HTTPS require karta hai — SSL zaroori hai.

### 2.9 — Backend Verify Karo

Browser mein jao:
```
https://api.yourdomain.com/docs
```
FastAPI Swagger UI dikhni chahiye.

---

## Step 3 — Frontend Setup (Next.js)

### 3.1 — CloudPanel mein Node.js App Banao

1. CloudPanel → **Sites** → **Add Site**
2. Domain: `app.yourdomain.com`
3. Site Type: **Node.js**
4. Node.js Version: `20`
5. App Root: `/home/cloudpanel/htdocs/invenco/frontend`
6. App Port: `3000`

### 3.2 — Frontend Directory Mein Jao

```bash
cd /home/cloudpanel/htdocs/invenco/frontend
```

### 3.3 — `.env.local` File Banao

```bash
nano .env.local
```

Paste karo:

```env
NEXT_PUBLIC_API_URL=https://api.yourdomain.com
NEXT_PUBLIC_SUPABASE_URL=https://xxxxxxxxxxxx.supabase.co
NEXT_PUBLIC_SUPABASE_ANON_KEY=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...
```

Ctrl+X → Y → Enter

### 3.4 — Dependencies Install aur Build Karo

```bash
npm install
npm run build
```

Build successfully complete honi chahiye (`✓ Compiled successfully`).

### 3.5 — Systemd Service Banao (Frontend)

```bash
nano /etc/systemd/system/invenco-frontend.service
```

Paste karo:

```ini
[Unit]
Description=Invenco Call Agent Frontend
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/home/cloudpanel/htdocs/invenco/frontend
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
systemctl enable invenco-frontend
systemctl start invenco-frontend

# Status check karo:
systemctl status invenco-frontend
```

### 3.6 — Nginx Config (Frontend)

CloudPanel → Sites → `app.yourdomain.com` → **Vhost**

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

### 3.7 — SSL Certificate (Frontend)

CloudPanel → Sites → `app.yourdomain.com` → **SSL/TLS** → **Let's Encrypt**

---

## Step 4 — Telnyx Webhook Configure Karo

Telnyx Dashboard → Your Application → **Webhook URL** mein ye set karo:

```
https://api.yourdomain.com/webhook/{webhook_token}
```

> `webhook_token` har user ka alag hoga — user jab settings page par Telnyx credentials save karega tab automatically generate hoga. User ko apna personal webhook URL settings page par dikhega.

---

## Step 5 — Final Verification Checklist

```
[ ] https://api.yourdomain.com/docs    → FastAPI Swagger dikhta hai
[ ] https://app.yourdomain.com         → Login page dikhta hai
[ ] Sign up / Sign in kaam karta hai
[ ] Dashboard load hota hai
[ ] Agent create ho jata hai
[ ] Settings page par Telnyx keys save hoti hain
[ ] Webhook URL settings page par dikhti hai
[ ] systemctl status invenco-backend   → active (running)
[ ] systemctl status invenco-frontend  → active (running)
```

---

## Useful Commands (Baad Mein Kaam Aayenge)

```bash
# Logs dekhne ke liye
journalctl -u invenco-backend -f
journalctl -u invenco-frontend -f

# Service restart karne ke liye
systemctl restart invenco-backend
systemctl restart invenco-frontend

# Code update ke baad (git pull + restart)
cd /home/cloudpanel/htdocs/invenco
git pull
systemctl restart invenco-backend

# Frontend code update ke baad
cd /home/cloudpanel/htdocs/invenco/frontend
git pull
npm run build
systemctl restart invenco-frontend
```

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| Backend 502 Bad Gateway | `systemctl status invenco-backend` se logs dekho |
| WebSocket connect nahi ho raha | Nginx vhost mein `Upgrade` aur `Connection` headers check karo |
| CORS error browser mein | `.env` mein `ALLOWED_ORIGINS` mein frontend domain check karo |
| Frontend API calls fail | `.env.local` mein `NEXT_PUBLIC_API_URL` check karo |
| Telnyx webhook 404 | Backend chal raha hai aur SSL theek hai check karo |
| `supabase_schema` error | Supabase mein `005_multi_tenancy.sql` run karo |
