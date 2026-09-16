# APEX Predator — three proposed edges beyond peer catch-up

These are new research proposals authored for this project on 2026-09-16. They
are my synthesis of market mechanisms and testable AI roles, not claims of
world-first invention or profitable performance. None is implemented, tested or
admitted for trading. The failed peer catch-up study remains a separate result.

My preferred direction is to model **how a market's response to pressure changes**.
Flow, information and deadlines supply different kinds of pressure. A possible
advantage is recognizing a change in the response before the remaining price
adjustment disappears into spreads, delay and impact. This is a hypothesis about
incremental information, not a claim that public pressure must predict returns.

## 1. Selling loses its effect

**Hypothesis:** After sustained selling, comparable new sell bursts sometimes
produce progressively less downward price movement while bids recover faster.
If selling then slows, this response change may identify a rebound more precisely
than oversold price indicators or order imbalance alone.

The proposed payer is urgency-driven selling that accepts a temporary concession.
We do not observe the seller's mandate or know its remaining order. Those are
uncertain explanatory possibilities, never fabricated participant identities.

The AI would estimate expected price response conditional on signed flow, depth,
spread, volatility and time of day, then detect a persistent change in observed
response. The first challenger is a small sequence model against a regularized
linear baseline. A larger world model is justified only by incremental results.
The signal requires evidence of both absorption and subsequent pressure decay;
continued heavy selling with little price movement alone does not imply profit.

| Contract item | Proposed first experiment |
|---|---|
| Inputs | Sequenced trades and bid/ask quotes; depth updates for any replenishment claim; source and receipt timestamps. |
| Decision | Observe pressure decay and recovery using only completed event windows; fixed long-versus-WAIT study. |
| Primary outcome | Five-minute executable long return after measured decision latency and costs. Fifteen-minute results are secondary and recorded as another test. |
| Strong baselines | Short-term reversal, order-flow imbalance, spread/depth, volatility and own-price features; identical selected samples. |
| Incremental feature | Change in price response and liquidity recovery conditional on comparable flow; no division by near-zero flow. |
| Kill test | No improvement after midpoint/quote controls; advantage disappears with realistic delay; or a simple imbalance/reversal model explains it. |

