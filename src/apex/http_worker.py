"""A killable read-only Alpaca transport worker. No caller-supplied URL or headers."""
import base64
import json
import sys
from .credentials import alpaca_credentials
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

PATHS = {"/v2/stocks/bars", "/v2/stocks/bars/latest", "/v2/stocks/quotes/latest"}
PARAMETERS = {"symbols", "feed", "timeframe", "start", "end", "limit", "sort", "adjustment", "page_token"}
MAX_BYTES = 2_000_000


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
    except (URLError, TimeoutError, OSError):
        result = {"status": 0, "error": "TRANSPORT_ERROR"}
    print(json.dumps(result))


if __name__ == "__main__":
    main()
