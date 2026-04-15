import os
from abc import ABC, abstractmethod
from openai import OpenAI
from google import genai 
from dotenv import load_dotenv

load_dotenv()

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

class BaseLLMProvider(ABC):
    @abstractmethod
    def ask(self, question: str, context_list: list) -> str:
        pass

class OpenAIProvider(BaseLLMProvider):
    def __init__(self, model: str = "gpt-4o-mini"):
        self.client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        self.model = model

    def ask(self, question: str, context_list: list) -> str:
        context_str = "\n\n".join([
            f"Context {i+1}: Soru: {item['question']}\nCevap: {item['answer']}" 
            for i, item in enumerate(context_list)
        ])
        
        prompt = f"""
        User Question: {question}

        Context (Referans Bilgiler):
        {context_str}

        Instruction:
        Yukarıdaki bilgileri kullanarak soruyu cevapla.
        Eğer bilgi yetersizse, AUZEF ile ilgili olduğunu belirt ve bilmediğini söyle.
        Dışarıdan ek bilgi katma, sadece Context içindekileri baz al.
        """

        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": "Sen AUZEF (Açık ve Uzaktan Eğitim Fakültesi) için yardımcı bir asistansın. Sadece sana verilen context'i kullanarak cevap verirsin."},
                {"role": "user", "content": prompt}
            ]
        )
        return response.choices[0].message.content


class GeminiProvider(BaseLLMProvider):
    def __init__(self, model: str = "gemini-2.5-flash-lite"):
        self.client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
        self.model = model

    def ask(self, question: str, context_list: list) -> str:
        context_str = "\n\n".join([
            f"Context {i+1}: Soru: {item['question']}\nCevap: {item['answer']}"
            for i, item in enumerate(context_list)
        ])

        prompt = f"""
Sen AUZEF (Açık ve Uzaktan Eğitim Fakültesi) için yardımcı bir asistansın.
Sadece verilen context ile cevap ver.

User Question: {question}

Context:
{context_str}

Instruction:
Sadece verilen context ile cevapla.
Bilmiyorsan AUZEF ile ilgili olduğunu söyle ve bilmediğini belirt.
"""

        response = self.client.models.generate_content(
            model=self.model,
            contents=prompt
        )

        return response.text
    
class OpenRouterProvider(BaseLLMProvider):
    def __init__(self, model: str = "openai/gpt-4o-mini"):
        self.client = OpenAI(
            api_key=os.getenv("OPENROUTER_API_KEY"),
            base_url="https://openrouter.ai/api/v1"
        )
        self.model = model

    def ask(self, question: str, context_list: list) -> str:
        context_str = "\n\n".join([
            f"Context {i+1}: Soru: {item['question']}\nCevap: {item['answer']}"
            for i, item in enumerate(context_list)
        ])

        prompt = f"""
        User Question: {question}

        Context (Referans Bilgiler):
        {context_str}

        Instruction:
        Yukarıdaki bilgileri kullanarak soruyu cevapla.
        Eğer bilgi yetersizse, AUZEF ile ilgili olduğunu belirt ve bilmediğini söyle.
        Dışarıdan ek bilgi katma, sadece Context içindekileri baz al.
        """

        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": "Sen AUZEF (Açık ve Uzaktan Eğitim Fakültesi) için yardımcı bir asistansın. Sadece sana verilen context'i kullanarak cevap verirsin."},
                {"role": "user", "content": prompt}
            ]
        )
        return response.choices[0].message.content