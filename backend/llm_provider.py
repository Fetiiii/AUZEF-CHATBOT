import os
from abc import ABC, abstractmethod
from typing import Optional
from openai import OpenAI
from google import genai
from dotenv import load_dotenv

load_dotenv()


class BaseLLMProvider(ABC):

    def _build_prompt(self, question: str, context_list: list) -> tuple:
        candidates = "\n".join([
            f"[{i+1}] Soru: {item['question']}\n    Cevap: {item['answer']}"
            for i, item in enumerate(context_list)
        ])
        system = (
            "Sen bir soru-cevap seçici asistansın. "
            "Sana verilen aday cevaplar arasından kullanıcının sorusuna en uygun olanı seçersin. "
            "Kendi cevabını asla üretmezsin, yalnızca bir sayı yazarsın."
        )
        user = (
            f"Kullanıcı sorusu: {question}\n\n"
            f"Aday cevaplar:\n{candidates}\n\n"
            f"Yalnızca en uygun adayın numarasını yaz (1-{len(context_list)}). "
            f"Hiçbiri uygun değilse 0 yaz. Başka hiçbir şey yazma."
        )
        return system, user

    def _parse_selection(self, raw: str, context_list: list) -> Optional[str]:
        try:
            idx = int(raw.strip())
            if 1 <= idx <= len(context_list):
                return context_list[idx - 1]['answer']
        except (ValueError, IndexError):
            pass
        return None

    @abstractmethod
    def ask(self, question: str, context_list: list) -> Optional[str]:
        pass


class OpenAIProvider(BaseLLMProvider):
    def __init__(self, model: str = "gpt-4o-mini"):
        self.client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        self.model = model

    def ask(self, question: str, context_list: list) -> Optional[str]:
        system, user = self._build_prompt(question, context_list)
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user}
            ],
            max_tokens=5,
            temperature=0,
        )
        raw = response.choices[0].message.content
        return self._parse_selection(raw, context_list)


class GeminiProvider(BaseLLMProvider):
    def __init__(self, model: str = "gemini-2.5-flash-lite"):
        self.client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
        self.model = model

    def ask(self, question: str, context_list: list) -> Optional[str]:
        system, user = self._build_prompt(question, context_list)
        response = self.client.models.generate_content(
            model=self.model,
            contents=f"{system}\n\n{user}"
        )
        return self._parse_selection(response.text, context_list)


class OpenRouterProvider(BaseLLMProvider):
    def __init__(self, model: str = "openai/gpt-4o-mini"):
        self.client = OpenAI(
            api_key=os.getenv("OPENROUTER_API_KEY"),
            base_url="https://openrouter.ai/api/v1"
        )
        self.model = model

    def ask(self, question: str, context_list: list) -> Optional[str]:
        system, user = self._build_prompt(question, context_list)
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user}
            ],
            max_tokens=5,
            temperature=0,
        )
        raw = response.choices[0].message.content
        return self._parse_selection(raw, context_list)


class LLMFactory:
    @staticmethod
    def create_provider(provider_name: str):
        if provider_name == "openai":
            return OpenAIProvider()
        elif provider_name == "gemini":
            return GeminiProvider()
        elif provider_name == "openrouter":
            return OpenRouterProvider()
        else:
            raise ValueError(f"Unsupported provider: {provider_name}")