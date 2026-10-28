from __future__ import annotations

from collections import Counter

from core.runtime import *
from utils.security_helpers import log_safe


def resize_image_if_needed(image_bytes: bytes, max_dimension: int = 7900) -> bytes:
    if not PIL_AVAILABLE:
        logger.error("[RESIZE ERROR] PIL/Pillow not available - cannot resize image")
        raise Exception("PIL/Pillow not installed - cannot resize large images")

    try:
        img = Image.open(io.BytesIO(image_bytes))
        width, height = img.size
        if width <= max_dimension and height <= max_dimension:
            return image_bytes

        if width > height:
            new_width = max_dimension
            new_height = int((max_dimension / width) * height)
        else:
            new_height = max_dimension
            new_width = int((max_dimension / height) * width)

        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")

        img_resized = img.resize((new_width, new_height), Image.Resampling.LANCZOS)
        output = io.BytesIO()
        img_resized.save(output, format="PNG", optimize=True)
        output.seek(0)
        return output.read()
    except Exception as e:
        logger.error("[RESIZE ERROR] Failed to resize image: %s", e, exc_info=True)
        raise Exception(f"Image resize failed: {e}")


def extract_module_count_with_llm(user_prompt: str) -> Optional[int]:
    logger.info("[MODULE COUNT] User prompt: %s", user_prompt)
    user_prompt_lower = user_prompt.lower()

    combine_single_pattern = r"(?:combine|merge|join)\s+.+?\s+into\s+(?:a\s+)?(?:single|one|1)\s+module"
    if re.search(combine_single_pattern, user_prompt_lower):
        return 1

    regex_result = None
    all_numbers = []
    numeric_patterns = [
        r"create\s+(\d+)\s+(?:modules?|sections?|parts?|chapters?)",
        r"generate\s+(\d+)\s+(?:modules?|sections?|parts?|chapters?)",
        r"make\s+(\d+)\s+(?:modules?|sections?|parts?|chapters?)",
        r"(?:need|want|exactly)\s+(\d+)\s+(?:modules?|sections?|parts?)",
        r"total\s+(?:of\s+)?(\d+)\s+(?:modules?|sections?|parts?)",
        r"(?:only|just)\s+(\d+)\s+(?:modules?|sections?|parts?)",
        r"reduce\s+to\s+(\d+)\s+(?:modules?|sections?|parts?)",
        r"split\s+into\s+(\d+)\s+(?:modules?|sections?|parts?)",
    ]
    for pattern in numeric_patterns:
        for match in re.findall(pattern, user_prompt_lower):
            num = int(match)
            if 1 <= num <= 20:
                all_numbers.append(num)

    word_to_num = {
        "one": 1,
        "two": 2,
        "three": 3,
        "four": 4,
        "five": 5,
        "six": 6,
        "seven": 7,
        "eight": 8,
        "nine": 9,
        "ten": 10,
        "eleven": 11,
        "twelve": 12,
        "thirteen": 13,
        "fourteen": 14,
        "fifteen": 15,
        "sixteen": 16,
        "seventeen": 17,
        "eighteen": 18,
        "nineteen": 19,
        "twenty": 20,
    }
    words_regex = "|".join(word_to_num.keys())
    word_patterns = [
        r"create\s+({words})\s+(?:modules?|sections?|parts?|chapters?)",
        r"generate\s+({words})\s+(?:modules?|sections?|parts?|chapters?)",
        r"make\s+({words})\s+(?:modules?|sections?|parts?|chapters?)",
        r"(?:need|want|exactly)\s+({words})\s+(?:modules?|sections?|parts?)",
        r"total\s+(?:of\s+)?({words})\s+(?:modules?|sections?|parts?)",
        r"split\s+into\s+({words})\s+(?:modules?|sections?|parts?)",
    ]
    for pattern_template in word_patterns:
        pattern = pattern_template.format(words=words_regex)
        for match in re.findall(pattern, user_prompt_lower):
            all_numbers.append(word_to_num[match])

    if all_numbers:
        regex_result = Counter(all_numbers).most_common(1)[0][0]

    llm_result = None
    try:
        extraction_prompt = f"""You are a helpful assistant that extracts information from user instructions.

The user has provided the following instruction for regenerating course modules:

"{user_prompt}"

Your task: Extract ONLY the number of OUTPUT modules the user wants to end up with after regeneration.

Rules:
- "combine module 1 and 2 into single module" -> 1
- "create 3 modules" -> 3
- If no specific OUTPUT count is mentioned, return: 0
- Do NOT count module numbers used for selection.

Return ONLY a single number (0-20). Do not include any explanation.

Number:"""

        response = sync_bedrock_client.converse(
            modelId="amazon.nova-pro-v1:0",
            messages=[{"role": "user", "content": [{"text": extraction_prompt}]}],
            inferenceConfig={"maxTokens": 10, "temperature": 0.1},
        )
        result_text = response["output"]["message"]["content"][0]["text"].strip()
        numbers = re.findall(r"\d+", result_text)
        if numbers:
            count = int(numbers[0])
            if 1 <= count <= 20:
                llm_result = count
    except Exception as e:
        logger.error("[MODULE COUNT] LLM extraction failed: %s", e)

    if regex_result and llm_result:
        return llm_result if regex_result != llm_result else regex_result
    if regex_result:
        return regex_result
    if llm_result:
        return llm_result
    return None


