"""One upgrade check for the combined branch's sole local schema addition."""
import json

from canvas_hub import db
from canvas_hub.paths import get_paths
from canvas_hub.services import Hub
from canvas_hub.util import now_iso


def test_schema16_to17_preserves_manual_tracker_and_original_chat(tmp_path, monkeypatch):
    migrations = db._migration_files()
    paths = get_paths(tmp_path / 'upgrade-home')
    with monkeypatch.context() as patch:
        patch.setattr(db, '_migration_files', lambda: [m for m in migrations if m[0] <= 16])
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
            assert db.schema_version(old.db.conn) == 16
        finally:
            old.close()
    current = Hub(paths)
    try:
        assert db.schema_version(current.db.conn) == 17
        assert current.components.get(account['id'], component['id'])['state']['done'] == {'read': True}
        assert current.db.conn.execute('SELECT content_json FROM assistant_messages WHERE conversation_id=?', (cid,)).fetchone()[0] == payload
        assert current.db.conn.execute('SELECT count(*) FROM assistant_context').fetchone()[0] == 0
        current.reopen()
        assert db.migrate(current.db.conn) == []
    finally:
        current.close()
