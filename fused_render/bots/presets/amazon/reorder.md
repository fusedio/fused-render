# Amazon add to cart or reorder
trigger: add to cart on amazon, buy on amazon, reorder on amazon, order again on amazon, amazon buy again, put in my amazon cart, amazon cart

1. For a repeat purchase goto https://www.amazon.com/gp/buyagain, `read` the list and open the matching item; otherwise search https://www.amazon.com/s?k=<query> and open the exact match (skip Sponsored). Sign-in or captcha → `login`.
2. On the product page pick the variant and quantity the task gives; if the task is ambiguous (size, pack count, seller), `ask` with the options you see.
3. Read the exact line items before acting: title, variant, quantity, unit price, seller, delivery date, and Subscribe & Save or coupon if the task wants it.
4. Stop before "Add to Cart" or "Buy Now", show the user exactly what will happen, and proceed only after approval.
5. After "Add to Cart" is approved and clicked, goto https://www.amazon.com/cart and `read` the cart: items, quantities, subtotal; remove nothing. Never click "Proceed to checkout" or "Place your order" unless the task says to buy, and even then stop at "Place your order" for approval; payment and 2FA stay with the user.
6. Report the cart contents, subtotal and the cart link, and say what the user still has to do to finish.
