# 06_troubleshooting.md

**Document ID:** RP-KB-06

**Effective:** 2026-05-20

## First checks

1. Wait **15 minutes**. Many TIMEOUTs settle or reverse without a ticket.
2. Open in-app history. SUCCESS means the recipient should have funds. FAILED means **no transfer**.
3. SMS can be delayed. Do not use SMS alone.

## If history says FAILED

No funds should have left, or they should reverse. Fee reverses within 1 business day if one was taken. The customer can try again. The assistant must **not** press retry on their behalf.

## If history says PENDING after 15 minutes

Handoff to a human. Do not tell the customer it succeeded.

## If history says SUCCESS but the recipient denies it

1. Confirm the recipient number (do not ask for a PIN).
2. If the number is wrong, reversal is **not guaranteed**.
3. Dispute **within 48 hours** of the send.

## Duplicate send

Same recipient and amount within two minutes: do not keep retrying. Handoff if both are PENDING.

## USSD dropped mid-send

Treat like TIMEOUT: wait 15 minutes, then check history. Do not assume success.