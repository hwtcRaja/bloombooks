# BloomBooks — HWTC Purchasing & Reimbursement System

Part of the Horizon West Theater Company volunteer tools suite.
Suite 108 — because every great show needs a paper trail.

---

## Run locally

```bash
pip install flask flask-cors cloudinary gunicorn
python app.py
# Open http://localhost:5001
```

## Demo accounts

| Email | Password | Role |
|---|---|---|
| admin@horizonwest.org | admin123 | Admin |
| treasurer@horizonwest.org | treasurer123 | Treasurer |
| president@horizonwest.org | president123 | President |
| volunteer@horizonwest.org | volunteer123 | Volunteer |

---

## Deploy to Railway

Push to GitHub, connect to Railway, then set these environment variables:

```
SECRET_KEY            = any-long-random-string
CLOUDINARY_CLOUD_NAME = your_cloud_name
CLOUDINARY_API_KEY    = your_api_key
CLOUDINARY_API_SECRET = your_api_secret
EMAIL_HOST            = smtp.gmail.com
EMAIL_PORT            = 587
EMAIL_USER            = your@gmail.com
EMAIL_PASS            = your-gmail-app-password
APP_URL               = https://your-app.up.railway.app
```

BloomBooks is part of the HWTC volunteer tools suite alongside RoleCall.
