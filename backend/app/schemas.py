from pydantic import BaseModel


class IngestRequest(BaseModel):
    repo_url: str


class QueryRequest(BaseModel):
    repo_id: str
    question: str
