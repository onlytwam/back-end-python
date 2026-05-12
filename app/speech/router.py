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
    → post-processing correction pass (maps mishearings → correct brand names)
    → {"transcript": "show me TWAM black bras"}
    → Android fires onResult event → JS navigates to /global-search?q=...

No environment variables used — all credentials come from the database.
"""

import io
import re
import wave
import logging

from fastapi import APIRouter, UploadFile, File, HTTPException
from fastapi.responses import JSONResponse

try:
    import azure.cognitiveservices.speech as speechsdk
except ImportError:
    speechsdk = None  # type: ignore

from app.speech.repository import get_speech_credentials, build_phrase_hints

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/Speech", tags=["Speech"])


# ── Known mishearing corrections ───────────────────────────────────────────────
# Maps what Azure commonly returns → what the user actually said.
# Keys are lowercase (matching is case-insensitive).
# This catches cases where even with phrase hints Azure's acoustic model wins.
#
# Add new entries whenever logcat shows a wrong word for a known brand/product.
#
_WORD_CORRECTIONS: dict[str, str] = {
    # TWAM mishearings
    "tom":      "TWAM",
    "tram":     "TWAM",
    "team":     "TWAM",
    "trim":     "TWAM",
    "twam":     "TWAM",
    "thwam":    "TWAM",
    "teem":     "TWAM",
    "twee":     "TWAM",
    "twem":     "TWAM",

    # Amante mishearings
    "amanda":   "Amante",
    "amount":   "Amante",
    "armante":  "Amante",
    "amantay":  "Amante",

    # Enamor mishearings
    "enamour":  "Enamor",
    "inamore":  "Enamor",

    # Soie mishearings
    "soy":      "Soie",
    "soya":     "Soie",
    "swah":     "Soie",
    "swoi":     "Soie",
    "swoy":     "Soie",

    # Blossoms mishearings
    "blossem":  "Blossoms",
    "blossom":  "Blossoms",

    # Triumph mishearings
    "tri":      "Triumph",

    # Bra mishearings — the most common one
    "bro":      "bra",
    "braw":     "bra",
    "brus":     "bras",
    "broz":     "bras",
    "braz":     "bras",

    # Panty mishearings
    "pantie":   "panty",
    "panties":  "panties",

    # Balconette mishearings
    "balcony":  "balconette",

    # Bandeau mishearings
    "bando":    "bandeau",
    "bandu":    "bandeau",

    # Camisole mishearings
    "camise":   "camisole",
    "cami":     "camisole",

    # Color mishearings
    "bej":      "beige",
    "choco":    "chocolate",
}

# Multi-word phrase corrections (checked before word-level)
_PHRASE_CORRECTIONS: list[tuple[str, str]] = [
    # (lowercase pattern, replacement)
    ("tee wam",     "TWAM"),
    ("t wam",       "TWAM"),
    ("twee am",     "TWAM"),
    ("a mante",     "Amante"),
    ("ah mante",    "Amante"),
    ("in amor",     "Enamor"),
    ("en amor",     "Enamor"),
    ("in a more",   "Enamor"),
    ("so way",      "Soie"),
    ("try umph",    "Triumph"),
    ("tri umph",    "Triumph"),
    ("t shirt bra", "T-shirt bra"),
    ("tee shirt bra","T-shirt bra"),
    ("push up bra", "push-up bra"),
    ("non wired",   "non-wired"),
    ("non padded",  "non-padded"),
    ("wire free",   "wireless"),
]


def _correct_transcript(transcript: str) -> str:
    """
    Two-pass post-processing correction:
      Pass 1: Multi-word phrase replacements (case-insensitive)
      Pass 2: Word-level replacements (case-insensitive, strips punctuation)
    """
    if not transcript:
        return transcript

    # Pass 1 — phrase level
    result = transcript
    lower = result.lower()
    for wrong, right in _PHRASE_CORRECTIONS:
        if wrong in lower:
            # Replace case-insensitively, preserving surrounding text
            pattern = re.compile(re.escape(wrong), re.IGNORECASE)
            result = pattern.sub(right, result)
            lower = result.lower()

    # Pass 2 — word level
    words = result.split()
    corrected = []
    for word in words:
        m = re.match(r"^([a-zA-Z''\-]+)([.,!?]*)$", word)
        if m:
            core, punct = m.group(1), m.group(2)
            replacement = _WORD_CORRECTIONS.get(core.lower())
            corrected.append((replacement if replacement else core) + punct)
        else:
            corrected.append(word)

    return " ".join(corrected)


# ── Helpers ────────────────────────────────────────────────────────────────────

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
    Returns the raw recognised transcript string.
    """
    speech_config = speechsdk.SpeechConfig(
        subscription=subscription_key,
        region=region
    )
    speech_config.speech_recognition_language = "en-IN"

    stream_format = speechsdk.audio.AudioStreamFormat(
        samples_per_second=16000,
        bits_per_sample=16,
        channels=1
    )
    push_stream  = speechsdk.audio.PushAudioInputStream(stream_format)
    audio_config = speechsdk.audio.AudioConfig(stream=push_stream)

    recognizer = speechsdk.SpeechRecognizer(
        speech_config=speech_config,
        audio_config=audio_config
    )

    # Boost all TWAM vocabulary — brands repeated 3×, categories 2× in repository
    phrase_list = speechsdk.PhraseListGrammar.from_recognizer(recognizer)
    for phrase in build_phrase_hints():
        phrase_list.addPhrase(phrase)

    push_stream.write(pcm_bytes)
    push_stream.close()

    result = recognizer.recognize_once_async().get()

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


