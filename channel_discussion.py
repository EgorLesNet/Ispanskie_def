"""Publish a rules message and poll as replies in a channel's discussion thread."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Sequence

from aiogram import Bot
from aiogram.types import Message

logger = logging.getLogger(__name__)


class ChannelDiscussionPublisher:
    def __init__(
        self,
        bot: Bot,
        channel_id: int,
        rules_text: str,
        poll_question: str,
        poll_options: Sequence[str],
        state_path: str | Path = "channel_discussion_state.json",
    ) -> None:
        if len(poll_options) < 2:
            raise ValueError("poll_options must contain at least two choices")
        self.bot = bot
        self.channel_id = channel_id
        self.rules_text = rules_text
        self.poll_question = poll_question
        self.poll_options = list(poll_options)
        self.state_path = Path(state_path)
        self._lock = asyncio.Lock()

    @classmethod
    def from_env(cls, bot: Bot) -> "ChannelDiscussionPublisher":
        options = [item.strip() for item in os.environ["COMMENT_POLL_OPTIONS"].split("|") if item.strip()]
        return cls(
            bot=bot,
            channel_id=int(os.environ["CHANNEL_ID"]),
            rules_text=os.environ["COMMENT_RULES_TEXT"],
            poll_question=os.environ.get("COMMENT_POLL_QUESTION", "Вы согласны с правилами обсуждения?"),
            poll_options=options,
            state_path=os.environ.get("CHANNEL_DISCUSSION_STATE", "channel_discussion_state.json"),
        )

    def _published_ids(self) -> set[int]:
        if not self.state_path.exists():
            return set()
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
            return {int(item) for item in raw.get("published_channel_message_ids", [])}
        except (OSError, ValueError, TypeError) as exc:
            logger.warning("Cannot read %s: %s", self.state_path, exc)
            return set()

    def _mark_published(self, channel_message_id: int) -> None:
        published = self._published_ids()
        published.add(channel_message_id)
        tmp_path = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        tmp_path.write_text(
            json.dumps({"published_channel_message_ids": sorted(published)}, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp_path.replace(self.state_path)

    async def publish(self, channel_post: Message) -> bool:
        """Reply with rules and a poll to this channel post's linked discussion message.

        Returns False for posts from another channel or posts already processed.
        """
        if channel_post.chat.id != self.channel_id:
            return False

        async with self._lock:
            if channel_post.message_id in self._published_ids():
                return False

            try:
                discussion_message = await self.bot.get_discussion_message(
                    chat_id=channel_post.chat.id,
                    message_id=channel_post.message_id,
                )
                reply_to_message_id = discussion_message.message_id
                discussion_chat_id = discussion_message.chat.id

                await self.bot.send_message(
                    chat_id=discussion_chat_id,
                    text=self.rules_text,
                    reply_to_message_id=reply_to_message_id,
                )
                await self.bot.send_poll(
                    chat_id=discussion_chat_id,
                    question=self.poll_question,
                    options=self.poll_options,
                    is_anonymous=False,
                    reply_to_message_id=reply_to_message_id,
                )
            except Exception:
                logger.exception(
                    "Could not publish discussion content for channel post %s", channel_post.message_id
                )
                raise

            self._mark_published(channel_post.message_id)
            return True