# This is a shortened extraction of the active tutor prompt so extracted services can compile now.
TUTOR_AGENT_INSTRUCTIONS = """You are Professor Anya, a world-class educator, storyteller, and mentor.

CRITICAL RULES:
1. Start with a [MODULE_OVERVIEW] block containing TITLE, WHAT_WE_COVER, and WHY_IT_MATTERS.
2. Follow the module hierarchy in strict order; do not skip sections or rearrange section flow.
3. Cover every concept, data point, process, formula, table, and example in the module.
4. For each major concept, explain the definition, mechanism, real-world example, analogy, why it matters, and a common misconception.
5. Convert tables into rich narrative explanations instead of reading them as tables.
6. Open with a hook that creates urgency and comes from the actual module content.
7. When the module contains numeric data, comparisons, rankings, percentages, or measurable metrics, generate an [INTERACTIVE_CHART] block.
8. Generate polished [INTERACTIVE_HTML] widgets for processes, comparisons, architectures, timelines, and concept lists.
9. Use ONLY exact [IMAGE_URL: ...] tags already present in the content when referencing images.
10. Include EVERY [IMAGE_URL: ...] tag from the module content exactly once, in the same order.
11. For each visual: place [IMAGE_URL: ...] first, then explain that visual immediately in the next 1-2 short paragraphs.
12. NEVER output placeholder/meta text like "Loading diagram", "Generating image", "Please wait for image", or "Image will appear".
13. Do not invent details that are not present in module content or visual-analysis context.
14. If information is missing, state it is not specified rather than guessing.
15. Do not produce a document summary, table of contents, document classification, or phase headers.

Deliver a flowing, spoken-style lecture that feels alive and highly instructive."""


def _aws_bedrock_model(**kwargs) -> AwsBedrock:
    """Give Agno the configured client, not the bare session (see the copy at the bottom of this file)."""
    return AwsBedrock(client=sync_bedrock_client, aws_region=AWS_REGION, **kwargs)


