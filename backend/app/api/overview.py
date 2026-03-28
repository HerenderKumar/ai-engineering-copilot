from fastapi import APIRouter

from app.services.vector_store import load_vector_index
from app.services.architecture import infer_architecture
from app.services.flow import infer_execution_flow
from app.services.contribution import infer_contribution_areas
from app.services.call_graph import build_call_graph

router = APIRouter()


@router.get("/overview/{repo_id}")
def repo_overview(repo_id: str):
    _, metadata = load_vector_index(repo_id)

    return {
        "total_files_indexed": len(set(c["source"] for c in metadata)),
        "architecture": infer_architecture(metadata),
        "execution_flow": infer_execution_flow(metadata),
        "call_graph": build_call_graph(metadata),
        "contribution_guidance": infer_contribution_areas(metadata),
    }
