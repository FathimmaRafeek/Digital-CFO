import os
import json
from typing import TypedDict, Dict, Any
from langgraph.graph import StateGraph, END
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import SystemMessage, HumanMessage

class AgentCFOState(TypedDict):
    question: str
    metrics: Dict[str, Any]
    ledger_analysis: str
    risk_audit: str
    final_cfo_briefing: str

def get_llm():
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key or api_key == "your_gemini_api_key_here":
        return None
    return ChatGoogleGenerativeAI(model="gemini-2.5-flash", temperature=0.2, google_api_key=api_key)

def ledger_analyst_node(state: AgentCFOState) -> AgentCFOState:
    m = state["metrics"]
    llm = get_llm()
    if not llm:
        runway = (
            f"{m['runway_months']} months"
            if m["runway_months"] is not None
            else "not available until monthly expenses are recorded"
        )
        state["ledger_analysis"] = f"Current month spend is {m['currency']} {m['current_month_expense']:,.2f} ({m['mom_expense_change_pct']:+.1f}% vs last month). Cash runway is {runway}."
        return state
    prompt = f"Analyze metrics:\n- Current Cash: {m['currency']} {m['current_cash']}\n- Current Spend: {m['current_month_expense']}\n- MoM Change: {m['mom_expense_change_pct']}%\nProvide a 3-sentence summary."
    res = llm.invoke([SystemMessage(content="You are a Financial Controller."), HumanMessage(content=prompt)])
    state["ledger_analysis"] = res.content
    return state

def risk_auditor_node(state: AgentCFOState) -> AgentCFOState:
    m = state["metrics"]
    llm = get_llm()
    if not llm:
        state["risk_audit"] = f"Detected {len(m['anomalies'])} anomalies and {len(m['upcoming_payables'])} payables totaling {m['currency']} {m['upcoming_payables_total']:,.2f}."
        return state
    prompt = f"Audit risks:\n- Anomalies: {json.dumps(m['anomalies'])}\n- Payables: {json.dumps(m['upcoming_payables'])}\nIdentify liquidity threats."
    res = llm.invoke([SystemMessage(content="You are a Risk Auditor."), HumanMessage(content=prompt)])
    state["risk_audit"] = res.content
    return state

def executive_cfo_node(state: AgentCFOState) -> AgentCFOState:
    m = state["metrics"]
    llm = get_llm()
    if not llm:
        state["final_cfo_briefing"] = f"### Executive CFO Summary ({m['company_name']})\n1. Liquidity: {state['ledger_analysis']}\n2. Risk: {state['risk_audit']}\n*(Set GEMINI_API_KEY in .env for live AI generation)*"
        return state
    prompt = f"Question: {state['question']}\nLedger Report:\n{state['ledger_analysis']}\nRisk Report:\n{state['risk_audit']}\nAct as CFO, provide executive response with 3 action items."
    res = llm.invoke([SystemMessage(content="You are an authoritative CFO."), HumanMessage(content=prompt)])
    state["final_cfo_briefing"] = res.content
    return state

workflow = StateGraph(AgentCFOState)
workflow.add_node("ledger_analyst", ledger_analyst_node)
workflow.add_node("risk_auditor", risk_auditor_node)
workflow.add_node("executive_cfo", executive_cfo_node)
workflow.set_entry_point("ledger_analyst")
workflow.add_edge("ledger_analyst", "risk_auditor")
workflow.add_edge("risk_auditor", "executive_cfo")
workflow.add_edge("executive_cfo", END)

# This is the exact object that main.py imports
cfo_agent_graph = workflow.compile()