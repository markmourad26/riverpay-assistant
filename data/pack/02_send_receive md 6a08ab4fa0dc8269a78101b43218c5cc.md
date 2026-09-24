# 02_send_receive.md

**Document ID:** RP-KB-02

**Effective:** 2026-04-01

**Audience:** Customers

## Send to a RiverPay wallet

In the app: Payments → Send → enter the recipient’s RiverPay number or wallet ID → amount → confirm with PIN.

On USSD: `*123*1*` then follow prompts.

The recipient usually sees funds **within 30 seconds** when the ledger status is SUCCESS. SMS is not legal proof of success; the in-app history is.

## What you cannot do in this handbook

- Send to a bank account outside RiverPay (off-net / cross-border) is **not offered**.
- You cannot send if your wallet is `frozen` or `pin_locked`.
- You cannot cancel a transfer the ledger already marks SUCCESS. Reversal is not guaranteed.

## Receiving

Anyone with your RiverPay number can send to you. Incoming transfers do not require your PIN.

## Merchant pay

Scan the merchant QR or enter the merchant code. Fees, if any, show before PIN. This handbook does not list merchant MDR rates.

## Daily send limits (rolling 24 hours)

- KYC0: 50 KBR
- KYC1: 2,000 KBR
- KYC2: 20,000 KBR

Attempting to send above the limit fails with `LIMIT_EXCEEDED`. Do not invent a one-time exception.