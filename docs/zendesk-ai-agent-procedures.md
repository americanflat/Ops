# AI agent procedures: master copies

Zendesk AI agents → Content → Use cases. To change a procedure, edit the text here, then paste the **whole** procedure into the **Update procedure** box. Pasting only part of it changes the flow in unexpected ways. Afterwards, check the canvas, then **Save version** and **Publish procedure**.

Shared setup:
- Integrations: **Shopify_Auth**, which gets the token (session params `token` and `expiresIn`), and **Shopify_Order_Lookup**, which looks up the order by number and email.
- Session params from the lookup: order_name, order_status, shipment_status, carrier, tracking_number, tracking_url, estimated_delivery, delivered_at, package_count, all_package_statuses, payment_status, total_refunded, refund_count, last_refund_date.
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
