"""A killable read-only Alpaca transport worker. No caller-supplied URL or headers."""
import base64
import json
import socket
import ssl
import sys
from .credentials import alpaca_credentials
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

PATHS = {"/v2/stocks/bars", "/v2/stocks/bars/latest", "/v2/stocks/quotes", "/v2/stocks/quotes/latest",
         "/v2/stocks/trades"}
PARAMETERS = {"symbols", "feed", "timeframe", "start", "end", "limit", "sort", "adjustment", "page_token"}
MAX_BYTES = 2_000_000


TRANSPORT_ERRORS = ("TLS_CERTIFICATE_ERROR", "DNS_RESOLUTION_ERROR", "CONNECTION_REFUSED",
                    "TRANSPORT_TIMEOUT", "TRANSPORT_ERROR")


def transport_error_class(exc) -> str:
    """Map a transport failure to one of a fixed set of names, reading only exception TYPES and errno."""
    seen, depth = exc, 0
    while seen is not None and depth < 5:
        if isinstance(seen, ssl.SSLCertVerificationError):
            return "TLS_CERTIFICATE_ERROR"
        if isinstance(seen, socket.gaierror):
            return "DNS_RESOLUTION_ERROR"
        if isinstance(seen, (TimeoutError, socket.timeout)):
            return "TRANSPORT_TIMEOUT"
        if isinstance(seen, ConnectionRefusedError):
            return "CONNECTION_REFUSED"
        seen = getattr(seen, "reason", None) if not isinstance(getattr(seen, "reason", None), str) else None
        depth += 1
    return "TRANSPORT_ERROR"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def main():
    request = json.load(sys.stdin)
    if request.get("path") not in PATHS or set(request.get("params", {})) - PARAMETERS:
        raise ValueError("READ_ONLY_ENDPOINT_NOT_ALLOWED")
    key, secret = alpaca_credentials()
    if not key or not secret:
        print(json.dumps({"status": 0, "error": "BLOCKED_EXTERNAL_CREDENTIAL"}))
        return
    url = "https://data.alpaca.markets" + request["path"] + "?" + urlencode(request["params"])
    req = Request(url, headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret, "Accept": "application/json"}, method="GET")
    try:
        with build_opener(NoRedirect()).open(req, timeout=request["timeout"]) as response:
            body = response.read(MAX_BYTES + 1)
            if len(body) > MAX_BYTES:
                result = {"status": 0, "error": "RESPONSE_BYTE_BUDGET_EXCEEDED"}
            else:
                # Reflected credentials, if any, are not persisted as market data.
                if any(value.encode() in body for value in (key, secret)):
                    result = {"status": 0, "error": "RESPONSE_CONTAINS_CREDENTIAL"}
                else:
                    result = {"status": response.status, "body_base64": base64.b64encode(body).decode()}
    except HTTPError as exc:
        result = {"status": exc.code, "error": "HTTP_ERROR"}  # no headers or server error text
    except (URLError, TimeoutError, OSError) as exc:
        # A misconfigured local trust store used to be indistinguishable from an outage, which cost real
        # diagnosis time. The CLASS of failure is reported from a fixed vocabulary; the exception text is not,
        # because it can carry hostnames, paths and server strings. TLS verification is never weakened.
        result = {"status": 0, "error": transport_error_class(exc)}
    print(json.dumps(result))


if __name__ == "__main__":
    main()