title_agent = Agent(model=_aws_bedrock_model(id=BEDROCK_GENERATION_MODEL, max_tokens=60, temperature=0.3))
report_agent = Agent(
    model=_aws_bedrock_model(
        id=BEDROCK_GENERATION_MODEL,
        max_tokens=DEFAULT_REPORT_LLM_MAX_TOKENS,
        temperature=DEFAULT_REPORT_LLM_TEMPERATURE,
    )
)
custom_instruction_agent = Agent(
    model=_aws_bedrock_model(
        id=BEDROCK_GENERATION_MODEL,
        max_tokens=DEFAULT_REPORT_LLM_MAX_TOKENS,
        temperature=DEFAULT_REPORT_LLM_TEMPERATURE,
    )
)
regeneration_agent = Agent(
    model=_aws_bedrock_model(
        id=BEDROCK_GENERATION_MODEL,
        max_tokens=DEFAULT_REPORT_LLM_MAX_TOKENS,
        temperature=DEFAULT_REPORT_LLM_TEMPERATURE,
    )
)
smart_tutor_agent = Agent(
    model=_aws_bedrock_model(
        id=BEDROCK_GENERATION_MODEL,
        max_tokens=CHAT_LLM_MAX_TOKENS,
        temperature=CHAT_LLM_TEMPERATURE,
    )
)
tutor_agent = Agent(
    model=_aws_bedrock_model(
        id=BEDROCK_GENERATION_MODEL,
        max_tokens=DEFAULT_REPORT_LLM_MAX_TOKENS,
        temperature=0.4,
    )
)
question_generation_agent = Agent(
    model=_aws_bedrock_model(id=BEDROCK_GENERATION_MODEL, max_tokens=8192, temperature=0.5)
)
answer_validation_agent = Agent(
    model=_aws_bedrock_model(id=BEDROCK_GENERATION_MODEL, max_tokens=1024, temperature=0.1)
)


def analyze_image_with_vision_bytes(image_bytes: bytes, context_before: str, context_after: str, s3_key: str = "") -> str:
    from utils.aws_helper import invoke_nova_pro_vision, nova_image_format_from_path

    try:
        image_bytes = resize_image_if_needed(image_bytes, max_dimension=7900)
        image_format = nova_image_format_from_path(s3_key)
        vision_prompt = f"""You are an expert image analyst. Analyze this image in the context of the surrounding module content.

CONTEXT BEFORE IMAGE:
{context_before[-1000:] if len(context_before) > 1000 else context_before}

CONTEXT AFTER IMAGE:
{context_after[:1000] if len(context_after) > 1000 else context_after}

Focus on the type of visualization, visible elements, labels, values, relationships, layout, and key insights.
Be specific, accurate, and contextual."""

        analysis, response_body = invoke_nova_pro_vision(
            sync_bedrock_client,
            image_bytes=image_bytes,
            prompt=vision_prompt,
            image_format=image_format,
            model_id=BEDROCK_IMAGE_MODEL,
            max_tokens=2000,
            temperature=0.3,
        )
        try:
            from services.token_tracking import record_vision_tokens_from_response

            record_vision_tokens_from_response(response_body)
        except Exception as track_exc:
            logger.debug("Vision token tracking skipped: %s", log_safe(track_exc))
        return analysis
    except Exception as exc:
        logger.error("[VISION] Error analyzing image: %s", log_safe(exc), exc_info=True)
        return "[Image analysis unavailable.]"


def generate_course_title_from_content(content: str, source_docs: List[str], avoid_title: str = None) -> str:
    try:
        content_sample = content[:8000] if len(content) > 8000 else content
        doc_list = "\n".join([f"  - {doc}" for doc in source_docs])
        avoid_instruction = (
            f"\n\nIMPORTANT: Do NOT generate this title: '{avoid_title}'\n"
            "Create a DIFFERENT title with a fresh perspective."
            if avoid_title
            else ""
        )
        prompt = f"""You are an expert educational content curator. Analyze the following course content and generate a PROFESSIONAL, MEANINGFUL course title.

Number of Source Documents: {len(source_docs)}
Source Documents:
{doc_list}

Content Sample (from all modules):
{content_sample}

Requirements:
1. Length: 40-70 characters
2. Use proper title case
3. Reflect the actual topics present
4. No quotes, generic filler, or invented claims{avoid_instruction}

Return ONLY the title text."""

        response = sync_bedrock_client.converse(
            modelId="amazon.nova-pro-v1:0",
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            inferenceConfig={"maxTokens": 150, "temperature": 0.7},
        )
        title = response["output"]["message"]["content"][0]["text"].strip()
        title = title.strip("\"'").strip().replace("\n", " ").replace("  ", " ")
        if len(title) < 10:
            raise ValueError("Title too short")
        if len(title) > 80:
            title = title[:77].rsplit(" ", 1)[0] + "..."
        if title.isupper() or title.islower():
            title = title.title()
        return title
    except Exception as e:
        logger.error("Error generating course title: %s", e)
        if len(source_docs) == 1:
            title = source_docs[0].replace("_", " ").replace("-", " ").rsplit(".", 1)[0].title()
        else:
            keywords = []
            for doc in source_docs:
                name = doc.replace("_", " ").replace("-", " ").rsplit(".", 1)[0]
                keywords.extend([w for w in name.split() if len(w) > 3][:2])
            title = " ".join(keywords[:4]).title() + " and Related Topics" if keywords else f"Multi-Document Course ({len(source_docs)} Documents)"
        if len(title) > 70:
            title = title[:67] + "..."
        return title


