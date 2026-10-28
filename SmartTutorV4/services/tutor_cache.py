from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed

from core.runtime import *
from models.domain import Course, SystemSettings, TokenUsage, TutorResponseCache
from services.agents import TUTOR_AGENT_INSTRUCTIONS, analyze_image_with_vision_bytes, tutor_agent
from services.knowledge_base import (
    get_image_from_s3_as_bytes,
    get_s3_signed_url,
    load_image_analysis_from_s3,
    normalize_document_name,
)
from utils.security_helpers import extract_image_tags, extract_markdown_image_urls, log_safe

# Must stay in sync with /tutor/explain-module cache lookup (build_tutor_cache_module_content).
TUTOR_RESPONSE_FORMAT_VERSION = "2026-05-27-hierarchy-image-flow-v3"


def build_tutor_cache_module_content(module_content: str) -> str:
    """Cache key input for tutor explain-module; publish pre-gen must use the same wrapper."""
    return f"[TUTOR_FORMAT:{TUTOR_RESPONSE_FORMAT_VERSION}]\n{module_content or ''}"


def calculate_pdf_hash(pdf_content: bytes) -> str:
    return hashlib.sha256(pdf_content).hexdigest()


def calculate_query_hash(query: str, module_content: str) -> str:
    prompt_version = "v6_no_quiz_widget"
    combined = f"{prompt_version}:{query}:{module_content}"
    return hashlib.sha256(combined.encode()).hexdigest()


def normalize_module_index(module_index) -> int:
    """Keep publish pre-gen and /tutor/explain-module on the same cache path key.

    Public name (no leading underscore) so `from services.tutor_cache import *` exports it.
    """
    try:
        return int(module_index if module_index is not None else 0)
    except (TypeError, ValueError):
        return 0


# Back-compat alias for any internal callers still using the private name.
_normalize_module_index = normalize_module_index


def calculate_token_cost(input_tokens: int, output_tokens: int, model_id: str, operation_type: str = "standard") -> int:
    model_key = str(model_id or "").lower()
    if "nova-pro" in model_key or "amazon.nova" in model_key:
        if operation_type == "batch":
            input_cost = input_tokens * 0.0004 / 1000
            output_cost = output_tokens * 0.0016 / 1000
        elif operation_type == "latency_optimized":
            input_cost = input_tokens * 0.001 / 1000
            output_cost = output_tokens * 0.004 / 1000
        else:
            input_cost = input_tokens * 0.0008 / 1000
            output_cost = output_tokens * 0.0032 / 1000
    elif "claude-sonnet-4" in model_id:
        total_tokens = input_tokens + output_tokens
        if operation_type == "batch":
            input_cost = input_tokens * 0.0015 / 1000
            output_cost = output_tokens * 0.0075 / 1000
        elif total_tokens > 200000:
            input_cost = input_tokens * 0.006 / 1000
            output_cost = output_tokens * 0.0225 / 1000
        else:
            input_cost = input_tokens * 0.003 / 1000
            output_cost = output_tokens * 0.015 / 1000
    else:
        logger.warning("Unknown model %s, using Nova Pro standard pricing", log_safe(model_id))
        input_cost = input_tokens * 0.0008 / 1000
        output_cost = output_tokens * 0.0032 / 1000

    total_cost_dollars = input_cost + output_cost
    return max(1, round(total_cost_dollars * 100))


