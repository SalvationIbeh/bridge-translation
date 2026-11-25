"""
Queue Manager — owns all asyncio queues and spawns worker coroutines.
Called once at server startup.
"""
import asyncio
import logging

import config
from pipeline.stt_worker       import stt_worker
from pipeline.translate_worker  import translate_worker
from pipeline.tts_worker        import tts_worker

logger = logging.getLogger(__name__)


class QueueManager:
    def __init__(self, metrics):
        self.metrics = metrics

        # The pipeline queues
        self.stt_queue       = asyncio.Queue()
        self.translate_queue = asyncio.Queue()
        self.tts_queue       = asyncio.Queue()
        self.result_queue    = asyncio.Queue()

        self._tasks: list[asyncio.Task] = []

    async def start(self):
        """Spawn all worker pools. Called from FastAPI lifespan."""
        logger.info("Starting worker pools...")

        for i in range(config.STT_WORKERS):
            t = asyncio.create_task(
                stt_worker(i, self.stt_queue, self.translate_queue, self.metrics),
                name=f"stt-worker-{i}",
            )
            self._tasks.append(t)

        for i in range(config.TRANSLATE_WORKERS):
            t = asyncio.create_task(
                translate_worker(i, self.translate_queue, self.tts_queue, self.metrics),
                name=f"translate-worker-{i}",
            )
            self._tasks.append(t)

        for i in range(config.TTS_WORKERS):
            t = asyncio.create_task(
                tts_worker(i, self.tts_queue, self.result_queue, self.metrics),
                name=f"tts-worker-{i}",
            )
            self._tasks.append(t)

        logger.info(
            f"Worker pools started: "
            f"{config.STT_WORKERS} STT, "
            f"{config.TRANSLATE_WORKERS} translate, "
            f"{config.TTS_WORKERS} TTS"
        )

    async def stop(self):
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        logger.info("Worker pools stopped.")

    def queue_depths(self) -> dict:
        return {
            "stt":       self.stt_queue.qsize(),
            "translate": self.translate_queue.qsize(),
            "tts":       self.tts_queue.qsize(),
            "result":    self.result_queue.qsize(),
        }
