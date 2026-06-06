"""
agent.py — Text-to-SQL LangGraph Agent

Graph nodes
──────────────────────────────────────────────────
1. generate_sql   : LangChain prompt + Groq LLM  → raw SQL
2. execute_sql    : Run query on MySQL via SQLAlchemy
3. grade_result   : Check if result is valid or has an error
4. fix_sql        : LangChain correction prompt  → patched SQL  (on error)
5. explain        : LangChain explanation chain  → human summary

Edges
──────────────────────────────────────────────────
generate_sql → execute_sql → grade_result
    grade_result →  (ok)   → explain → END
    grade_result →  (fail) → fix_sql → execute_sql   (max 3 retries)
    grade_result →  (max)  → explain → END            (with error note)
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional
from typing_extensions import TypedDict

from langchain_groq import ChatGroq
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser
from langchain_community.utilities import SQLDatabase
from langgraph.graph import StateGraph, END

from db import get_schema


# ── Agent State ──────────────────────────────────────────────────────────────

class SQLAgentState(TypedDict):
    question: str           # original NL question
    schema: str             # DB schema string
    sql: str                # current SQL being tried
    result: list            # rows returned by MySQL
    explanation: str        # human-friendly summary
    error: Optional[str]    # last execution error (None = success)
    retries: int            # how many fix attempts so far
    max_retries: int        # ceiling (default 3)


# ── Prompts ───────────────────────────────────────────────────────────────────

GENERATE_PROMPT = ChatPromptTemplate.from_messages([
    ("system", """You are an expert MySQL query writer.
Given the database schema below, write a single valid MySQL SELECT query that answers the user's question.

RULES:
- Output ONLY the raw SQL query, nothing else.
- No markdown, no backticks, no explanation.
- Use only tables and columns that exist in the schema.
- If the question cannot be answered from the schema, output: SELECT 'Cannot answer from available data' AS message;

SCHEMA:
{schema}
"""),
    ("human", "Question: {question}"),
])

FIX_PROMPT = ChatPromptTemplate.from_messages([
    ("system", """You are an expert MySQL query debugger.
The following SQL query produced an error. Fix it so it runs correctly on MySQL.

RULES:
- Output ONLY the corrected raw SQL query, nothing else.
- No markdown, no backticks, no explanation.

SCHEMA:
{schema}

ORIGINAL QUESTION: {question}

FAILED SQL:
{sql}

ERROR:
{error}
"""),
])

EXPLAIN_PROMPT = ChatPromptTemplate.from_messages([
    ("system", """You are a helpful data analyst.
Explain the following SQL query result to a non-technical user in 2–3 clear sentences.
Be specific about the numbers and data you see.
If there was an error, briefly say what went wrong and suggest how the user might rephrase their question.
"""),
    ("human", """Question: {question}

SQL Used:
{sql}

Result:
{result}

