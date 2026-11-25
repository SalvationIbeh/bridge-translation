"""
Translation Workers — Opus-MT pool draining the translation queue.
Supports multiple language pairs with LRU eviction when VRAM is constrained.

IMPORTANT: Only Apache 2.0 / MIT licensed models (Opus-MT, MADLAD-400).
NLLB-200 (CC-BY-NC) is explicitly excluded.
"""
import asyncio
import logging
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

import config
from pipeline.job import Job

logger = logging.getLogger(__name__)

_executor = ThreadPoolExecutor(max_workers=config.TRANSLATE_WORKERS, thread_name_prefix="translate")

# Opus-MT model IDs for supported language pairs.
# Key: "src-tgt", Value: HuggingFace model ID
OPUS_MT_MODELS = {
    "en-es": "Helsinki-NLP/opus-mt-en-es",
    "es-en": "Helsinki-NLP/opus-mt-es-en",
    "en-fr": "Helsinki-NLP/opus-mt-en-fr",
    "fr-en": "Helsinki-NLP/opus-mt-fr-en",
    "en-de": "Helsinki-NLP/opus-mt-en-de",
    "de-en": "Helsinki-NLP/opus-mt-de-en",
    "en-pt": "Helsinki-NLP/opus-mt-tc-big-en-pt",
    "pt-en": "Helsinki-NLP/opus-mt-tc-big-pt-en",
    "en-zh": "Helsinki-NLP/opus-mt-en-zh",
    "zh-en": "Helsinki-NLP/opus-mt-zh-en",
    "en-ar": "Helsinki-NLP/opus-mt-en-ar",
    "ar-en": "Helsinki-NLP/opus-mt-ar-en",
    "en-ja": "Helsinki-NLP/opus-mt-en-jap",
    "ja-en": "Helsinki-NLP/opus-mt-jap-en",
    "es-fr": "Helsinki-NLP/opus-mt-es-fr",
    "fr-es": "Helsinki-NLP/opus-mt-fr-es",
}

MAX_LOADED_PAIRS = 6    # Keep at most 6 language pairs in memory simultaneously


class ModelCache:
    """
    LRU cache of loaded Opus-MT model instances.
    Evicts least-recently-used pair when MAX_LOADED_PAIRS is reached.
    Thread-safe via asyncio lock.
    """

    def __init__(self):
        self._cache: OrderedDict = OrderedDict()   # pair_key → (tokenizer, model)
        self._lock  = asyncio.Lock()
        self._active: set[str] = set()    # Pairs currently serving a session — never evict

    async def get(self, src: str, tgt: str):
        key = f"{src}-{tgt}"
        async with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)   # Mark as recently used
                return self._cache[key]
            return None

    async def load(self, src: str, tgt: str, loop):
        key = f"{src}-{tgt}"
        model_id = OPUS_MT_MODELS.get(key)
        if not model_id:
            raise ValueError(f"Unsupported language pair: {key}")

        async with self._lock:
            # Evict LRU if at capacity (only non-active pairs)
            while len(self._cache) >= MAX_LOADED_PAIRS:
                evict_key = next(
                    (k for k in self._cache if k not in self._active),
                    None
                )
                if evict_key is None:
                    break   # All loaded pairs are active — can't evict
                logger.info(f"Evicting translation model: {evict_key}")
                del self._cache[evict_key]

            logger.info(f"Loading translation model: {model_id}")
            pair = await loop.run_in_executor(_executor, _load_model, model_id)
            self._cache[key] = pair
            self._cache.move_to_end(key)
            logger.info(f"Translation model loaded: {key}")
            return pair

    def pin(self, src: str, tgt: str):
        self._active.add(f"{src}-{tgt}")

    def unpin(self, src: str, tgt: str):
        self._active.discard(f"{src}-{tgt}")


def _load_model(model_id: str):
    from transformers import MarianMTModel, MarianTokenizer
    device = config.TRANSLATE_DEVICE
    tokenizer = MarianTokenizer.from_pretrained(model_id, cache_dir=config.MODELS_DIR)
    model     = MarianMTModel.from_pretrained(model_id, cache_dir=config.MODELS_DIR).to(device)
    model.eval()
    return tokenizer, model


def _translate(tokenizer, model, text: str) -> str:
    import torch
    device = config.TRANSLATE_DEVICE
    inputs = tokenizer([text], return_tensors="pt", padding=True).to(device)
    with torch.no_grad():
        outputs = model.generate(**inputs)
    return tokenizer.decode(outputs[0], skip_special_tokens=True)


# Module-level shared cache — all workers share it
model_cache = ModelCache()


async def translate_worker(
    worker_id: int,
    translate_queue: asyncio.Queue,
    tts_queue: asyncio.Queue,
    metrics,
):
    """
    Long-running coroutine. Pulls jobs from translate_queue, translates,
    pushes to tts_queue.
    """
    loop = asyncio.get_event_loop()
    logger.info(f"Translation worker {worker_id} started.")

    while True:
        job: Job = await translate_queue.get()
        job.translate_start = time.monotonic()

        try:
            # Same-language passthrough — no translation needed
            if job.source_lang == job.target_lang:
                job.translated    = job.transcript
                job.translate_end = time.monotonic()
                await tts_queue.put(job)
                metrics.record_translate(job)
                continue

            # Ensure model is loaded (loads on first use per language pair)
            pair = await model_cache.get(job.source_lang, job.target_lang)
            if pair is None:
                pair = await model_cache.load(job.source_lang, job.target_lang, loop)

            tokenizer, model = pair
            translated = await loop.run_in_executor(
                _executor, _translate, tokenizer, model, job.transcript
            )

            job.translated    = translated
            job.translate_end = time.monotonic()

            await tts_queue.put(job)
            logger.debug(
                f"[{job.session_id[:8]}] Translate done in "
                f"{(job.translate_end - job.translate_start)*1000:.0f}ms ({len(translated)} chars)"
            )
            metrics.record_translate(job)

        except ValueError as e:
            logger.error(f"[{job.session_id[:8]}] Unsupported language pair: {e}")
            metrics.record_error("translate", job.session_id)
        except Exception as e:
            logger.error(f"[{job.session_id[:8]}] Translation error: {e}", exc_info=True)
            metrics.record_error("translate", job.session_id)
        finally:
            translate_queue.task_done()
