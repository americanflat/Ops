# Working notes for Claude

## Pasting into tools (Zendesk, Shopify, etc.)

When Anthony needs to put something into a field such as an API integration **Body**, a header, a scenario query or a procedure, give the **complete, ready-to-paste content** in a code block, with every value filled in. Don't describe the structure ("add a JSON body with a query and variables"), and don't give a partial snippet to merge. He selects all, pastes, and saves.

Put **every value he has to enter in its own code block**, so each has a copy button: names, keys, descriptions, queries, header values and test values, not only bodies. Label each block with the field it goes in. Anthony has said this copy-per-field format is very helpful. Double-check each block before sending, so there's never a "correction" block after a wrong one.

For JSON bodies, keep any escaping the field needs (for example `\"` inside a one-line GraphQL query) exactly as it must be pasted.
