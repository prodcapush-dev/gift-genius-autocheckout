# app/main.py
import os
import re
import html
import logging
from typing import Optional
from urllib.parse import urlencode, urlparse, parse_qsl, urlunparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field, validator
import stripe

# =========================
# Logging
# =========================
logging.basicConfig(level=logging.INFO)

# =========================
# Environment & Defaults
# =========================
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "").strip()
if not STRIPE_SECRET_KEY:
    raise RuntimeError("Missing STRIPE_SECRET_KEY environment variable")
stripe.api_key = STRIPE_SECRET_KEY

SERVICE_FEE_CENTS = int(os.getenv("SERVICE_FEE_CENTS", "99"))  # €0.99 default
DEFAULT_SUCCESS_URL = os.getenv("SUCCESS_URL", "https://gift-genius-autocheckout.onrender.com/thanks")
DEFAULT_CANCEL_URL  = os.getenv("CANCEL_URL",  "https://gift-genius-autocheckout.onrender.com/cancel")
GPT_RETURN_URL      = os.getenv("GPT_RETURN_URL", "https://chat.openai.com/")

# session id format guard (prevents invented/truncated ids)
SESSION_ID_RE = re.compile(r"^cs_(test|live)_[A-Za-z0-9]{24,}$")

# =========================
# App
# =========================
app = FastAPI(title="Gift Genius AutoCheckout", version="2.4.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"]
)

# =========================
# Helpers
# =========================
def add_params(url: str, **params) -> str:
    """Safely add query params to a URL (handles ? vs &)."""
    u = urlparse(url)
    q = dict(parse_qsl(u.query))
    for k, v in params.items():
        if v is not None:
            q[k] = v
    return urlunparse(u._replace(query=urlencode(q)))

# =========================
# Models
# =========================
class CreateCheckoutBody(BaseModel):
    product_name: str = Field(..., description="Name of the product")
    product_price: float = Field(..., gt=0, description="Price, e.g., 35.00")
    currency: str = Field("EUR", description="ISO currency (EUR, USD, GBP…)")
    quantity: int = Field(1, gt=0)
    service_fee_cents: Optional[int] = Field(None, description="Override service fee in cents")
    success_url: Optional[str] = Field(None, description="Redirect after success")
    cancel_url: Optional[str] = Field(None, description="Redirect after cancel")
    locale: Optional[str] = Field(None, description="Stripe checkout locale, e.g. 'fr'")

    @validator("currency")
    def currency_upper(cls, v: str) -> str:
        return v.upper().strip()

# =========================
# Health / Debug
# =========================
@app.get("/")
def root():
    return {
        "status": "ok",
        "service": "Gift Genius AutoCheckout",
        "mode": "test" if STRIPE_SECRET_KEY.startswith("sk_test_") else "live",
    }

@app.get("/debug")
def debug():
    try:
        acct = stripe.Account.retrieve()
        return {
            "runtime_mode": "test" if STRIPE_SECRET_KEY.startswith("sk_test_") else "live",
            "stripe_account": acct.get("id"),
            "stripe_livemode": acct.get("livemode"),
        }
    except Exception as e:
        raise HTTPException(400, f"Stripe debug failed: {e}")

