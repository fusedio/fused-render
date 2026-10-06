# Google Maps and Yelp reviews summary
trigger: reviews for, is it any good, what do people say about, reviews of, worth going to, yelp reviews, google reviews

1. goto https://www.google.com/maps/search/<place name>+<city>/ and click the matching result if a list appears. Consent dialog → dismiss; captcha → `login`.
2. `read` the place panel: rating, review count, price level, category, address, hours today, and the "Popular times" sentence.
3. Click the "Reviews" tab. Click "Sort" and choose "Newest"; scroll and `read` the first 20 reviews (stars, date, text, owner reply). Then "Sort" → "Lowest rating" and `read` 10. Use the magnifying-glass "Search reviews" box for a word the task cares about ("wait", "parking", "noise") and read what matches.
4. goto https://www.yelp.com/search?find_desc=<place name>&find_loc=<city>, click the matching business, add ?sort_by=date_desc to its /biz/ URL and `read` the rating, review count and the first 10 reviews. If Yelp shows a captcha or "This page is not available", use `login` once; if still blocked, skip Yelp and say so.
5. Report: Google rating (count) and Yelp rating (count); 3 things people praise and 3 they complain about, each with a date and which site; whether recent reviews trend up or down; a one-line verdict; both links.
