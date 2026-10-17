"""Synthetic FR31 end-to-end API scope and unchanged business results."""
import csv
import hashlib
import io
import json
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from 测试.acceptance.test_pipeline_api import b07, login, write_headers, ORIGIN, assert_error
from 测试.integration.test_reconcile import imported
from 测试.synthetic_xlsx import synthetic_workbook
from 主程序.repositories.database import database_transaction
from 主程序.repositories.xlsx_reader import SHEETS
from 主程序.services.snapshot_service import prepare_snapshot, sha256, QUALITY_VERSION
from 主程序.services.snapshot_catalog import SnapshotCatalog, Registration, REGISTRATIONS
from 主程序.web.app import create_app


@pytest.fixture
def quality_api(b07, tmp_path, monkeypatch):
    import 主程序.services.export_service as exporter
    monkeypatch.setattr(exporter, 'OUTPUT_ROOT', tmp_path / 'exports')
    db, _, _, users, _ = b07
    rows = {2: ['S1', 'SKU', 'synthetic', '1', '40500', '2', '123', 'UK'],
            3: ['C1', 'SKU', 'synthetic cancellation', '-2', '40501', '2', '123', 'UK'],
            4: ['C2', 'POST', 'synthetic fee', '-1', '40502', '2', '123', 'UK'],
            5: ['C1', 'SKU', 'outside same invoice', '-1', '40503', '2', '123', 'UK']}
    source = synthetic_workbook(tmp_path / 'quality-source.xlsx', rows)
    registrations = []
    for version in (1, 2):
        directory = tmp_path / f'quality-v{version}'
        result = prepare_snapshot(source, directory, expected_sha=sha256(source), selection=(SHEETS[1], 2, 4),
                                  **({'quality_version': QUALITY_VERSION} if version == 2 else {}))
        manifest = json.loads((directory / 'snapshot_10k.json').read_text(encoding='utf-8'))
        registrations.append(Registration(result['snapshot_id'], directory.name, result['manifest_digest'],
                                          manifest.get('quality', {}).get('quality_digest')))
    catalog = SnapshotCatalog(root=tmp_path, source=source, expected_sha=sha256(source), registrations=registrations)
    # Historical schema has snapshots.sha256 UNIQUE. Use separate pure synthetic
    # databases for v1/v2 (identical CSV); do not mutate old DDL/facts to compare.
    import shutil
    old_db = tmp_path / 'quality-v1.sqlite3'
    shutil.copyfile(db, old_db)
    return db, users, create_app(db, app_origin=ORIGIN, snapshot_catalog=catalog), catalog, create_app(old_db, app_origin=ORIGIN, snapshot_catalog=catalog)


def _upload(client, catalog, registration):
    directory = catalog.root / registration.directory
    response = client.post('/api/v1/pipeline', data={'snapshot_id': registration.snapshot_id,
                           'manifest_digest': registration.manifest_digest, 'matching_rule_version': 'conservative-v1-90d'},
                           files={'file': ('rows.csv', (directory / 'retail_2010_rows_2_10001.csv').read_bytes(), 'text/csv')},
                           headers=write_headers(client))
    assert response.status_code == 201, response.text
    return response.json()


def _csv(response):
    assert response.status_code == 200, response.text
    return list(csv.DictReader(io.StringIO(response.content.decode('utf-8'))))


