## Real incident — confirmed and fixed

**Root cause.** `app/main.py:60` computed the express delivery estimate with `placed_at.replace(day=placed_at.day + 2)`. `datetime.replace` sets the day field within the *same* month rather than doing date arithmetic, so any express order placed within two days of month end raises `ValueError: day is out of range for month`. `order_detail` is called by `load_order`, so the unhandled exception surfaces as a 500 on `GET /api/orders/{order_id}` — matching the stack trace, span status, and 500 counter in the evidence.

The trigger is the seed data at `app/main.py:40-43`: `express-1002` is deliberately seeded with `previous_month_end`. Today is 2026-09-28, so that row's `created_at` is 2026-08-31, and `day=33` is invalid. This is the only express order in the seed set, and `GET /api/orders` doesn't call `order_detail`, which is why the existing suite stayed green.

**Fix** (`app/main.py:60`): `estimated_at = placed_at + timedelta(days=2)`. `timedelta` was already imported; this is a one-line change that rolls over month and year boundaries correctly.

**Regression test** (`tests/test_api.py`): `test_express_estimated_delivery_crosses_month_boundary` fetches `express-1002` and asserts a 200 plus the correct `estimated_delivery`. I verified it is a genuine regression test by temporarily restoring the buggy line — it failed with the exact `ValueError: day is out of range for month` at `app/main.py:60` — then restored the fix. Full suite: **4 passed**.

**Deploy.** `docker compose up --build -d --wait app` rebuilt the image from the fixed source and the container came up healthy.

**One gap you should close.** I could not run the final live HTTP check. `curl`, `docker compose exec`, and a Python `urlopen` from Bash were all denied by the current permission mode, and `WebFetch` refuses localhost. So the post-deploy confirmation rests on the regression test plus a healthy rebuild, not on an actual request to port 8000. Please run:

```
curl -si http://localhost:8000/api/orders/express-1002
```

It should return `200` with `"estimated_delivery": "2026-09-02"`. If you'd rather I verify it myself, grant Bash permission for `curl` and I'll re-run it.

Nothing is committed — the diff above is yours to review.

RESULT: fixed - `datetime.replace(day=...)` overflowed for express orders placed near month end; replaced with `timedelta(days=2)`, added a regression test, and rebuilt the app, though the final live curl check was blocked by permissions.
