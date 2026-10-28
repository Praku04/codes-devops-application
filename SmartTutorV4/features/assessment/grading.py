"""Shared MCQ + subjective grading for module and end-of-course assessments."""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from core.runtime import BEDROCK_GENERATION_MODEL, S3_BUCKET_NAME, logger, s3_client, sync_bedrock_client
from models.domain import Course
from services.knowledge_base import normalize_document_name
from utils.security_helpers import log_safe

# Keep Bedrock prompts within a safe size for end-of-course (all modules) grading.
GRADING_CONTEXT_MAX_CHARS = 100_000


def normalize_question_type(raw_type: Any) -> str:
    """Normalize question type labels to ``mcq`` or ``subjective``."""
    t = str(raw_type or "mcq").strip().lower()
    if t in ("mcq", "multiple_choice", "multiple-choice"):
        return "mcq"
    return "subjective"


def _truncate_grading_context(content: str, max_chars: int = GRADING_CONTEXT_MAX_CHARS) -> str:
    text = (content or "").strip()
    if len(text) <= max_chars:
        return text
    return (
        text[:max_chars]
        + "\n\n[Content truncated for evaluation — focus on the key concepts above.]"
    )


def grading_context_for_question(
    course: Course,
    scope_module_index: Optional[int],
    question: Dict[str, Any],
) -> str:
    """
    Resolve markdown ground-truth for one question.
    Module assessments use the module scope; end-of-course uses full course content
    unless the question carries its own ``module_index``.
    """
    if scope_module_index is not None:
        module_idx: Optional[int] = scope_module_index
    else:
        q_module = question.get("module_index")
        if q_module is not None:
            try:
                module_idx = int(q_module)
            except (TypeError, ValueError):
                module_idx = None
        else:
            module_idx = None
    return _truncate_grading_context(load_grading_content_for_scope(course, module_idx))


def _course_docs_normalized_list(course: Course) -> List[str]:
    docs: List[str] = []
    if course.source_documents:
        try:
            raw = json.loads(course.source_documents)
            if isinstance(raw, list):
                docs = [normalize_document_name(d) for d in raw]
        except Exception:
            pass
    if not docs:
        docs = [normalize_document_name(course.source_doc_name)]
    return docs


def _module_metadata_matches_course(meta_data: Dict[str, Any], course: Course, course_docs: List[str]) -> bool:
    meta_course_id = meta_data.get("course_id")
    if meta_course_id:
        return meta_course_id == course.id
    src = normalize_document_name(meta_data.get("source_doc", ""))
    return any(src == d for d in course_docs)


def _collect_ordered_module_md_keys(course: Course) -> List[str]:
    """
    Return markdown S3 keys in sequential module order (1, 2, 3…).
    Matches ``_get_modules_for_course`` / trainee dashboard ordering.
    """
    if not course:
        return []

    course_docs = _course_docs_normalized_list(course)
    seen_keys: Set[Tuple[str, int]] = set()
    modules: List[Dict[str, Any]] = []

    try:
        paginator = s3_client.get_paginator("list_objects_v2")
        for page in paginator.paginate(
            Bucket=S3_BUCKET_NAME,
            Prefix=f"{course.author_id}/generated_reports/",
        ):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if not key.endswith(".json"):
                    continue
                try:
                    meta_obj = s3_client.get_object(Bucket=S3_BUCKET_NAME, Key=key)
                    meta_data = json.loads(meta_obj["Body"].read().decode("utf-8"))
                    if not _module_metadata_matches_course(meta_data, course, course_docs):
                        continue

                    seg_index = int(meta_data.get("seg_index", 0) or 0)
                    src = normalize_document_name(meta_data.get("source_doc", ""))
                    dedup_key = (src, seg_index)
                    if dedup_key in seen_keys:
                        continue
                    seen_keys.add(dedup_key)

                    modules.append({
                        "seg_index": seg_index,
                        "md_key": key.replace(".json", ".md"),
                        "s3_key": key,
                    })
                except Exception as meta_err:
                    logger.warning(
                        "[GRADING] Error reading module metadata %s: %s",
                        log_safe(key),
                        log_safe(meta_err),
                    )
    except Exception as exc:
        logger.warning(
            "[GRADING] Failed to list modules for course %s: %s",
            ("" if course.id is None else str(course.id).replace("\n", "").replace("\r", "")),
            log_safe(exc),
        )
        return []

    modules.sort(key=lambda m: (m["seg_index"], m["s3_key"]))
    return [m["md_key"] for m in modules]


