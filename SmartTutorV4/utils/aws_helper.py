import boto3
import json
import os
import base64
from botocore.config import Config
from dotenv import load_dotenv

load_dotenv()

# Socket bounds for the Bedrock clients built below. Vision analysis of a slide is a single long
# response, so the read timeout has to outlast the model rather than the network - the same reason
# core/runtime.py gives its shared client 120s/600s. Stated here explicitly because these used to
# come from a process-wide urllib3 patch in services/knowledge_base.py, which reached every other
# AWS client too; this module is standalone (it does not import core.runtime), so it carries its
# own copy of the numbers rather than a shared constant.
_BEDROCK_CLIENT_CONFIG = Config(
    connect_timeout=120,
    read_timeout=600,
    retries={"max_attempts": 3, "mode": "adaptive"},
)


def _clean_env(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if len(normalized) >= 2 and normalized[0] == normalized[-1] and normalized[0] in {"'", '"'}:
        normalized = normalized[1:-1].strip()
    return normalized or None


def _bedrock_client_kwargs() -> dict:
    """
    Credential kwargs for Bedrock clients.
    Temporary credentials (access key prefix ASIA…) require AWS_SESSION_TOKEN.
    """
    kwargs = {"region_name": _clean_env(os.getenv("AWS_REGION")) or "us-east-1"}
    access_key = _clean_env(os.getenv("AWS_ACCESS_KEY_ID"))
    secret_key = _clean_env(os.getenv("AWS_SECRET_ACCESS_KEY"))
    session_token = _clean_env(os.getenv("AWS_SESSION_TOKEN"))
    if access_key and secret_key:
        kwargs["aws_access_key_id"] = access_key
        kwargs["aws_secret_access_key"] = secret_key
        if session_token:
            kwargs["aws_session_token"] = session_token
    return kwargs


_DEFINITION_MARKERS = (
    "is",
    "are",
    "means",
    "refers to",
    "defined as",
)


def _has_definition_marker(sentence: str) -> bool:
    """True if sentence contains a definition marker as a whole word/phrase."""
    lower = sentence.lower()
    for marker in _DEFINITION_MARKERS:
        start = 0
        mlen = len(marker)
        while True:
            idx = lower.find(marker, start)
            if idx < 0:
                break
            before_ok = idx == 0 or not (lower[idx - 1].isalnum() or lower[idx - 1] == "_")
            after = idx + mlen
            after_ok = after >= len(lower) or not (lower[after].isalnum() or lower[after] == "_")
            if before_ok and after_ok:
                return True
            start = idx + 1
    return False


def _extract_definition_sentences(text: str, limit: int = 3) -> list[str]:
    """
    Collect sentences that contain definition markers (is/are/means/...).
    Linear scan — replaces ReDoS-prone ``[^.!?]*\\b(?:is|...)\\b[^.!?]*[.!?]``.
    """
    if not text:
        return []
    found: list[str] = []
    n = len(text)
    i = 0
    while i < n and len(found) < limit:
        while i < n and text[i] in ".!?":
            i += 1
        start = i
        while i < n and text[i] not in ".!?":
            i += 1
        if i >= n:
            break
        end = i + 1
        sentence = text[start:end]
        if _has_definition_marker(sentence):
            found.append(sentence.strip())
        i = end
    return found


def _strip_numbered_toc_markdown_links(text: str) -> str:
    """Convert ``1. [Title](#anchor)`` → ``1. Title`` (linear, Sonar S2631-safe)."""
    if not text or "](" not in text:
        return text
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        if text[i].isdigit():
            j = i
            while j < n and text[j].isdigit():
                j += 1
            if j < n and text[j] == ".":
                k = j + 1
                while k < n and text[k].isspace():
                    k += 1
                if k < n and text[k] == "[":
                    label_start = k + 1
                    label_end = text.find("]", label_start)
                    if label_end > label_start and label_end + 1 < n and text[label_end + 1] == "(":
                        url_end = text.find(")", label_end + 2)
                        if url_end > label_end + 1:
                            out.append(text[i:k])
                            out.append(text[label_start:label_end])
                            i = url_end + 1
                            continue
        out.append(text[i])
        i += 1
    return "".join(out)


def _strip_bullet_toc_markdown_links(text: str) -> str:
    """Convert ``• [Title](#anchor)`` → ``   - Title`` (linear)."""
    if not text or "](" not in text:
        return text
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in "•·":
            j = i + 1
            while j < n and text[j].isspace():
                j += 1
            if j < n and text[j] == "[":
                label_start = j + 1
                label_end = text.find("]", label_start)
                if label_end > label_start and label_end + 1 < n and text[label_end + 1] == "(":
                    url_end = text.find(")", label_end + 2)
                    if url_end > label_end + 1:
                        out.append("   - ")
                        out.append(text[label_start:label_end])
                        i = url_end + 1
                        continue
        out.append(ch)
        i += 1
    return "".join(out)


def _strip_expected_annotations(text: str) -> str:
    """
    Remove ``Expected: ...`` and ``*Expected: ...*`` lines (case-insensitive).
    Linear equivalent of the former ReDoS-prone regexes (same apply order).
    """
    if not text or "expected:" not in text.lower():
        return text

    def _pass_plain(src: str) -> str:
        out: list[str] = []
        i = 0
        n = len(src)
        lower = src.lower()
        while i < n:
            if lower.startswith("expected:", i):
                j = i + len("expected:")
                while j < n and src[j] != "\n":
                    j += 1
                if j < n and src[j] == "\n":
                    j += 1
                i = j
                continue
            out.append(src[i])
            i += 1
        return "".join(out)

    def _pass_starred(src: str) -> str:
        out: list[str] = []
        i = 0
        n = len(src)
        lower = src.lower()
        while i < n:
            if src[i] == "*" and lower.startswith("expected:", i + 1):
                j = i + 1 + len("expected:")
                while j < n and src[j].isspace() and src[j] != "\n":
                    j += 1
                star = src.find("*", j)
                nl = src.find("\n", i)
                if star >= 0 and (nl < 0 or star <= nl):
                    i = star + 1
                    if i < n and src[i] == "\n":
                        i += 1
                    continue
            out.append(src[i])
            i += 1
        return "".join(out)

    return _pass_starred(_pass_plain(text))


def get_bedrock_client():
    access_key = _clean_env(os.getenv("AWS_ACCESS_KEY_ID")) or ""
    session_token = _clean_env(os.getenv("AWS_SESSION_TOKEN"))
    if access_key.startswith("ASIA") and not session_token:
        raise ValueError(
            "AWS_ACCESS_KEY_ID looks like temporary STS credentials (ASIA…), "
            "but AWS_SESSION_TOKEN is missing. Refresh credentials and set all three "
            "values in .env, or use long-lived IAM access keys."
        )
    return boto3.client(
        "bedrock-runtime", config=_BEDROCK_CLIENT_CONFIG, **_bedrock_client_kwargs()
    )


NOVA_PRO_MODEL_ID = "amazon.nova-pro-v1:0"
_ALLOWED_NOVA_IMAGE_FORMATS = frozenset({"png", "jpeg", "gif", "webp"})
_ALLOWED_NOVA_MODEL_IDS = frozenset({NOVA_PRO_MODEL_ID})


def nova_image_format_from_media_type(media_type: str) -> str:
    return {
        "image/png": "png",
        "image/jpeg": "jpeg",
        "image/jpg": "jpeg",
        "image/gif": "gif",
        "image/webp": "webp",
    }.get((media_type or "").lower(), "png")


def nova_image_format_from_path(path: str) -> str:
    ext = os.path.splitext(path or "")[1].lower()
    if ext in {".jpg", ".jpeg"}:
        return "jpeg"
    if ext == ".gif":
        return "gif"
    if ext == ".webp":
        return "webp"
    return "png"


def extract_bedrock_usage_tokens(response_body: dict) -> tuple[int, int]:
    """Read input/output token counts from Claude, Nova invoke_model, or Converse responses."""
    if not isinstance(response_body, dict):
        return 0, 0

    usage = response_body.get("usage") or {}
    if usage.get("input_tokens") is not None or usage.get("output_tokens") is not None:
        return int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0)
    if usage.get("inputTokens") is not None or usage.get("outputTokens") is not None:
        return int(usage.get("inputTokens") or 0), int(usage.get("outputTokens") or 0)

    metrics = response_body.get("amazon-bedrock-invocationMetrics") or {}
    input_tokens = int(metrics.get("inputTokenCount") or metrics.get("inputTokens") or 0)
    output_tokens = int(metrics.get("outputTokenCount") or metrics.get("outputTokens") or 0)
    if input_tokens or output_tokens:
        return input_tokens, output_tokens

    return 0, 0


