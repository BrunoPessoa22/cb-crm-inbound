# cb-crm-inbound — reply detection for the CB B2B pipeline

Receives replies to Cultura Builder outreach at
`vendas@reply.culturabuilder.com`, classifies them (real reply,
out-of-office, opt-out, bounce) and updates the CRM spine on the CB Neon
database (`crm_*` tables from the `cb-crm` repo). Real replies fire an
instant `[LEAD]` Telegram alert to Bruno and a copy is forwarded to
`bruno@culturabuilder.com`.

Production: Coolify app `cb-crm-inbound` on EC2 13.49.198.195, domain
`http://crm.culturabuilder.com` (Cloudflare-proxied).

## Why an SMTP listener (and not Resend inbound on the custom domain)

Verified Jul 12 2026:

- Resend inbound (`email.received` webhook) exists, but a custom
  receiving domain must be added to the Resend account first. The Aguia
  account is on the free plan (1 domain, `brunopessoa.com` already used).
  `POST /domains reply.culturabuilder.com` returns 403
  "Your plan includes 1 domain."
- Cloudflare Email Routing needs zone-level permissions; the
  culturabuilder.com token is Zone:DNS:Edit only. The
  `/zones/{id}/email/routing` API returns "Authentication error".

So the MX for `reply.culturabuilder.com` points straight at this app: an
aiosmtpd listener on the same event loop as FastAPI (host port 25 mapped
to container 2525). If processing fails the listener answers `451` so
the sending MTA retries — replies are not lost during downtime windows.

## Transports (all feed the same pipeline)

1. **SMTP listener** — live path today. Accepts mail for
   `*@reply.culturabuilder.com` only (550 otherwise).
2. **`POST /webhooks/resend-inbound`** — Resend `email.received`,
   svix-signature verified. Becomes the primary path if the Resend plan
   is upgraded and `reply.culturabuilder.com` is added as a receiving
   domain (then the MX flips from this box to Resend's).
3. **`POST /webhooks/resend-events`** — Resend outbound events
   (`email.bounced`, `email.complained`) for the Aguia account sends.
4. **IMAP poller** — dormant. Activates automatically when `IMAP_HOST`
   and `IMAP_PASSWORD` are set (e.g. a Google Workspace mailbox).

## Classification -> CRM actions

| Category | Detection | Actions |
| --- | --- | --- |
| reply | no auto/OOO/opt-out signals | contact `replied` (never downgrades `meeting_booked`), enrollments `stopped_reply`, `crm_events` `replied` (meta.full_text carries the complete message, capped 10240 chars, for the EC2 auto-reply responder), `crm_touches` in/reply, Telegram `[LEAD]`, forward copy to Bruno |
| optout | remover / descadastrar / nao quero / parar de / unsubscribe ... | `crm_suppressions`, contact `opted_out`, enrollments `stopped_optout`, `crm_events` `opt_out`. Nothing is sent back |
| ooo | Auto-Submitted/Precedence headers + PT-BR heuristics (ausente, ferias, fora do escritorio ...) | enrollments `paused` with `next_action_at = now() + 7 days` (the sequencer resumes them), `crm_touches` in/auto_reply_ooo. No alert |
| auto | machine headers without OOO text | logged only |
| bounce (webhook) | `email.bounced` | contact `bounced`, enrollments `stopped_bounce`, suppression on hard (Permanent) bounce |
| complaint (webhook) | `email.complained` | suppression reason `complaint`, contact `opted_out`, enrollments `stopped_optout` |

Dedupe: RFC Message-ID goes to `crm_touches.external_id` (unique).
Webhook retries and SMTP redeliveries are no-ops.

Unknown senders (address not in `crm_contacts`): real replies are still
forwarded to Bruno's inbox, but no CRM rows are written and no Telegram
alert fires (spam protection for the alert channel).

## Env vars (Coolify env store, never in git)

| Var | Use |
| --- | --- |
| `CRM_DATABASE_URL` | CB Neon pooled endpoint (same value as `/home/ubuntu/aguia/.cb-crm.env`) |
| `RESEND_API_KEY` | Aguia Resend account (forwarding + received-email fetch) |
| `RESEND_WEBHOOK_SECRET` | svix secret for the inbound webhook (empty = endpoint rejects all, fail closed) |
| `RESEND_EVENTS_WEBHOOK_SECRET` | svix secret for the events webhook |
| `TELEGRAM_BOT_TOKEN` | fleet bot (same token notify.sh reads) |
| `TELEGRAM_CHAT_ID` | default 5138814159 |
| `INBOUND_DOMAIN` | default `reply.culturabuilder.com` |
| `FORWARD_REPLIES_TO` | default `bruno@culturabuilder.com` |
| `FORWARD_FROM` | must stay on `@brunopessoa.com` (only Resend-verified domain) |
| `SMTP_LISTEN_PORT` | default 2525 (host 25 -> container 2525 via Coolify ports mapping) |
| `IMAP_HOST` / `IMAP_PORT` / `IMAP_USER` / `IMAP_PASSWORD` / `IMAP_FOLDER` / `IMAP_POLL_SECONDS` | IMAP fallback; poller starts only when host+password exist |

## DNS (culturabuilder.com zone, Cloudflare)

- `mx.reply.culturabuilder.com` A -> 13.49.198.195, **unproxied** (SMTP
  cannot go through the Cloudflare proxy)
- `reply.culturabuilder.com` MX 10 -> `mx.reply.culturabuilder.com`
- `reply.culturabuilder.com` TXT `"v=spf1 -all"` (domain never sends)
- `crm.culturabuilder.com` A -> 13.49.198.195, proxied (webhooks/health)

EC2 security group `sg-0758a990db064e009` has TCP 25 open (added Jul 12
2026 for this listener).

## Dev

```bash
python3.13 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/pytest            # classifier + signature tests, no network
```

Deploy: git push to `BrunoPessoa22/cb-crm-inbound` does NOT auto-deploy.
Trigger Coolify: `POST /api/v1/deploy?uuid=<app-uuid>&force=true`.
