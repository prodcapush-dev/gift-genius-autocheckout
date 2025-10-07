
# Gift Genius AutoCheckout V2 (Starter Pack)

Create a **Stripe checkout** from your GPT, automatically adding a **€0.99 service fee**.

## What you get
- `app/main.py` — FastAPI server with `/create_checkout` endpoint
- `requirements.txt` — dependencies
- `.env.example` — env variables to copy
- `openapi.json` — paste this in your GPT "Create Action" screen
- (Optional) `/webhook` endpoint for later

## Deploy quickly (Render)
- Start command: `uvicorn app.main:app --host 0.0.0.0 --port 10000`
- Env vars: `STRIPE_SECRET_KEY`, optional `SERVICE_FEE_CENTS`, optional `STRIPE_WEBHOOK_SECRET`
