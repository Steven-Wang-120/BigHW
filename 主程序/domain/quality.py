"""Quality metadata presentation, independent of row classification and matching."""

from 主程序.domain.models import FLAG_TRUNCATED_INVOICE


def quality_summary(manifest, *, scoped=False):
    quality = manifest.get('quality')
    result = {'status': 'VERIFIED' if quality else 'UNAVAILABLE',
              'quality_version': quality['quality_version'] if quality else None,
              'provided': quality is not None}
    if not scoped:
        result['marked_row_count'] = quality['row_flag_counts'][FLAG_TRUNCATED_INVOICE] if quality else None
    return result


def unavailable_quality():
    return quality_summary({})


def row_quality(manifest, flags):
    """No batch counts: safe for a single authorized source/difference."""
    return {**quality_summary(manifest, scoped=True),
            'truncated_invoice_range': FLAG_TRUNCATED_INVOICE in flags if manifest.get('quality') else None}
