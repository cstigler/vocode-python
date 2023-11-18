import asyncio
import json
import logging
from typing import Optional
import audioop
from urllib.parse import urlencode
from vocode import getenv
from deepgram import Deepgram

from vocode.streaming.transcriber.base_transcriber import (
    BaseAsyncTranscriber,
    Transcription,
    meter,
)
from vocode.streaming.models.transcriber import (
    DeepgramTranscriberConfig,
    EndpointingConfig,
    EndpointingType,
    PunctuationEndpointingConfig,
    TimeEndpointingConfig,
)
from vocode.streaming.models.audio_encoding import AudioEncoding
from websockets.exceptions import ConnectionClosedError 


PUNCTUATION_TERMINATORS = [".", "!", "?"]
NUM_RESTARTS = 5


avg_latency_hist = meter.create_histogram(
    name="transcriber.deepgram.avg_latency",
    unit="seconds",
)
max_latency_hist = meter.create_histogram(
    name="transcriber.deepgram.max_latency",
    unit="seconds",
)
min_latency_hist = meter.create_histogram(
    name="transcriber.deepgram.min_latency",
    unit="seconds",
)
duration_hist = meter.create_histogram(
    name="transcriber.deepgram.duration",
    unit="seconds",
)


class DeepgramTranscriber(BaseAsyncTranscriber[DeepgramTranscriberConfig]):
    def __init__(
        self,
        transcriber_config: DeepgramTranscriberConfig,
        api_key: Optional[str] = None,
        logger: Optional[logging.Logger] = None,
    ):
        super().__init__(transcriber_config)
        self.api_key = api_key or getenv("DEEPGRAM_API_KEY")
        if not self.api_key:
            raise Exception(
                "Please set DEEPGRAM_API_KEY environment variable or pass it as a parameter"
            )
        self._ended = False
        self.is_ready = False
        self.logger = logger or logging.getLogger(__name__)
        self.audio_cursor = 0.0
        self._speaking_signal_is_active = False
        self.deepgram = Deepgram(self.api_key)
        self.transcript_buffer = ""
    
    @property
    def speaking_signal_is_active(self):
        return self._speaking_signal_is_active

    @speaking_signal_is_active.setter
    def speaking_signal_is_active(self, value):
        self._speaking_signal_is_active = value


    async def _run_loop(self):
        restarts = 0
        while not self._ended and restarts < NUM_RESTARTS:
            await self.process()
            restarts += 1
            self.logger.debug(
                "Deepgram connection died, restarting, num_restarts: %s", restarts
            )

    def send_audio(self, chunk):
        if (
            self.transcriber_config.downsampling
            and self.transcriber_config.audio_encoding == AudioEncoding.LINEAR16
        ):
            chunk, _ = audioop.ratecv(
                chunk,
                2,
                1,
                self.transcriber_config.sampling_rate
                * self.transcriber_config.downsampling,
                self.transcriber_config.sampling_rate,
                None,
            )
        super().send_audio(chunk)

    def terminate(self):
        terminate_msg = json.dumps({"type": "CloseStream"})
        self.input_queue.put_nowait(terminate_msg)
        self._ended = True
        super().terminate()

    def is_speech_final(
        self, current_buffer: str, deepgram_response: dict, time_silent: float
    ):
        transcript = deepgram_response["channel"]["alternatives"][0]["transcript"]

        # if it is not time based, then return true if speech is final and there is a transcript
        if not self.transcriber_config.endpointing_config:
            return transcript and deepgram_response["speech_final"]
        elif isinstance(
            self.transcriber_config.endpointing_config, TimeEndpointingConfig
        ):
            # if it is time based, then return true if there is no transcript
            # and there is some speech to send
            # and the time_silent is greater than the cutoff
            return (
                not transcript
                and current_buffer
                and (time_silent + deepgram_response["duration"])
                > self.transcriber_config.endpointing_config.time_cutoff_seconds
            )
        elif isinstance(
            self.transcriber_config.endpointing_config, PunctuationEndpointingConfig
        ):
            return (
                transcript
                and deepgram_response["speech_final"]
                and transcript.strip()[-1] in PUNCTUATION_TERMINATORS
            ) or (
                not transcript
                and current_buffer
                and (time_silent + deepgram_response["duration"])
                > self.transcriber_config.endpointing_config.time_cutoff_seconds
            )
        raise Exception("Endpointing config not supported")

    def calculate_time_silent(self, data: dict):
        end = data["start"] + data["duration"]
        words = data["channel"]["alternatives"][0]["words"]
        if words:
            return end - words[-1]["end"]
        return data["duration"]

    async def process(self):
        self.audio_cursor = 0.0
        try:
            deepgramLive = await self.deepgram.transcription.live(
                {
                    "punctuate": True,
                    "interim_results": True,
                    "language": self.transcriber_config.language,
                    "model": self.transcriber_config.model,
                    "encoding": self.transcriber_config.audio_encoding.value,
                    "sample_rate": self.transcriber_config.sampling_rate,
                    "endpointing": self.transcriber_config.endpointing,
                    "channels": 1,
                }
            )
        except Exception as e:
            self.logger.debug(f"Could not open socket: {e}")
            return

        deepgramLive.registerHandler(
            deepgramLive.event.CLOSE,
            lambda c: self.logger.debug(f"Connection closed with code {c}."),
        )
        deepgramLive.registerHandler(
            deepgramLive.event.TRANSCRIPT_RECEIVED, self.handle_transcript
        )

        async def sender():  # sends audio to websocket
            while not self._ended:
                try:
                    data = await asyncio.wait_for(self.input_queue.get(), 5)
                except asyncio.exceptions.TimeoutError as e:
                    self.logger.error(f"Transcriber timeout error: {e}")
                    break
                num_channels = 1
                sample_width = 2
                self.audio_cursor += len(data) / (
                    self.transcriber_config.sampling_rate * num_channels * sample_width
                )
                deepgramLive.send(data)
            self.logger.debug("Terminating Deepgram transcriber sender")

        await asyncio.gather(sender())
        await deepgramLive.finish()

    def handle_transcript(self, data):
        self.logger.debug(f"Received transcription data from Deepgram: {data}")
        if not "is_final" in data:  # means we've finished receiving transcriptions
            self.logger.debug(
                f" --> is_final not present, so we're finished receiving transcriptions"
            )
            return
        if "channel" not in data:  # check if 'channel' key is in data
            self.logger.error(
                f"Unexpected data from Deepgram, 'channel' key missing: {data}"
            )
            return
        channel_data = data["channel"]  # get the channel object
        if (
            "alternatives" not in channel_data
        ):  # check if 'alternatives' key is in channel_data
            self.logger.error(
                f"Unexpected channel data from Deepgram, 'alternatives' key missing: {channel_data}"
            )
            return
        if (
            "start" not in data or "duration" not in data
        ):  # check if 'start' and 'duration' keys are in data
            self.logger.error(
                f"Unexpected data from Deepgram, 'start' or 'duration' key missing: {data}"
            )
            return

        start = data["start"]  # get the start value from data
        duration = data["duration"]  # get the duration value from data
        cur_max_latency = self.audio_cursor - start
        transcript_cursor = start + duration
        cur_min_latency = self.audio_cursor - transcript_cursor

        avg_latency_hist.record((cur_min_latency + cur_max_latency) / 2 * duration)
        duration_hist.record(duration)

        # Log max and min latencies
        max_latency_hist.record(cur_max_latency)
        min_latency_hist.record(max(cur_min_latency, 0))

        is_final = data["is_final"]
        top_choice = channel_data["alternatives"][0]
        confidence = top_choice["confidence"]

        if top_choice["transcript"] and confidence > 0.0:
            current_transcript = top_choice["transcript"]
            time_silent = self.calculate_time_silent(data)
            speech_final = self.is_speech_final(current_transcript, data, time_silent)

            duration = data["duration"]
            start_time = data["start"]
            self.logger.debug(
                f"Current Transcript: {current_transcript}, Time Silent: {time_silent}, Speech Final: {speech_final}, Is Final: {is_final} for Start Time: {start_time} and Duration {duration}"
            )

            if is_final:
                # Add a space before the new transcript if the buffer is not empty
                separator = " " if self.transcript_buffer else ""
                self.transcript_buffer += (
                    separator + current_transcript
                )  # concatenate to the buffer
            if speech_final:
                self.output_queue.put_nowait(
                    Transcription(
                        message=self.transcript_buffer,
                        confidence=confidence,
                        is_final=is_final,
                    )
                )
                self.transcript_buffer = ""  # reset the buffer
        else:
            self.logger.debug(f"No transcript with any confidence, discarding!")            