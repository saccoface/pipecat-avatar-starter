# Avatar Host — Pipecat edition

A talking event-host avatar that interviews each guest, built as a [Pipecat](https://docs.pipecat.ai) pipeline:

```
your mic → Deepgram Flux (listens, knows when you've finished)
         → Claude (thinks, in character)
         → Deepgram Flux TTS (speaks)
         → SpatialReal (animates the face to that speech)
         → your browser
```

Each step is one line in `bot.py`. Swap any of them and the rest keeps working.

## What you need

- Python 3.11 or newer, and [uv](https://docs.astral.sh/uv/getting-started/installation/)
  (`curl -LsSf https://astral.sh/uv/install.sh | sh` on Mac, or see the link for Windows)
- Chrome, a microphone, and **wired headphones** (on speakers the avatar hears itself)
- Three keys, all your own:
  - **Deepgram** — [console.deepgram.com](https://console.deepgram.com) → API Keys → Create a New API Key.
    New accounts get free credit.
  - **Anthropic** — [console.anthropic.com](https://console.anthropic.com) → API Keys.
  - **SpatialReal** — [app.spatialreal.ai](https://app.spatialreal.ai) → Apps → Create API Key.
    Copy the **API key** and the **App ID**. The free tier covers a workshop.

## Run it

```bash
cd pipecat-avatar-starter
cp env.example .env       # then open .env and paste your keys
uv sync                   # about a minute the first time
uv run bot.py -t webrtc
```

Open **http://localhost:7860/avatar/** in Chrome, allow the microphone and press **Start**.

### Pick a face

Go to [app.spatialreal.ai/avatars/library](https://app.spatialreal.ai/avatars/library), hover an
avatar, click the copy icon and paste the ID into `SPATIALREAL_AVATAR_ID` in `.env`.

### Talk to it

The avatar is an event host. It interviews you, one question at a time: how the night is going,
how you heard about it, what you do, how much you code, whether you have used Pipecat, and what
you want avatars for. Answer out loud and see how it reacts.

## Make it yours

| Change | Where |
|---|---|
| Which character runs | `CHARACTER` in `.env`: `host-interview` (the event host) or `front-desk` (a business assistant) |
| The front-desk business | `BUSINESS_NAME` and `BUSINESS_FACTS` in `.env` — plain sentences: hours, address, what you sell |
| A brand-new character | Copy a file in `prompts/`, rewrite it, set `CHARACTER` to its name. Any `[UPPER_CASE]` word in brackets is filled from the `.env` setting of the same name |
| The host's questions | `prompts/host-interview.md`: change the list and it asks your questions instead |
| The voice | `DEEPGRAM_VOICE` in `.env`, any `flux-<name>-en` voice |
| The model | `CLAUDE_MODEL` in `bot.py` (Haiku 4.5 is the fastest to start talking) |
| A different LLM or TTS entirely | Swap the service in `run_bot()` in `bot.py`; Pipecat has dozens |

## When it goes wrong

- **The bot's terminal says `... is not set in .env`** — that key is missing or misspelled in `.env`.
- **"SpatialReal rejected the API key"** — copy the key again from app.spatialreal.ai, no spaces.
- **The face loads but never moves** — the voice still plays; check the terminal for a SpatialReal
  error, and that `SPATIALREAL_AVATAR_ID` is an avatar your app can use.
- **The avatar talks to itself** — you are on speakers. Use wired headphones.
- **Port 7860 is in use** — `uv run bot.py -t webrtc --port 7861`, then open that port, and change
  `PIPECAT_ALLOWED_ORIGINS` in `.env` to match.

The avatar processor and the page are adapted from SpatialReal's
[pipecat-spatialreal](https://github.com/SpatialReal-ai/pipecat-spatialreal) example (Apache-2.0).
