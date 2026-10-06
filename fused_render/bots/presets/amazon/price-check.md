# Amazon price check and tracker
trigger: good price on amazon, amazon deal, price drop on amazon, track amazon price, amazon price history, is this a good price, watch the price

1. Open the product: goto the link the task gives, or search https://www.amazon.com/s?k=<query> and open the exact match (skip Sponsored). Captcha → `login`.
2. Read the current price, "List Price", any "Typical price" line, the coupon checkbox, the "Subscribe & Save" price and the seller. Note the ASIN from the URL (/dp/<ASIN>).
3. Click "See All Buying Options" (or "New & Used from") and `read` the offer list: lowest new price, lowest used price, their sellers and delivery.
4. goto https://camelcamelcamel.com/product/<ASIN> (if it 404s, paste the Amazon URL into the search box on https://camelcamelcamel.com). `read` the price history table: Current, Highest, Lowest and Average for "Amazon" and "3rd Party New". If the site is blocked, skip it and say so.
5. Judge: current price against the average and the lowest ever; whether the coupon or Subscribe & Save beats it; whether a used or 3rd-party offer is cheaper and from whom.
6. Report: current price and best offer, the historical low and average with dates, a one-line verdict (buy now or wait), the Amazon link. Then `offer` a price tracker app that checks this ASIN daily and notifies the user at a target price.
