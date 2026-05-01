import os
import httpx
import asyncio
from dotenv import load_dotenv

load_dotenv()

async def test_groq():
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        print("GROQ_API_KEY not found")
        return
    
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": "llama-3.3-70b-versatile",
        "messages": [{"role": "user", "content": "Hi"}],
        "max_tokens": 10
    }
    
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(url, headers=headers, json=payload)
            response.raise_for_status()
            print("Groq API: Success")
            print(response.json()["choices"][0]["message"]["content"])
        except Exception as e:
            print(f"Groq API Error: {e}")

if __name__ == "__main__":
    asyncio.run(test_groq())
