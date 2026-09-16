"""LLM 封装：DeepSeek（OpenAI 兼容协议）。"""
from langchain_openai import ChatOpenAI

from app.config import settings


def get_llm(temperature: float = 0.0) -> ChatOpenAI:
    return ChatOpenAI(
        model=settings.deepseek_model,
        api_key=settings.deepseek_api_key,
        base_url=settings.deepseek_base_url,
        temperature=temperature,
        max_retries=0,  # 关掉 SDK 内层重试：重试统一由 app.retry 控制，避免嵌套放大
    )


def get_llm_model_name() -> str:
    return settings.deepseek_model
