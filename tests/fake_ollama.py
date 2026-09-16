"""Minimal stand-in for Ollama's OpenAI-compatible API, used by the tests."""
import json

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

app = FastAPI()
seen_auth: list[str] = []


@app.get("/api/tags")
async def tags():
    return {"models": [{"name": "fake:27b"}]}


@app.get("/v1/models")
async def models():
    return {"object": "list", "data": [{"id": "fake:27b", "object": "model"}]}


@app.post("/v1/chat/completions")
async def chat(request: Request):
    body = await request.json()
    seen_auth.append(request.headers.get("authorization", ""))
    usage = {"prompt_tokens": 40, "completion_tokens": 60, "total_tokens": 100}
    if body.get("stream"):
        async def gen():
            for word in ["hello", " ", "world"]:
                yield "data: " + json.dumps({"choices": [{"delta": {"content": word}}]}) + "\n\n"
            yield "data: " + json.dumps({"choices": [], "usage": usage}) + "\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(gen(), media_type="text/event-stream")
    return {"model": body.get("model"), "choices": [{"message": {"role": "assistant", "content": "hello world"}}], "usage": usage}
