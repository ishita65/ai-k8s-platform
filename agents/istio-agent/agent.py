import os
from typing import Annotated, List

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict

GATEWAY_URL = os.getenv(
    "GATEWAY_URL",
    "http://envoy-default-envoy-ai-gateway-basic-21a9f8f8.envoy-gateway-system.svc.cluster.local",
)
MODEL_ID = os.getenv("MODEL_ID", "us.meta.llama3-3-70b-instruct-v1:0")

SYSTEM_PROMPT = (
    "You are a helpful AI assistant with deep expertise in service mesh technology, "
    "Istio, and Kubernetes networking. Be concise and technically precise."
)

TASKS = [
    "Explain how Istio sidecar injection works and what the istio-proxy container does inside a Pod.",
    "What is the difference between PERMISSIVE and STRICT mTLS modes in a PeerAuthentication resource?",
    "Why does an Istio sidecar prevent a Kubernetes Job from reaching the Completed state, and what is the standard fix?",
    "How does Istio auto-mTLS work when enableAutoMtls is true — what role does ALPN play?",
    "What is the Envoy xDS API and how does istiod use it to push configuration to sidecar proxies?",
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
    print(f"\n[{idx}/{len(TASKS)}] {task}")

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
    print("=== Istio Agent ===")
    print(f"Gateway : {GATEWAY_URL}")
    print(f"Model   : {MODEL_ID}")
    print(f"Tasks   : {len(TASKS)}")

    agent = build_agent()
    final_state = agent.invoke({"messages": [], "task_index": 0, "results": []})

    print("\n=== All tasks completed ===")
    for i, (task, result) in enumerate(zip(TASKS, final_state["results"]), 1):
        print(f"\n--- Task {i} ---")
        print(f"Q: {task}")
        print(f"A: {result}")


if __name__ == "__main__":
    main()
