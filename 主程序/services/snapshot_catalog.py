"""Server-owned snapshot allowlist; uploaded manifests never establish trust."""

from dataclasses import dataclass
from pathlib import Path

from 主程序.domain.quality import quality_summary
from 主程序.repositories.xlsx_reader import InputError
from 主程序.services.manifest_service import verify_snapshot
from 主程序.services.snapshot_service import SOURCE_SHA

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CSV_NAME = "retail_2010_rows_2_10001.csv"


@dataclass(frozen=True)
class Registration:
    snapshot_id: str
    directory: str
    manifest_digest: str
    quality_digest: str | None = None


# Trust anchor copied from the locally generated, independently checked B04
# evidence (README / 2026-10-14 final-real-import), not directory discovery.
REGISTRATIONS = (Registration(
    "435c1f15eb21b69f7426af057b31233642544f3631572db4d0322409a96b39c7",
    "scan-20261014-final",
    "9fe9c9b69b641962b0dfd0bd2f6c7fda7bb74e9e80df01fa24df3fe0c95fe549",
), Registration(
    # Independently checked source-only rows and identities, 2026-10-16;
    # strict validation passed before explicit registration on 2026-10-17.
    'fa4abef1ecd45f0ba429266052b27bbab3bb39c86524ea992241b7746d4112a2',
    'scan-20261016-quality',
    '644edd0b1cc431d08e7eaf195ecd327a3eca2559a8dfaeb41f98cfc02d5a1ec7',
    '05db46db7a0fc83fa8e5146a7de67c76e060afff189dbd07e2b1a14f70acca2d',
))


def confined(path, root):
    """Reject symlinks/junctions, including in ancestors, and never expose paths."""
    path, root = Path(path).absolute(), Path(root).absolute()
    try:
        path.relative_to(root)
        if path.resolve(strict=True) != path or root.resolve(strict=True) != root:
            raise ValueError()
        for part in (path, *path.parents):
            if part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction()):
                raise ValueError()
    except (OSError, ValueError, RuntimeError) as exc:
        raise InputError("SNAPSHOT_UNTRUSTED", 422) from exc
    return path


class SnapshotCatalog:
    def __init__(self, *, root=PROJECT_ROOT / "数据/processed",
                 source=PROJECT_ROOT / "数据/raw/uci_online_retail_ii/online_retail_II.xlsx",
                 expected_sha=SOURCE_SHA, registrations=REGISTRATIONS):
        self.root, self.source = Path(root), Path(source)
        self.expected_sha, self.registrations = expected_sha, tuple(registrations)
        if len({r.snapshot_id for r in self.registrations}) != len(self.registrations):
            raise ValueError("duplicate server snapshot registration")

    def registration(self, snapshot_id):
        for entry in self.registrations:
            if entry.snapshot_id == snapshot_id:
                return entry
        raise InputError("SNAPSHOT_NOT_FOUND", 404)

    def verified(self, snapshot_id, manifest_digest=None):
        entry = self.registration(snapshot_id)
        if manifest_digest is not None and manifest_digest != entry.manifest_digest:
            raise InputError("MANIFEST_MISMATCH", 422)
        directory = confined(self.root / entry.directory, self.root)
        for name in ("snapshot_10k.json", "full_scan.json", CSV_NAME):
            confined(directory / name, self.root)
        source = confined(self.source, self.source.parent)
        verified = verify_snapshot(directory, source, entry.manifest_digest, self.expected_sha)
        if verified[0]["snapshot_id"] != entry.snapshot_id or (entry.quality_digest is not None and
                verified[0].get('quality', {}).get('quality_digest') != entry.quality_digest):
            raise InputError("SNAPSHOT_UNTRUSTED", 422)
        return verified

    def listing(self, *, page=1, page_size=50):
        entries = sorted(self.registrations, key=lambda r: r.snapshot_id)
        items = []
        for entry in entries[(page - 1) * page_size:page * page_size]:
            manifest, _, _, _, _ = self.verified(entry.snapshot_id)
            items.append({k: manifest[k] for k in ("snapshot_id", "source_sha256", "csv_sha256", "sheet",
                          "start_row", "end_row", "original_total", "selected_count", "outside_count", "truncation")} |
                         {"manifest_digest": entry.manifest_digest, "scan_status": "VERIFIED",
                          'quality': quality_summary(manifest), 'manifest_version': manifest.get('manifest_version', 1),
                          'recommended': manifest.get('manifest_version') == 2})
        return {"items": items, "total": len(entries), "page": page, "page_size": page_size}
