import os
from typing import Annotated, List
from typing_extensions import TypedDict
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage, BaseMessage
from langgraph.graph import StateGraph, END
from langgraph.graph.message import add_messages
import openai

GATEWAY_URL = os.getenv(
    "GATEWAY_URL",
    "http://envoy-default-envoy-ai-gateway-basic-21a9f8f8.envoy-gateway-system.svc.cluster.local",
)
PRIMARY_MODEL = os.getenv("MODEL_ID", "us.meta.llama3-3-70b-instruct-v1:0")
FALLBACK_MODEL = os.getenv("OPENAI_MODEL_ID", "gpt-4o")

# Status codes that indicate the upstream backend is the problem — trigger fallback.
FALLBACK_STATUS_CODES = {404, 409, 429, 500, 502, 503, 504}

SYSTEM_PROMPT = (
    "You are a helpful AI assistant with expertise in cloud infrastructure, "
    "Kubernetes, and AI systems. Be concise and precise."
)

TASKS = [
    "Explain Kubernetes in one short paragraph.",
    "What are the key benefits of using Envoy as a service proxy?",
    "How does AWS Bedrock simplify deploying foundation models compared to self-hosted LLMs?",
    "Write a two-sentence summary of what a LangGraph agent is.",
]


class State(TypedDict):
    messages: Annotated[List[BaseMessage], add_messages]
    task_index: int
    results: List[str]
    backends_used: List[str]


def _llm(model: str) -> ChatOpenAI:
    """Both primary and fallback go through the same Envoy AI Gateway.
    The gateway routes by the model name in the request to the correct backend
    (Bedrock for PRIMARY_MODEL, OpenAI for FALLBACK_MODEL) and injects the
    appropriate credentials via BackendSecurityPolicy."""
    return ChatOpenAI(
        model=model,
        base_url=f"{GATEWAY_URL}/v1",
        api_key="not-needed",
        timeout=30,
    )


def _should_fallback(exc: Exception) -> bool:
    if isinstance(exc, openai.APIStatusError):
        return exc.status_code in FALLBACK_STATUS_CODES
    if isinstance(exc, (openai.APIConnectionError, openai.APITimeoutError)):
        return True
    return False


def process_task(state: State) -> dict:
    task = TASKS[state["task_index"]]
    idx = state["task_index"] + 1
    messages = [SystemMessage(content=SYSTEM_PROMPT), HumanMessage(content=task)]

    print(f"\n[{idx}/{len(TASKS)}] Prompt: {task}")

    backend_used = "bedrock"
    try:
        response = _llm(PRIMARY_MODEL).invoke(messages)
        print(f"  Backend: Bedrock ({PRIMARY_MODEL})")
    except Exception as exc:
        if _should_fallback(exc):
            status = getattr(exc, "status_code", "N/A")
            print(f"  Bedrock failed (status={status}): {exc}")
            print(f"  Falling back via gateway to OpenAI ({FALLBACK_MODEL}).")
            response = _llm(FALLBACK_MODEL).invoke(messages)
            backend_used = "openai-fallback"
            print(f"  Backend: OpenAI fallback ({FALLBACK_MODEL})")
        else:
            raise

    answer = response.content
    print(f"  Response: {answer[:300]}{'...' if len(answer) > 300 else ''}")

    return {
        "messages": [HumanMessage(content=task), response],
        "task_index": state["task_index"] + 1,
        "results": state["results"] + [answer],
        "backends_used": state["backends_used"] + [backend_used],
    }


def route(state: State) -> str:
    return "process" if state["task_index"] < len(TASKS) else END


def build_agent():
    graph = StateGraph(State)
    graph.add_node("process", process_task)
    graph.set_entry_point("process")
    graph.add_conditional_edges("process", route, {"process": "process", END: END})
    return graph.compile()


def main():
    print("=== Fallback Agent ===")
    print(f"Gateway        : {GATEWAY_URL}")
    print(f"Primary model  : {PRIMARY_MODEL}")
    print(f"Fallback model : {FALLBACK_MODEL}")
    print(f"Fallback codes : {sorted(FALLBACK_STATUS_CODES)}")
    print(f"Tasks          : {len(TASKS)}\n")

    agent = build_agent()
    final_state = agent.invoke(
        {"messages": [], "task_index": 0, "results": [], "backends_used": []}
    )

    print("\n=== All tasks completed ===")
    for i, (task, result, backend) in enumerate(
        zip(TASKS, final_state["results"], final_state["backends_used"]), 1
    ):
        print(f"\n--- Task {i} [{backend}] ---")
        print(f"Q: {task}")
        print(f"A: {result}")

    bedrock_count = final_state["backends_used"].count("bedrock")
    fallback_count = final_state["backends_used"].count("openai-fallback")
    print(f"\nSummary: {bedrock_count} via Bedrock, {fallback_count} via OpenAI fallback")


if __name__ == "__main__":
    main()
