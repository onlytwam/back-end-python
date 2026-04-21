"""
app/speech/router.py
─────────────────────
Voice search for the TWAM Android APK.

Flow:
  Android AudioRecord (raw 16 kHz 16-bit mono PCM)
    → POST /api/Speech/transcribe  (multipart, field name: "audio")
    → Azure Cognitive Services Speech SDK
        - credentials read from speech.azure_speech_config table
        - PhraseListGrammar loaded from live Brand/Category/Product/Color data
    → {"transcript": "show me black bras"}
    → Android fires onResult event → JS frontend navigates to /global-search?q=...

No environment variables used — all credentials come from the database.
"""

import io
import wave
import logging

from fastapi import APIRouter, UploadFile, File, HTTPException
from fastapi.responses import JSONResponse

import azure.cognitiveservices.speech as speechsdk

from app.speech.repository import get_speech_credentials, build_phrase_hints

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/Speech", tags=["Speech"])


# ── Helpers ───────────────────────────────────────────────────────────────────

def _pcm_to_wav(pcm_bytes: bytes,
                sample_rate: int  = 16000,
                channels: int     = 1,
                sample_width: int = 2) -> bytes:
    """Wrap raw PCM bytes in a minimal WAV container."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sample_width)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_bytes)
    return buf.getvalue()


def _transcribe(pcm_bytes: bytes, subscription_key: str, region: str) -> str:
    """
    Feed raw PCM to Azure Speech SDK with TWAM phrase hints.
    Returns the recognised transcript string (may be empty on silence).
    """
    # ── Speech config ──────────────────────────────────────────────────────
    speech_config = speechsdk.SpeechConfig(
        subscription=subscription_key,
        region=region
    )
    speech_config.speech_recognition_language = "en-IN"

    # ── Audio config — push raw PCM directly (no file on disk) ────────────
    stream_format = speechsdk.audio.AudioStreamFormat(
        samples_per_second=16000,
        bits_per_sample=16,
        channels=1
    )
    push_stream = speechsdk.audio.PushAudioInputStream(stream_format)
    audio_config = speechsdk.audio.AudioConfig(stream=push_stream)

    # ── Recogniser ─────────────────────────────────────────────────────────
    recognizer = speechsdk.SpeechRecognizer(
        speech_config=speech_config,
        audio_config=audio_config
    )

    # ── Phrase hints — boost TWAM vocabulary strongly ──────────────────────
    # PhraseListGrammar tells Azure to prefer these words/phrases over
    # acoustically similar general-English alternatives.
    # e.g. "TWAM" beats "tram/team", "Amante" beats "Amanda/amount".
    phrase_list = speechsdk.PhraseListGrammar.from_recognizer(recognizer)
    for phrase in build_phrase_hints():
        phrase_list.addPhrase(phrase)

    # ── Push audio and run recognition ─────────────────────────────────────
    push_stream.write(pcm_bytes)
    push_stream.close()

    result = recognizer.recognize_once_async().get()

    # ── Handle result ───────────────────────────────────────────────────────
    if result.reason == speechsdk.ResultReason.RecognizedSpeech:
        return result.text.strip()

    if result.reason == speechsdk.ResultReason.NoMatch:
        logger.info("Azure Speech: no speech recognised (silence or noise)")
        return ""

    if result.reason == speechsdk.ResultReason.Canceled:
        details = speechsdk.CancellationDetails.from_result(result)
        logger.error(
            "Azure Speech cancelled: %s — %s",
            details.reason, details.error_details
        )
        raise RuntimeError(f"Azure Speech error: {details.error_details}")

    return ""


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post(
    "/transcribe",
    summary="Transcribe voice search audio",
    response_description="Recognised transcript from the audio",
)
async def transcribe_audio(
    audio: UploadFile = File(
        ...,
        description="Raw PCM audio — 16 kHz, 16-bit, mono. "
                    "Sent as multipart/form-data with field name 'audio'."
    )
):
    """
    Receive raw PCM audio from the Android APK and return a transcript.

    - **audio**: raw 16 kHz 16-bit mono PCM bytes (multipart field name: `audio`)
    - Returns: `{"transcript": "show me black bras"}`
    - Credentials are fetched from `speech.azure_speech_config` in the DB.
    - Phrase hints are built from the live Brand / Category / Product / Color tables.
    """
    # ── Read audio bytes ───────────────────────────────────────────────────
    raw_bytes = await audio.read()

    if len(raw_bytes) < 3200:           # less than 0.1 s at 16kHz 16-bit mono
        raise HTTPException(
            status_code=400,
            detail="Audio too short — minimum 0.1 seconds required"
        )

    if len(raw_bytes) > 10 * 16000 * 2:   # more than 10 seconds
        raw_bytes = raw_bytes[:10 * 16000 * 2]   # truncate silently

    # ── Load credentials ───────────────────────────────────────────────────
    try:
        creds = get_speech_credentials()
    except RuntimeError as e:
        logger.error("Speech config missing: %s", e)
        raise HTTPException(status_code=503, detail=str(e))

    # ── Transcribe ─────────────────────────────────────────────────────────
    try:
        transcript = _transcribe(
            pcm_bytes=raw_bytes,
            subscription_key=creds["subscription_key"],
            region=creds["region"],
        )
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:
        logger.exception("Unexpected error during transcription")
        raise HTTPException(status_code=500, detail="Transcription failed")

    return JSONResponse({"transcript": transcript})


@router.get(
    "/health",
    summary="Check Speech service configuration",
    include_in_schema=False,
)
async def speech_health():
    """Quick check that the DB has an active Speech config row."""
    try:
        creds = get_speech_credentials()
        return {
            "status": "ok",
            "region": creds["region"],
            "key_prefix": creds["subscription_key"][:6] + "...",
        }
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))