def _read_s3_text(key: str) -> str:
    try:
        obj = s3_client.get_object(Bucket=S3_BUCKET_NAME, Key=key)
        return obj["Body"].read().decode("utf-8")
    except Exception as load_err:
        logger.warning("[GRADING] Could not load S3 object %s: %s", log_safe(key), log_safe(load_err))
        return ""


def load_grading_content_for_scope(course: Course, module_index: Optional[int]) -> str:
    """Load markdown ground-truth: one module, or all modules concatenated for end-of-course."""
    try:
        md_keys = _collect_ordered_module_md_keys(course)
        if not md_keys:
            return ""

        if module_index is not None:
            idx = module_index - 1
            if idx < 0 or idx >= len(md_keys):
                logger.warning(
                    "[GRADING] module_index %s out of range (%s modules) for course %s",
                    ("" if module_index is None else str(module_index).replace("\n", "").replace("\r", "")),
                    len(md_keys),
                    ("" if course.id is None else str(course.id).replace("\n", "").replace("\r", "")),
                )
                return ""
            return _read_s3_text(md_keys[idx])

        parts = [_read_s3_text(k) for k in md_keys]
        return "\n\n".join(p for p in parts if p)
    except Exception as exc:
        logger.warning(
            "[GRADING] Failed to load grading content for course %s: %s",
            ("" if course.id is None else str(course.id).replace("\n", "").replace("\r", "")),
            log_safe(exc),
        )
        return ""


def load_module_content_for_grading(course: Course, module_index: int) -> str:
    """Load markdown for a single module by sequential index."""
    return load_grading_content_for_scope(course, module_index)


def normalize_mcq_selections(raw_answer: Any, question: Optional[Dict[str, Any]] = None) -> Set[str]:
    """Normalize MCQ answers into a canonical set of option letters (A-D)."""
    if raw_answer is None:
        return set()

    text = str(raw_answer).strip()
    if not text:
        return set()

    def _norm_text(s: str) -> str:
        return re.sub(r"[^A-Z0-9]+", " ", (s or "").upper()).strip()

    option_text_to_letter: Dict[str, str] = {}
    if question:
        for letter, key in [("A", "option_a"), ("B", "option_b"), ("C", "option_c"), ("D", "option_d")]:
            opt_val = question.get(key)
            if opt_val:
                option_text_to_letter[_norm_text(str(opt_val))] = letter

    parts = re.split(r"[,;/|]|\n|&|\band\b", text, flags=re.IGNORECASE)
    selections: Set[str] = set()

    for part in parts:
        token = part.strip()
        if not token:
            continue

        token_upper = token.upper()
        m = re.match(r"^\(?\s*([A-D])\s*[\)\.]?$", token_upper)
        if m:
            selections.add(m.group(1))
            continue

        m = re.match(r"^([A-D])\s*[:\-\)]", token_upper)
        if m:
            selections.add(m.group(1))
            continue

        mapped = option_text_to_letter.get(_norm_text(token))
        if mapped:
            selections.add(mapped)

    return selections


