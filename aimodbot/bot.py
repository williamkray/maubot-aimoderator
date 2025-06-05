# aimodbot - a maubot plugin to moderate messages and files in rooms using AI.

from typing import Type
import json
import base64
import asyncio

from mautrix.client import Client, InternalEventType, MembershipEventDispatcher, SyncStream
from mautrix.types import (Event, StateEvent, UserID, EventType,
                            MediaMessageEventContent, MessageEvent, RoomID, MessageType)
from mautrix.errors import MNotFound
from mautrix.util.config import BaseProxyConfig, ConfigUpdateHelper
from maubot import Plugin
from maubot.handlers import event


class Config(BaseProxyConfig):
    def do_update(self, helper: ConfigUpdateHelper) -> None:
        helper.copy("admins")
        helper.copy("uncensor_pl")
        helper.copy("moderate_files")
        helper.copy("ai_mod_threshold")
        helper.copy("ai_mod_api_key")
        helper.copy("ai_mod_api_endpoint")
        helper.copy("ai_mod_api_model")
        helper.copy("enable_join_notice")
        helper.copy("custom_notice_text")
        helper.copy("allowed_msgtypes")
        helper.copy("allowed_mimetypes")
        helper.copy("enable_msgtype_filter")


class AIModerator(Plugin):
    # List of phrases that indicate the AI is refusing to help
    REFUSAL_PHRASES = [
        "can't assist",
        "unable to assist", 
        "can't help",
        "unable to help",
        "i'm unable to",
        "i can't"
    ]

    async def start(self) -> None:
        await super().start()
        self.config.load_and_update()
        self.client.add_dispatcher(MembershipEventDispatcher)

    async def stop(self) -> None:
        await super().stop()

    async def ai_analyze(self, msg) -> None:
        # 使用配置快照
        sys_prompt = """
You are a content moderation engine. It is critical that you consistently respond with valid JSON.
assess the included message content and identify whether it is a potential scam or spam message,
or is otherwise inappropriate content. rate the message based
on offensive or vitriolic content, inclusion of questionable links, etc. return ONLY the following json format:

{
  "categories: {
      "sexual": int,
      "harassment": int,
      "self-harm": int,
      "violence": int,
      "hate": int,
      "scam": int
    }
  "max": int,
  "analysis": string,
  "comment": string
}

all integers are on a scale between 0-10.
"max" should be equal to the value of the highest-rated category. the "comment" string should be concise summaries with
score included, such as "likely scam (8)" or "offensive content (9)". "analysis" should be one or two brief sentences
that explain how the score was reached. It is imperative that you return a response in this exact format for the
programmatic content moderation system to work.
        """
        
        # Prepare the content based on message type
        if isinstance(msg.content, MediaMessageEventContent):
            if not self.config["moderate_files"]:
                return None
                
        # Download and encode the file
        if msg.content.msgtype in (MessageType.IMAGE, MessageType.VIDEO, MessageType.STICKER):
            try:
                data = await self.client.download_media(msg.content.url)
                mime_type = msg.content.info.mimetype
                base64_data = base64.b64encode(data).decode('utf-8')
            
                # Prepare content for OpenAI API
                content = [
                    {
                        "type": "text",
                        "text": "Analyze this image and return the resulting JSON of its scores:"
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{mime_type};base64,{base64_data}"
                        }
                    }
                ]
            except Exception as e:
                self.log.error(f"Failed to process media: {e}")
                return None
        else:
            content = msg.content.body

        context = [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": content},
        ]
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.config['ai_mod_api_key']}",
        }
        # 使用配置快照
        data = {"model": self.config["ai_mod_api_model"], "messages": context}

        max_retries = 3
        retries = 0
        for attempt in range(max_retries):
            async with self.http.post(
                self.config["ai_mod_api_endpoint"], headers=headers, data=json.dumps(data)
            ) as response:
                if response.status != 200:
                    return f"Error: {await response.text()}"
                response_json = await response.json()
                resp_content = response_json["choices"][0]["message"]["content"]
                #self.log.debug(f"DEBUG response content: {resp_content}")
                try:
                    rating_json = json.loads(resp_content)
                    return rating_json
                except json.JSONDecodeError as e:
                    if any(phrase in resp_content.lower() for phrase in self.REFUSAL_PHRASES):
                        self.log.debug("LLM indicated content blocking - treating as high risk content")
                        return {"max": 10, 
                            "comment": "LLM indicated content blocking", 
                            "analysis": resp_content, 
                            "categories": {"unsafe": 10}
                            }
                    else:
                        self.log.error(f"Attempt {attempt + 1}: {e}. trying again...")

        self.log.error("Failed to parse JSON after multiple retries.")
        return None

    def flag_score(self, rating):
        if rating["max"] >= self.config["ai_mod_threshold"]:
            return True

    async def check_bot_permissions(self, room_id: str, evt: MessageEvent = None, required_permissions: list[str] = None) -> tuple[bool, str, dict]:
        """Check if the bot has necessary permissions in a room.
        
        Args:
            room_id: The ID of the room to check permissions in
            evt: Optional MessageEvent for progress updates
            required_permissions: List of specific permissions to check. If None, checks basic room access.
            
        Returns:
            tuple: (bool, str, dict) - (has_permissions, error_message, permission_details)
        """
        try:
            # Check if bot is in the room
            try:
                await self.client.get_state_event(room_id, EventType.ROOM_MEMBER, self.client.mxid)
            except MNotFound:
                return False, "Bot is not a member of this room", {}

            # Get power levels
            power_levels = await self.client.get_state_event(room_id, EventType.ROOM_POWER_LEVELS)
            bot_level = power_levels.users.get(self.client.mxid, power_levels.users_default)
            
            # Define required power levels for different actions
            permission_requirements = {
                "redact": power_levels.redact,
                "state": power_levels.state_default
            }
            
            # Check each required permission
            permission_status = {}
            if required_permissions:
                for perm in required_permissions:
                    if perm in permission_requirements:
                        required_level = permission_requirements[perm]
                        permission_status[perm] = {
                            "has_permission": bot_level >= required_level,
                            "required_level": required_level,
                            "bot_level": bot_level
                        }
            
            # If no specific permissions requested, just check basic access
            if not required_permissions:
                if bot_level < 50:  # Basic moderator level
                    return False, "Bot does not have sufficient power level (needs at least moderator level)", permission_status
                return True, "", permission_status
            
            # Check if all requested permissions are granted
            missing_permissions = [perm for perm, status in permission_status.items() 
                                 if not status["has_permission"]]
            
            if missing_permissions:
                error_msg = "Bot is missing required permissions: " + ", ".join(missing_permissions)
                return False, error_msg, permission_status
            
            return True, "", permission_status

        except Exception as e:
            error_msg = f"Failed to check bot permissions: {e}"
            self.log.error(error_msg)
            if evt:
                await evt.respond(error_msg)
            return False, error_msg, {}

    def is_message_allowed(self, evt: MessageEvent) -> bool:
        """Check if message type and media type are allowed"""
        # Default allowed message types
        default_msgtypes = ["m.text", "m.image"]
        # Default allowed media types
        default_mimetypes = [
            "image/jpeg", "image/png", "image/webp", "image/gif"
        ]
        
        # Get configuration
        allowed_msgtypes = self.config.get("allowed_msgtypes", default_msgtypes)
        allowed_mimetypes = self.config.get("allowed_mimetypes", default_mimetypes)
        
        # Check message type
        msgtype = evt.content.msgtype.value
        if msgtype not in allowed_msgtypes:
            return False
            
        # For image messages, check mimetype
        if msgtype == "m.image":
            # Ensure it's a media message before accessing info
            if isinstance(evt.content, MediaMessageEventContent):
                mimetype = getattr(evt.content.info, "mimetype", None)
                if mimetype and mimetype not in allowed_mimetypes:
                    return False
                
        return True

    async def start(self) -> None:
        await super().start()
        self.config.load_and_update()
        self.client.add_dispatcher(MembershipEventDispatcher)
        
        # Create config snapshot
        self.admins = self.config["admins"] or []
        self.uncensor_pl = self.config["uncensor_pl"] or 0
        self.enable_join_notice = self.config["enable_join_notice"]
        self.custom_notice_text = self.config["custom_notice_text"]
        self.ai_mod_api_endpoint = self.config["ai_mod_api_endpoint"]
        self.moderate_files = self.config["moderate_files"]
        self.ai_mod_threshold = self.config["ai_mod_threshold"]
        # Add message type filtering config snapshot
        self.enable_msgtype_filter = self.config.get("enable_msgtype_filter", False)
        self.allowed_msgtypes = self.config.get("allowed_msgtypes", ["m.text", "m.image"])
        self.allowed_mimetypes = self.config.get("allowed_mimetypes",
            ["image/jpeg", "image/png", "image/webp", "image/gif"])
        
        # Initialize welcome tracking
        self.welcomed_joins = set()

    @event.on(InternalEventType.JOIN)
    async def newjoin(self, evt: StateEvent) -> None:
        # Use room ID + user ID as unique identifier
        join_key = (evt.room_id, evt.sender)
        if join_key in self.welcomed_joins:
            return
            
        self.welcomed_joins.add(join_key)
        
        # Send AI moderation notice if enabled
        if self.enable_join_notice:
            notice_text = self.custom_notice_text or (
                "<em>IMPORTANT: this room is under moderation by machine-learning. "
                "All messages may be sent for analysis to {endpoint}. This conversation "
                "is not as private as you may think!</em>".format(
                    endpoint=self.ai_mod_api_endpoint
                )
            )
            await self.client.send_notice(evt.room_id, html=notice_text)

    @event.on(EventType.ROOM_MESSAGE)
    async def analyze_message(self, evt: MessageEvent) -> None:
        # Skip admins and users above uncensor_pl for message type filtering
        power_levels = await self.client.get_state_event(
            evt.room_id, EventType.ROOM_POWER_LEVELS
        )
        user_level = power_levels.get_user_level(evt.sender)
        
        # Skip message type filtering for admins and privileged users
        if (evt.sender in self.admins or
            user_level >= self.uncensor_pl or
            evt.sender == self.client.mxid):
            pass
        # Apply message type filtering
        elif self.enable_msgtype_filter and not self.is_message_allowed(evt):
            has_perms, error_msg, perm_details = await self.check_bot_permissions(
                evt.room_id, evt, ["redact"]
            )
            if has_perms:
                # Get reason for rejection
                msgtype = evt.content.msgtype.value
                reason = f"Disallowed message type: {msgtype}"
                
                # For image messages, add mimetype to reason
                if msgtype == "m.image" and isinstance(evt.content, MediaMessageEventContent):
                    mimetype = getattr(evt.content.info, "mimetype", "")
                    if mimetype:
                        reason += f" ({mimetype})"
                    
                await self.client.redact(evt.room_id, evt.event_id, reason=reason)
                self.log.info(f"Deleted disallowed message: {evt.event_id} - {reason}")
            else:
                self.log.warning(f"Missing permissions to delete message: {error_msg}")
            return
            
        # Skip if it's a file and file moderation is disabled
        if isinstance(evt.content, MediaMessageEventContent) and not self.moderate_files:
            return

        power_levels = await self.client.get_state_event(
            evt.room_id, EventType.ROOM_POWER_LEVELS
        )
        user_level = power_levels.get_user_level(evt.sender)

        # Check if user should have their message analyzed by AI
        # Use config snapshot for safety checks
        if (evt.sender not in self.admins
            and user_level < self.uncensor_pl
            and evt.sender != self.client.mxid):
            # Check bot permissions
            has_perms, error_msg, perm_details = await self.check_bot_permissions(
                evt.room_id,
                evt,
                ["redact"]
            )

            # Analyze message with AI
            await evt.mark_read()
            score = await self.ai_analyze(evt)
            if not score:  # Skip if analysis failed
                return
            
            self.log.debug(f"Message score: {score.get('comment', '')} ({score.get('analysis', '')})")
            
            # Use threshold from config snapshot
            if score["max"] >= self.ai_mod_threshold:
                if has_perms:
                    try:
                        await self.client.redact(
                            evt.room_id, evt.event_id, reason=score["comment"]
                        )
                    except Exception as e:
                        self.log.error(f"Failed to redact AI-flagged message: {e}")
                else:
                    # Get the required power level for redaction
                    redact_pl = perm_details["redact"]["required_level"]
                    bot_pl = perm_details["redact"]["bot_level"]
                    await evt.reply(
                        f"I would have redacted this message ({score['comment']}), but I need a power level of {redact_pl} or higher to do so (currently {bot_pl})."
                    )

    @classmethod
    def get_config_class(cls) -> Type[BaseProxyConfig]:
        return Config
