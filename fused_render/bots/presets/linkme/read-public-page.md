# Linkme read a public bio page
trigger: linkme.bio, what's on their linkme, linkme page of, this linkme page, bio page of, competitor link in bio, their link in bio

1. goto https://linkme.bio/<handle> from the task (any handle; public pages need no login). If the task gives a different link-in-bio address, open it and treat it the same way.
2. `read` the header: profile name, bio text, social icons, WhatsApp button, verified badge.
3. Scroll to the end and `read` every block in order: title, type (link, product, digital product, PIX card), URL, price for products.
4. If the task asks where the links go, open at most 5 with tab new, `read` the destination's first screen, and tab close each.
5. Report: the page owner, the blocks in order with URLs, which are products with prices, what the page pushes hardest (top position, buttons), and the page link. Comparing with my page? goto https://linkme.bio/<my-username>, `read` it, and add a second column. `save` as linkme-<handle>.md if more than 15 blocks.
