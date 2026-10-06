# Amazon product price and reviews
trigger: amazon price, look up on amazon, amazon reviews, on amazon, amazon product, how much is it on amazon, is it good on amazon

1. goto https://www.amazon.com/s?k=<query>. If the page says "Enter the characters you see below" or "not a robot", use `login`; close any "Deliver to" location popup.
2. `read` the results list and open the exact match by brand and model; skip cards labelled "Sponsored". If several variants or sellers fit, `ask` which one.
3. On the product page (/dp/<ASIN>) read: title, price, "List Price" strike-through, coupon or "Subscribe & Save" offers, "Ships from" and "Sold by", the delivery date line, the variant selectors (size, colour, pack) and the star rating with review count.
4. Scroll to "Customer reviews" and `read` the "Customers say" AI summary with its feature tags, then the "Top reviews" shown on the page.
5. goto https://www.amazon.com/product-reviews/<ASIN>?sortBy=recent&filterByStar=critical and `read` the first page (about 10 critical reviews, newest first). If Amazon asks you to sign in to see more reviews, use `login`.
6. Report: price with any coupon, seller and delivery date; a three-line verdict; the recurring complaint from critical reviews with dates; the product link. If the user will ask this again, `offer` a price tracker app.