def track_token_usage(
    db: Session,
    user_id: str,
    operation_type: str,
    input_tokens: int,
    output_tokens: int = None,
    course_id: str = None,
    model_id: str = None,
):
    if model_id is None:
        model_id = BEDROCK_GENERATION_MODEL
    if output_tokens is None:
        output_tokens = max(1, input_tokens // 4)

    total_tokens = input_tokens + output_tokens
    cost_cents = calculate_token_cost(input_tokens, output_tokens, model_id, "standard")

    usage = TokenUsage(
        user_id=user_id,
        course_id=course_id,
        operation_type=operation_type,
        tokens_used=total_tokens,
        cost_cents=cost_cents,
    )
    db.add(usage)
    db.commit()

    logger.info(
        "Token usage tracked: %s input + %s output = %s total tokens, $%.4f (%s)",
        input_tokens,
        output_tokens,
        total_tokens,
        cost_cents / 100,
        log_safe(model_id),
    )
    return cost_cents


def convert_old_urls_to_new_format(cached_data: dict) -> dict:
    if not cached_data:
        return cached_data

    def convert_urls_in_value(value):
        if isinstance(value, str):
            pattern = r'https://smart-tutor-kb\.s3(?:\.us-east-1)?\.amazonaws\.com/([^"?\s\]]+)(?:\?[^"\]]*)?'

            def replace_url(match):
                s3_key = match.group(1)
                new_url = f"/api/images/s3/{s3_key}"
                logger.info("[URL CONVERSION] Converting old URL to: %s", new_url)
                return new_url

            return re.sub(pattern, replace_url, value)
        if isinstance(value, dict):
            return {k: convert_urls_in_value(v) for k, v in value.items()}
        if isinstance(value, list):
            return [convert_urls_in_value(item) for item in value]
        return value

    converted_data = convert_urls_in_value(cached_data)
    original_str = json.dumps(cached_data)
    converted_str = json.dumps(converted_data)
    converted_urls = len(re.findall(r"/api/images/s3/", converted_str))
    if converted_urls > 0:
        logger.info("[URL CONVERSION] Converted %s old signed URLs to new format", converted_urls)
    return converted_data


def get_cached_tutor_response(db: Session, course_id: str, module_index: int, query: str, module_content: str):
    module_index = _normalize_module_index(module_index)
    query_hash = calculate_query_hash(query, module_content)

    if course_id != "generic":
        cache_entry = db.query(TutorResponseCache).filter(
            TutorResponseCache.course_id == course_id,
            TutorResponseCache.module_index == module_index,
            TutorResponseCache.query_hash == query_hash,
        ).first()
        if cache_entry:
            try:
                response = s3_client.get_object(Bucket=S3_BUCKET_NAME, Key=cache_entry.response_s3_key)
                cached_data = json.loads(response["Body"].read().decode("utf-8"))
                cached_data = convert_old_urls_to_new_format(cached_data)
                logger.info("Cache HIT for tutor query (saved %s tokens)", cache_entry.tokens_used)
                return cached_data
            except Exception as e:
                logger.error("Error fetching cached response: %s", log_safe(e))
                return None

    try:
        s3_key = f"tutor_cache/{course_id}/{module_index}/{query_hash}.json"
        response = s3_client.get_object(Bucket=S3_BUCKET_NAME, Key=s3_key)
        cached_data = json.loads(response["Body"].read().decode("utf-8"))
        cached_data = convert_old_urls_to_new_format(cached_data)
        logger.info(
            "Cache HIT for generic tutor query from S3: %s",
            ("" if s3_key is None else str(s3_key).replace("\n", "").replace("\r", "")),
        )
        return cached_data
    except ClientError as e:
        if e.response["Error"]["Code"] == "NoSuchKey":
            logger.info(
                "CACHE MISS - No cached response found for %s/%s",
                ("" if course_id is None else str(course_id).replace("\n", "").replace("\r", "")),
                ("" if module_index is None else str(module_index).replace("\n", "").replace("\r", "")),
            )
        else:
            logger.error("Error checking S3 cache: %s", log_safe(e))
        return None
    except Exception as e:
        logger.error("Error fetching cached response from S3: %s", log_safe(e))
        return None


def cache_tutor_response(
    db: Session,
    course_id: str,
    module_index: int,
    query: str,
    module_content: str,
    response_data: dict,
    tokens_used: int,
    user_id: str,
):
    try:
        module_index = _normalize_module_index(module_index)
        query_hash = calculate_query_hash(query, module_content)
        s3_key = f"tutor_cache/{course_id}/{module_index}/{query_hash}.json"

        s3_client.put_object(
            Bucket=S3_BUCKET_NAME,
            Key=s3_key,
            Body=json.dumps(response_data),
            ContentType="application/json",
        )

        existing_entry = db.query(TutorResponseCache).filter(
            TutorResponseCache.course_id == course_id,
            TutorResponseCache.module_index == module_index,
            TutorResponseCache.query_hash == query_hash,
        ).first()

        if course_id != "generic":
            if existing_entry:
                existing_entry.response_s3_key = s3_key
                existing_entry.tokens_used = tokens_used
                existing_entry.created_at = datetime.now(timezone.utc)
                logger.info(
                    "Updated existing cache entry in S3: %s",
                    ("" if s3_key is None else str(s3_key).replace("\n", "").replace("\r", "")),
                )
            else:
                cache_entry = TutorResponseCache(
                    course_id=course_id,
                    module_index=module_index,
                    query_hash=query_hash,
                    response_s3_key=s3_key,
                    tokens_used=tokens_used,
                )
                db.add(cache_entry)
                logger.info(
                    "Cached tutor response to S3: %s",
                    ("" if s3_key is None else str(s3_key).replace("\n", "").replace("\r", "")),
                )
            db.commit()
        else:
            logger.info(
                "Cached generic tutor response to S3 only: %s",
                ("" if s3_key is None else str(s3_key).replace("\n", "").replace("\r", "")),
            )
    except Exception as e:
        logger.error("Error caching tutor response: %s", e)
        db.rollback()


TUTOR_WATERMARK_PATTERNS = (
    "made_with",
    "madewith",
    "gamma",
    "canva",
    "watermark",
    "logo",
    "badge",
    "branding",
    "powered_by",
    "created_with",
    "slide_template",
    "background",
)
TUTOR_TINY_IMAGE_THRESHOLD = 8 * 1024


def _doc_names_from_image_tags(image_tags: list, course=None) -> set:
    doc_names = set()
    if course:
        if course.source_doc_name:
            doc_names.add(course.source_doc_name)
        if course.source_documents:
            try:
                for doc in json.loads(course.source_documents):
                    if doc:
                        doc_names.add(doc)
            except Exception:
                pass
    for s3_key in image_tags:
        if "/extracted_images/" in s3_key:
            parts = s3_key.split("/extracted_images/")
            if len(parts) > 1:
                doc_path = parts[1]
                doc_name = doc_path.split("/")[0] if "/" in doc_path else doc_path
                if doc_name:
                    doc_names.add(doc_name)
    return doc_names


def _format_cached_vision_text(cached_data: dict) -> str:
    return (
        "**Image Analysis (Cached):**\n"
        f"Caption: {cached_data.get('caption', 'Image')}\n"
        f"Type: {cached_data.get('type', 'unknown')}\n"
        f"Keywords: {', '.join(cached_data.get('keywords', []))}\n"
        f"Topics: {', '.join(cached_data.get('topics', []))}\n"
        f"Content: {cached_data.get('content', 'No description available')}"
    )


def _filter_watermark_images(image_tags: list) -> list:
    kept = []
    for s3_key in image_tags:
        filename_lower = s3_key.split("/")[-1].lower()
        if any(pat in filename_lower for pat in TUTOR_WATERMARK_PATTERNS):
            logger.warning("[TUTOR FILTER] Removing watermark/branding image by filename: %s", s3_key)
            continue
        try:
            head = s3_client.head_object(Bucket=S3_BUCKET_NAME, Key=s3_key)
            file_size = head.get("ContentLength", 0)
            if file_size < TUTOR_TINY_IMAGE_THRESHOLD:
                logger.warning(
                    "[TUTOR FILTER] Removing tiny image (%s bytes): %s", file_size, s3_key
                )
                continue
        except Exception as size_err:
            logger.warning("[TUTOR FILTER] Could not check size for %s: %s", s3_key, size_err)
        kept.append(s3_key)
    return kept


def _vision_analysis_for_image(
    s3_key: str,
    module_content: str,
    cached_analyses: dict,
) -> tuple[str, str | None]:
    if s3_key in cached_analyses:
        return s3_key, _format_cached_vision_text(cached_analyses[s3_key])

    image_pattern = f"[IMAGE: {s3_key}]"
    image_pos = module_content.find(image_pattern)
    if image_pos >= 0:
        before = module_content[max(0, image_pos - 800) : image_pos]
        if "VISION ANALYSIS" in before or "Image Analysis (Cached)" in before:
            logger.info("[TUTOR] Skipping vision — analysis already in module content for %s", s3_key)
            return s3_key, None

    image_bytes = get_image_from_s3_as_bytes(s3_key)
    if not image_bytes:
        logger.warning("[TUTOR] Could not download image bytes for: %s", s3_key)
        return s3_key, None

    if image_pos < 0:
        image_pos = module_content.find(s3_key)
    context_before = module_content[max(0, image_pos - 2000) : image_pos] if image_pos >= 0 else ""
    context_after = (
        module_content[
            image_pos + len(image_pattern) : min(
                len(module_content), image_pos + len(image_pattern) + 2000
            )
        ]
        if image_pos >= 0
        else ""
    )
    logger.info("[TUTOR] Analyzing image with vision (cache miss): %s", s3_key)
    analysis = analyze_image_with_vision_bytes(
        image_bytes, context_before, context_after, s3_key
    )
    return s3_key, analysis


def enrich_module_content_for_tutor(
    module_content: str,
    author_user_id: str,
    course=None,
    *,
    max_parallel_vision: int = 4,
) -> tuple[str, dict[str, str]]:
    """
    Prepare module content for the tutor agent: image URLs, cached vision (fast path),
    and parallel vision only for images without cached analysis.
    """
    image_tags = extract_image_tags(module_content)
    if not image_tags:
        image_tags = extract_markdown_image_urls(module_content)

    if not image_tags:
        return module_content, {}

    image_tags = _filter_watermark_images(list(dict.fromkeys(image_tags)))
    removed = set(extract_image_tags(module_content)) - set(image_tags)
    for s3_key in removed:
        module_content = module_content.replace(f"[IMAGE: {s3_key}]", "")
        module_content = re.sub(
            rf"\n\s*\[IMAGE:\s*{re.escape(s3_key)}\s*\]\s*\n", "\n", module_content
        )

    s3_to_url_map = {s3_key: f"/api/images/s3/{s3_key}" for s3_key in image_tags}

    cached_analyses = {}
    for doc_name in _doc_names_from_image_tags(image_tags, course):
        cached_analyses.update(load_image_analysis_from_s3(author_user_id, doc_name))
    if cached_analyses:
        logger.info(
            "[TUTOR] Loaded %s cached image analyses for tutor explain",
            len(cached_analyses),
        )

    image_analyses = {}
    need_vision = [k for k in image_tags if k not in cached_analyses]
    if need_vision:
        workers = min(max_parallel_vision, len(need_vision))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(
                    _vision_analysis_for_image, s3_key, module_content, cached_analyses
                ): s3_key
                for s3_key in need_vision
            }
            for future in as_completed(futures):
                try:
                    s3_key, analysis = future.result()
                    if analysis:
                        image_analyses[s3_key] = analysis
                except Exception as e:
                    logger.error("[TUTOR] Parallel vision failed for %s: %s", futures[future], e)

    for s3_key in image_tags:
        if s3_key in cached_analyses and s3_key not in image_analyses:
            image_analyses[s3_key] = _format_cached_vision_text(cached_analyses[s3_key])

    content_out = module_content
    for s3_key, image_url in s3_to_url_map.items():
        vision_context = ""
        if s3_key in image_analyses:
            vision_context = (
                f"\n\n**VISION ANALYSIS FOR THIS IMAGE:**\n{image_analyses[s3_key]}\n\n"
            )
        replacement = f"{vision_context}[IMAGE_URL: {image_url}]"
        tag = f"[IMAGE: {s3_key}]"
        if tag in content_out:
            content_out = content_out.replace(tag, replacement)
        else:
            content_out = re.sub(
                rf"!\[(.*?)\]\({re.escape(s3_key)}\)",
                replacement,
                content_out,
            )

    return content_out, s3_to_url_map


