# Правила и опрос в комментариях к посту

`ChannelDiscussionPublisher` обрабатывает обновление `channel_post`, получает через `getDiscussionMessage` его зеркальное сообщение в привязанной группе обсуждений и публикует правила и опрос с `reply_to_message_id` этого сообщения. Поэтому оба сообщения оказываются именно в комментариях к нужному посту.

## Условия

- В канале должна быть привязана группа обсуждений.
- Бот должен быть администратором канала и группы, иметь право публиковать сообщения и создавать опросы.
- Получение `channel_post` должно быть включено в используемом у бота webhook/polling.

## Настройки окружения

```dotenv
CHANNEL_ID=-1001234567890
COMMENT_RULES_TEXT=Правила обсуждения:\n1. Соблюдаем уважительный тон.\n2. Не публикуем рекламу и персональные данные.
COMMENT_POLL_QUESTION=Вы ознакомились с правилами?
COMMENT_POLL_OPTIONS=Да|Нет
CHANNEL_DISCUSSION_STATE=channel_discussion_state.json
```

## Подключение к существующему aiogram-боту

Создайте экземпляр один раз после создания `Bot`:

```python
from channel_discussion import ChannelDiscussionPublisher

publisher = ChannelDiscussionPublisher.from_env(bot)
```

В существующем обработчике обновлений канального поста вызовите publisher **до** другой бизнес-логики:

```python
@router.channel_post()
async def on_channel_post(message: Message) -> None:
    await publisher.publish(message)
```

Если проект использует aiogram 2, тело обработчика остаётся тем же:

```python
@dp.channel_post_handler()
async def on_channel_post(message: types.Message) -> None:
    await publisher.publish(message)
```

Модуль сохраняет обработанные `message_id` в JSON-файл и не дублирует правила/опрос при повторной доставке апдейта. Не добавляйте этот state-файл в Git; он уже предназначен для runtime-состояния.
