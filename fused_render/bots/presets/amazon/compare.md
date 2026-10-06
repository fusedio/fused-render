# Amazon compare products
trigger: compare on amazon, which should i buy on amazon, best on amazon, amazon alternatives, amazon shortlist, compare these on amazon, options on amazon

1. goto https://www.amazon.com/s?k=<query>. Apply the task's filters by clicking them in the left rail ("Customer Reviews" 4 Stars & Up, a "Price" range, "Brand", "Prime" or "Get It by" delivery) or by adding &s=review-rank (best rated first) or &s=price-asc-rank (cheapest first) to the URL. Captcha → `login`.
2. `read` the first page of results (16-48 cards): title, price, rating, review count, "Sponsored" label. Pick 3 candidates (4 if the task names them): not sponsored, highest rating with at least a few hundred reviews, inside the budget.
3. Open each candidate's /dp/ page in turn (`back` between them) and read price, coupon, seller, delivery date, the bullet-point specs and the "Customers say" summary with its negative tags.
4. For each, goto https://www.amazon.com/product-reviews/<ASIN>?sortBy=recent&filterByStar=critical and `read` the first page to find the deal-breaker complaint.
5. Report a table: product, price, rating (count), key spec, delivery, deal-breaker, link; then one line naming the pick and why. If the task compares more than 4 products, `save` the table as amazon-compare.md and keep the chat to the pick.