def pre_generate_tutor_responses(db: Session, user_id: str, course_id: str, source_doc_name: str):
    try:
        course = db.query(Course).filter(Course.id == course_id).first()
        if not course:
            logger.error("Course %s not found for tutor cache generation", course_id)
            return 0

        course_docs_normalized = []
        try:
            if course.source_documents:
                source_docs_list = json.loads(course.source_documents)
                if isinstance(source_docs_list, list):
                    for doc in source_docs_list:
                        course_docs_normalized.append(normalize_document_name(doc))
        except Exception as e:
            logger.warning("Could not parse source_documents JSON: %s", e)

        if not course_docs_normalized:
            course_docs_normalized = [normalize_document_name(source_doc_name)]

        modules = []
        paginator = s3_client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=S3_BUCKET_NAME, Prefix=f"{user_id}/generated_reports/"):
            for obj in page.get("Contents", []):
                if not obj["Key"].endswith(".json"):
                    continue
                try:
                    meta_obj = s3_client.get_object(Bucket=S3_BUCKET_NAME, Key=obj["Key"])
                    meta_data = json.loads(meta_obj["Body"].read().decode("utf-8"))
                    source_doc = meta_data.get("source_doc", "")
                    source_doc_normalized = normalize_document_name(source_doc)
                    is_match = any(
                        source_doc_normalized == course_doc_norm
                        or course_doc_norm in source_doc_normalized
                        or source_doc_normalized in course_doc_norm
                        for course_doc_norm in course_docs_normalized
                    )
                    if is_match:
                        md_key = obj["Key"].replace(".json", ".md")
                        report_obj = s3_client.get_object(Bucket=S3_BUCKET_NAME, Key=md_key)
                        module_content = report_obj["Body"].read().decode("utf-8")
                        modules.append(
                            {
                                "index": meta_data.get("seg_index", 0),
                                "title": meta_data.get("title", "Module"),
                                "content": module_content,
                                "source_doc": source_doc,
                            }
                        )
                except Exception as e:
                    logger.error("Error loading module metadata: %s", e)

        if not modules:
            logger.warning(
                "No modules found for course %s",
                ("" if source_doc_name is None else str(source_doc_name).replace("\n", "").replace("\r", "")),
            )
            return 0

        modules.sort(key=lambda x: x["index"])
        total_tokens_saved = 0

        for module in modules:
            module_index = _normalize_module_index(module["index"])
            module_content = module["content"]
            cache_key_content = build_tutor_cache_module_content(module_content)
            cached = get_cached_tutor_response(db, course_id, module_index, "explain_module", cache_key_content)
            if cached:
                continue

            try:
                image_tags = extract_image_tags(module_content)
                s3_to_signed_url_map = {}
                image_analyses = {}
                cached_analyses = {}

                if image_tags:
                    doc_names = set()
                    for s3_key in image_tags:
                        if "/extracted_images/" in s3_key:
                            parts = s3_key.split("/extracted_images/")
                            if len(parts) > 1:
                                doc_path = parts[1]
                                doc_name = doc_path.split("/")[0] if "/" in doc_path else doc_path
                                doc_names.add(doc_name)
                    for doc_name in doc_names:
                        cached_analyses.update(load_image_analysis_from_s3(user_id, doc_name))

                for s3_key in image_tags:
                    try:
                        signed_url = get_s3_signed_url(s3_key, expiration=3600)
                        if not signed_url:
                            continue
                        s3_to_signed_url_map[s3_key] = signed_url
                        if s3_key in cached_analyses:
                            cached_data = cached_analyses[s3_key]
                            vision_analysis = (
                                "**Image Analysis (Cached):**\n"
                                f"Caption: {cached_data.get('caption', 'Image')}\n"
                                f"Type: {cached_data.get('type', 'unknown')}\n"
                                f"Keywords: {', '.join(cached_data.get('keywords', []))}\n"
                                f"Topics: {', '.join(cached_data.get('topics', []))}\n"
                                f"Content: {cached_data.get('content', 'No description available')}"
                            )
                            image_analyses[s3_key] = vision_analysis
                        else:
                            image_bytes = get_image_from_s3_as_bytes(s3_key)
                            if image_bytes:
                                image_pos = module_content.find(f"[IMAGE: {s3_key}]")
                                if image_pos != -1:
                                    context_before = module_content[max(0, image_pos - 2000) : image_pos]
                                    context_after = module_content[
                                        image_pos + len(f"[IMAGE: {s3_key}]") : min(
                                            len(module_content),
                                            image_pos + len(f"[IMAGE: {s3_key}]") + 2000,
                                        )
                                    ]
                                    image_analyses[s3_key] = analyze_image_with_vision_bytes(
                                        image_bytes,
                                        context_before,
                                        context_after,
                                        s3_key,
                                    )
                    except Exception as e:
                        logger.error("Error processing image %s: %s", s3_key, e)

                module_content_with_signed_urls = module_content
                for s3_key, signed_url in s3_to_signed_url_map.items():
                    vision_context = ""
                    if s3_key in image_analyses:
                        vision_context = f"\n\n**VISION ANALYSIS FOR THIS IMAGE:**\n{image_analyses[s3_key]}\n\n"
                    replacement = f"{vision_context}[IMAGE_URL: {signed_url}]"
                    module_content_with_signed_urls = module_content_with_signed_urls.replace(
                        f"[IMAGE: {s3_key}]",
                        replacement,
                    )

                image_instruction = ""
                if s3_to_signed_url_map:
                    valid_image_urls = list(s3_to_signed_url_map.values())
                    image_instruction = (
                        "\n\nIMPORTANT: When referencing images in your response, ONLY use these exact "
                        "IMAGE_URL tags that are provided in the content. Do NOT create new image "
                        f"filenames or modify existing ones:\n{valid_image_urls}\n\n"
                        "If you need to reference an image, copy the exact [IMAGE_URL: ...] tag "
                        "from the content above."
                    )

                full_prompt = (
                    f"{TUTOR_AGENT_INSTRUCTIONS}{image_instruction}\n\n"
                    f"Here is the module content:\n\n{module_content_with_signed_urls}"
                )
                lecture_script = tutor_agent.run(full_prompt).content
                response_data = {
                    "lecture_script": lecture_script,
                    "image_urls": list(s3_to_signed_url_map.values()),
                }
                estimated_tokens = len(full_prompt.split()) + len(lecture_script.split())
                cache_tutor_response(
                    db,
                    course_id,
                    module_index,
                    "explain_module",
                    cache_key_content,
                    response_data,
                    estimated_tokens,
                    user_id,
                )
                total_tokens_saved += estimated_tokens
            except Exception as e:
                logger.error("Error generating response for Module %s: %s", module_index, e)

        logger.info("Pre-generation complete with %s estimated tokens", total_tokens_saved)
        return total_tokens_saved
    except Exception as e:
        logger.error("Error pre-generating tutor responses: %s", e, exc_info=True)
        return 0


