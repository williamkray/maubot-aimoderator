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
        helper.copy("sleep")
        helper.copy("admins")
        helper.copy("uncensor_pl")
        helper.copy("moderate_files")
        helper.copy("ai_mod_threshold")
        helper.copy("ai_mod_api_key")
        helper.copy("ai_mod_api_endpoint")
        helper.copy("ai_mod_api_model")


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

    @event.on(InternalEventType.JOIN)
    async def newjoin(self, evt: StateEvent) -> None:
        if evt.source & SyncStream.STATE:
            return
        else:
            # Send AI moderation notice when someone joins
                aimod_greeting = (
                    "<em>IMPORTANT: this room is under moderation by machine-learning. "
                    "All messages may be sent for analysis to {endpoint}. This conversation "
                    "is not as private as you may think!</em>".format(
                        endpoint=self.config["ai_mod_api_endpoint"]
                        )
                    )
                await self.client.send_notice(evt.room_id, html=aimod_greeting)

    @event.on(EventType.ROOM_MESSAGE)
    async def analyze_message(self, evt: MessageEvent) -> None:

        # Skip if it's a file and file moderation is disabled
        if isinstance(evt.content, MediaMessageEventContent) and not self.config["moderate_files"]:
            return

        power_levels = await self.client.get_state_event(
            evt.room_id, EventType.ROOM_POWER_LEVELS
        )
        user_level = power_levels.get_user_level(evt.sender)

        # Check if user should have their message analyzed by AI
        if (evt.sender not in self.config["admins"]
            and user_level < self.config["uncensor_pl"]
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

            self.log.debug(f"DEBUG message score: {score['comment']} ({score['analysis']})")
            
            # If score is high enough, either redact or notify about missing permissions
            if self.flag_score(score):
                if has_perms:
                    try:
                        await self.client.redact(
                            evt.room_id, evt.event_id, reason=score["comment"]
                        )
                    except Exception as e:
                        self.log.error(f"AI-flagged message should be redacted: {e}")
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
