from concurrent.futures import ThreadPoolExecutor
import asyncio
import logging
from typing import AsyncGenerator, Optional, Tuple, Any
from langchain import ConversationChain
from vocode.streaming.agent.base_agent import RespondAgent
from vocode.streaming.models.agent import LlamacppAgentConfig
from vocode.streaming.agent.utils import stream_response_async, CallbackOutput
from langchain.callbacks.base import BaseCallbackHandler
from langchain.callbacks.manager import CallbackManager
from langchain.llms import LlamaCpp
from langchain.prompts import (
    ChatPromptTemplate,
    MessagesPlaceholder,
    HumanMessagePromptTemplate,
)
from langchain.schema import LLMResult, SystemMessage, get_buffer_string
from langchain.memory import ConversationBufferMemory
from langchain.prompts import (
    ChatPromptTemplate,
    MessagesPlaceholder,
    HumanMessagePromptTemplate,
    PromptTemplate,
)
from langchain.prompts.base import DEFAULT_FORMATTER_MAPPING

ALPACA_TEMPLATE_WITH_HISTORY = """### Instruction:
Your previous conversation history:
{history}

Current instruction/message to respond to: {input}
### Response:"""


class FormatHistoryPromptTemplate(PromptTemplate):
    def format(self, **kwargs: Any) -> str:
        kwargs = self._merge_partial_and_user_variables(**kwargs)
        kwargs["history"] = get_buffer_string(kwargs["history"])
        return DEFAULT_FORMATTER_MAPPING[self.template_format](self.template, **kwargs)


class CustomStreamingCallbackHandler(BaseCallbackHandler):
    def __init__(self, output_queue: asyncio.Queue) -> None:
        super().__init__()
        self.output_queue = output_queue

    def on_llm_new_token(self, token: str, **kwargs: Any) -> None:
        """Run on new LLM token. Only available when streaming is enabled."""
        self.output_queue.put_nowait(CallbackOutput(token=token))

    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        """Run when LLM ends running."""
        self.output_queue.put_nowait(CallbackOutput(finish=True, response=response))


class LlamacppAgent(RespondAgent[LlamacppAgentConfig]):
    def __init__(
        self,
        agent_config: LlamacppAgentConfig,
        logger: Optional[logging.Logger] = None,
    ):
        super().__init__(agent_config=agent_config, logger=logger)

        if type(agent_config.prompt_template) is str:
            if agent_config.prompt_template == "alpaca":
                prompt = FormatHistoryPromptTemplate(
                    input_variables=["history", "input"],
                    template=ALPACA_TEMPLATE_WITH_HISTORY,
                )
            else:
                raise ValueError(
                    f"Unknown prompt template {agent_config.prompt_template}"
                )
        else:
            prompt = (
                agent_config.prompt_template
                or ChatPromptTemplate.from_messages(
                    [
                        MessagesPlaceholder(variable_name="history"),
                        HumanMessagePromptTemplate.from_template("{input}"),
                    ]
                )
            )
        self.prompt : PromptTemplate = prompt

        self.callback_queue : asyncio.Queue = asyncio.Queue()
        callback = CustomStreamingCallbackHandler(self.callback_queue)
        callback_manager = CallbackManager([callback])
        self.llm = LlamaCpp(
            callback_manager=callback_manager, **agent_config.llamacpp_kwargs
        )

        self.memory = ConversationBufferMemory(return_messages=True)
        self.memory.chat_memory.messages.append(
            SystemMessage(content=self.agent_config.prompt_preamble)
        )

        self.conversation = ConversationChain(
            memory=self.memory, prompt=self.prompt, llm=self.llm
        )
        self.thread_pool_executor = ThreadPoolExecutor(max_workers=1)

    async def respond(
        self,
        human_input,
        conversation_id: str,
        is_interrupt: bool = False,
    ) -> Tuple[str, bool]:
        text = await asyncio.get_event_loop().run_in_executor(
            self.thread_pool_executor,
            lambda input: self.conversation.predict(input=input),
            human_input,
        )

        self.logger.debug(f"LLM response: {text}")
        return text, False

    async def generate_response(
        self,
        human_input: str,
        conversation_id: str,
        is_interrupt: bool = False,
    ) -> AsyncGenerator[str, None]:
        asyncio.get_event_loop().run_in_executor(
            self.thread_pool_executor,
            lambda input: self.conversation.predict(input=input),
            human_input,
        )

        async def stream():
            while True:
                callback_output = await self.callback_queue.get()
                if callback_output.finish:
                    break
                yield callback_output

        async for message in stream_response_async(
            stream(),
            get_text=lambda o: o.token,
            openai=False,
        ):
            yield message