def get_system_setting(db: Session, key: str, default: str = "false") -> str:
    setting = db.query(SystemSettings).filter(SystemSettings.setting_key == key).first()
    return setting.setting_value if setting else default


def set_system_setting(db: Session, key: str, value: str):
    setting = db.query(SystemSettings).filter(SystemSettings.setting_key == key).first()
    if setting:
        setting.setting_value = value
        setting.updated_at = datetime.now(timezone.utc)
    else:
        setting = SystemSettings(setting_key=key, setting_value=value)
        db.add(setting)
    db.commit()


def update_course_with_pdf_hash(
    db: Session,
    user_id: str,
    source_doc_name: str,
    pdf_content: bytes,
    estimated_tokens: int = 0,
):
    try:
        pdf_hash = calculate_pdf_hash(pdf_content)
        course = db.query(Course).filter(Course.author_id == user_id, Course.source_doc_name == source_doc_name).first()
        if course:
            course.pdf_hash = pdf_hash
            course.tokens_used = estimated_tokens
            estimated_input_tokens = estimated_tokens // 3
            estimated_output_tokens = estimated_tokens - estimated_input_tokens
            course.generation_cost = calculate_token_cost(
                estimated_input_tokens,
                estimated_output_tokens,
                BEDROCK_GENERATION_MODEL,
            )
            db.commit()

            estimated_input_tokens_for_tracking = estimated_tokens // 4 * 3
            if estimated_tokens > estimated_input_tokens_for_tracking:
                estimated_output_tokens_for_tracking = estimated_tokens - estimated_input_tokens_for_tracking
            else:
                estimated_output_tokens_for_tracking = estimated_tokens // 4
            track_token_usage(
                db,
                user_id,
                "module_generation",
                estimated_input_tokens_for_tracking,
                estimated_output_tokens_for_tracking,
                course.id,
                BEDROCK_GENERATION_MODEL,
            )
        else:
            logger.warning("Course not found for PDF hash update: %s", source_doc_name)
    except Exception as e:
        logger.error("Error updating course with PDF hash: %s", e)


__all__ = [
    "TUTOR_RESPONSE_FORMAT_VERSION",
    "build_tutor_cache_module_content",
    "calculate_pdf_hash",
    "calculate_query_hash",
    "normalize_module_index",
    "_normalize_module_index",
    "calculate_token_cost",
    "track_token_usage",
    "convert_old_urls_to_new_format",
    "get_cached_tutor_response",
    "cache_tutor_response",
    "pre_generate_tutor_responses",
    "get_system_setting",
    "set_system_setting",
    "update_course_with_pdf_hash",
]
from core.runtime import *
from models.domain import *
from schemas.domain import *
from services.jobs import *
from services.auth_platform import *
from services.documents import *
from services.knowledge_base import *
from services.agents import TUTOR_AGENT_INSTRUCTIONS, tutor_agent, analyze_image_with_vision_bytes

import hashlib

def calculate_pdf_hash(pdf_content: bytes) -> str:
    """Calculate SHA256 hash of PDF content for duplicate detection"""
    return hashlib.sha256(pdf_content).hexdigest()

def calculate_query_hash(query: str, module_content: str) -> str:
    """Calculate hash for tutor query caching"""
    # PROMPT_VERSION: bump this when TUTOR_AGENT_INSTRUCTIONS changes significantly
    # so old cached responses are automatically bypassed.
    PROMPT_VERSION = "v6_no_quiz_widget"
    combined = f"{PROMPT_VERSION}:{query}:{module_content}"
    return hashlib.sha256(combined.encode()).hexdigest()

