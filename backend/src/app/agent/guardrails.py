"""Input/output guardrails — content filtering, PII redaction, topic focus."""


def check_input(text: str) -> str:
    """Return sanitised text or raise ValueError if input is blocked."""
    # TODO: implement content policy checks
    return text


def check_output(text: str) -> str:
    """Return sanitised LLM output, stripping any leaked PII or off-topic content."""
    # TODO: implement output filtering
    return text
