"""Avatar Host, built with Pipecat: an event-host voice bot with a SpatialReal avatar that interviews each guest.

    uv run bot.py -t webrtc

then open http://localhost:7860/avatar/ and press Start.

The pipeline, one processor per job:

    your mic -> Deepgram Flux (speech to text, and it decides when you have finished a turn)
             -> Claude (thinks, in character)
             -> Deepgram Flux TTS (speaks)
             -> SpatialRealAvatarProcessor (animates the face to that speech)
             -> your browser

Pipecat's own peer-to-peer WebRTC carries the audio, so there is no Daily or LiveKit account to
make. Adapted from SpatialReal's example (github.com/SpatialReal-ai/pipecat-spatialreal,
Apache-2.0): its Gemini and Cartesia are swapped for Claude and Deepgram Flux, and its Daily
transport for SmallWebRTC.
"""

import asyncio
import os
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp
from dotenv import load_dotenv
from fastapi import HTTPException, Request
from fastapi.staticfiles import StaticFiles
from loguru import logger
from pipecat.frames.frames import LLMRunFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker, ProcessorUnusablePolicy
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
from pipecat.runner.run import app, main
from pipecat.runner.types import RunnerArguments
from pipecat.runner.utils import create_transport
from pipecat.services.anthropic.llm import AnthropicLLMService
from pipecat.services.deepgram.flux.stt import DeepgramFluxSTTService
from pipecat.services.deepgram.flux.tts import DeepgramFluxTTSService
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.workers.runner import WorkerRunner

from pipecat_spatialreal import SpatialRealAvatarProcessor

load_dotenv(override=True)

HERE = Path(__file__).resolve().parent

# Claude, called directly with your own Anthropic key.
# The fastest Claude for live speech: about 0.7 s to the first word in our tests.
CLAUDE_MODEL = "claude-haiku-4-5"
# Any Flux voice works; the avatar lip-syncs to whatever audio it is given.
DEFAULT_VOICE = "flux-brooke-en"
# Where session tokens for the browser come from (SpatialReal's production API).
SPATIALREAL_API = "https://api.spatialreal.com"
# Greet even if the browser never says it is ready (a scripted caller, say).
GREETING_FALLBACK_S = 5.0

transport_params = {
    "webrtc": lambda: TransportParams(audio_in_enabled=True, audio_out_enabled=True),
}


def need(name: str) -> str:
    """The setting ``name`` from .env, or a clear error naming it."""
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is not set in .env (see env.example)")
    return value


def character_prompt() -> str:
    """The character's prompt: prompts/<CHARACTER>.md, host-interview unless .env says otherwise.

    Any [UPPER_CASE] placeholder in the file is filled from the .env setting of the same name,
    so a new character is a new .md file, not a code change."""
    name = os.getenv("CHARACTER", "").strip() or "host-interview"
    path = HERE / "prompts" / f"{name}.md"
    if not path.is_file():
        options = ", ".join(sorted(p.stem for p in (HERE / "prompts").glob("*.md")))
        raise RuntimeError(f"CHARACTER={name} has no prompts/{name}.md (choose from: {options})")
    text = path.read_text(encoding="utf-8")
    for placeholder in set(re.findall(r"\[([A-Z][A-Z0-9_]*)\]", text)):
        text = text.replace(f"[{placeholder}]", need(placeholder))
    return text


# ---------------------------------------------------------------------------------------------
# The browser needs a short-lived session token to draw the avatar. This route mints one with
# your API key, so the key itself never reaches the page. It only answers pages this server
# serves: otherwise any website open in your browser could take tokens billed to you.
# ---------------------------------------------------------------------------------------------

_PORTS = {"http": 80, "https": 443}


def _origin(url: str):
    parts = urlsplit(url)
    if parts.scheme not in _PORTS or not parts.hostname:
        return None
    return parts.scheme, parts.hostname.lower(), parts.port or _PORTS[parts.scheme]


def refuse_other_origins(request: Request) -> None:
    if request.headers.get("sec-fetch-site", "").lower() == "cross-site":
        raise HTTPException(status_code=403, detail="refused: a request from another site")
    origin = request.headers.get("origin")
    if origin is not None and _origin(origin) != _origin(str(request.url)):
        raise HTTPException(status_code=403, detail=f"refused: a request from {origin}")