Order-flow imbalance and depth have a documented relationship with short-interval
price changes ([Cont, Kukanov and Stoikov](https://arxiv.org/abs/1011.6402)).
Persistent order flow also need not yield predictable returns: adaptive liquidity
can offset it ([Taranto, Bormetti and Lillo](https://arxiv.org/abs/1403.0842)).
Those results motivate measuring the response change; neither establishes this
five-minute strategy. Fast competition may consume the whole opportunity.

## 2. The second reading of a release

**Hypothesis:** A company announcement can contain a material change in forward
economics that is poorly represented by its headline and initial price move.
A model that reconciles the actual economic change with prior expectations may
identify some intraday adjustments that continue after the first reaction.

Illustration only: the headline reports weak earnings, while recurring margins,
guidance and cash conversion jointly improve relative to expectations. This is
not a rule that such a combination should be bought. The question is whether its
residual information predicts a future return after controlling for the initial
reaction, standard earnings surprises and trading costs.

The AI extracts source-linked claims into a fixed table: reporting period,
units, GAAP/non-GAAP basis, actual result, previous guidance, current guidance,
and point-in-time consensus where licensed. Deterministic checks enforce period
alignment and accounting consistency. The return model then tests whether this
information adds value. The language model does not invent a fair price or
replace a missing consensus estimate with a guess.

| Contract item | Proposed first experiment |
|---|---|
| Inputs | First-seen issuer release/filing versions, archived prior guidance and expectations, real quote/trade stream, full receipt timeline. |
| Decision | After the source is received, extraction is complete and all validation gates pass; never timestamped at the earlier publication time. |
| Primary outcome | Thirty-minute executable long-versus-WAIT result for favorable validated surprises, with a fixed intraday exit window. |
| Strong baselines | EPS/revenue/guidance surprise, simple sentiment, initial move, volatility and volume on identical events; the same model with manually validated extraction. |
| Incremental feature | Verified forward-economic changes missed by the simpler representation; uncertainty and contradictions remain explicit. |
| Kill test | Extraction is unreliable, the advantage is already in simple surprise/reaction features, or prospective results vanish after measured model latency. |

Research finds that identifying economically relevant news changes its observed
relationship with returns ([Boudoukh and coauthors](https://www.nber.org/papers/w18725)).
That evidence does not establish a thirty-minute AI edge. Historical language
models may already know later events; a masked historical replay alone cannot
remove that risk. The decisive evaluation must use prospectively captured new
announcements and frozen model/prompt versions.

## 3. Closing demand that is not being absorbed

**Hypothesis:** The evolution of closing-auction demand, paired volume and
continuous-market liquidity may reveal when a price adjustment remains before
the close. The added information is the response of available liquidity to
changing demand, beyond the latest imbalance number alone.

The proposed payer is a participant prioritizing closing execution. Individual
obligations are not visible. Published imbalances can change, attract offsetting
orders and already be reflected in price. We must predict what remains after
the decision rather than assume every imbalance creates directional profit.

The model estimates the next evolution of imbalance and indicative clearing
price from the observed message history, and compares this with the continuous
bid/ask. It must respect the fields actually disseminated in each feed phase.
The initial design studies funded longs under positive pressure and WAIT;
shorting, auction participation and a reversal variant are separate hypotheses.

| Contract item | Proposed first experiment |
|---|---|
| Inputs | Licensed, timestamped auction imbalance history, indicative prices, matched shares and contemporaneous quotes; applicable session calendar. |
| Decision | One fixed checkpoint five minutes before the scheduled regular close, after receipt of required messages. |
| Primary outcome | Continuous-market ask entry after latency and bid exit two minutes before that close; no assumed auction fill. |
| Strong baselines | Latest imbalance, indicative-price gap, time of day, short-term momentum and spread/depth on the same sessions. |
| Incremental feature | Persistence/acceleration of demand relative to new matching interest and liquidity response. |
| Kill test | No gain over the latest imbalance/gap, pressure changes before execution, costs absorb the effect, or simulated auction access is necessary. |

Nasdaq documents subscription-based imbalance dissemination before its crosses
([exchange documentation](https://m.nasdaqtrader.com/Trader.aspx?id=OpenClose)).
Availability of that data is not evidence of alpha. A historical adapter must
version the actual rules and field cadence for each date; broad product pages
are insufficient for implementing message-level logic.

## Where the frontier work belongs

The common learning task is a conditional response model: what follows when
observed pressure persists, fades or reverses? A proposed scenario engine would
evaluate each of those cases along with deteriorating liquidity and delayed
execution. Its scenario weights must be checked against subsequent observations.
Invented participant narratives and Monte Carlo counts do not calibrate them.

The slower AI director would allocate a bounded research budget across these
mechanisms, request missing observations, compare explanations, retire failed
variants and produce reproducible proposals. A future continuous numerical
scanner would maintain a timestamped opportunity queue across a declared
universe, including rejected and unavailable cases. The AI does not gain
arbitrary-code or capital authority by adding a proposal to this document.

I would prioritize the selling-response experiment first because its mechanism
can be measured directly without relying on language-model interpretations.
The announcement experiment provides a distinct role for semantic reasoning.
The auction experiment offers a clearly timed, separate source of pressure.
This ranking is engineering judgment, not an expected-return estimate.

## Shared research contract

These proposals are **designs, not completed preregistrations**. Exact instruments,
data entitlements, training/development/holdout dates, event thresholds and the
search budget still need to be frozen before evaluating any outcomes. The draft
machine-readable inventory is `research-proposals-002.json`; the current director
does not execute it. Its only supported study remains the v0.6 peer experiment.

For each future experiment:

1. Use a universe selected from information available before each session; retain
   all candidates and exclusion reasons. Do not select historic winners.
2. Freeze one primary horizon, comparison, policy and tuning budget. Overlapping
   events cannot silently be treated as independent bets or simultaneous funded
   positions. Count each later variant in a global research-attempt record.
3. Measure incremental information against the strong baseline and incremental
   executable economics at identical opportunities. Also compare scanner coverage
   with an otherwise identical baseline scanner to expose selection effects.
4. Evaluate uncertainty in blocks of sessions/events with the declared search
   process accounted for. A chosen sample count is not proof of adequate power.
5. Retain unsuccessful tests. Require new evidence after changes, then prospective
   paper evaluation with actual timing, account constraints and reconciliation.

The existing minute-bar capture cannot supply signed trades, order replenishment,
news-receipt histories or auction messages. It must not be used to simulate those
as if observed. None of the proposals presently supports a six- or seven-figure
account-growth claim. The goal is to discover repeatable, net economic advantage
and measure its capacity before assigning capital.
