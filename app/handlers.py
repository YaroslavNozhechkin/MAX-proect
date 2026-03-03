"""
Webhook handlers for processing incoming messages from GREEN-API (Max).
"""

import logging
from typing import Dict, Any, Optional
from pydantic import BaseModel

from app.config import settings
from app.telegram_client import telegram_client

logger = logging.getLogger(__name__)


# Pydantic models for GREEN-API webhook payloads
class MessageData(BaseModel):
    """Message data from GREEN-API webhook."""

    typeWebhook: str
    instanceData: Optional[Dict[str, Any]] = None
    timestamp: Optional[int] = None
    idMessage: Optional[str] = None
    senderData: Optional[Dict[str, Any]] = None
    messageData: Optional[Dict[str, Any]] = None


class WebhookHandler:
    """Handler for processing webhooks from GREEN-API."""

    def __init__(self):
        """Initialize webhook handler."""
        self.target_chat_id = settings.max_chat_id
        logger.info(f"Webhook handler initialized. Target chat: {self.target_chat_id or 'ALL'}")

    def should_process_message(self, chat_id: Optional[str]) -> bool:
        """
        Check if message from this chat should be processed.

        Args:
            chat_id: Chat ID from incoming message

        Returns:
            bool: True if message should be processed
        """
        # If no specific chat filter is set, process all messages
        if not self.target_chat_id:
            return True

        # Compare as strings (env is string, webhook can be int)
        return str(chat_id) == str(self.target_chat_id)

    async def handle_incoming_message(self, payload: Any) -> Dict[str, Any]:
        """
        Handle incoming webhook from GREEN-API.

        Args:
            payload: Webhook payload from GREEN-API

        Returns:
            Dict with status
        """
        try:
            # 0) Sometimes payload may be a batch (list of notifications)
            if isinstance(payload, list):
                results = []
                for item in payload:
                    results.append(await self.handle_incoming_message(item))
                return {"status": "batch", "results": results}

            if not isinstance(payload, dict):
                logger.warning(f"Unsupported payload type: {type(payload)}")
                return {"status": "ignored", "reason": "invalid_payload"}

            # 1) GREEN-API can send either:
            #    a) flat webhook: { "typeWebhook": "...", ... }
            #    b) wrapper: { "receiptId": ..., "body": { "typeWebhook": "...", ... } }
            notification = payload
            if isinstance(payload.get("body"), dict):
                notification = payload["body"]

            # Parse webhook type
            webhook_type = notification.get("typeWebhook")
            logger.info(f"Received webhook type: {webhook_type}")

            # 2) You need ONLY MAX -> Telegram, so accept ONLY incoming messages
            if webhook_type != "incomingMessageReceived":
                return {"status": "ignored", "reason": "not_incoming"}

            # Extract message data
            message_data = notification.get("messageData", {}) or {}
            sender_data = notification.get("senderData", {}) or {}

            # Get chat ID to check filter
            chat_id = sender_data.get("chatId") or sender_data.get("sender")
            if chat_id is not None:
                chat_id = str(chat_id)

            if not self.should_process_message(chat_id):
                logger.info(f"Skipping message from chat {chat_id} (not target chat)")
                return {"status": "ignored", "reason": "chat_filter"}

            # Extract sender info
            sender_name = sender_data.get("senderName") or sender_data.get("name")

            # For MAX sender may not be a phone number; try senderPhoneNumber first, fallback to sender
            sender_phone = sender_data.get("senderPhoneNumber")
            if sender_phone is None or str(sender_phone) == "0":
                sender_phone = sender_data.get("sender", "")
            sender_phone = str(sender_phone).replace("@c.us", "")

            # Process based on message type
            type_message = message_data.get("typeMessage")

            if type_message == "textMessage":
                await self._handle_text_message(message_data, sender_name, sender_phone)
            elif type_message == "extendedTextMessage":
                await self._handle_extended_text_message(message_data, sender_name, sender_phone)
            elif type_message == "imageMessage":
                await self._handle_image_message(message_data, sender_name, sender_phone)
            elif type_message == "videoMessage":
                await self._handle_video_message(message_data, sender_name, sender_phone)
            elif type_message == "documentMessage":
                await self._handle_document_message(message_data, sender_name, sender_phone)
            elif type_message == "audioMessage":
                await self._handle_audio_message(message_data, sender_name, sender_phone)
            elif type_message == "voiceMessage":
                await self._handle_voice_message(message_data, sender_name, sender_phone)
            else:
                logger.warning(f"Unsupported message type: {type_message}")
                return {"status": "unsupported", "type": str(type_message)}

            return {"status": "success"}

        except Exception as e:
            logger.error(f"Error handling webhook: {e}", exc_info=True)
            return {"status": "error", "message": str(e)}

    async def _handle_text_message(
        self, message_data: Dict[str, Any], sender_name: Optional[str], sender_phone: Optional[str]
    ):
        """Handle text message."""
        text = message_data.get("textMessageData", {}).get("textMessage", "")
        if text:
            await telegram_client.send_text_message(text, sender_name, sender_phone)

    async def _handle_extended_text_message(
        self, message_data: Dict[str, Any], sender_name: Optional[str], sender_phone: Optional[str]
    ):
        """Handle extended text message (outgoing messages from GREEN-API)."""
        text = message_data.get("extendedTextMessageData", {}).get("text", "")
        if text:
            await telegram_client.send_text_message(text, sender_name, sender_phone)

    async def _handle_image_message(
        self, message_data: Dict[str, Any], sender_name: Optional[str], sender_phone: Optional[str]
    ):
        """Handle image message."""
        image_data = message_data.get("fileMessageData") or message_data.get("downloadUrl")
        if isinstance(image_data, dict):
            image_url = image_data.get("downloadUrl")
            caption = message_data.get("caption")
        else:
            image_url = image_data
            caption = None

        if image_url:
            await telegram_client.send_photo(image_url, caption, sender_name, sender_phone)

    async def _handle_video_message(
        self, message_data: Dict[str, Any], sender_name: Optional[str], sender_phone: Optional[str]
    ):
        """Handle video message."""
        video_data = message_data.get("fileMessageData") or message_data.get("downloadUrl")
        if isinstance(video_data, dict):
            video_url = video_data.get("downloadUrl")
            caption = message_data.get("caption")
        else:
            video_url = video_data
            caption = None

        if video_url:
            await telegram_client.send_video(video_url, caption, sender_name, sender_phone)

    async def _handle_document_message(
        self, message_data: Dict[str, Any], sender_name: Optional[str], sender_phone: Optional[str]
    ):
        """Handle document message."""
        doc_data = message_data.get("fileMessageData") or {}
        document_url = doc_data.get("downloadUrl")
        filename = doc_data.get("fileName") or "document"
        caption = message_data.get("caption")

        if document_url:
            await telegram_client.send_document(
                document_url, filename, caption, sender_name, sender_phone
            )

    async def _handle_audio_message(
        self, message_data: Dict[str, Any], sender_name: Optional[str], sender_phone: Optional[str]
    ):
        """Handle audio message (treat as document)."""
        audio_data = message_data.get("fileMessageData") or {}
        audio_url = audio_data.get("downloadUrl")
        filename = audio_data.get("fileName") or "audio.mp3"

        if audio_url:
            await telegram_client.send_document(
                audio_url, filename, "🎵 Audio", sender_name, sender_phone
            )

    async def _handle_voice_message(
        self, message_data: Dict[str, Any], sender_name: Optional[str], sender_phone: Optional[str]
    ):
        """Handle voice message (treat as document)."""
        voice_data = message_data.get("fileMessageData") or {}
        voice_url = voice_data.get("downloadUrl")
        filename = voice_data.get("fileName") or "voice.ogg"

        if voice_url:
            await telegram_client.send_document(
                voice_url, filename, "🎤 Voice message", sender_name, sender_phone
            )


# Global webhook handler instance
webhook_handler = WebhookHandler()
