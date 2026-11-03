"""Explicit versioned billing receipts; missing receipts never become zero fees."""
import json

import pytest

from fsf.production.command_driver import CommandDriver
from fsf.production.contracts import ProductionError
from fsf.production.runner import run_batch
from fsf.production.store import ProductionStore
from fsf.production.store_runner import run_store_batch
from fsf.remote.contracts import sha
from test_production_runner import setup
from test_production_command_driver import driver


def billed_driver(tmp_path, plan):
    _, config = driver(tmp_path, plan)
    script = tmp_path / 'operator_backend.py'
    text = script.read_text()
    text = text.replace("else:result='unsupported'", """elif op=='settlement':
 result={'request_id':rid,'units':1,'currency':'USD','unit_scale':100}
 result['receipt_digest']=hashlib.sha256(json.dumps(result,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
else:result='unsupported'""")
    script.write_text(text)
    config['settings']['files'][-1]['sha256'] = sha(script)
    config['evaluator_digest'] = sha(script)
    config.update(schema_version='fsf_production_command_driver_v2',
                  settlement={'operation': 'settlement', 'currency': 'USD', 'unit_scale': 100})
    root = tmp_path / 'billed-driver'
    root.mkdir()
    return CommandDriver(config, plan=plan, state_root=root)


@pytest.mark.parametrize('indexed', [False, True])
def test_explicit_receipts_settle_in_both_authorities(tmp_path, indexed):
    plan, book = setup(tmp_path)
    backend = billed_driver(tmp_path, plan)
    if indexed:
        root = tmp_path / 'indexed'
        root.mkdir()
        store = ProductionStore.create(root, plan, book.inspect()['authorization'])
        assert run_store_batch(plan, store=store, driver=backend)['status'] == 'machine_accepted'
        assert store.inspect()['used_units'] == 2
        store.check()
        store.close()
    else:
        assert run_batch(plan, state_root=tmp_path, ledger=book, driver=backend)['status'] == 'machine_accepted'
        assert all(value['settled_units'] == 1 for value in book.inspect()['reservations'].values())


def test_wrong_currency_is_refused_before_submit(tmp_path):
    plan, book = setup(tmp_path)
    backend = billed_driver(tmp_path, plan)
    backend.config['settlement']['currency'] = 'EUR'
    with pytest.raises(ProductionError, match='SETTLEMENT_INVALID'):
        run_batch(plan, state_root=tmp_path, ledger=book, driver=backend)
    assert not (tmp_path / 'submitted.json').exists()
