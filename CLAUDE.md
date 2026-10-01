# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A Flask chatbot web application that authenticates users via **Azure Entra ID SSO** and routes LLM requests through a **Portkey Hybrid AI Gateway**. The Entra-issued ID token is forwarded directly to Portkey as the API key — no separate Portkey API key is needed.

## Running the App

```bash
cp sample.env .env   # fill in values
pip install -r requirements.txt
python app.py        # → http://localhost:8000
```

## Architecture

### Authentication Flow

1. User logs in via Entra ID (MSAL auth code flow, scope: `User.Read`)
2. MSAL returns an ID token — stored in Flask session as `session["id_token"]`
3. ID token is forwarded on every LLM request as `x-portkey-api-key: <JWT>`
4. Portkey gateway validates the JWT locally (Mode B — standard IdP token, `JWT_ENABLED=ON`)
5. Workspace is pinned via `x-portkey-workspace` header (required — without it the gateway falls back to the org default workspace)

### MSAL Scope Note

Use `["User.Read"]` — **never** pass `["openid", "profile", "email"]` explicitly. MSAL reserves those and throws `ValueError` if you include them. The ID token is returned automatically alongside the `User.Read` access token.

### Portkey Metadata

The app sends the following with every request (sourced from `id_token_claims`):

```python
{
  "_user": preferred_username or email or sub,
  "email_id": preferred_username or email or sub,
  "name": name claim,
  "roles": comma-separated roles,
  "ip_address": ipaddr claim (if present),
}
```

**Known limitation (Mode B):** Portkey ignores client-supplied `_user` metadata for JWT-authenticated requests to prevent spoofing — it always resolves `_user` from the JWT `sub` claim. The custom fields (`email_id`, `name`, etc.) still appear in Portkey Observe logs as searchable metadata.

### Portkey Workspace Routing

`x-portkey-workspace` header is only honoured for user-attributed tokens **with** an `email_id` claim matching an active workspace member. Without `email_id` in the token, the header is ignored and the gateway uses the org default workspace. Current workaround: set `PORTKEY_WORKSPACE` in `.env` to the target workspace slug — the header is sent regardless and works in practice once the workspace is properly configured.

### Available Models (all via groq provider)

- `openai/gpt-oss-120b`
- `openai/gpt-oss-20b`
- `qwen/qwen3.8-27b`

## Environment Variables

```
# Azure Entra ID
AZURE_CLIENT_ID=
AZURE_CLIENT_SECRET=
AZURE_TENANT_ID=
AZURE_REDIRECT_URI=http://localhost:8000/auth/callback

# Portkey Gateway
PORTKEY_GATEWAY_URL=http://localhost/v1
PORTKEY_PROVIDER=@<virtual-key-slug-in-target-workspace>
PORTKEY_WORKSPACE=<workspace-slug>
FLASK_SECRET_KEY=<long-random-string>
```

`PORTKEY_PROVIDER` must match the **virtual key slug** in the target workspace (not just the display name). Adding optional Entra claims (e.g. `ctry`, `upn`) can break JWT validation at the gateway — add them one at a time and test.

## Portkey Gateway (Kubernetes / Helm)

Gateway env vars required in `values.yaml`:

```yaml
JWT_ENABLED: "ON"
ORGANISATIONS_TO_SYNC: "<org-uuid>"
PORTKEY_CLIENT_AUTH: "<service-token>"
JWT_LOCAL_AUTH_DEFAULT_SCOPES: "completions.write"
```

JWKS JSON for Portkey Admin (Organisation → Authentication) is fetched from:
`https://login.microsoftonline.com/<tenant-id>/discovery/v2.0/keys`

Microsoft rotates keys periodically — update the JWKS JSON in Portkey when keys change.