def invoke_nova_pro_vision(
    client,
    *,
    image_bytes: bytes,
    prompt: str,
    image_format: str = "png",
    model_id: str = NOVA_PRO_MODEL_ID,
    max_tokens: int = 2048,
    temperature: float = 0.3,
) -> tuple[str, dict]:
    """Invoke Amazon Nova Pro with an image + text prompt (Bedrock invoke_model API)."""
    if image_format not in _ALLOWED_NOVA_IMAGE_FORMATS:
        image_format = "png"
    if model_id not in _ALLOWED_NOVA_MODEL_IDS:
        model_id = NOVA_PRO_MODEL_ID
    max_tokens = max(1, min(int(max_tokens), 8192))
    temperature = max(0.0, min(float(temperature), 1.0))

    image_b64 = base64.b64encode(image_bytes).decode("utf-8")
    request_body = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"image": {"format": image_format, "source": {"bytes": image_b64}}},
                    {"text": str(prompt or "")},
                ],
            }
        ],
        "inferenceConfig": {
            "max_new_tokens": max_tokens,
            "temperature": temperature,
        },
    }
    response = client.invoke_model(modelId=model_id, body=json.dumps(request_body))
    response_body = json.loads(response["body"].read())
    try:
        text = response_body["output"]["message"]["content"][0]["text"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("Nova Pro vision response missing text content") from exc
    return str(text or ""), response_body


def extract_visual_cue_timestamps(transcript_text, target_count=12):
    """
    Extract chronologically ordered visual timestamps from transcript cues.
    This is transcript-driven and does not inject hardcoded timeline timestamps.
    """
    import re

    def time_to_seconds(time_str):
        parts = list(map(int, time_str.split(':')))
        if len(parts) == 2:
            return parts[0] * 60 + parts[1]
        if len(parts) == 3:
            return parts[0] * 3600 + parts[1] * 60 + parts[2]
        return 0

    def seconds_to_mmss(total_seconds):
        total_seconds = max(0, int(total_seconds))
        return f"{total_seconds // 60:02d}:{total_seconds % 60:02d}"

    strong_cues = [
        r'let me show you', r'i\'m showing', r'i\'ll show you', r'sharing my screen',
        r'screen share', r'as you can see here', r'if you look at this', r'on this screen',
        r'this screen shows', r'on my screen', r'take a look at', r'what you\'re seeing'
    ]
    medium_cues = [
        r'as you can see', r'the dashboard', r'the interface', r'the screen', r'this page',
        r'this window', r'this panel', r'this section', r'this view', r'the console',
        r'the terminal', r'the browser', r'the application', r'login', r'log in', r'sign in',
        r'landing page', r'home page'
    ]
    progression_cues = [
        r'click', r'select', r'open', r'submit', r'save', r'configure', r'create',
        r'update', r'navigate', r'next', r'then', r'after that', r'now'
    ]

    lines = transcript_text.split('\n')
    timeline = []
    for i, line in enumerate(lines):
        ts_match = re.search(r'\*\*[^*]+\*\*\s*(\d{1,2}:\d{2}(?::\d{2})?)', line)
        if not ts_match:
            continue
        ts = ts_match.group(1)
        sec = time_to_seconds(ts)
        context = ' '.join(lines[i:min(i + 4, len(lines))]).lower()
        score = 0
        if any(re.search(c, context, re.IGNORECASE) for c in strong_cues):
            score += 10
        if any(re.search(c, context, re.IGNORECASE) for c in medium_cues):
            score += 5
        if any(re.search(c, context, re.IGNORECASE) for c in progression_cues):
            score += 3
        timeline.append({"timestamp": ts, "seconds": sec, "score": score})

    if not timeline:
        return []

    timeline.sort(key=lambda x: x["seconds"])
    all_seconds = sorted(set(item["seconds"] for item in timeline))
    span = max(1, all_seconds[-1] - all_seconds[0]) if len(all_seconds) > 1 else 1
    adaptive_gap = max(6, int(span / max(target_count * 2, 1)))

    # Keep strongest cue at each second, preserve chronological order.
    best_by_second = {}
    for item in timeline:
        sec = item["seconds"]
        if sec not in best_by_second or item["score"] > best_by_second[sec]["score"]:
            best_by_second[sec] = item

    scored_timeline = [best_by_second[s] for s in sorted(best_by_second.keys())]
    selected_seconds = []
    for item in scored_timeline:
        if item["score"] <= 0:
            continue
        if not selected_seconds or (item["seconds"] - selected_seconds[-1]) >= adaptive_gap:
            selected_seconds.append(item["seconds"])
        if len(selected_seconds) >= target_count:
            break

    # Coverage fill (still transcript-driven): sample from observed transcript timestamps only.
    if len(selected_seconds) < target_count:
        required = target_count - len(selected_seconds)
        if required > 0 and all_seconds:
            step = max(1, len(all_seconds) // (required + 1))
            for idx in range(step - 1, len(all_seconds), step):
                sec = all_seconds[idx]
                if sec in selected_seconds:
                    continue
                if not selected_seconds or min(abs(sec - x) for x in selected_seconds) >= max(4, adaptive_gap // 2):
                    selected_seconds.append(sec)
                if len(selected_seconds) >= target_count:
                    break

    selected_seconds = sorted(set(selected_seconds))[:target_count]
    if selected_seconds:
        print(f"✓ Selected {len(selected_seconds)} chronological visual timestamps from transcript cues")
    return [seconds_to_mmss(sec) for sec in selected_seconds]

def extract_domain_context(context_text, full_transcript=""):
    """
    Dynamically extracts domain information from transcript without hardcoded keywords.
    Uses NLP-like patterns to identify technical concepts, entities, and discussion topics.
    """
    combined_text = (full_transcript[:5000] if full_transcript else "") + "\n\n" + (context_text[:2000] if context_text else "")
    
    if not combined_text.strip():
        return "No domain context available - analyze the screen content based on what you see."
    
    import re
    from collections import Counter
    
    # 1. Extract multi-word capitalized terms (likely product/service/concept names)
    # Pattern: Two or more capitalized words together (e.g., "Amazon Bedrock", "Visual Studio")
    caps_terms = re.findall(r'\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+\b', combined_text)
    caps_terms.extend(re.findall(r'\b[A-Z][a-z]+(?:[A-Z][a-z]+)+\b', combined_text))  # CamelCase
    
    # 2. Extract acronyms and abbreviations (2-6 uppercase letters)
    acronyms = re.findall(r'\b[A-Z]{2,6}\b', combined_text)
    
    # 3. Extract technical verbs and actions (words ending in common tech suffixes)
    tech_actions = re.findall(r'\b\w+(?:ing|ed|tion|ment|ance|ence|ity|ness)\b', combined_text.lower())
    
    # 4. Extract quoted terms (often important concepts)
    quoted_terms = re.findall(r'["\']([^"\']+)["\']', combined_text)
    
    # 5. Count frequency to identify most important terms
    all_terms = caps_terms + acronyms + quoted_terms
    term_freq = Counter(all_terms)
    top_terms = [term for term, count in term_freq.most_common(20) if count > 1]
    
    # 6. Extract sentences containing "is", "are", "means" (definitions/explanations)
    definition_sentences = _extract_definition_sentences(combined_text, limit=3)
    key_concepts = ' '.join(definition_sentences)
    
    # 7. Build dynamic context summary
    domain_summary = f"""
MEETING CONTEXT (Dynamically Extracted):

Key Terms & Concepts Discussed:
{', '.join(top_terms[:15]) if top_terms else 'Technical discussion'}

Core Definitions/Explanations from Meeting:
{key_concepts[:500] if key_concepts else 'See transcript for context'}

CRITICAL INSTRUCTION:
Your visual analysis MUST directly relate to what was actually discussed in this meeting.
- Reference specific terms from the list above
- Connect screen content to the concepts being explained
- If the screen shows something unrelated to these topics, explicitly state the mismatch
- Focus on HOW the screen content illustrates or demonstrates what was discussed
"""
    return domain_summary

def generate_notes_and_select_frames(transcript_text, frame_paths):
    """
    Uses Bedrock to generate structured notes from transcript and select relevant frames.
    
    Since we can't easily pass 100 images to the LLM at once efficiently in one go without hitting limits or high costs,
    this function will be a bit simplified. It will:
    1. Pass the transcript to get a summary.
    2. (Optional/Advanced) Pass a subset of frames to see if they match the summary.
    
    For this implementation, we will pass the transcript and ASK the LLM to identify key visual moments described,
    and then we might map those to timestamps, but without sophisticated visual matching this is hard.
    
    ALTERNATIVE: We pass the transcript and a subsample of frames (e.g. 5-10 key frames) to the multimodal model.
    """
    
    client = get_bedrock_client()
    
    # encode images
    encoded_images = []
    # Limit to first 10 frames or sampling to avoid payload too large for now
    # In a real app, we'd use a more smart selection or multiple calls.
    # We will pick up to 5 evenly spaced frames from the list to send as context.
    
    selected_indices = []
    if len(frame_paths) > 5:
        step = len(frame_paths) // 5
        selected_indices = list(range(0, len(frame_paths), step))[:5]
    else:
        selected_indices = list(range(len(frame_paths)))
        
    for i in selected_indices:
        with open(frame_paths[i], "rb") as image_file:
            encoded_string = base64.b64encode(image_file.read()).decode('utf-8')
            encoded_images.append(encoded_string)

    # Prompt construction
    # Nova format:
    # {
    #   "messages": [
    #     {
    #       "role": "user",
    #       "content": [
    #         {"image": {"format": "jpeg", "source": {"bytes": "..."}}},
    #         {"text": "..."}
    #       ]
    #     }
    #   ]
    # }
    
    content_block = []
    for img_b64 in encoded_images:
        content_block.append({
            "image": {
                "format": "jpeg", 
                "source": {"bytes": img_b64}
            }
        })
    
    content_block.append({
        "text": f"You are an expert assistant. I have a transcript of a meeting and some frames from the recording video. \n\nTRANSCRIPT:\n{transcript_text[:15000]}...\n(truncated if too long)\n\nPlease generate a comprehensive meeting minutes / study guide based on this. Also, reference the visual content from the provided images if they seem relevant. The output should be formatted as markdown."
    })

    request_body = {
        "messages": [
            {
                "role": "user",
                "content": content_block
            }
        ],
        "inferenceConfig": {
            "max_new_tokens": 4096,
            "temperature": 0.7
        }
    }

    try:
        response = client.invoke_model(
            modelId="amazon.nova-pro-v1:0",
            body=json.dumps(request_body)
        )
        
        response_body = json.loads(response.get('body').read())
        output_text = response_body['output']['message']['content'][0]['text']
        
        # We also want to return the 'relevant' frames. 
        # For simplicity in this demo, we return the frames we sent to the LLM as 'relevant'.
        relevant_frames = [frame_paths[i] for i in selected_indices]
        
        return output_text, relevant_frames

    except Exception as e:
        print(f"Error calling Bedrock: {e}")
        return f"Error generating content: {e}", []

def analyze_transcript_structure(transcript_text, custom_instructions=None):
    """
    Analyzes transcript using Amazon Nova Pro.
    Returns: Extremely detailed markdown report with [IMAGE: MM:SS] placeholders.
    Intelligently identifies timestamps where visual content is likely being shown.
    Word count is dynamic based on transcript length for comprehensive coverage.
    
    Args:
        transcript_text: The meeting transcript text
        custom_instructions: Optional custom instructions from user to guide the analysis
    """
    client = get_bedrock_client()
    
    # Calculate dynamic word count target based on transcript length
    transcript_word_count = len(transcript_text.split())
    
    # Target: 10,000-15,000 words minimum, scaling with transcript size
    # For every 1000 words of transcript, generate 2000-3000 words of report
    min_words = max(10000, int(transcript_word_count * 2))
    max_words = max(15000, int(transcript_word_count * 3))
    
    print(f"📊 Transcript: {transcript_word_count} words → Target report: {min_words}-{max_words} words")
    
    # Add custom instructions section if provided
    custom_instructions_section = ""
    if custom_instructions and custom_instructions.strip():
        custom_instructions_section = f"""

**🎯 USER'S CUSTOM INSTRUCTIONS - FOLLOW THESE CAREFULLY:**

The user has provided specific instructions for this analysis. You MUST follow these instructions while generating the report:

---
{custom_instructions.strip()}
---

Integrate these custom instructions throughout your analysis. If they specify focus areas, emphasis points, or special requirements, ensure they are reflected in your comprehensive report.
"""
        print(f"✨ Custom instructions provided: {custom_instructions[:100]}...")

    
    prompt = f"""You are an Expert Technical Documentation Specialist creating a comprehensive SOP-standard technical reference document from a software walkthrough recording.

INPUT: A transcript from a software walkthrough/demo recording with timestamps and speakers.
OUTPUT: A professional SOP-standard technical document in markdown format.

═══════════════════════════════════════════════════════════════
CRITICAL RULES — MUST FOLLOW WITHOUT EXCEPTION
═══════════════════════════════════════════════════════════════

1. **SOP LANGUAGE — NO MEETING REFERENCES**:
   - This is a TECHNICAL DOCUMENT, not meeting notes
   - NEVER write: "during the meeting", "was discussed in the meeting", "the presenter showed", "as seen in the meeting", "at this point in the meeting", "the team discussed", "it was mentioned in the meeting", "the landing page was shown"
   - ALWAYS write: "the system provides", "the interface displays", "the configuration enables", "the workflow demonstrates", "as illustrated", "the process involves", "the feature allows", "the landing page of [system] displays"
   - Write as if documenting a system for a new user reading an SOP, not summarising a meeting

2. **NO PERSONAL NAMES**:
   - Replace ALL names with system/process references
   - Use passive voice: "is configured", "is demonstrated", "is shown", "is enabled"

3. **IMAGE PLACEMENT — STRICTLY CHRONOLOGICAL ORDER**:
   - Place [IMAGE: MM:SS] tags where screen content is being demonstrated
   - ALL timestamps MUST be in strictly ascending order (01:25, 02:30, 05:45 — never reversed)
   - Aim for 15-25 images distributed throughout the document
   - Each image MUST have a 2-3 sentence SOP-standard technical description

4. **CONTEXT-IMAGE-ANALYSIS STRUCTURE** (for every image):
   - BEFORE image: 2-3 paragraphs of SOP-standard technical context
   - IMAGE tag with description
   - AFTER image: 1-2 paragraphs of technical analysis/significance

═══════════════════════════════════════════════════════════════
DOCUMENT STRUCTURE — GENERATE EXACTLY THIS FORMAT
═══════════════════════════════════════════════════════════════

# [Generate a Professional Technical Title Based on Content]

*Standard Operating Procedure — Technical Reference Document*

---

## Table of Contents

1. Executive Summary
2. System Overview & Background
3. Detailed Technical Walkthrough
4. System Architecture & Implementation
5. Security, Performance & Compliance
6. Technical Decisions & Rationale
7. Action Items & Next Steps
8. Risks & Mitigation Strategies
9. Conclusion & Recommendations

---

**CRITICAL TOC RULE**: The Table of Contents MUST be a plain numbered list ONLY.
DO NOT use markdown hyperlinks. DO NOT write [text](#anchor). DO NOT add parentheses with hash links.
CORRECT: "1. Executive Summary"
WRONG: "1. [Executive Summary](#executive-summary)"

---

## Executive Summary

[400-600 words. SOP-standard overview:
- Purpose and scope of this technical document
- Complete overview of all major system components and workflows covered
- Key technical configurations and decisions documented
- System capabilities and functional areas described
- Outcomes and recommendations
Write in formal technical paragraphs. NO meeting references.]

## System Overview & Background

- **Document Scope**: [What system/process is documented]
- **System Type**: [Application type, platform, technology stack]
- **Primary Functional Areas**: [List of main modules/features covered]
- **Technical Scope**: [Systems, components, integrations documented]

## Detailed Technical Walkthrough

[CREATE MULTIPLE SUBSECTIONS — ONE PER MAJOR FEATURE/MODULE. Place images naturally within sections.]

### [Feature/Module Name — Descriptive Technical Title]

**Overview**:
[100-150 words. SOP-standard description: What this feature/module does, its purpose in the system, business function it serves. NO meeting language.]

**Technical Details**:
[300-500 words. Comprehensive technical documentation:
- Functional description and capabilities
- Configuration options and parameters
- User interface elements and their functions
- Integration points and data flows
- Step-by-step operational procedures

CORRECT SOP IMAGE PLACEMENT EXAMPLE:

"The authentication module provides OAuth 2.0-based identity verification with support for multiple identity providers. The configuration interface enables administrators to define token expiration policies, manage client credentials, and configure provider-specific settings.

[IMAGE: 05:23]
*The OAuth 2.0 configuration panel displaying the identity provider settings, including client ID and secret fields, enabled providers list (Google, Microsoft, SAML), and JWT token configuration with expiration and refresh token settings.*

This figure demonstrates the multi-provider authentication architecture. The JWT token expiration is configurable per deployment requirement, with the refresh token window providing a balance between security and user session continuity."

NOTICE: NO "during the meeting", NO "the presenter showed", NO "was discussed". Pure SOP language.]

[IMAGE: MM:SS]
*[2-3 sentence SOP-standard technical description of the specific screen, UI elements, configuration, or data visible.]*

**Configuration & Setup**:
[100-150 words. Step-by-step configuration procedures, settings, parameters.]

**Operational Procedures**:
[100-150 words. How to use/operate this feature, workflows, user actions.]

### [Next Feature/Module]

[Continue same SOP format for each major topic. Distribute images chronologically.]

## System Architecture & Implementation

[300-400 words. Technical architecture, components, integrations, data flows.]

## Security, Performance & Compliance

[200-300 words. Security controls, performance characteristics, compliance requirements.]

## Technical Decisions & Rationale

[Numbered list. Each decision: what was decided, why, alternatives considered, trade-offs.]

## Action Items & Next Steps

[Numbered list. Each item: task description, responsible area, priority, dependencies.]

## Risks & Mitigation Strategies

[Numbered list. Each risk: description, impact level, mitigation approach.]

## Conclusion & Recommendations

[200-250 words. Summary of system capabilities documented, key recommendations, next steps.]

---

═══════════════════════════════════════════════════════════════
EXECUTION CHECKLIST — VERIFY BEFORE GENERATING
═══════════════════════════════════════════════════════════════

1. TOC is plain numbered list — NO markdown links, NO (#anchor) format
2. ALL image timestamps are in ascending chronological order
3. ZERO instances of "during the meeting", "was discussed", "the presenter", "landing page was shown"
4. Every image has a 2-3 sentence SOP-standard technical description
5. Every image has SOP-standard context before and analysis after
6. Target {min_words}-{max_words} words for comprehensive coverage
7. Formal SOP tone throughout — document a system, not a meeting
8. **COMPREHENSIVE DEPTH**: Explain concepts thoroughly with full context
{custom_instructions_section}

TRANSCRIPT:
{transcript_text}

Generate the comprehensive SOP-standard technical document now (target {min_words}-{max_words} words):"""

    request_body = {
        "messages": [
            {
                "role": "user",
                "content": [{"text": prompt}]
            }
        ],
        "inferenceConfig": {
            "max_new_tokens": 8192,
            "temperature": 0.2
        }
    }

    try:
        response = client.invoke_model(
            modelId="amazon.nova-pro-v1:0",
            body=json.dumps(request_body)
        )
        
        response_body = json.loads(response.get('body').read())
        output_text = response_body['output']['message']['content'][0]['text']
        
        # Remove word count instructions from output
        import re
        output_text = re.sub(r'\(\d+[-–]\d+\s*words?\)', '', output_text, flags=re.IGNORECASE)
        output_text = re.sub(r'\[\d+[-–]\d+\s*words?\]', '', output_text, flags=re.IGNORECASE)

        # Fix TOC — strip markdown hyperlinks, keep plain text
        # Converts "1. [Executive Summary](#executive-summary)" → "1. Executive Summary"
        output_text = _strip_numbered_toc_markdown_links(output_text)
        # Also fix bullet-style TOC entries: "• [Landing Page Overview](#...)" → "   - Landing Page Overview"
        output_text = _strip_bullet_toc_markdown_links(output_text)

        # Remove informal meeting phrases
        informal_phrases = [
            r'[Dd]uring the meeting[,.]?\s*',
            r'[Ww]as discussed in the meeting[,.]?\s*',
            r'[Aa]s seen in the meeting[,.]?\s*',
            r'[Aa]t this point in the meeting[,.]?\s*',
            r'[Tt]he meeting covered[,.]?\s*',
            r'[Ii]n the meeting[,.]?\s*',
            r'[Tt]he team discussed[,.]?\s*',
            r'[Ii]t was mentioned[,.]?\s*',
        ]
        for phrase in informal_phrases:
            output_text = re.sub(phrase, '', output_text)
        
        # Dynamic name removal
        speaker_pattern = r'\*\*([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\*\*\s*\d+:\d+'
        names_found = list(set(re.findall(speaker_pattern, transcript_text)))
        
        role_replacements = [
            'the presenter', 'the technical lead', 'the architect', 'the developer',
            'the engineer', 'one team member', 'another participant', 'the project manager',
            'the specialist', 'a colleague'
        ]
        
        for idx, name in enumerate(names_found):
            if len(name.split()) >= 2:
                replacement = role_replacements[idx % len(role_replacements)]
                output_text = re.sub(r'\b' + re.escape(name) + r'\b', replacement, output_text, flags=re.IGNORECASE)
        
        # If AI didn't generate enough image tags, add intelligent ones based on transcript analysis
        image_count = len(re.findall(r'\[IMAGE:', output_text, re.IGNORECASE))
        
        if image_count < 15:  # Increased from 8 to 15
            print(f"⚠️ AI generated only {image_count} images. Adding intelligent timestamps based on transcript analysis...")
            additional_timestamps = extract_visual_cue_timestamps(transcript_text, target_count=20)  # Increased from 12 to 20
            
            if additional_timestamps and len(additional_timestamps) > 0:
                # Insert timestamps naturally into the document, not as a separate section
                # Find the last major section before conclusion
                sections = re.split(r'\n## ', output_text)
                if len(sections) > 2:
                    # Insert before the last 2 sections (usually Action Items and Conclusion)
                    main_content = '\n## '.join(sections[:-2])
                    ending_sections = '\n## '.join(sections[-2:])
                    
                    # Add timestamps with better descriptions - NO "Expected:" prefix
                    additional_content = "\n\n### Additional Visual Demonstrations\n\n"
                    additional_content += "The following moments captured key visual demonstrations during the meeting:\n\n"
                    
                    for idx, ts in enumerate(additional_timestamps[:20 - image_count], 1):
                        additional_content += f"[IMAGE: {ts}]\n"
                        additional_content += f"*Visual demonstration #{idx} showing screen content relevant to the discussion at this point in the meeting. The screen likely displays technical configurations, code, diagrams, or interface elements that were being discussed.*\n\n"
                    
                    output_text = main_content + additional_content + '\n## ' + ending_sections
                else:
                    # Fallback: append at the end
                    output_text += "\n\n### Additional Visual Demonstrations\n\n"
                    for idx, ts in enumerate(additional_timestamps[:20 - image_count], 1):
                        output_text += f"[IMAGE: {ts}]\n"
                        output_text += f"*Visual demonstration #{idx} showing screen content relevant to the discussion at this point.*\n\n"
        
        return output_text

    except Exception as e:
        print(f"Error structuring transcript: {e}")
        return f"# Error\nCould not analyze transcript: {e}"


def get_nova_image_description(image_path, context_text="", full_transcript=""):
    """
    Generates SOP-standard caption and visual analysis for a screenshot.
    """
    client = get_bedrock_client()
    
    with open(image_path, "rb") as f:
        img_bytes = f.read()
    
    encoded_image = base64.b64encode(img_bytes).decode('utf-8')
    
    # Extract domain context
    domain_context = extract_domain_context(context_text, full_transcript)
    
    prompt = f"""You are a Technical Documentation Specialist creating SOP-standard documentation for a software walkthrough.

BACKGROUND CONTEXT (use only if it matches what you actually see in the image):
{domain_context}

TRANSCRIPT CONTEXT (immediately before this screenshot — use as background only):
{context_text[-1000:] if context_text else 'No context available'}

YOUR TASK:
Carefully examine the screenshot and generate a professional SOP-standard caption and visual analysis.

⚠️ CRITICAL ACCURACY RULE:
Your description MUST be based on what is ACTUALLY VISIBLE in the screenshot.
If the image shows an email client (e.g. Outlook), describe the email client.
If the image shows a dashboard, describe the dashboard.
If the image shows a terminal, describe the terminal.
DO NOT describe something from the transcript context if it does not match what you see.
The transcript context is provided only as background — it does NOT override what is visually present.

STRICT RULES:
1. **VISUAL ACCURACY FIRST**: Describe only what is actually visible in the screenshot
2. **SOP PROFESSIONAL TONE**: Write as formal Standard Operating Procedure documentation
3. **NO INFORMAL PHRASES**: NEVER write "during the meeting", "was discussed in the meeting", "the presenter showed"
4. **PASSIVE TECHNICAL VOICE**: Use "is displayed", "illustrates", "demonstrates", "shows", "depicts"
5. **SPECIFIC UI ELEMENTS**: Name exact panels, buttons, fields, charts, tables, application names visible
6. **NO ROLE LABELS**: Never use "the presenter", "the trainer", "the speaker"

OUTPUT FORMAT (follow exactly):

**Caption:** [One precise technical sentence, 15-25 words, describing the SPECIFIC APPLICATION/INTERFACE actually visible in the screenshot. Name the exact application, module, or screen you can see.]

**Analysis:**

[One comprehensive SOP-standard paragraph, 120-160 words describing:
- Sentence 1-2: What application/interface is shown and its key visible elements
- Sentence 3-4: Technical details of the visible content, data, or workflow
- Sentence 5-6: Functional significance of what is shown
- Sentence 7: How this relates to the broader process being documented

Use ONLY: "this figure illustrates", "the figure shows", "as depicted", "as displayed in this figure", "the interface provides"
NEVER use: "during the meeting", "was discussed", "the presenter", "as seen in the meeting"]

NOW ANALYZE THIS SCREENSHOT — describe what you actually see:"""

    request_body = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "image": {
                            "format": "jpeg", 
                            "source": {"bytes": encoded_image}
                        }
                    },
                    {"text": prompt}
                ]
            }
        ],
        "inferenceConfig": {
            "max_new_tokens": 800,
            "temperature": 0.1,
            "top_p": 0.9
        }
    }

    try:
        response = client.invoke_model(
            modelId="amazon.nova-pro-v1:0",
            body=json.dumps(request_body)
        )
        response_body = json.loads(response.get('body').read())
        description = response_body['output']['message']['content'][0]['text']
        
        # Clean up
        import re
        description = re.sub(r'\(\d+[-–]\d+\s*words?\)', '', description, flags=re.IGNORECASE)
        description = re.sub(r'\[\d+[-–]\d+\s*words?\]', '', description, flags=re.IGNORECASE)
        
        # Remove any "Expected:" text
        description = _strip_expected_annotations(description)
        
        # Remove informal meeting phrases
        informal_phrases = [
            r'[Dd]uring the meeting[,.]?\s*',
            r'[Ww]as discussed in the meeting[,.]?\s*',
            r'[Aa]s seen in the meeting[,.]?\s*',
            r'[Aa]t this point in the meeting[,.]?\s*',
            r'[Tt]he meeting covered[,.]?\s*',
            r'[Ii]n the meeting[,.]?\s*',
        ]
        for phrase in informal_phrases:
            description = re.sub(phrase, '', description)
        
        # Ensure Analysis starts on new line
        description = re.sub(r'\*\*Analysis:\*\*\s*([A-Z])', r'**Analysis:**\n\n\1', description)
        description = re.sub(r'\*\*Caption:\*\*\s*([A-Z])', r'**Caption:** \1', description)
        
        return description.strip()
        
    except Exception as e:
        print(f"Error getting image description: {e}")
        return f"**Caption:** Screenshot captured at this timestamp.\n\n**Analysis:**\n\nThis figure depicts the system interface at the relevant point in the workflow."

    request_body = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "image": {
                            "format": "jpeg", 
                            "source": {"bytes": encoded_image}
                        }
                    },
                    {"text": prompt}
                ]
            }
        ],
        "inferenceConfig": {
            "max_new_tokens": 800,  # Reduced for concise output
            "temperature": 0.1,
            "top_p": 0.9
        }
    }

    try:
        response = client.invoke_model(
            modelId="amazon.nova-pro-v1:0",
            body=json.dumps(request_body)
        )
        response_body = json.loads(response.get('body').read())
        description = response_body['output']['message']['content'][0]['text']
        
        # Clean up word count markers
        import re
        description = re.sub(r'\(\d+[-–]\d+\s*words?\)', '', description, flags=re.IGNORECASE)
        description = re.sub(r'\[\d+[-–]\d+\s*words?\]', '', description, flags=re.IGNORECASE)
        
        # Remove informal meeting phrases
        informal_phrases = [
            r'[Dd]uring the meeting[,.]?\s*',
            r'[Ww]as discussed in the meeting[,.]?\s*',
            r'[Aa]s seen in the meeting[,.]?\s*',
            r'[Aa]t this point in the meeting[,.]?\s*',
            r'[Tt]he meeting covered[,.]?\s*',
            r'[Ii]n the meeting[,.]?\s*',
        ]
        for phrase in informal_phrases:
            description = re.sub(phrase, '', description)
        
        # Ensure Analysis starts on new line
        description = re.sub(r'\*\*Analysis:\*\*\s*([A-Z])', r'**Analysis:**\n\n\1', description)
        description = re.sub(r'\*\*Caption:\*\*\s*([A-Z])', r'**Caption:** \1', description)
        description = re.sub(r'\n{3,}', '\n\n', description)
        
        return description.strip()
        
    except Exception as e:
        print(f"Error describing image: {e}")
        return "**Caption:** Screenshot captured at this timestamp.\n\n**Analysis:**\n\nThis figure depicts the system interface at the relevant point in the workflow."