def test_v1_unknown_v2_verified_business_results_unchanged_and_scoped_csv(quality_api):
    db, users, app, catalog, old_app = quality_api
    with TestClient(old_app, base_url=ORIGIN) as old_client:
        login(old_client, 'finance')
        old = _upload(old_client, catalog, catalog.registrations[0])
        old_counts = old_client.get(f"/api/v1/imports/{old['batch_id']}/counts").json()
        assert old_counts['quality']['provided'] is False
        assert old_client.get(f"/api/v1/runs/{old['run_id']}").json()['quality']['status'] == 'UNAVAILABLE'
    with TestClient(app, base_url=ORIGIN) as client:
        login(client, 'finance')
        listing = client.get('/api/v1/snapshots').json()['items']
        assert len(listing) == 2
        assert {item['manifest_version']: item['quality']['provided'] for item in listing} == {1: False, 2: True}
        v2listing = next(item for item in listing if item['manifest_version'] == 2)
        assert v2listing['recommended'] and v2listing['quality']['marked_row_count'] == 1
        new = _upload(client, catalog, catalog.registrations[1])
        metrics = ('matched', 'suspected', 'no_original', 'non_product_review', 'difference_count', 'cancel_amount_units',
                   'allocated_amount_units', 'unmatched_amount_units', 'quantity_delta', 'amount_delta_units', 'source_counts')
        assert {k: old['summary'][k] for k in metrics} == {k: new['summary'][k] for k in metrics}
        counts = client.get(f"/api/v1/imports/{new['batch_id']}/counts").json()
        summary = client.get(f"/api/v1/runs/{new['run_id']}").json()
        assert counts['quality']['provided'] is summary['quality']['provided'] is True
        assert counts['quality']['marked_row_count'] == 1
        run = new['run_id']
        all_items = client.get(f'/api/v1/runs/{run}/differences').json()['items']
        target = next(i for i in all_items if i['type'] == 'INSUFFICIENT_QUANTITY')
        detail = client.get('/api/v1/differences/' + target['id']).json()
        assert detail['source']['quality']['provided'] is True
        assert detail['source']['quality']['truncated_invoice_range'] is True
        assert 'TRUNCATED_INVOICE_RANGE' in detail['source']['flags']
        assert 'marked_row_count' not in detail['source']['quality']
        login(client, 'supervisor')
        assigned = client.post(f"/api/v1/differences/{target['id']}/assign", json={'assignee_id': users['HANDLER']['id'], 'expected_version': 0}, headers=write_headers(client))
        assert assigned.status_code == 200, assigned.text
        query = f"type=INSUFFICIENT_QUANTITY&status=PENDING&assignee_id={users['HANDLER']['id']}"
        visible = client.get(f'/api/v1/runs/{run}/differences?{query}&page_size=1').json()
        exported = _csv(client.get(f'/api/v1/runs/{run}/export?format=csv&{query}'))
        differences = [r for r in exported if r['record_type'] == 'difference']
        assert {r['difference_id'] for r in differences} == {i['id'] for i in visible['items']} == {target['id']}
        assert exported[0]['quality_marked_row_count'] == '1'
        assert exported[-1]['scoped_marked_row_count'] == '1'
        assert all(r['quality_provided'] == 'true' and r['quality_version'] == QUALITY_VERSION for r in exported)
        assert 'TRUNCATED_INVOICE_RANGE' in differences[0]['source_flags']
        login(client, 'handler')
        summary = client.get(f'/api/v1/runs/{run}').json()
        assert summary['quality'] == {'status': 'VERIFIED', 'quality_version': QUALITY_VERSION, 'provided': True}
        assert all(k not in summary for k in ('matched', 'difference_count', 'source_counts', 'batch_id'))
        assert_error(client.get(f"/api/v1/imports/{new['batch_id']}/counts"), 403, 'FORBIDDEN')
        hidden = next(i for i in all_items if i['id'] != target['id'])
        assert_error(client.get('/api/v1/differences/' + hidden['id']), 404, 'NOT_FOUND')
        handler_detail = client.get('/api/v1/differences/' + target['id']).json()
        assert handler_detail['source']['quality']['truncated_invoice_range'] is True
        scoped = _csv(client.get(f'/api/v1/runs/{run}/export?format=csv&{query}'))
        assert 'quality_marked_row_count' not in scoped[0]
        assert all('全批' not in r['scope_note'] for r in scoped)
        assert scoped[-1]['scoped_count'] == scoped[-1]['scoped_marked_row_count'] == '1'
        assert {r['difference_id'] for r in scoped if r['record_type'] == 'difference'} == {target['id']}
        empty = _csv(client.get(f'/api/v1/runs/{run}/export?format=csv&status=CLOSED'))
        assert empty[-1]['scoped_count'] == empty[-1]['scoped_marked_row_count'] == '0'


