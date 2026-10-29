# AI agent procedures: master copies

Zendesk AI agents → Content → Use cases. To change a procedure, edit the text here, then paste the **whole** procedure into the **Update procedure** box. Pasting only part of it changes the flow in unexpected ways. Afterwards, check the canvas, then **Save version** and **Publish procedure**.

Shared setup:
- Integrations: **Shopify_Auth**, which gets the token (session params `token` and `expiresIn`), **Shopify_Order_Lookup**, which looks up the order by number and email, and **Shopify_Product_Search**, which searches the catalog (see section 5).
- Session params from the lookup: order_name, order_status, shipment_status, carrier, tracking_number, tracking_url, estimated_delivery, delivered_at, package_count, all_package_statuses, payment_status, total_refunded, refund_count, last_refund_date, order_created_at, cancelled_at.
- Only orders from the last 60 days can be found, because the `read_all_orders` permission wasn't granted.

---

## 1. Order status

```
1. If the customer hasn't given both, ask for their order number and the email address they used at checkout.

2. Remove any "#" sign and spaces from the order number. If it isn't 4–8 digits, ask them to check it and send it again.

3. Use the Shopify_Order_Lookup integration with the order number and email.

4. If the order is found: tell the customer the order status. If it has shipped, give the carrier, tracking number and tracking link, and the estimated delivery date if there is one. If it hasn't shipped yet, explain that orders are processed within 1–2 business days and then take 5–7 business days to arrive.

5. If no order is found: say exactly "I couldn't find an order matching those details. Please check the order number and the email you used at checkout." Let them try again. After 3 failed attempts in total, stop looking up orders and escalate to a team member.

6. If the lookup fails for any other reason: apologize that you can't check the order right now, and escalate to a team member.

7. If tracking shows the order as delivered but the customer says they don't have it, or the order is more than 9 business days old and still hasn't shipped, give them the status and then escalate to a team member.

After a successful lookup, use the session parameters order_name, order_status, shipment_status, carrier, tracking_number, tracking_url and estimated_delivery to reply. Explain statuses in plain words and always include the tracking link when there is one. Show estimated_delivery as a friendly date like "Saturday, October 17". If shipment_status is DELIVERED, don't mention the estimated delivery date.

Rules:
- Never say whether an order number exists, or which detail was wrong.
- Never share a delivery address, phone number, name, payment information or the list of items.
- Only look up orders using details the customer gave in this conversation.
```

---

## 2. Damaged, missing or wrong item

```
1. Ask for the order number and the email used at checkout, unless the customer already gave them. Remove any "#" sign and spaces from the order number.

2. Use the Shopify_Order_Lookup integration to find the order. If no order is found, say "I couldn't find an order matching those details. Please check the order number and the email you used at checkout." After 3 failed attempts, escalate to a team member.

3. Ask which applies, unless it's already clear: the item arrived damaged, part of the order is missing, or the wrong item arrived.

4. DAMAGED:
- Ask which item is damaged. Then ask for the photos one at a time: first a photo of the damaged item and wait for it; once it's received, ask for a photo of the shipping box. Explain both photos are needed to process the claim. Customers can send only one photo per message, so never ask for both in the same message, and don't repeat a request for a photo already received.
- If delivered_at is more than 14 days ago, don't refuse. Only tell the customer our team will review the claim.
- Tell the customer that our team will review the photos and follow up with next steps by email within 1 business day. Do not promise a replacement or say that they can keep the item. Then escalate to a team member.

5. MISSING ITEMS:
- If package_count is more than 1 and any package in all_package_statuses is not DELIVERED, explain that their order was sent in several packages, the rest is still on its way, and share the tracking details. Ask them to get back in touch if it hasn't arrived within 2 business days of the last estimated delivery date.
- Otherwise, ask which items are missing and for a photo of the packing slip if they have it, then escalate to a team member.

6. WRONG ITEM:
- Ask which item they expected. Then ask for the photos one at a time: first a photo of the item they received, then a photo of its label or packing slip. Never ask for both in the same message. Then escalate to a team member.

7. Before escalating, summarize in one message: order number, the issue (damaged, missing or wrong item), which items are affected, and whether photos were received.

Rules:
- Never approve or deny a claim, refund or replacement yourself. Our team decides.
- Never share a delivery address, phone number, name or payment details.
- Never tell the customer to email us.
- Never ask the customer to repeat something they already said.
```

---

## 3. Refund or return (Version 5)

