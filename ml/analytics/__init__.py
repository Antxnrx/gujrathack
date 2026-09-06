"""ml.analytics — track-level aggregation and rule-based event detection.

The inference layer (``ml/pipeline.py``) is deliberately frame-level: one row in
``detections`` per box per processed frame. Everything an operator actually
cares about is *track-level* — "this vehicle loitered for 4 minutes", "this
plate is GJ01AB1234 across 11 reads", "these two vehicles collided".

This package is the bridge. It reads ``detections``, folds them into ``tracks``,
and applies rules that emit ``events``. It holds no models and imports no CV
stack, so it runs anywhere the database is reachable — including inside the API
process.
"""

from ml.analytics.anpr import consensus_plate, plate_similarity
from ml.analytics.geometry import bbox_iou, heading_deg, point_in_polygon

__all__ = [
    "consensus_plate",
    "plate_similarity",
    "point_in_polygon",
    "heading_deg",
    "bbox_iou",
]
