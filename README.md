# UpliftAI TTS Integration for Pipecat

ngrok http --url=sash-reconcile-jab.ngrok-free.dev 7860

This repository provides an integration of [Uplift AI's](https://upliftai.org) Text-to-Speech (TTS) service with the [Pipecat](https://github.com/pipecat-ai/pipecat) framework.

## About

Uplift AI provides high-quality, low-latency text-to-speech synthesis optimized for conversational AI applications using local languages like Urdu. This integration enables seamless use of Uplift's TTS capabilities within Pipecat pipelines.

**Integration Type:** This is a **TTSService** integration (HTTP-based service without word/timestamp alignment), using the Uplift API endpoint: `https://api.upliftai.org/v1/synthesis/text-to-speech`

This is the first integration of Uplift AI with Pipecat, starting with their TTS service. Future integrations may include additional Uplift AI services.

**Company Attribution:** This integration was developed by havkerboi123 as a personal contribution to enable Uplift TTS within the Pipecat framework.

## Features

- ✅ Multiple voice options (Info/Edu, Gen Z, Dada Jee, Nostalgic News)
- ✅ Various audio formats (WAV, MP3, OGG, ULAW)
- ✅ Configurable sample rates
- ✅ Async/await support
- ✅ Comprehensive error handling
- ✅ Metrics and usage tracking
- ✅ Dynamic voice switching
- ✅ Session management with aiohttp

## Installation

### Install from GitHub

```bash
# With pip
pip install git+https://github.com/havkerboi123/pipecat-upliftai-tts.git

# With uv (recommended - faster)
uv pip install git+https://github.com/havkerboi123/pipecat-upliftai-tts.git
```

> **Note:** PyPI package will be published after community review and approval.

### Ubuntu / Linux Installation

On a fresh Ubuntu (20.04 / 22.04 / 24.04) system, install system dependencies first:

```bash
# Update package list
sudo apt update

# Install Python, pip, venv, git, and audio libraries
sudo apt install -y python3 python3-pip python3-venv git ffmpeg libsndfile1 portaudio19-dev

# (Optional) Install uv for faster dependency management
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Then set up the project in a virtual environment:

```bash
# Clone the repository
git clone https://github.com/havkerboi123/pipecat-upliftai-tts.git
cd pipecat-upliftai-tts

# Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install the package
pip install -e .
# or with uv
uv pip install -e .
```

### Development Installation

```bash
# Clone the repository
git clone https://github.com/havkerboi123/pipecat-upliftai-tts.git
cd pipecat-upliftai-tts

# Install in editable mode
uv pip install -e .
# or with pip
pip install -e .
```

## Prerequisites

- Python 3.8 or higher
- UpliftAI API key ([Get yours here](https://platform.upliftai.org/studio/api-keys))
- OpenAI API key

## Getting Your API Key

1. Sign up at [Uplift AI](https://upliftai.org)
2. Go to [API Keys](https://platform.upliftai.org/studio/api-keys)
3. Generate an API key (format: `sk_api_...`)

## Usage

## Usage with Pipecat Pipeline

`UpliftHttpTTSService` integrates Uplift's text-to-speech into a Pipecat pipeline, converting LLM text output into high-quality speech.

```python
import os
import aiohttp
from pipecat.pipeline.pipeline import Pipeline
from pipecat_upliftai import UpliftHttpTTSService

async with aiohttp.ClientSession() as session:
    llm = ...  # Your LLM service
    stt = ...  # Your STT service

    tts = UpliftHttpTTSService(
        aiohttp_session=session,
        api_key=os.getenv("UPLIFT_API_KEY"),
        voice_id="v_8eelc901",  # Info/Edu voice
        output_format="WAV_22050_16",
    )

    pipeline = Pipeline([
        transport.input(),               # audio/user input
        stt,                             # speech to text
        context_aggregator.user(),       # add user text to context
        llm,                             # LLM generates response
        tts,                             # Uplift TTS synthesis
        transport.output(),              # stream audio back to user
        context_aggregator.assistant(),  # store assistant response
    ])
```

See [`example.py`](example.py) for a complete working example.

### Available Voices

| Voice ID     | Description                          |
| ------------ | ------------------------------------ |
| `v_8eelc901` | Info/Edu - Clear, educational tone   |
| `v_kwmp7zxt` | Gen Z - Casual, modern style         |
| `v_yypgzenx` | Dada Jee - Traditional, respectful   |
| `v_30s70t3a` | Nostalgic News - Classic news anchor |

### Available Audio Formats

- `WAV_22050_16` - 16-bit WAV at 22.05 kHz (default)
- `WAV_22050_32` - 32-bit WAV at 22.05 kHz
- `MP3_22050_32` - MP3 at 32 kbps
- `MP3_22050_64` - MP3 at 64 kbps
- `MP3_22050_128` - MP3 at 128 kbps
- `OGG_22050_16` - OGG Vorbis 16-bit
- `ULAW_8000_8` - 8-bit μ-law at 8 kHz

### Dynamic Voice Switching

```python
# Change voice during runtime
await tts.set_voice_id("v_kwmp7zxt")  # Switch to Gen Z voice

# Change output format
await tts.set_output_format("MP3_22050_128")
```

## Running the Example

1. Install dependencies:

   ```bash
   uv sync
   # or
   pip install -r requirements.txt
   ```

2. Set up your environment:

   ```bash
   cp .env.example .env
   # Edit .env and add your API keys
   ```

3. Run the example:
   ```bash
   python example.py
   # or with uv
   uv run python example.py
   ```

The bot will connect via WebRTC and synthesize text using Uplift TTS.

## Configuration Options

### Constructor Parameters

```python
UpliftHttpTTSService(
    api_key: str,                    # Required: Your Uplift API key
    base_url: str,                   # Optional: API endpoint (default: Uplift's API)
    voice_id: str,                   # Optional: Default voice (default: "v_8eelc901")
    output_format: str,              # Optional: Audio format (default: "WAV_22050_16")
    sample_rate: int,                # Optional: Sample rate in Hz (default: 22050)
    aiohttp_session: ClientSession,  # Optional: Shared aiohttp session
    params: InputParams,             # Optional: Additional parameters
)
```

### Input Parameters

Use the `InputParams` class for advanced configuration:

```python
from uplift_tts_service import UpliftHttpTTSService

params = UpliftHttpTTSService.InputParams(
    voice_id="v_kwmp7zxt",
    output_format="MP3_22050_64",
)

tts = UpliftHttpTTSService(
    api_key=api_key,
    params=params,
)
```

## Error Handling

The service includes comprehensive error handling:

```python
from pipecat.frames.frames import ErrorFrame

async for frame in tts.run_tts("Hello world"):
    if isinstance(frame, ErrorFrame):
        print(f"TTS Error: {frame.error}")
    else:
        # Process audio frame
        pass
```

## Limitations

- Maximum text length: 2,500 characters per request
- Rate limits apply based on your Uplift API plan
- Some audio formats may have specific browser compatibility requirements

## Compatibility

- **Pipecat Version:** Tested with Pipecat v0.0.86 and later
- **Python Version:** 3.8+
- **Dependencies:**
  - `pipecat-ai >= 0.0.86`
  - `aiohttp >= 3.8.0`
  - `loguru >= 0.7.0`

## Support

- **Issues:** [GitHub Issues](https://github.com/havkerboi123/pipecat-uplift-tts/issues)
- **Uplift AI Documentation:** [https://upliftai.org/docs](https://upliftai.org/docs)
- **Pipecat Documentation:** [https://docs.pipecat.ai](https://docs.pipecat.ai)

## License

This project is licensed under the BSD 2-Clause License - see the [LICENSE](LICENSE) file for details.

## Changelog

See [CHANGELOG.md](CHANGELOG.md) for version history and updates.

## Acknowledgments

- Thanks to the [Pipecat](https://github.com/pipecat-ai/pipecat) team for the excellent framework
- Thanks to [Uplift AI](https://upliftai.org) for providing high-quality TTS services