def calculate_token_cost(input_tokens: int, output_tokens: int, model_id: str, operation_type: str = "standard") -> int:
    """
    Calculate cost in cents based on 2026 pricing for Amazon Bedrock models.
    
    Args:
        input_tokens: Number of input tokens
        output_tokens: Number of output tokens  
        model_id: The model identifier
        operation_type: "standard", "batch", "latency_optimized"
    
    Returns:
        Cost in cents
    """
    
    # Amazon Nova Pro v1:0 Pricing (2026)
    model_key = str(model_id or "").lower()
    if "nova-pro" in model_key or "amazon.nova" in model_key:
        if operation_type == "batch":
            # Batch Mode: 50% discount
            input_cost = input_tokens * 0.0004 / 1000  # $0.0004 per 1K tokens
            output_cost = output_tokens * 0.0016 / 1000  # $0.0016 per 1K tokens
        elif operation_type == "latency_optimized":
            # Latency Optimized
            input_cost = input_tokens * 0.001 / 1000  # $0.001 per 1K tokens
            output_cost = output_tokens * 0.004 / 1000  # $0.004 per 1K tokens
        else:
            # Standard On-Demand
            input_cost = input_tokens * 0.0008 / 1000  # $0.0008 per 1K tokens
            output_cost = output_tokens * 0.0032 / 1000  # $0.0032 per 1K tokens
    
    # Claude 4 Sonnet Pricing (2026)
    elif "claude-sonnet-4" in model_id:
        total_tokens = input_tokens + output_tokens
        
        if operation_type == "batch":
            # Batch Processing: 50% discount
            input_cost = input_tokens * 0.0015 / 1000  # $1.50 per 1M tokens
            output_cost = output_tokens * 0.0075 / 1000  # $7.50 per 1M tokens
        elif total_tokens > 200000:
            # Long-Context Pricing (>200K tokens)
            input_cost = input_tokens * 0.006 / 1000  # $6.00 per 1M tokens
            output_cost = output_tokens * 0.0225 / 1000  # $22.50 per 1M tokens
        else:
            # Standard Pricing (≤200K tokens)
            input_cost = input_tokens * 0.003 / 1000  # $3.00 per 1M tokens
            output_cost = output_tokens * 0.015 / 1000  # $15.00 per 1M tokens
    
    else:
        # Fallback for unknown models (use Nova Pro standard rates)
        logger.warning('Unknown model %s, using Nova Pro standard pricing', log_safe(model_id))
        input_cost = input_tokens * 0.0008 / 1000
        output_cost = output_tokens * 0.0032 / 1000
    
    total_cost_dollars = input_cost + output_cost
    return max(1, round(total_cost_dollars * 100))  # Convert to cents, minimum 1 cent