# =========================
# Create Checkout
# =========================
@app.post("/create_checkout")
def create_checkout(body: CreateCheckoutBody):
    # Convert price to minor units
    try:
        unit_amount = int(round(body.product_price * 100))
    except Exception:
        raise HTTPException(400, "Invalid product_price format")

    if body.service_fee_cents is not None and body.service_fee_cents < 0:
        raise HTTPException(400, "service_fee_cents must be >= 0")

    fee_cents = SERVICE_FEE_CENTS if body.service_fee_cents is None else body.service_fee_cents
    products_total = unit_amount * body.quantity

    # Clean redirect URLs (server defaults + placeholders)
    success_base = body.success_url or DEFAULT_SUCCESS_URL
    cancel_base  = body.cancel_url  or DEFAULT_CANCEL_URL
    success_url  = add_params(success_base, session_id="{CHECKOUT_SESSION_ID}", status="success")
    cancel_url   = add_params(cancel_base, status="cancel")

    try:
        session = stripe.checkout.Session.create(
            mode="payment",
            ui_mode="hosted",
            line_items=[
                {
                    "price_data": {
                        "currency": body.currency,
                        "product_data": {"name": body.product_name},
                        "unit_amount": unit_amount,
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
            success_url=success_url,
            cancel_url=cancel_url,
            locale=body.locale,
            allow_promotion_codes=False,
        )

        if not getattr(session, "url", None):
            raise HTTPException(500, "Stripe did not return a checkout URL")

        logging.info(
            "[stripe] session_id=%s livemode=%s url_has_hash=%s",
            session.id, session.livemode, ("#" in (session.url or ""))
        )

    except stripe.error.StripeError as e:
        raise HTTPException(400, f"Stripe error: {str(e)}")

    return {
        "checkout_url": session.url,   # may include a # fragment
        "redirect_url": f"https://gift-genius-autocheckout.onrender.com/r/{session.id}",  # safe for chat UIs
        "currency": body.currency,
        "amount_product_cents": products_total,
        "amount_service_fee_cents": fee_cents,
        "amount_total_cents": products_total + fee_cents,
    }

# =========================
# Safe Redirect (guards fake ids)
# =========================
@app.get("/r/{session_id}")
def redirect_to_stripe(session_id: str):
    # refuse invented/truncated ids before querying Stripe
    if not SESSION_ID_RE.match(session_id):
        raise HTTPException(status_code=400, detail="Invalid checkout session id format.")

    try:
        sess = stripe.checkout.Session.retrieve(session_id)
        url = getattr(sess, "url", None)
        if not url:
            raise HTTPException(404, "Checkout session has no URL")
        return RedirectResponse(url, status_code=302)
    except stripe.error.StripeError as e:
        raise HTTPException(400, f"Stripe error: {str(e)}")

# =========================
# Thank you & Cancel pages (robust to placeholders)
# =========================
@app.get("/thanks", response_class=HTMLResponse)
def thanks(session_id: Optional[str] = None, status: Optional[str] = None):
    """
    Friendly thank-you page.
    Guard: don't call Stripe if 'session_id' is a placeholder (e.g., {CHECKOUT_SESSION_ID}) or missing.
    """
    invalid = (
        not session_id or
        "CHECKOUT_SESSION_ID" in session_id or
        "{" in session_id or
        "}" in session_id
    )

    amount_total = None
    currency = None
    items_html = ""
    email = None

    if not invalid:
        try:
            sess = stripe.checkout.Session.retrieve(
                session_id,
                expand=["line_items.data.price.product", "customer_details"],
            )
            amount_total = (sess.amount_total or 0) / 100.0
            currency = (sess.currency or "eur").upper()
            email = (sess.get("customer_details") or {}).get("email")

            lines = (sess.get("line_items") or {}).get("data", [])
            rows = []
            for li in lines:
                name = li.get("description") or "Item"
                qty = li.get("quantity", 1)
                unit = ((li.get("price") or {}).get("unit_amount") or 0) / 100.0
                rows.append(
                    f"<tr><td>{html.escape(str(name))}</td>"
                    f"<td>x{qty}</td>"
                    f"<td style='text-align:right'>{unit:.2f}</td></tr>"
                )
            if rows:
                items_html = "<table style='width:100%;border-collapse:collapse'>" + "".join(rows) + "</table>"
        except Exception:
            pass

    return f"""
    <html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Payment successful</title>
    <style>
      body {{ font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; margin: 32px; }}
      .card {{ max-width: 680px; margin:auto; padding:24px; border:1px solid #eee; border-radius:16px; }}
      .ok {{ color:#0a7f2e; font-weight:700; font-size:28px; }}
      .btn {{ display:inline-block; margin-top:16px; padding:12px 16px; border-radius:10px; background:#0b5fff; color:#fff; text-decoration:none }}
      .muted {{ color:#666; font-size:14px }}
      table td {{ padding:6px 0; border-bottom:1px solid #f1f1f1; }}
    </style>
    </head>
    <body>
      <div class="card">
        <div class="ok">✅ Payment successful</div>
        <p>Thanks for your purchase with <strong>Gift Genius</strong>!</p>
        {('<p class="muted">Receipt will be sent to: ' + html.escape(email) + '</p>') if email else ''}
        {items_html}
        {f"<p><strong>Total paid:</strong> {amount_total:.2f} {currency}</p>" if amount_total else ''}
        <a class="btn" href="{html.escape(GPT_RETURN_URL)}">Back to Gift Genius</a>
        <p class="muted">You can return to the GPT to continue.</p>
      </div>
    </body></html>
    """

@app.get("/cancel", response_class=HTMLResponse)
def cancel(status: Optional[str] = None):
    return f"""
    <html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Payment canceled</title>
    <style>
      body {{ font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; margin: 32px; }}
      .card {{ max-width: 680px; margin:auto; padding:24px; border:1px solid #eee; border-radius:16px; }}
      .bad {{ color:#a10; font-weight:700; font-size:28px; }}
      .btn {{ display:inline-block; margin-top:16px; padding:12px 16px; border-radius:10px; background:#0b5fff; color:#fff; text-decoration:none }}
      .muted {{ color:#666; font-size:14px }}
    </style>
    </head>
    <body>
      <div class="card">
        <div class="bad">❌ Payment canceled</div>
        <p>No charge was made.</p>
        <a class="btn" href="{html.escape(GPT_RETURN_URL)}">Back to Gift Genius</a>
        <p class="muted">You can try again anytime.</p>
      </div>
    </body></html>
    """

# =========================
# Webhook (optional)
# =========================
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
        # session = event["data"]["object"]
        # TODO: fulfill order / mark as paid
        pass

    return {"received": True}
