# Private contacts and support inbox

The owner dashboard at `/dashboard/#contacts-inbox` lists saved report-email contacts and verified email accounts. A verified login is separate from confirmed marketing consent. Checkout counts mean Stripe sessions were created, not that a payment completed. Contacts include test accounts unless explicitly removed elsewhere.

All `/api/admin/` routes require the existing admin bearer token. They return `Cache-Control: no-store`. No customer addresses or message bodies are embedded in the static site. Message bodies are rendered as escaped plain text without remote images or executable HTML.

## Sending

The composer sends individual support messages to existing contacts through Resend. It requires `RESEND_API_KEY`, `RESEND_FROM`, and an actual monitored `RESEND_REPLY_TO`. It does not implement marketing campaigns. The owner reviews and confirms each message. A client request ID and provider idempotency key prevent a retry from sending twice. An uncertain provider result is kept as `unknown`; check the provider before sending another message. `accepted` is not a delivery receipt.

The founder-approved October 21 checkout support email was sent through the connected Gmail account. Its verified Sent-folder record is imported using the owner-only mailbox bridge. Do not send that message again through the dashboard.

## Gmail replies

`POST /api/admin/contacts/<id>/mailbox-import` saves messages read through the authorized Gmail connector. Imports are deduplicated by provider message ID and must match the saved contact. It accepts at most 100 messages per call. A sync timestamp is displayed, including when no new replies were found.

Gmail OAuth access in the assistant connector is not available to the website backend. Dashboard refresh reloads saved records and does not poll Gmail. The UI says this explicitly and provides a contact-specific Gmail link. Replies to the October 21 Gmail message continue to that Gmail conversation.

## Automatic Resend receiving

The existing Resend key has send-only permission. Do not use it to claim incoming mail works. Automatic receiving needs:

- `ADSCAN_INBOX_RECEIVING_KEY`, with provider permission to retrieve received emails.
- `ADSCAN_INBOX_REPLY_DOMAIN`, an actual receiving domain configured in Resend.
- A Resend webhook at `https://api.adscanvideo.com/api/email/inbound`, subscribed to `email.received`, `email.delivered`, `email.bounced`, `email.failed`, and `email.delivery_delayed`.
- `ADSCAN_INBOX_WEBHOOK_SECRET`, the signing secret for that endpoint.

The handler validates the Svix signature and a five-minute timestamp window before fetching the full incoming message from the provider. It matches an opaque per-contact reply address and the sender address, deduplicates provider IDs, and stores plain text only. The sender address is not proof of the sender's identity. It never follows email instructions, downloads attachments, or sends an automatic reply. Invalid signature requests are rejected. Failed body retrieval returns 503 so the provider can retry.

Do not change root-domain MX records or create broader credentials without the owner's explicit approval. Verify the receiving address with a controlled test before claiming automatic sync is live. Existing Gmail threads are not automatically migrated to a new receiving domain.

## Deployment and validation

Run `python -m unittest test_contact_inbox.ContactInboxTests` plus the existing service, native-video, report-email and email-auth suites. Stage and test the backend before restart; defer if analysis jobs are active. Back up source, environment and databases. Restore source/config only if rollback is needed; never overwrite new live customer records with an old database snapshot.

## October 21 receiving setup

The owner approved a new server-only full-access receiving key. The existing Resend login `timuran1@gmail.com` has a predefined `phsloli.resend.app` receiving domain. Incoming replies use that domain; the working AdScanVideo send-only key is retained. This receiving account lists the Connect Limo sending domain, so do not assume it owns `mail.adscanvideo.com`. The October 13 setup did not record the sending key owner. No root-domain DNS or existing sending-domain configuration is changed.

The dashboard polls saved contact/message history every 30 seconds while visible, preserving the support composer and history scroll position. Legacy Gmail conversations still need the Gmail connector import; changing the future Reply-To does not redirect replies to an email already sent from Gmail. Delivery events from a separate sending account are not implied by incoming integration.