Error (if any): {error}
"""),
])


# ── Helper ────────────────────────────────────────────────────────────────────

def clean_sql(raw: str) -> str:
    """Strip markdown fences and extra whitespace from LLM output."""
    raw = raw.strip()
    raw = re.sub(r"^```(?:sql)?", "", raw, flags=re.IGNORECASE).strip()
    raw = re.sub(r"```$", "", raw).strip()
    return raw


# ── Graph Nodes ───────────────────────────────────────────────────────────────

def node_generate_sql(state: SQLAgentState, llm: ChatGroq) -> dict:
    """LangChain chain: schema + question → SQL."""
    chain = GENERATE_PROMPT | llm | StrOutputParser()
    raw = chain.invoke({"schema": state["schema"], "question": state["question"]})
    return {"sql": clean_sql(raw), "error": None}


def node_execute_sql(state: SQLAgentState, db: SQLDatabase) -> dict:
    """Run the SQL on MySQL and capture results or errors."""
    try:
        result = db.run(state["sql"], fetch="all")

        # db.run returns a string; parse it into a list of dicts for JSON serialisation
        if isinstance(result, str):
            # Try to safely eval the string representation of rows
            try:
                import ast
                rows = ast.literal_eval(result)
                if isinstance(rows, list):
                    return {"result": rows, "error": None}
            except Exception:
                pass
            return {"result": [{"output": result}], "error": None}

        return {"result": list(result) if result else [], "error": None}

    except Exception as e:
        return {"result": [], "error": str(e)}


def node_grade_result(state: SQLAgentState) -> str:
    """
    Conditional edge function.
    Returns 'ok', 'fix', or 'max_retries'.
    """
    if state["error"] is None:
        return "ok"
    if state["retries"] >= state["max_retries"]:
        return "max_retries"
    return "fix"


def node_fix_sql(state: SQLAgentState, llm: ChatGroq) -> dict:
    """LangChain correction chain: failed SQL + error → fixed SQL."""
    chain = FIX_PROMPT | llm | StrOutputParser()
    raw = chain.invoke({
        "schema": state["schema"],
        "question": state["question"],
        "sql": state["sql"],
        "error": state["error"],
    })
    return {
        "sql": clean_sql(raw),
        "retries": state["retries"] + 1,
        "error": None,     # reset; execute_sql will set it again if still broken
    }


def node_explain(state: SQLAgentState, llm: ChatGroq) -> dict:
    """LangChain explanation chain: results → human-readable summary."""
    chain = EXPLAIN_PROMPT | llm | StrOutputParser()
    explanation = chain.invoke({
        "question": state["question"],
        "sql": state["sql"],
        "result": str(state["result"])[:2000],   # cap to avoid token overflow
        "error": state["error"] or "None",
    })
    return {"explanation": explanation}


# ── Graph Builder ─────────────────────────────────────────────────────────────

def build_graph(llm: ChatGroq, db: SQLDatabase) -> Any:
    """Construct and compile the LangGraph state machine."""

    # Bind dependencies via closures
    def gen(state):   return node_generate_sql(state, llm)
    def exe(state):   return node_execute_sql(state, db)
    def fix(state):   return node_fix_sql(state, llm)
    def expl(state):  return node_explain(state, llm)

    builder = StateGraph(SQLAgentState)

    # Add nodes
    builder.add_node("generate_sql", gen)
    builder.add_node("execute_sql",  exe)
    builder.add_node("fix_sql",      fix)
    builder.add_node("explain",      expl)

    # Entry point
    builder.set_entry_point("generate_sql")

    # Linear edges
    builder.add_edge("generate_sql", "execute_sql")
    builder.add_edge("fix_sql",      "execute_sql")
    builder.add_edge("explain",      END)

    # Conditional edge after execute
    builder.add_conditional_edges(
        "execute_sql",
        node_grade_result,
        {
            "ok":           "explain",
            "fix":          "fix_sql",
            "max_retries":  "explain",
        },
    )

    return builder.compile()


# ── Public API ────────────────────────────────────────────────────────────────

def run_sql_agent(question: str, db_uri: str, groq_api_key: str) -> Dict[str, Any]:
    """
    Run the full Text-to-SQL LangGraph agent.
    Returns a dict with: question, sql, result, explanation, retries, error.
    """
    llm = ChatGroq(
    model="llama-3.3-70b-versatile",
    api_key=groq_api_key,
    temperature=0,
)

    db = SQLDatabase.from_uri(db_uri)
    schema = get_schema(db_uri)

    graph = build_graph(llm, db)

    initial_state: SQLAgentState = {
        "question":   question,
        "schema":     schema,
        "sql":        "",
        "result":     [],
        "explanation": "",
        "error":      None,
        "retries":    0,
        "max_retries": 3,
    }

    final = graph.invoke(initial_state)

    return {
        "question":    final["question"],
        "sql":         final["sql"],
        "result":      final["result"],
        "explanation": final["explanation"],
        "retries":     final["retries"],
        "error":       final["error"],
    }
