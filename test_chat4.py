import logging
import sys
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s [%(levelname)s] %(name)s: %(message)s', force=True, stream=sys.stdout)

for name in ['src.app.agent.brain.core', 'src.app.agent.guardrails', 'src.app.agent.brain.fast_intent', 'src.app.agent.orchestrator']:
    logging.getLogger(name).setLevel(logging.DEBUG)

import httpx
import asyncio
import json

async def test():
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            'http://localhost:8000/api/v1/chat',
            json={'session_id': 'test-127', 'message': 'formal shoes size 9'},
            headers={'Content-Type': 'application/json'}
        )
        data = resp.json()
        ui = data.get('ui_actions', [])
        for a in ui:
            if a.get('type') == 'show_products':
                products = a.get('payload', {}).get('products', [])
                for p in products[:3]:
                    print(f'PRODUCT: {p.get("name")} | Stock: {p.get("in_stock")}')
        print('TEXT:', data.get('text', '')[:200])

asyncio.run(test())