def test_real_historical_shapes_enriched_without_rewriting_and_corrupt_cache_rejected(quality_api):
    db, users, app, catalog, _ = quality_api
    with TestClient(app, base_url=ORIGIN) as client:
        login(client, 'finance')
        data = _upload(client, catalog, catalog.registrations[0])
        batch, run = data['batch_id'], data['run_id']
        with database_transaction(db) as c:
            counts = json.loads(c.execute('SELECT counts_json FROM import_batches WHERE id=?', (batch,)).fetchone()[0]); counts.pop('quality')
            summary = json.loads(c.execute('SELECT summary_json FROM runs WHERE id=?', (run,)).fetchone()[0]); summary.pop('quality')
            c.execute('UPDATE import_batches SET counts_json=? WHERE id=?', (json.dumps(counts), batch))
            c.execute('UPDATE runs SET summary_json=? WHERE id=?', (json.dumps(summary), run))
        digest = hashlib.sha256(db.read_bytes()).hexdigest()
        for url in (f'/api/v1/imports/{batch}/counts', f'/api/v1/runs/{run}'):
            response = client.get(url)
            assert response.status_code == 200, response.text
            assert response.json()['quality'] == {'status': 'UNAVAILABLE', 'quality_version': None, 'marked_row_count': None, 'provided': False}
        assert hashlib.sha256(db.read_bytes()).hexdigest() == digest
        exported = _csv(client.get(f'/api/v1/runs/{run}/export?format=csv'))
        assert exported[0]['quality_provided'] == 'false'
        assert exported[-1]['scoped_marked_row_count'] == ''
        with database_transaction(db) as c:
            counts.pop('original_total')
            c.execute('UPDATE import_batches SET counts_json=? WHERE id=?', (json.dumps(counts), batch))
        assert_error(client.get(f'/api/v1/imports/{batch}/counts'), 422, 'COUNT_MISMATCH')


def test_v2_in_v1_database_reports_explicit_conflict_without_replacing_facts(quality_api):
    _, _, _, catalog, app = quality_api
    with TestClient(app, base_url=ORIGIN) as client:
        login(client, 'finance')
        old = _upload(client, catalog, catalog.registrations[0])
        old_summary = client.get(f"/api/v1/runs/{old['run_id']}").json()
        registration = catalog.registrations[1]
        response = client.post('/api/v1/imports', data={'snapshot_id': registration.snapshot_id, 'manifest_digest': registration.manifest_digest},
                               files={'file': ('rows.csv', (catalog.root / registration.directory / 'retail_2010_rows_2_10001.csv').read_bytes(), 'text/csv')},
                               headers=write_headers(client))
        details = assert_error(response, 409, 'SNAPSHOT_CONTENT_ALREADY_REGISTERED')
        assert details['existing_snapshot_id'] == catalog.registrations[0].snapshot_id
        current = client.get(f"/api/v1/runs/{old['run_id']}").json()
        assert {k: v for k, v in current.items() if k != 'request_id'} == {k: v for k, v in old_summary.items() if k != 'request_id'}
        assert client.get('/api/v1/runs').json()['total'] == 1


def test_fixed_production_catalog_keeps_old_anchor_and_new_explicit_identity():
    assert len(REGISTRATIONS) == 2
    old, new = REGISTRATIONS
    assert old.snapshot_id == '435c1f15eb21b69f7426af057b31233642544f3631572db4d0322409a96b39c7'
    assert old.quality_digest is None
    assert new.snapshot_id == 'fa4abef1ecd45f0ba429266052b27bbab3bb39c86524ea992241b7746d4112a2'
    assert new.manifest_digest == '644edd0b1cc431d08e7eaf195ecd327a3eca2559a8dfaeb41f98cfc02d5a1ec7'
    assert new.quality_digest == '05db46db7a0fc83fa8e5146a7de67c76e060afff189dbd07e2b1a14f70acca2d'
