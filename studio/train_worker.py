"""Runs in its own process: train RF-DETR on a dataset snapshot, then export ONNX.

Kept apart from the studio so torch is loaded only here, a crash cannot take the studio down, and
a cancel is a plain process stop. The studio reads progress from Lightning's metrics.csv.

Last line on stdout on success: `RESULT {"onnx": ..., "resolution": ..., "checkpoint": ...}`.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SIZES = {"nano": "RFDETRNano", "small": "RFDETRSmall", "medium": "RFDETRMedium", "large": "RFDETRLarge"}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--size", choices=sorted(SIZES), default="nano")
    ap.add_argument("--epochs", type=int, default=50)
    ap.add_argument("--batch-size", type=int, default=4)
    ap.add_argument("--grad-accum", type=int, default=4)
    ap.add_argument("--lr", type=float, default=1e-4)
    ap.add_argument("--device", default="auto")
    ap.add_argument("--patience", type=int, default=10)
    a = ap.parse_args(argv)

    import rfdetr  # noqa: PLC0415 - heavy, only in this process
    from rfdetr import RFDETR

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    model = getattr(rfdetr, SIZES[a.size])()
    kwargs = dict(dataset_dir=a.dataset, epochs=a.epochs, batch_size=a.batch_size, grad_accum_steps=a.grad_accum,
                  lr=a.lr, output_dir=str(out), early_stopping=True, early_stopping_patience=a.patience)
    if a.device != "auto":
        kwargs["device"] = a.device
    model.train(**kwargs)
    best = out / "checkpoint_best_total.pth"
    if not best.exists():
        print("No best checkpoint was written; training did not finish an evaluation.", file=sys.stderr)
        return 2
    trained = RFDETR.from_checkpoint(best, trust_checkpoint=True)
    onnx_path = trained.export(output_dir=str(out / "export"), format="onnx", verbose=False)
    resolution = int(getattr(trained.model_config, "resolution", 0) or 0)
    print("RESULT " + json.dumps({"onnx": str(onnx_path), "resolution": resolution, "checkpoint": str(best)}), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
