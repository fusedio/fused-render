# Google Maps places near an address
trigger: near me, nearby, restaurants near, coffee near, places around, open now near

1. goto https://www.google.com/maps/search/<what>+near+<address or neighbourhood>/ (for example /maps/search/ramen+near+Mission+District+San+Francisco/). Dismiss the "Before you continue to Google" consent dialog; an "unusual traffic" captcha → `login`. Close any sign-in or location prompt.
2. Apply the task's constraints with the filter chips above the list: "Open now", "Rating" (4.0+ or 4.5+), "Price" ($ to $$$$), "Hours"; the "Relevance" chip switches to "Distance". If a chip is missing, put the words in the query instead ("open now", "cheap").
3. `read` the results list on the left (10-20 entries): name, rating, review count, price level, category, address or distance, "Open" or "Closed" with the closing time; scroll the list once to load more. You cannot see the map, so use the list only.
4. Rank by rating with at least 50 reviews, then distance; drop anything closed at the time the user wants to go. Open the top 4 by clicking their names and `read` the place panel: address, hours today, phone, website, the "Popular times" sentence, and the 3 review snippets.
5. Report a ranked list: name, rating (count), price, distance, open until, one-line why, Maps link (the /maps/place/ URL from the address bar). If the task asked for more than 8 places, `save` as places-<what>.md and keep the chat to the top 5.
