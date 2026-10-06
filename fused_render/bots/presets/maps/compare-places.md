# Google Maps compare two or three places
trigger: compare these places, which is better, this place or that, compare restaurants, which one should we go to, which hotel, pick between

1. For each place (2 or 3), goto https://www.google.com/maps/search/<name>+<city>/ and open it. Consent dialog → dismiss; captcha → `login`.
2. `read` each place panel: rating, review count, price level, address, hours today, the "Popular times" sentence, and the amenities under "About" (reservations, outdoor seating, parking, wheelchair access).
3. In each place's "Reviews" tab click "Sort" → "Newest" and `read` 10, then "Sort" → "Lowest rating" and `read` 5. Note the standout praise and the standout complaint with dates.
4. If the task gives a starting point, goto https://www.google.com/maps/dir/?api=1&origin=<start>&destination=<place>&travelmode=driving (or transit, walking) for each and read the minutes and distance.
5. Report a side-by-side table: rating (count), price, hours today, travel time, standout praise, standout complaint, the amenities that matter for the task; then one line naming the pick and why, with each place's Maps link.
