# Amazon orders and deliveries
trigger: my amazon orders, where is my package, amazon delivery, track my amazon order, amazon order status, did my amazon order ship, amazon returns, amazon refund

1. goto https://www.amazon.com/your-orders/orders?timeFilter=last30 (timeFilter=months-3 or year-2026 for older orders; the "past 3 months" dropdown on the page does the same). If a sign-in page or 2FA appears, use `login`.
2. `read` the order list: for each order the date, total, items, status line ("Arriving", "Delivered", "Return started", "Cancelled") and the delivery estimate. Read page 2 only if the task's period needs it.
3. For every order not yet delivered (at most 5), click "Track package", read the latest scan and expected date, then `back`.
4. If the task is about a return or refund, click "View order details" for that order and read the return or refund status. Do not click "Return or replace items".
5. Report: arriving soon (item, expected date, carrier status) first, then delivered this period, then returns, refunds and cancellations, each with its order link. If the user asked for a full history or more than 10 orders, `save` as amazon-orders.md.
