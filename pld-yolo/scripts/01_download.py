"""Download the original PLDM (mountain) split from the authors' Google Drive.

Source: https://github.com/SnorkerHeng/PLD-UAV
Folder: https://drive.google.com/drive/folders/1bKFEuXKHRsy0tnOnoEVW6oRi7hS5oekr

This is the *unaugmented* release: 237 train + 50 test source images at
540x360 with binary mask PNGs as ground truth. Tens of MB total.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DST = ROOT / "data" / "raw"

PLDM_FOLDER_ID = "1bKFEuXKHRsy0tnOnoEVW6oRi7hS5oekr"


def main() -> int:
    DST.mkdir(parents=True, exist_ok=True)

    target = DST / "PLDM"
    if target.exists() and any(target.iterdir()):
        print(f"[skip] {target} already exists and is non-empty.")
        return 0

    try:
        import gdown
    except ImportError:
        print("gdown is not installed. Run `pipenv install`.", file=sys.stderr)
        return 1

    print(f"Downloading PLDM from Google Drive into {target} ...")
    target.mkdir(parents=True, exist_ok=True)
    gdown.download_folder(
        id=PLDM_FOLDER_ID,
        output=str(target),
        quiet=False,
        use_cookies=False,
    )
    print("Done.")
    print("\nExtract any zip files inside data/raw/PLDM if needed, then run:")
    print("    pipenv run python scripts/02_convert_to_yolo.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
