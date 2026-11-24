"""The agent service. Run: uvicorn agent_service.app:app --port 8000"""

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, HTTPException  # noqa: E402

from .schemas import ChatRequest, ChatResponse  # noqa: E402
from .service import ChatService  # noqa: E402

app = FastAPI(title="City Care front desk agent", version="1.0")
_service: ChatService | None = None


def get_service() -> ChatService:
    """Built on first use (so importing the app needs no API key). Tests replace it with set_service()."""
    global _service
    if _service is None:
        _service = ChatService.from_env()
    return _service


def set_service(service: ChatService | None) -> None:
    global _service
    _service = service


@app.get("/health")
async def health():
    return {"ok": True}


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest) -> ChatResponse:
    return await get_service().handle(req)


@app.get("/trace/{session_id}")
async def trace(session_id: str):
    """Every model call and tool call of every turn in this session: inputs, outputs, timing, tokens, cost."""
    traces = get_service().traces.get(session_id)
    if traces is None:
        raise HTTPException(status_code=404, detail="No trace for this session")
    return {"session_id": session_id, "turns": traces,
            "total_cost_usd": round(sum(t["totals"]["cost_usd"] for t in traces), 6)}
