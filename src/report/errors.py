"""Content failures are terminal after the report's bounded repair steps."""
from src.llm import LLMError


class ReportValidationError(LLMError):
    """Repeating the same cached generation cannot repair these issues."""

    def __init__(self, issues):
        self.issues = list(issues)
        super().__init__('После доработки отчёт содержит ошибки: ' + '; '.join(self.issues[:8]))
