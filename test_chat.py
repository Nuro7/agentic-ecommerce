import httpx
import asyncio

async def test():
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            "http://localhost:8000/api/v1/chat",
            json={"session_id": "test-123", "message": "formal shoes size 9"},
            headers={"Content-Type": "application/json"}
        )
        print(f"Status: {resp.status_code}")
        import json
        print(json.dumps(resp.json(), indent=2))

asyncio.run(test())