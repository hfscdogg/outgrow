#!/usr/bin/env python3
"""Credential smoke test — read-only API probes for Phase 1 pre-flight."""

from __future__ import annotations

import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request


def probe_anthropic() -> bool:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("[SKIP] anthropic (cred missing)")
        return True  # SKIP doesn't fail
    import anthropic  # noqa: PLC0415  # optional dep — only needed when probe runs

    try:
        client = anthropic.Anthropic(api_key=api_key)
        client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1,
            messages=[{"role": "user", "content": "hi"}],
        )
        print("[OK] anthropic 200")
        return True
    except anthropic.APIStatusError as e:
        print(f"[FAIL] anthropic {e.status_code} {str(e)[:120]}")
        return False
    except Exception as e:
        print(f"[FAIL] anthropic ERROR {str(e)[:120]}")
        return False


def probe_zoho_crm() -> bool:
    refresh_token = os.environ.get("ZOHO_CRM_REFRESH_TOKEN", "")
    client_id = os.environ.get("ZOHO_CRM_CLIENT_ID", "")
    client_secret = os.environ.get("ZOHO_CRM_CLIENT_SECRET", "")
    dc = os.environ.get("ZOHO_CRM_DC") or "com"  # default to .com (US)

    if not all([refresh_token, client_id, client_secret]):
        print("[SKIP] zoho_crm (cred missing)")
        return True

    try:
        # Step 1: refresh access token
        token_url = f"https://accounts.zoho.{dc}/oauth/v2/token"
        params = urllib.parse.urlencode(
            {
                "refresh_token": refresh_token,
                "client_id": client_id,
                "client_secret": client_secret,
                "grant_type": "refresh_token",
            }
        ).encode()
        req = urllib.request.Request(token_url, data=params, method="POST")
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read())

        access_token = body.get("access_token")
        if not access_token:
            print(f"[FAIL] zoho_crm token_refresh failed: {str(body)[:120]}")
            return False

        # Step 2: read-only contacts probe
        contacts_url = f"https://www.zohoapis.{dc}/crm/v3/Contacts?per_page=1&fields=Email"
        req2 = urllib.request.Request(
            contacts_url,
            headers={"Authorization": f"Zoho-oauthtoken {access_token}"},
        )
        with urllib.request.urlopen(req2, timeout=15) as resp2:
            status = resp2.status

        print(f"[OK] zoho_crm {status}")
        return True
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:120]
        print(f"[FAIL] zoho_crm {e.code} {body}")
        return False
    except Exception as e:
        print(f"[FAIL] zoho_crm ERROR {str(e)[:120]}")
        return False


def probe_qbo_sandbox() -> bool:
    client_id = os.environ.get("QBO_CLIENT_ID", "")
    client_secret = os.environ.get("QBO_CLIENT_SECRET", "")
    refresh_token = os.environ.get("QBO_SANDBOX_REFRESH_TOKEN", "")
    realm_id = os.environ.get("QBO_SANDBOX_REALM_ID", "")

    if not all([client_id, client_secret, refresh_token, realm_id]):
        print("[SKIP] qbo_sandbox (cred missing)")
        return True

    try:
        # Step 1: refresh access token
        creds = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        token_url = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
        params = urllib.parse.urlencode(
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            }
        ).encode()
        req = urllib.request.Request(
            token_url,
            data=params,
            method="POST",
            headers={
                "Authorization": f"Basic {creds}",
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read())

        access_token = body.get("access_token")
        if not access_token:
            print(f"[FAIL] qbo_sandbox token_refresh failed: {str(body)[:120]}")
            return False

        # Step 2: read-only company info probe (sandbox base URL)
        info_url = (
            f"https://sandbox-quickbooks.api.intuit.com/v3/company/{realm_id}"
            f"/companyinfo/{realm_id}?minorversion=65"
        )
        req2 = urllib.request.Request(
            info_url,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(req2, timeout=15) as resp2:
            status = resp2.status

        print(f"[OK] qbo_sandbox {status}")
        return True
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:120]
        print(f"[FAIL] qbo_sandbox {e.code} {body}")
        return False
    except Exception as e:
        print(f"[FAIL] qbo_sandbox ERROR {str(e)[:120]}")
        return False


def main() -> None:
    results = [
        probe_anthropic(),
        probe_zoho_crm(),
        probe_qbo_sandbox(),
    ]
    if not all(results):
        sys.exit(1)


if __name__ == "__main__":
    main()
