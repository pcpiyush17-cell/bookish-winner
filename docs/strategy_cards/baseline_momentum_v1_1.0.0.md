# Baseline Momentum V1 — Strategy Card

## Release status

Engineering validation only. Evidence grade E0 until research runs are reviewed. This strategy
is not approved for live trading and makes no profitability claim.

## Hypothesis and economic rationale

A simple long-only trend rule may capture persistent price movement in liquid equities while
avoiding the turnover of short-horizon trading. Its purpose is to validate the platform,
execution assumptions, accounting, and promotion process—not to establish an investable edge.

## Universe

The tracked configuration enables `NSE:INFY` as a placeholder from the approved NSE equity
namespace. Any expansion to 5–20 liquid large-cap equities or ETFs requires point-in-time
liquidity, spread, corporate-action, price, and data-completeness review.

## Data and features

- Decision data: completed daily or 15-minute OHLCV bars.
- Features: fast and slow simple moving averages, recent return volatility, average traded
  value, and an externally supplied broad-market regime flag.
- Timestamp semantics: the current completed bar may create a signal; execution is allowed no
  earlier than a later bar.
- Warm-up: the slow moving-average window.
- Missing values: no forward or backward filling; insufficient history produces no signal.
- Scaling: none and no whole-sample normalization.
- Live eligibility: not approved.

## Entry, sizing, and exit

Entry requires fast trend above slow trend, positive regime, volatility below its cap,
liquidity above its floor, no existing position, and configured expected edge above estimated
costs plus uncertainty. Sizing is a fixed configured quantity, long-only, without leverage,
and limited to one position.

Exit occurs on trend reversal, maximum holding period, or the configured stop loss. Signals
carry an expiry, invalidation text, reason codes, and the complete decision feature snapshot.

## Risk and holding period

Pre-trade risk approval remains external and mandatory before OMS submission. The strategy
cannot access a broker. The intended holding period is multi-bar and bounded by the configured
time stop. Kill-switch, exposure, cash, loss, frequency, and reconciliation gates remain the
responsibility of the WP-09 risk engine.

## Turnover and cost assumptions

The design prefers one multi-bar position. Research must use the versioned NSE delivery charge
model, conservative next-open execution, and base, 1.5×, and 2× execution-cost scenarios. DP
charges and small-account fixed costs must not be omitted.

## Training and validation

There is no trained model. Parameters are explicit, versioned rules. Required validation is
development, untouched validation, final holdout, walk-forward testing, alternate parameter
neighbourhoods, buy-and-hold comparison, random-entry comparison with matched holding period,
and no-cost versus full-cost comparison.

## Failure regimes

- Sideways markets causing repeated trend reversals.
- Gap moves beyond modeled execution prices.
- Abrupt volatility increases after signal creation.
- Negative market regime incorrectly classified as positive.
- Liquidity or spreads deteriorating relative to historical bars.
- Corporate actions or universe changes not represented point in time.
- Performance dominated by market beta or one symbol/period.

## Changelog

- `1.0.0`: Initial broker-independent engineering baseline for WP-12.
