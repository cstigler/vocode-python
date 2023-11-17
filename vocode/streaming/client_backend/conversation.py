import logging
from typing import Callable, Optional
import typing

from dataclasses import dataclass
from fastapi import APIRouter, WebSocket
from vocode.streaming.agent.base_agent import BaseAgent
from vocode.streaming.models.audio_encoding import AudioEncoding
from vocode.streaming.models.client_backend import InputAudioConfig, OutputAudioConfig
from vocode.streaming.models.synthesizer import AzureSynthesizerConfig
from vocode.streaming.models.transcriber import (
    DeepgramTranscriberConfig,
    PunctuationEndpointingConfig,
    TimeEndpointingConfig
)
from vocode.streaming.models.websocket import (
    AudioConfigStartMessage,
    AudioMessage,
    ReadyMessage,
    SpeakingSignalMessage,
    WebSocketMessage,
    WebSocketMessageType,
)

from vocode.streaming.output_device.websocket_output_device import WebsocketOutputDevice
from vocode.streaming.streaming_conversation import StreamingConversation
from vocode.streaming.synthesizer.azure_synthesizer import AzureSynthesizer
from vocode.streaming.synthesizer.base_synthesizer import BaseSynthesizer
from vocode.streaming.transcriber.base_transcriber import BaseTranscriber
from vocode.streaming.transcriber.deepgram_transcriber import DeepgramTranscriber
from vocode.streaming.utils.base_router import BaseRouter

from vocode.streaming.models.events import Event, EventType
from vocode.streaming.models.transcript import TranscriptEvent
from vocode.streaming.utils import events_manager

BASE_CONVERSATION_ENDPOINT = "/conversation"


@dataclass
class ConversationParams:
    human_name: str
    agent_name: str
    company_name: str
    situation: str

    @staticmethod
    def get_conversation_params(websocket):
        query_params = websocket.query_params
        return ConversationParams(
            human_name=query_params.get('humanName'),
            agent_name=query_params.get('agentName'),
            company_name=query_params.get('companyName'),
            situation=query_params.get('situation')
        )


class ConversationRouter(BaseRouter):
    def __init__(
        self,
        agent_thunk: Callable[[], BaseAgent],
        transcriber_thunk: Callable[
            [InputAudioConfig], BaseTranscriber
        ] = lambda input_audio_config, logger: DeepgramTranscriber(
            transcriber_config=DeepgramTranscriberConfig.from_input_audio_config(
                input_audio_config=input_audio_config,
                endpointing_config=PunctuationEndpointingConfig(),
                # mute_during_speech = True,
            ),
            logger=logger
        ),
        synthesizer_thunk: Callable[
            [OutputAudioConfig], BaseSynthesizer
        ] = lambda output_audio_config: AzureSynthesizer(
            AzureSynthesizerConfig.from_output_audio_config(
                output_audio_config=output_audio_config
            )
        ),
        logger: Optional[logging.Logger] = None,
        conversation_endpoint: str = BASE_CONVERSATION_ENDPOINT,
    ):
        super().__init__()
        self.transcriber_thunk = transcriber_thunk
        self.agent_thunk = agent_thunk
        self.synthesizer_thunk = synthesizer_thunk
        self.logger = logger or logging.getLogger(__name__)
        self.router = APIRouter()
        self.router.websocket(conversation_endpoint)(self.conversation)

    def get_conversation(
        self,
        websocket: WebSocket,
        output_device: WebsocketOutputDevice,
        start_message: AudioConfigStartMessage,
    ) -> StreamingConversation:
        transcriber = self.transcriber_thunk(
            input_audio_config = start_message.input_audio_config, 
            logger = self.logger)
        synthesizer = self.synthesizer_thunk(start_message.output_audio_config)
        synthesizer.synthesizer_config.should_encode_as_wav = True

        conversation_params = ConversationParams.get_conversation_params(
            websocket)

        self.logger.debug(conversation_params)

        return StreamingConversation(
            output_device=output_device,
            transcriber=transcriber,
            agent=self.agent_thunk(conversation_params),
            synthesizer=synthesizer,
            conversation_id=start_message.conversation_id,
            events_manager=TranscriptEventManager(output_device, self.logger)
            if start_message.subscribe_transcript
            else None,
            logger=self.logger,
        )

    async def conversation(self, websocket: WebSocket):
        await websocket.accept()
        start_message: AudioConfigStartMessage = AudioConfigStartMessage.parse_obj(
            await websocket.receive_json()
        )
        self.logger.debug(f"Conversation started")
        output_device = WebsocketOutputDevice(
            websocket,
            start_message.output_audio_config.sampling_rate,
            start_message.output_audio_config.audio_encoding,
        )
        conversation = self.get_conversation(
            websocket, output_device, start_message)
        await conversation.start(lambda: websocket.send_text(ReadyMessage().json()))
        while conversation.is_active():
            message: WebSocketMessage = WebSocketMessage.parse_obj(
                await websocket.receive_json()
            )
            if message.type == WebSocketMessageType.STOP:
                break
            elif message.type == WebSocketMessageType.SPEAKING_SIGNAL_CHANGE:
                speaking_signal = typing.cast(SpeakingSignalMessage, message)
                self.logger.debug(
                    f"Conversation.py: speaking signal received as {speaking_signal.is_active}")
                conversation.speaking_signal_is_active = speaking_signal.is_active
                self.logger.debug(
                    f"Conversation.py: Conversation.speaking_signal_is_active set to {conversation.speaking_signal_is_active}")
            else:
                audio_message = typing.cast(AudioMessage, message)
                conversation.receive_audio(audio_message.get_bytes())
        output_device.mark_closed()
        await conversation.terminate()

    def get_router(self) -> APIRouter:
        return self.router


class TranscriptEventManager(events_manager.EventsManager):
    def __init__(
        self,
        output_device: WebsocketOutputDevice,
        logger: Optional[logging.Logger] = None,
    ):
        super().__init__(subscriptions=[EventType.TRANSCRIPT])
        self.output_device = output_device
        self.logger = logger or logging.getLogger(__name__)

    async def handle_event(self, event: Event):
        if event.type == EventType.TRANSCRIPT:
            transcript_event = typing.cast(TranscriptEvent, event)
            self.output_device.consume_transcript(transcript_event)
            # self.logger.debug(event.dict())

    def restart(self, output_device: WebsocketOutputDevice):
        self.output_device = output_device
