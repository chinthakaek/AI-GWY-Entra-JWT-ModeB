# Flask Chatbot — Azure Entra ID + Portkey JWT Auth

A Flask web app that authenticates users via **Azure Entra ID SSO** and routes LLM requests through the **Portkey AI Gateway**. The Entra-issued ID token is forwarded directly to Portkey as the API credential — no separate Portkey API key is needed.

Workspace routing is managed centrally in Entra: each user's Portkey workspace slug is issued as a **hardcoded custom claim** in their JWT, so the app never holds any workspace configuration itself.

---

## Architecture and Token Flow

```
1. User logs in via Entra ID (MSAL auth code flow, scope: User.Read)
         ↓
2. Entra issues a signed ID token containing custom claims:
         portkey_oid       = "your-org-uuid"          ← hardcoded in Entra
         portkey_workspace = "ws-jwt-wo-..."           ← hardcoded in Entra
         scopes            = "completions.write"       ← hardcoded in Entra
         sub, preferred_username, name, ...            ← standard claims
         ↓
3. Flask stores the raw JWT in the session (session["id_token"])
         ↓
4. On every LLM request, the app reads claims from the token and sends:
         x-portkey-api-key:   <raw JWT>
         x-portkey-provider:  @<virtual-key-slug>      ← from .env
         x-portkey-workspace: <portkey_workspace claim> ← read from JWT
         x-portkey-metadata:  { email_id, name, ... }  ← read from JWT claims
         ↓
5. Portkey validates the JWT signature against Microsoft's JWKS,
   extracts portkey_oid / portkey_workspace / scopes,
   and routes the request to the correct workspace and LLM provider
```

**Key point:** `portkey_workspace` is set once in Entra's claim configuration — the app reads it from the JWT at runtime and passes it straight to Portkey. There is no workspace configuration in the app or `.env`.

---

## Prerequisites

- Python 3.10+
- An Azure Entra ID tenant with an App Registration
- A Portkey account with JWT authentication configured
- OpenSSL (for generating the certificate Entra requires for custom claims)

---

## Part 1 — Azure Entra ID Setup

Entra must be configured to issue three custom hardcoded claims in the ID token. Because these are static (literal) values, Entra requires a custom signing certificate on the app registration before it will allow them.

### Step 1 — Generate a self-signed certificate

Run this in your terminal:

```bash
openssl req -x509 -newkey rsa:2048 -keyout key.pem -out cert.pem -days 730 -nodes \
  -subj "/CN=PortkeyTokenSigningKey"
```

Verify: `ls -l cert.pem` confirms `cert.pem` was created. Keep `key.pem` private — it never leaves your machine.

### Step 2 — Upload the certificate to your Entra App Registration

1. Go to **Microsoft Entra ID → App registrations → your app → Certificates & secrets**
2. Under the **Certificates** tab, click **Upload certificate**
3. Select `cert.pem` and click **Add**

Verify: `PortkeyTokenSigningKey` appears under the Certificates tab with valid start and expiration dates.

### Step 3 — Enable mapped claims in the app manifest

1. Go to **App registrations → your app → Manifest**
2. Find `"acceptMappedClaims": null` (or `false`)
3. Change it to `"acceptMappedClaims": true`
4. Click **Save**

Verify: Re-open the Manifest editor and confirm the value is saved. This is what allows custom claim mappings and resolves the `AADSTS50146` error.

### Step 4 — Configure hardcoded custom claims

This is where the workspace slug is baked into the token.

1. Go to **Microsoft Entra ID → Enterprise applications → your app**
2. Click **Single sign-on → Attributes & Claims → Edit**
3. Click **Add new claim** for each of the following:

| Claim name          | Source attribute (exact literal value in double quotes) |
|---------------------|---------------------------------------------------------|
| `portkey_oid`       | `"your-portkey-org-uuid"`                               |
| `portkey_workspace` | `"your-portkey-workspace-slug"`                         |
| `scopes`            | `"completions.write"`                                   |

4. Click **Save**