def _resolve_mcq_correct_options_with_ai(module_content: str, question: Dict[str, Any]) -> Set[str]:
    """Resolve correct MCQ option letters using module ground truth."""
    try:
        if not module_content:
            return set()

        qtext = question.get("question", question.get("question_text", ""))
        options = {
            "A": question.get("option_a", ""),
            "B": question.get("option_b", ""),
            "C": question.get("option_c", ""),
            "D": question.get("option_d", ""),
        }

        prompt = f"""You are validating a multiple-choice question against source module content.
Return ONLY valid JSON with this shape:
{{"correct_options":["A"],"reason":"short reason"}}

Rules:
- Determine the correct option letter(s) strictly from module content.
- Do not rely on any template answer key.
- If unsure, return an empty array.

Module Content:
---
{module_content}
---

Question:
{qtext}

Options:
A) {options['A']}
B) {options['B']}
C) {options['C']}
D) {options['D']}
"""

        request_body = {
            "messages": [{"role": "user", "content": [{"text": prompt}]}],
            "inferenceConfig": {"maxTokens": 300, "temperature": 0.1, "topP": 0.9},
        }
        response = sync_bedrock_client.invoke_model(
            modelId=BEDROCK_GENERATION_MODEL,
            body=json.dumps(request_body),
            contentType="application/json",
        )
        response_body = json.loads(response["body"].read())
        text = response_body["output"]["message"]["content"][0]["text"]

        try:
            parsed = json.loads(text)
        except Exception:
            m = re.search(r"\{.*\}", text, re.DOTALL)
            if not m:
                return set()
            parsed = json.loads(m.group(0))

        raw_options = parsed.get("correct_options", [])
        if not isinstance(raw_options, list):
            return set()

        resolved: Set[str] = set()
        for opt in raw_options:
            val = str(opt).strip().upper()
            if val in {"A", "B", "C", "D"}:
                resolved.add(val)
        return resolved
    except Exception as exc:
        logger.warning("[MCQ VALIDATION] AI resolution failed: %s", log_safe(exc))
        return set()


def _resolve_mcq_correct_options_general(question: Dict[str, Any]) -> Set[str]:
    """Resolve MCQ option letters using question/options only (fallback)."""
    try:
        qtext = question.get("question", question.get("question_text", ""))
        options = {
            "A": question.get("option_a", ""),
            "B": question.get("option_b", ""),
            "C": question.get("option_c", ""),
            "D": question.get("option_d", ""),
        }

        prompt = f"""You are validating a multiple-choice question.
Return ONLY valid JSON with this exact shape:
{{"correct_options":["A"],"confidence":"high|medium|low","reason":"short reason"}}

Rules:
- Choose the best option(s) based on technical correctness.
- If uncertain, return an empty array.
- Never return anything outside A/B/C/D.

Question:
{qtext}

Options:
A) {options['A']}
B) {options['B']}
C) {options['C']}
D) {options['D']}
"""

        request_body = {
            "messages": [{"role": "user", "content": [{"text": prompt}]}],
            "inferenceConfig": {"maxTokens": 250, "temperature": 0.05, "topP": 0.9},
        }
        response = sync_bedrock_client.invoke_model(
            modelId=BEDROCK_GENERATION_MODEL,
            body=json.dumps(request_body),
            contentType="application/json",
        )
        response_body = json.loads(response["body"].read())
        text = response_body["output"]["message"]["content"][0]["text"]

        try:
            parsed = json.loads(text)
        except Exception:
            m = re.search(r"\{.*\}", text, re.DOTALL)
            if not m:
                return set()
            parsed = json.loads(m.group(0))

        raw_options = parsed.get("correct_options", [])
        if not isinstance(raw_options, list):
            return set()

        resolved: Set[str] = set()
        for opt in raw_options:
            val = str(opt).strip().upper()
            if val in {"A", "B", "C", "D"}:
                resolved.add(val)
        return resolved
    except Exception as exc:
        logger.warning("[MCQ VALIDATION] General resolution failed: %s", log_safe(exc))
        return set()