def validate_frame_content(image_path):
    """
    Smart validation - accepts screen shares with avatars on the side.
    Returns: True if main screen share content visible (40%+), False if only gallery/faces
    """
    client = get_bedrock_client()
    
    with open(image_path, "rb") as f:
        img_bytes = f.read()
    encoded_image = base64.b64encode(img_bytes).decode('utf-8')
    
    # Smart prompt - focus on MAIN content, not side avatars
    prompt = """Analyze this Teams meeting frame.

CRITICAL RULE: If there is MAIN screen share content (browser, slide, code, dashboard, diagram, document), the frame is VALID even if there are small circular avatars or faces on the RIGHT SIDE.

QUESTIONS:
Q1. Is there MAIN screen share content visible (browser window, presentation slide, code editor, dashboard, diagram, document, application interface, login page, landing page)? [YES/NO]
Q2. What percentage of the screen shows this MAIN content? [0-100]
Q3. Is this ONLY a participant gallery view with NO screen share content at all? [YES/NO]

DECISION RULES:
- If Q3=YES (only gallery, no content) -> INVALID
- If Q1=YES AND Q2>=25 -> VALID (accept even if avatars/faces visible on the side)
- If Q1=NO (no main content) -> INVALID
- Otherwise -> INVALID

IMPORTANT: 
- Circular avatars with initials (SM, AS, PT, RB, DB) on the RIGHT SIDE are OK if there's main content on the LEFT
- Small participant video feeds on the side are OK if there's main screen share content
- Focus on whether MAIN CONTENT is being shared, not on whether people are visible

OUTPUT FORMAT (be precise):
Q1 Main Content: [YES/NO]
Q2 Content %: [Number]
Q3 Only Gallery: [YES/NO]
Decision: [VALID/INVALID]"""
    
    request_body = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "image": {
                            "format": "jpeg", 
                            "source": {"bytes": encoded_image}
                        }
                    },
                    {"text": prompt}
                ]
            }
        ],
        "inferenceConfig": {
            "max_new_tokens": 300,
            "temperature": 0.0
        }
    }

    try:
        response = client.invoke_model(
            modelId="amazon.nova-pro-v1:0",
            body=json.dumps(request_body)
        )
        response_body = json.loads(response.get('body').read())
        full_text = response_body['output']['message']['content'][0]['text'].strip().upper()
        
        # Parse responses - focus on main content presence
        has_main_content = "Q1 MAIN CONTENT: YES" in full_text or "MAIN CONTENT: YES" in full_text or "Q1: YES" in full_text
        only_gallery = "Q3 ONLY GALLERY: YES" in full_text or "ONLY GALLERY: YES" in full_text or "Q3: YES" in full_text
        
        # Extract percentage if available
        import re
        percent_match = re.search(r'Q2.*?(\d+)', full_text)
        content_percent = int(percent_match.group(1)) if percent_match else 0
        
        # Reject ONLY if it's pure gallery with no content
        if only_gallery:
            print(f"❌ REJECTED: Only gallery view, no screen share content")
            return False
        
        # Accept if has main content with sufficient percentage
        if has_main_content and content_percent >= 25:
            print(f"✅ ACCEPTED: Screen share content ({content_percent}% - avatars on side are OK)")
            return True
        
        # Accept if has main content even without clear percentage
        if has_main_content:
            print(f"✅ ACCEPTED: Screen share content detected (avatars on side are OK)")
            return True
        
        # Check decision as fallback
        is_valid = "DECISION: VALID" in full_text or "DECISION:VALID" in full_text
        
        if is_valid:
            print(f"✅ ACCEPTED: Valid content")
            return True
        else:
            print(f"❌ REJECTED: No main screen share content detected")
            return False
            
    except Exception as e:
        print(f"❌ Error validating frame: {e}")
        return False



def process_image_async(args):
    """
    Process a single image (for parallel execution).
    Returns: (success, figure_num, timestamp, description, frame_path)
    """
    frame_path, context_chunk, full_transcript, figure_num, timestamp = args
    
    try:
        if not validate_frame_content(frame_path):
            return (False, figure_num, timestamp, None, frame_path)
        
        description = get_nova_image_description(
            frame_path,
            context_text=context_chunk,
            full_transcript=full_transcript
        )
        
        return (True, figure_num, timestamp, description, frame_path)
    except Exception as e:
        print(f"Error processing image at {timestamp}: {e}")
        return (False, figure_num, timestamp, None, frame_path)

def batch_process_images_parallel(image_tasks, max_workers=5):
    """
    Process multiple images in parallel using ThreadPoolExecutor.
    image_tasks: List of (frame_path, context, transcript, figure_num, timestamp)
    Returns: List of results
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    
    results = []
    
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_task = {executor.submit(process_image_async, task): task for task in image_tasks}
        
        for future in as_completed(future_to_task):
            try:
                result = future.result()
                results.append(result)
            except Exception as e:
                print(f"Error in parallel processing: {e}")
    
    return results