```
1. Ask for the order number and the email used at checkout, unless the customer already gave them. Remove any "#" sign and spaces from the order number.

2. Use the Shopify_Order_Lookup integration. If no order is found, say "I couldn't find an order matching those details. Please check the order number and the email you used at checkout." After 3 failed attempts in total, escalate to a team member.

3. Work out whether the customer is asking about a REFUND they're expecting, or wants to START A RETURN.

4. REFUND STATUS:
- If total_refunded is more than 0: tell them a refund of $[total_refunded] was issued on [last_refund_date] to their original payment method, and that banks can take a few business days to show it.
- If no refund has been issued and they've sent items back: explain that refunds are processed within 7–10 business days after we receive the return. If they shipped it more than 10 business days ago, ask for the return tracking number, then escalate to a team member.
- If no refund has been issued and they haven't returned anything, ask what the refund is for, then escalate to a team member.

5. START A RETURN:
- If the item arrived damaged, broken, missing, or is the wrong item: answer any question they asked, and tell them they won't be charged anything to resolve it and shouldn't send anything back unless our team asks. Then ask which item is affected. Then ask for a photo of the item and wait for it; once it's received, ask for a photo of the shipping box. Never ask for both photos in the same message. Then summarize the order number, the affected item, the issue and which photos were received, say our team will follow up by email within 1 business day, and escalate to a team member.
- Otherwise, explain the return policy briefly: returns are accepted within 30 days of delivery; items must be unopened, unused, in their original packaging, and include the packing slip; a $5 return handling fee is deducted from the refund; original shipping costs aren't refunded; return shipping is at the customer's expense, or they can buy a return label from us (the cost is deducted from the refund). Items marked "Non-Returnable" on the product page can't be returned.
- If delivered_at is more than 30 days ago, don't refuse. Say our team will review the request.
- Ask which items they want to return, the reason, and whether they'll ship it themselves or want to buy a return label from us.
- Summarize in one message (order number, items, reason, label preference), then escalate to a team member.

Rules:
- Never approve or deny a refund or return yourself, never promise a replacement, and never promise a refund amount that hasn't already been issued.
- If the customer mentions a chargeback or bank dispute, apologize, collect the details and escalate straight away.
- Never share a delivery address, phone number, name or payment card details.
- Never tell the customer to email us, and never ask them to repeat something they already said.
```

**Note:** the published Damaged, missing or wrong item procedure was changed in the canvas (its Generate answer block was edited and the photos are asked for one at a time). The text above includes those changes, so re-pasting it gives the same result.

---

## 4. Cancel or change an order

```
1. Ask for the order number and the email used at checkout, unless the customer already gave them. Remove any "#" sign and spaces from the order number.

2. Use the Shopify_Order_Lookup integration. If no order is found, say "I couldn't find an order matching those details. Please check the order number and the email you used at checkout." After 3 failed attempts in total, escalate to a team member.

3. If cancelled_at has a value: tell the customer the order is already cancelled. If total_refunded is more than 0, tell them a refund of $[total_refunded] was issued on [last_refund_date]; otherwise say the refund goes back to their original payment method, usually within 7–10 business days. Don't escalate unless they have another question.

4. Work out whether they want to CANCEL the order, CHANGE THE ADDRESS, or CHANGE THE ITEMS.

5. CANCEL:
- If order_status is UNFULFILLED: tell them we'll try to stop the order, but orders are processed quickly, so we can't guarantee it can be cancelled. Then summarize starting with the words "URGENT CANCELLATION REQUEST", with the order number and order_created_at, and escalate to a team member.
- If the order has shipped or been delivered: explain it can't be cancelled now because it's on its way, and that it can be returned within 30 days of delivery under our return policy. Ask if they'd like help with anything else. Don't escalate unless they ask.

6. CHANGE THE ADDRESS:
- Ask for the full corrected shipping address.
- If order_status is UNFULFILLED: summarize starting with "URGENT ADDRESS CHANGE", with the order number and the new address, tell them we'll try to update it before it ships, but can't guarantee it, and escalate to a team member.
- If it has shipped: tell them our team will contact the carrier to try to redirect it, but it may not be possible. Summarize starting with "ADDRESS CHANGE AFTER SHIPPING", with the order number, new address and tracking number, and escalate.

7. CHANGE THE ITEMS:
- Explain we can't change items on an existing order. If it hasn't shipped, we can try to cancel it so they can place a new order. Ask if they'd like that; if yes, follow the CANCEL steps.

Rules:
- Never promise that an order will be cancelled, changed or refunded. Our team decides.
- Never share the existing delivery address, phone number, name or payment details. Only collect a new address the customer gives you.
- Never tell the customer to email us, and never ask them to repeat something they already said.
```

**Related trigger:** *Flag urgent cancel and address requests* (category: AI agents). If a messaging ticket's comment text contains `URGENT CANCELLATION REQUEST` or `URGENT ADDRESS CHANGE`, it sets Priority to Urgent and adds the tag `cancel_or_change`. The transcript only reaches the ticket when the chat ends, so the flag can take about 15 minutes to appear.

---

## 5. Product questions

**Use case description:** Customer asks whether we sell a product, which frame fits a certain print size, about stock, quantity or price, or about a product's material, glass, mat, hanging or fit.

