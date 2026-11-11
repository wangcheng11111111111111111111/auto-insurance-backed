"""
LLM 大模型服务 - 统一的模型调用接口
支持 OpenAI / DeepSeek / Azure OpenAI / Ollama
"""
import json
import logging
from typing import Optional, AsyncIterator

from openai import AsyncOpenAI, AsyncAzureOpenAI

from config.settings import settings

logger = logging.getLogger(__name__)


class LLMService:
    """大模型服务，封装对话和 Embedding 能力"""

    def __init__(self):
        self.provider = settings.llm.PROVIDER
        self._client: Optional[AsyncOpenAI] = None
        self._embedding_client: Optional[AsyncOpenAI] = None
        logger.info("LLM 服务初始化，提供商: %s", self.provider)

    @property
    def client(self) -> AsyncOpenAI:
        if self._client is None:
            if self.provider == "azure":
                self._client = AsyncAzureOpenAI(
                    api_key=settings.llm.AZURE_OPENAI_API_KEY,
                    azure_endpoint=settings.llm.AZURE_OPENAI_ENDPOINT,
                    api_version=settings.llm.AZURE_OPENAI_API_VERSION,
                )
            elif self.provider == "deepseek":
                self._client = AsyncOpenAI(
                    api_key=settings.llm.DEEPSEEK_API_KEY,
                    base_url=settings.llm.DEEPSEEK_BASE_URL,
                )
            elif self.provider == "ollama":
                self._client = AsyncOpenAI(
                    base_url=f"{settings.llm.OLLAMA_BASE_URL}/v1",
                    api_key="ollama",
                )
            else:  # openai
                self._client = AsyncOpenAI(
                    api_key=settings.llm.OPENAI_API_KEY,
                    base_url=settings.llm.OPENAI_BASE_URL,
                )
        return self._client

    @property
    def embedding_client(self) -> AsyncOpenAI:
        if self._embedding_client is None:
            self._embedding_client = AsyncOpenAI(
                api_key=settings.embedding.API_KEY,
                base_url=settings.embedding.BASE_URL,
            )
        return self._embedding_client

    async def chat(
        self,
        system_prompt: str,
        user_message: str,
        history: Optional[list[dict]] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> str:
        """单轮对话"""
        messages = [{"role": "system", "content": system_prompt}]
        if history:
            messages.extend(history)
        messages.append({"role": "user", "content": user_message})

        model = self._get_model()
        temp = self._get_temperature() if temperature is None else temperature
        max_tok = self._get_max_tokens() if max_tokens is None else max_tokens

        logger.info("LLM 请求: model=%s, messages_count=%d", model, len(messages))
        response = await self.client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=temp,
            max_tokens=max_tok,
        )
        content = response.choices[0].message.content or ""
        logger.info("LLM 响应长度: %d", len(content))
        return content

    async def chat_stream(
        self,
        system_prompt: str,
        user_message: str,
        history: Optional[list[dict]] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> AsyncIterator[str]:
        """流式对话"""
        messages = [{"role": "system", "content": system_prompt}]
        if history:
            messages.extend(history)
        messages.append({"role": "user", "content": user_message})

        model = self._get_model()
        temp = self._get_temperature() if temperature is None else temperature
        max_tok = self._get_max_tokens() if max_tokens is None else max_tokens

        stream = await self.client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=temp,
            max_tokens=max_tok,
            stream=True,
        )
        async for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """文本向量化"""
        model = settings.embedding.MODEL
        response = await self.embedding_client.embeddings.create(
            model=model,
            input=texts,
        )
        return [d.embedding for d in response.data]

    async def classify_intent(self, user_input: str, intents: list[str]) -> str:
        """意图分类"""
        prompt = f"""你是一个车险智能体的意图分类器。请将用户的输入分类为以下意图之一。

用户输入："{user_input}"

可选意图：
{chr(10).join(f"- {i}" for i in intents)}

只返回意图名称，不要解释。"""
        result = await self.chat("", prompt, temperature=0.0, max_tokens=50)
        return result.strip()

    async def extract_slots(self, user_input: str, slot_schema: dict) -> dict:
        """槽位提取 - 从用户输入中提取结构化信息"""
        schema_str = "\n".join(f"- {k}: {v}" for k, v in slot_schema.items())
        prompt = f"""你是一个车险信息提取器。从用户输入中提取以下信息，返回 JSON 格式。

用户输入："{user_input}"

需要提取的槽位：
{schema_str}

只返回 JSON，不要其他内容。如果某个槽位无法提取，值设为 null。"""
        result = await self.chat("", prompt, temperature=0.0, max_tokens=500)
        try:
            return json.loads(result.strip().removeprefix("```json").removesuffix("```").strip())
        except json.JSONDecodeError:
            logger.warning("槽位提取 JSON 解析失败: %s", result)
            return {}

    def _get_model(self) -> str:
        if self.provider == "azure":
            return settings.llm.AZURE_OPENAI_DEPLOYMENT
        elif self.provider == "deepseek":
            return settings.llm.DEEPSEEK_MODEL
        elif self.provider == "ollama":
            return settings.llm.OLLAMA_MODEL
        return settings.llm.OPENAI_MODEL

    def _get_temperature(self) -> float:
        if self.provider == "deepseek":
            return settings.llm.DEEPSEEK_TEMPERATURE
        elif self.provider == "ollama":
            return settings.llm.OLLAMA_TEMPERATURE
        return settings.llm.OPENAI_TEMPERATURE

    def _get_max_tokens(self) -> int:
        if self.provider == "azure":
            return settings.llm.AZURE_OPENAI_MAX_TOKENS
        elif self.provider == "deepseek":
            return settings.llm.DEEPSEEK_MAX_TOKENS
        elif self.provider == "ollama":
            return settings.llm.OLLAMA_MAX_TOKENS
        return settings.llm.OPENAI_MAX_TOKENS


llm_service = LLMService()