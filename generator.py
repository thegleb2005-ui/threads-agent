"""
Генерация черновика поста для Threads на основе транскрипта.

Два режима:
  1. Базовый промпт (BASE_SYSTEM_PROMPT ниже) — модель вытаскивает из
     транскрипта ОДНУ идею и пишет собственный оригинальный текст, не
     пересказывая источник. Это режим по умолчанию.
  2. Индивидуальный промпт — то, что ты написал боту сообщением после
     ссылки. Твоя инструкция полностью заменяет базовую задачу: можно
     попросить сохранить структуру исходного видео, сменить тон, язык,
     формат, длину — что угодно.
"""
import asyncio
from openai import OpenAI

from config import KIE_API_KEY, KIE_BASE_URL, KIE_MODEL, POST_LANGUAGE, POST_STYLE_PROMPT

_client = OpenAI(api_key=KIE_API_KEY, base_url=KIE_BASE_URL)

BASE_SYSTEM_PROMPT = """You are a ghostwriter for a Threads (Meta) account about clear thinking, \
mental models, and decision-making. You write short, original posts inspired by ideas you're \
given from source material — never a translation, summary, or retelling of that material.

Rules:
- Exactly ONE core idea per post, explained entirely in your own words and your own examples
- Strong hook in the first line: a sharp question, a contrarian claim, or a concrete scenario
- Plain, conversational English. No corporate tone, no hashtags, no emoji spam
- Under 500 characters total
- Never mention the source video, its author, or that this is based on a video
- Never quote or closely paraphrase the source text — synthesize, don't retell
"""

CUSTOM_SYSTEM_PROMPT = """You are a ghostwriter for a Threads (Meta) account. You will be given \
a transcript of a video and a specific instruction from the account owner about what to do \
with it. Follow that instruction precisely — it takes priority over any default assumptions \
about format, length, tone, or language.

Output ONLY the post text itself: no preamble, no explanations, no quotation marks around it, \
no "Here's your post:" — just the text ready to publish."""


def _build_base_user_prompt(transcript: str) -> str:
    extra = f"\nAdditional style notes: {POST_STYLE_PROMPT}" if POST_STYLE_PROMPT else ""
    return (
        f"Below is a raw, messy transcript of a video about thinking, psychology or "
        f"productivity. Find ONE genuinely interesting idea in it and write an original "
        f"Threads post in {POST_LANGUAGE} inspired by that idea.{extra}\n\n"
        f"--- TRANSCRIPT START ---\n{transcript[:12000]}\n--- TRANSCRIPT END ---"
    )


def _build_custom_user_prompt(transcript: str, custom_prompt: str) -> str:
    return (
        f"Instruction from the account owner:\n{custom_prompt}\n\n"
        f"--- TRANSCRIPT START ---\n{transcript[:12000]}\n--- TRANSCRIPT END ---"
    )


def _generate_sync(transcript: str, custom_prompt: str | None = None) -> str:
    if custom_prompt:
        system_prompt = CUSTOM_SYSTEM_PROMPT
        user_prompt = _build_custom_user_prompt(transcript, custom_prompt)
    else:
        system_prompt = BASE_SYSTEM_PROMPT
        user_prompt = _build_base_user_prompt(transcript)

    response = _client.chat.completions.create(
        model=KIE_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.9,
        max_tokens=1500,  # с запасом: индивидуальный промпт может просить длинный текст
    )
    return response.choices[0].message.content.strip()


async def generate_draft(transcript: str, custom_prompt: str | None = None) -> str:
    """Генерирует черновик поста. Если задан custom_prompt — используется он,
    иначе базовая задача из BASE_SYSTEM_PROMPT."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _generate_sync, transcript, custom_prompt)
