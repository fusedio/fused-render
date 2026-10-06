# Facebook Marketplace search
trigger: marketplace, facebook marketplace, for sale near me, find on marketplace, marketplace listings, second hand on facebook, used on facebook

1. goto https://www.facebook.com/marketplace/search/?query=<query>&sortBy=creation_time_descend and add the filters the task gives: &minPrice=N&maxPrice=N, &daysSinceListed=1|7|30, &exact=true for an exact phrase. Other sorts: best_match, price_ascend, price_descend, distance_ascend. Marketplace needs the login: use `login` on a sign-in page.
2. `read` the left panel: the location line ("<city> · <N> km") and the Delivery method chips (Local pickup / Shipping). If the task names another place or radius, click the location line, type the place, set the radius slider, Apply.
3. `read` the listing grid: title, price (a struck-through price means it was reduced), location, distance. Scroll twice if fewer than 20 listings are visible.
4. Collect 20 listings. Open the 5 best matches (click the card): `read` price, condition, description, "Listed N ago", pickup location, seller name and "Joined Facebook in <year>". Press Escape or `back` to return. Never click Message, "Send" or "Make offer".
5. Report a table ranked by fit: title, price, condition, location, listed when, one-line note, link https://www.facebook.com/marketplace/item/<id>/ . Flag warning signs (price far below the others, seller joined this year, "contact me on WhatsApp"). Over 10 rows: also `save` marketplace-<query>.csv.
