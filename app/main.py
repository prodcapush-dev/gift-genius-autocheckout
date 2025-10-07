import os
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, validator
import stripe

# --- Environment ---
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "").strip()
if not STRIPE_SECRET_KEY:
    raise RuntimeError("Missing STRIPE_SECRET_KEY environment variable")
stripe.api_key = STRIPE_SECRET_KEY

# Default service fee in smallest currency unit (e.g., cents)
DEFAULT_SERVICE_FEE_CENTS = int(os.getenv("SERVICE_FEE_CENTS", "99"))  # €0.99 by default

app = FastAPI(title="Gift Genius AutoCheckout V2", version="1.0.0")

# CORS (handy for tests; not strictly required for GPT Actions)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -------- Models --------
class CreateCheckoutBody(BaseModel):
    product_name: str = Field(..., description="Name of the selected product")
    product_price: float = Field(..., gt=0, description="Price, e.g., 35.00")
    currency: str = Field("EUR", description="Three-letter currency code")
    service_fee_cents: int | None = Field(None, description="Override fee in cents; defaults to env")
    quantity: int = Field(1, gt=0, description="Quantity to purchase")
    success_url: str = Field("https://chat.openai.com/", description="Redirect after success")
    cancel_url: str = Field("https://chat.openai.com/", description="Redirect after cancel")
    locale: str | None = Field(None, description="Stripe checkout locale (e.g., en, fr)")

    @validator("currency")
    def currency_upper(cls, v: str) -> str:
        return v.upper().strip()

# -------- Health --------
@app.get("/")
def root():
    return {"status": "ok", "service": "Gift Genius AutoCheckout V2"}

# -------- Create Checkout --------
@app.post("/create_checkout")
def create_checkout(body: CreateCheckoutBody):
    # Convert to smallest unit (cents)
    try:
        product_amount_cents = int(round(body.product_price * 100))
    except Exception:
        raise HTTPException(400, "Invalid product_price format")

    fee_cents = body.service_fee_cents if body.service_fee_cents is not None else DEFAULT_SERVICE_FEE_CENTS
    if fee_cents < 0:
        raise HTTPException(400, "service_fee_cents must be >= 0")

    try:
        session = stripe.checkout.Session.create(
            mode="payment",
            ui_mode="hosted",  # ensure a hosted checkout URL is returned
            line_items=[
                {
                    "price_data": {
                        "currency": body.currency,
                        "product_data": {"name": body.product_name},
                        "unit_amount": product_amount_cents,
                    },
                    "quantity": body.quantity,
                },
                {
                    "price_data": {
                        "currency": body.currency,
                        "product_data": {"name": "Gift Genius Service Fee"},
                        "unit_amount": fee_cents,
                    },
                    "quantity": 1,
                },
            ],
            success_url=body.success_url + "?status=success",
            cancel_url=body.cancel_url + "?status=cancel",
            locale=body.locale,
            allow_promotion_codes=False,
        )

        # Safety: ensure Stripe returned a hosted checkout URL
        if not getattr(session, "url", None):
            raise HTTPException(500, "Stripe did not return a checkout URL")

    except stripe.error.StripeError as e:
        raise HTTPException(400, f"Stripe error: {str(e)}")

    total_cents = product_amount_cents * body.quantity + fee_cents
    return {
        "checkout_url": session.url,
        "currency": body.currency,
        "amount_product_cents": product_amount_cents * body.quantity,
        "amount_service_fee_cents": fee_cents,
        "amount_total_cents": total_cents,
    }

# -------- Webhook (optional, for later automation) --------
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "").strip()

@app.post("/webhook")
async def stripe_webhook(request: Request):
    # In dev without a webhook secret, just acknowledge
    if not STRIPE_WEBHOOK_SECRET:
        return {"received": True, "warning": "No STRIPE_WEBHOOK_SECRET set"}

    payload = await request.body()
    sig_header = request.headers.get("Stripe-Signature", "")
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except Exception as e:
        raise HTTPException(400, f"Webhook signature verification failed: {e}")

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        # TODO: fulfill order / notify / etc.

    return {"received": True}
from fastapi.responses import HTMLResponse

@app.get("/thanks", response_class=HTMLResponse)
def thanks(session_id: str | None = None):
    return f"""
    <html><body style="font-family:system-ui;margin:40px">
      <h1>✅ Payment successful</h1>
      <p>Thanks for your purchase with Gift Genius!</p>
      {'<p>Session: ' + session_id + '</p>' if session_id else ''}
      <p>You can return to the GPT to continue.</p>
    </body></html>
    """

@app.get("/cancel", response_class=HTMLResponse)
def cancel():
    return """
    <html><body style="font-family:system-ui;margin:40px">
      <h1>❌ Payment canceled</h1>
      <p>No charge was made. You can try again anytime.</p>
    </body></html>
    """
