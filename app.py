import os
import uuid
import json
import logging
import msal
import requests
from functools import wraps
from flask import (
    Flask, render_template, session, redirect,
    url_for, request, Response, stream_with_context, jsonify
)
from dotenv import load_dotenv

logging.basicConfig(level=logging.DEBUG)

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", os.urandom(24))

AZURE_CLIENT_ID = os.environ.get("AZURE_CLIENT_ID")
AZURE_CLIENT_SECRET = os.environ.get("AZURE_CLIENT_SECRET")
AZURE_TENANT_ID = os.environ.get("AZURE_TENANT_ID")
AZURE_REDIRECT_URI = os.environ.get("AZURE_REDIRECT_URI", "http://localhost:8000/auth/callback")
PORTKEY_GATEWAY_URL = os.environ.get("PORTKEY_GATEWAY_URL", "https://aigw.portkey.ai/v1").rstrip("/")
PORTKEY_PROVIDER = os.environ.get("PORTKEY_PROVIDER", "")

AVAILABLE_MODELS = [
    {"id": "openai/gpt-oss-120b", "name": "GPT OSS 120B"},
    {"id": "openai/gpt-oss-20b", "name": "GPT OSS 20B"},
    {"id": "qwen/qwen3.8-27b", "name": "Qwen 3.8 27B"},
]

ERROR_MESSAGES = {
    400: "Bad request — check the message format.",
    401: "Authentication failed — your session may have expired. Please log in again.",
    403: "Access denied — your account lacks permission for this model or workspace.",
    412: "Usage limit exceeded — your token quota has been reached.",
    429: "Rate limit exceeded — too many requests. Please wait a moment.",
    500: "The AI gateway encountered an internal error.",
    502: "The AI gateway is unreachable (bad gateway).",
    503: "The AI gateway is currently unavailable.",
    504: "The request timed out waiting for the AI gateway.",
}


def _msal_app():
    return msal.ConfidentialClientApplication(
        client_id=AZURE_CLIENT_ID,
        client_credential=AZURE_CLIENT_SECRET,
        authority=f"https://login.microsoftonline.com/{AZURE_TENANT_ID}",
    )


def _format_error(status: int, body: dict) -> str:
    base = ERROR_MESSAGES.get(status, f"Unexpected error (HTTP {status}).")
    detail = ""
    if isinstance(body, dict):
        detail = (
            body.get("error", {}).get("message", "")
            if isinstance(body.get("error"), dict)
            else str(body.get("error", ""))
        )
    return f"{base}{(' — ' + detail) if detail else ''}"


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "id_token" not in session:
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


# ── Auth routes ───────────────────────────────────────────────────────────────

@app.route("/login")
def login():
    state = str(uuid.uuid4())
    session["oauth_state"] = state
    auth_url = _msal_app().get_authorization_request_url(
        scopes=["User.Read"],
        redirect_uri=AZURE_REDIRECT_URI,
        state=state,
    )
    return redirect(auth_url)


@app.route("/auth/callback")
def auth_callback():
    if request.args.get("state") != session.pop("oauth_state", None):
        return render_template("error.html", message="State mismatch — possible CSRF."), 400

    error = request.args.get("error")
    if error:
        return render_template(
            "error.html",
            message=request.args.get("error_description", error),
        ), 401

    result = _msal_app().acquire_token_by_authorization_code(
        code=request.args["code"],
        scopes=["User.Read"],
        redirect_uri=AZURE_REDIRECT_URI,
    )

    if "error" in result:
        return render_template(
            "error.html",
            message=result.get("error_description", result["error"]),
        ), 401

    session["id_token"] = result["id_token"]
    session["user"] = result.get("id_token_claims", {})
    return redirect(url_for("index"))


@app.route("/logout")
def logout():
    session.clear()
    post_logout = url_for("login", _external=True)
    return redirect(
        f"https://login.microsoftonline.com/{AZURE_TENANT_ID}/oauth2/v2.0/logout"
        f"?post_logout_redirect_uri={post_logout}"
    )


# ── App routes ────────────────────────────────────────────────────────────────

@app.route("/api/debug/claims")
@login_required
def debug_claims():
    """Show all Entra ID claims received in the current session token."""
    user_claims = session.get("user", {})
    token = session.get("id_token", "")
    # Decode payload without verifying signature (already validated by MSAL)
    import base64
    parts = token.split(".")
    token_preview = {}
    if len(parts) == 3:
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        try:
            token_preview = json.loads(base64.b64decode(padded))
        except Exception as e:
            token_preview = {"decode_error": str(e)}
    return jsonify({
        "session_claims": user_claims,
        "token_payload": token_preview,
        "gateway_url": PORTKEY_GATEWAY_URL,
        "provider": PORTKEY_PROVIDER,
        "workspace_from_jwt": user_claims.get("portkey_workspace", ""),
    })


