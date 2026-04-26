"""Local YOLO inference for the Phase A demo UI.

This package mirrors the cloud Phase D.6 worker (``worker/yolo_detect``) but
runs against the locally cached frame thumbnails instead of S3, and writes
detection JSONs into ``data/yolo/<digest>.json`` so the local FastAPI server
in ``scripts.embed.serve`` can overlay polygons on each search result.
"""
