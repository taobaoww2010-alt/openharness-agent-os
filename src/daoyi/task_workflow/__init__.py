"""Task Workflow Engine — learn, cache, and reuse execution patterns.

Instead of blindly forwarding every user request to the LLM with all 43 tools
and the entire system prompt, this engine:
  1. Classifies the task type from user input (keyword/regex, no LLM call).
  2. Looks up a matching workflow template in the local registry.
  3. Executes the workflow phase-by-phase, sending only the tools + context
     needed for each phase (much smaller prompt → much faster inference).
  4. After a normal agent-loop completes, automatically extracts the
     execution pattern and saves it as a new workflow template.
"""

from daoyi.task_workflow.registry import WorkflowRegistry, get_workflow_registry
from daoyi.task_workflow.executor import WorkflowExecutor
from daoyi.task_workflow.learner import ToolDiscoverer, WorkflowLearner
from daoyi.task_workflow.classifier import TaskClassifier

__all__ = [
    "WorkflowRegistry",
    "get_workflow_registry",
    "WorkflowExecutor",
    "WorkflowLearner",
    "ToolDiscoverer",
    "TaskClassifier",
]
