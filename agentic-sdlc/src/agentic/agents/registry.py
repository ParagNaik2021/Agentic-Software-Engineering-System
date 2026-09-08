"""Agent registry: name -> Agent class (Section 8.2's twelve agents).

Deliberately a leaf module: it imports base.py and every concrete agent
module, but nothing imports *it* from inside agents/ — putting this in
base.py itself created a circular import the moment anything imported a
concrete agent module directly (e.g. workflows/greenfield.py), since
base.py's own bottom-of-file registry build would try to re-import that
same, still-initializing module.
"""

from __future__ import annotations

from agentic.agents.ambiguity import AmbiguityAgent
from agentic.agents.api_contract import ApiContractAgent
from agentic.agents.architect import ArchitectAgent
from agentic.agents.base import Agent
from agentic.agents.codebase_analyst import CodebaseAnalystAgent
from agentic.agents.data_model import DataModelAgent
from agentic.agents.implementer import ImplementerAgent
from agentic.agents.planner import PlannerAgent
from agentic.agents.release_manager import ReleaseManagerAgent
from agentic.agents.requirements import RequirementsAgent
from agentic.agents.security_reviewer import SecurityReviewerAgent
from agentic.agents.technical_writer import TechnicalWriterAgent
from agentic.agents.test_engineer import TestEngineerAgent

AGENT_REGISTRY: dict[str, type[Agent]] = {
    "requirements": RequirementsAgent,
    "ambiguity": AmbiguityAgent,
    "planner": PlannerAgent,
    "codebase_analyst": CodebaseAnalystAgent,
    "architect": ArchitectAgent,
    "data_model": DataModelAgent,
    "api_contract": ApiContractAgent,
    "implementer": ImplementerAgent,
    "test_engineer": TestEngineerAgent,
    "security_reviewer": SecurityReviewerAgent,
    "technical_writer": TechnicalWriterAgent,
    "release_manager": ReleaseManagerAgent,
}
