"""Deterministic JSON encoding for manifests and persistence, without IO."""

import json


def json_text(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
