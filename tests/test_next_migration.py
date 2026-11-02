"""Upgrade paths for the combined branch's context and usage additions."""
import json

import pytest

from canvas_hub import db
from canvas_hub.paths import get_paths
from canvas_hub.services import Hub
from canvas_hub.util import now_iso


@pytest.mark.parametrize('from_version', [16, 17])
def test_upgrade_preserves_manual_tracker_original_chat_and_summary_usage(tmp_path, monkeypatch, from_version):
    migrations = db._migration_files()
    paths = get_paths(tmp_path / 'upgrade-home')
    with monkeypatch.context() as patch:
        patch.setattr(db, '_migration_files', lambda: [m for m in migrations if m[0] <= from_version])
        old = Hub(paths)
        try:
            instance = old.identity.add_instance('https://canvas.example.edu')
            account = old.identity.upsert_verified_account(instance['id'], {'canvas_user_id': 'synthetic'})
            component = old.components.create(account['id'], 'checklist', {'title': 'Keep manual state', 'sections': [
                {'id': 'work', 'rows': [{'id': 'read', 'label': 'Read'}]}]})
            old.components.set_state(account['id'], component['id'], {'done': {'read': True}})
            stamp = now_iso()
            cid = old.db.conn.execute('INSERT INTO assistant_conversations(account_id,title,created_at,updated_at) VALUES (?,?,?,?)',
                                     (account['id'], 'Keep history', stamp, stamp)).lastrowid
            payload = json.dumps({'text': 'Synthetic original history', 'sent_text': 'Synthetic original history'})
            old.db.conn.execute('INSERT INTO assistant_messages(conversation_id,role,content_json,created_at) VALUES (?,?,?,?)',
                                (cid, 'user', payload, stamp))
            if from_version == 17:
                old.db.conn.execute('INSERT INTO assistant_context_usage(conversation_id,provider,model,status,usage_json,estimated_input_tokens,created_at) VALUES (?,?,?,?,?,?,?)',
                                    (cid, 'allowance_gateway', 'mock-text', 'completed', '{"input_tokens":10,"output_tokens":2}', 12, stamp))
            assert db.schema_version(old.db.conn) == from_version
        finally:
            old.close()
    current = Hub(paths)
    try:
        assert db.schema_version(current.db.conn) == 18
        assert current.components.get(account['id'], component['id'])['state']['done'] == {'read': True}
        assert current.db.conn.execute('SELECT content_json FROM assistant_messages WHERE conversation_id=?', (cid,)).fetchone()[0] == payload
        assert current.db.conn.execute('SELECT count(*) FROM assistant_context').fetchone()[0] == 0
        if from_version == 17:
            row = current.db.conn.execute('SELECT purpose,usage_json FROM assistant_context_usage WHERE conversation_id=?', (cid,)).fetchone()
            assert row['purpose'] == 'summary' and json.loads(row['usage_json']) == {'input_tokens': 10, 'output_tokens': 2}
        current.reopen()
        assert db.migrate(current.db.conn) == []
    finally:
        current.close()
