
import os
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, validator
import stripe

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "").strip()
if not STRIPE_SECRET_KEY:
    raise RuntimeError("Missing STRIPE_SECRET_KEY environment variable")
stripe.api_key = STRIPE_SECRET_KEY

DEFAULT_SERVICE_FEE_CENTS = int(os.getenv("SERVICE_FEE_CENTS", "99"))

app = FastAPI(title="Gift Genius AutoCheckout V2", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class CreateCheckoutBody(BaseModel):
    product_name: str = Field(...)
    product_price: float = Field(..., gt=0)
    currency: str = Field("EUR")
    service_fee_cents: int | None = Field(None)
    quantity: int = Field(1, gt=0)
    success_url: str = Field("https://chat.openai.com/")
    cancel_url: str = Field("https://chat.openai.com/")
    locale: str | None = Field(None)
    @validator("currency")
    def currency_upper(cls, v): return v.upper().strip()

@app.get("/")
def root():
    return {"status": "ok", "service": "Gift Genius AutoCheckout V2"}

@app.post("/create_checkout")
def create_checkout(body: CreateCheckoutBody):
    try:
        product_amount_cents = int(round(body.product_price * 100))
    except Exception:
        raise HTTPException(400, "Invalid product_price format")
    fee_cents = body.service_fee_cents if body.service_fee_cents is not None else DEFAULT_SERVICE_FEE_CENTS
    if fee_cents < 0: raise HTTPException(400, "service_fee_cents must be >= 0")
    try:
        session = stripe.checkout.Session.create(
            mode="payment",
            line_items=[
                {"price_data": {"currency": body.currency, "product_data": {"name": body.product_name}, "unit_amount": product_amount_cents}, "quantity": body.quantity},
                {"price_data": {"currency": body.currency, "product_data": {"name": "Gift Genius Service Fee"}, "unit_amount": fee_cents}, "quantity": 1},
            ],
            success_url=body.success_url + "?status=success",
            cancel_url=body.cancel_url + "?status=cancel",
            locale=body.locale,
            allow_promotion_codes=False,
        )
    except stripe.error.StripeError as e:
        raise HTTPException(400, f"Stripe error: {str(e)}")
    total_cents = product_amount_cents * body.quantity + fee_cents
    return {"checkout_url": session.url, "currency": body.currency, "amount_product_cents": product_amount_cents * body.quantity, "amount_service_fee_cents": fee_cents, "amount_total_cents": total_cents}

STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "").strip()

@app.post("/webhook")
async def stripe_webhook(request: Request):
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
    return {"received": True}