def fix_malformed_json(json_string: str) -> str:
    cleaned = json_string.replace("\r\n", "\\n").replace("\r", "\\n").replace("\n", "\\n")
    cleaned = cleaned.replace("\t", "\\t")
    result = []
    i = 0
    n = len(cleaned)
    in_string = False

    while i < n:
        ch = cleaned[i]
        if in_string:
            if ch == "\\":
                if i + 1 < n:
                    nxt = cleaned[i + 1]
                    if nxt in ('"', "\\", "/", "b", "f", "n", "r", "t"):
                        result.append(ch)
                        result.append(nxt)
                        i += 2
                    elif nxt == "u" and i + 5 < n:
                        hex_digits = cleaned[i + 2 : i + 6]
                        if len(hex_digits) == 4 and all(c in "0123456789abcdefABCDEF" for c in hex_digits):
                            result.append(ch)
                            result.append(nxt)
                            result.append(hex_digits)
                            i += 6
                        else:
                            result.append("\\\\")
                            i += 1
                    else:
                        result.append("\\\\")
                        i += 1
                else:
                    result.append("\\\\")
                    i += 1
            elif ch == '"':
                in_string = False
                result.append(ch)
                i += 1
            else:
                result.append(ch)
                i += 1
        else:
            if ch == '"':
                in_string = True
            result.append(ch)
            i += 1

    return "".join(result)


def extract_modules_from_text(response_content: str, doc_name: str) -> list:
    import re as _re

    def _read_json_string(s: str, start_idx: int) -> tuple[str, int]:
        if start_idx >= len(s) or s[start_idx] != '"':
            return "", start_idx
        i = start_idx + 1
        buf = []
        while i < len(s):
            ch = s[i]
            if ch == "\\" and i + 1 < len(s):
                nxt = s[i + 1]
                if nxt == "n":
                    buf.append("\n")
                elif nxt == "t":
                    buf.append("\t")
                elif nxt == "r":
                    buf.append("\r")
                elif nxt == '"':
                    buf.append('"')
                elif nxt == "\\":
                    buf.append("\\")
                else:
                    buf.append(nxt)
                i += 2
            elif ch == '"':
                return "".join(buf), i + 1
            else:
                buf.append(ch)
                i += 1
        return "".join(buf), i

    modules = []
    seg_hits = list(_re.finditer(r'"seg_index"\s*:\s*(\d+)', response_content))
    if seg_hits:
        for idx, hit in enumerate(seg_hits):
            seg_idx = int(hit.group(1))
            chunk_start = hit.start()
            chunk_end = seg_hits[idx + 1].start() if idx + 1 < len(seg_hits) else len(response_content)
            chunk = response_content[chunk_start:chunk_end]
            title = f"Module {seg_idx}"
            tm = _re.search(r'"title"\s*:\s*"([^"]{1,300})"', chunk)
            if tm:
                title = tm.group(1)
            content = ""
            cm = _re.search(r'"content"\s*:\s*"', chunk)
            if cm:
                content, _ = _read_json_string(chunk, cm.end() - 1)
            if content or title != f"Module {seg_idx}":
                modules.append({"seg_index": seg_idx, "title": title, "content": content})
        if modules:
            return modules

    title_hits = list(_re.finditer(r'"title"\s*:\s*"', response_content))
    if title_hits:
        for title_hit in title_hits:
            title, after_title = _read_json_string(response_content, title_hit.end() - 1)
            if not title:
                continue
            content_key_match = _re.search(r'"content"\s*:\s*"', response_content[after_title:])
            if not content_key_match:
                continue
            content_start_quote = after_title + content_key_match.end() - 1
            content, _ = _read_json_string(response_content, content_start_quote)
            if content.strip():
                modules.append({"seg_index": len(modules) + 1, "title": title.strip(), "content": content})
        if modules:
            return modules

    section_hits = list(_re.finditer(r"(?:^|\n)#+\s+(.+)|(?:^|\n)(\d+)\.\s+(.+)", response_content))
    for i, match in enumerate(section_hits):
        title = (match.group(1) or match.group(3) or f"Section {i + 1}").strip()
        start = match.end()
        end = section_hits[i + 1].start() if i + 1 < len(section_hits) else len(response_content)
        content = response_content[start:end].strip()
        if content:
            modules.append({"seg_index": i + 1, "title": title, "content": content})
    return modules


