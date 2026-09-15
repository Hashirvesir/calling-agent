"""EXPERIMENTAL, standalone: talk to OpenAI's gpt-live-1 in a browser.

Not wired into main.py / bot.py / webhooks.py — this is a local-only proof of
concept for the new OpenAI Live API (announced 2026-09-10), which is a
different protocol (wss://.../v1/live/sessions) from the Realtime API our
production pipeline uses, and needs pipecat>=1.9.0 (project pin was 1.8.1 at
the time this was written — upgraded locally for this POC; requirements.txt
not touched yet).

Run:
    .venv\\Scripts\\python.exe scripts\\gpt_live_poc.py --port 7861

Then open the URL it prints (WebRTC test page) and talk to it in the browser.
Needs OPENAI_API_KEY in .env and the pipecat-ai[webrtc] extra installed
(aiortc — already added to this venv for this POC).

TEST (2026-09-14): does OpenAILiveLLMService + ResponsesDelegation work under
our existing app's PipelineTask/PipelineRunner (not the newer PipelineWorker/
WorkerRunner every official pipecat example uses)? The class docstring only
documents a hard WorkerRunner requirement for ClientDelegation (registering
the backend as a child worker) — this script checks whether ResponsesDelegation
is exempt, since a yes means the real integration into bot.py's run_bot() can
reuse its single shared PipelineTask/PipelineRunner ending unchanged, same as
the existing openai_realtime/grok_voice modes.
"""

import os

from dotenv import load_dotenv
from loguru import logger

load_dotenv(".env")

from pipecat.frames.frames import LLMRunFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    AssistantTurnStoppedMessage,
    LLMContextAggregatorPair,
    UserTurnMessageAddedMessage,
)
from pipecat.runner.types import RunnerArguments
from pipecat.runner.utils import create_transport
from pipecat.services.openai.live.llm import OpenAILiveLLMService
from pipecat.services.openai.responses.llm import OpenAIResponsesLLMService
from pipecat.transports.base_transport import BaseTransport, TransportParams

FRONTEND_INSTRUCTIONS = """You are Sana, a friendly voice assistant for a
Pakistani bank. Speak Urdu by default, in one or two short sentences at a
time. If the caller speaks English, switch to English. Let them finish before
you reply. If they ask something you don't know for certain (fees, specific
policies), delegate to your backend to look it up."""

BACKEND_INSTRUCTIONS = """You are the backend of a bank's voice assistant.
Each message is the recent voice conversation as a transcript. Answer
concisely in plain text the assistant can say aloud."""

transport_params = {
    "webrtc": lambda: TransportParams(
        audio_in_enabled=True,
        audio_out_enabled=True,
    ),
}


async def run_bot(transport: BaseTransport, runner_args: RunnerArguments):
    logger.info("Starting gpt-live-1 POC bot (PipelineTask/PipelineRunner test)")

    llm = OpenAILiveLLMService(
        api_key=os.environ["OPENAI_API_KEY"],
        settings=OpenAILiveLLMService.Settings(system_instruction=FRONTEND_INSTRUCTIONS),
        delegation=OpenAILiveLLMService.ResponsesDelegation(
            settings=OpenAIResponsesLLMService.Settings(
                model="gpt-4o",
                system_instruction=BACKEND_INSTRUCTIONS,
            ),
        ),
    )

    context = LLMContext(
        [{"role": "developer", "content": "Greet the caller in Urdu and ask how you can help."}],
    )

    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(context)

    pipeline = Pipeline(
        [
            transport.input(),
            user_aggregator,
            llm,
            transport.output(),
            assistant_aggregator,
        ]
    )

    # The part under test: our app's existing pattern, not PipelineWorker/WorkerRunner.
    task = PipelineTask(
        pipeline,
        params=PipelineParams(enable_metrics=True, enable_usage_metrics=True),
    )

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        logger.info("Client connected")
        await task.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info("Client disconnected")
        await task.cancel()

    @llm.event_handler("on_delegation_created")
    async def on_delegation_created(llm, delegation):
        logger.info(f"Delegated to the backend: {delegation.id}")

    @user_aggregator.event_handler("on_user_turn_message_added")
    async def on_user_turn_message_added(aggregator, message: UserTurnMessageAddedMessage):
        logger.info(f"User: {message.content}")

    @assistant_aggregator.event_handler("on_assistant_turn_stopped")
    async def on_assistant_turn_stopped(aggregator, message: AssistantTurnStoppedMessage):
        logger.info(f"Bot: {message.content}")

    runner = PipelineRunner(handle_sigint=runner_args.handle_sigint)
    await runner.run(task)


async def bot(runner_args: RunnerArguments):
    transport = await create_transport(runner_args, transport_params)
    await run_bot(transport, runner_args)


if __name__ == "__main__":
    from pipecat.runner.run import main

    main()
