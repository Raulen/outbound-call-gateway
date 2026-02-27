# Twilio Setup for Outbound Calls

This document covers the exact Twilio configuration required to make outbound SIP calls work through LiveKit.

---

## 1. Use an Elastic SIP Trunk — not a SIP Domain

This is the most critical distinction.

| Resource | Endpoint format | Purpose |
|----------|----------------|---------|
| **Elastic SIP Trunk** ✅ | `<name>.pstn.twilio.com` | Routes calls **to the PSTN** (outbound) |
| SIP Domain ❌ | `<name>.sip.twilio.com` | Receives calls and triggers a webhook (inbound TwiML) |

If you point LiveKit at a SIP Domain, Twilio will play a demo message ("Configure your number's voice URL...") and hang up. The call never reaches the destination phone.

---

## 2. Elastic SIP Trunk — Termination

The **Termination SIP URI** is the address LiveKit dials to send calls to Twilio:

```
<trunk-name>.pstn.twilio.com
```

Region-specific endpoints are also available (lower latency). For Brazil, use:

```
<trunk-name>.pstn.sao-paulo.twilio.com
```

All available region URIs are listed under:
**Elastic SIP Trunking → Networking info → Localized SIP Domain URIs**

### Authentication — use Credential Lists, not IP ACL

LiveKit Cloud SIP gateways use **dynamic IPs** (load-balanced, may change per call). IP ACL will break intermittently.

**Use Credential Lists instead:**

1. Go to **Elastic SIP Trunking → Credential lists → Create new**
   - Username: must match `authUsername` in the LiveKit SIP Trunk
   - Password: must match `authPassword` in the LiveKit SIP Trunk
2. Go to the trunk → **Termination → Credential Lists** → attach the list you just created
3. Remove any IP ACL entries from the trunk

---

## 3. Numbers

The outbound caller ID (`SIP_FROM_NUMBER`) must be a Twilio number associated with the trunk:

1. Buy or port a number in **Twilio Console → Phone Numbers**
2. Go to the trunk → **Numbers → Add a number** → select it

The number must have **Voice** capability. FAX-only numbers will not work.

---

## 4. Geographic Permissions

By default, Twilio blocks international calls. Enable the destination countries explicitly:

**Twilio Console → Voice → Settings → Voice Geographic Permissions**

Enable at minimum:
- **Brazil - Mobile** and **Brazil - Fixed** for `+55` numbers
- **Chile - Mobile** and **Chile - Fixed** for `+56` numbers

Without this, Twilio returns `403 Forbidden` even with a correctly configured trunk.

---

## 5. LiveKit SIP Trunk configuration

In **LiveKit Cloud → SIP → Outbound Trunks**, the trunk must be configured with:

| Field | Value |
|-------|-------|
| Address | `<trunk-name>.pstn.twilio.com` |
| Auth Username | the username from the Twilio Credential List |
| Auth Password | the password from the Twilio Credential List |

Then set in `.env`:

```env
SIP_TRUNK_ID_BR=<LiveKit trunk ID, e.g. ST_xxx>
SIP_FROM_NUMBER_BR=<E.164 Twilio number, e.g. +551150395793>
```

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| Twilio plays a demo message ("Configure your number's voice URL...") | LiveKit trunk points to a SIP Domain (`*.sip.twilio.com`) | Change address to `*.pstn.twilio.com` |
| `TwirpError: 403 Forbidden` | IP ACL active but LiveKit IP not listed, or geo permissions disabled | Switch to Credential List auth; enable geo permissions |
| `TwirpError: 404 not_found` | SIP Trunk ID belongs to a different LiveKit project | Check `SIP_TRUNK_ID_BR` matches the trunk in the current LiveKit project |
| Phone never rings but no error | `wait_until_answered=True` timed out silently | Check Twilio call logs in **Monitor → Calls** for the INVITE status |
