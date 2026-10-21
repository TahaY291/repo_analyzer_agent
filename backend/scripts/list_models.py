import httpx

from app.core.config import settings

r = httpx.get(
    "https://api.groq.com/openai/v1/models",
    headers={"Authorization": f"Bearer {settings.groq_api_key}"},
    timeout=15,
)
r.raise_for_status()
for m in sorted(r.json()["data"], key=lambda m: m["id"]):
    print(m["id"])