def track_token_usage(db: Session, user_id: str, operation_type: str, input_tokens: int, output_tokens: int = None, course_id: str = None, model_id: str = None):
    """
    Track token usage for analytics with accurate pricing.
    
    Args:
        input_tokens: Number of input tokens
        output_tokens: Number of output tokens (if None, assumes 25% of input for estimation)
        model_id: The model used (defaults to BEDROCK_GENERATION_MODEL)
    """
    
    # Default model if not specified
    if model_id is None:
        model_id = BEDROCK_GENERATION_MODEL
    
    # Estimate output tokens if not provided (typical ratio is 1:4 input:output for generation tasks)
    if output_tokens is None:
        output_tokens = max(1, input_tokens // 4)
    
    total_tokens = input_tokens + output_tokens
    
    # Calculate cost based on model and token distribution
    # Pass "standard" as operation_type for cost calculation (not the same as the tracking operation_type)
    cost_cents = calculate_token_cost(input_tokens, output_tokens, model_id, "standard")
    
    usage = TokenUsage(
        user_id=user_id,
        course_id=course_id,
        operation_type=operation_type,
        tokens_used=total_tokens,
        cost_cents=cost_cents
    )
    db.add(usage)
    db.commit()
    
    logger.info(
        '💰 Token usage tracked: %s input + %s output = %s total tokens, $%s (%s)',
        input_tokens,
        output_tokens,
        total_tokens,
        f"{cost_cents/100:.4f}",
        log_safe(model_id),
    )
    return cost_cents

def convert_old_urls_to_new_format(cached_data: dict) -> dict:
    """
    Convert old signed S3 URLs in cached responses to new /api/images/s3/ format.
    This allows keeping cache while fixing image display issues.
    
    Patterns to convert:
    - https://smart-tutor-kb.s3.amazonaws.com/{s3_key}?... → /api/images/s3/{s3_key}
    - https://smart-tutor-kb.s3.us-east-1.amazonaws.com/{s3_key}?... → /api/images/s3/{s3_key}
    """
    import re
    
    if not cached_data:
        return cached_data
    
    # Work directly with the dictionary structure instead of JSON string manipulation
    # This prevents JSON parsing errors from malformed strings
    def convert_urls_in_value(value):
        if isinstance(value, str):
            # More precise pattern that stops at the first quote or bracket
            pattern = r'https://smart-tutor-kb\.s3(?:\.us-east-1)?\.amazonaws\.com/([^"?\s\]]+)(?:\?[^"\]]*)?'
            
            def replace_url(match):
                s3_key = match.group(1)
                new_url = f"/api/images/s3/{s3_key}"
                logger.info('[URL CONVERSION] Converting old URL to: %s', ("" if new_url is None else str(new_url).replace("\n", "").replace("\r", "")))
                return new_url
            
            return re.sub(pattern, replace_url, value)
        elif isinstance(value, dict):
            return {k: convert_urls_in_value(v) for k, v in value.items()}
        elif isinstance(value, list):
            return [convert_urls_in_value(item) for item in value]
        else:
            return value
    
    converted_data = convert_urls_in_value(cached_data)
    
    # Count conversions for logging
    original_str = json.dumps(cached_data)
    converted_str = json.dumps(converted_data)
    original_urls = len(re.findall(r'https://smart-tutor-kb\.s3(?:\.us-east-1)?\.amazonaws\.com/', original_str))
    converted_urls = len(re.findall(r'/api/images/s3/', converted_str))
    
    if converted_urls > 0:
        logger.info('[URL CONVERSION] ✓ Converted %s old signed URLs to new format', ("" if converted_urls is None else str(converted_urls).replace("\n", "").replace("\r", "")))
    
    return converted_data

def get_cached_tutor_response(db: Session, course_id: str, module_index: int, query: str, module_content: str):
    """Check if tutor response is cached"""
    module_index = _normalize_module_index(module_index)
    query_hash = calculate_query_hash(query, module_content)
    
    # For real courses, check database first
    if course_id != "generic":
        cache_entry = db.query(TutorResponseCache).filter(
            TutorResponseCache.course_id == course_id,
            TutorResponseCache.module_index == module_index,
            TutorResponseCache.query_hash == query_hash
        ).first()
        
        if cache_entry:
            try:
                # Fetch cached response from S3
                response = s3_client.get_object(Bucket=S3_BUCKET_NAME, Key=cache_entry.response_s3_key)
                cached_data = json.loads(response['Body'].read().decode('utf-8'))
                
                # Log what's in the cached response before conversion
                if 'lecture_script' in cached_data:
                    script_preview = cached_data['lecture_script'][:500] if isinstance(cached_data['lecture_script'], str) else str(cached_data['lecture_script'])[:500]
                    logger.info('[CACHE] Cached lecture_script preview: %s', ("" if script_preview is None else str(script_preview).replace("\n", "").replace("\r", "")))
                    logger.info(
                        '[CACHE] Contains [IMAGE_URL: tags: %s',
                        cached_data['lecture_script'].count('[IMAGE_URL:')
                        if isinstance(cached_data['lecture_script'], str)
                        else 0,
                    )
                    
                    # Find and log the exact image URL format
                    if '[IMAGE_URL:' in cached_data['lecture_script']:
                        idx = cached_data['lecture_script'].index('[IMAGE_URL:')
                        sample = cached_data['lecture_script'][idx:idx+300]
                        logger.info('[CACHE] Sample IMAGE_URL tag: %s', ("" if repr(sample) is None else str(repr(sample)).replace("\n", "").replace("\r", "")))
                
                # CRITICAL FIX: Convert old signed URLs to new /api/images/s3/ format
                # This allows keeping cache while fixing image display issues
                cached_data = convert_old_urls_to_new_format(cached_data)
                
                # Log after conversion
                if 'lecture_script' in cached_data:
                    script_preview = cached_data['lecture_script'][:500] if isinstance(cached_data['lecture_script'], str) else str(cached_data['lecture_script'])[:500]
                    logger.info('[CACHE] After conversion preview: %s', ("" if script_preview is None else str(script_preview).replace("\n", "").replace("\r", "")))
                
                logger.info('✅ Cache HIT for tutor query (saved %s tokens)', ("" if cache_entry.tokens_used is None else str(cache_entry.tokens_used).replace("\n", "").replace("\r", "")))
                return cached_data
            except Exception as e:
                logger.error('Error fetching cached response: %s', log_safe(e))
                return None
    
    # For generic courses or if no database entry, check S3 directly
    try:
        s3_key = f"tutor_cache/{course_id}/{module_index}/{query_hash}.json"
        response = s3_client.get_object(Bucket=S3_BUCKET_NAME, Key=s3_key)
        cached_data = json.loads(response['Body'].read().decode('utf-8'))
        
        # Convert old URLs to new format
        cached_data = convert_old_urls_to_new_format(cached_data)
        
        logger.info('✅ Cache HIT for generic tutor query from S3: %s', ("" if s3_key is None else str(s3_key).replace("\n", "").replace("\r", "")))
        return cached_data
    except ClientError as e:
        if e.response['Error']['Code'] == 'NoSuchKey':
            logger.info('⚠️ CACHE MISS - No cached response found for %s/%s', ("" if course_id is None else str(course_id).replace("\n", "").replace("\r", "")), ("" if module_index is None else str(module_index).replace("\n", "").replace("\r", "")))
        else:
            logger.error('Error checking S3 cache: %s', log_safe(e))
        return None
    except Exception as e:
        logger.error('Error fetching cached response from S3: %s', log_safe(e))
        return None

def cache_tutor_response(db: Session, course_id: str, module_index: int, query: str, module_content: str, response_data: dict, tokens_used: int, user_id: str):
    """Cache tutor response to S3"""
    try:
        module_index = _normalize_module_index(module_index)
        query_hash = calculate_query_hash(query, module_content)
        s3_key = f"tutor_cache/{course_id}/{module_index}/{query_hash}.json"
        
        # Store response in S3
        s3_client.put_object(
            Bucket=S3_BUCKET_NAME,
            Key=s3_key,
            Body=json.dumps(response_data),
            ContentType='application/json'
        )
        
        # Check if cache entry already exists
        existing_entry = db.query(TutorResponseCache).filter(
            TutorResponseCache.course_id == course_id,
            TutorResponseCache.module_index == module_index,
            TutorResponseCache.query_hash == query_hash
        ).first()
        
        # Only create database cache entry for real courses, not generic ones
        if course_id != "generic":
            if existing_entry:
                # Update existing entry
                existing_entry.response_s3_key = s3_key
                existing_entry.tokens_used = tokens_used
                existing_entry.created_at = datetime.now(timezone.utc)
                logger.info('✅ Updated existing cache entry in S3: %s', ("" if s3_key is None else str(s3_key).replace("\n", "").replace("\r", "")))
            else:
                # Create new cache entry
                cache_entry = TutorResponseCache(
                    course_id=course_id,
                    module_index=module_index,
                    query_hash=query_hash,
                    response_s3_key=s3_key,
                    tokens_used=tokens_used
                )
                db.add(cache_entry)
                logger.info('✅ Cached tutor response to S3: %s', ("" if s3_key is None else str(s3_key).replace("\n", "").replace("\r", "")))
            
            db.commit()
        else:
            logger.info('✅ Cached generic tutor response to S3 only: %s', ("" if s3_key is None else str(s3_key).replace("\n", "").replace("\r", "")))
            # For generic courses, we only cache to S3, not database
    except Exception as e:
        logger.error('Error caching tutor response: %s', e)
        db.rollback()

def pre_generate_tutor_responses(db: Session, user_id: str, course_id: str, source_doc_name: str):
    """
    Pre-generate and cache ALL tutor responses for a course when it's published.
    This eliminates LLM calls when trainees view modules.
    Supports both single-document and multi-document courses.
    """
    try:
        # Get course to check for multi-document support
        course = db.query(Course).filter(Course.id == course_id).first()
        if not course:
            logger.error('Course %s not found for tutor cache generation', ("" if course_id is None else str(course_id).replace("\n", "").replace("\r", "")))
            return 0
        
        # CRITICAL FIX: Parse source_documents JSON array for multi-document courses
        course_docs_normalized = []
        try:
            if course.source_documents:
                source_docs_list = json.loads(course.source_documents)
                if isinstance(source_docs_list, list):
                    for doc in source_docs_list:
                        doc_normalized = normalize_document_name(doc)
                        course_docs_normalized.append(doc_normalized)
                    logger.info('🚀 Pre-generating tutor responses for MULTI-DOCUMENT course with %s documents: %s', len(course_docs_normalized), ("" if course_docs_normalized is None else str(course_docs_normalized).replace("\n", "").replace("\r", "")))
        except Exception as e:
            logger.warning('Could not parse source_documents JSON: %s', e)
        
        # Fallback to single source_doc_name if source_documents is empty
        if not course_docs_normalized:
            course_doc_normalized = normalize_document_name(source_doc_name)
            course_docs_normalized = [course_doc_normalized]
            logger.info('🚀 Pre-generating tutor responses for SINGLE-DOCUMENT course: %s', ("" if course_doc_normalized is None else str(course_doc_normalized).replace("\n", "").replace("\r", "")))
        
        # Get all modules for this course (from ALL documents)
        modules = []
        paginator = s3_client.get_paginator('list_objects_v2')
        for page in paginator.paginate(Bucket=S3_BUCKET_NAME, Prefix=f"{user_id}/generated_reports/"):
            for obj in page.get('Contents', []):
                if obj['Key'].endswith('.json'):
                    try:
                        meta_obj = s3_client.get_object(Bucket=S3_BUCKET_NAME, Key=obj['Key'])
                        meta_data = json.loads(meta_obj['Body'].read().decode('utf-8'))
                        source_doc = meta_data.get('source_doc', '')
                        
                        # Normalize the metadata source_doc for comparison
                        source_doc_normalized = normalize_document_name(source_doc)
                        
                        # CRITICAL FIX: Check if this module matches ANY of the course documents
                        is_match = False
                        for course_doc_norm in course_docs_normalized:
                            if (source_doc_normalized == course_doc_norm or 
                                course_doc_norm in source_doc_normalized or
                                source_doc_normalized in course_doc_norm):
                                is_match = True
                                break
                        
                        if is_match:
                            # Get module content
                            md_key = obj['Key'].replace('.json', '.md')
                            report_obj = s3_client.get_object(Bucket=S3_BUCKET_NAME, Key=md_key)
                            module_content = report_obj['Body'].read().decode('utf-8')
                            
                            modules.append({
                                'index': meta_data.get('seg_index', 0),
                                'title': meta_data.get('title', 'Module'),
                                'content': module_content,
                                'source_doc': source_doc  # Keep for logging
                            })
                            logger.info('  ✓ Found module %s: %s (from %s)', ("" if meta_data.get('seg_index', 0) is None else str(meta_data.get('seg_index', 0)).replace("\n", "").replace("\r", "")), ("" if meta_data.get('title', 'Module')[:50] is None else str(meta_data.get('title', 'Module')[:50]).replace("\n", "").replace("\r", "")), ("" if source_doc is None else str(source_doc).replace("\n", "").replace("\r", "")))
                    except Exception as e:
                        logger.error('Error loading module metadata: %s', e)
        
        if not modules:
            logger.warning("No modules found for course '%s' (checked %s documents)", ("" if source_doc_name is None else str(source_doc_name).replace("\n", "").replace("\r", "")), len(course_docs_normalized))
            return 0
        
        # Sort modules by index
        modules.sort(key=lambda x: x['index'])
        logger.info('Found %s modules across %s document(s) to pre-generate responses for', len(modules), len(course_docs_normalized))
        
        total_tokens_saved = 0
        
        # Pre-generate tutor explanation for each module
        for module in modules:
            module_index = _normalize_module_index(module['index'])
            module_content = module['content']
            
            cache_key_content = build_tutor_cache_module_content(module_content)
            cached = get_cached_tutor_response(db, course_id, module_index, "explain_module", cache_key_content)
            if cached:
                logger.info('  ✓ Module %s already cached, skipping', ("" if module_index is None else str(module_index).replace("\n", "").replace("\r", "")))
                continue
            
            logger.info('  🤖 Generating tutor response for Module %s: %s... (from %s)', ("" if module_index is None else str(module_index).replace("\n", "").replace("\r", "")), ("" if module['title'][:50] is None else str(module['title'][:50]).replace("\n", "").replace("\r", "")), ("" if module.get('source_doc', 'unknown') is None else str(module.get('source_doc', 'unknown')).replace("\n", "").replace("\r", "")))
            
            try:
                # Generate the tutor explanation (same logic as /tutor/explain-module)
                # Process images in module content using CACHED analysis
                image_tags = extract_image_tags(module_content)
                s3_to_signed_url_map = {}
                image_analyses = {}
                
                # Load cached image analyses to avoid redundant AI calls
                cached_analyses = {}
                if image_tags:
                    logger.info('  📋 Loading cached analysis for %s images...', len(image_tags))
                    # Extract document name from S3 keys to load cached analyses
                    doc_names = set()
                    for s3_key in image_tags:
                        if '/extracted_images/' in s3_key:
                            parts = s3_key.split('/extracted_images/')
                            if len(parts) > 1:
                                doc_path = parts[1]
                                doc_name = doc_path.split('/')[0] if '/' in doc_path else doc_path
                                doc_names.add(doc_name)
                    
                    # Load cached analyses for all relevant documents
                    for doc_name in doc_names:
                        doc_analyses = load_image_analysis_from_s3(user_id, doc_name)
                        cached_analyses.update(doc_analyses)
                        logger.info('  📋 Loaded %s cached analyses for %s', len(doc_analyses), ("" if doc_name is None else str(doc_name).replace("\n", "").replace("\r", "")))
                
                # Generate signed URLs and use cached analysis
                for s3_key in image_tags:
                    try:
                        signed_url = get_s3_signed_url(s3_key, expiration=3600)
                        if signed_url:
                            s3_to_signed_url_map[s3_key] = signed_url
                            
                            # Use cached analysis instead of making new AI calls
                            if s3_key in cached_analyses:
                                cached_data = cached_analyses[s3_key]
                                vision_analysis = f"""**Image Analysis (Cached):**
Caption: {cached_data.get('caption', 'Image')}
Type: {cached_data.get('type', 'unknown')}
Keywords: {', '.join(cached_data.get('keywords', []))}
Topics: {', '.join(cached_data.get('topics', []))}
Content: {cached_data.get('content', 'No description available')}"""
                                image_analyses[s3_key] = vision_analysis
                                logger.info('  ✓ Using cached analysis for %s', ("" if s3_key is None else str(s3_key).replace("\n", "").replace("\r", "")))
                            else:
                                # Fallback: Generate new analysis only if no cache exists
                                logger.warning('  ⚠ No cached analysis for %s, generating new analysis...', ("" if s3_key is None else str(s3_key).replace("\n", "").replace("\r", "")))
                                image_bytes = get_image_from_s3_as_bytes(s3_key)
                                if image_bytes:
                                    image_pos = module_content.find(f'[IMAGE: {s3_key}]')
                                    if image_pos != -1:
                                        context_before = module_content[max(0, image_pos - 2000):image_pos]
                                        context_after = module_content[image_pos + len(f'[IMAGE: {s3_key}]'):min(len(module_content), image_pos + len(f'[IMAGE: {s3_key}]') + 2000)]
                                        vision_analysis = analyze_image_with_vision_bytes(image_bytes, context_before, context_after, s3_key)
                                        image_analyses[s3_key] = vision_analysis
                    except Exception as e:
                        logger.error('Error processing image %s: %s', ("" if s3_key is None else str(s3_key).replace("\n", "").replace("\r", "")), e)
                
                # Replace image tags with signed URLs and vision analysis
                # CRITICAL FIX: Show IMAGE FIRST, then analysis below (better UX)
                module_content_with_signed_urls = module_content
                for s3_key, signed_url in s3_to_signed_url_map.items():
                    vision_context = ""
                    if s3_key in image_analyses:
                        # Use the same label the prompt expects: VISION ANALYSIS FOR THIS IMAGE
                        vision_context = f"\n\n**VISION ANALYSIS FOR THIS IMAGE:**\n{image_analyses[s3_key]}\n\n"
                    
                    if f'[IMAGE: {s3_key}]' in module_content_with_signed_urls:
                        # Vision analysis BEFORE the image tag so the prompt can read it first
                        replacement = f'{vision_context}[IMAGE_URL: {signed_url}]'
                        module_content_with_signed_urls = module_content_with_signed_urls.replace(f'[IMAGE: {s3_key}]', replacement)
                
                # Add image instruction if images are present
                image_instruction = ""
                if s3_to_signed_url_map:
                    valid_image_urls = list(s3_to_signed_url_map.values())
                    image_instruction = f"\n\nIMPORTANT: When referencing images in your response, ONLY use these exact IMAGE_URL tags that are provided in the content. Do NOT create new image filenames or modify existing ones:\n{valid_image_urls}\n\nIf you need to reference an image, copy the exact [IMAGE_URL: ...] tag from the content above."

                # Generate tutor explanation
                full_prompt = f"{TUTOR_AGENT_INSTRUCTIONS}{image_instruction}\n\nHere is the module content:\n\n{module_content_with_signed_urls}"
                lecture_script = tutor_agent.run(full_prompt).content
                
                response_data = {
                    "lecture_script": lecture_script,
                    "image_urls": list(s3_to_signed_url_map.values())
                }
                
                # Estimate tokens and cache
                estimated_tokens = len(full_prompt.split()) + len(lecture_script.split())
                cache_tutor_response(db, course_id, module_index, "explain_module", cache_key_content, response_data, estimated_tokens, user_id)
                
                total_tokens_saved += estimated_tokens
                logger.info('  ✅ Cached Module %s response (%s tokens)', ("" if module_index is None else str(module_index).replace("\n", "").replace("\r", "")), ("" if estimated_tokens is None else str(estimated_tokens).replace("\n", "").replace("\r", "")))
                
            except Exception as e:
                logger.error('  ❌ Error generating response for Module %s: %s', ("" if module_index is None else str(module_index).replace("\n", "").replace("\r", "")), e)
        
        logger.info('✅ Pre-generation complete! Generated %s tutor responses from %s document(s) (%s tokens)', len(modules), len(course_docs_normalized), ("" if total_tokens_saved is None else str(total_tokens_saved).replace("\n", "").replace("\r", "")))
        return total_tokens_saved
        
    except Exception as e:
        logger.error('Error pre-generating tutor responses: %s', e, exc_info=True)
        return 0

def get_system_setting(db: Session, key: str, default: str = "false") -> str:
    """Get system setting value"""
    setting = db.query(SystemSettings).filter(SystemSettings.setting_key == key).first()
    return setting.setting_value if setting else default

def set_system_setting(db: Session, key: str, value: str):
    """Set system setting value"""
    setting = db.query(SystemSettings).filter(SystemSettings.setting_key == key).first()
    if setting:
        setting.setting_value = value
        setting.updated_at = datetime.now(timezone.utc)
    else:
        setting = SystemSettings(setting_key=key, setting_value=value)
        db.add(setting)
    db.commit()

def update_course_with_pdf_hash(db: Session, user_id: str, source_doc_name: str, pdf_content: bytes, estimated_tokens: int = 0):
    """Update course with PDF hash and token usage after module generation"""
    try:
        pdf_hash = calculate_pdf_hash(pdf_content)
        
        course = db.query(Course).filter(
            Course.author_id == user_id,
            Course.source_doc_name == source_doc_name
        ).first()
        
        if course:
            course.pdf_hash = pdf_hash
            course.tokens_used = estimated_tokens
            # Calculate cost using new pricing model
            estimated_input_tokens = estimated_tokens // 3  # Rough estimate for document processing
            estimated_output_tokens = estimated_tokens - estimated_input_tokens
            course.generation_cost = calculate_token_cost(estimated_input_tokens, estimated_output_tokens, BEDROCK_GENERATION_MODEL)
            db.commit()
            logger.info("✅ Updated course '%s' with PDF hash and %s tokens", ("" if source_doc_name is None else str(source_doc_name).replace("\n", "").replace("\r", "")), ("" if estimated_tokens is None else str(estimated_tokens).replace("\n", "").replace("\r", "")))
            
            # Track token usage with updated pricing - use estimated tokens for input calculation
            estimated_input_tokens_for_tracking = estimated_tokens // 4 * 3  # Assume 75% input tokens
            # Calculate output tokens with proper bounds checking
            if estimated_tokens > estimated_input_tokens_for_tracking:
                estimated_output_tokens_for_tracking = estimated_tokens - estimated_input_tokens_for_tracking
            else:
                estimated_output_tokens_for_tracking = estimated_tokens // 4
            track_token_usage(db, user_id, "module_generation", estimated_input_tokens_for_tracking, estimated_output_tokens_for_tracking, course.id, BEDROCK_GENERATION_MODEL)
        else:
            logger.warning('⚠️ Course not found for PDF hash update: %s', ("" if source_doc_name is None else str(source_doc_name).replace("\n", "").replace("\r", "")))
    except Exception as e:
        logger.error('Error updating course with PDF hash: %s', e)


__all__ = [name for name in globals() if not name.startswith('_')]
