"""Opt-in live smoke test. Sends only synthetic examples; uses provider credits."""
import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
from app.config import ROOT, llm_ready, llm_settings
from app.engine import draft_from_notes
from app.main import create_app
from app.store import Store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='Authorize five live reasoning requests')
    args = parser.parse_args()
    if not args.live:
        parser.error('Use --live to make requests that consume provider credits.')
    if not llm_ready():
        raise SystemExit('Reasoning is not configured. Check your local .env.')
    print('Provider/model:', llm_settings()['provider'], llm_settings()['model'])
    cases = [
        ('English handoff', 'My technician has not arrived. My booking ID is CD1042.', 'Technician delay', 'needs_human', 'CD1042', None),
        ('English knowledge', 'How often should I replace the filter in my purifier?', 'Filter replacement', 'open', None, 2),
        ('Hindi knowledge', 'मेरे वाटर प्यूरीफायर का फ़िल्टर कितने समय में बदलना चाहिए? हिंदी में बताइए।', 'Filter replacement', 'open', None, 2),
        ('Telugu handoff', 'టెక్నీషియన్ ఇంకా రాలేదు. నా బుకింగ్ ఐడీ CD2042. దయచేసి తెలుగులో సమాధానం ఇవ్వండి.', 'Technician delay', 'needs_human', 'CD2042', None),
    ]
    with tempfile.TemporaryDirectory(prefix='awaazdesk-smoke-') as directory:
        url = f'sqlite:///{directory}/test.db'
        store = Store(url)
        ids = []
        with TestClient(create_app(store)) as client:
            assert client.post('/api/business/publish', json={'revision': 1}).status_code == 200
            for label, message, category, status, booking, source in cases:
                call = client.post('/api/calls').json()
                result = client.post(f'/api/calls/{call["id"]}/turn', json={'revision': 0, 'text': message})
                if result.status_code != 200:
                    raise SystemExit(f'{label}: HTTP {result.status_code}: {result.json().get("detail")}')
                saved = result.json()
                ticket = saved['ticket']
                assert ticket['category'] == category, f'{label}: category {ticket["category"]}'
                assert ticket['status'] == status, f'{label}: status {ticket["status"]}'
                assert ticket['engine'] == 'hosted-llm'
                if booking:
                    assert ticket['fields'].get('booking_id') == booking, label
                if source:
                    assert source in [s['id'] for s in ticket['sources']], f'{label}: missing evidence'
                print(label, 'PASS:', saved['messages'][-1]['content'])
                ids.append(call['id'])
        second_store = Store(url)
        with TestClient(create_app(second_store)) as restarted:
            for call_id in ids:
                call = restarted.get('/api/calls/' + call_id).json()
                assert call['revision'] == 1 and len(call['messages']) == 3
        print('Persistence after app recreation: PASS')
        store.engine.dispose()
        second_store.engine.dispose()
    business = json.loads((ROOT / 'sample_business.json').read_text())
    proposal = asyncio.run(draft_from_notes('Change the business name to ClearDrop Demo. Keep all existing policies.', business))
    assert proposal['config']['name'] == 'ClearDrop Demo'
    print('Business draft generation: PASS (proposal only; nothing published)')


if __name__ == '__main__':
    main()
