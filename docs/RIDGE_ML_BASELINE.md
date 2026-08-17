# QR-09 interpretable ridge-regression baseline

QR-09 is the first supervised-learning baseline. It consumes only a fingerprinted QR-08 dataset
and evaluates a fixed ridge-regression specification fold by fold.

Feature means and standard deviations are learned from each training fold only. Ridge coefficients
are solved deterministically, predictions are clipped, and validation reports RMSE, a
training-mean RMSE control, information coefficient, and directional accuracy. The economic view
ranks predictions within each signal time and reports mean non-compounded forward-return events
after round-trip 1.0x, 1.5x, and 2.0x costs against an equal-weight event control.

Non-compounded events are intentional: QR-08 labels can overlap, so treating them as an equity
curve would fabricate deployable P&L. Hyperparameters are fixed rather than selected on validation.

```powershell
uv run pq research-ridge-model-check
```

QR-09 cannot inspect the final holdout, approve itself, route orders, or alter WP-14 evidence.
