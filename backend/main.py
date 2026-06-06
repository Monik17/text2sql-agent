"""
Text-to-SQL Agent  — Backend
Stack : FastAPI + LangChain + LangGraph + Groq (Llama-3) + MySQL
"""

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import os
from dotenv import load_dotenv
load_dotenv()

from agent import run_sql_agent
from db import get_schema, test_connection

app = FastAPI(title="Text-to-SQL Agent API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Request / Response models ─────────────────────────────────────────────────

class QueryRequest(BaseModel):
    question: str
    db_uri: str          # e.g. mysql+pymysql://user:pass@host:3306/dbname


class ConfigRequest(BaseModel):
    db_uri: str


class QueryResponse(BaseModel):
    question: str
    sql: str
    result: list
    explanation: str
    retries: int
    error: Optional[str] = None


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/")
def root():
    return {"status": "ok", "message": "Text-to-SQL Agent is running"}


@app.post("/test-connection")
def check_connection(req: ConfigRequest):
    """Verify MySQL connection and return table list."""
    ok, info = test_connection(req.db_uri)
    if not ok:
        raise HTTPException(status_code=400, detail=info)
    return {"status": "connected", "tables": info}


@app.post("/schema")
def schema(req: ConfigRequest):
    """Return the full CREATE TABLE schema of the connected DB."""
    try:
        s = get_schema(req.db_uri)
        return {"schema": s}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/query", response_model=QueryResponse)
def query(req: QueryRequest):
    """Main endpoint: natural language → SQL → result."""
    groq_key = os.getenv("GROQ_API_KEY", "")
    if not groq_key:
        raise HTTPException(status_code=500, detail="GROQ_API_KEY not set in environment")

    result = run_sql_agent(
        question=req.question,
        db_uri=req.db_uri,
        groq_api_key=groq_key,
    )
    return result