@app.route("/api/debug/probe")
@login_required
def debug_probe():
    """Send a minimal non-streaming request to the gateway and return full response detail."""
    token = session["id_token"]
    user_claims = session.get("user", {})
    user_email = (
        user_claims.get("email")
        or user_claims.get("preferred_username")
        or user_claims.get("sub", "")
    )
    workspace = user_claims.get("portkey_workspace", "")

    headers = {
        "Content-Type": "application/json",
        "x-portkey-api-key": token,
        "x-portkey-metadata": json.dumps({"_user": user_email, "email_id": user_email}),
    }
    if PORTKEY_PROVIDER:
        headers["x-portkey-provider"] = PORTKEY_PROVIDER
    if workspace:
        headers["x-portkey-workspace"] = workspace

    payload = {
        "model": AVAILABLE_MODELS[0]["id"],
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 5,
    }

    logging.debug("Probe headers (redacted token): %s", {
        k: (v[:40] + "...") if k == "x-portkey-api-key" else v
        for k, v in headers.items()
    })

    try:
        resp = requests.post(
            f"{PORTKEY_GATEWAY_URL}/chat/completions",
            headers=headers,
            json=payload,
            timeout=30,
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 503

    try:
        resp_body = resp.json()
    except Exception:
        resp_body = resp.text

    return jsonify({
        "status_code": resp.status_code,
        "response_headers": dict(resp.headers),
        "response_body": resp_body,
        "request_headers_sent": {
            k: (v[:40] + "...") if k == "x-portkey-api-key" else v
            for k, v in headers.items()
        },
    })


@app.route("/")
@login_required
def index():
    return render_template("index.html", user=session["user"], models=AVAILABLE_MODELS)



# ── API routes ────────────────────────────────────────────────────────────────

@app.route("/api/chat", methods=["POST"])
@login_required
def chat():
    body = request.get_json(silent=True) or {}
    messages = body.get("messages", [])
    model = body.get("model", AVAILABLE_MODELS[0]["id"])

    if not messages:
        return jsonify({"error": True, "status": 400, "message": "No messages provided."}), 400

    token = session["id_token"]
    user_claims = session.get("user", {})
    user_email = (
        user_claims.get("email")
        or user_claims.get("preferred_username")
        or user_claims.get("sub", "")
    )
    metadata = {k: v for k, v in {
        "_user": user_email,
        "email_id": user_email,
        "name": user_claims.get("name", ""),
        "roles": ", ".join(user_claims.get("roles", [])),
        "ip_address": user_claims.get("ipaddr", ""),
    }.items() if v}

    workspace = user_claims.get("portkey_workspace", "")

    headers = {
        "Content-Type": "application/json",
        "x-portkey-api-key": token,
        "x-portkey-metadata": json.dumps(metadata),
    }
    if PORTKEY_PROVIDER:
        headers["x-portkey-provider"] = PORTKEY_PROVIDER
    if workspace:
        headers["x-portkey-workspace"] = workspace

    payload = {"model": model, "messages": messages, "stream": True}

    try:
        resp = requests.post(
            f"{PORTKEY_GATEWAY_URL}/chat/completions",
            headers=headers,
            json=payload,
            stream=True,
            timeout=90,
        )
    except requests.exceptions.ConnectionError:
        return jsonify({
            "error": True, "status": 503,
            "message": f"Cannot reach Portkey gateway at {PORTKEY_GATEWAY_URL}.",
        }), 503
    except requests.exceptions.Timeout:
        return jsonify({"error": True, "status": 504, "message": ERROR_MESSAGES[504]}), 504

    if not resp.ok:
        try:
            err_body = resp.json()
        except Exception:
            err_body = {"raw": resp.text}
        logging.error("Portkey error %s: %s", resp.status_code, err_body)
        msg = _format_error(resp.status_code, err_body)
        return jsonify({"error": True, "status": resp.status_code, "message": msg, "detail": err_body}), resp.status_code

    def generate():
        for raw in resp.iter_lines():
            if raw:
                yield raw.decode("utf-8") + "\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


if __name__ == "__main__":
    app.run(debug=True, port=8000)
