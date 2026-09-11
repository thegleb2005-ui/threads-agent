"""
Генерация оригинального черновика поста для Threads на основе транскрипта.

Важно: модель НЕ пересказывает транскрипт, а вытаскивает 1 идею и пишет
собственный текст. Это принципиально для избежания плагиата чужого контента.
"""
import asyncio
from openai import OpenAI

from config import KIE_API_KEY, KIE_BASE_URL, KIE_MODEL, POST_LANGUAGE, POST_STYLE_PROMPT

_client = OpenAI(api_key=KIE_API_KEY, base_url=KIE_BASE_URL)

SYSTEM_PROMPT = """You are a ghostwriter for a Threads (Meta) account about clear thinking, \
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


def _build_user_prompt(transcript: str) -> str:
    extra = f"\nAdditional style notes: {POST_STYLE_PROMPT}" if POST_STYLE_PROMPT else ""
    return (
        f"Below is a raw, messy transcript of a video about thinking, psychology or "
        f"productivity. Find ONE genuinely interesting idea in it and write an original "
        f"Threads post in {POST_LANGUAGE} inspired by that idea.{extra}\n\n"
        f"--- TRANSCRIPT START ---\n{transcript[:12000]}\n--- TRANSCRIPT END ---"
    )


def _generate_sync(transcript: str) -> str:
    response = _client.chat.completions.create(
        model=KIE_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _build_user_prompt(transcript)},
        ],
        temperature=0.9,
        max_tokens=400,
    )
    return response.choices[0].message.content.strip()


async def generate_draft(transcript: str) -> str:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _generate_sync, transcript)