def adjudicate_mcq_correct_options(
    question: Dict[str, Any],
    user_answer: str,
    template_correct_answer: str,
    module_content: str,
) -> Set[str]:
    """
    Determine final correct options for an MCQ using layered strategy:
    1) Template answer when user matches
    2) Module-content-based AI resolution
    3) General-knowledge AI resolution
    4) Template answer fallback
    """
    template_sel = normalize_mcq_selections(template_correct_answer, question)
    user_sel = normalize_mcq_selections(user_answer, question)

    if user_sel and user_sel == template_sel:
        return template_sel

    module_resolved = (
        _resolve_mcq_correct_options_with_ai(module_content, question) if module_content else set()
    )
    if module_resolved:
        return module_resolved

    general_resolved = _resolve_mcq_correct_options_general(question)
    if general_resolved:
        return general_resolved

    return template_sel


def _subjective_fallback_score(user_answer: str, weightage: int) -> Tuple[int, str]:
    """Length-based fallback when AI grading is unavailable."""
    answer_length = len(user_answer.strip())
    answer_words = len(user_answer.strip().split())

    if answer_length >= 200 and answer_words >= 30:
        question_score = round(weightage * 0.85)
    elif answer_length >= 150 and answer_words >= 20:
        question_score = round(weightage * 0.80)
    elif answer_length >= 100 and answer_words >= 15:
        question_score = round(weightage * 0.75)
    elif answer_length >= 50 and answer_words >= 8:
        question_score = round(weightage * 0.65)
    elif answer_length > 50:
        question_score = round(weightage * 0.70)
    elif answer_length > 20:
        question_score = round(weightage * 0.55)
    else:
        question_score = round(weightage * 0.50)

    actual_percentage = round((question_score / weightage) * 100) if weightage > 0 else 0
    feedback = (
        f"Score: {question_score}/{weightage} marks ({actual_percentage}%). "
        "Your answer has been evaluated."
    )
    return question_score, feedback


