import os
from typing import Annotated, List
from typing_extensions import TypedDict
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage, BaseMessage
from langgraph.graph import StateGraph, END
from langgraph.graph.message import add_messages

GATEWAY_URL = os.getenv("GATEWAY_URL", "http://envoy-default-envoy-ai-gateway-basic-21a9f8f8.envoy-gateway-system.svc.cluster.local")
# MODEL_ID = os.getenv("MODEL_ID", "us.anthropic.claude-sonnet-4-5-20250929-v1:0")
MODEL_ID = os.getenv("MODEL_ID", "us.meta.llama3-3-70b-instruct-v1:0")

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


def _llm() -> ChatOpenAI:
    return ChatOpenAI(
        model=MODEL_ID,
        base_url=f"{GATEWAY_URL}/v1",
        api_key="not-needed",
        timeout=60,
    )


def process_task(state: State) -> dict:
    task = TASKS[state["task_index"]]
    idx = state["task_index"] + 1
    print(f"\n[{idx}/{len(TASKS)}] Prompt: {task}")

    response = _llm().invoke(
        [SystemMessage(content=SYSTEM_PROMPT), HumanMessage(content=task)]
    )

    answer = response.content
    print(f"Response: {answer[:300]}{'...' if len(answer) > 300 else ''}")

    return {
        "messages": [HumanMessage(content=task), response],
        "task_index": state["task_index"] + 1,
        "results": state["results"] + [answer],
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
    print("=== First Agent ===")
    print(f"Gateway : {GATEWAY_URL}")
    print(f"Model   : {MODEL_ID}")
    print(f"Tasks   : {len(TASKS)}\n")

    agent = build_agent()
    final_state = agent.invoke(
        {"messages": [], "task_index": 0, "results": []}
    )

    print("\n=== All tasks completed ===")
    for i, (task, result) in enumerate(zip(TASKS, final_state["results"]), 1):
        print(f"\n--- Task {i} ---")
        print(f"Q: {task}")
        print(f"A: {result}")


if __name__ == "__main__":
    main()
