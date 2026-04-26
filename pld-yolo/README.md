# pld-yolo

Demo: train a **YOLO26-seg** instance-segmentation model to detect power lines
from UAV imagery, using a small slice of the **PLDM** (Power Line Dataset of
Mountain scenes) split of the PLD-UAV dataset.

> Heads up: PLDM is the *harder* of the two PLD-UAV splits. Lines are 1–3 px
> wide because the UAV is shooting from >30 m away, with leafy/grassy
> backgrounds. Mask quality will not be amazing — that is the point of the
> dataset. For an easier first demo, switch to PLDU.

## Attribution

The PLD-UAV dataset is released under **CC BY 4.0** by the original authors.
If you publish, deploy, or otherwise share this demo, you must credit them:

> Heng Zhang, Wen Yang, Huai Yu, Haijian Zhang, Gui-Song Xia.
> *PLD-UAV: Power Line Detection in UAV.* 2019.
> https://snorkerheng.github.io/PLD-UAV/
> https://github.com/SnorkerHeng/PLD-UAV
> Paper: https://www.mdpi.com/2072-4292/11/11/1342

We pull the original release directly from the authors' Google Drive (linked
from the GitHub repo above). For the urban split or augmented variants, see
the [Dataset Ninja](https://datasetninja.com/pld-uav) re-distribution.

## Setup

Managed with **pipenv**. The `Pipfile` pulls **CPU-only PyTorch** from the
official PyTorch index — swap to a CUDA index (e.g. `cu121`) if you have a
GPU.

```bash
pipenv install
pipenv shell        # or prefix everything below with `pipenv run`
```

GPU strongly recommended for training. CPU is feasible for this small dataset
but slow (expect ~minutes per epoch on a modern laptop CPU).

## Pipeline

```bash
# 1. Download PLDM (mountain) split from the authors' Google Drive (~tens of MB).
pipenv run python scripts/01_download.py

# 2. Convert binary mask PNGs to YOLO-seg polygons (one instance per
#    connected component). Honors any train/test layout in the download.
pipenv run python scripts/02_convert_to_yolo.py

# 3. Train yolo26s-seg.
pipenv run python scripts/03_train.py

# 4. Predict on a held-out image or directory.
pipenv run python scripts/04_predict.py --source data/yolo_pldm/images/val
```

### Switching to GPU PyTorch

In `Pipfile`, replace the `pytorch-cpu` source URL with the matching CUDA
build, e.g. `https://download.pytorch.org/whl/cu121`, then
`pipenv lock && pipenv sync`.

## When the model overfits

237 unique mountain scenes is small, so a YOLO26s easily overfits — train
loss keeps falling while val mAP plateaus or drifts down. Two knobs:

1. **Up the dataset** by including the rotation / flip / scale variants:

   ```bash
   pipenv run python scripts/02_convert_to_yolo.py --use-augmented
   ```

   That goes from 237 → ~11,376 train images. Val (the 50 originals) stays
   the same, so metrics remain comparable.

2. **Up the on-the-fly augmentation** with the `--heavy-aug` preset:

   ```bash
   pipenv run python scripts/03_train.py --heavy-aug --epochs 60 --imgsz 640
   ```

   Adds vertical flips, mixup, copy_paste, stronger HSV/scale jitter.

Use both together for the strongest demo run.

## Inference shortcut

`scripts/04_predict.py` auto-picks the most recently modified `best.pt` (or
falls back to `last.pt`) under `runs/`, so you can run inference even while
training is in progress:

```bash
pipenv run python scripts/04_predict.py --source path/to/uav.mp4
```

## Layout

```
configs/pldm.yaml          YOLO data config (paths, class names)
scripts/01_download.py     dataset-tools download
scripts/02_convert_to_yolo.py  Supervisely bitmap masks -> YOLO-seg polygons
scripts/03_train.py        Train yolo26s-seg
scripts/04_predict.py      Inference / visualization
data/                      (gitignored) raw + processed datasets
runs/                      (gitignored) ultralytics training output
```

## Notes on the conversion

- Original PLDM ships ground truth as a single binary PNG mask per image.
- We split each mask into 8-connected components, treat each component as one
  `power_line` instance, trace its contour with OpenCV, simplify with
  `approxPolyDP`, normalize to `[0, 1]`, and write one YOLO-seg polygon line.
- If the Google Drive folder includes a native `train` / `test` layout, we
  honor it (test → our val). Otherwise we split deterministically with
  `--val-frac` (default 0.15).

## Model

Default is `yolo26s-seg.pt` (~22 MB, 10.4 M params). Swap to `yolo26n-seg.pt`
for max speed or `yolo26m-seg.pt` for better masks. If `yolo26*-seg.pt` is not
yet available in your `ultralytics` install, fall back to `yolo11s-seg.pt` —
the train/predict scripts accept `--model` to override.
