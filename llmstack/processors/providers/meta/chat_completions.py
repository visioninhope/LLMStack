import logging
from typing import List, Optional

from asgiref.sync import async_to_sync
from pydantic import Field

from llmstack.apps.schemas import OutputTemplate
from llmstack.common.blocks.base.schema import StrEnum
from llmstack.processors.providers.api_processor_interface import (
    ApiProcessorInterface,
    ApiProcessorSchema,
)

logger = logging.getLogger(__name__)


class MessagesModel(StrEnum):
    LLAMA_3_8B = "llama-3-8b"
    LLAMA_3_8B_INSTRUCT = "llama-3-8b-instruct"
    LLAMA_3_70B = "llama-3.1-70b"
    LLAMA_3_70B_INSTRUCT = "llama-3.1-70b-instruct"
    LLAMA_3_1_8B = "llama-3.1-8b"
    LLAMA_3_1_8B_INSTRUCT = "llama-3.1-8b-instruct"
    LLAMA_3_1_70B = "llama-3.1-70b"
    LLAMA_3_1_70B_INSTRUCT = "llama-3.1-70b-instruct"
    LLAMA_3_1_405B = "llama-3.1-405b"
    LLAMA_3_1_405B_INSTRUCT = "llama-3.1-405b-instruct"

    def model_name(self):
        return self.value


class Role(StrEnum):
    USER = "user"
    SYSTEM = "system"
    ASSISTANT = "assistant"


class ChatMessage(ApiProcessorSchema):
    role: Role = Field(
        default=Role.USER,
        description="The role of the message. Can be 'system', 'user' or 'assistant'.",
    )
    message: str = Field(
        default="",
        description="The message text.",
        json_schema_extra={"widget": "textarea"},
    )


class MessagesInput(ApiProcessorSchema):
    messages: List[ChatMessage] = Field(
        default=[
            ChatMessage(),
        ],
        description="A list of messages, each with a role and message text.",
    )


class MessagesOutput(ApiProcessorSchema):
    result: str = Field(description="The response message.")


class MessagesConfiguration(ApiProcessorSchema):
    system_message: str = Field(
        default="You are a helpful assistant.",
        description="A system prompt is a way of providing context and instructions to the model.",
        json_schema_extra={"widget": "textarea", "advanced_parameter": False},
    )
    model: MessagesModel = Field(
        default=MessagesModel.LLAMA_3_8B,
        description="The Llama model that will generate the responses.",
        json_schema_extra={"widget": "customselect", "advanced_parameter": False},
    )
    max_tokens: int = Field(
        ge=1,
        default=256,
        description="The maximum number of tokens to generate before stopping.",
        json_schema_extra={"advanced_parameter": False},
    )
    temperature: float = Field(
        default=0.5,
        description="Amount of randomness injected into the response.",
        multiple_of=0.1,
        ge=0.0,
        le=1.0,
        json_schema_extra={"advanced_parameter": False},
    )
    retain_history: Optional[bool] = Field(
        default=False,
        description="Retain and use the chat history. (Only works in apps)",
    )
    top_k: Optional[bool] = Field(
        default=False,
        description="Whether to inject a safety prompt before all conversations.",
    )
    seed: Optional[int] = Field(
        default=None,
        description="The seed to use for random sampling. If set, different calls will generate deterministic results.",
    )
    top_p: Optional[float] = Field(
        default=0.9,
        description="The cumulative probability of the top tokens to sample from.",
        multiple_of=0.1,
        ge=0.0,
        le=1.0,
        json_schema_extra={"advanced_parameter": False},
    )
    repetition_penalty: Optional[float] = Field(
        default=1.0,
        description="The penalty for repeating the same token.",
        multiple_of=0.1,
        json_schema_extra={"advanced_parameter": False},
    )
    stop: Optional[List[str]] = Field(
        default=["<|eot_id|>"],
        description="A list of tokens at which to stop generation.",
        json_schema_extra={"advanced_parameter": False},
    )
    deployment_names: Optional[List[str]] = Field(default=None, description="The deployment provider config to use.")


class MessagesProcessor(ApiProcessorInterface[MessagesInput, MessagesOutput, MessagesConfiguration]):
    @staticmethod
    def name() -> str:
        return "Chat Completions"

    @staticmethod
    def slug() -> str:
        return "chat_completions"

    @staticmethod
    def description() -> str:
        return "Llama Chat Completions"

    @staticmethod
    def provider_slug() -> str:
        return "meta"

    @classmethod
    def get_output_template(cls) -> Optional[OutputTemplate]:
        return OutputTemplate(
            markdown="""{{result}}""",
            jsonpath="$.result",
        )

    def process_session_data(self, session_data):
        self._chat_history = session_data["chat_history"] if "chat_history" in session_data else []

    def session_data_to_persist(self) -> dict:
        if self._config.retain_history:
            return {"chat_history": self._chat_history}
        return {}

    def process(self) -> MessagesOutput:
        from llmstack.common.utils.sslr import LLM

        deployment_config = self.get_provider_config(model_slug=self._config.model)
        if not deployment_config:
            raise Exception(f"Model deployment config not found for {self.provider_slug()}/{self._config.model}")

        messages = []

        if self._config.system_message:
            messages.append({"role": "system", "content": self._config.system_message})

        if self._chat_history:
            for message in self._chat_history:
                messages.append({"role": message["role"], "content": message["message"]})

        for message in self._input.messages:
            messages.append({"role": str(message.role), "content": str(message.message)})

        client = LLM(provider="custom", deployment_config=deployment_config.model_dump().get("deployment_config"))

        result = client.chat.completions.create(
            messages=messages,
            model="",
            max_tokens=self._config.max_tokens,
            stream=True,
            seed=self._config.seed,
            temperature=self._config.temperature,
            top_p=self._config.top_p,
            stop=self._config.stop,
        )
        for entry in result:
            async_to_sync(self._output_stream.write)(
                MessagesOutput(
                    result=entry.choices[0].delta.content_str,
                ),
            )

        output = self._output_stream.finalize()

        if self._config.retain_history:
            for message in self._input.messages:
                self._chat_history.append({"role": str(message.role), "message": str(message.message)})

            self._chat_history.append({"role": "assistant", "message": output.message})

        return output