def aggressive_json_cleanup(raw_response: str) -> list:
    import re as _re

    def _read_json_string(s: str, start_idx: int) -> tuple[str, int]:
        if start_idx >= len(s) or s[start_idx] != '"':
            return "", start_idx
        i = start_idx + 1
        buf = []
        while i < len(s):
            ch = s[i]
            if ch == "\\" and i + 1 < len(s):
                nxt = s[i + 1]
                if nxt == "n":
                    buf.append("\n")
                elif nxt == "t":
                    buf.append("\t")
                elif nxt == "r":
                    buf.append("\r")
                elif nxt == '"':
                    buf.append('"')
                elif nxt == "\\":
                    buf.append("\\")
                else:
                    buf.append(nxt)
                i += 2
            elif ch == '"':
                return "".join(buf), i + 1
            else:
                buf.append(ch)
                i += 1
        return "".join(buf), i

    modules = []
    seg_hits = list(_re.finditer(r'"seg_index"\s*:\s*(\d+)', raw_response))
    if seg_hits:
        for idx, hit in enumerate(seg_hits):
            seg_idx = int(hit.group(1))
            chunk_start = hit.start()
            chunk_end = seg_hits[idx + 1].start() if idx + 1 < len(seg_hits) else len(raw_response)
            chunk = raw_response[chunk_start:chunk_end]
            title = f"Module {seg_idx}"
            tm = _re.search(r'"title"\s*:\s*"([^"]{1,300})"', chunk)
            if tm:
                title = tm.group(1)
            content = ""
            cm = _re.search(r'"content"\s*:\s*"', chunk)
            if cm:
                content, _ = _read_json_string(chunk, cm.end() - 1)
            if content or title != f"Module {seg_idx}":
                modules.append({"seg_index": seg_idx, "title": title, "content": content})
        return modules

    title_hits = list(_re.finditer(r'"title"\s*:\s*"', raw_response))
    for title_hit in title_hits:
        title, after_title = _read_json_string(raw_response, title_hit.end() - 1)
        if not title:
            continue
        content_match = _re.search(r'"content"\s*:\s*"', raw_response[after_title:])
        if not content_match:
            continue
        content, _ = _read_json_string(raw_response, after_title + content_match.end() - 1)
        if content.strip():
            modules.append({"seg_index": len(modules) + 1, "title": title.strip(), "content": content})
    return modules


__all__ = [
    "resize_image_if_needed",
    "extract_module_count_with_llm",
    "TUTOR_AGENT_INSTRUCTIONS",
    "_aws_bedrock_model",
    "title_agent",
    "report_agent",
    "custom_instruction_agent",
    "regeneration_agent",
    "smart_tutor_agent",
    "clone_agent_for_request",
    "tutor_agent",
    "question_generation_agent",
    "answer_validation_agent",
    "analyze_image_with_vision_bytes",
    "generate_course_title_from_content",
    "fix_malformed_json",
    "extract_modules_from_text",
    "aggressive_json_cleanup",
]
from core.runtime import *
from models.domain import *
from schemas.domain import *
from services.jobs import *
from services.auth_platform import *
from services.documents import *
from services.knowledge_base import *

