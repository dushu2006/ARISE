import asyncio
import uuid

from arise.config.settings import get_settings
from arise.server import create_app
from arise.core.models import (
    ModelRequest,
    ModelMessage,
    ModelRole,
    ModelSelectionRequest,
)

async def main():
    settings = get_settings()
    app = create_app(settings)
    router = app.state.services.router

    request = ModelRequest(
        request_id=str(uuid.uuid4()),
        correlation_id=str(uuid.uuid4()),
        session_id="nvidia-test",
        role=ModelRole.FAST_REASONER,
        messages=(
            ModelMessage(
                role="user",
                content="Reply with exactly: ARISE NVIDIA GATEWAY OK",
            ),
        ),
        model_id=settings.model.model_id,
        max_output_tokens=32,
        temperature=0,
        timeout_seconds=30,
        stream=False,
    )

    selection = ModelSelectionRequest(
        role=ModelRole.FAST_REASONER,
        task_type="test",
        complexity="low",
        latency_budget_ms=30000,
        context_tokens=128,
        privacy="cloud_allowed",
    )

    response = await router.complete(request, selection=selection)

    print("RESPONSE:", response.content)
    print("STATUS:", router.status())

    await router.close()

asyncio.run(main())