> **Important notes:**
> - Wrap all literal values in double quotes in the Azure UI (e.g. `"completions.write"`, not `completions.write`)
> - Use `scopes` not `scope` — `scope` is a reserved OIDC claim and cannot be remapped
> - `portkey_workspace` here is the workspace slug from Portkey Admin (e.g. `ws-jwt-wo-179272`), not the display name

### Step 5 — (Optional) Enforce v2.0 tokens

1. Go to **App registrations → your app → Manifest**
2. Find `"accessTokenAcceptedVersion"`
3. Set it to `2`
4. Click **Save**

Verify: Acquire a new token and decode it at [jwt.ms](https://jwt.ms) — `iss` should end with `/v2.0` and `portkey_oid`, `portkey_workspace`, and `scopes` should appear in the payload.

---

## Part 2 — Portkey Setup

1. In **Portkey Admin → Organisation → Authentication**, set the JWKS source:
   - **JWKS URL:** `https://login.microsoftonline.com/<your-tenant-id>/discovery/v2.0/keys`
   - Portkey fetches Microsoft's public keys from here to validate token signatures

2. Create a **virtual key** for your LLM provider in the target workspace and note its slug (e.g. `@groq`)

> Microsoft rotates signing keys periodically. If JWT validation starts failing unexpectedly, the JWKS URL configuration in Portkey Admin may need to be re-saved to trigger a key refresh.

---

## Environment Variables

Copy `sample.env` to `.env` and fill in the values:

```bash
cp sample.env .env
```

```env
# Azure Entra ID
AZURE_CLIENT_ID=your-app-client-id
AZURE_CLIENT_SECRET=your-app-client-secret
AZURE_TENANT_ID=your-tenant-id
AZURE_REDIRECT_URI=http://localhost:8000/auth/callback

# Portkey Gateway
PORTKEY_GATEWAY_URL=https://aigw.portkey.ai/v1
PORTKEY_PROVIDER=@your-virtual-key-slug

# Flask
FLASK_SECRET_KEY=a-long-random-string
```

`PORTKEY_WORKSPACE` is **not** configured here — the workspace slug comes from the `portkey_workspace` claim in the Entra JWT (set in Step 4 above).

---

## Running the App

```bash
pip install -r requirements.txt
python app.py
# → http://localhost:8000
```

---

## Available Models

All routed via the configured provider virtual key:

| Model ID              | Display name |
|-----------------------|--------------|
| `openai/gpt-oss-120b` | GPT OSS 120B |
| `openai/gpt-oss-20b`  | GPT OSS 20B  |
| `qwen/qwen3.8-27b`    | Qwen 3.8 27B |

---

## Debug Endpoints

Two endpoints are available when logged in, useful for verifying the setup:

| Endpoint                | What it shows                                                                    |
|-------------------------|----------------------------------------------------------------------------------|
| `/api/debug/claims`     | All Entra claims in the current session — verify `portkey_workspace` is present  |
| `/api/debug/probe`      | Sends a real request to the gateway and returns the full response detail         |

After completing the Entra setup, visit `/api/debug/claims` and confirm `portkey_workspace` appears in `token_payload` with the correct workspace slug. Then use `/api/debug/probe` to confirm the gateway accepts the token end-to-end.

---

## JWT Claims Reference

| Claim               | Required by Portkey | Set by          | Description                                          |
|---------------------|---------------------|-----------------|------------------------------------------------------|
| `portkey_oid`       | Yes                 | Entra (Step 4)  | Portkey organisation UUID                            |
| `portkey_workspace` | Yes                 | Entra (Step 4)  | Portkey workspace slug — drives `x-portkey-workspace`|
| `scopes`            | Yes                 | Entra (Step 4)  | Must include `completions.write` for chat requests   |
| `exp`               | Yes                 | Entra (auto)    | Token expiry — validated by Portkey on every request |
| `sub`               | Auto                | Entra (auto)    | Used by Portkey for user attribution in logs         |
| `preferred_username`| No                  | Entra (auto)    | Forwarded as `email_id` in Portkey request metadata  |