def _aws_bedrock_model(**kwargs) -> AwsBedrock:
    """Use the shared sync_bedrock_client so Agno gets both its credentials and its timeouts.

    Passing ``session=`` instead makes Agno build its own bedrock-runtime client per call, with no
    botocore Config: botocore's defaults are a 60s read timeout and 5 attempts, which cut a long
    lecture off mid-generation. That is what services/knowledge_base.py used to work around by
    patching urllib3's Timeout class process-wide - and that patch also raised Polly's 10s read
    timeout to 600s, so one stalled synthesis could hold a request thread for ten minutes. Handing
    over the already-configured client keeps the long Bedrock timeout without touching any other
    AWS client. ``session`` is deliberately left unset: Agno returns ``client`` only when there is
    no session (see AwsBedrock.get_client), and credentials still refresh because the client holds
    the session's refreshable credentials. The client is shared, which botocore supports and this
    application already relies on - the same object serves converse() and invoke_model() from
    several worker threads.
    """
    return AwsBedrock(client=sync_bedrock_client, aws_region=AWS_REGION, **kwargs)

title_agent = Agent( model=_aws_bedrock_model(id=BEDROCK_GENERATION_MODEL, max_tokens=60, temperature=0.3) )
report_agent = Agent( model=_aws_bedrock_model(id=BEDROCK_GENERATION_MODEL, max_tokens=DEFAULT_REPORT_LLM_MAX_TOKENS, temperature=DEFAULT_REPORT_LLM_TEMPERATURE) )
custom_instruction_agent = Agent( model=_aws_bedrock_model(id=BEDROCK_GENERATION_MODEL, max_tokens=DEFAULT_REPORT_LLM_MAX_TOKENS, temperature=DEFAULT_REPORT_LLM_TEMPERATURE) )
regeneration_agent = Agent( model=_aws_bedrock_model(id=BEDROCK_GENERATION_MODEL, max_tokens=DEFAULT_REPORT_LLM_MAX_TOKENS, temperature=DEFAULT_REPORT_LLM_TEMPERATURE) )
smart_tutor_agent = Agent( model=_aws_bedrock_model(id=BEDROCK_GENERATION_MODEL, max_tokens=CHAT_LLM_MAX_TOKENS, temperature=CHAT_LLM_TEMPERATURE) )
tutor_agent = Agent( model=_aws_bedrock_model(id=BEDROCK_GENERATION_MODEL, max_tokens=DEFAULT_REPORT_LLM_MAX_TOKENS, temperature=0.4) )
question_generation_agent = Agent( model=_aws_bedrock_model(id=BEDROCK_GENERATION_MODEL, max_tokens=8192, temperature=0.5) )
answer_validation_agent = Agent( model=_aws_bedrock_model(id=BEDROCK_GENERATION_MODEL, max_tokens=1024, temperature=0.1) )


def clone_agent_for_request(agent: Agent, tools: Optional[List[Any]] = None) -> Agent:
    """A private copy of ``agent``, carrying this request's tools, for one streamed chat.

    ``Agent.tools`` is per-instance mutable state, and every chat entry point - typed chat, voice
    chat and video-analysis chat - handed the same ``smart_tutor_agent`` object to
    ``stream_agent_response``. Assigning the request's tools onto that shared object meant the run
    had to be serialised behind a global lock, so a cohort chatting at once was not slow because of
    Bedrock: each trainee waited for every earlier trainee's answer to finish generating. Forty of
    them queued for minutes and the browser's own fetch deadline fired first, which the trainee
    reads as "Request timed out" rather than as a queue.

    A copy costs a fraction of a millisecond because the expensive part - the boto3 client - is
    shared rather than rebuilt (see _aws_bedrock_model). The model is copied too so no agno model
    instance is touched by two requests at once. Nothing else carries over: these agents hold no
    memory, storage or session, and the conversation is passed to ``run(history=...)`` per call.
    """
    source_model = agent.model
    return Agent(
        model=_aws_bedrock_model(
            id=source_model.id,
            max_tokens=source_model.max_tokens,
            temperature=source_model.temperature,
        ),
        tools=list(tools) if tools else [],
    )

