"""Evaluation matrix rubric aligned with the assessment criteria."""

from utils.voice_training.schemas import MetricScore, SOPActivity

RUBRIC = [
    {
        "category": "Intent Understanding",
        "parameter": "Intent Accuracy",
        "definition": "Ability to correctly understand the customer's query.",
        "what_to_check": "Did the AI map the query to the correct intent?",
        "weight_key": "weight_intent",
    },
    {
        "category": "Resolution",
        "parameter": "Issue Resolution",
        "definition": "Ability to resolve the query correctly.",
        "what_to_check": "Was the issue solved?",
        "weight_key": "weight_resolution",
    },
    {
        "category": "SOP Compliance",
        "parameter": "Process Adherence",
        "definition": "Following the correct SOP or Knowledge Base.",
        "what_to_check": "Did the AI use the correct process?",
        "weight_key": "weight_sop",
    },
    {
        "category": "SOP Compliance",
        "parameter": "Hallucination Control",
        "definition": "Avoids generating incorrect information.",
        "what_to_check": "Any info provided outside the KB?",
        "weight_key": "weight_sop",
    },
    {
        "category": "Tone",
        "parameter": "Politeness",
        "definition": "Respectful interaction.",
        "what_to_check": "Uses polite words.",
        "weight_key": "weight_tone",
    },
    {
        "category": "Tone",
        "parameter": "Empathy",
        "definition": "Emotional acknowledgment.",
        "what_to_check": "Acknowledges the customer's issue.",
        "weight_key": "weight_tone",
    },
    {
        "category": "Tone",
        "parameter": "Professionalism",
        "definition": "Formal, structured communication.",
        "what_to_check": "No slang, clear sentences.",
        "weight_key": "weight_tone",
    },
    {
        "category": "Voice Quality",
        "parameter": "Speech Clarity",
        "definition": "Clear, understandable voice.",
        "what_to_check": "TTS clarity and pronunciation.",
        "weight_key": "weight_voice",
    },
    {
        "category": "Voice Quality",
        "parameter": "Speech Pace",
        "definition": "Speed of speech.",
        "what_to_check": "Too fast or too slow?",
        "weight_key": "weight_voice",
    },
    {
        "category": "Dialogue Management",
        "parameter": "Conversation Flow",
        "definition": "Logical flow of the conversation.",
        "what_to_check": "Smooth transitions?",
        "weight_key": "weight_dialogue",
    },
    {
        "category": "Performance",
        "parameter": "Response Time",
        "definition": "Speed of reply.",
        "what_to_check": "Delay between responses.",
        "weight_key": "weight_performance",
    },
]


def score_to_grade(percentage: float) -> str:
    if percentage >= 90:
        return "A"
    if percentage >= 80:
        return "B"
    if percentage >= 70:
        return "C"
    if percentage >= 60:
        return "D"
    return "F"
