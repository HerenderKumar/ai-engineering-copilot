from app.services.analysis.project_summary import get_project_summary
from app.services.analysis.architecture import explain_architecture
from app.services.analysis.entrypoints import find_entry_points
from app.services.analysis.folder_responsibilities import explain_folder_responsibilities
from app.services.analysis.reading_order import suggest_reading_order
from app.services.analysis.safe_contributions import suggest_safe_contributions
from app.services.analysis.feature_flow import explain_feature_flow


def run_full_analysis(repo_path: str) -> dict:
    return {
        "project_summary": get_project_summary(repo_path),
        "architecture": explain_architecture(repo_path),
        "entry_points": find_entry_points(repo_path),
        "folder_responsibilities": explain_folder_responsibilities(repo_path),
        "reading_order": suggest_reading_order(repo_path),
        "safe_contributions": suggest_safe_contributions(repo_path),
        "feature_flow": explain_feature_flow(repo_path),
    }