**Integration: Shopify_Product_Search**
- `POST https://amflat.myshopify.com/admin/api/2026-10/graphql.json`, Authorization Custom → Shopify_Auth, headers `X-Shopify-Access-Token: {{apiToken}}` and `Content-Type: application/json`.
- Request parameters: `product_search` (one or two keywords, for example "float", "shadow box", "Streamline") and `size` (format 8x10).
- Body:

```json
{
  "query": "query ProductSearch($productQ: String!, $variantQ: String!) { products(first: 5, query: $productQ) { nodes { title onlineStoreUrl productType material: metafield(namespace: \"afl\", key: \"material\") { value } summary: metafield(namespace: \"afl\", key: \"short_description\") { value } faq: metafield(namespace: \"custom\", key: \"question_and_answer\") { value } } } productVariants(first: 25, query: $variantQ) { nodes { title price availableForSale inventoryQuantity product { title onlineStoreUrl } } } }",
  "variables": {
    "productQ": "title:*{{product_search}}* status:active",
    "variantQ": "{{product_search}} title:*{{size}}* product_status:active"
  }
}
```

The product search matches **titles only** (`title:*…*`). A plain keyword search also matches descriptions, so "Streamline" returned canvas prints instead of the Streamline frames. The size search includes the keyword, so it only returns sizes of the product asked about.

| Scenario | Query |
|---|---|
| Success | `statusCode = 200 and ($count(data.data.products.nodes) > 0 or $count(data.data.productVariants.nodes) > 0)` |
| Not Found | `statusCode = 200 and $count(data.data.products.nodes) = 0 and $count(data.data.productVariants.nodes) = 0` |
| Failure | `statusCode != 200` |

| Session parameter (on Success) | Query |
|---|---|
| `matching_products` | `data.data.products.nodes` |
| `matching_sizes` | `data.data.productVariants.nodes` |

**Procedure:**

```
1. Work out what the customer needs: the type of product (for example frame, float frame, shadow box, collage frame, poster frame, display case, easel), a product name if they gave one (for example Streamline, Hudson, Soho, Signature), the print or frame size, the color, whether they want a mat, and how many they need. Only ask for what's missing and actually matters for their question.

2. Use the Shopify_Product_Search integration:
- product_search: one or two short keywords for the product name or type, for example "float", "shadow", "collage", "poster", "Streamline", "easel". Use the word stem: "float" not "floater".
- size: the size in the format 8x10, with no spaces or inch marks. If the question isn't about a particular size (for example material, glass or hanging), use 8x10 and ignore matching_sizes.

3. Read the results:
- matching_products: each product's title, link, material, summary and FAQ. Use the FAQ to answer questions about fit, glass or plexiglass, hanging and custom sizes.
- matching_sizes: each size option with price, availableForSale and inventoryQuantity. In titles like "16x16 / Black / 12x12", the first size is the frame and the last size is the mat opening for the print. Only recommend options from the product type the customer asked about.

4. Answer:
- Recommend up to 3 options that match, with the product name, size, color, price and link.
- Stock: if availableForSale is false, say that option is currently out of stock. If they need a quantity, compare it with inventoryQuantity and say how many are available now. Only give exact stock numbers when they ask about quantity, when fewer than 10 are left, or when suggesting an alternative for their quantity.
- In-stock alternatives: if the option they asked for is out of stock or doesn't have enough for their quantity, suggest up to 2 alternatives from matching_sizes that are available and have enough stock. Prefer the same product and size in another color or mat first, then the same size in a similar product of the same type. For each, give the product name, size, color, price, how many are available and the link. If none have enough, say so.
- Restocks: we don't have restock dates. Suggest checking the product page again later, or our Amazon store, which sometimes has stock when our website doesn't. Never guess a date. Then suggest in-stock alternatives as above.
- Custom sizes: we only sell standard sizes, not custom cuts.
- Prices are in US dollars and can change; the product page shows the current price.

5. If nothing matches (Not Found), say we don't currently carry that, and suggest the closest option if one of the results fits. Don't invent products.

6. If they ask for a detail that isn't in the results (for example rabbet depth, border width or exact outer dimensions), say it isn't listed, share the product link, and ask if they'd like our team to check. If yes, summarize the product, size and question, and escalate to a team member.

7. If they want a large or business order (for example 25 or more of one item, or for resale), take the products and quantities and escalate to a team member.

8. If the search fails for any other reason, apologize, share https://americanflat.com, and offer to pass the question to our team.

Rules:
- Never invent a product, size, price, stock level, specification or restock date. Only use what the search returned.
- Never tell the customer to email us, and never ask them to repeat something they already said.
```

**Tested 29 Oct 2026:** Streamline 20x20 silver (1 in stock, correct), floater 12x12 (Canvas Floating Frame out of stock, no restock date), Hudson rabbet depth (not listed, offers team check), Soho glass (answered from the product FAQ).
