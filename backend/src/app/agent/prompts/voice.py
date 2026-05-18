"""Voice-optimised prompt variants — shorter, punchier responses for TTS."""


def build_voice_system_prompt(store_name: str, currency: str) -> str:
    return f"""You are Aria, voice shopping assistant for {store_name} ({currency}).
Keep all responses under 2 sentences. No markdown, no lists — plain spoken language only."""
