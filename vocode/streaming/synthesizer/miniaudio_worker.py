from typing import Optional, Tuple
import asyncio
import miniaudio

from vocode.streaming.models.synthesizer import SynthesizerConfig
from vocode.streaming.utils import convert_wav
from vocode.streaming.utils.mp3_helper import decode_mp3
from vocode.streaming.utils.worker import ThreadAsyncWorker, logger


class MiniaudioWorker(ThreadAsyncWorker[Tuple[Optional[bytes], bool]]):
    def __init__(
        self,
        synthesizer_config: SynthesizerConfig,
        input_queue: asyncio.Queue[Tuple[Optional[bytes], bool]],
        output_queue: asyncio.Queue[Tuple[Optional[bytes], bool]],
    ) -> None:
        super().__init__(input_queue, output_queue)
        self.synthesizer_config = synthesizer_config

    def _run_loop(self):
        while True:
            # Get a tuple of (mp3_chunk, is_last) from the input queue
            mp3_chunk, is_last = self.input_janus_queue.sync_q.get()
            if mp3_chunk is None:
                self.output_janus_queue.sync_q.put((None, True))
                continue
            try:
                output_bytes_io = decode_mp3(
                    mp3_chunk,
                )
            except miniaudio.DecodeError as e:
                # How should I log this
                logger.exception("MiniaudioWorker error: " + str(e), exc_info=True)
                self.output_janus_queue.sync_q.put((None, True))
                continue
            output_bytes_io = convert_wav(
                output_bytes_io,
                output_sample_rate=self.synthesizer_config.sampling_rate,
                output_encoding=self.synthesizer_config.audio_encoding,
            )
            # Put a tuple of (wav_chunk, is_last) in the output queue
            self.output_janus_queue.sync_q.put((output_bytes_io, is_last))