def _score_subjective_answer(
    *,
    qid: str,
    qtext: str,
    user_answer: str,
    grading_context: str,
    weightage_per_question: int,
) -> Tuple[int, str]:
    """AI-grade a subjective answer against module/course ground-truth content."""
    grading_context = grading_context if grading_context else f"Question context: {qtext}"
    ai_feedback = "No answer provided."

    if not user_answer:
        return 0, ai_feedback

    evaluation_prompt = f"""You are an expert educational evaluator. Your task is to evaluate a trainee's answer by comparing it with the provided ground truth content from the learning module.

GROUND TRUTH CONTENT (Module Context):
{grading_context}

QUESTION BEING EVALUATED:
{qtext}

TRAINEE'S ANSWER:
{user_answer}

EVALUATION CRITERIA - BE GENEROUS WITH PARTIAL CREDIT:
1. **Accuracy (25%)**: How factually correct is the answer compared to the ground truth?
2. **Completeness (25%)**: Does the answer cover the key points from the module content?
3. **Understanding (25%)**: Does the answer demonstrate comprehension of concepts from the ground truth?
4. **Relevance (25%)**: How well does the answer address the specific question asked?

SCORING GUIDELINES - GIVE PARTIAL CREDIT FOR EFFORT AND UNDERSTANDING:
- 90-100%: Excellent answer that accurately reflects ground truth content with complete understanding
- 80-89%: Good answer with minor gaps but demonstrates solid understanding of module content
- 70-79%: Satisfactory answer with some inaccuracies but shows basic understanding of key concepts
- 60-69%: Basic answer that shows some understanding, even if incomplete or partially incorrect
- 50-59%: Minimal answer that shows some effort and basic awareness of the topic
- 40-49%: Poor answer but shows some attempt to address the question
- 30-39%: Very poor answer but demonstrates minimal understanding
- 20-29%: Inadequate answer with little relevance but shows some effort
- 10-19%: Very inadequate answer but trainee attempted to respond
- 0-9%: No meaningful content or completely irrelevant answer

IMPORTANT: Give partial credit for any relevant content, even if not perfectly aligned with ground truth.

This question is worth {weightage_per_question} marks total. Please provide a percentage score (0-100).

REQUIRED RESPONSE FORMAT:
Score: [0-100]
Feedback: [Detailed explanation of the score]"""

    try:
        request_body = {
            "messages": [{"role": "user", "content": [{"text": evaluation_prompt}]}],
            "inferenceConfig": {"maxTokens": 1500, "temperature": 0.3, "topP": 0.9},
        }
        response = sync_bedrock_client.invoke_model(
            modelId=BEDROCK_GENERATION_MODEL,
            body=json.dumps(request_body),
            contentType="application/json",
        )
        response_body = json.loads(response["body"].read())
        ai_response = response_body["output"]["message"]["content"][0]["text"]

        lines = ai_response.strip().split("\n")
        score_percentage = 60
        score_found = False
        feedback_lines: List[str] = []

        for i, line in enumerate(lines):
            line = line.strip()
            if not score_found and ("score:" in line.lower() or "score =" in line.lower()):
                score_match = re.search(r"(\d+)", line)
                if score_match:
                    score_percentage = max(0, min(100, int(score_match.group(1))))
                    score_found = True
            elif "feedback:" in line.lower():
                feedback_text = line.split(":", 1)[-1].strip()
                if feedback_text:
                    feedback_lines.append(feedback_text)
                feedback_lines.extend([l.strip() for l in lines[i + 1 :] if l.strip()])
                break
            elif score_found and line:
                feedback_lines.append(line)

        if feedback_lines:
            ai_feedback = " ".join(feedback_lines).strip()

        if not score_found:
            for match in re.findall(r"(\d+)%?", ai_response):
                potential_score = int(match)
                if 0 <= potential_score <= 100:
                    score_percentage = potential_score
                    break

        question_score = min(
            round((score_percentage / 100.0) * weightage_per_question),
            weightage_per_question,
        )
        actual_percentage = (
            round((question_score / weightage_per_question) * 100)
            if weightage_per_question > 0
            else 0
        )
        ai_feedback = (
            f"Score: {question_score}/{weightage_per_question} marks ({actual_percentage}%). "
            f"{ai_feedback}"
        )
        logger.info(
            "[AI EVALUATION] Question %s: %s%% -> %s/%s marks",
            ("" if qid is None else str(qid).replace("\n", "").replace("\r", "")),
            score_percentage,
            question_score,
            weightage_per_question,
        )
        return question_score, ai_feedback
    except Exception as bedrock_error:
        logger.error(
            "[AI EVALUATION] Nova Pro failed for question %s: %s",
            ("" if qid is None else str(qid).replace("\n", "").replace("\r", "")),
            log_safe(bedrock_error),
        )
        question_score, ai_feedback = _subjective_fallback_score(user_answer, weightage_per_question)
        return question_score, f"{ai_feedback} (AI evaluation temporarily unavailable)"


