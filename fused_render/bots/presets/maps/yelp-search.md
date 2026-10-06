# Yelp top-rated search with filters
trigger: on yelp, yelp search, find on yelp, yelp top rated, best rated on yelp, yelp near, most reviewed on yelp

1. goto https://www.yelp.com/search?find_desc=<what>&find_loc=<city or neighbourhood>&sortby=rating (sortby=review_count for most reviewed; recommended is the default). If Yelp shows a captcha or "This page is not available", use `login`; close "Get the app" and sign-up popups.
2. Apply the task's filters with the chips at the top: "Open Now", price "$" to "$$$$", "Offers Delivery", "Reservations", "Good for Kids", or the "All" filters panel. Note which applied.
3. `read` the results list (10 per page; entries marked "Ad" or "Sponsored" come first, skip them): name, rating, review count, price, category, neighbourhood, open or closed. Click "Next" (start=10) and `read` page 2.
4. Open the top 3 by clicking the name (/biz/<slug>) and `read`: address, hours today, amenities, and the first 5 reviews after adding ?sort_by=date_desc to the URL.
5. Report a ranked list: name, Yelp rating (count), price, neighbourhood, open until, one-line why, Yelp link; if the task asked for both sites, add the Google Maps rating for the top 3 from https://www.google.com/maps/search/<name>+<city>/. If more than 8 results, `save` as yelp-<what>.md.
