"""Stable public protocol boundary.

The engine keeps a single implementation in the established modules while this
namespace provides a release-friendly import surface for schemas, wire errors,
and structured role contracts.
"""

from ..app_server import (
    AppServerClient,
    AppServerError,
    AppServerRequestError,
    AppServerTurnFailed,
    StructuredOutputProtocolError,
)
from ..contracts import (
    AUDIT_RESULT_KEYS,
    DIRECTOR_PLAN_KEYS,
    OUTPUT_PROTOCOL_VERSION,
    WORKER_RESULT_KEYS,
    contract_name,
    render_contract_keys,
)
from ..models import AuditResult, DirectorPlan, ResearchTask
from ..schema import (
    OutputSchemaCompatibilityError,
    preflight_output_schema_files,
    validate_output_schema_compatibility,
)

__all__ = [
    "AppServerClient",
    "AppServerError",
    "AppServerRequestError",
    "AppServerTurnFailed",
    "StructuredOutputProtocolError",
    "OutputSchemaCompatibilityError",
    "AUDIT_RESULT_KEYS",
    "DIRECTOR_PLAN_KEYS",
    "OUTPUT_PROTOCOL_VERSION",
    "WORKER_RESULT_KEYS",
    "AuditResult",
    "DirectorPlan",
    "ResearchTask",
    "contract_name",
    "render_contract_keys",
    "preflight_output_schema_files",
    "validate_output_schema_compatibility",
]
