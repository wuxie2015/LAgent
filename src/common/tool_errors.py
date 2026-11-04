def tool_error(code, message, retryable=False, **details):
    """An error is data for correction, never permission to replay a side effect."""
    return {"error": message, "error_code": code, "retryable": retryable, **details}