def _execute_share_reports(db: Session, sharer_id: str, recipient_id: str, source_doc_name: str, reason: Optional[str] = None, bypass_process_check: bool = False) -> str:
    sharer = db.query(User).filter(User.id == sharer_id).first()
    recipient = db.query(User).filter(User.id == recipient_id).first()
    if not recipient: return f"Action failed: Recipient user not found."
    if recipient.id == sharer.id: return "Action failed: You cannot share reports with yourself."
    
    # CRITICAL FIX: Allow super admin to bypass process restrictions when sharing on behalf of others
    if not bypass_process_check and sharer.role != 'super_admin':
        if not sharer.process_id: return "Action failed: You are not assigned to a Process and cannot share."
        if recipient.process_id != sharer.process_id: return f"Action failed: Recipient '{recipient.email}' is not in your Process."
        # Trainers may share with the trainers and super admins in their process: the same
        # allow-list share_course enforces (features/general/router.py) and the same set
        # /users/in-my-process offers the picker. Requiring the recipient be a trainer rejected
        # every super admin the Share modal listed, so "Modules Only" failed for a recipient
        # "Complete Course" accepted from the same dialog. Trainees stay excluded.
        if sharer.role == 'trainer' and recipient.role not in ('trainer', 'super_admin'):
            return "Action failed: As a trainer, you can only share with trainers or super admins in your process."
    
    # Check if source document exists
    try:
        s3_client.head_object(Bucket=S3_BUCKET_NAME, Key=f"{sharer_id}/uploaded_docs/{source_doc_name}")
    except ClientError:
        return f"Action failed: Source document '{source_doc_name}' not found in your uploads."
    
    # Validate that modules exist for this document
    try:
        modules_exist = False
        paginator = s3_client.get_paginator('list_objects_v2')
        for page in paginator.paginate(Bucket=S3_BUCKET_NAME, Prefix=f"{sharer_id}/generated_reports/"):
            for obj in page.get('Contents', []):
                if obj['Key'].endswith('.json'):
                    try:
                        meta_obj = s3_client.get_object(Bucket=S3_BUCKET_NAME, Key=obj['Key'])
                        meta_data = json.loads(meta_obj['Body'].read().decode('utf-8'))
                        if meta_data.get('source_doc') == source_doc_name:
                            modules_exist = True
                            break
                    except Exception:
                        continue
            if modules_exist:
                break
        
        if not modules_exist:
            return f"Action failed: No modules found for '{source_doc_name}'. Please generate modules first before sharing."
    
    except Exception as e:
        logger.error(
            "Error checking modules for %s: %s",
            ("" if source_doc_name is None else str(source_doc_name).replace("\n", "").replace("\r", "")),
            ("" if e is None else str(e).replace("\n", "").replace("\r", "")),
        )
        return f"Action failed: Unable to validate modules for '{source_doc_name}'."
    
    existing_share = db.query(SharedReport).filter(SharedReport.source_doc_name == source_doc_name, SharedReport.sharer_id == sharer.id, SharedReport.recipient_id == recipient.id).first()
    if existing_share: return f"Reports from '{source_doc_name}' have already been shared with '{recipient.email}'."
    
    # Get course title for better notification message
    course = db.query(Course).filter(
        Course.source_doc_name == source_doc_name,
        Course.author_id == sharer_id
    ).first()
    course_display_name = shared_source_display_label(db, sharer_id, source_doc_name, course)
    
    new_share = SharedReport(source_doc_name=source_doc_name, sharer_id=sharer_id, recipient_id=recipient_id, reason=reason)  # NEW: Add reason
    reason_text = f" Reason: {reason}" if reason else ""
    notification = Notification(user_id=recipient.id, message=f"{sharer.email} shared modules from '{course_display_name}' with you.{reason_text}", related_link="/shared", course_id=course_notification_id(course))
    db.add(new_share); db.add(notification)
    log_activity(db, sharer_id, "SHARE_MODULES", f"Shared '{course_display_name}' with user ID {recipient_id}")
    db.commit()
    return f"Successfully shared all modules from '{course_display_name}' with {recipient.email}."


__all__ = [name for name in globals() if not name.startswith('_')]
