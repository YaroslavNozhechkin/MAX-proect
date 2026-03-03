"""
Webhook handlers for processing incoming messages from GREEN-API (Max).
"""

import json
import logging
from typing import Dict, Any, Optional, List, Union
from pydantic import BaseModel, Field  # Field может быть не нужен, оставляю для совместимости

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

    def _unwrap_notification(self, payload: Any) -> Dict[str, Any]:
        """
        GREEN-API can send:
          - flat webhook: { "typeWebhook": "...", ... }
          - wrapper: { "receiptId": ..., "body": { ... } }
          - wrapper with body as JSON string: { "receiptId": ..., "body": "{...}" }

        This function unwraps body up to a few levels defensively.
        """
        if not isinstance(payload, dict):
            return {}

        obj: Any = payload

        for _ in range(5):
            if isinstance(obj, dict) and "typeWebhook" in obj:
                return obj

            if not isinstance(obj, dict):
                break

            body = obj.get("body")

            if isinstance(body, str):
                # body may be JSON string
                try:
                    body = json.loads(body)
                except Exception:
                    break

            if isinstance(body, dict):
                obj = body
                continue

            break

        return obj if isinstance(obj, dict) else {}

    def _find_dict_with_key(self, obj: Any, key: str) -> Optional[Dict[str, Any]]:
        """
        Find the first dict in nested structures (dict/list) that contains `key`.
        """
        if isinstance(obj, dict):
            if key in obj:
                return obj
            for v in obj.values():
                found = self._find_dict_with_key(v, key)
                if found is not None:
                    return found
        elif isinstance(obj, list):
            for item in obj:
                found = self._find_dict_with_key(item, key)
                if found is not None:
                    return found
        return None

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
                results: List[Dict[str, Any]] = []
                for item in payload:
                    results.append(await self.handle_incoming_message(item))
                return {"status": "batch", "results": results}

            if not isinstance(payload, dict):
                logger.warning(f"Unsupported payload type: {type(payload)}")
                return {"status": "ignored", "reason": "invalid_payload"}

            # Диагностика структуры (без полного JSON)
            logger.info(f"Webhook top keys: {list(payload.keys())}")
            if "body" in payload:
                logger.info(f"Webhook has body, type: {type(payload.get('body')).__name__}")

            # 1) Unwrap common wrapper formats
            notification = self._unwrap_notification(payload)

            # 2) Determine webhook type robustly
            base = notification if isinstance(notification, dict) and "typeWebhook" in notification else None
            if base is None:
                base = self._find_dict_with_key(payload, "typeWebhook") or notification

            webhook_type = None
            if isinstance(base, dict):
                webhook_type = base.get("typeWebhook")

            logger.info(f"Получен тип вебхука: {webhook_type}")

            # 3) Вам нужно ТОЛЬКО MAX -> Telegram, значит берём только входящие сообщения
            if webhook_type != "incomingMessageReceived":
                return {"status": "ignored", "reason": "not_incoming"}

            # 4) Extract message data (from the same base dict that has typeWebhook)
            message_data = (base.get("messageData") if isinstance(base, dict) else None) or {}
            sender_data = (base.get("senderData") if isinstance(base, dict) else None) or {}

            # Defensive fallback: sometimes fields can be nested differently
            if not isinstance(message_data, dict):
                message_data = {}
            if not isinstance(sender_data, dict):
                sender_data = {}

            # 5) Chat filter
            chat_id = sender_data.get("chatId") or sender_data.get("sender")
            if chat_id is not None:
                chat_id = str(chat_id)

            if not self.should_process_message(chat_id):
                logger.info(f"Skipping message from chat {chat_id} (not target chat)")
                return {"status": "ignored", "reason": "chat_filter"}

            # 6) Sender info
            sender_name = sender_data.get("senderName") or sender_data.get("name")

            # For MAX sender may not be a phone number; try senderPhoneNumber first, fallback to sender
            sender_phone = sender_data.get("senderPhoneNumber")
            if sender_phone is None or str(sender_phone) == "0":
                sender_phone = sender_data.get("sender", "")
            sender_phone = str(sender_phone).replace("@c.us", "")

            # 7) Process based on message type
            type_message = message_data.get("typeMessage")

            if type_message == "textMessage":
                await self._handle_text_message(message_data, sender_name, sender_phone)
            elif type_message == "extendedTextMessage":
                # редко, но оставляем
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
        """Handle extended text message."""
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
