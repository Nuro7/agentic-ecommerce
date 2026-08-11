import httpx
import asyncio
import json

async def test():
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            "http://localhost:8000/api/v1/chat",
            json={"session_id": "test-123", "message": "formal shoes size 9"},
            headers={"Content-Type": "application/json"}
        )
        print(f"Status: {resp.status_code}")
        data = resp.json()
        print(f"text: {data.get('text', '')[:200]}")
        print(f"response_text: {data.get('response_text', '')[:200]}")
        print(f"speech_text: {data.get('speech_text', '')[:200]}")
        ui_actions = data.get('ui_actions', [])
        print(f"ui_actions count: {len(ui_actions)}")
        for a in ui_actions:
            if a.get('type') == 'show_products':
                products = a.get('payload', {}).get('products', [])
                print(f"  Products shown: {len(products)}")
                for p in products[:3]:
                    print(f"    {p.get('name')} | ${p.get('price')} | In stock: {p.get('in_stock')}")

asyncio.run(test())