@app.get("/spatialreal-token")
async def spatialreal_token(request: Request) -> dict:
    refuse_other_origins(request)
    try:
        api_key, app_id, avatar_id = (
            need("SPATIALREAL_API_KEY"),
            need("SPATIALREAL_APP_ID"),
            need("SPATIALREAL_AVATAR_ID"),
        )
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    try:
        async with (
            aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as http,
            http.post(
                f"{SPATIALREAL_API}/v1/auth/session-token",
                json={"expire_at": int(time.time()) + 3600},
                headers={"X-API-KEY": api_key},
            ) as response,
        ):
            reply = await response.json(content_type=None)
            status = response.status
    except (aiohttp.ClientError, TimeoutError, ValueError) as e:
        raise HTTPException(status_code=502, detail=f"could not reach SpatialReal: {e}") from e
    if status != 200 or not isinstance(reply, dict) or not reply.get("session_token"):
        detail = "SpatialReal rejected the API key" if status == 401 else f"SpatialReal answered {status}"
        raise HTTPException(status_code=502, detail=detail)
    return {"session_token": reply["session_token"], "avatar_id": avatar_id, "app_id": app_id}


# The page, served from here so the token route and the page share an origin.
app.mount("/avatar", StaticFiles(directory=HERE / "client", html=True), name="avatar")


async def run_bot(transport: BaseTransport, runner_args: RunnerArguments) -> None:
    logger.info("Starting the avatar host")

    # 1. Ears. Flux transcribes and also decides when you have stopped talking.
    stt = DeepgramFluxSTTService(api_key=need("DEEPGRAM_API_KEY"))

    # 2. Brain. The character prompt is the system instruction.
    llm = AnthropicLLMService(
        api_key=need("ANTHROPIC_API_KEY"),
        settings=AnthropicLLMService.Settings(
            model=CLAUDE_MODEL, system_instruction=character_prompt()
        ),
    )

    # 3. Voice.
    tts = DeepgramFluxTTSService(
        api_key=need("DEEPGRAM_API_KEY"),
        settings=DeepgramFluxTTSService.Settings(voice=os.getenv("DEEPGRAM_VOICE") or DEFAULT_VOICE),
    )

    # 4. Face. Right after the TTS, before the transport's output. The audio still plays through
    #    the transport; the animation reaches the browser as RTVI messages lined up with it.
    avatar = SpatialRealAvatarProcessor(
        api_key=need("SPATIALREAL_API_KEY"),
        app_id=need("SPATIALREAL_APP_ID"),
        avatar_id=need("SPATIALREAL_AVATAR_ID"),
    )

    context = LLMContext()
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(context)

    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            user_aggregator,
            llm,
            tts,
            avatar,
            transport.output(),
            assistant_aggregator,
        ]
    )

    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(enable_metrics=True, enable_usage_metrics=True),
        idle_timeout_secs=runner_args.pipeline_idle_timeout_secs,
        processor_unusable_policy=ProcessorUnusablePolicy.END,
    )
    runner = WorkerRunner(handle_sigint=runner_args.handle_sigint)

    greeted = {"done": False, "fallback": None}

    async def greet() -> None:
        if greeted["done"]:
            return
        greeted["done"] = True
        fallback = greeted["fallback"]
        if fallback is not None and fallback is not asyncio.current_task():
            fallback.cancel()
        context.add_message(
            {"role": "user", "content": "(Someone has just arrived. Greet them in one short sentence and ask your first question.)"}
        )
        await worker.queue_frames([LLMRunFrame()])

    # Greet when the browser says it is ready, so the first words arrive with their animation.
    @worker.rtvi.event_handler("on_client_ready")
    async def on_client_ready(rtvi):
        await greet()

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        async def greet_anyway() -> None:
            await asyncio.sleep(GREETING_FALLBACK_S)
            await greet()

        if greeted["fallback"] is None and not greeted["done"]:
            greeted["fallback"] = asyncio.create_task(greet_anyway())

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        fallback = greeted["fallback"]
        if fallback is not None:
            fallback.cancel()
        await runner.cancel()

    await runner.add_workers(worker)
    await runner.run()


async def bot(runner_args: RunnerArguments) -> None:
    """Pipecat's runner calls this once per browser session."""
    transport = await create_transport(runner_args, transport_params)
    await run_bot(transport, runner_args)


if __name__ == "__main__":
    main()
