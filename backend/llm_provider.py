import os
import re
from abc import ABC, abstractmethod
from typing import Optional
from openai import OpenAI
from google import genai
from dotenv import load_dotenv

load_dotenv()


def _regex_split(message: str) -> list:
    """LLM ile ayırma başarısız olursa kullanılan basit yedek ayırıcı.

    Yalnızca '?' ve satır sonu gibi belirgin ayraçlara bakar; noktalama
    olmayan durumlarda tek soru olarak döndürür.
    """
    message = message.strip()
    if not message:
        return []
    parts = re.split(r'[?\n]+', message)
    questions = [p.strip() for p in parts if len(p.strip()) >= 3]
    if len(questions) <= 1:
        return [message]
    seen = set()
    unique = []
    for q in questions:
        key = q.lower()
        if key not in seen:
            seen.add(key)
            unique.append(q)
    return unique


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

    def _build_split_prompt(self, message: str) -> tuple:
        system = (
            "Sen bir soru ayırıcı asistansın. "
            "Kullanıcının mesajındaki birbirinden farklı soruları ayırırsın. "
            "Noktalama işareti (soru işareti, virgül vb.) hiç olmasa bile "
            "farklı konulardaki soruları tespit edersin. "
            "Soruları asla cevaplamazsın, sadece ayırırsın."
        )
        user = (
            f"Kullanıcı mesajı: {message}\n\n"
            "Mesajdaki her farklı soruyu ayrı bir satıra yaz. "
            "Her soruyu, kendi başına anlaşılır ve eksiksiz olacak şekilde yaz. "
            "Mesajda tek bir soru varsa sadece o soruyu tek satır olarak yaz. "
            "Numaralandırma, açıklama veya başka hiçbir şey ekleme."
        )
        return system, user

    def _parse_split(self, raw: str, original: str) -> list:
        if not raw:
            return [original]
        lines = []
        for line in raw.splitlines():
            # baştaki madde/numara işaretlerini temizle ("1. ", "- ", "* ", "• ")
            line = re.sub(r'^\s*(?:[-*•]|\d+[.)])\s*', '', line).strip()
            if len(line) >= 3:
                lines.append(line)
        seen = set()
        unique = []
        for l in lines:
            key = l.lower()
            if key not in seen:
                seen.add(key)
                unique.append(l)
        return unique or [original]

    def split_questions(self, message: str) -> list:
        """Mesajı LLM ile alt sorulara böler (noktalama olmasa bile).

        Selector mantığı korunur: bu adım sadece soruları ayırmak içindir,
        cevaplar daha sonra her alt soru için ayrı ayrı birebir seçtirilir.
        Herhangi bir hata olursa basit regex yedeğine düşer.
        """
        message = message.strip()
        if not message:
            return []
        try:
            system, user = self._build_split_prompt(message)
            raw = self._complete(system, user, max_tokens=300)
            return self._parse_split(raw, message)
        except Exception:
            return _regex_split(message)

    @abstractmethod
    def _complete(self, system: str, user: str, max_tokens: int = 5) -> str:
        """Sağlayıcıya özgü ham metin tamamlama çağrısı."""
        pass

    @abstractmethod
    def ask(self, question: str, context_list: list) -> Optional[str]:
        pass


class OpenAIProvider(BaseLLMProvider):
    def __init__(self, model: str = "gpt-4o-mini"):
        self.client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        self.model = model

    def _complete(self, system: str, user: str, max_tokens: int = 5) -> str:
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user}
            ],
            max_tokens=max_tokens,
            temperature=0,
        )
        return response.choices[0].message.content

    def ask(self, question: str, context_list: list) -> Optional[str]:
        system, user = self._build_prompt(question, context_list)
        raw = self._complete(system, user, max_tokens=5)
        return self._parse_selection(raw, context_list)


class GeminiProvider(BaseLLMProvider):
    def __init__(self, model: str = "gemini-2.5-flash-lite"):
        self.client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
        self.model = model

    def _complete(self, system: str, user: str, max_tokens: int = 5) -> str:
        response = self.client.models.generate_content(
            model=self.model,
            contents=f"{system}\n\n{user}"
        )
        return response.text

    def ask(self, question: str, context_list: list) -> Optional[str]:
        system, user = self._build_prompt(question, context_list)
        raw = self._complete(system, user, max_tokens=5)
        return self._parse_selection(raw, context_list)


class OpenRouterProvider(BaseLLMProvider):
    def __init__(self, model: str = "openai/gpt-4o-mini"):
        self.client = OpenAI(
            api_key=os.getenv("OPENROUTER_API_KEY"),
            base_url="https://openrouter.ai/api/v1"
        )
        self.model = model

    def _complete(self, system: str, user: str, max_tokens: int = 5) -> str:
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user}
            ],
            max_tokens=max_tokens,
            temperature=0,
        )
        return response.choices[0].message.content

    def ask(self, question: str, context_list: list) -> Optional[str]:
        system, user = self._build_prompt(question, context_list)
        raw = self._complete(system, user, max_tokens=5)
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