# ── Endpoints ──────────────────────────────────────────────────────────────────

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
    Receive raw PCM audio from the Android APK and return a corrected transcript.
    """
    raw_bytes = await audio.read()

    if len(raw_bytes) < 3200:
        raise HTTPException(
            status_code=400,
            detail="Audio too short — minimum 0.1 seconds required"
        )

    if len(raw_bytes) > 10 * 16000 * 2:
        raw_bytes = raw_bytes[:10 * 16000 * 2]

    try:
        creds = get_speech_credentials()
    except RuntimeError as e:
        logger.error("Speech config missing: %s", e)
        raise HTTPException(status_code=503, detail=str(e))

    try:
        raw_transcript = _transcribe(
            pcm_bytes=raw_bytes,
            subscription_key=creds["subscription_key"],
            region=creds["region"],
        )
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception:
        logger.exception("Unexpected error during transcription")
        raise HTTPException(status_code=500, detail="Transcription failed")

    # ── Post-processing: fix brand/product mishearings ─────────────────────
    transcript = _correct_transcript(raw_transcript)

    if raw_transcript != transcript:
        logger.info(
            "Transcript corrected: %r → %r", raw_transcript, transcript
        )

    return JSONResponse({"transcript": transcript})


@router.get(
    "/health",
    summary="Check Speech service configuration",
    include_in_schema=False,
)
async def speech_health():
    try:
        creds = get_speech_credentials()
        return {
            "status": "ok",
            "region": creds["region"],
            "key_prefix": creds["subscription_key"][:6] + "...",
        }
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.get("/debug")
async def speech_debug():
    """Test each layer independently."""
    results = {}

    try:
        creds = get_speech_credentials()
        results["db_credentials"] = {
            "status": "OK",
            "region": creds["region"],
            "key_prefix": creds["subscription_key"][:8] + "...",
        }
    except Exception as e:
        results["db_credentials"] = {"status": "FAIL", "error": str(e)}
        return results

    try:
        speechsdk.SpeechConfig(
            subscription=creds["subscription_key"],
            region=creds["region"]
        )
        results["azure_config"] = {"status": "OK"}
    except Exception as e:
        results["azure_config"] = {"status": "FAIL", "error": str(e)}
        return results

    try:
        hints = build_phrase_hints()
        results["phrase_hints"] = {
            "status": "OK",
            "count": len(hints),
            "sample": hints[:5],
        }
    except Exception as e:
        results["phrase_hints"] = {"status": "FAIL", "error": str(e)}

    return results