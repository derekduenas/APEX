# Measured feed publisher

The paper service opens no market-data connection. This publisher is the only component that does. It writes one
generation — a measured `APEX_DATA_V1` input and the exchange session it belongs to — and makes the pair visible
in a single step.

**This is ordinary capture, not a premarket stage.** APEX has no premarket intelligence component. Naming a
capture "premarket" would invent one, so the publisher's own document says what it is in `coverage`.

## The exchange session is fetched

`regular()` knows weekdays and clock hours only. That is how a study came to issue 13 requests on Labor Day and
report them as `QUOTE_STALE`, because the vocabulary could not say "the exchange was closed".

`exchange_session` reads the provider's calendar for the market date and validates it:

| Condition | Outcome |
|---|---|
| Date absent from the calendar | `EXCHANGE_CLOSED_OR_CALENDAR_MISSING_DATE:<date>` |
| Two rows for one date | `CALENDAR_AMBIGUOUS_FOR_DATE:<date>` |
| Missing or unparseable times | `CALENDAR_SESSION_TIMES_INVALID` |
| Close before open, or a session longer than 8h | `CALENDAR_SESSION_IMPLAUSIBLE` |
| Request failed | `CALENDAR_REQUEST_FAILED:<class>` and no capture is attempted |
| Early close | Carried through, `early_close: true` |

A holiday is an **absent row**, never a row to be filled in with 09:30–16:00. The raw response SHA-256, endpoint,
host and retrieval time are recorded beside the session.

## Credential-bearing requests

`http_worker` now names each reachable endpoint with **its own host and its own parameter set**, rather than
allowing a host. The calendar and clock live on the trading host, which also serves orders and positions; listing
endpoints individually means no order path is reachable with these credentials even by mistake. Redirects are
refused and reported as `REDIRECT_REFUSED` rather than followed to a host that was never allowlisted.

## The pair is the unit

Replacing two files atomically, one at a time, still leaves a window in which a reader sees a new input beside an
old session. Two mechanisms, because one is not enough:

- both documents carry the same `generation_id`, and `verify_generation` refuses a mismatched pair by name;
- the pair is swapped in behind a single directory symlink, so the window is narrow as well as detectable.

`publish_generation` refuses to write a pair that does not already match, so nothing becomes visible when the
inputs are wrong. A failed publication leaves the previous generation in place and no `.tmp` file behind.

## What the publisher refuses, and what it deliberately does not

| Condition | Where it is handled | Why |
|---|---|---|
| No observations | `CAPTURE_RETURNED_NO_MARKET_DATA` | Nothing to publish |
| No quote at all | `CAPTURE_CONTAINS_NO_QUOTE` | No decision could ever use that generation |
| Rows not `MEASURED_RECEIPT` | `PUBLISHER_REQUIRES_MEASURED_RECEIPTS:<bases>` | Names a misconfiguration where it happened |
| Incomplete history | `CAPTURE_HISTORY_INCOMPLETE` | Silently shortens the forecast's training window |
| **Stale quote** | **Published, age recorded** | `quote_at` refuses it by name at the decision. Refusing to publish would replace a recorded refusal with silence. |

`capture_alpaca` labels anything fetched through an injected transport `SYNTHETIC_CLOCK`, so **no test can
manufacture a measured feed**. That is why acquisition and file mechanics are separate functions: the mechanics
are exercised with documents supplied directly, and the measured path is only ever demonstrated against a real
host.

## Nanoseconds are kept, never reconstructed

An earlier attempt to recover provider nanoseconds from float seconds was replaced twice and was wrong both
times. The second version added an ulp and rounded up; measurement over 200,000 stamps found it still landed
*earlier than the truth* about once in a hundred, by up to 112ns.

The reason no bound was available: a float produced as `n / 1e9` is **not** the nearest float to `n/10**9`,
because `n` exceeds 2**53 and the int-to-float conversion rounds before the division happens. Any bound therefore
depends on how the float was produced, which the reader is never told. Provenance is the missing information, not
precision.

So there is no reconstruction. `exact_ns` accepts integer seconds, which carry no lost digits, and otherwise
refuses with `FRACTIONAL_SECONDS_REQUIRE_NANOSECOND_STAMPS:<field>`. Adapters keep the nanoseconds they were
given; capture already emits `event_ns`, `available_ns`, `raw_response_sha256`, `provider_timestamp` and `feed`.

## Not established

No orders, fills or P&L. No activation. A published generation means the data is admissible to the paper
service's gate — not that the account, its recovery behaviour or its arithmetic have been commissioned on a host.