def grade_trainee_assessment_answers(
    *,
    unique_qs: List[Dict[str, Any]],
    answers: Dict[str, Any],
    weightage_per_question: int,
    course: Course,
    scope_module_index: Optional[int] = None,
) -> Dict[str, Any]:
    """Grade MCQ and subjective questions using the same rules for module and course assessments."""
    weightage = max(1, int(weightage_per_question or 1))
    score = 0
    total_possible = len(unique_qs) * weightage
    detailed_results: List[Dict[str, Any]] = []
    mcq_results_map: Dict[str, bool] = {}
    resolved_correct_answers_map: Dict[str, str] = {}
    subjective_results_map: Dict[str, Dict[str, Any]] = {}

    for question in unique_qs:
        qid = question["id"]
        qtype = normalize_question_type(question.get("type", question.get("question_type", "mcq")))
        qtext = question.get("question", question.get("question_text", ""))
        correct_ans = question.get("correct_answer", "")
        explanation = question.get("explanation", "")
        question_context = grading_context_for_question(course, scope_module_index, question)

        user_answer = str(answers.get(str(qid), answers.get(qid, ""))).strip()
        question_score = 0

        if qtype == "mcq":
            user_selections = normalize_mcq_selections(user_answer, question)
            resolved_correct_selections = adjudicate_mcq_correct_options(
                question=question,
                user_answer=user_answer,
                template_correct_answer=correct_ans,
                module_content=question_context,
            )

            is_mcq_correct = (
                user_selections == resolved_correct_selections and len(user_selections) > 0
            )
            resolved_correct_label = (
                ",".join(sorted(resolved_correct_selections))
                if resolved_correct_selections
                else str(correct_ans)
            )
            mcq_results_map[str(qid)] = bool(is_mcq_correct)
            resolved_correct_answers_map[str(qid)] = resolved_correct_label

            if is_mcq_correct:
                question_score = weightage
                score += question_score
                feedback = f"Correct! The answer is {resolved_correct_label}."
            else:
                feedback = (
                    f"Incorrect. The correct answer is {resolved_correct_label}. "
                    f"{explanation or ''}"
                )

            detailed_results.append({
                "question_id": qid,
                "question_text": qtext,
                "user_answer": user_answer,
                "correct_answer": resolved_correct_label,
                "score": question_score,
                "max_score": weightage,
                "explanation": explanation,
                "feedback": feedback,
                "question_type": "mcq",
            })
        else:
            if user_answer:
                question_score, ai_feedback = _score_subjective_answer(
                    qid=str(qid),
                    qtext=qtext,
                    user_answer=user_answer,
                    grading_context=question_context,
                    weightage_per_question=weightage,
                )
                score += question_score
            else:
                question_score = 0
                ai_feedback = "No answer provided."

            subjective_results_map[str(qid)] = {
                "score": question_score,
                "max_score": weightage,
                "feedback": ai_feedback,
            }

            detailed_results.append({
                "question_id": qid,
                "question_text": qtext,
                "user_answer": user_answer,
                "score": question_score,
                "max_score": weightage,
                "feedback": ai_feedback,
                "question_type": "subjective",
            })

    return {
        "score": score,
        "total_possible": total_possible,
        "detailed_results": detailed_results,
        "mcq_results_map": mcq_results_map,
        "resolved_correct_answers_map": resolved_correct_answers_map,
        "subjective_results_map": subjective_results_map,
    }


def sanitize_detailed_results_for_trainee(
    detailed_results: List[Dict[str, Any]],
    *,
    passed: bool,
) -> List[Dict[str, Any]]:
    """
    Trainee-facing submit payloads must not leak answer keys on failed attempts.
    Pass keeps full feedback; fail keeps only what the trainee submitted + score marks.
    """
    if passed or not detailed_results:
        return detailed_results

    sanitized: List[Dict[str, Any]] = []
    for row in detailed_results:
        if not isinstance(row, dict):
            continue
        qtype = normalize_question_type(row.get("question_type", "mcq"))
        max_score = row.get("max_score")
        score = row.get("score")
        clean: Dict[str, Any] = {
            "question_id": row.get("question_id"),
            "question_text": row.get("question_text"),
            "user_answer": row.get("user_answer"),
            "question_type": qtype,
        }
        if qtype == "mcq":
            clean["score"] = score
            clean["max_score"] = max_score
            if max_score is not None:
                try:
                    clean["is_correct"] = float(score or 0) >= float(max_score) and float(max_score) > 0
                except (TypeError, ValueError):
                    clean["is_correct"] = False
        # Subjective: keep trainee text only — no evaluator score/feedback on fail.
        sanitized.append(clean)
    return sanitized
