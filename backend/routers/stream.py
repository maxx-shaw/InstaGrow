import asyncio
import json
from fastapi import APIRouter
from fastapi.responses import StreamingResponse
import events

router = APIRouter(prefix="/stream", tags=["stream"])


@router.get("/live")
async def live_stream():
    """SSE endpoint — pushes job state to the browser every second."""
    async def generate():
        try:
            while True:
                state = events.get_state()
                yield f"data: {json.dumps(state)}\n\n"
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
