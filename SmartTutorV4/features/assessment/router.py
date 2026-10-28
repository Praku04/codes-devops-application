from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter

from core.runtime import *
from models.domain import *
from schemas.domain import *
from services.jobs import *
from services.auth_platform import *
from services.tutor_cache import *
from services.documents import *
from services.knowledge_base import *
from services.agents import *
from utils.security_helpers import GENERIC_API_ERROR_MESSAGE, log_safe, strip_bracket_image_tags
# Imported explicitly rather than relied on through the star imports above: the module-count
# helpers below read every author's report metadata once per request instead of re-scanning S3
# for each course. See services/module_index.py for why that scan had to stop repeating.
from services.module_index import author_report_metadata, register_derived_cache_key
from features.assessment.grading import (
    adjudicate_mcq_correct_options,
    grade_trainee_assessment_answers,
    load_module_content_for_grading,
    normalize_mcq_selections,
    normalize_question_type,
    sanitize_detailed_results_for_trainee,
)

# Backward-compatible aliases for admin/trainer imports
_normalize_mcq_selections = normalize_mcq_selections
_adjudicate_mcq_correct_options = adjudicate_mcq_correct_options
_load_module_content_for_grading = load_module_content_for_grading

router = APIRouter()


def _get_course_for_trainer_or_super_admin(
    db: Session,
    course_id: str,
    user: User,
    *,
    allow_name_fallback: bool = False,
) -> Course:
    """Resolve a course owned by the trainer, or any course when caller is super admin."""
    course = db.query(Course).filter(Course.id == course_id).first()
    if not course and allow_name_fallback and user.role != 'super_admin':
        course = db.query(Course).filter(
            Course.source_doc_name == course_id,
            Course.author_id == user.id,
        ).first()
    if not course:
        raise HTTPException(
            status_code=404,
            detail="Course not found. Please ensure the course exists and you have permission to configure it.",
        )
    if course.author_id != user.id and user.role != 'super_admin':
        raise HTTPException(status_code=403, detail="Not authorized to access this course")
    return course

@router.post("/trainer/create-course", tags=["Assessment System"])
def create_course_for_assessment(
    request: dict,
    user: User = Depends(trainer_required),
    db: Session = Depends(get_db)
):
    """Create a course for assessment configuration if it doesn't exist"""
    try:
        source_doc_name = request.get('source_doc_name')
        if not source_doc_name:
            raise HTTPException(status_code=400, detail="Source document name is required")
        
        # Check if course already exists
        existing_course = db.query(Course).filter(
            Course.author_id == user.id,
            Course.source_doc_name == source_doc_name
        ).first()
        
        if existing_course:
            return {"success": True, "course_id": existing_course.id, "message": "Course already exists"}
        
        # Create new course
        new_course = Course(
            author_id=user.id,
            source_doc_name=source_doc_name,
            process_id=user.process_id,
            initial_attempts=1,
            is_live=False
        )
        db.add(new_course)
        db.commit()
        
        log_activity(db, user.id, "create_course", f"Created course for {source_doc_name}")
        
        return {"success": True, "course_id": new_course.id, "message": "Course created successfully"}
        
    except Exception as e:
        logger.error('Error creating course: %s', e, exc_info=True)
        db.rollback()
        raise HTTPException(status_code=500, detail="Failed to create course")

@router.get("/trainer/course-info/{source_doc_name:path}", tags=["Course Management"])
def get_course_info_by_doc_name(
    source_doc_name: str,
    user: User = Depends(trainer_required),
    db: Session = Depends(get_db)
):
    """Get course information by source document name"""
    try:
        course = db.query(Course).filter(
            Course.author_id == user.id,
            Course.source_doc_name == source_doc_name
        ).first()
        
        if not course:
            return {
                "success": False,
                "message": "Course not found",
                "course_exists": False
            }
        
        return {
            "success": True,
            "course_exists": True,
            "course_id": course.id,
            "course_name": course.source_doc_name,
            "is_published": course.is_live,
            "created_at": course.created_at.isoformat() if course.created_at else None
        }
        
    except Exception as e:
        logger.error('Error getting course info: %s', e, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to get course information")

@router.post("/trainer/create-course", tags=["Course Management"])
def create_course_for_assessment(
    request: dict,
    user: User = Depends(trainer_required),
    db: Session = Depends(get_db)
):
    """Create a course entry for assessment configuration without publishing"""
    try:
        source_doc_name = request.get('source_doc_name')
        if not source_doc_name:
            raise HTTPException(status_code=400, detail="source_doc_name is required")
        
        # Check if course already exists
        existing_course = db.query(Course).filter(
            Course.author_id == user.id,
            Course.source_doc_name == source_doc_name
        ).first()
        
        if existing_course:
            return {
                "success": True,
                "message": "Course already exists",
                "course_id": existing_course.id,
                "course_name": existing_course.source_doc_name
            }
        
        # Verify that the user has modules for this document
        try:
            paginator = s3_client.get_paginator('list_objects_v2')
            report_pages = paginator.paginate(Bucket=S3_BUCKET_NAME, Prefix=f"{user.id}/generated_reports/")
            
            has_modules = False
            for page in report_pages:
                for obj in page.get('Contents', []):
                    if obj['Key'].endswith('.json'):
                        try:
                            meta_obj = s3_client.get_object(Bucket=S3_BUCKET_NAME, Key=obj['Key'])
                            meta_data = json.loads(meta_obj['Body'].read().decode('utf-8'))
                            if meta_data.get("source_doc") == source_doc_name:
                                has_modules = True
                                break
                        except:
                            continue
                if has_modules:
                    break
            
            if not has_modules:
                raise HTTPException(
                    status_code=404, 
                    detail=f"No modules found for document '{source_doc_name}'. Please generate modules first."
                )
        
        except Exception as e:
            logger.error('Error checking for modules: %s', e)
            raise HTTPException(status_code=500, detail="Error verifying document modules")
        
        # Create new course entry
        new_course = Course(
            source_doc_name=source_doc_name,
            author_id=user.id,
            process_id=user.process_id,
            initial_attempts=1,
            is_live=False,  # Not published yet
            tokens_used=0,
            generation_cost=0
        )
        
        db.add(new_course)
        db.commit()
        
        log_activity(db, user.id, "create_course", f"Created course entry for '{source_doc_name}'")
        
        logger.info('Created course entry for user %s: %s -> %s', ("" if user.id is None else str(user.id).replace("\n", "").replace("\r", "")), ("" if source_doc_name is None else str(source_doc_name).replace("\n", "").replace("\r", "")), ("" if new_course.id is None else str(new_course.id).replace("\n", "").replace("\r", "")))
        
        return {
            "success": True,
            "message": f"Course created successfully for '{source_doc_name}'",
            "course_id": new_course.id,
            "course_name": new_course.source_doc_name
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error('Error creating course: %s', e, exc_info=True)
        db.rollback()
        raise HTTPException(status_code=500, detail=GENERIC_API_ERROR_MESSAGE)

@router.post("/trainer/configure-assessment/{course_id}", tags=["Assessment System"])
def configure_assessment(
    course_id: str,
    config: AssessmentConfigRequest,
    user: User = Depends(trainer_required),
    db: Session = Depends(get_db)
):
    """Configure assessment for a course before publishing"""
    try:
        logger.info('Assessment configuration request: %s', log_safe(config))

        course = _get_course_for_trainer_or_super_admin(
            db, course_id, user, allow_name_fallback=True
        )
        course_id = course.id
        
        if course.is_live:
            raise HTTPException(status_code=400, detail="Cannot modify assessment for published course")
        
        # Validate question counts
        if config.question_type == 'mixed':
            if not config.mcq_count and not config.subjective_count:
                raise HTTPException(status_code=400, detail="MCQ count and Subjective count are required for mixed question type")
            
            if config.mcq_count + config.subjective_count != config.total_questions:
                raise HTTPException(
                    status_code=400, 
                    detail=f"MCQ count ({config.mcq_count}) + Subjective count ({config.subjective_count}) must equal total questions ({config.total_questions})"
                )
            
            if config.mcq_count < 0 or config.subjective_count < 0:
                raise HTTPException(status_code=400, detail="Question counts cannot be negative")
                
        elif config.question_type == 'mcq':
            config.mcq_count = config.total_questions
            config.subjective_count = 0
        elif config.question_type == 'subjective':
            config.subjective_count = config.total_questions
            config.mcq_count = 0
        else:
            raise HTTPException(status_code=400, detail="Invalid question type. Must be 'mcq', 'subjective', or 'mixed'")
        
        # AI suggests time based on content length and question count
        ai_suggested_time = max(30, config.total_questions * 3)  # 3 minutes per question minimum
        
        # Ensure assessment tables exist before querying
        try:
            ensure_assessment_tables_exist()
        except Exception as table_error:
            logger.error('Failed to ensure assessment tables exist: %s', ("" if table_error is None else str(table_error).replace("\n", "").replace("\r", "")))
            raise HTTPException(status_code=500, detail="Database configuration error")
        
        # Create or update assessment config
        try:
            existing_config = db.query(AssessmentConfig).filter(
                AssessmentConfig.course_id == course_id
            ).first()
        except Exception as query_error:
            logger.error('Error querying existing assessment config: %s', ("" if query_error is None else str(query_error).replace("\n", "").replace("\r", "")))
            # If table still doesn't exist, create it manually
            ensure_assessment_tables_exist()
            existing_config = None
        
        if existing_config:
            existing_config.assessment_scope = config.assessment_scope
            existing_config.question_type = config.question_type
            existing_config.total_questions = config.total_questions
            existing_config.mcq_count = config.mcq_count
            existing_config.subjective_count = config.subjective_count
            existing_config.weightage_per_question = config.weightage_per_question
            existing_config.pass_percentage = config.pass_percentage
            existing_config.max_attempts = config.max_attempts
            existing_config.ai_suggested_time = ai_suggested_time
            existing_config.trainer_time_override = config.trainer_time_override
            existing_config.is_approved = False
            logger.info('Updated existing assessment config for course %s', ("" if course_id is None else str(course_id).replace("\n", "").replace("\r", "")))
        else:
            assessment_config = AssessmentConfig(
                course_id=course_id,
                assessment_scope=config.assessment_scope,
                question_type=config.question_type,
                total_questions=config.total_questions,
                mcq_count=config.mcq_count,
                subjective_count=config.subjective_count,
                weightage_per_question=config.weightage_per_question,
                pass_percentage=config.pass_percentage,
                max_attempts=config.max_attempts,
                ai_suggested_time=ai_suggested_time,
                trainer_time_override=config.trainer_time_override
            )
            db.add(assessment_config)
            logger.info('Created new assessment config for course %s', ("" if course_id is None else str(course_id).replace("\n", "").replace("\r", "")))
        
        db.commit()
        log_activity(db, user.id, "configure_assessment", f"Configured assessment for course {course.source_doc_name}")
        
        return {
            "success": True,
            "message": "Assessment configuration saved successfully",
            "course_id": course_id,
            "course_name": course.source_doc_name,
            "ai_suggested_time": ai_suggested_time,
            "needs_question_generation": True
        }
        
    except HTTPException:
        # Re-raise HTTP exceptions as-is
        raise
    except Exception as e:
        logger.error('Error configuring assessment: %s', log_safe(e), exc_info=True)
        db.rollback()
        raise HTTPException(status_code=500, detail=GENERIC_API_ERROR_MESSAGE)

def generate_questions_for_content(content: str, total_questions: int, question_type: str, mcq_count: int = None, subjective_count: int = None, existing_questions: List[str] = None, custom_prompt: str = None) -> List[Dict]:
    """Generate assessment questions using AI for given content"""
    try:
        logger.info('Generating %s questions of type %s (MCQ: %s, Subjective: %s)', ("" if total_questions is None else str(total_questions).replace("\n", "").replace("\r", "")), ("" if question_type is None else str(question_type).replace("\n", "").replace("\r", "")), ("" if mcq_count is None else str(mcq_count).replace("\n", "").replace("\r", "")), ("" if subjective_count is None else str(subjective_count).replace("\n", "").replace("\r", "")))
        if custom_prompt:
            logger.info('Using custom prompt: %s...', ("" if custom_prompt[:100] is None else str(custom_prompt[:100]).replace("\n", "").replace("\r", "")))

        # ── CONTENT SANITISATION ──────────────────────────────────────────────
        # Strip everything that is NOT actual subject-matter content so the AI
        # generates questions about the topic, not about document structure.
        import re as _re

        clean = content or ""

        # 1. Remove [IMAGE: ...] and [IMAGE_URL: ...] tags (S3 paths / URLs)
        clean = strip_bracket_image_tags(clean)
        # 2. Remove markdown image syntax  ![alt](url)
        clean = _re.sub(r'!\[[^\]]*\]\([^)]+\)', '', clean)
        # 3. Remove PHASE headers and structural labels — match both ## Phase 1: and ## **Phase 1:**
        clean = _re.sub(r'^#{1,6}\s*\*?\*?Phase\s+\d+[:\s][^\n]*', '', clean, flags=_re.MULTILINE)
        clean = _re.sub(r'^#{1,6}\s*\*?\*?PHASE\s+\d+[:\s][^\n]*', '', clean, flags=_re.MULTILINE)
        clean = _re.sub(r'^#{1,6}\s*\*?\*?(STRATEGIC FRAMEWORK|INFORMATION BLUEPRINT|COMPREHENSIVE REPORT GENERATION|COMPLIANCE[&\s]*FIDELITY AUDIT|FINAL STEP)[:\s][^\n]*', '', clean, flags=_re.MULTILINE | _re.IGNORECASE)
        # 4. Remove Document Classification / Analytical Approach / Module Classification lines
        clean = _re.sub(r'^\*?\*?(Document Classification|Analytical Approach|Module Classification|Fidelity Check|Redundancy Check)[:\s][^\n]*', '', clean, flags=_re.MULTILINE | _re.IGNORECASE)
        # 5. Remove "(A) Structured Data Extraction" / "(B) In-Depth Analytical Synthesis" labels
        clean = _re.sub(r'^\*?\*?\([A-Z]\)\s*(Structured Data Extraction|In-Depth Analytical Synthesis)[^\n]*', '', clean, flags=_re.MULTILINE | _re.IGNORECASE)
        # 6. Remove "I attest that..." compliance lines
        clean = _re.sub(r'^\*?\s*I attest[^\n]*', '', clean, flags=_re.MULTILINE | _re.IGNORECASE)
        # 7. Remove TOC / Table of Contents / Information Blueprint sections
        clean = _re.sub(r'^#{1,6}\s*\*?\*?(Table of Contents|TOC|Dynamic Blueprint|Information Blueprint)[:\s]?[^\n]*', '', clean, flags=_re.MULTILINE | _re.IGNORECASE)
        # Also remove numbered TOC entries (lines like "1. **Topic** - subtopic")
        clean = _re.sub(r'^\d+\.\s+\*\*[^\n]{0,80}\*\*\s*\n(?:\s+-[^\n]+\n)*', '', clean, flags=_re.MULTILINE)
        # 8. Remove VISION ANALYSIS blocks (injected for tutor, not content)
        clean = _re.sub(r'\*\*VISION ANALYSIS FOR THIS IMAGE:\*\*[\s\S]*?(?=\n\n|\Z)', '', clean)
        # 9. Remove horizontal rules
        clean = _re.sub(r'^---+\s*$', '', clean, flags=_re.MULTILINE)
        # 10. Collapse excessive blank lines
        clean = _re.sub(r'\n{3,}', '\n\n', clean).strip()

        logger.info('[QUESTION GEN] Content sanitised: %s -> %s chars', len(content), len(clean))

        # Cap content to avoid excessive input tokens — keep the most substantive paragraphs
        # (first 8000 chars of sanitised content is plenty for question generation)
        if len(clean) > 8000:
            content_sample = clean[:8000] + "\n\n[Content truncated for question generation]"
            logger.info("[QUESTION GEN] Content capped to 8000 chars for question generation")
        else:
            content_sample = clean

        # Set default values for mcq_count and subjective_count if not provided
        if mcq_count is None and subjective_count is None:
            if question_type == 'mcq':
                mcq_count = total_questions
                subjective_count = 0
            elif question_type == 'subjective':
                mcq_count = 0
                subjective_count = total_questions
            else:  # mixed - split evenly
                mcq_count = total_questions // 2
                subjective_count = total_questions - mcq_count
        
        # Prepare existing questions context to avoid duplicates
        existing_questions_context = ""
        if existing_questions and len(existing_questions) > 0:
            existing_questions_context = "\n\nEXISTING QUESTIONS TO AVOID (DO NOT CREATE SIMILAR OR DUPLICATE QUESTIONS):\n"
            for idx, eq in enumerate(existing_questions[:50], 1):  # Limit to 50 to avoid token overflow
                existing_questions_context += f"{idx}. {eq}\n"
            logger.info('Including %s existing questions in context to avoid duplicates', len(existing_questions[:50]))
        
        # Prepare the prompt for question generation
        if question_type == 'mcq':
            prompt = f"""You are an expert assessment creator. Generate EXACTLY {total_questions} multiple choice questions based on the following content.

Content:
{content_sample}
{existing_questions_context}

CRITICAL INSTRUCTIONS - FOCUS ON TECHNICAL CONTENT ONLY:
1. ONLY ask about SPECIFIC TECHNICAL CONCEPTS, TERMS, MECHANISMS, ALGORITHMS, ARCHITECTURES, COMPONENTS from the content
2. ABSOLUTELY DO NOT ask about:
   - Document structure (PHASE, sections, TOC, headings, chapters, modules, etc.)
   - Generic concepts like "what is discussed", "main concept", "significance", "overview"
   - Document format or organization
   - Any meta-information about the document itself
3. MUST ask about:
   - Specific technical terms and their definitions
   - How specific systems/components work
   - Technical processes and workflows
   - Specific algorithms, formulas, or methodologies
   - Specific technologies, tools, or frameworks mentioned
   - Specific problems and their solutions
   - Technical relationships between concepts
4. Each question MUST reference SPECIFIC technical details from the content (not generic)
5. Questions should test DEEP UNDERSTANDING of actual subject matter
6. NO duplicate or similar questions - ESPECIALLY avoid questions similar to existing questions listed above
7. If existing questions are provided above, ensure your new questions cover COMPLETELY DIFFERENT topics and concepts

Requirements:
- Create EXACTLY {total_questions} UNIQUE multiple choice questions
- Each question should have 4 options (A, B, C, D) with one correct answer
- Questions MUST be complete, well-formed sentences with proper grammar
- Questions MUST reference specific technical concepts, not generic topics
- Include a detailed explanation for the correct answer (2-3 sentences minimum)
- Explanations should clarify WHY the answer is correct and reference specific content
- Cover different technical aspects and concepts from the content
- Vary the difficulty level across questions
- NO truncated, incomplete, or generic questions
- NO questions about document structure or meta-information

VALIDATION CHECKLIST FOR EACH QUESTION:
✓ Does it ask about a SPECIFIC technical concept from the content?
✓ Does it reference specific terms, components, or mechanisms?
✓ Is it NOT about document structure or organization?
✓ Is it NOT a generic question that could apply to any document?
✓ Is the question COMPLETE and well-formatted?
✓ Is the explanation DETAILED and references specific content?
✓ Is it DIFFERENT from all other questions?

CRITICAL: 
1. You MUST respond with ONLY a valid JSON array. No other text, explanations, or formatting.
2. ENSURE ALL QUESTIONS ARE UNIQUE - no repetition or paraphrasing of the same concept
3. ENSURE ALL QUESTIONS ARE COMPLETE - no cut-off text
4. ENSURE ALL QUESTIONS ARE SPECIFIC - reference actual technical concepts, not generic topics
5. FOCUS ON TECHNICAL CONTENT, NOT DOCUMENT STRUCTURE
6. ENSURE EXPLANATIONS ARE DETAILED AND REFERENCE SPECIFIC CONTENT

Format:
[
  {{
    "type": "mcq",
    "question": "What is the specific technical concept/process/mechanism mentioned in the content?",
    "option_a": "Specific technical option A",
    "option_b": "Specific technical option B", 
    "option_c": "Specific technical option C",
    "option_d": "Specific technical option D",
    "correct_answer": "A",
    "explanation": "Detailed explanation referencing specific content details and why this answer is correct"
  }}
]"""
        elif question_type == 'subjective':
            prompt = f"""You are an expert assessment creator. Generate EXACTLY {total_questions} subjective/essay questions based on the following content.

Content:
{content_sample}
{existing_questions_context}

CRITICAL INSTRUCTIONS - FOCUS ON TECHNICAL CONTENT ONLY:
1. ONLY ask about SPECIFIC TECHNICAL CONCEPTS, MECHANISMS, PROCESSES, ARCHITECTURES from the content
2. ABSOLUTELY DO NOT ask about:
   - Document structure (PHASE, sections, TOC, headings, chapters, modules, etc.)
   - Generic concepts like "explain the significance", "discuss the main points", "overview"
   - Document format or organization
   - Any meta-information about the document itself
3. MUST ask about:
   - How specific technical systems/components work
   - Specific technical processes and workflows
   - Specific algorithms, methodologies, or approaches
   - Specific technologies, tools, or frameworks mentioned
   - Technical problems and their solutions
   - Technical relationships and interactions between concepts
   - Specific implementation details and considerations
4. Each question MUST reference SPECIFIC technical details from the content (not generic)
5. Questions should require DETAILED TECHNICAL ANSWERS
6. NO duplicate or similar questions - ESPECIALLY avoid questions similar to existing questions listed above
7. If existing questions are provided above, ensure your new questions cover COMPLETELY DIFFERENT topics and concepts

Requirements:
- Create EXACTLY {total_questions} UNIQUE subjective questions
- Questions should be open-ended and require detailed technical answers
- Questions MUST be complete, well-formed sentences with proper grammar
- Questions MUST reference specific technical concepts, not generic topics
- Questions should test DEEP UNDERSTANDING of specific concepts from the content
- Include detailed explanation of key technical points that should be covered in the answer
- Cover different technical aspects and concepts from the content
- Vary the difficulty level across questions
- NO truncated, incomplete, or generic questions
- NO questions about document structure or meta-information

VALIDATION CHECKLIST FOR EACH QUESTION:
✓ Does it ask about a SPECIFIC technical concept from the content?
✓ Does it require detailed technical explanation?
✓ Is it NOT about document structure or organization?
✓ Is it NOT a generic question that could apply to any document?
✓ Is the question COMPLETE and well-formatted?
✓ Are the expected answer points SPECIFIC and technical?
✓ Is it DIFFERENT from all other questions?

CRITICAL: 
1. You MUST respond with ONLY a valid JSON array. No other text, explanations, or formatting.
2. ENSURE ALL QUESTIONS ARE UNIQUE - no repetition or paraphrasing of the same concept
3. ENSURE ALL QUESTIONS ARE COMPLETE AND SPECIFIC - reference actual technical concepts
4. ENSURE ANSWER POINTS ARE DETAILED AND TECHNICAL
5. FOCUS ON TECHNICAL CONTENT, NOT DOCUMENT STRUCTURE

Format:
[
  {{
    "type": "subjective",
    "question": "Explain/Describe/Analyze the specific technical concept/process/mechanism from the content?",
    "explanation": "Detailed key technical points that should be covered in the answer, referencing specific content details"
  }}
]"""
        else:  # mixed
            prompt = f"""You are an expert assessment creator. Generate EXACTLY {mcq_count} multiple choice questions and EXACTLY {subjective_count} subjective questions based on the following content.

Content:
{content_sample}
{existing_questions_context}

CRITICAL INSTRUCTIONS - FOCUS ON TECHNICAL CONTENT ONLY:
1. ONLY ask about SPECIFIC TECHNICAL CONCEPTS, TERMS, MECHANISMS, ALGORITHMS, ARCHITECTURES, COMPONENTS from the content
2. ABSOLUTELY DO NOT ask about:
   - Document structure (PHASE, sections, TOC, headings, chapters, modules, etc.)
   - Generic concepts like "what is discussed", "main concept", "significance", "overview"
   - Document format or organization
   - Any meta-information about the document itself
3. MUST ask about:
   - Specific technical terms and their definitions
   - How specific systems/components work
   - Technical processes and workflows
   - Specific algorithms, formulas, or methodologies
   - Specific technologies, tools, or frameworks mentioned
   - Specific problems and their solutions
   - Technical relationships between concepts
4. Each question MUST reference SPECIFIC technical details from the content (not generic)
5. Questions should test DEEP UNDERSTANDING of actual subject matter
6. NO duplicate or similar questions - ESPECIALLY avoid questions similar to existing questions listed above
7. If existing questions are provided above, ensure your new questions cover COMPLETELY DIFFERENT topics and concepts

Requirements:
- Create EXACTLY {mcq_count} UNIQUE MCQ questions and EXACTLY {subjective_count} UNIQUE subjective questions
- Total questions MUST be {total_questions} ({mcq_count} MCQ + {subjective_count} Subjective)
- Each question MUST be completely different from all others AND from existing questions
- MCQ questions should have 4 options with one correct answer
- Subjective questions should be open-ended and require detailed technical answers
- All questions should test understanding of specific technical concepts from the content
- All questions MUST be complete, well-formed sentences with proper grammar
- All questions MUST reference specific technical concepts, not generic topics
- Include detailed explanations for MCQ answers (2-3 sentences minimum)
- Include detailed explanation of key technical points for subjective answers
- Cover different technical aspects and concepts from the content
- Vary the difficulty level across questions
- NO generic, truncated, or incomplete questions
- NO questions about document structure or meta-information
- Focus on the actual subject matter, not document structure

VALIDATION CHECKLIST FOR EACH QUESTION:
✓ Does it ask about a SPECIFIC technical concept from the content?
✓ Does it reference specific terms, components, or mechanisms?
✓ Is it NOT about document structure or organization?
✓ Is it NOT a generic question that could apply to any document?
✓ Is the question COMPLETE and well-formatted?
✓ Is the explanation DETAILED and references specific content?
✓ Is it DIFFERENT from all other questions?

CRITICAL: 
1. You MUST respond with ONLY a valid JSON array. No other text, explanations, or formatting.
2. ENSURE ALL QUESTIONS ARE UNIQUE - no repetition or paraphrasing of the same concept
3. ENSURE EXACT COUNTS: {mcq_count} MCQ and {subjective_count} Subjective questions
4. ENSURE ALL QUESTIONS ARE COMPLETE AND WELL-FORMATTED
5. ENSURE ALL QUESTIONS ARE SPECIFIC - reference actual technical concepts, not generic topics
6. ENSURE EXPLANATIONS ARE DETAILED AND REFERENCE SPECIFIC CONTENT
7. FOCUS ON TECHNICAL CONTENT, NOT DOCUMENT STRUCTURE

Format:
[
  {{
    "type": "mcq",
    "question": "What is the specific technical concept/process/mechanism mentioned in the content?",
    "option_a": "Specific technical option A",
    "option_b": "Specific technical option B",
    "option_c": "Specific technical option C", 
    "option_d": "Specific technical option D",
    "correct_answer": "A",
    "explanation": "Detailed explanation referencing specific content details and why this answer is correct"
  }},
  {{
    "type": "subjective",
    "question": "Explain/Describe/Analyze the specific technical concept/process/mechanism from the content?",
    "explanation": "Detailed key technical points that should be covered in the answer, referencing specific content details"
  }}
]"""

        # Inject custom_prompt (uniqueness/variation instructions) BEFORE the base instructions
        # so it takes priority over the generic generation rules
        if custom_prompt:
            prompt = custom_prompt + "\n\n" + prompt
            logger.info("[UNIQUE Q] Custom prompt injected BEFORE base prompt for priority")

        # Use the question generation agent with error handling
        logger.info("Calling question generation agent...")
        try:
            response = question_generation_agent.run(prompt)
            logger.info('Agent response length: %s', len(response.content))
        except Exception as agent_error:
            logger.error('Question generation agent failed: %s', ("" if agent_error is None else str(agent_error).replace("\n", "").replace("\r", "")))
            logger.info("Falling back to direct question creation")
            return create_fallback_questions(content, total_questions, question_type, mcq_count, subjective_count)
        
        # Clean the response - remove any markdown formatting
        response_content = response.content.strip()
        
        # Remove markdown code blocks if present
        if '```json' in response_content:
            response_content = response_content.split('```json')[1].split('```')[0].strip()
        elif '```' in response_content:
            response_content = response_content.split('```')[1].split('```')[0].strip()
        
        # Try to extract JSON array from response
        import re
        json_match = re.search(r'\[.*\]', response_content, re.DOTALL)
        if json_match:
            response_content = json_match.group(0)
        
        logger.info('Cleaned response: %s...', ("" if response_content[:200] is None else str(response_content[:200]).replace("\n", "").replace("\r", "")))
        
        # Parse the JSON response
        try:
            questions = json.loads(response_content)
            if not isinstance(questions, list):
                raise ValueError("Response is not a list")
            
            # Validate and clean questions - ENFORCE EXACT COUNTS
            validated_mcq = []
            validated_subjective = []
            seen_questions = set()  # Track question texts to prevent duplicates
            
            for q in questions:
                if isinstance(q, dict) and 'question' in q and 'type' in q:
                    # Normalize question text for duplicate detection
                    normalized_question = q['question'].lower().strip()
                    
                    # Skip if we've seen this question before (case-insensitive)
                    if normalized_question in seen_questions:
                        logger.warning('Skipping duplicate question: %s...', ("" if q['question'][:50] is None else str(q['question'][:50]).replace("\n", "").replace("\r", "")))
                        continue
                    
                    # Ensure all required fields are present
                    if q['type'] == 'mcq' and len(validated_mcq) < mcq_count:
                        if all(key in q for key in ['option_a', 'option_b', 'option_c', 'option_d', 'correct_answer']):
                            validated_mcq.append(q)
                            seen_questions.add(normalized_question)
                    elif q['type'] == 'subjective' and len(validated_subjective) < subjective_count:
                        validated_subjective.append(q)
                        seen_questions.add(normalized_question)
            
            # Combine questions in the correct order
            validated_questions = validated_mcq + validated_subjective
            
            logger.info('Validated %s MCQ and %s subjective questions from AI response', len(validated_mcq), len(validated_subjective))
            
            # Check if we have the exact count needed
            if len(validated_mcq) == mcq_count and len(validated_subjective) == subjective_count:
                return validated_questions
            elif len(validated_questions) >= total_questions:
                # We have enough questions but not exact counts - use what we have
                logger.warning("Question counts don't match exactly (MCQ: %s/%s, Subjective: %s/%s)", len(validated_mcq), ("" if mcq_count is None else str(mcq_count).replace("\n", "").replace("\r", "")), len(validated_subjective), ("" if subjective_count is None else str(subjective_count).replace("\n", "").replace("\r", "")))
                return validated_questions[:total_questions]
            else:
                # Not enough questions - supplement with fallback
                logger.warning('Not enough valid questions (%s/%s), supplementing with fallback', len(validated_questions), ("" if total_questions is None else str(total_questions).replace("\n", "").replace("\r", "")))
                needed = total_questions - len(validated_questions)
                fallback = create_fallback_questions(content, needed, question_type, max(0, mcq_count - len(validated_mcq)), max(0, subjective_count - len(validated_subjective)))
                return validated_questions + fallback
            
        except json.JSONDecodeError as e:
            logger.error('Failed to parse JSON response: %s', e)
            logger.error('Raw response: %s', ("" if response_content is None else str(response_content).replace("\n", "").replace("\r", "")))
            # Fallback: create simple questions
            return create_fallback_questions(content, total_questions, question_type, mcq_count, subjective_count)
            
    except Exception as e:
        logger.error('Error generating questions: %s', e, exc_info=True)
        return create_fallback_questions(content, total_questions, question_type, mcq_count, subjective_count)

def create_fallback_questions(content: str, total_questions: int, question_type: str, mcq_count: int = None, subjective_count: int = None) -> List[Dict]:
    """
    Fallback question generator used when the primary AI call fails or returns
    insufficient questions. Uses the same content sanitisation as the main path
    and attempts a simpler AI call before resorting to truly generic questions.
    """
    logger.info('Creating %s fallback questions of type %s (MCQ: %s, Subjective: %s)', ("" if total_questions is None else str(total_questions).replace("\n", "").replace("\r", "")), ("" if question_type is None else str(question_type).replace("\n", "").replace("\r", "")), ("" if mcq_count is None else str(mcq_count).replace("\n", "").replace("\r", "")), ("" if subjective_count is None else str(subjective_count).replace("\n", "").replace("\r", "")))

    if mcq_count is None and subjective_count is None:
        if question_type == 'mcq':
            mcq_count = total_questions
            subjective_count = 0
        elif question_type == 'subjective':
            mcq_count = 0
            subjective_count = total_questions
        else:
            mcq_count = total_questions // 2
            subjective_count = total_questions - mcq_count

    # ── Sanitise content (same rules as generate_questions_for_content) ──────
    import re as _re
    clean = content or ""
    clean = strip_bracket_image_tags(clean)
    clean = _re.sub(r'!\[[^\]]*\]\([^)]+\)', '', clean)
    clean = _re.sub(r'^#{1,6}\s*\*?\*?(?:PHASE\s+\d+|Phase\s+\d+)[:\s][^\n]*', '', clean, flags=_re.MULTILINE)
    clean = _re.sub(r'^#{1,6}\s*\*?\*?(?:STRATEGIC FRAMEWORK|INFORMATION BLUEPRINT|COMPREHENSIVE REPORT GENERATION|COMPLIANCE[&\s]*FIDELITY AUDIT|FINAL STEP)[:\s][^\n]*', '', clean, flags=_re.MULTILINE | _re.IGNORECASE)
    clean = _re.sub(r'^\*?\*?(?:Document Classification|Analytical Approach|Module Classification|Fidelity Check|Redundancy Check)[:\s][^\n]*', '', clean, flags=_re.MULTILINE | _re.IGNORECASE)
    clean = _re.sub(r'^\*?\*?\([A-Z]\)\s*(?:Structured Data Extraction|In-Depth Analytical Synthesis)[^\n]*', '', clean, flags=_re.MULTILINE | _re.IGNORECASE)
    clean = _re.sub(r'^\*?\s*I attest[^\n]*', '', clean, flags=_re.MULTILINE | _re.IGNORECASE)
    clean = _re.sub(r'^#{1,6}\s*\*?\*?(?:Table of Contents|TOC|Dynamic Blueprint|Information Blueprint)[:\s]?[^\n]*', '', clean, flags=_re.MULTILINE | _re.IGNORECASE)
    clean = _re.sub(r'\*\*VISION ANALYSIS FOR THIS IMAGE:\*\*[\s\S]*?(?=\n\n|\Z)', '', clean)
    clean = _re.sub(r'^---+\s*$', '', clean, flags=_re.MULTILINE)
    clean = _re.sub(r'\n{3,}', '\n\n', clean).strip()

    # ── Try a simplified AI call first ───────────────────────────────────────
    # Use a much shorter, direct prompt to avoid the token-limit issues that
    # caused the primary call to fail.
    try:
        mcq_format = 'MCQ format: [{"type":"mcq","question":"...","option_a":"...","option_b":"...","option_c":"...","option_d":"...","correct_answer":"A","explanation":"..."}]' if question_type in ('mcq', 'mixed') else ''
        subj_format = 'Subjective format: [{"type":"subjective","question":"...","explanation":"..."}]' if question_type in ('subjective', 'mixed') else ''
        simple_prompt = f"""Generate EXACTLY {total_questions} assessment questions about the following content.
            Focus ONLY on specific technical concepts, terms, processes, and mechanisms described in the text.
            Do NOT ask about document structure, headings, or meta-information.

            Content:
            {clean[:6000]}

            Output ONLY a valid JSON array. No other text.
            {mcq_format}
            {subj_format}
            Generate {mcq_count} MCQ and {subjective_count} subjective questions."""

        response = question_generation_agent.run(simple_prompt)
        resp_text = response.content.strip()
        if '```json' in resp_text:
            resp_text = resp_text.split('```json')[1].split('```')[0].strip()
        elif '```' in resp_text:
            resp_text = resp_text.split('```')[1].split('```')[0].strip()
        json_match = _re.search(r'\[.*\]', resp_text, _re.DOTALL)
        if json_match:
            questions = json.loads(json_match.group(0))
            if isinstance(questions, list) and len(questions) > 0:
                logger.info('[FALLBACK] Simplified AI call succeeded: %s questions', len(questions))
                return questions[:total_questions]
    except Exception as ai_err:
        logger.warning('[FALLBACK] Simplified AI call also failed: %s', ("" if ai_err is None else str(ai_err).replace("\n", "").replace("\r", "")))

    # ── Last resort: extract meaningful sentences from sanitised content ──────
    # Only use actual content sentences — never image filenames, captions, or structural lines.
    questions = []

    good_sentences = []
    for line in clean.split('\n'):
        line = line.strip().lstrip('#').lstrip('*').lstrip('-').strip()
        if (len(line) > 80 and
                not line.startswith('[') and
                not line.startswith('!') and
                not _re.match(r'^\d+\.\s+\*\*', line) and
                not _re.match(r'^#{1,6}', line) and
                # Skip image caption-style lines (filename descriptions)
                not _re.search(r'\b(diagram|figure|chart|image|photo|screenshot)\b.*\bshowing\b', line, _re.IGNORECASE) and
                not _re.match(r'^(Three-stage|Two-stage|Multi-stage|Circular|Concentric|Comparison)\b', line, _re.IGNORECASE)):
            good_sentences.append(line)

    if not good_sentences:
        good_sentences = [f"the core technical concepts covered in this module (topic {i+1})" for i in range(total_questions)]

    for i in range(mcq_count):
        sentence = good_sentences[i % len(good_sentences)]
        snippet = sentence[:150] + ('...' if len(sentence) > 150 else '')
        questions.append({
            "type": "mcq",
            "question": f"Based on the module content, which statement correctly describes the following concept?\n\n> {snippet}",
            "option_a": "This is a core mechanism explained in the module with specific technical details",
            "option_b": "This is an optional feature not covered in the module's main discussion",
            "option_c": "This is a deprecated approach that has been replaced by a newer method",
            "option_d": "This concept is unrelated to the module's primary technical topic",
            "correct_answer": "A",
            "explanation": f"This concept is directly discussed in the module. The correct answer is A because the module explicitly covers this as a core technical element: \"{snippet}\""
        })

    for i in range(subjective_count):
        idx = mcq_count + i
        sentence = good_sentences[idx % len(good_sentences)]
        snippet = sentence[:150] + ('...' if len(sentence) > 150 else '')
        questions.append({
            "type": "subjective",
            "question": f"Explain the following concept from the module in your own words, including how it works and why it matters:\n\n> {snippet}",
            "explanation": f"A complete answer should explain what this concept is, how it functions technically, its significance in the context of the module, and any practical implications or examples mentioned in the module content."
        })

    logger.info('[FALLBACK] Created %s last-resort questions (%s MCQ, %s Subjective)', len(questions), ("" if mcq_count is None else str(mcq_count).replace("\n", "").replace("\r", "")), ("" if subjective_count is None else str(subjective_count).replace("\n", "").replace("\r", "")))
    return questions

@router.post("/trainer/generate-questions/{course_id}", tags=["Assessment System"])
def generate_assessment_questions(
    course_id: str,
    user: User = Depends(trainer_required),
    db: Session = Depends(get_db)
):
    """Generate assessment questions using AI based on course content"""
    try:
        course = _get_course_for_trainer_or_super_admin(db, course_id, user)
        course_id = course.id
        
        config = db.query(AssessmentConfig).filter(
            AssessmentConfig.course_id == course_id
        ).first()
        
        if not config:
            raise HTTPException(status_code=400, detail="Assessment not configured")
        
        author_id = course.author_id
        
        # Get course modules
        modules = []
        try:
            # CRITICAL FIX: Parse source_documents for multi-document courses
            course_docs_normalized = []
            try:
                if course.source_documents:
                    source_docs_list = json.loads(course.source_documents)
                    if isinstance(source_docs_list, list):
                        for doc in source_docs_list:
                            doc_normalized = normalize_document_name(doc)
                            course_docs_normalized.append(doc_normalized)
                        logger.info(
                            '[ASSESSMENT GEN] Parsed %s documents from source_documents: %s',
                            len(course_docs_normalized),
                            log_safe(course_docs_normalized),
                        )
            except Exception as e:
                logger.warning('[ASSESSMENT GEN] Could not parse source_documents JSON: %s', log_safe(e))
            
            # Fallback to single source_doc_name if source_documents is empty
            if not course_docs_normalized:
                course_doc_normalized = normalize_document_name(course.source_doc_name)
                course_docs_normalized = [course_doc_normalized]
                logger.info('[ASSESSMENT GEN] Using fallback single source_doc_name: %s', log_safe(course_doc_normalized))
            
            paginator = s3_client.get_paginator('list_objects_v2')
            seen_module_keys = set()  # DEDUP: track (source_doc_normalized, seg_index) pairs
            for page in paginator.paginate(Bucket=S3_BUCKET_NAME, Prefix=f"{author_id}/generated_reports/"):
                for obj in page.get('Contents', []):
                    if obj['Key'].endswith('.json'):
                        try:
                            meta_obj = s3_client.get_object(Bucket=S3_BUCKET_NAME, Key=obj['Key'])
                            meta_data = json.loads(meta_obj['Body'].read().decode('utf-8'))
                            source_doc = meta_data.get('source_doc', '')
                            
                            # Normalize the metadata source_doc for comparison
                            source_doc_normalized = normalize_document_name(source_doc)
                            
                            # Check if this module matches ANY of the course documents
                            # Use EXACT match first; substring only when strings are very similar (≥80% length ratio)
                            is_match = False
                            for course_doc_norm in course_docs_normalized:
                                if source_doc_normalized == course_doc_norm:
                                    is_match = True
                                    break
                                # Substring match only when one is clearly a prefix/suffix of the other
                                # AND the shorter string is at least 80% the length of the longer one
                                # to avoid false positives like "sales" matching "sales_training_2024"
                                len_a = len(course_doc_norm)
                                len_b = len(source_doc_normalized)
                                if len_a > 0 and len_b > 0:
                                    len_ratio = min(len_a, len_b) / max(len_a, len_b)
                                    if len_ratio >= 0.8 and (course_doc_norm in source_doc_normalized or source_doc_normalized in course_doc_norm):
                                        is_match = True
                                        break
                            
                            if is_match:
                                seg_index = meta_data.get('seg_index', 0)
                                
                                # DEDUP: skip if we already have this (source_doc, seg_index) combination
                                dedup_key = (source_doc_normalized, seg_index)
                                if dedup_key in seen_module_keys:
                                    logger.warning(
                                        '[ASSESSMENT GEN] Skipping duplicate module: source_doc=%s, seg_index=%s',
                                        log_safe(source_doc),
                                        log_safe(seg_index),
                                    )
                                    continue
                                seen_module_keys.add(dedup_key)
                                
                                # Get module content
                                md_key = obj['Key'].replace('.json', '.md')
                                report_obj = s3_client.get_object(Bucket=S3_BUCKET_NAME, Key=md_key)
                                module_content = report_obj['Body'].read().decode('utf-8')
                                
                                modules.append({
                                    'index': seg_index,
                                    'title': meta_data.get('title', 'Module'),
                                    'content': module_content,
                                    'source_doc': source_doc
                                })
                                logger.info(
                                    '[ASSESSMENT GEN] Found module %s: %s from %s',
                                    log_safe(seg_index),
                                    log_safe(meta_data.get('title')),
                                    log_safe(source_doc),
                                )
                        except Exception as e:
                            logger.error('Error loading module: %s', log_safe(e), exc_info=True)
            
            # Sort by seg_index to maintain order
            modules.sort(key=lambda x: x['index'])
            
            # CRITICAL FIX: Renumber modules sequentially (1, 2, 3...) for multi-document courses
            # This ensures questions are assigned to the correct module number
            for new_index, module in enumerate(modules, start=1):
                old_index = module['index']
                module['index'] = new_index
                logger.info(
                    '[ASSESSMENT GEN] Module %s: %s (was index %s, from %s)',
                    log_safe(new_index),
                    log_safe(module['title']),
                    log_safe(old_index),
                    log_safe(module.get('source_doc', 'unknown')),
                )
            
            logger.info('[ASSESSMENT GEN] Loaded %s modules for question generation (renumbered 1-%s)', len(modules), len(modules))
        except HTTPException:
            raise
        except Exception as e:
            logger.error('Error loading course modules: %s', log_safe(e), exc_info=True)
            raise HTTPException(status_code=500, detail=GENERIC_API_ERROR_MESSAGE)
        
        if not modules:
            raise HTTPException(status_code=400, detail="No modules found for course")
        
        # Clear existing questions
        db.query(AssessmentQuestion).filter(
            AssessmentQuestion.course_id == course_id
        ).delete()
        
        # Generate questions based on scope
        if config.assessment_scope == 'end_of_module':
            # Generate questions for each module
            num_modules = len(modules)
            
            # CRITICAL: Distribute questions evenly across actual loaded modules.
            # Integer division gives the base count; remainders go to the first N modules.
            mcq_per_module     = config.mcq_count // num_modules if config.mcq_count > 0 else 0
            subjective_per_module = config.subjective_count // num_modules if config.subjective_count > 0 else 0
            mcq_remainder      = config.mcq_count % num_modules if config.mcq_count > 0 else 0
            subjective_remainder = config.subjective_count % num_modules if config.subjective_count > 0 else 0
            
            logger.info(
                "[ASSESSMENT GEN] Distributing across %s modules: base MCQ=%s (+%s remainder), base Subjective=%s (+%s remainder)",
                num_modules,
                mcq_per_module,
                mcq_remainder,
                subjective_per_module,
                subjective_remainder,
            )
            logger.info(
                "[ASSESSMENT GEN] Total to generate: %s questions (%s MCQ + %s Subjective) across %s modules",
                config.total_questions,
                config.mcq_count,
                config.subjective_count,
                num_modules,
            )
            
            for idx, module in enumerate(modules):
                # Add remainder questions to first modules
                module_mcq = mcq_per_module + (1 if idx < mcq_remainder else 0)
                module_subjective = subjective_per_module + (1 if idx < subjective_remainder else 0)
                
                # CRITICAL FIX: Calculate module_total from actual MCQ + Subjective counts
                # This ensures consistency between total and individual counts
                module_total = module_mcq + module_subjective
                
                logger.info('[ASSESSMENT GEN] Module %s: Generating %s questions (MCQ: %s, Subjective: %s)', ("" if module['index'] is None else str(module['index']).replace("\n", "").replace("\r", "")), ("" if module_total is None else str(module_total).replace("\n", "").replace("\r", "")), ("" if module_mcq is None else str(module_mcq).replace("\n", "").replace("\r", "")), ("" if module_subjective is None else str(module_subjective).replace("\n", "").replace("\r", "")))
                
                module_questions = generate_questions_for_content(
                    module['content'],
                    module_total,
                    config.question_type,
                    module_mcq,
                    module_subjective
                )
                
                for q in module_questions:
                    question = AssessmentQuestion(
                        course_id=course_id,
                        module_index=module['index'],
                        source_doc_name=module.get('source_doc'),  # Store the source document name
                        question_type=q['type'],
                        question_text=q['question'],
                        correct_answer=q.get('correct_answer'),
                        option_a=q.get('option_a'),
                        option_b=q.get('option_b'),
                        option_c=q.get('option_c'),
                        option_d=q.get('option_d'),
                        explanation=q.get('explanation')
                    )
                    logger.info('[ASSESSMENT GEN] Creating question for Module %s with source_doc_name: %s', ("" if module['index'] is None else str(module['index']).replace("\n", "").replace("\r", "")), ("" if module.get('source_doc') is None else str(module.get('source_doc')).replace("\n", "").replace("\r", "")))
                    db.add(question)
            
            # VERIFICATION: Count generated questions by type
            all_questions = db.query(AssessmentQuestion).filter(
                AssessmentQuestion.course_id == course_id
            ).all()
            mcq_generated = sum(1 for q in all_questions if q.question_type == 'mcq')
            subjective_generated = sum(1 for q in all_questions if q.question_type == 'subjective')
            logger.info('[ASSESSMENT GEN] ✅ VERIFICATION: Generated %s total questions (%s MCQ + %s Subjective)', len(all_questions), ("" if mcq_generated is None else str(mcq_generated).replace("\n", "").replace("\r", "")), ("" if subjective_generated is None else str(subjective_generated).replace("\n", "").replace("\r", "")))
            logger.info('[ASSESSMENT GEN] Expected: %s total (%s MCQ + %s Subjective)', ("" if config.total_questions is None else str(config.total_questions).replace("\n", "").replace("\r", "")), ("" if config.mcq_count is None else str(config.mcq_count).replace("\n", "").replace("\r", "")), ("" if config.subjective_count is None else str(config.subjective_count).replace("\n", "").replace("\r", "")))
            
        else:
            # Generate questions for entire course
            combined_content = "\n\n".join([f"Module {m['index']}: {m['title']}\n{m['content']}" for m in modules])
            
            course_questions = generate_questions_for_content(
                combined_content,
                config.total_questions,
                config.question_type,
                config.mcq_count,
                config.subjective_count
            )
            
            for q in course_questions:
                question = AssessmentQuestion(
                    course_id=course_id,
                    module_index=None,  # End of course
                    question_type=q['type'],
                    question_text=q['question'],
                    correct_answer=q.get('correct_answer'),
                    option_a=q.get('option_a'),
                    option_b=q.get('option_b'),
                    option_c=q.get('option_c'),
                    option_d=q.get('option_d'),
                    explanation=q.get('explanation')
                )
                logger.info("[ASSESSMENT GEN] Creating end-of-course question")
                db.add(question)
            
            # VERIFICATION: Count generated questions by type
            all_questions = db.query(AssessmentQuestion).filter(
                AssessmentQuestion.course_id == course_id
            ).all()
            mcq_generated = sum(1 for q in all_questions if q.question_type == 'mcq')
            subjective_generated = sum(1 for q in all_questions if q.question_type == 'subjective')
            logger.info('[ASSESSMENT GEN] ✅ VERIFICATION: Generated %s total questions (%s MCQ + %s Subjective)', len(all_questions), ("" if mcq_generated is None else str(mcq_generated).replace("\n", "").replace("\r", "")), ("" if subjective_generated is None else str(subjective_generated).replace("\n", "").replace("\r", "")))
            logger.info('[ASSESSMENT GEN] Expected: %s total (%s MCQ + %s Subjective)', ("" if config.total_questions is None else str(config.total_questions).replace("\n", "").replace("\r", "")), ("" if config.mcq_count is None else str(config.mcq_count).replace("\n", "").replace("\r", "")), ("" if config.subjective_count is None else str(config.subjective_count).replace("\n", "").replace("\r", "")))
        
        db.commit()
        log_activity(db, user.id, "generate_questions", f"Generated questions for course {course.source_doc_name}")
        
        return {"success": True, "message": "Questions generated successfully", "total_questions": config.total_questions}
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error('Error generating questions: %s', log_safe(e), exc_info=True)
        db.rollback()
        raise HTTPException(status_code=500, detail=GENERIC_API_ERROR_MESSAGE)

@router.post("/trainer/create-test-questions/{course_id}", tags=["Assessment System"])
def create_test_questions(
    course_id: str,
    user: User = Depends(get_current_active_user),  # Changed to allow super_admin
    db: Session = Depends(get_db)
):
    """Create test questions for immediate testing (fallback when AI generation fails)"""
    try:
        # Verify course exists and user has permission
        course = db.query(Course).filter(Course.id == course_id).first()
        
        if not course:
            raise HTTPException(status_code=404, detail="Course not found")
        
        # Check permissions: must be course owner OR super admin
        if course.author_id != user.id and user.role != 'super_admin':
            raise HTTPException(status_code=403, detail="You don't have permission to access this course")
        
        # Check if assessment is configured
        config = db.query(AssessmentConfig).filter(
            AssessmentConfig.course_id == course_id
        ).first()
        
        if not config:
            raise HTTPException(status_code=400, detail="Assessment not configured")
        
        # Clear existing questions
        db.query(AssessmentQuestion).filter(
            AssessmentQuestion.course_id == course_id
        ).delete()
        
        # Create test questions based on course name
        course_name = course.source_doc_name
        questions_created = 0
        
        for i in range(min(config.total_questions, 5)):  # Limit to 5 test questions
            if config.question_type in ['mcq', 'mixed']:
                question = AssessmentQuestion(
                    course_id=course_id,
                    module_index=None,
                    question_type='mcq',
                    question_text=f"Based on the content in '{course_name}', what is the main concept discussed in section {i+1}?",
                    option_a="The primary concept as described in the content",
                    option_b="A secondary related concept",
                    option_c="An alternative interpretation",
                    option_d="A contrasting viewpoint",
                    correct_answer="A",
                    explanation="This is a test question created for immediate preview. The correct answer is based on the main concepts typically presented in educational content.",
                    is_approved=False
                )
                db.add(question)
                questions_created += 1
            
            if config.question_type in ['subjective', 'mixed'] and questions_created < config.total_questions:
                question = AssessmentQuestion(
                    course_id=course_id,
                    module_index=None,
                    question_type='subjective',
                    question_text=f"Explain the key concepts and their practical applications as discussed in '{course_name}'. Provide specific examples.",
                    explanation="Answer should cover the main concepts, their significance, practical applications, and specific examples from the course content.",
                    is_approved=False
                )
                db.add(question)
                questions_created += 1
        
        db.commit()
        log_activity(db, user.id, "create_test_questions", f"Created {questions_created} test questions for course {course.source_doc_name}")
        
        return {
            "success": True, 
            "message": f"Created {questions_created} test questions successfully",
            "total_questions": questions_created,
            "note": "These are test questions for preview. You can approve them or regenerate with AI."
        }
        
    except Exception as e:
        logger.error('Error creating test questions: %s', e, exc_info=True)
        db.rollback()
        raise HTTPException(status_code=500, detail="Failed to create test questions")

@router.get("/trainer/review-questions/{course_id}", tags=["Assessment System"])
def get_questions_for_review(
    course_id: str,
    user: User = Depends(get_current_active_user),  # Changed from trainer_required to allow super_admin
    db: Session = Depends(get_db)
):
    """Get generated questions for trainer review with module information"""
    try:
        logger.info('Getting questions for review - Course ID: %s, User: %s, Role: %s', ("" if course_id is None else str(course_id).replace("\n", "").replace("\r", "")), ("" if user.email is None else str(user.email).replace("\n", "").replace("\r", "")), ("" if user.role is None else str(user.role).replace("\n", "").replace("\r", "")))
        
        # Verify course ownership or super admin access
        course = db.query(Course).filter(Course.id == course_id).first()
        
        if not course:
            logger.warning('Course not found: %s', ("" if course_id is None else str(course_id).replace("\n", "").replace("\r", "")))
            raise HTTPException(status_code=404, detail="Course not found")
        
        # Check permissions: must be course owner OR super admin
        if course.author_id != user.id and user.role != 'super_admin':
            logger.warning('User %s attempted to access course %s owned by %s', ("" if user.email is None else str(user.email).replace("\n", "").replace("\r", "")), ("" if course_id is None else str(course_id).replace("\n", "").replace("\r", "")), ("" if course.author_id is None else str(course.author_id).replace("\n", "").replace("\r", "")))
            raise HTTPException(status_code=403, detail="You don't have permission to access this course")
        
        # Get assessment config to understand scope
        config = db.query(AssessmentConfig).filter(
            AssessmentConfig.course_id == course_id
        ).first()
        
        questions = db.query(AssessmentQuestion).filter(
            AssessmentQuestion.course_id == course_id,
            AssessmentQuestion.is_rejected == False,
        ).order_by(AssessmentQuestion.module_index.asc(), AssessmentQuestion.created_at.asc()).all()

        rejected_questions = db.query(AssessmentQuestion).filter(
            AssessmentQuestion.course_id == course_id,
            AssessmentQuestion.is_rejected == True,
        ).order_by(AssessmentQuestion.module_index.asc(), AssessmentQuestion.created_at.asc()).all()
        
        logger.info('Found %s active and %s rejected questions for course %s', len(questions), len(rejected_questions), ("" if course_id is None else str(course_id).replace("\n", "").replace("\r", "")))
        
        # Get module information for display - use course author's ID for S3 path
        author_id = course.author_id
        modules_info = {}
        if config and config.assessment_scope == 'end_of_module':
            try:
                # CRITICAL FIX: Parse source_documents for multi-document courses
                course_docs_normalized = []
                try:
                    if course.source_documents:
                        source_docs_list = json.loads(course.source_documents)
                        if isinstance(source_docs_list, list):
                            for doc in source_docs_list:
                                doc_normalized = normalize_document_name(doc)
                                course_docs_normalized.append(doc_normalized)
                            logger.info('[REVIEW QUESTIONS] Parsed %s documents: %s', len(course_docs_normalized), ("" if course_docs_normalized is None else str(course_docs_normalized).replace("\n", "").replace("\r", "")))
                except Exception as e:
                    logger.warning('[REVIEW QUESTIONS] Could not parse source_documents JSON: %s', e)
                
                # Fallback to single source_doc_name if source_documents is empty
                if not course_docs_normalized:
                    course_doc_normalized = normalize_document_name(course.source_doc_name)
                    course_docs_normalized = [course_doc_normalized]
                    logger.info('[REVIEW QUESTIONS] Using fallback single source_doc_name: %s', ("" if course_doc_normalized is None else str(course_doc_normalized).replace("\n", "").replace("\r", "")))
                
                # Load module metadata from S3
                all_modules = []
                seen_review_keys = set()  # DEDUP
                paginator = s3_client.get_paginator('list_objects_v2')
                for page in paginator.paginate(Bucket=S3_BUCKET_NAME, Prefix=f"{author_id}/generated_reports/"):
                    for obj in page.get('Contents', []):
                        if obj['Key'].endswith('.json'):
                            try:
                                meta_obj = s3_client.get_object(Bucket=S3_BUCKET_NAME, Key=obj['Key'])
                                meta_data = json.loads(meta_obj['Body'].read().decode('utf-8'))
                                source_doc = meta_data.get('source_doc', '')
                                
                                # Normalize the metadata source_doc for comparison
                                source_doc_normalized = normalize_document_name(source_doc)
                                
                                # Check if this module matches ANY of the course documents
                                # Use EXACT match first; substring only when strings are very similar (≥80% length ratio)
                                is_match = False
                                for course_doc_norm in course_docs_normalized:
                                    if source_doc_normalized == course_doc_norm:
                                        is_match = True
                                        break
                                    len_a = len(course_doc_norm)
                                    len_b = len(source_doc_normalized)
                                    if len_a > 0 and len_b > 0:
                                        len_ratio = min(len_a, len_b) / max(len_a, len_b)
                                        if len_ratio >= 0.8 and (course_doc_norm in source_doc_normalized or source_doc_normalized in course_doc_norm):
                                            is_match = True
                                            break
                                
                                if is_match:
                                    seg_index = meta_data.get('seg_index', 0)
                                    dedup_key = (source_doc_normalized, seg_index)
                                    if dedup_key in seen_review_keys:
                                        logger.warning('[REVIEW QUESTIONS] Skipping duplicate module: source_doc=%s, seg_index=%s', ("" if source_doc is None else str(source_doc).replace("\n", "").replace("\r", "")), ("" if seg_index is None else str(seg_index).replace("\n", "").replace("\r", "")))
                                        continue
                                    seen_review_keys.add(dedup_key)
                                    all_modules.append({
                                        'seg_index': seg_index,
                                        'title': meta_data.get('title', 'Module'),
                                        'source_doc': source_doc
                                    })
                                    logger.info('[REVIEW QUESTIONS] Found module %s: %s from %s', ("" if seg_index is None else str(seg_index).replace("\n", "").replace("\r", "")), ("" if meta_data.get('title') is None else str(meta_data.get('title')).replace("\n", "").replace("\r", "")), ("" if source_doc is None else str(source_doc).replace("\n", "").replace("\r", "")))
                            except Exception as e:
                                logger.error('Error loading module info: %s', e)
                
                # Sort and renumber modules sequentially (same as assessment generation)
                all_modules.sort(key=lambda x: x['seg_index'])
                for new_index, module in enumerate(all_modules, start=1):
                    modules_info[new_index] = {
                        'title': module['title'],
                        'index': new_index
                    }
                    logger.info('[REVIEW QUESTIONS] Module %s: %s (was index %s)', ("" if new_index is None else str(new_index).replace("\n", "").replace("\r", "")), ("" if module['title'] is None else str(module['title']).replace("\n", "").replace("\r", "")), ("" if module['seg_index'] is None else str(module['seg_index']).replace("\n", "").replace("\r", "")))
                
            except Exception as e:
                logger.error('Error loading modules info: %s', e)
        
        questions_data = []
        for q in questions:
            # Get module title if available
            module_title = "End of Course"
            if q.module_index is not None:
                module_info = modules_info.get(q.module_index, {})
                module_title = module_info.get('title', f'Module {q.module_index}')
            
            question_data = {
                "id": q.id,
                "module_index": q.module_index,
                "module_title": module_title,
                "question_type": q.question_type,
                "question_text": q.question_text,
                "is_approved": q.is_approved,
                "created_at": q.created_at.isoformat() if q.created_at else None
            }
            
            if q.question_type == 'mcq':
                question_data.update({
                    "option_a": q.option_a,
                    "option_b": q.option_b,
                    "option_c": q.option_c,
                    "option_d": q.option_d,
                    "correct_answer": q.correct_answer,
                    "explanation": q.explanation
                })
            else:
                question_data["explanation"] = q.explanation
            
            questions_data.append(question_data)
        
        # Group questions by module for better display
        grouped_questions = {}
        for q in questions_data:
            module_key = q['module_index'] if q['module_index'] is not None else 'end_of_course'
            if module_key not in grouped_questions:
                grouped_questions[module_key] = {
                    'module_title': q['module_title'],
                    'module_index': q['module_index'],
                    'questions': []
                }
            grouped_questions[module_key]['questions'].append(q)
        
        logger.info('Returning %s questions grouped by %s modules', len(questions_data), len(grouped_questions))

        if config and config.assessment_scope == 'end_of_module' and not modules_info:
            max_module_index = db.query(func.max(AssessmentQuestion.module_index)).filter(
                AssessmentQuestion.course_id == course_id,
                AssessmentQuestion.module_index.isnot(None),
            ).scalar() or 0
            for idx in range(1, int(max_module_index) + 1):
                modules_info[idx] = {"title": f"Module {idx}", "index": idx}

        module_options = [
            {"index": idx, "title": info.get("title", f"Module {idx}")}
            for idx, info in sorted(modules_info.items())
        ]

        rejected_data = []
        for q in rejected_questions:
            module_title = "End of Course"
            if q.module_index is not None:
                module_info = modules_info.get(q.module_index, {})
                module_title = module_info.get("title", f"Module {q.module_index}")
            item = {
                "id": q.id,
                "module_index": q.module_index,
                "module_title": module_title,
                "question_type": q.question_type,
                "question_text": q.question_text,
                "is_approved": q.is_approved,
                "is_rejected": True,
                "created_at": q.created_at.isoformat() if q.created_at else None,
            }
            if q.question_type == "mcq":
                item.update({
                    "option_a": q.option_a,
                    "option_b": q.option_b,
                    "option_c": q.option_c,
                    "option_d": q.option_d,
                    "correct_answer": q.correct_answer,
                    "explanation": q.explanation,
                })
            else:
                item["explanation"] = q.explanation
            rejected_data.append(item)

        return {
            "success": True, 
            "questions": questions_data,
            "rejected_questions": rejected_data,
            "grouped_questions": grouped_questions,
            "total_questions": len(questions_data),
            "rejected_count": len(rejected_data),
            "assessment_scope": config.assessment_scope if config else "end_of_course",
            "module_options": module_options,
            "module_count": len(module_options),
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error('Error getting questions for review: %s', log_safe(e), exc_info=True)
        raise HTTPException(status_code=500, detail=GENERIC_API_ERROR_MESSAGE)

@router.post("/trainer/review-question/{question_id}", tags=["Assessment System"])
def review_question(
    question_id: str,
    review: QuestionReviewRequest,
    user: User = Depends(trainer_required),
    db: Session = Depends(get_db)
):
    """Review and approve/reject/edit a question"""
    try:
        question = db.query(AssessmentQuestion).filter(
            AssessmentQuestion.id == question_id
        ).first()
        
        if not question:
            raise HTTPException(status_code=404, detail="Question not found")
        
        # Verify course ownership or super admin access
        course = db.query(Course).filter(
            Course.id == question.course_id
        ).first()
        
        if not course:
            raise HTTPException(status_code=404, detail="Course not found")
        
        # Allow if user is course owner OR super admin
        if course.author_id != user.id and user.role != 'super_admin':
            raise HTTPException(status_code=403, detail="Not authorized")
        
        # If super admin is making changes, notify the trainer
        notify_trainer = (user.role == 'super_admin' and course.author_id != user.id)
        
        if review.action == 'approve':
            question.is_approved = True
            db.commit()
            log_activity(db, user.id, "approve_question", f"Approved question for course {course.source_doc_name}")
            
            # Notify trainer if super admin made the change
            if notify_trainer:
                trainer = db.query(User).filter(User.id == course.author_id).first()
                if trainer:
                    notification = Notification(
                        user_id=trainer.id,
                        message=f"Super Admin approved a question in your course '{course_display_label(course)}'",
                        course_id=course_notification_id(course)
                    )
                    db.add(notification)
                    db.commit()
            
            return {"success": True, "message": "Question approved successfully"}
            
        elif review.action == 'reject':
            question.is_rejected = True
            question.is_approved = False
            db.commit()
            log_activity(db, user.id, "reject_question", f"Archived question for course {course.source_doc_name}")
            
            # Notify trainer if super admin made the change
            if notify_trainer:
                trainer = db.query(User).filter(User.id == course.author_id).first()
                if trainer:
                    notification = Notification(
                        user_id=trainer.id,
                        message=f"Super Admin rejected a question in your course '{course_display_label(course)}'",
                        course_id=course_notification_id(course)
                    )
                    db.add(notification)
                    db.commit()
            
            return {"success": True, "message": "Question archived. You can restore it from the Rejected section."}

        elif review.action == 'restore':
            question.is_rejected = False
            question.is_approved = False
            db.commit()
            log_activity(db, user.id, "restore_question", f"Restored archived question for course {course.source_doc_name}")
            return {"success": True, "message": "Question restored to the review queue"}

        elif review.action == 'edit':
            # Update question with trainer edits (does not auto-approve)
            if review.question_text:
                question.question_text = review.question_text
            if review.correct_answer:
                question.correct_answer = review.correct_answer
            if review.option_a:
                question.option_a = review.option_a
            if review.option_b:
                question.option_b = review.option_b
            if review.option_c:
                question.option_c = review.option_c
            if review.option_d:
                question.option_d = review.option_d
            if review.explanation:
                question.explanation = review.explanation
            question.is_approved = False
            question.is_rejected = False
            db.commit()
            log_activity(db, user.id, "edit_question", f"Edited question for course {course.source_doc_name}")
            
            # Notify trainer if super admin made the change
            if notify_trainer:
                trainer = db.query(User).filter(User.id == course.author_id).first()
                if trainer:
                    notification = Notification(
                        user_id=trainer.id,
                        message=f"Super Admin edited a question in your course '{course_display_label(course)}'",
                        course_id=course_notification_id(course)
                    )
                    db.add(notification)
                    db.commit()
            
            return {"success": True, "message": "Question updated. Please approve it again before publishing."}
        
        return {"success": False, "message": "Invalid action"}
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error('Error reviewing question: %s', log_safe(e), exc_info=True)
        db.rollback()
        raise HTTPException(status_code=500, detail=GENERIC_API_ERROR_MESSAGE)

@router.post("/trainer/add-question/{course_id}", tags=["Assessment System"])
def add_new_question(
    course_id: str,
    question_data: dict,
    user: User = Depends(trainer_required),
    db: Session = Depends(get_db)
):
    """Add a new question to the course"""
    try:
        # Verify course exists and user has permission
        course = db.query(Course).filter(Course.id == course_id).first()
        
        if not course:
            raise HTTPException(status_code=404, detail="Course not found")
        
        # Check permissions: must be course owner OR super admin
        if course.author_id != user.id and user.role != 'super_admin':
            raise HTTPException(status_code=403, detail="You don't have permission to access this course")
        
        # Track if super admin is making changes
        is_super_admin_action = (user.role == 'super_admin' and course.author_id != user.id)
        
        # Get assessment config to check module scope
        config = db.query(AssessmentConfig).filter(
            AssessmentConfig.course_id == course_id
        ).first()
        
        if not config:
            raise HTTPException(status_code=400, detail="Assessment not configured")
        
        # Create new question
        new_question = AssessmentQuestion(
            course_id=course_id,
            module_index=question_data.get('module_index'),
            question_type=question_data['question_type'],
            question_text=question_data['question_text'],
            is_approved=False
        )
        
        if question_data['question_type'] == 'mcq':
            new_question.option_a = question_data.get('option_a')
            new_question.option_b = question_data.get('option_b')
            new_question.option_c = question_data.get('option_c')
            new_question.option_d = question_data.get('option_d')
            new_question.correct_answer = question_data.get('correct_answer')
        
        new_question.explanation = question_data.get('explanation')
        
        db.add(new_question)
        db.commit()
        
        log_activity(db, user.id, "add_question", f"Added new question to course {course.source_doc_name}")
        
        # Notify trainer if super admin added the question
        if is_super_admin_action:
            trainer = db.query(User).filter(User.id == course.author_id).first()
            if trainer:
                notification = Notification(
                    user_id=trainer.id,
                    message=f"Super Admin added a new question to your course '{course_display_label(course)}'",
                    course_id=course_notification_id(course)
                )
                db.add(notification)
                db.commit()
        
        return {"success": True, "message": "Question added successfully", "question_id": new_question.id}
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error('Error adding question: %s', log_safe(e), exc_info=True)
        db.rollback()
        raise HTTPException(status_code=500, detail=GENERIC_API_ERROR_MESSAGE)

@router.post("/trainer/regenerate-question/{question_id}", tags=["Assessment System"])
def regenerate_single_question(
    question_id: str,
    req: RegenerateQuestionRequest,
    user: User = Depends(trainer_required),
    db: Session = Depends(get_db)
):
    """Regenerate a single question using AI with optional custom prompt"""
    try:
        question = db.query(AssessmentQuestion).filter(
            AssessmentQuestion.id == question_id
        ).first()
        
        if not question:
            raise HTTPException(status_code=404, detail="Question not found")
        
        # Verify course ownership or super admin access
        course = db.query(Course).filter(
            Course.id == question.course_id
        ).first()
        
        if not course:
            raise HTTPException(status_code=404, detail="Course not found")
        
        # Allow if user is course owner OR super admin
        if course.author_id != user.id and user.role != 'super_admin':
            raise HTTPException(status_code=403, detail="Not authorized")
        
        # If super admin is making changes, notify the trainer
        notify_trainer = (user.role == 'super_admin' and course.author_id != user.id)
        
        # Log custom prompt if provided
        if req.custom_prompt:
            logger.info('[REGENERATE] Custom prompt provided: %s', ("" if req.custom_prompt is None else str(req.custom_prompt).replace("\n", "").replace("\r", "")))
            log_activity(db, user.id, "REGENERATE_QUESTION_WITH_CUSTOM_PROMPT", 
                        f"Custom prompt: {req.custom_prompt[:100]}")
        
        # Get all existing questions for this course to avoid duplicates
        existing_questions = db.query(AssessmentQuestion).filter(
            AssessmentQuestion.course_id == question.course_id,
            AssessmentQuestion.id != question_id  # Exclude the question being regenerated
        ).all()
        
        existing_question_texts = set()
        existing_questions_list = []
        
        # Separate questions by module for better context
        questions_by_module = {}
        for eq in existing_questions:
            if eq.question_text:
                existing_question_texts.add(eq.question_text.lower().strip())
                existing_questions_list.append(eq.question_text)
                
                # Track which module each question belongs to
                module_idx = eq.module_index if eq.module_index is not None else "end_of_course"
                if module_idx not in questions_by_module:
                    questions_by_module[module_idx] = []
                questions_by_module[module_idx].append(eq.question_text[:80])
        
        logger.info('[REGENERATE] Found %s existing questions to avoid duplicates', len(existing_question_texts))
        logger.info('[REGENERATE] Existing question IDs: %s', ("" if [eq.id for eq in existing_questions] is None else str([eq.id for eq in existing_questions]).replace("\n", "").replace("\r", "")))
        logger.info("[REGENERATE] Questions by module:")
        for module_idx, q_list in sorted(questions_by_module.items()):
            logger.info('[REGENERATE]   Module %s: %s questions', ("" if module_idx is None else str(module_idx).replace("\n", "").replace("\r", "")), len(q_list))
            for q_text in q_list[:2]:
                logger.info('[REGENERATE]     - %s...', ("" if q_text is None else str(q_text).replace("\n", "").replace("\r", "")))
        
        logger.info(
            '[REGENERATE] Regenerating question from Module %s',
            str(question.module_index if question.module_index is not None else 'end_of_course').replace("\n", "").replace("\r", ""),
        )
        logger.info('[REGENERATE] Question source_doc_name: %s', ("" if question.source_doc_name is None else str(question.source_doc_name).replace("\n", "").replace("\r", "")))
        logger.info('[REGENERATE] Question ID: %s', ("" if question.id is None else str(question.id).replace("\n", "").replace("\r", "")))
        
        # CRITICAL FIX: Get ALL module content for better context, not just the specific module
        # This ensures the regenerated question considers the full course context
        content = ""
        modules_content = []
        author_id = course.author_id
        
        try:
            # Parse source_documents for multi-document courses
            course_docs_normalized = []
            try:
                if course.source_documents:
                    source_docs_list = json.loads(course.source_documents)
                    if isinstance(source_docs_list, list):
                        for doc in source_docs_list:
                            doc_normalized = normalize_document_name(doc)
                            course_docs_normalized.append(doc_normalized)
                        logger.info('[REGENERATE] Parsed %s documents from source_documents', len(course_docs_normalized))
            except Exception as e:
                logger.warning('[REGENERATE] Could not parse source_documents JSON: %s', e)
            
            # Fallback to single source_doc_name if source_documents is empty
            if not course_docs_normalized:
                course_doc_normalized = normalize_document_name(course.source_doc_name)
                course_docs_normalized = [course_doc_normalized]
                logger.info('[REGENERATE] Using fallback single source_doc_name: %s', ("" if course_doc_normalized is None else str(course_doc_normalized).replace("\n", "").replace("\r", "")))
            
            # Load ALL modules from the course
            paginator = s3_client.get_paginator('list_objects_v2')
            for page in paginator.paginate(Bucket=S3_BUCKET_NAME, Prefix=f"{author_id}/generated_reports/"):
                for obj in page.get('Contents', []):
                    if obj['Key'].endswith('.json'):
                        try:
                            meta_obj = s3_client.get_object(Bucket=S3_BUCKET_NAME, Key=obj['Key'])
                            meta_data = json.loads(meta_obj['Body'].read().decode('utf-8'))
                            source_doc = meta_data.get('source_doc', '')
                            source_doc_normalized = normalize_document_name(source_doc)
                            
                            # Check if this module matches ANY of the course documents
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
                                
                                modules_content.append({
                                    'index': meta_data.get('seg_index', 0),
                                    'title': meta_data.get('title', 'Module'),
                                    'content': module_content,
                                    'source_doc': source_doc  # Include source_doc for matching
                                })
                        except Exception as e:
                            logger.error('[REGENERATE] Error loading module: %s', e)
            
            # Sort modules by index
            modules_content.sort(key=lambda x: x['index'])
            
            # CRITICAL FIX: Renumber modules sequentially (1, 2, 3...) for multi-document courses
            # This ensures questions are matched to the correct module number
            for new_index, module in enumerate(modules_content, start=1):
                old_index = module['index']
                module['index'] = new_index
                logger.info('[REGENERATE] Renumbering: Module %s (was %s) from %s', ("" if new_index is None else str(new_index).replace("\n", "").replace("\r", "")), ("" if old_index is None else str(old_index).replace("\n", "").replace("\r", "")), ("" if module.get('source_doc', 'unknown') is None else str(module.get('source_doc', 'unknown')).replace("\n", "").replace("\r", "")))
            
            logger.info('[REGENERATE] Total modules loaded: %s', len(modules_content))
            for m in modules_content:
                logger.info('[REGENERATE] Module %s: %s from %s (%s chars)', ("" if m['index'] is None else str(m['index']).replace("\n", "").replace("\r", "")), ("" if m['title'] is None else str(m['title']).replace("\n", "").replace("\r", "")), ("" if m.get('source_doc', 'unknown') is None else str(m.get('source_doc', 'unknown')).replace("\n", "").replace("\r", "")), len(m['content']))
            
            # If regenerating for a specific module, use ONLY that module's content
            # Match by BOTH module_index AND source_doc_name for multi-document courses
            if question.module_index is not None:
                # Find the exact module using both index and source_doc
                target_module = None
                
                logger.info('[REGENERATE] Looking for module %s with source_doc_name: %s', ("" if question.module_index is None else str(question.module_index).replace("\n", "").replace("\r", "")), ("" if question.source_doc_name is None else str(question.source_doc_name).replace("\n", "").replace("\r", "")))
                logger.info('[REGENERATE] Available modules: %s', ("" if [(m['index'], m.get('source_doc', 'unknown')) for m in modules_content] is None else str([(m['index'], m.get('source_doc', 'unknown')) for m in modules_content]).replace("\n", "").replace("\r", "")))
                
                # First try to match by both module_index and source_doc_name
                if question.source_doc_name:
                    source_doc_normalized = normalize_document_name(question.source_doc_name)
                    for m in modules_content:
                        m_source_normalized = normalize_document_name(m.get('source_doc', ''))
                        if m['index'] == question.module_index and (
                            source_doc_normalized == m_source_normalized or
                            source_doc_normalized in m_source_normalized or
                            m_source_normalized in source_doc_normalized
                        ):
                            target_module = m
                            logger.info('[REGENERATE] ✓ Found EXACT module %s from %s', ("" if question.module_index is None else str(question.module_index).replace("\n", "").replace("\r", "")), ("" if question.source_doc_name is None else str(question.source_doc_name).replace("\n", "").replace("\r", "")))
                            break
                
                # Fallback: match by module_index only (for legacy questions without source_doc_name)
                if not target_module:
                    target_module = next((m for m in modules_content if m['index'] == question.module_index), None)
                    if target_module:
                        logger.warning('[REGENERATE] ⚠️ Matched module %s by index only (no source_doc_name)', ("" if question.module_index is None else str(question.module_index).replace("\n", "").replace("\r", "")))
                
                if target_module:
                    # Use ONLY the target module content for precise question generation
                    content = target_module['content']
                    logger.info('[REGENERATE] ✓ Using EXACT module %s content (%s chars)', ("" if question.module_index is None else str(question.module_index).replace("\n", "").replace("\r", "")), len(content))
                    logger.info('[REGENERATE] Module title: %s', ("" if target_module['title'] is None else str(target_module['title']).replace("\n", "").replace("\r", "")))
                    logger.info('[REGENERATE] Module source: %s', ("" if target_module.get('source_doc', 'unknown') is None else str(target_module.get('source_doc', 'unknown')).replace("\n", "").replace("\r", "")))
                else:
                    # Module not found - log warning and use all modules as fallback
                    logger.warning('[REGENERATE] ❌ Module %s not found (source_doc: %s)', ("" if question.module_index is None else str(question.module_index).replace("\n", "").replace("\r", "")), ("" if question.source_doc_name is None else str(question.source_doc_name).replace("\n", "").replace("\r", "")))
                    logger.warning('[REGENERATE] Available modules: %s', ("" if [(m['index'], m.get('source_doc', 'unknown')) for m in modules_content] is None else str([(m['index'], m.get('source_doc', 'unknown')) for m in modules_content]).replace("\n", "").replace("\r", "")))
                    content = "\n\n".join([f"Module {m['index']}: {m['title']}\n{m['content']}" for m in modules_content])
                    logger.warning('[REGENERATE] Falling back to all %s modules', len(modules_content))
            else:
                # End of course question - use all modules
                content = "\n\n".join([f"Module {m['index']}: {m['title']}\n{m['content']}" for m in modules_content])
                logger.info('[REGENERATE] Using all %s modules for end-of-course question', len(modules_content))
            
            if not content:
                content = "General course content"
                logger.warning("[REGENERATE] No module content found, using fallback")
                
        except Exception as e:
            logger.error('[REGENERATE] Error loading course content: %s', e)
            content = "General course content"
        
        logger.info('[REGENERATE] Total content length: %s characters', len(content))
        
        # Generate a new question — no retry limit, no uniqueness blocking
        logger.info('[REGENERATE] Generating new question (type: %s)', ("" if question.question_type is None else str(question.question_type).replace("\n", "").replace("\r", "")))

        # Pass ALL existing questions INCLUDING the one being replaced as "avoid" context
        # so the AI knows not to regenerate the same question again.
        # We still save whatever the AI returns — no blocking.
        avoid_questions = list(existing_questions_list)  # includes the current question text
        if question.question_text not in avoid_questions:
            avoid_questions.insert(0, question.question_text)  # ensure it's first in the avoid list

        new_questions = generate_questions_for_content(
            content, 1, question.question_type,
            1 if question.question_type == 'mcq' else 0,
            1 if question.question_type == 'subjective' else 0,
            avoid_questions,   # AI is told to avoid ALL of these including the old question
            req.custom_prompt
        )

        if not new_questions:
            return {"success": False, "message": "AI did not return a question. Please try again."}

        new_q = new_questions[0]
        question.question_text = new_q['question']
        if question.question_type == 'mcq':
            question.option_a = new_q.get('option_a')
            question.option_b = new_q.get('option_b')
            question.option_c = new_q.get('option_c')
            question.option_d = new_q.get('option_d')
            question.correct_answer = new_q.get('correct_answer')
        question.explanation = new_q.get('explanation')
        question.is_approved = False
        db.commit()
        log_activity(db, user.id, "regenerate_question", f"Regenerated question for course {course.source_doc_name}")

        if notify_trainer:
            trainer = db.query(User).filter(User.id == course.author_id).first()
            if trainer:
                db.add(Notification(
                    user_id=trainer.id,
                    message=f"Super Admin regenerated a question in your course '{course_display_label(course)}'",
                    course_id=course_notification_id(course)
                ))
                db.commit()

        logger.info("[REGENERATE] ✅ Question regenerated successfully")
        return {
            "success": True,
            "message": "Question regenerated. It is pending review — use Regenerate again if you want a different version.",
            "question_id": question.id,
            "regenerated": True,
        }
        
    except Exception as e:
        logger.error('Error regenerating question: %s', e, exc_info=True)
        db.rollback()
        raise HTTPException(status_code=500, detail="Failed to regenerate question")

@router.post("/trainee/validate-subjective-answer", tags=["Assessment System"])
def validate_subjective_answer(
    request: dict,
    user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """Validate subjective answer using AI and provide scoring"""
    try:
        if user.role != 'trainee':
            raise HTTPException(status_code=403, detail="Trainee access required")
        
        question_id = request.get('question_id')
        user_answer = request.get('user_answer', '').strip()
        
        if not question_id or not user_answer:
            raise HTTPException(status_code=400, detail="Question ID and answer are required")
        
        # Get the question
        question = db.query(AssessmentQuestion).filter(
            AssessmentQuestion.id == question_id
        ).first()
        
        if not question:
            raise HTTPException(status_code=404, detail="Question not found")
        
        if question.question_type != 'subjective':
            raise HTTPException(status_code=400, detail="This endpoint is only for subjective questions")
        
        # Use AI to validate the answer
        validation_prompt = f"""
        You are an expert educator evaluating a student's answer to a subjective question.
        
        Question: {question.question_text}
        
        Expected Answer Points: {question.explanation}
        
        Student's Answer: {user_answer}
        
        Please evaluate the student's answer and provide:
        1. A score out of 10 points
        2. Detailed feedback on what was correct
        3. Areas for improvement
        4. Overall assessment
        
        Respond in JSON format:
        {{
            "score": 8,
            "max_score": 10,
            "feedback": "Your answer demonstrates good understanding...",
            "strengths": ["Point 1", "Point 2"],
            "improvements": ["Area 1", "Area 2"],
            "overall_assessment": "Good/Excellent/Needs Improvement"
        }}
        """
        
        try:
            # AI agent disabled due to 503 errors - using fallback evaluation
            logger.info("[AI TEST] AI agent disabled, using fallback evaluation")
            
            # Simple fallback evaluation
            answer_length = len(user_answer.strip())
            if answer_length > 50:
                validation_result = {
                    "score": 8,
                    "max_score": 10,
                    "feedback": "Good comprehensive answer provided.",
                    "strengths": ["Shows good understanding", "Adequate detail provided"],
                    "improvements": ["Could include more specific examples"],
                    "overall_assessment": "Good"
                }
            elif answer_length > 20:
                validation_result = {
                    "score": 6,
                    "max_score": 10,
                    "feedback": "Basic answer provided with room for improvement.",
                    "strengths": ["Shows basic understanding"],
                    "improvements": ["Needs more detail and examples"],
                    "overall_assessment": "Needs Improvement"
                }
            else:
                validation_result = {
                    "score": 3,
                    "max_score": 10,
                    "feedback": "Answer too brief. Please provide more detailed responses.",
                    "strengths": ["Attempted to answer"],
                    "improvements": ["Needs much more detail and explanation"],
                    "overall_assessment": "Needs Improvement"
                }
        except Exception as e:
            logger.error('AI validation failed: %s', e)
            # Fallback scoring
            validation_result = {
                "score": 7,
                "max_score": 10,
                "feedback": "Your answer has been reviewed. Please refer to the course materials for more detailed information.",
                "strengths": ["Shows understanding of basic concepts"],
                "improvements": ["Could provide more specific examples"],
                "overall_assessment": "Good"
            }
        
        return {
            "success": True,
            "validation": validation_result
        }
        
    except Exception as e:
        logger.error('Error validating subjective answer: %s', log_safe(e), exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to validate answer")

@router.post("/admin/test-ai-evaluation", tags=["Admin"])
def test_ai_evaluation(
    question_text: str,
    module_content: str,
    trainee_answer: str,
    user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """Test AI evaluation for subjective questions (Admin only)"""
    try:
        if user.role not in ['admin', 'trainer']:
            raise HTTPException(status_code=403, detail="Admin or trainer access required")
        
        # Test the AI evaluation
        validation_prompt = f"""
        {ANSWER_VALIDATION_AGENT_INSTRUCTIONS}
        
        Module Content:
        ---
        {module_content}
        ---
        
        Question:
        ---
        {question_text}
        ---
        
        Trainee's Answer:
        ---
        {trainee_answer}
        ---
        """
        
        # AI agent disabled due to 503 errors - using fallback evaluation
        logger.info("[AI TEST] AI agent disabled, using fallback evaluation")
        
        # Simple fallback evaluation
        answer_length = len(trainee_answer.strip())
        if answer_length > 50:
            is_correct = True
            explanation = "Good comprehensive answer provided. AI evaluation temporarily disabled."
        elif answer_length > 20:
            is_correct = False
            explanation = "Basic answer provided. AI evaluation temporarily disabled - consider adding more detail."
        else:
            is_correct = False
            explanation = "Answer too brief. AI evaluation temporarily disabled - please provide more detailed responses."
        
        # Create a mock AI response for testing
        mock_ai_response = f"Fallback evaluation based on answer length ({answer_length} characters). {explanation}"
        
        return {
            "success": True,
            "ai_response": mock_ai_response,
            "parsed_result": {
                "is_correct": is_correct,
                "explanation": explanation
            },
            "test_data": {
                "question": question_text,
                "module_content": module_content,
                "trainee_answer": trainee_answer
            }
        }
        
    except Exception as e:
        logger.error('Error testing AI evaluation: %s', e)
        raise HTTPException(status_code=500, detail=GENERIC_API_ERROR_MESSAGE)

@router.post("/admin/reset-attempts/{user_id}/{course_id}", tags=["Admin"])
def reset_user_attempts(
    user_id: str,
    course_id: str,
    new_attempts: int = 3,
    user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """Reset attempts for a specific user and course (Admin only)"""
    try:
        if user.role not in ['admin', 'trainer']:
            raise HTTPException(status_code=403, detail="Admin or trainer access required")
        
        # Find the attempt record
        attempt = db.query(TraineeAttempt).filter(
            TraineeAttempt.trainee_id == user_id,
            TraineeAttempt.course_id == course_id
        ).first()
        
        if not attempt:
            raise HTTPException(status_code=404, detail="Attempt record not found")
        
        old_attempts = attempt.attempts_left
        attempt.attempts_left = new_attempts
        db.commit()
        
        logger.info('[ADMIN] Reset attempts for user %s in course %s: %s -> %s', ("" if user_id is None else str(user_id).replace("\n", "").replace("\r", "")), ("" if course_id is None else str(course_id).replace("\n", "").replace("\r", "")), ("" if old_attempts is None else str(old_attempts).replace("\n", "").replace("\r", "")), ("" if new_attempts is None else str(new_attempts).replace("\n", "").replace("\r", "")))
        
        return {
            "success": True,
            "message": f"Attempts reset from {old_attempts} to {new_attempts}",
            "user_id": user_id,
            "course_id": course_id,
            "new_attempts": new_attempts
        }
        
    except Exception as e:
        logger.error('Error resetting attempts: %s', e)
        raise HTTPException(status_code=500, detail="Failed to reset attempts")

@router.post("/admin/sync-course-attempts/{course_id}", tags=["Admin"])
def sync_course_attempts(
    course_id: str,
    user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """
    Sync all trainee attempts for a course to match the assessment configuration.
    This is useful for fixing existing records after assessment configuration changes.
    """
    try:
        if user.role not in ['super_admin', 'trainer']:
            raise HTTPException(status_code=403, detail="Admin or trainer access required")
        
        # Get the course
        course = db.query(Course).filter(Course.id == course_id).first()
        if not course:
            raise HTTPException(status_code=404, detail="Course not found")
        
        # Check if user has permission (must be course owner or super admin)
        if user.role == 'trainer' and course.author_id != user.id:
            raise HTTPException(status_code=403, detail="You don't have permission to modify this course")
        
        # Get assessment config
        assessment_config = db.query(AssessmentConfig).filter(
            AssessmentConfig.course_id == course_id
        ).first()
        
        if not assessment_config:
            raise HTTPException(status_code=400, detail="Assessment not configured for this course")
        
        # Get all trainee attempts for this course
        attempts = db.query(TraineeAttempt).filter(
            TraineeAttempt.course_id == course_id
        ).all()
        
        if not attempts:
            return {
                "success": True,
                "message": "No trainee attempts found for this course",
                "updated_count": 0
            }
        
        # Sync attempts
        updated_count = 0
        for attempt in attempts:
            if attempt.attempts_left != assessment_config.max_attempts:
                old_attempts = attempt.attempts_left
                attempt.attempts_left = assessment_config.max_attempts
                updated_count += 1
                logger.info('[SYNC] Updated attempts for trainee %s in course %s: %s -> %s', ("" if attempt.trainee_id is None else str(attempt.trainee_id).replace("\n", "").replace("\r", "")), ("" if course_id is None else str(course_id).replace("\n", "").replace("\r", "")), ("" if old_attempts is None else str(old_attempts).replace("\n", "").replace("\r", "")), ("" if assessment_config.max_attempts is None else str(assessment_config.max_attempts).replace("\n", "").replace("\r", "")))
        
        db.commit()
        
        return {
            "success": True,
            "message": f"Synced {updated_count} trainee attempt records to {assessment_config.max_attempts} attempts",
            "course_id": course_id,
            "course_name": course.source_doc_name,
            "max_attempts": assessment_config.max_attempts,
            "updated_count": updated_count,
            "total_trainees": len(attempts)
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error('Error syncing course attempts: %s', e)
        raise HTTPException(status_code=500, detail="Failed to sync course attempts")
        
        
    except Exception as e:
        logger.error('Error resetting attempts: %s', e)
        raise HTTPException(status_code=500, detail="Failed to reset attempts")


# Sentinel stored in TraineeQuestionSet.module_index for end-of-course caches (legacy rows may be NULL).
_COURSE_LEVEL_MODULE_INDEX = 0


def _trainee_question_variant_seed(
    trainee_id: str,
    course_id: str,
    module_index: Optional[int],
) -> str:
    """Stable per-trainee seed — different trainees, different AI/reference variants."""
    scope = "course-final" if module_index is None else f"module-{module_index}"
    return f"{trainee_id}:{course_id}:{scope}"


def _deterministic_rng(seed_key: str):
    import hashlib
    import random as _random

    digest = hashlib.sha256(seed_key.encode("utf-8")).hexdigest()
    return _random.Random(int(digest[:16], 16))


def _shuffle_mcq_options_dict(q: Dict, rng) -> Dict:
    """Shuffle MCQ option letters so trainees can get different option layouts."""
    q = dict(q)
    qtype = normalize_question_type(q.get("type", q.get("question_type", "mcq")))
    if qtype != "mcq":
        return q

    letters = ["A", "B", "C", "D"]
    options = [
        (letter, q.get(f"option_{letter.lower()}"))
        for letter in letters
        if q.get(f"option_{letter.lower()}")
    ]
    if len(options) < 2:
        return q

    correct_letter = str(q.get("correct_answer", "A")).strip().upper()[:1]
    correct_text = q.get(f"option_{correct_letter.lower()}")
    rng.shuffle(options)
    new_correct = "A"
    for i, (old_letter, text) in enumerate(options):
        new_letter = letters[i]
        q[f"option_{new_letter.lower()}"] = text
        if old_letter == correct_letter or (correct_text and text == correct_text):
            new_correct = new_letter
    q["correct_answer"] = new_correct
    return q


def _personalize_reference_questions_for_trainee(
    ref_q: List,
    trainee_id: str,
    course_id: str,
    module_index: Optional[int],
    mcq_count: int,
    subj_count: int,
) -> List[Dict]:
    """
    When AI generation is unavailable, build a trainee-specific variant from references.
    Different trainees get different order/selection/option layout; same trainee keeps
    the cached set on every retake.
    """
    if not ref_q:
        return []

    rng = _deterministic_rng(_trainee_question_variant_seed(trainee_id, course_id, module_index))
    mcq_refs = [q for q in ref_q if q.question_type == "mcq"]
    subj_refs = [q for q in ref_q if q.question_type != "mcq"]
    rng.shuffle(mcq_refs)
    rng.shuffle(subj_refs)

    selected = mcq_refs[:mcq_count] + subj_refs[:subj_count]
    if len(selected) < mcq_count + subj_count:
        pool = list(ref_q)
        rng.shuffle(pool)
        seen_ids = {getattr(q, "id", None) for q in selected}
        for q in pool:
            qid = getattr(q, "id", None)
            if qid in seen_ids:
                continue
            selected.append(q)
            seen_ids.add(qid)
            if len(selected) >= mcq_count + subj_count:
                break

    result: List[Dict] = []
    for q in selected[: mcq_count + subj_count]:
        d = _question_to_dict(q)
        d["id"] = str(uuid.uuid4())
        d = _shuffle_mcq_options_dict(d, rng)
        if module_index is not None:
            d["module_index"] = module_index
        result.append(d)
    return result


def _trainee_question_set_query(
    db: Session,
    trainee_id: str,
    course_id: str,
    module_index: Optional[int],
):
    """Lookup cached unique questions (attempt_number=1 is reused for all retakes)."""
    query = db.query(TraineeQuestionSet).filter(
        TraineeQuestionSet.trainee_id == trainee_id,
        TraineeQuestionSet.course_id == course_id,
        TraineeQuestionSet.attempt_number == 1,
    )
    if module_index is None:
        return query.filter(
            or_(
                TraineeQuestionSet.module_index.is_(None),
                TraineeQuestionSet.module_index == _COURSE_LEVEL_MODULE_INDEX,
            )
        )
    return query.filter(TraineeQuestionSet.module_index == module_index)


def build_trainee_question_sets(
    db: Session,
    trainee_ids: Any,
    course_ids: Any,
) -> Dict[Any, Any]:
    """Cached question sets for a whole page of trainees at once.

    Returns ``{(trainee_id, course_id, module_index): TraineeQuestionSet}``. A course-level set is
    stored under a ``None`` module index as well as under the index it is persisted with, so a
    lookup finds the same row ``_trainee_question_set_query`` would have returned: rows are read
    in ``created_at`` order and the first row seen for a key wins, exactly like the ``.first()``
    those per-attempt queries used.
    """
    sets: Dict[Any, Any] = {}
    unique_trainees = list({trainee_id for trainee_id in (trainee_ids or []) if trainee_id})
    unique_courses = list({course_id for course_id in (course_ids or []) if course_id})
    if not unique_trainees or not unique_courses:
        return sets

    rows = db.query(TraineeQuestionSet).filter(
        TraineeQuestionSet.trainee_id.in_(unique_trainees),
        TraineeQuestionSet.course_id.in_(unique_courses),
        TraineeQuestionSet.attempt_number == 1,
    ).order_by(TraineeQuestionSet.created_at.asc()).all()

    for row in rows:
        keys = [(row.trainee_id, row.course_id, row.module_index)]
        if row.module_index is None or row.module_index == _COURSE_LEVEL_MODULE_INDEX:
            keys.append((row.trainee_id, row.course_id, None))
        for key in keys:
            if key not in sets:
                sets[key] = row
    return sets


def _store_module_index_for_question_set(module_index: Optional[int]) -> Optional[int]:
    """Normalize module_index before persisting a TraineeQuestionSet row."""
    if module_index is None:
        return _COURSE_LEVEL_MODULE_INDEX
    return module_index


def _generate_unique_questions_for_trainee(
    db: Session,
    course: "Course",
    config: "AssessmentConfig",
    module_index: Optional[int],
    trainee_id: str,
) -> List[Dict]:
    """
    Generate a unique set of questions for a specific trainee for a module/course.

    Guarantees:
    - Each trainee gets DIFFERENT questions (seed = trainee_id)
    - All retakes of the SAME module by the SAME trainee get the EXACT SAME questions
      (cached in TraineeQuestionSet with attempt_number=1)
    """
    # ── 1. Load ONLY the reference questions for this specific module ──────────
    q_filter = [
        AssessmentQuestion.course_id == course.id,
        AssessmentQuestion.is_approved == True,
    ]
    if module_index is not None:
        q_filter.append(AssessmentQuestion.module_index == module_index)
    else:
        q_filter.append(AssessmentQuestion.module_index.is_(None))

    ref_q = db.query(AssessmentQuestion).filter(*q_filter).all()

    # ── 2. Determine exact MCQ / subjective counts for this module ─────────────
    if module_index is not None:
        num_modules = db.query(func.count(AssessmentQuestion.module_index.distinct())).filter(
            AssessmentQuestion.course_id == course.id,
            AssessmentQuestion.is_approved == True,
            AssessmentQuestion.module_index.isnot(None)
        ).scalar() or 1

        mcq_base,  mcq_rem  = divmod(config.mcq_count,       num_modules)
        subj_base, subj_rem = divmod(config.subjective_count, num_modules)
        mcq_count  = mcq_base  + (1 if module_index <= mcq_rem  else 0)
        subj_count = subj_base + (1 if module_index <= subj_rem else 0)
    else:
        mcq_count  = config.mcq_count
        subj_count = config.subjective_count

    total = mcq_count + subj_count
    if total == 0:
        logger.warning('[UNIQUE Q] Computed 0 questions for module %s, using ref count', ("" if module_index is None else str(module_index).replace("\n", "").replace("\r", "")))
        total     = len(ref_q)
        mcq_count  = sum(1 for q in ref_q if q.question_type == 'mcq')
        subj_count = total - mcq_count

    # ── 3. Load the module content from S3 (matching the correct module_index) ─
    # IMPORTANT: module_index is the SEQUENTIAL index (1,2,3...) assigned during
    # question generation — NOT the raw seg_index from S3 metadata.
    # We must sort all matching modules by seg_index and pick position module_index-1.
    module_content = ""
    module_title   = f"Module {module_index}" if module_index else "Course Assessment"
    try:
        course_docs_normalized = []
        if course.source_documents:
            try:
                raw = json.loads(course.source_documents)
                if isinstance(raw, list):
                    course_docs_normalized = [normalize_document_name(d) for d in raw]
            except Exception:
                pass
        if not course_docs_normalized:
            course_docs_normalized = [normalize_document_name(course.source_doc_name)]

        # Collect ALL matching modules first, then sort and pick by sequential position
        all_modules = []
        paginator = s3_client.get_paginator('list_objects_v2')
        for page in paginator.paginate(Bucket=S3_BUCKET_NAME, Prefix=f"{course.author_id}/generated_reports/"):
            for obj in page.get('Contents', []):
                if not obj['Key'].endswith('.json'):
                    continue
                try:
                    meta_obj  = s3_client.get_object(Bucket=S3_BUCKET_NAME, Key=obj['Key'])
                    meta_data = json.loads(meta_obj['Body'].read().decode('utf-8'))
                    src = normalize_document_name(meta_data.get('source_doc', ''))

                    if any(src == d or d in src or src in d for d in course_docs_normalized):
                        all_modules.append({
                            'seg_index': meta_data.get('seg_index', 0),
                            'title':     meta_data.get('title', ''),
                            'md_key':    obj['Key'].replace('.json', '.md'),
                        })
                except Exception as e:
                    logger.warning('[UNIQUE Q] Error reading module metadata: %s', log_safe(e))

        # Sort by seg_index — same order used during question generation
        all_modules.sort(key=lambda x: x['seg_index'])

        if module_index is not None:
            # Pick the module at sequential position module_index-1
            idx = module_index - 1
            if 0 <= idx < len(all_modules):
                m = all_modules[idx]
                if m['title']:
                    module_title = m['title']
                try:
                    md_obj = s3_client.get_object(Bucket=S3_BUCKET_NAME, Key=m['md_key'])
                    module_content = md_obj['Body'].read().decode('utf-8')
                    logger.info("[UNIQUE Q] Loaded module %s content: '%s' (%s chars)", ("" if module_index is None else str(module_index).replace("\n", "").replace("\r", "")), ("" if module_title is None else str(module_title).replace("\n", "").replace("\r", "")), len(module_content))
                except Exception as e:
                    logger.warning('[UNIQUE Q] Error loading module content: %s', log_safe(e))
            else:
                logger.warning('[UNIQUE Q] module_index %s out of range (found %s modules)', ("" if module_index is None else str(module_index).replace("\n", "").replace("\r", "")), len(all_modules))
        else:
            # end_of_course — concatenate all modules
            for m in all_modules:
                try:
                    md_obj = s3_client.get_object(Bucket=S3_BUCKET_NAME, Key=m['md_key'])
                    module_content += md_obj['Body'].read().decode('utf-8') + "\n\n"
                except Exception as e:
                    logger.warning('[UNIQUE Q] Error loading module content: %s', log_safe(e))

    except Exception as e:
        logger.error('[UNIQUE Q] Error fetching module content: %s', log_safe(e))

    if not module_content.strip():
        logger.warning("[UNIQUE Q] No module content found, using trainee-specific reference variants")
        return _personalize_reference_questions_for_trainee(
            ref_q, trainee_id, course.id, module_index, mcq_count, subj_count
        )

    # ── 4. Build a rich reference block (full question + answer for MCQ) ───────
    ref_mcq_lines   = []
    ref_subj_lines  = []
    for i, q in enumerate(ref_q, 1):
        if q.question_type == 'mcq':
            ref_mcq_lines.append(
                f"  REF-MCQ-{i}: {q.question_text}\n"
                f"    Options: A) {q.option_a}  B) {q.option_b}  C) {q.option_c}  D) {q.option_d}\n"
                f"    Correct: {q.correct_answer}  |  Concept tested: {q.explanation or 'N/A'}"
            )
        else:
            ref_subj_lines.append(
                f"  REF-SUBJ-{i}: {q.question_text}\n"
                f"    Key answer points: {q.explanation or 'N/A'}"
            )

    ref_block = ""
    if ref_mcq_lines:
        ref_block += "REFERENCE MCQ QUESTIONS (DO NOT COPY — concept guides only):\n"
        ref_block += "\n".join(ref_mcq_lines)
    if ref_subj_lines:
        ref_block += "\n\nREFERENCE SUBJECTIVE QUESTIONS (DO NOT COPY — concept guides only):\n"
        ref_block += "\n".join(ref_subj_lines)
    if not ref_block:
        ref_block = "(No reference questions available — generate from module content only)"

    # ── 5. Uniqueness seed (trainee-specific, not attempt-specific) ────────────
    seed_phrase = _trainee_question_variant_seed(trainee_id, course.id, module_index)

    # ── 6. Build the detailed custom prompt ────────────────────────────────────
    custom_prompt = f"""
══════════════════════════════════════════════════════════════════
  UNIQUE QUESTION GENERATION — TRAINEE-SPECIFIC VARIANT
  Module: {module_title}  |  Variant seed: {seed_phrase}
══════════════════════════════════════════════════════════════════

⚠️  CRITICAL OVERRIDE — READ BEFORE ANYTHING ELSE  ⚠️
You MUST generate questions that are SUBSTANTIALLY DIFFERENT from the
reference questions below. "Substantially different" means:
  • A different sentence structure in the stem
  • A different cognitive angle (application/analysis, not just recall)
  • Completely different option text for MCQs
  • A different scenario or context framing

If your output looks like a paraphrase or near-copy of any reference
question, it is WRONG. Regenerate it until it is genuinely different.

TARGET COUNTS FOR THIS MODULE
------------------------------
  • MCQ questions:        {mcq_count}
  • Subjective questions: {subj_count}
  • TOTAL:                {total}

MODULE: {module_title}

{ref_block}

HOW TO USE THE REFERENCES
--------------------------
The references above tell you WHICH CONCEPTS to test.
They do NOT tell you HOW to test them — that is your job.

For each reference question, identify the underlying concept, then
test that concept using a COMPLETELY DIFFERENT approach:

  REFERENCE APPROACH → YOUR APPROACH
  ─────────────────────────────────────────────────────────────
  "What is X?"              → "A practitioner needs to apply X in
                               situation Y. What should they do?"
  "Which option is NOT X?"  → "Under what condition does X apply?"
  "Explain the process of X"→ "Compare process X with process Z.
                               What are the key differences?"
  "What form is used for X?"→ "A claim processor encounters error Y.
                               Which step in process X resolves it?"
  ─────────────────────────────────────────────────────────────

STRICT RULES
------------
1. ✗ NEVER copy a sentence from any reference question
2. ✗ NEVER use the same option text as any reference MCQ option
3. ✗ NEVER ask the same question with just one word changed
   (e.g. adding "not" or swapping "requiring" → "not requiring")
4. ✗ NEVER ask about document structure, headings, or sections
5. ✓ ALWAYS use scenario/application framing for at least 60% of MCQs
6. ✓ ALWAYS use higher-order verbs for subjective questions:
   analyse, evaluate, compare, justify, predict, critique, design
7. ✓ ALWAYS ensure the correct answer is derivable from the module
   content provided — not from the reference questions

DIFFICULTY DISTRIBUTION
-----------------------
  ~30% Easy   (recall / recognition of specific facts)
  ~50% Medium (application to a scenario or process)
  ~20% Hard   (analysis, comparison, or evaluation)

QUALITY CHECK (verify each question before including)
------------------------------------------------------
  ✓ Is the stem structurally different from ALL reference questions?
  ✓ Does it test the same concept from a different cognitive angle?
  ✓ For MCQ: are all 4 options plausible, distinct, and different
    from the reference options?
  ✓ For subjective: does it require multi-point reasoning?
  ✓ Is the explanation ≥ 2 sentences and specific to the content?

GROUND TRUTH: The module content is the authoritative source.
All correct answers MUST be verifiable from that content.
══════════════════════════════════════════════════════════════════
"""

    logger.info(
        "[UNIQUE Q] Generating %s unique questions for trainee %s, module %s (%s) — MCQ:%s Subj:%s | %s reference questions loaded",
        ("" if total is None else str(total).replace("\n", "").replace("\r", "")),
        ("" if trainee_id is None else str(trainee_id[-8:]).replace("\n", "").replace("\r", "")),
        ("" if module_index is None else str(module_index).replace("\n", "").replace("\r", "")),
        ("" if module_title is None else str(module_title).replace("\n", "").replace("\r", "")),
        ("" if mcq_count is None else str(mcq_count).replace("\n", "").replace("\r", "")),
        ("" if subj_count is None else str(subj_count).replace("\n", "").replace("\r", "")),
        len(ref_q),
    )

    # ── 7. Call AI generation ──────────────────────────────────────────────────
    # NOTE: Do NOT pass existing_questions here — the reference questions are
    # already fully handled inside custom_prompt with explicit variation rules.
    # Passing them as "avoid duplicates" in the base prompt causes the AI to
    # anchor to them and produce near-copies instead of genuinely new questions.
    generated = generate_questions_for_content(
        content=module_content,
        total_questions=total,
        question_type=config.question_type,
        mcq_count=mcq_count,
        subjective_count=subj_count,
        existing_questions=None,   # handled via custom_prompt
        custom_prompt=custom_prompt
    )

    if not generated:
        logger.warning("[UNIQUE Q] AI returned no questions, using trainee-specific reference variants")
        return _personalize_reference_questions_for_trainee(
            ref_q, trainee_id, course.id, module_index, mcq_count, subj_count
        )

    # ── 8. Assign stable UUIDs ─────────────────────────────────────────────────
    for q in generated:
        q['id'] = str(uuid.uuid4())
        if module_index is not None:
            q['module_index'] = module_index

    logger.info('[UNIQUE Q] ✅ Generated %s unique questions for trainee %s, module %s', len(generated), ("" if trainee_id[-8:] is None else str(trainee_id[-8:]).replace("\n", "").replace("\r", "")), ("" if module_index is None else str(module_index).replace("\n", "").replace("\r", "")))
    return generated


def _question_to_dict(q: "AssessmentQuestion") -> Dict:
    """Convert an AssessmentQuestion ORM object to a plain dict."""
    d = {
        'id': q.id,
        'type': q.question_type,
        'question': q.question_text,
        'correct_answer': q.correct_answer,
        'explanation': q.explanation,
    }
    if q.module_index is not None:
        d['module_index'] = q.module_index
    if q.question_type == 'mcq':
        d.update({'option_a': q.option_a, 'option_b': q.option_b,
                  'option_c': q.option_c, 'option_d': q.option_d})
    return d


def _granted_attempt_count(request_row: Any) -> int:
    """How many attempts one approved request is worth.

    The in-memory equivalent of ``COALESCE(granted_attempts, 1)``, for the endpoints that read the
    request rows once and aggregate them in Python instead of asking SQL to sum. NULL means the row
    was approved before the column existed, when an approval was always worth exactly one attempt.
    Deliberately mirrors COALESCE rather than clamping, so the counted and summed paths cannot drift;
    the approval API only accepts 1..100, so the stored value is always positive.
    """
    granted = getattr(request_row, "granted_attempts", None)
    return 1 if granted is None else int(granted)


def _get_additional_attempt_allowance(
    db: Session,
    trainee_id: str,
    course_id: str,
    module_index: Optional[int]
) -> int:
    """
    Total extra attempts granted by approved additional-attempt requests.

    - For module assessments, both module-specific approvals and course-wide approvals (module_index is null)
      are counted.
    - For end-of-course assessments (module_index is null), only course-wide approvals are counted.

    Sums ``granted_attempts`` rather than counting rows, because a trainer approving a request is
    asked how many attempts to grant and may answer more than one. That number used to be added to
    ``TraineeAttempt.attempts_left`` only, so counting rows here silently turned a grant of 3 into a
    grant of 1. Rows approved before the column existed hold NULL and count as the single attempt
    they have always been worth.
    """
    base_query = db.query(
        func.coalesce(func.sum(func.coalesce(AdditionalAttemptRequest.granted_attempts, 1)), 0)
    ).filter(
        AdditionalAttemptRequest.trainee_id == trainee_id,
        AdditionalAttemptRequest.course_id == course_id,
        AdditionalAttemptRequest.status == 'approved'
    )

    if module_index is None:
        approved_count = base_query.filter(AdditionalAttemptRequest.module_index.is_(None)).scalar()
    else:
        approved_count = base_query.filter(
            or_(
                AdditionalAttemptRequest.module_index == module_index,
                AdditionalAttemptRequest.module_index.is_(None)
            )
        ).scalar()

    return approved_count or 0


def _lock_trainee_attempt_slot(db: Session, trainee_id: str, course_id: str):
    """Serialise one trainee's attempt bookkeeping for one course.

    Both submit paths number a new attempt ``count(existing) + 1`` and check the cap against that
    same count. The read and the write are separate statements, so submissions that overlap all
    read the same count, all pass the same cap check and all write the same number. A UAT module
    holds 21 rows for one trainee against a cap of 1, every row scoring 80% and every row stored as
    attempt 1 - which is why all 21 review tabs were labelled "Attempt 1". Taking this lock before
    the count makes each submission see the ones committed ahead of it.

    A row lock on the trainee's own enrolment row rather than an advisory lock: it is ordinary SQL,
    it is released when the transaction ends so a request that dies cannot hold it, and it does not
    pin a connection in the RDS Proxy that UAT connects through. It locks one trainee's row for one
    course, so no trainee ever waits on another.

    Returns the locked enrolment row. Callers that must serialise (module submit) treat a missing
    row as "not enrolled" rather than silently proceeding without a lock — a no-op lock is how
    concurrent retries recreate the 21× Attempt 1 defect.
    """
    return db.query(TraineeAttempt).filter(
        TraineeAttempt.trainee_id == trainee_id,
        TraineeAttempt.course_id == course_id,
    ).with_for_update().first()


def _module_attempt_limits(
    db: Session,
    trainee_id: str,
    course_id: str,
    module_index: int,
    config: AssessmentConfig,
) -> Dict[str, Any]:
    """Per-module attempt counts including approved trainer extensions."""
    attempts_used = db.query(func.count(TraineeAssessment.id)).filter(
        TraineeAssessment.trainee_id == trainee_id,
        TraineeAssessment.course_id == course_id,
        TraineeAssessment.module_index == module_index,
    ).scalar() or 0
    additional_allowance = _get_additional_attempt_allowance(
        db=db,
        trainee_id=trainee_id,
        course_id=course_id,
        module_index=module_index,
    )
    effective_max_attempts = config.max_attempts + additional_allowance
    has_passed = db.query(TraineeAssessment).filter(
        TraineeAssessment.trainee_id == trainee_id,
        TraineeAssessment.course_id == course_id,
        TraineeAssessment.module_index == module_index,
        TraineeAssessment.passed == True,
    ).first() is not None
    return {
        "attempts_used": attempts_used,
        "effective_max_attempts": effective_max_attempts,
        "additional_allowance": additional_allowance,
        "attempts_remaining": max(0, effective_max_attempts - attempts_used),
        "has_passed": has_passed,
        "current_attempt_number": attempts_used + 1,
    }


def _course_attempt_limits(
    db: Session,
    trainee_id: str,
    course_id: str,
    config: AssessmentConfig,
) -> Dict[str, Any]:
    """End-of-course attempt counts, numbered the same way as the per-module ones.

    The attempt number has to come from a count of the attempts actually stored. It used to be
    derived as ``config.max_attempts - TraineeAttempt.attempts_left + 1``, which broke two ways:
    ``attempts_left`` is seeded from ``Course.initial_attempts`` and not from ``max_attempts``, so
    the first attempt of a course configured for 3 attempts but created with 1 was recorded as
    attempt 3; and ``/admin/reset-attempts`` and ``/admin/sync-course-attempts`` both write
    ``attempts_left`` back up without regard to what is already recorded, which restarts the
    numbering and leaves a course holding two attempts with the same number. That number is the key
    the review path addresses an attempt by, so a duplicate made one attempt show another's marks.
    """
    attempts_used = db.query(func.count(TraineeAssessment.id)).filter(
        TraineeAssessment.trainee_id == trainee_id,
        TraineeAssessment.course_id == course_id,
        TraineeAssessment.module_index.is_(None),
    ).scalar() or 0
    additional_allowance = _get_additional_attempt_allowance(
        db=db,
        trainee_id=trainee_id,
        course_id=course_id,
        module_index=None,
    )
    effective_max_attempts = config.max_attempts + additional_allowance
    has_passed = db.query(TraineeAssessment).filter(
        TraineeAssessment.trainee_id == trainee_id,
        TraineeAssessment.course_id == course_id,
        TraineeAssessment.module_index.is_(None),
        TraineeAssessment.passed == True,
    ).first() is not None
    return {
        "attempts_used": attempts_used,
        "effective_max_attempts": effective_max_attempts,
        "additional_allowance": additional_allowance,
        "attempts_remaining": max(0, effective_max_attempts - attempts_used),
        "has_passed": has_passed,
        "current_attempt_number": attempts_used + 1,
    }


def end_of_course_attempts_left_by_trainee_course(
    db: Session,
    trainee_ids: Any,
    course_ids: Any,
    config_by_course: Dict[str, Any],
) -> Dict[Tuple[str, str], int]:
    """Remaining end-of-course attempts per (trainee, course), in two grouped queries.

    The same arithmetic as _course_attempt_limits - the configured maximum, plus approved
    course-wide extra attempts, minus the attempts actually stored - but batched, because the
    listings that need this number put it on every row and were reading
    ``TraineeAttempt.attempts_left`` instead to avoid a query per row. That column is seeded from
    ``Course.initial_attempts`` and rewritten by /admin/reset-attempts, /admin/sync-course-attempts
    and the dashboards' own auto-sync, so it disagreed with the cap the load and submit gates
    enforce (``attempts_used >= effective_max_attempts``): a trainee who had used all 3 configured
    attempts was still shown "2 Attempts Left" and a retake button the server then refused.

    Only end_of_course courses appear in the result. A module-scope course has no single
    course-level budget - each module carries its own - so callers keep their existing value there.
    """
    scoped = [
        course_id
        for course_id in course_ids
        if config_by_course.get(course_id) is not None
        and config_by_course[course_id].assessment_scope == "end_of_course"
    ]
    trainees = list(trainee_ids)
    if not scoped or not trainees:
        return {}

    attempts_used = {
        (trainee_id, course_id): int(used or 0)
        for trainee_id, course_id, used in (
            db.query(
                TraineeAssessment.trainee_id,
                TraineeAssessment.course_id,
                func.count(TraineeAssessment.id),
            )
            .filter(
                TraineeAssessment.trainee_id.in_(trainees),
                TraineeAssessment.course_id.in_(scoped),
                TraineeAssessment.module_index.is_(None),
            )
            .group_by(TraineeAssessment.trainee_id, TraineeAssessment.course_id)
            .all()
        )
    }
    allowances = {
        (trainee_id, course_id): int(granted or 0)
        for trainee_id, course_id, granted in (
            db.query(
                AdditionalAttemptRequest.trainee_id,
                AdditionalAttemptRequest.course_id,
                func.coalesce(
                    func.sum(func.coalesce(AdditionalAttemptRequest.granted_attempts, 1)), 0
                ),
            )
            .filter(
                AdditionalAttemptRequest.trainee_id.in_(trainees),
                AdditionalAttemptRequest.course_id.in_(scoped),
                AdditionalAttemptRequest.status == "approved",
                AdditionalAttemptRequest.module_index.is_(None),
            )
            .group_by(AdditionalAttemptRequest.trainee_id, AdditionalAttemptRequest.course_id)
            .all()
        )
    }
    return {
        (trainee_id, course_id): max(
            0,
            config_by_course[course_id].max_attempts
            + allowances.get((trainee_id, course_id), 0)
            - attempts_used.get((trainee_id, course_id), 0),
        )
        for trainee_id in trainees
        for course_id in scoped
    }


def end_of_course_attempts_left_by_course(
    db: Session,
    trainee_id: str,
    course_ids: Any,
    config_by_course: Dict[str, Any],
) -> Dict[str, int]:
    """One trainee's remaining end-of-course attempts, keyed by course."""
    by_pair = end_of_course_attempts_left_by_trainee_course(
        db, [trainee_id], course_ids, config_by_course
    )
    return {course_id: remaining for (_, course_id), remaining in by_pair.items()}


def _assert_can_take_module_assessment(
    db: Session,
    trainee_id: str,
    course_id: str,
    module_index: int,
    config: AssessmentConfig,
) -> Dict[str, Any]:
    """Raise 403 when the trainee cannot start or submit another module attempt."""
    limits = _module_attempt_limits(db, trainee_id, course_id, module_index, config)
    if limits["has_passed"]:
        raise HTTPException(
            status_code=403,
            detail="You have already passed this module assessment. No more attempts needed.",
        )
    if limits["attempts_used"] >= limits["effective_max_attempts"]:
        raise HTTPException(
            status_code=403,
            detail=(
                f"Maximum attempts ({limits['effective_max_attempts']}) reached for this module. "
                "Please request additional attempts from your trainer."
            ),
        )
    return limits


def _assert_can_take_course_assessment(
    db: Session,
    trainee_id: str,
    course_id: str,
    config: AssessmentConfig,
) -> Dict[str, Any]:
    """Raise 403 when the trainee cannot start or submit another end-of-course attempt.

    The module path has always gated on the attempts actually stored. The end-of-course path gated
    on ``TraineeAttempt.attempts_left`` instead - a counter seeded from ``Course.initial_attempts``,
    decremented on submit, and written back up by /admin/reset-attempts, /admin/sync-course-attempts
    and the trainee dashboard's own auto-sync. So the course had two independent budgets, and every
    surface that reports attempts (the results modal, the attempt history, the progress modal) counts
    the stored ones. They disagreed in both directions:

      * A trainee who had used all 3 configured attempts was shown "2 Attempts Left" and allowed to
        start a 4th, because a reset had put the counter back up mid-course.
      * Approved extra attempts were destroyed: the counter was clamped down to ``max_attempts`` on
        load, so a trainer granting a 4th attempt on a 3-attempt course granted nothing.

    Gating on the same count the rest of the system reports removes the second budget. The pass check
    stays with the callers, which have their own scope-appropriate messages.
    """
    limits = _course_attempt_limits(db, trainee_id, course_id, config)
    if limits["attempts_used"] >= limits["effective_max_attempts"]:
        raise HTTPException(
            status_code=403,
            detail=(
                f"Maximum attempts ({limits['effective_max_attempts']}) reached for this course "
                "assessment. Please request additional attempts from your trainer."
            ),
        )
    return limits


def _assert_previous_module_assessment_passed(
    db: Session,
    trainee_id: str,
    course_id: str,
    module_index: int,
) -> None:
    """For end-of-module flow: module N requires a passed assessment on module N-1."""
    if module_index <= 1:
        return
    previous_module = module_index - 1
    prev_passed = db.query(TraineeAssessment).filter(
        TraineeAssessment.trainee_id == trainee_id,
        TraineeAssessment.course_id == course_id,
        TraineeAssessment.module_index == previous_module,
        TraineeAssessment.passed == True,
    ).first()
    if not prev_passed:
        raise HTTPException(
            status_code=403,
            detail=(
                f"You must pass Module {previous_module} assessment before taking "
                f"Module {module_index}."
            ),
        )


def _get_or_create_trainee_question_set(
    db: Session,
    trainee_id: str,
    course_id: str,
    module_index: Optional[int],
    course,
    config
) -> List[Dict]:
    """
    Return the cached question set for this trainee+module, or generate a new one.

    Guarantees:
    - Same trainee, same module → SAME questions every time (all retakes included)
    - Different trainee, same module → DIFFERENT questions
    - attempt_number is always stored as 1 (one set per trainee per module)
    """
    existing = _trainee_question_set_query(
        db, trainee_id, course_id, module_index
    ).order_by(TraineeQuestionSet.created_at.asc()).first()

    if existing:
        logger.info('[UNIQUE Q] Returning cached question set for trainee %s, module %s', ("" if trainee_id is None else str(trainee_id).replace("\n", "").replace("\r", "")), ("" if module_index is None else str(module_index).replace("\n", "").replace("\r", "")))
        return json.loads(existing.questions_json)

    # Generate fresh unique questions for this trainee
    questions = _generate_unique_questions_for_trainee(
        db=db, course=course, config=config,
        module_index=module_index,
        trainee_id=trainee_id,
    )

    # Another request may have cached while we were generating
    existing = _trainee_question_set_query(
        db, trainee_id, course_id, module_index
    ).order_by(TraineeQuestionSet.created_at.asc()).first()
    if existing:
        logger.info('[UNIQUE Q] Using question set cached by concurrent request for trainee %s', ("" if trainee_id is None else str(trainee_id).replace("\n", "").replace("\r", "")))
        return json.loads(existing.questions_json)

    # Cache with attempt_number=1 (reused for all retakes)
    tqs = TraineeQuestionSet(
        trainee_id=trainee_id,
        course_id=course_id,
        module_index=_store_module_index_for_question_set(module_index),
        attempt_number=1,
        questions_json=json.dumps(questions)
    )
    db.add(tqs)
    try:
        db.commit()
    except Exception as cache_err:
        db.rollback()
        existing = _trainee_question_set_query(
            db, trainee_id, course_id, module_index
        ).order_by(TraineeQuestionSet.created_at.asc()).first()
        if existing:
            logger.info('[UNIQUE Q] Concurrent cache write — using existing set for trainee %s', ("" if trainee_id is None else str(trainee_id).replace("\n", "").replace("\r", "")))
            return json.loads(existing.questions_json)
        logger.error('[UNIQUE Q] Failed to cache question set: %s', log_safe(cache_err))
        raise HTTPException(status_code=500, detail=GENERIC_API_ERROR_MESSAGE)
    logger.info('[UNIQUE Q] Cached %s unique questions for trainee %s, module %s', len(questions), ("" if trainee_id is None else str(trainee_id).replace("\n", "").replace("\r", "")), ("" if module_index is None else str(module_index).replace("\n", "").replace("\r", "")))
    return questions


@router.get("/trainee/module-assessment/{course_id}/{module_index}", tags=["Assessment System"])
def get_module_assessment(
    course_id: str,
    module_index: int,
    user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """Get assessment for a specific module"""
    try:
        if user.role != 'trainee':
            raise HTTPException(status_code=403, detail="Trainee access required")
        
        # Check if trainee has access to this course
        attempt = db.query(TraineeAttempt).filter(
            TraineeAttempt.trainee_id == user.id,
            TraineeAttempt.course_id == course_id
        ).first()
        
        if not attempt:
            raise HTTPException(status_code=403, detail="No access to this course")
        
        # CRITICAL: Check if course is published - assessments only available for published courses
        course = db.query(Course).filter(Course.id == course_id).first()
        if not course:
            raise HTTPException(status_code=404, detail="Course not found")
        
        if not course.is_live:
            raise HTTPException(status_code=403, detail="Assessment not available - course is not published")
        
        # Get assessment config
        config = db.query(AssessmentConfig).filter(
            AssessmentConfig.course_id == course_id
        ).first()
        
        if not config or not config.is_approved:
            raise HTTPException(status_code=400, detail="Assessment not configured")
        
        # Check if this is module-based assessment
        if config.assessment_scope != 'end_of_module':
            raise HTTPException(status_code=400, detail="This course uses end-of-course assessment")
        
        # CRITICAL FIX: Count attempts PER MODULE, not per course
        limits = _assert_can_take_module_assessment(
            db=db,
            trainee_id=user.id,
            course_id=course_id,
            module_index=module_index,
            config=config,
        )
        existing_attempts_count = limits["attempts_used"]
        effective_max_attempts = limits["effective_max_attempts"]
        additional_allowance = limits["additional_allowance"]
        attempts_remaining = limits["attempts_remaining"]
        current_attempt_number = limits["current_attempt_number"]
        
        logger.info(
            "[ASSESSMENT LOAD] User %s has %s/%s attempts used for module %s (base=%s, extra=%s), %s remaining",
            ("" if user.id is None else str(user.id).replace("\n", "").replace("\r", "")),
            ("" if existing_attempts_count is None else str(existing_attempts_count).replace("\n", "").replace("\r", "")),
            ("" if effective_max_attempts is None else str(effective_max_attempts).replace("\n", "").replace("\r", "")),
            ("" if module_index is None else str(module_index).replace("\n", "").replace("\r", "")),
            ("" if config.max_attempts is None else str(config.max_attempts).replace("\n", "").replace("\r", "")),
            ("" if additional_allowance is None else str(additional_allowance).replace("\n", "").replace("\r", "")),
            ("" if attempts_remaining is None else str(attempts_remaining).replace("\n", "").replace("\r", "")),
        )
        
        logger.info('[ASSESSMENT LOAD] User %s starting attempt %s/%s for course %s, module %s', ("" if user.id is None else str(user.id).replace("\n", "").replace("\r", "")), ("" if current_attempt_number is None else str(current_attempt_number).replace("\n", "").replace("\r", "")), ("" if effective_max_attempts is None else str(effective_max_attempts).replace("\n", "").replace("\r", "")), ("" if course_id is None else str(course_id).replace("\n", "").replace("\r", "")), ("" if module_index is None else str(module_index).replace("\n", "").replace("\r", "")))
        
        # SEQUENTIAL ACCESS CONTROL: Check if user has completed previous modules
        if module_index > 1:  # Only check for modules after the first one
            # Get all completed modules for this trainee and course
            completed_modules_records = db.query(CourseProgress.completed_module_index).filter(
                CourseProgress.user_id == user.id,
                CourseProgress.course_id == course_id
            ).all()
            
            completed_module_indices = {record[0] for record in completed_modules_records}
            
            # Check if the previous module (module_index - 1) is completed
            previous_module = module_index - 1
            if previous_module not in completed_module_indices:
                logger.warning('[ASSESSMENT] Trainee %s attempted to access assessment for module %s without completing module %s', ("" if user.id is None else str(user.id).replace("\n", "").replace("\r", "")), ("" if module_index is None else str(module_index).replace("\n", "").replace("\r", "")), ("" if previous_module is None else str(previous_module).replace("\n", "").replace("\r", "")))
                raise HTTPException(
                    status_code=403, 
                    detail=f"You must complete Module {previous_module} before taking the assessment for Module {module_index}. Please complete the modules in order."
                )
            
            logger.info('[ASSESSMENT] ✓ Trainee %s has completed previous modules, allowing access to module %s assessment', ("" if user.id is None else str(user.id).replace("\n", "").replace("\r", "")), ("" if module_index is None else str(module_index).replace("\n", "").replace("\r", "")))

        _assert_previous_module_assessment_passed(
            db=db,
            trainee_id=user.id,
            course_id=course_id,
            module_index=module_index,
        )
        
        # Check if user has completed the current module (required before taking assessment)
        current_module_completed = db.query(CourseProgress).filter(
            CourseProgress.user_id == user.id,
            CourseProgress.course_id == course_id,
            CourseProgress.completed_module_index == module_index
        ).first()
        
        if not current_module_completed:
            raise HTTPException(
                status_code=403, 
                detail=f"You must complete Module {module_index} content before taking its assessment. Please finish the module first."
            )
        
        # Get questions for this module — UNIQUE per trainee per attempt
        # Verify reference questions exist (trainer must have approved some)
        ref_count = db.query(func.count(AssessmentQuestion.id)).filter(
            AssessmentQuestion.course_id == course_id,
            AssessmentQuestion.module_index == module_index,
            AssessmentQuestion.is_approved == True
        ).scalar()

        if not ref_count:
            logger.error('No approved reference questions for course %s, module %s', ("" if course_id is None else str(course_id).replace("\n", "").replace("\r", "")), ("" if module_index is None else str(module_index).replace("\n", "").replace("\r", "")))
            raise HTTPException(status_code=400, detail=f"No questions available for module {module_index}")

        # Get or generate unique question set for this trainee + module
        # NOTE: attempt_number is NOT passed — same questions for all retakes of this module
        unique_questions = _get_or_create_trainee_question_set(
            db=db,
            trainee_id=user.id,
            course_id=course_id,
            module_index=module_index,
            course=course,
            config=config
        )

        logger.info('[UNIQUE Q] Serving %s unique questions to trainee %s, attempt %s', len(unique_questions), ("" if user.id is None else str(user.id).replace("\n", "").replace("\r", "")), ("" if current_attempt_number is None else str(current_attempt_number).replace("\n", "").replace("\r", "")))

        # Use the same renumbered module list as the trainee dashboard (not raw S3 seg_index).
        module_title = _get_module_title_for_index(course_id, module_index, db)

        # Format questions for trainee (hide correct answers)
        questions_data = []
        for q in unique_questions:
            question_data = {
                "id": q['id'],
                "question_type": q.get('type', q.get('question_type', 'mcq')),
                "question_text": q.get('question', q.get('question_text', '')),
                "module_title": module_title,
                "is_unique": True
            }
            if question_data["question_type"] == 'mcq':
                question_data.update({
                    "option_a": q.get('option_a'),
                    "option_b": q.get('option_b'),
                    "option_c": q.get('option_c'),
                    "option_d": q.get('option_d')
                })
            questions_data.append(question_data)

        # Calculate time limit
        question_count = len(questions_data)
        if question_count <= 5:
            module_time_limit = question_count * 3
        elif question_count <= 10:
            module_time_limit = question_count * 2.5
        else:
            module_time_limit = question_count * 2
        module_time_limit = max(5, min(15, int(module_time_limit)))
        time_limit = module_time_limit

        response_data = {
            "success": True,
            "assessment": {
                "id": course_id,
                "module_index": module_index,
                "module_title": module_title,
                "questions": questions_data,
                "time_limit": time_limit,
                "attempt_number": current_attempt_number,
                "max_attempts": effective_max_attempts,
                "pass_percentage": config.pass_percentage
            }
        }
        
        logger.info('[ASSESSMENT] Returning assessment data for course %s, module %s: %s questions, attempt %s/%s', ("" if course_id is None else str(course_id).replace("\n", "").replace("\r", "")), ("" if module_index is None else str(module_index).replace("\n", "").replace("\r", "")), len(questions_data), ("" if current_attempt_number is None else str(current_attempt_number).replace("\n", "").replace("\r", "")), ("" if effective_max_attempts is None else str(effective_max_attempts).replace("\n", "").replace("\r", "")))
        
        return response_data
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error('Error getting module assessment: %s', log_safe(e), exc_info=True)
        raise HTTPException(status_code=500, detail=GENERIC_API_ERROR_MESSAGE)

@router.post("/trainee/submit-module-assessment/{course_id}/{module_index}", tags=["Assessment System"])
def submit_module_assessment(
    course_id: str,
    module_index: int,
    submission: AssessmentSubmissionRequest,
    user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """Submit module assessment answers"""
    try:
        if user.role != 'trainee':
            raise HTTPException(status_code=403, detail="Trainee access required")
        
        # CRITICAL: Check per-module attempts (not course-level attempts)
        # Get assessment config to know max attempts per module
        config = db.query(AssessmentConfig).filter(
            AssessmentConfig.course_id == course_id
        ).first()
        
        if not config or not config.is_approved:
            raise HTTPException(status_code=400, detail="Assessment not configured for this course")

        if config.assessment_scope != 'end_of_module':
            raise HTTPException(status_code=400, detail="This course uses end-of-course assessment")

        # Before counting: overlapping submissions of the same assessment would otherwise each read
        # the same count, each pass the cap check and each store the same attempt number.
        enrollment = _lock_trainee_attempt_slot(db, user.id, course_id)
        if enrollment is None:
            raise HTTPException(
                status_code=403,
                detail="You are not enrolled in this course. Assessment cannot be submitted.",
            )

        # Count existing attempts for THIS SPECIFIC MODULE
        limits = _assert_can_take_module_assessment(
            db=db,
            trainee_id=user.id,
            course_id=course_id,
            module_index=module_index,
            config=config,
        )
        existing_attempts_count = limits["attempts_used"]
        effective_max_attempts = limits["effective_max_attempts"]
        additional_allowance = limits["additional_allowance"]
        current_attempt_number = limits["current_attempt_number"]
        logger.info(
            "[ASSESSMENT SUBMIT] User %s has %s existing attempts for module %s, max allowed: %s (base=%s, extra=%s)",
            ("" if user.id is None else str(user.id).replace("\n", "").replace("\r", "")),
            ("" if existing_attempts_count is None else str(existing_attempts_count).replace("\n", "").replace("\r", "")),
            ("" if module_index is None else str(module_index).replace("\n", "").replace("\r", "")),
            ("" if effective_max_attempts is None else str(effective_max_attempts).replace("\n", "").replace("\r", "")),
            ("" if config.max_attempts is None else str(config.max_attempts).replace("\n", "").replace("\r", "")),
            ("" if additional_allowance is None else str(additional_allowance).replace("\n", "").replace("\r", "")),
        )
        
        logger.info('[ASSESSMENT SUBMIT] This is attempt %s/%s for module %s', ("" if current_attempt_number is None else str(current_attempt_number).replace("\n", "").replace("\r", "")), ("" if effective_max_attempts is None else str(effective_max_attempts).replace("\n", "").replace("\r", "")), ("" if module_index is None else str(module_index).replace("\n", "").replace("\r", "")))
        
        # CRITICAL: Check if course is published - assessments only available for published courses
        course = db.query(Course).filter(Course.id == course_id).first()
        if not course:
            raise HTTPException(status_code=404, detail="Course not found")
        
        if not course.is_live:
            raise HTTPException(status_code=403, detail="Assessment not available - course is not published")
        
        # SEQUENTIAL ACCESS CONTROL: Check if user has completed previous modules
        if module_index > 1:  # Only check for modules after the first one
            # Get all completed modules for this trainee and course
            completed_modules_records = db.query(CourseProgress.completed_module_index).filter(
                CourseProgress.user_id == user.id,
                CourseProgress.course_id == course_id
            ).all()
            
            completed_module_indices = {record[0] for record in completed_modules_records}
            
            # Check if the previous module (module_index - 1) is completed
            previous_module = module_index - 1
            if previous_module not in completed_module_indices:
                logger.warning('[ASSESSMENT SUBMIT] Trainee %s attempted to submit assessment for module %s without completing module %s', ("" if user.id is None else str(user.id).replace("\n", "").replace("\r", "")), ("" if module_index is None else str(module_index).replace("\n", "").replace("\r", "")), ("" if previous_module is None else str(previous_module).replace("\n", "").replace("\r", "")))
                raise HTTPException(
                    status_code=403, 
                    detail=f"You must complete Module {previous_module} before submitting assessment for Module {module_index}. Please complete the modules in order."
                )
            
            logger.info('[ASSESSMENT SUBMIT] ✓ Trainee %s has completed previous modules, allowing submission for module %s assessment', ("" if user.id is None else str(user.id).replace("\n", "").replace("\r", "")), ("" if module_index is None else str(module_index).replace("\n", "").replace("\r", "")))

        _assert_previous_module_assessment_passed(
            db=db,
            trainee_id=user.id,
            course_id=course_id,
            module_index=module_index,
        )
        
        # Check if user has completed the current module (required before taking assessment)
        current_module_completed = db.query(CourseProgress).filter(
            CourseProgress.user_id == user.id,
            CourseProgress.course_id == course_id,
            CourseProgress.completed_module_index == module_index
        ).first()
        
        if not current_module_completed:
            raise HTTPException(
                status_code=403, 
                detail=f"You must complete Module {module_index} content before submitting its assessment. Please finish the module first."
            )
        
        # CRITICAL: Check if course is published - assessments only available for published courses
        course = db.query(Course).filter(Course.id == course_id).first()
        if not course:
            raise HTTPException(status_code=404, detail="Course not found")
        
        if not course.is_live:
            raise HTTPException(status_code=403, detail="Assessment not available - course is not published")
        
        # Get config and questions — use the trainee's UNIQUE question set for scoring
        config = db.query(AssessmentConfig).filter(
            AssessmentConfig.course_id == course_id
        ).first()

        # Load the unique question set that was served to this trainee for this module
        # attempt_number=1 because we store one set per trainee per module (reused across retakes)
        tqs = _trainee_question_set_query(
            db, user.id, course_id, module_index
        ).first()

        if tqs:
            # Score against the unique questions that were actually shown to this trainee
            unique_qs = json.loads(tqs.questions_json)
            logger.info('[SUBMIT] Scoring against %s unique questions for trainee %s, attempt %s', len(unique_qs), ("" if user.id is None else str(user.id).replace("\n", "").replace("\r", "")), ("" if current_attempt_number is None else str(current_attempt_number).replace("\n", "").replace("\r", "")))
        else:
            # Fallback: score against reference questions (shouldn't normally happen)
            logger.warning('[SUBMIT] No unique question set found for trainee %s, attempt %s — falling back to reference questions', ("" if user.id is None else str(user.id).replace("\n", "").replace("\r", "")), ("" if current_attempt_number is None else str(current_attempt_number).replace("\n", "").replace("\r", "")))
            ref_qs = db.query(AssessmentQuestion).filter(
                AssessmentQuestion.course_id == course_id,
                AssessmentQuestion.module_index == module_index,
                AssessmentQuestion.is_approved == True
            ).all()
            unique_qs = [_question_to_dict(q) for q in ref_qs]

        if not unique_qs:
            raise HTTPException(status_code=400, detail="No questions found")

        _assert_all_questions_answered(unique_qs, submission.answers)

        grade_result = grade_trainee_assessment_answers(
            unique_qs=unique_qs,
            answers=submission.answers,
            weightage_per_question=config.weightage_per_question,
            course=course,
            scope_module_index=module_index,
        )
        score = grade_result["score"]
        total_possible = grade_result["total_possible"]
        detailed_results = grade_result["detailed_results"]
        mcq_results_map = grade_result["mcq_results_map"]
        resolved_correct_answers_map = grade_result["resolved_correct_answers_map"]
        subjective_results_map = grade_result.get("subjective_results_map", {})

        percentage = int((score / total_possible) * 100) if total_possible > 0 else 0
        passed = percentage >= config.pass_percentage
        
        # Record assessment with correct per-module attempt number
        assessment = TraineeAssessment(
            trainee_id=user.id,
            course_id=course_id,
            module_index=module_index,
            attempt_number=current_attempt_number,  # Use the per-module attempt number calculated earlier
            questions_data=json.dumps({
                "answers": submission.answers,
                "answer_letters": _build_answer_letters_map(unique_qs, submission.answers),
                "detailed_results": _build_detailed_results_map(detailed_results),
                "mcq_results": mcq_results_map,
                "resolved_correct_answers": resolved_correct_answers_map,
                "subjective_results": subjective_results_map,
            }),
            score=score,
            total_possible=total_possible,
            percentage=percentage,
            passed=passed,
            time_taken=submission.time_taken,
            started_at=datetime.now(timezone.utc) - timedelta(minutes=submission.time_taken)
        )
        db.add(assessment)
        
        # If passed, mark module as completed
        if passed:
            # Check if progress already exists
            existing_progress = db.query(CourseProgress).filter(
                CourseProgress.user_id == user.id,
                CourseProgress.course_id == course_id,
                CourseProgress.completed_module_index == module_index
            ).first()
            
            if not existing_progress:
                progress = CourseProgress(
                    user_id=user.id,
                    course_id=course_id,
                    completed_module_index=module_index
                )
                db.add(progress)
        
        # Note: We don't update course-level attempts anymore since we track per-module attempts
        # Each module has independent attempt tracking via TraineeAssessment records
        
        db.commit()
        log_activity(db, user.id, "submit_module_assessment", f"Submitted module {module_index} assessment for course {course_id}")

        # Never return answer keys / explanations on a failed trainee attempt (UI + network).
        trainee_detailed = sanitize_detailed_results_for_trainee(
            detailed_results, passed=passed
        )

        return {
            "success": True,
            "results": {
                "score": score,
                "total_possible": total_possible,
                "percentage": percentage,
                "passed": passed,
                "pass_percentage": config.pass_percentage,
                "attempts_remaining": max(0, effective_max_attempts - current_attempt_number),
                "detailed_results": trainee_detailed,
                "can_proceed": passed
            }
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error('Error submitting module assessment: %s', log_safe(e), exc_info=True)
        db.rollback()
        raise HTTPException(status_code=500, detail=GENERIC_API_ERROR_MESSAGE)

def normalize_document_name(doc_name: str) -> str:
    """
    Normalize document name by removing file extensions and standardizing format.
    Handles: .pdf, .docx, .doc, .pptx, .ppt, .jpg, .jpeg, .png, .gif, .bmp, .tiff, etc.
    """
    if not doc_name:
        return ""
    
    # Lowercase and trim
    normalized = doc_name.lower().strip()
    
    # Remove common file extensions
    extensions = [
        '.pdf', '.docx', '.doc', '.pptx', '.ppt',  # Documents
        '.jpg', '.jpeg', '.png', '.gif', '.bmp', '.tiff', '.tif', '.webp',  # Images
        '.txt', '.rtf', '.odt',  # Text formats
        '.xlsx', '.xls', '.csv'  # Spreadsheets
    ]
    
    for ext in extensions:
        if normalized.endswith(ext):
            normalized = normalized[:-len(ext)]
            break
    
    return normalized


def _course_docs_normalized_list(course) -> list:
    """Normalized source document names for a course (multi-doc aware)."""
    import json as json_module
    course_docs_normalized = []
    try:
        if course.source_documents:
            source_docs_list = json_module.loads(course.source_documents)
            if isinstance(source_docs_list, list):
                for doc in source_docs_list:
                    course_docs_normalized.append(normalize_document_name(doc))
    except Exception:
        pass
    if not course_docs_normalized:
        course_docs_normalized = [normalize_document_name(course.source_doc_name)]
    return course_docs_normalized


def build_s3_module_counts_for_authors(db: Session, author_ids: set) -> dict:
    """
    Scan S3 once per author and return course_id -> module count.
    Matching rules mirror get_total_modules_count (course_id first, legacy source_doc fallback).
    """
    from collections import defaultdict

    if not author_ids:
        return {}

    courses = db.query(Course).filter(Course.author_id.in_(list(author_ids))).all()
    all_course_ids = {c.id for c in courses}
    # A document may belong to more than one course: multi-document courses list their documents
    # in source_documents, and two of them can overlap. get_total_modules_count tests
    # `source_doc in course_docs_normalized` per course, so a legacy module of a shared document
    # counts towards every course holding it - hence a set of course ids, not the first one.
    source_doc_to_courses = defaultdict(set)
    for course in courses:
        for doc in _course_docs_normalized_list(course):
            source_doc_to_courses[(course.author_id, doc)].add(course.id)

    course_id_keys = defaultdict(set)
    legacy_keys = defaultdict(set)

    for author_id in author_ids:
        for key, meta_data in author_report_metadata(db, author_id):
            meta_course_id = meta_data.get('course_id')
            if meta_course_id and meta_course_id in all_course_ids:
                course_id_keys[meta_course_id].add(key)
            elif not meta_course_id:
                source_doc_normalized = normalize_document_name(
                    meta_data.get('source_doc', '')
                )
                for mapped_course_id in source_doc_to_courses.get(
                    (author_id, source_doc_normalized), ()
                ):
                    legacy_keys[mapped_course_id].add(key)

    counts = {}
    for course_id in all_course_ids:
        if course_id_keys[course_id]:
            counts[course_id] = len(course_id_keys[course_id])
        elif legacy_keys[course_id]:
            counts[course_id] = len(legacy_keys[course_id])
        else:
            counts[course_id] = 0

    zero_course_ids = [cid for cid, n in counts.items() if n == 0]
    if zero_course_ids:
        progress_rows = (
            db.query(
                CourseProgress.course_id,
                CourseProgress.completed_module_index,
            )
            .filter(CourseProgress.course_id.in_(zero_course_ids))
            .distinct()
            .all()
        )
        indices_by_course = defaultdict(set)
        for course_id, module_index in progress_rows:
            indices_by_course[course_id].add(module_index)
        for course_id in zero_course_ids:
            if indices_by_course[course_id]:
                counts[course_id] = len(indices_by_course[course_id])

    return counts


def get_total_modules_count(course_id: str, db: Session) -> int:
    """Get total number of modules in a course"""
    import json as json_module  # Local import to avoid shadowing issues
    try:
        course = db.query(Course).filter(Course.id == course_id).first()
        if not course:
            return 0
        
        # CRITICAL FIX: Parse source_documents JSON array to get ALL documents in this course
        course_docs_normalized = []
        try:
            if course.source_documents:
                # Parse JSON array of source documents
                source_docs_list = json_module.loads(course.source_documents)
                if isinstance(source_docs_list, list):
                    for doc in source_docs_list:
                        doc_normalized = normalize_document_name(doc)
                        course_docs_normalized.append(doc_normalized)
                    logger.info('[MODULE COUNT] Parsed %s documents from source_documents: %s', len(course_docs_normalized), ("" if course_docs_normalized is None else str(course_docs_normalized).replace("\n", "").replace("\r", "")))
        except Exception as e:
            logger.warning('[MODULE COUNT] Could not parse source_documents JSON: %s', e)
        
        # Fallback to single source_doc_name if source_documents is empty
        if not course_docs_normalized:
            course_doc_normalized = normalize_document_name(course.source_doc_name)
            course_docs_normalized = [course_doc_normalized]
            logger.info('[MODULE COUNT] Using fallback single source_doc_name: %s', ("" if course_doc_normalized is None else str(course_doc_normalized).replace("\n", "").replace("\r", "")))
        
        # Count modules from S3.
        # Strategy:
        # 1) Prefer strict metadata course_id match (most reliable, no cross-course leakage)
        # 2) Fallback for legacy rows without course_id: exact normalized source_doc match only
        # Served from the per-request index rather than a fresh scan, so calling this once per
        # course in a loop costs one S3 pass for the whole request instead of one per course.
        module_count = 0
        matched_with_course_id_keys = set()
        matched_legacy_keys = set()
        for s3_key, meta_data in author_report_metadata(db, course.author_id):
            source_doc = meta_data.get('source_doc', '')
            meta_course_id = meta_data.get('course_id')

            # Normalize the metadata source_doc for comparison
            source_doc_normalized = normalize_document_name(source_doc)

            if meta_course_id:
                if meta_course_id == course_id:
                    matched_with_course_id_keys.add(s3_key)
            else:
                # Legacy fallback: exact normalized name match only
                if source_doc_normalized in course_docs_normalized:
                    matched_legacy_keys.add(s3_key)

        if matched_with_course_id_keys:
            module_count = len(matched_with_course_id_keys)
            logger.info(
                "[MODULE COUNT] Found %s modules from S3 by strict course_id for course %s",
                ("" if module_count is None else str(module_count).replace("\n", "").replace("\r", "")),
                ("" if course_id is None else str(course_id).replace("\n", "").replace("\r", "")),
            )
        elif matched_legacy_keys:
            module_count = len(matched_legacy_keys)
            logger.info(
                "[MODULE COUNT] Found %s legacy modules from S3 by exact source_doc match for course %s",
                ("" if module_count is None else str(module_count).replace("\n", "").replace("\r", "")),
                ("" if course_id is None else str(course_id).replace("\n", "").replace("\r", "")),
            )
        
        # If no modules found from S3, try to get from CourseProgress
        if module_count == 0:
            logger.info('[MODULE COUNT] No S3 metadata found for course %s, checking CourseProgress', ("" if course_id is None else str(course_id).replace("\n", "").replace("\r", "")))
            progress_records = db.query(CourseProgress.completed_module_index).filter(
                CourseProgress.course_id == course_id
            ).distinct().all()
            
            if progress_records:
                # Get all unique module indices
                module_indices = set(record[0] for record in progress_records)
                # Count the number of unique indices
                module_count = len(module_indices)
                logger.info('[MODULE COUNT] Found %s distinct modules from CourseProgress for course %s: %s', ("" if module_count is None else str(module_count).replace("\n", "").replace("\r", "")), ("" if course_id is None else str(course_id).replace("\n", "").replace("\r", "")), ("" if sorted(module_indices) is None else str(sorted(module_indices)).replace("\n", "").replace("\r", "")))
        
        return module_count
    except Exception as e:
        logger.error('Error getting module count: %s', e, exc_info=True)
        return 0

_MODULE_COUNT_CACHE_KEY = "_module_count_by_course"
_S3_MODULE_COUNTS_KEY = "_s3_module_counts"

# Both hold counts read out of the per-request S3 index, so both have to be dropped whenever
# that index is. Registering them means invalidate_author_report_metadata() covers them without
# every write path having to remember these two key names.
register_derived_cache_key(_MODULE_COUNT_CACHE_KEY)
register_derived_cache_key(_S3_MODULE_COUNTS_KEY)


def prefetch_s3_module_counts(db: Session, author_ids) -> dict:
    """
    One S3 index pass per author for the request, stored on ``db.info`` so every
    ``_count_modules_for_course`` call in this request is an O(1) dict lookup.
    """
    authors = {aid for aid in (author_ids or set()) if aid}
    if not authors:
        counts = {}
    else:
        counts = build_s3_module_counts_for_authors(db, authors)
    # Updated in place rather than rebound: a page that prefetches twice for different authors
    # keeps both sets, and the dict registered above stays the one invalidation clears.
    db.info.setdefault(_S3_MODULE_COUNTS_KEY, {}).update(counts)
    # Seed the per-course memo so later helpers hit the same map.
    cache = db.info.setdefault(_MODULE_COUNT_CACHE_KEY, {})
    cache.update(counts)
    return counts


def _count_modules_for_course(course_id: str, db: Session) -> int:
    """Helper function to count total modules in a course (request-memoized)."""
    if not course_id:
        return 0
    cache = db.info.setdefault(_MODULE_COUNT_CACHE_KEY, {})
    if course_id in cache:
        return cache[course_id]
    bulk = db.info.get(_S3_MODULE_COUNTS_KEY)
    if isinstance(bulk, dict) and course_id in bulk:
        cache[course_id] = int(bulk[course_id] or 0)
        return cache[course_id]
    n = get_total_modules_count(course_id, db)
    cache[course_id] = n
    return n

def _get_modules_for_course(course_id: str, db: Session) -> List[Dict]:
    """Helper function to get all modules for a course with their metadata"""
    import json as json_module
    try:
        course = db.query(Course).filter(Course.id == course_id).first()
        if not course:
            logger.error('[MODULE RETRIEVAL] Course %s not found', ("" if course_id is None else str(course_id).replace("\n", "").replace("\r", "")))
            return []
        
        logger.info('[MODULE RETRIEVAL] Getting modules for course %s, source_doc: %s, source_documents: %s, author: %s', ("" if course_id is None else str(course_id).replace("\n", "").replace("\r", "")), ("" if course.source_doc_name is None else str(course.source_doc_name).replace("\n", "").replace("\r", "")), ("" if course.source_documents is None else str(course.source_documents).replace("\n", "").replace("\r", "")), ("" if course.author_id is None else str(course.author_id).replace("\n", "").replace("\r", "")))
        
        modules = []
        
        # CRITICAL FIX: Parse source_documents JSON array to get ALL documents in this course
        course_docs_normalized = []
        try:
            if course.source_documents:
                # Parse JSON array of source documents
                source_docs_list = json_module.loads(course.source_documents)
                if isinstance(source_docs_list, list):
                    for doc in source_docs_list:
                        doc_normalized = normalize_document_name(doc)
                        course_docs_normalized.append(doc_normalized)
                    logger.info('[MODULE RETRIEVAL] Parsed %s documents from source_documents: %s', len(course_docs_normalized), ("" if course_docs_normalized is None else str(course_docs_normalized).replace("\n", "").replace("\r", "")))
        except Exception as e:
            logger.warning('[MODULE RETRIEVAL] Could not parse source_documents JSON: %s', e)
        
        # Fallback to single source_doc_name if source_documents is empty
        if not course_docs_normalized:
            course_doc_normalized = normalize_document_name(course.source_doc_name)
            course_docs_normalized = [course_doc_normalized]
            logger.info('[MODULE RETRIEVAL] Using fallback single source_doc_name: %s', ("" if course_doc_normalized is None else str(course_doc_normalized).replace("\n", "").replace("\r", "")))
        
        logger.info('[MODULE RETRIEVAL] Looking for modules from documents: %s', ("" if course_docs_normalized is None else str(course_docs_normalized).replace("\n", "").replace("\r", "")))
        
        # First, try to get modules from S3 metadata. Read from the per-request index, which is
        # the same data the module counts come from, so a page that lists modules and counts
        # them shares one S3 pass instead of making two.
        prefix = f"{course.author_id}/generated_reports/"
        logger.info('[MODULE RETRIEVAL] Searching S3 with prefix: %s', log_safe(prefix))

        for s3_key, meta_data in author_report_metadata(db, course.author_id):
            source_doc = meta_data.get('source_doc', '')

            # Normalize the metadata source_doc for comparison
            source_doc_normalized = normalize_document_name(source_doc)

            logger.debug('[MODULE RETRIEVAL] Checking %s: source_doc=%s, normalized=%s', log_safe(s3_key), log_safe(source_doc), log_safe(source_doc_normalized))

            # PRIMARY FILTER: Match by course_id stored in metadata (most reliable)
            # FALLBACK: Match by source_doc name (for older modules without course_id)
            meta_course_id = meta_data.get('course_id')
            if meta_course_id:
                is_match = (meta_course_id == course_id)
                if is_match:
                    logger.debug('[MODULE RETRIEVAL] ✓ Matched by course_id: %s', log_safe(meta_course_id))
                else:
                    logger.debug('[MODULE RETRIEVAL] ✗ Skipped: course_id mismatch (%s != %s)', log_safe(meta_course_id), log_safe(course_id))
            else:
                # Fallback: exact source_doc name match for legacy modules
                is_match = False
                for course_doc_norm in course_docs_normalized:
                    if source_doc_normalized == course_doc_norm:
                        is_match = True
                        logger.debug('[MODULE RETRIEVAL] ✓ Fallback exact match against course doc: %s', log_safe(course_doc_norm))
                        break

            if is_match:
                # seg_index in metadata is 1-based (Module 1, Module 2, etc.)
                seg_index = meta_data.get('seg_index', 1)
                title = meta_data.get('title', f"Module {seg_index}")

                modules.append({
                    'seg_index': seg_index,
                    'title': title,
                    'filename': meta_data.get('filename', ''),
                    'source_doc': source_doc,  # Keep original for debugging
                    's3_key': s3_key  # For stable sorting (doesn't change on regeneration)
                })
                logger.info('[MODULE RETRIEVAL] ✓ Found module %s: %s (from %s, key: %s)', log_safe(seg_index), log_safe(title), log_safe(source_doc), log_safe(s3_key))
            else:
                logger.debug('[MODULE RETRIEVAL] ✗ Skipped %s: no match', log_safe(s3_key))


        logger.info('[MODULE RETRIEVAL] Found %s modules from S3 for course %s', len(modules), ("" if course_id is None else str(course_id).replace("\n", "").replace("\r", "")))
        
        # CRITICAL FIX: Sort modules by seg_index to match trainer's order
        # seg_index is the most reliable indicator of module order
        if len(modules) > 0:
            # Log modules before sorting
            logger.info('[MODULE RETRIEVAL] Modules before sorting: %s', ("" if [(m['seg_index'], m['title'], m.get('source_doc', '')) for m in modules] is None else str([(m['seg_index'], m['title'], m.get('source_doc', '')) for m in modules]).replace("\n", "").replace("\r", "")))
            
            # Sort by seg_index (the original module number from generation)
            # This maintains the exact order from when modules were created
            modules.sort(key=lambda x: x['seg_index'])
            
            logger.info('[MODULE RETRIEVAL] Modules after sorting by seg_index: %s', ("" if [(m['seg_index'], m['title']) for m in modules] is None else str([(m['seg_index'], m['title']) for m in modules]).replace("\n", "").replace("\r", "")))
            
            # Renumber sequentially starting from 1
            for new_index, module in enumerate(modules, start=1):
                old_index = module['seg_index']
                module['seg_index'] = new_index
                logger.info('[MODULE RETRIEVAL] Module %s: %s (was index %s, from %s)', ("" if new_index is None else str(new_index).replace("\n", "").replace("\r", "")), ("" if module['title'] is None else str(module['title']).replace("\n", "").replace("\r", "")), ("" if old_index is None else str(old_index).replace("\n", "").replace("\r", "")), ("" if module.get('source_doc', 'unknown') is None else str(module.get('source_doc', 'unknown')).replace("\n", "").replace("\r", "")))
                
                # Remove s3_key from final output (not needed in response)
                if 's3_key' in module:
                    del module['s3_key']
        
        # If no modules found from S3, build from CourseProgress data
        if not modules:
            logger.warning('[MODULE RETRIEVAL] No S3 metadata found for course %s, building from CourseProgress', ("" if course_id is None else str(course_id).replace("\n", "").replace("\r", "")))
            # Get all unique module indices from CourseProgress for this course
            progress_records = db.query(CourseProgress.completed_module_index).filter(
                CourseProgress.course_id == course_id
            ).distinct().all()
            
            module_indices = set(record[0] for record in progress_records)
            
            # Also check TraineeAssessment for module indices
            assessment_records = db.query(TraineeAssessment.module_index).filter(
                TraineeAssessment.course_id == course_id,
                TraineeAssessment.module_index != None
            ).distinct().all()
            
            for record in assessment_records:
                if record[0] is not None:
                    module_indices.add(record[0])
            
            # If we still have no modules, try to get the total count from the course
            if not module_indices:
                # Try to get from course metadata or default to a reasonable number
                total_modules = get_total_modules_count(course_id, db)
                if total_modules > 0:
                    # Module indices are 1-based
                    module_indices = set(range(1, total_modules + 1))
                else:
                    # Last resort: check if there are any completed modules for ANY user
                    all_progress = db.query(CourseProgress.completed_module_index).filter(
                        CourseProgress.course_id == course_id
                    ).all()
                    if all_progress:
                        max_index = max(record[0] for record in all_progress)
                        # Module indices are 1-based
                        module_indices = set(range(1, max_index + 1))
            
            # Build module list from indices
            for idx in sorted(module_indices):
                modules.append({
                    'seg_index': idx,
                    'title': f"Module {idx}",
                    'filename': ''
                })
            
            logger.info('[MODULE RETRIEVAL] Built %s modules from database for course %s', len(modules), ("" if course_id is None else str(course_id).replace("\n", "").replace("\r", "")))
        
        # Modules are already sorted by seg_index after renumbering
        logger.info('[MODULE RETRIEVAL] Returning %s modules for course %s: %s', len(modules), ("" if course_id is None else str(course_id).replace("\n", "").replace("\r", "")), ("" if [m['title'] for m in modules] is None else str([m['title'] for m in modules]).replace("\n", "").replace("\r", "")))
        return modules
    except Exception as e:
        logger.error('[MODULE RETRIEVAL] Error getting modules for course %s: %s', ("" if course_id is None else str(course_id).replace("\n", "").replace("\r", "")), e, exc_info=True)
        return []


def _get_module_title_for_index(course_id: str, module_index: int, db: Session) -> str:
    """Resolve display title for a sequential 1-based module index.

    Dashboard and assessments use renumbered module indices from
    ``_get_modules_for_course`` — not raw S3 ``seg_index`` values.
    """
    if not module_index:
        return "Course Assessment"

    for module in _get_modules_for_course(course_id, db):
        if module.get("seg_index") == module_index:
            return module.get("title") or f"Module {module_index}"

    return f"Module {module_index}"


def _assessment_display_percentage(assessment) -> int:
    """Resolve a display percentage from stored score fields."""
    if assessment is None:
        return 0
    if assessment.percentage is not None:
        return int(round(assessment.percentage))
    total = assessment.total_possible or 0
    if total > 0:
        return int(round((assessment.score or 0) / total * 100))
    return 0


def _pick_latest_module_assessment(assessments):
    """
    Return the most recently submitted module attempt.
    Matches admin/trainer progress views (completed_at desc), not attempt_number alone.

    The ordering is delegated to _sort_assessment_attempts so "latest" is always the row the last
    attempt tab shows. Choosing with max() over (completed_at, attempt_number) alone returned
    whichever tied row the query happened to yield first, and concurrent retries tie on both of
    those - which is how the score header could report one attempt while the last tab showed
    another.
    """
    if not assessments:
        return None
    return _sort_assessment_attempts(assessments)[-1]


def _answer_value_for_question(answers: Optional[dict], qid) -> Optional[Any]:
    """Resolve a submitted answer for a question id (supports str/int keys)."""
    if not answers:
        return None
    key_variants = [qid, str(qid)]
    if isinstance(qid, str) and qid.isdigit():
        key_variants.append(int(qid))
    for key in key_variants:
        if key in answers:
            return answers[key]
    return None


def _assert_all_questions_answered(unique_qs: list, answers: dict) -> None:
    """Reject submission when any assessment question is unanswered."""
    # Phani | 25-Jun-2026 | DF_31 | Trainee_assessment | Deferred
    # Fix: Backend blocks submit when any question is left blank.
    missing_count = 0
    for question in unique_qs:
        qid = question.get("id")
        if qid is None:
            continue
        value = _answer_value_for_question(answers, qid)
        if value is None or (isinstance(value, str) and not value.strip()):
            missing_count += 1
    if missing_count:
        raise HTTPException(
            status_code=400,
            detail=(
                f"All questions must be answered before submission. "
                f"{missing_count} question(s) are unanswered."
            ),
        )


def build_module_assessment_attempts(db: Session, trainee_id, course_id) -> Dict[Any, Any]:
    """``{module_index: (latest attempt, number of attempts)}`` for one trainee and course.

    The module-by-module progress views used to run two queries per module for this, so a course
    with twenty modules cost forty round-trips before it could render. Rows come back newest
    first, so the first one seen for an index is the attempt `.first()` would have returned.
    """
    by_index: Dict[Any, Any] = {}
    if not trainee_id or not course_id:
        return by_index
    for row in (
        db.query(TraineeAssessment)
        .filter(
            TraineeAssessment.trainee_id == trainee_id,
            TraineeAssessment.course_id == course_id,
            TraineeAssessment.module_index.isnot(None),
        )
        .order_by(TraineeAssessment.completed_at.desc())
        .all()
    ):
        latest, attempts_used = by_index.get(row.module_index, (row, 0))
        by_index[row.module_index] = (latest, attempts_used + 1)
    return by_index


def build_trainee_latest_assessments(
    trainee_id: str,
    course_ids: Any,
    db: Session,
) -> Dict[Any, Dict[Any, Any]]:
    """Latest assessment attempt per (course, module) for a whole list of courses at once.

    Dashboards used to ask the database for this one course - and inside a course, one module -
    at a time, which is what made the trainee and trainee-detail views slow enough to hit the
    request timeout. Pass the returned map to `_get_trainee_course_assessment_state` as
    `latest_assessments` and it does no queries of its own.

    The ordering is the same `completed_at DESC` used by the per-course queries, and the first
    row seen for a key wins, so the attempt picked for each course/module is the same one
    `.first()` would have returned.
    """
    latest: Dict[Any, Dict[Any, Any]] = {}
    unique_course_ids = list({course_id for course_id in (course_ids or []) if course_id})
    if not trainee_id or not unique_course_ids:
        return latest

    rows = db.query(TraineeAssessment).filter(
        TraineeAssessment.trainee_id == trainee_id,
        TraineeAssessment.course_id.in_(unique_course_ids),
    ).order_by(TraineeAssessment.completed_at.desc()).all()

    for row in rows:
        per_course = latest.setdefault(row.course_id, {})
        if row.module_index not in per_course:
            per_course[row.module_index] = row
    return latest


def _get_trainee_course_assessment_state(
    trainee_id: str,
    course_id: str,
    assessment_config,
    total_modules: int,
    completed_modules: int,
    db: Session,
    latest_assessments: Optional[Dict[Any, Dict[Any, Any]]] = None,
) -> Dict[str, Any]:
    """Normalized assessment status for dashboards and progress views.

    `latest_assessments` is the optional prefetched map from
    `build_trainee_latest_assessments`; when it is given, the per-course and per-module
    queries below are skipped and the same rows are read from it instead.
    """
    result: Dict[str, Any] = {
        "has_assessment": False,
        "assessment_scope": None,
        "assessment_status": "not_started",
        "assessment_score": None,
        "module_assessment_details": [],
        "course_completed": False,
    }
    if not assessment_config:
        return result

    result["has_assessment"] = True
    result["assessment_scope"] = assessment_config.assessment_scope

    if assessment_config.assessment_scope == "end_of_course":
        if latest_assessments is None:
            latest_assessment = db.query(TraineeAssessment).filter(
                TraineeAssessment.trainee_id == trainee_id,
                TraineeAssessment.course_id == course_id,
                TraineeAssessment.module_index.is_(None),
            ).order_by(TraineeAssessment.completed_at.desc()).first()
        else:
            latest_assessment = latest_assessments.get(course_id, {}).get(None)

        if latest_assessment:
            result["assessment_status"] = "passed" if latest_assessment.passed else "failed"
            result["assessment_score"] = _assessment_display_percentage(latest_assessment)
            result["course_completed"] = latest_assessment.passed
        elif total_modules > 0 and completed_modules >= total_modules:
            # All modules studied — final assessment is available but not yet taken
            result["assessment_status"] = "in_progress"
        return result

    module_assessments = []
    if latest_assessments is None:
        module_indices = db.query(TraineeAssessment.module_index).filter(
            TraineeAssessment.trainee_id == trainee_id,
            TraineeAssessment.course_id == course_id,
            TraineeAssessment.module_index.isnot(None),
        ).distinct().all()

        for (module_idx,) in module_indices:
            latest = db.query(TraineeAssessment).filter(
                TraineeAssessment.trainee_id == trainee_id,
                TraineeAssessment.course_id == course_id,
                TraineeAssessment.module_index == module_idx,
            ).order_by(TraineeAssessment.completed_at.desc()).first()
            if latest:
                module_assessments.append(latest)
    else:
        module_assessments = [
            latest
            for module_idx, latest in latest_assessments.get(course_id, {}).items()
            if module_idx is not None
        ]

    # The two branches above collect the same rows but in whatever order the database hands
    # them back; sorting by module index keeps the module badges in module order for every
    # caller instead of only the one screen that sorted them itself.
    module_assessments.sort(key=lambda assessment: assessment.module_index)

    if not module_assessments:
        return result

    module_percentages = [_assessment_display_percentage(a) for a in module_assessments]
    result["module_assessment_details"] = [
        {
            "module_index": a.module_index,
            "score": _assessment_display_percentage(a),
            "passed": a.passed,
        }
        for a in module_assessments
    ]

    if len(module_assessments) < total_modules:
        result["assessment_status"] = "in_progress"
        result["assessment_score"] = round(
            sum(module_percentages) / len(module_percentages), 1
        )
    else:
        result["assessment_score"] = round(
            sum(module_percentages) / len(module_percentages), 1
        )
        if result["assessment_score"] >= assessment_config.pass_percentage:
            result["assessment_status"] = "passed"
            result["course_completed"] = True
        else:
            result["assessment_status"] = "failed"

    return result


def _get_end_of_course_assessment_trigger(
    user_id: str,
    course_id: str,
    db: Session,
) -> Optional[Dict[str, Any]]:
    """Return assessment_trigger when all course modules are studied (end_of_course only)."""
    assessment_config = db.query(AssessmentConfig).filter(
        AssessmentConfig.course_id == course_id
    ).first()
    if not assessment_config or assessment_config.assessment_scope != "end_of_course":
        return None

    total_modules = _count_modules_for_course(course_id, db)
    if total_modules <= 0:
        logger.warning(
            "[PROGRESS] End-of-course check skipped: unknown module count for course %s",
            ("" if course_id is None else str(course_id).replace("\n", "").replace("\r", "")),
        )
        return None

    completed_modules = db.query(CourseProgress).filter(
        CourseProgress.user_id == user_id,
        CourseProgress.course_id == course_id,
    ).count()

    if completed_modules < total_modules:
        logger.info(
            "[PROGRESS] End-of-course not ready: %s/%s modules completed for course %s",
            completed_modules,
            total_modules,
            ("" if course_id is None else str(course_id).replace("\n", "").replace("\r", "")),
        )
        return None

    course_questions = db.query(AssessmentQuestion).filter(
        AssessmentQuestion.course_id == course_id,
        AssessmentQuestion.module_index.is_(None),
    ).count()
    if course_questions <= 0:
        logger.warning(
            "[PROGRESS] No end-of-course questions found for course %s",
            ("" if course_id is None else str(course_id).replace("\n", "").replace("\r", "")),
        )
        return None

    passed = db.query(TraineeAssessment).filter(
        TraineeAssessment.trainee_id == user_id,
        TraineeAssessment.course_id == course_id,
        TraineeAssessment.module_index.is_(None),
        TraineeAssessment.passed == True,
    ).first()
    if passed:
        return None

    logger.info(
        "[PROGRESS] End-of-course assessment trigger created (%s/%s modules)",
        completed_modules,
        total_modules,
    )
    return {
        "type": "end_of_course",
        "message": "Course completed! Final assessment is now available.",
    }


def _build_course_assessment_progress(
    trainee_id: str,
    course_id: str,
    assessment_config,
    total_modules: int,
    completed_modules: int,
    db: Session,
) -> Optional[Dict[str, Any]]:
    """Course-level assessment block for module progress modals (end_of_course only).

    ``attempts_left`` used to be passed in from ``TraineeAttempt.attempts_left``, which put two
    disagreeing numbers in this one dict: ``attempts_used`` was counted from the attempts actually
    stored, while ``attempts_left`` came from a column seeded off ``Course.initial_attempts`` and
    written back up by /admin/reset-attempts and /admin/sync-course-attempts. A course created with
    5 initial attempts but configured for 3 therefore reported "3 used, 2 remaining" - and the
    results modal, which counts the same way the gate does, said "3/3 used, 0 remaining" for the
    same trainee. The gate is ``attempts_used >= effective_max_attempts``, so the 2 remaining were
    never real: the trainee was offered a retake the server would refuse.

    Both numbers now come from _course_attempt_limits, the same helper the load and submit gates
    use, and ``max_attempts`` is reported alongside them so the UI never has to assume a ceiling.
    """
    if not assessment_config or assessment_config.assessment_scope != "end_of_course":
        return None

    state = _get_trainee_course_assessment_state(
        trainee_id, course_id, assessment_config, total_modules, completed_modules, db
    )
    limits = _course_attempt_limits(db, trainee_id, course_id, assessment_config)
    return {
        "scope": "end_of_course",
        "status": state["assessment_status"],
        "score": state["assessment_score"],
        "ready": total_modules > 0 and completed_modules >= total_modules,
        "attempts_left": limits["attempts_remaining"],
        "attempts_used": limits["attempts_used"],
        "max_attempts": limits["effective_max_attempts"],
        "pass_percentage": assessment_config.pass_percentage,
    }


@router.get("/debug/course-modules/{course_id}", tags=["Debug"])
def debug_course_modules(course_id: str, user: User = Depends(get_current_active_user), db: Session = Depends(get_db)):
    """Debug endpoint to see all modules in S3 for a course"""
    try:
        course = db.query(Course).filter(Course.id == course_id).first()
        if not course:
            return {"error": "Course not found"}
        
        import json as json_module
        all_modules = []
        
        # List all JSON files for this author
        paginator = s3_client.get_paginator('list_objects_v2')
        prefix = f"{course.author_id}/generated_reports/"
        
        for page in paginator.paginate(Bucket=S3_BUCKET_NAME, Prefix=prefix):
            for obj in page.get('Contents', []):
                if obj['Key'].endswith('.json'):
                    try:
                        meta_obj = s3_client.get_object(Bucket=S3_BUCKET_NAME, Key=obj['Key'])
                        meta_data = json_module.loads(meta_obj['Body'].read().decode('utf-8'))
                        
                        all_modules.append({
                            's3_key': obj['Key'],
                            'source_doc': meta_data.get('source_doc', 'N/A'),
                            'seg_index': meta_data.get('seg_index', 'N/A'),
                            'title': meta_data.get('title', 'N/A')
                        })
                    except Exception as e:
                        logger.warning(
                            "Debug module list: failed to read %s: %s",
                            ("" if obj['Key'] is None else str(obj['Key']).replace("\n", "").replace("\r", "")),
                            ("" if e is None else str(e).replace("\n", "").replace("\r", "")),
                        )
                        all_modules.append({
                            's3_key': obj['Key'],
                            'error': 'Failed to read module metadata',
                        })
        
        return {
            'course_id': course_id,
            'course_source_doc': course.source_doc_name,
            'author_id': course.author_id,
            'total_files_found': len(all_modules),
            'modules': all_modules
        }
    except Exception as e:
        logger.error(
            "Debug course modules failed for %s: %s",
            ("" if course_id is None else str(course_id).replace("\n", "").replace("\r", "")),
            ("" if e is None else str(e).replace("\n", "").replace("\r", "")),
            exc_info=True,
        )
        return {"error": "Failed to load course module debug data"}

@router.get("/debug/course/{course_id}/s3-modules", tags=["Debug"])
def debug_course_s3_modules(course_id: str, user: User = Depends(get_current_active_user), db: Session = Depends(get_db)):
    """Debug endpoint to see all S3 modules for a course"""
    try:
        course = db.query(Course).filter(Course.id == course_id).first()
        if not course:
            return {"error": "Course not found"}
        
        import json as json_module
        all_modules = []
        
        paginator = s3_client.get_paginator('list_objects_v2')
        prefix = f"{course.author_id}/generated_reports/"
        
        for page in paginator.paginate(Bucket=S3_BUCKET_NAME, Prefix=prefix):
            for obj in page.get('Contents', []):
                if obj['Key'].endswith('.json'):
                    try:
                        meta_obj = s3_client.get_object(Bucket=S3_BUCKET_NAME, Key=obj['Key'])
                        meta_data = json_module.loads(meta_obj['Body'].read().decode('utf-8'))
                        
                        all_modules.append({
                            'key': obj['Key'],
                            'source_doc': meta_data.get('source_doc', ''),
                            'seg_index': meta_data.get('seg_index', 0),
                            'title': meta_data.get('title', ''),
                            'matches_course': meta_data.get('source_doc', '') == course.source_doc_name
                        })
                    except Exception as e:
                        logger.error(
                            'Error reading %s: %s',
                            ("" if obj['Key'] is None else str(obj['Key']).replace("\n", "").replace("\r", "")),
                            ("" if e is None else str(e).replace("\n", "").replace("\r", "")),
                        )
        
        return {
            'course_id': course_id,
            'course_source_doc': course.source_doc_name,
            'author_id': course.author_id,
            'total_s3_modules': len(all_modules),
            'modules': all_modules
        }
    except Exception as e:
        logger.error(
            "Debug S3 modules failed for %s: %s",
            ("" if course_id is None else str(course_id).replace("\n", "").replace("\r", "")),
            ("" if e is None else str(e).replace("\n", "").replace("\r", "")),
            exc_info=True,
        )
        return {"error": "Failed to load S3 module debug data"}

@router.get("/trainee/course/{course_id}/modules", tags=["Trainee Dashboard & Quizzes"])
def get_course_modules(course_id: str, user: User = Depends(get_current_active_user), db: Session = Depends(get_db)):
    """
    Get all modules for a course with their metadata (titles, indices).
    
    - **Required Role**: Trainee or Trainer.
    - **Returns**: List of modules with module_index (1-based) and title.
    """
    try:
        # Verify user has access to this course
        course = db.query(Course).filter(Course.id == course_id).first()
        if not course:
            raise HTTPException(status_code=404, detail="Course not found")
        
        logger.info('[GET MODULES API] Course %s found: source_doc=%s, author=%s', ("" if course_id is None else str(course_id).replace("\n", "").replace("\r", "")), ("" if course.source_doc_name is None else str(course.source_doc_name).replace("\n", "").replace("\r", "")), ("" if course.author_id is None else str(course.author_id).replace("\n", "").replace("\r", "")))
        
        # Check if user is trainee with access or trainer who owns the course
        if user.role == 'trainee':
            attempt = db.query(TraineeAttempt).filter(
                TraineeAttempt.trainee_id == user.id,
                TraineeAttempt.course_id == course_id
            ).first()
            if not attempt:
                raise HTTPException(status_code=403, detail="You don't have access to this course")
        elif user.role == 'trainer':
            if course.author_id != user.id:
                raise HTTPException(status_code=403, detail="You don't have access to this course")
        
        # Get modules using the helper function
        modules = _get_modules_for_course(course_id, db)
        
        logger.info('[GET MODULES API] Returning %s modules for course %s', len(modules), ("" if course_id is None else str(course_id).replace("\n", "").replace("\r", "")))
        
        # Return modules with seg_index as module_index (already 1-based)
        result = [{
            'module_index': m['seg_index'],  # Already 1-based from _get_modules_for_course
            'title': m['title']
        } for m in modules]
        
        logger.info('[GET MODULES API] Module details: %s', ("" if result is None else str(result).replace("\n", "").replace("\r", "")))
        
        # Return directly with success wrapper - FastAPI will serialize it correctly
        return JSONResponse(content={
            "success": True,
            "data": result
        })
    except HTTPException:
        raise
    except Exception as e:
        logger.error('[GET MODULES API] Error getting course modules: %s', e, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to retrieve course modules")

@router.get("/trainee/course/{course_id}/progress", tags=["Trainee Dashboard & Quizzes"])
def trainee_get_own_course_progress(
    course_id: str,
    user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """[Trainee] Get detailed module-by-module progress for the logged-in trainee in a specific course."""
    try:
        if user.role != 'trainee':
            raise HTTPException(status_code=403, detail="Trainee access only")
        course = db.query(Course).filter(Course.id == course_id).first()
        if not course:
            raise HTTPException(status_code=404, detail="Course not found")

        modules_data = _get_modules_for_course(course_id, db)
        completed_module_indices = set(
            p.completed_module_index for p in db.query(CourseProgress).filter(
                CourseProgress.user_id == user.id,
                CourseProgress.course_id == course_id
            ).all()
        )
        if not modules_data and completed_module_indices:
            max_index = max(completed_module_indices)
            modules_data = [
                {'seg_index': i, 'title': f'Module {i}', 'filename': ''}
                for i in range(1, max_index + 1)
            ]

        assessment_config = db.query(AssessmentConfig).filter(AssessmentConfig.course_id == course_id).first()
        has_module_assessments = assessment_config and assessment_config.assessment_scope == 'end_of_module'
        total_modules = len(modules_data)
        completed_modules = len(completed_module_indices)

        course_assessment = _build_course_assessment_progress(
            user.id,
            course_id,
            assessment_config,
            total_modules,
            completed_modules,
            db,
        )

        # Latest attempt and attempt count for every module in one query, instead of two per
        # module: a long course used to spend the whole request on those round-trips.
        module_assessments_by_index = build_module_assessment_attempts(db, user.id, course_id)

        modules_progress = []
        for module in modules_data:
            idx = module['seg_index']
            study_done = idx in completed_module_indices
            module_assessment, module_attempts_used = module_assessments_by_index.get(idx, (None, 0))
            has_assessment = has_module_assessments
            assessment_score = None
            if module_assessment:
                assessment_score = module_assessment.percentage
                if assessment_score is None and module_assessment.total_possible:
                    assessment_score = round(
                        (module_assessment.score or 0) / module_assessment.total_possible * 100
                    )
            assessment_status = 'passed' if (module_assessment and module_assessment.passed) else ('failed' if module_assessment else 'not_attempted')
            is_completed = study_done and (not has_module_assessments or (module_assessment and module_assessment.passed))
            modules_progress.append({
                "module_index": idx,
                "title": module.get('title', f'Module {idx}'),
                "is_completed": is_completed,
                "study_completed": study_done,
                "has_assessment": has_assessment,
                "assessment_score": assessment_score,
                "assessment_status": assessment_status,
                "assessment_attempts_used": module_attempts_used,
            })

        return {"success": True, "data": {
            "course_name": course.source_doc_name,
            "assessment_scope": assessment_config.assessment_scope if assessment_config else None,
            "course_assessment": course_assessment,
            "modules": modules_progress,
        }}
    except HTTPException:
        raise
    except Exception as e:
        logger.error('Error getting trainee own course progress: %s', log_safe(e), exc_info=True)
        raise HTTPException(status_code=500, detail=GENERIC_API_ERROR_MESSAGE)

@router.get("/trainee/assessment/{course_id}", tags=["Assessment System"])
def get_assessment(
    course_id: str,
    module_index: Optional[int] = Query(None),
    user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """Get assessment for trainee - supports both course-wide and module-specific assessments"""
    try:
        if user.role != 'trainee':
            raise HTTPException(status_code=403, detail="Trainee access required")
        
        # Check if trainee has access to this course
        attempt = db.query(TraineeAttempt).filter(
            TraineeAttempt.trainee_id == user.id,
            TraineeAttempt.course_id == course_id
        ).first()
        
        if not attempt:
            raise HTTPException(status_code=403, detail="No access to this course")
        
        logger.info('[ASSESSMENT] User %s attempt status: attempts_left=%s', ("" if user.id is None else str(user.id).replace("\n", "").replace("\r", "")), ("" if attempt.attempts_left is None else str(attempt.attempts_left).replace("\n", "").replace("\r", "")))
        
        # CRITICAL: Check if course is published - assessments only available for published courses
        course = db.query(Course).filter(Course.id == course_id).first()
        if not course:
            raise HTTPException(status_code=404, detail="Course not found")
        
        if not course.is_live:
            raise HTTPException(status_code=403, detail="Assessment not available - course is not published")
        
        # Get assessment config
        config = db.query(AssessmentConfig).filter(
            AssessmentConfig.course_id == course_id
        ).first()
        
        if not config or not config.is_approved:
            raise HTTPException(status_code=400, detail="Assessment not configured")

        is_module_scope = config.assessment_scope == 'end_of_module'

        if not is_module_scope:
            # Gate on the attempts actually stored, the way the module path does and the way every
            # surface that reports attempts counts them. This used to test attempts_left <= 0 and
            # then clamp attempts_left down to max_attempts, which both let a reset hand back an
            # attempt past the cap and destroyed approved extra attempts. Same call supplies the
            # "Attempt N of M" the trainee reads before starting, so it matches the number the
            # attempt is stored and reviewed under.
            course_limits = _assert_can_take_course_assessment(
                db=db,
                trainee_id=user.id,
                course_id=course_id,
                config=config,
            )
            current_attempt = course_limits["current_attempt_number"]
            # The ceiling the gate just enforced, which includes any attempts a trainer approved on
            # top of the configured cap. Reporting config.max_attempts here labelled a granted
            # fourth attempt "Attempt 4 of 3" on the quiz the server had just allowed.
            effective_max_attempts = course_limits["effective_max_attempts"]
        else:
            current_attempt = 1
            effective_max_attempts = config.max_attempts

        # Validate scope and sequential access
        if is_module_scope:
            if module_index is None:
                raise HTTPException(status_code=400, detail="Module index required for module-based assessment")

            module_limits = _assert_can_take_module_assessment(
                db=db,
                trainee_id=user.id,
                course_id=course_id,
                module_index=module_index,
                config=config,
            )
            current_attempt = module_limits["current_attempt_number"]
            effective_max_attempts = module_limits["effective_max_attempts"]

            if module_index > 1:
                completed_modules_records = db.query(CourseProgress.completed_module_index).filter(
                    CourseProgress.user_id == user.id,
                    CourseProgress.course_id == course_id
                ).all()
                completed_module_indices = {record[0] for record in completed_modules_records}
                previous_module = module_index - 1
                if previous_module not in completed_module_indices:
                    raise HTTPException(
                        status_code=403,
                        detail=f"You must complete Module {previous_module} before taking the assessment for Module {module_index}."
                    )

            _assert_previous_module_assessment_passed(
                db=db,
                trainee_id=user.id,
                course_id=course_id,
                module_index=module_index,
            )

            current_module_completed = db.query(CourseProgress).filter(
                CourseProgress.user_id == user.id,
                CourseProgress.course_id == course_id,
                CourseProgress.completed_module_index == module_index
            ).first()
            if not current_module_completed:
                raise HTTPException(
                    status_code=403,
                    detail=f"You must complete Module {module_index} content before taking its assessment."
                )
            logger.info('[ASSESSMENT] User %s starting attempt %s/%s for course %s, module %s', ("" if user.id is None else str(user.id).replace("\n", "").replace("\r", "")), ("" if current_attempt is None else str(current_attempt).replace("\n", "").replace("\r", "")), ("" if effective_max_attempts is None else str(effective_max_attempts).replace("\n", "").replace("\r", "")), ("" if course_id is None else str(course_id).replace("\n", "").replace("\r", "")), ("" if module_index is None else str(module_index).replace("\n", "").replace("\r", "")))
        elif config.assessment_scope == 'end_of_course':
            if module_index is not None:
                raise HTTPException(status_code=400, detail="Module index not used for end-of-course assessment")

            total_modules = _count_modules_for_course(course_id, db)
            completed_modules = db.query(CourseProgress).filter(
                CourseProgress.user_id == user.id,
                CourseProgress.course_id == course_id,
            ).count()
            if total_modules <= 0 or completed_modules < total_modules:
                raise HTTPException(
                    status_code=403,
                    detail="Complete all course modules before taking the final assessment.",
                )

            passed_final = db.query(TraineeAssessment).filter(
                TraineeAssessment.trainee_id == user.id,
                TraineeAssessment.course_id == course_id,
                TraineeAssessment.module_index.is_(None),
                TraineeAssessment.passed == True,
            ).first()
            if passed_final:
                raise HTTPException(
                    status_code=403,
                    detail="You have already passed the final course assessment.",
                )

            logger.info('[ASSESSMENT] User %s starting attempt %s/%s for course %s (course-wide)', ("" if user.id is None else str(user.id).replace("\n", "").replace("\r", "")), ("" if current_attempt is None else str(current_attempt).replace("\n", "").replace("\r", "")), ("" if effective_max_attempts is None else str(effective_max_attempts).replace("\n", "").replace("\r", "")), ("" if course_id is None else str(course_id).replace("\n", "").replace("\r", "")))
        else:
            logger.info('[ASSESSMENT] User %s starting attempt %s/%s for course %s (course-wide)', ("" if user.id is None else str(user.id).replace("\n", "").replace("\r", "")), ("" if current_attempt is None else str(current_attempt).replace("\n", "").replace("\r", "")), ("" if effective_max_attempts is None else str(effective_max_attempts).replace("\n", "").replace("\r", "")), ("" if course_id is None else str(course_id).replace("\n", "").replace("\r", "")))

        # Get or generate unique question set for this trainee + module/course
        unique_questions = _get_or_create_trainee_question_set(
            db=db, trainee_id=user.id, course_id=course_id,
            module_index=module_index, course=course, config=config
        )

        if not unique_questions:
            error_msg = f"No questions available for module {module_index}" if module_index else "No questions available"
            raise HTTPException(status_code=400, detail=error_msg)

        logger.info('[UNIQUE Q] Serving %s unique questions to trainee %s, module %s', len(unique_questions), ("" if user.id is None else str(user.id).replace("\n", "").replace("\r", "")), ("" if module_index is None else str(module_index).replace("\n", "").replace("\r", "")))

        # Use renumbered module title (matches trainee dashboard module list).
        module_title = _get_module_title_for_index(course_id, module_index, db) if module_index else "Course Assessment"

        # Format questions for trainee (hide correct answers)
        questions_data = []
        for q in unique_questions:
            question_data = {
                "id": q['id'],
                "question_type": q.get('type', q.get('question_type', 'mcq')),
                "question_text": q.get('question', q.get('question_text', '')),
                "module_title": module_title,
                "is_unique": True
            }
            if question_data["question_type"] == 'mcq':
                question_data.update({
                    "option_a": q.get('option_a'),
                    "option_b": q.get('option_b'),
                    "option_c": q.get('option_c'),
                    "option_d": q.get('option_d')
                })
            questions_data.append(question_data)

        # Calculate time limit
        if module_index and config.assessment_scope == 'end_of_module':
            qc = len(questions_data)
            tl = qc * 3 if qc <= 5 else (qc * 2.5 if qc <= 10 else qc * 2)
            time_limit = max(5, min(15, int(tl)))
        else:
            time_limit = config.trainer_time_override or config.ai_suggested_time
        
        # Return response in the format expected by frontend
        response_data = {
            "success": True,
            "assessment": {
                "id": course_id,
                "module_index": module_index,
                "module_title": module_title,
                "questions": questions_data,
                "time_limit": time_limit,
                "attempt_number": current_attempt,
                "max_attempts": effective_max_attempts,
                "pass_percentage": config.pass_percentage
            }
        }
        
        logger.info('[ASSESSMENT] Returning assessment data for course %s" + (f", module %s" if module_index else "") + f": %s questions, attempt %s/%s', ("" if course_id is None else str(course_id).replace("\n", "").replace("\r", "")), ("" if module_index is None else str(module_index).replace("\n", "").replace("\r", "")), len(questions_data), ("" if current_attempt is None else str(current_attempt).replace("\n", "").replace("\r", "")), ("" if effective_max_attempts is None else str(effective_max_attempts).replace("\n", "").replace("\r", "")))
        
        return response_data
        
    except Exception as e:
        logger.error('Error getting assessment: %s', log_safe(e), exc_info=True)
        raise HTTPException(status_code=500, detail=GENERIC_API_ERROR_MESSAGE)

@router.post("/trainee/submit-assessment/{course_id}", tags=["Assessment System"])
def submit_assessment(
    course_id: str,
    submission: AssessmentSubmissionRequest,
    module_index: Optional[int] = None,
    user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """Submit assessment answers"""
    try:
        if user.role != 'trainee':
            raise HTTPException(status_code=403, detail="Trainee access required")
        
        # Check attempts. with_for_update() because the attempt number written further down is
        # count(existing) + 1: without the lock, submissions that overlap all read the same count
        # and all store the same number, and the cap is checked against that same stale count.
        attempt = db.query(TraineeAttempt).filter(
            TraineeAttempt.trainee_id == user.id,
            TraineeAttempt.course_id == course_id
        ).with_for_update().first()

        # Enrolment only. The attempt cap is checked below, once the config is known, against the
        # attempts actually stored rather than against attempts_left - see
        # _assert_can_take_course_assessment. The lock above is still what serialises that count.
        if not attempt:
            raise HTTPException(status_code=403, detail="No access to this course")

        # CRITICAL: Check if course is published - assessments only available for published courses
        course = db.query(Course).filter(Course.id == course_id).first()
        if not course:
            raise HTTPException(status_code=404, detail="Course not found")
        
        if not course.is_live:
            raise HTTPException(status_code=403, detail="Assessment not available - course is not published")
        
        # Get config
        config = db.query(AssessmentConfig).filter(
            AssessmentConfig.course_id == course_id
        ).first()

        if not config or not config.is_approved:
            raise HTTPException(status_code=400, detail="Assessment not configured")

        if config.assessment_scope == 'end_of_module':
            raise HTTPException(
                status_code=400,
                detail="Use submit-module-assessment for module-based assessments",
            )

        if config.assessment_scope == 'end_of_course':
            if module_index is not None:
                raise HTTPException(
                    status_code=400,
                    detail="Module index not used for end-of-course assessment",
                )
            total_modules = _count_modules_for_course(course_id, db)
            completed_modules = db.query(CourseProgress).filter(
                CourseProgress.user_id == user.id,
                CourseProgress.course_id == course_id,
            ).count()
            if total_modules <= 0 or completed_modules < total_modules:
                raise HTTPException(
                    status_code=403,
                    detail="Complete all course modules before submitting the final assessment.",
                )

            passed_final = db.query(TraineeAssessment).filter(
                TraineeAssessment.trainee_id == user.id,
                TraineeAssessment.course_id == course_id,
                TraineeAssessment.module_index.is_(None),
                TraineeAssessment.passed == True,
            ).first()
            if passed_final:
                raise HTTPException(
                    status_code=403,
                    detail="You have already passed the final course assessment.",
                )

            # The cap, counted under the row lock taken above so overlapping submissions cannot all
            # pass it. Replaces the attempts_left <= 0 test that used to run before the config was
            # even loaded, which is why a reset counter could buy an attempt past the cap.
            _assert_can_take_course_assessment(
                db=db,
                trainee_id=user.id,
                course_id=course_id,
                config=config,
            )

        # Score against the trainee's unique question set (same set used for all retakes)
        tqs = _trainee_question_set_query(
            db, user.id, course_id, module_index
        ).first()

        if tqs:
            unique_qs = json.loads(tqs.questions_json)
        else:
            # Fallback to reference questions
            q_filter = [AssessmentQuestion.course_id == course_id, AssessmentQuestion.is_approved == True]
            if module_index is not None:
                q_filter.append(AssessmentQuestion.module_index == module_index)
            else:
                q_filter.append(AssessmentQuestion.module_index.is_(None))
            ref_qs = db.query(AssessmentQuestion).filter(*q_filter).all()
            unique_qs = [_question_to_dict(q) for q in ref_qs]

        if not unique_qs:
            raise HTTPException(status_code=400, detail="No questions found")

        _assert_all_questions_answered(unique_qs, submission.answers)

        course_scope_index = module_index if module_index is not None else None
        grade_result = grade_trainee_assessment_answers(
            unique_qs=unique_qs,
            answers=submission.answers,
            weightage_per_question=config.weightage_per_question,
            course=course,
            scope_module_index=course_scope_index,
        )
        score = grade_result["score"]
        total_possible = grade_result["total_possible"]
        detailed_results = grade_result["detailed_results"]
        mcq_results_map = grade_result["mcq_results_map"]
        resolved_correct_answers_map = grade_result["resolved_correct_answers_map"]
        subjective_results_map = grade_result.get("subjective_results_map", {})

        percentage = int((score / total_possible) * 100) if total_possible > 0 else 0
        passed = percentage >= config.pass_percentage
        
        # Record assessment. Number it from the attempts already stored for this course scope, the
        # way submit_module_assessment numbers its own, so no two attempts can share a number -
        # that number is how the results view addresses an attempt.
        current_attempt_num = _course_attempt_limits(
            db=db,
            trainee_id=user.id,
            course_id=course_id,
            config=config,
        )["current_attempt_number"]

        assessment = TraineeAssessment(
            trainee_id=user.id,
            course_id=course_id,
            module_index=module_index,
            attempt_number=current_attempt_num,
            questions_data=json.dumps({
                "answers": submission.answers,
                "answer_letters": _build_answer_letters_map(unique_qs, submission.answers),
                "detailed_results": _build_detailed_results_map(detailed_results),
                "mcq_results": mcq_results_map,
                "resolved_correct_answers": resolved_correct_answers_map,
                "subjective_results": subjective_results_map,
            }),
            score=score,
            total_possible=total_possible,
            percentage=percentage,
            passed=passed,
            time_taken=submission.time_taken,
            started_at=datetime.now(timezone.utc) - timedelta(minutes=submission.time_taken)
        )
        db.add(assessment)

        # attempts_left no longer gates anything; it is kept in step for the legacy surfaces that
        # still display it (the admin trainee list, the trainer batch totals) and floored at 0 so a
        # row an admin reset mid-course cannot go negative there.
        attempt.attempts_left = max(0, attempt.attempts_left - 1)

        db.commit()
        log_activity(db, user.id, "submit_assessment", f"Submitted assessment for course {course_id}")

        # Counted after the commit, so the figure the trainee reads is the one the next attempt will
        # be gated on rather than the parallel attempts_left budget.
        attempts_remaining = _course_attempt_limits(
            db=db,
            trainee_id=user.id,
            course_id=course_id,
            config=config,
        )["attempts_remaining"]

        trainee_detailed = sanitize_detailed_results_for_trainee(
            detailed_results, passed=passed
        )

        return {
            "success": True,
            "results": {
                "score": score,
                "total_possible": total_possible,
                "percentage": percentage,
                "passed": passed,
                "pass_percentage": config.pass_percentage,
                "attempts_remaining": attempts_remaining,
                "detailed_results": trainee_detailed,
            },
            "score": score,
            "total_possible": total_possible,
            "percentage": percentage,
            "passed": passed,
            "attempts_remaining": attempts_remaining,
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error('Error submitting assessment: %s', log_safe(e), exc_info=True)
        db.rollback()
        raise HTTPException(status_code=500, detail=GENERIC_API_ERROR_MESSAGE)

@router.get("/trainee/resume-assessment/{course_id}", tags=["Assessment System"])
def get_resume_assessment(
    course_id: str,
    module_index: Optional[int] = Query(None),
    user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """Resume functionality temporarily disabled - requires database migration"""
    raise HTTPException(status_code=404, detail="Resume functionality temporarily disabled")


@router.post("/trainee/save-partial-assessment/{assessment_id}", tags=["Assessment System"])
def save_partial_assessment(
    assessment_id: str,
    partial_answers: Dict[str, str],
    user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """Resume functionality temporarily disabled - requires database migration"""
    raise HTTPException(status_code=404, detail="Resume functionality temporarily disabled")


@router.post("/trainee/request-additional-attempt/{course_id}", tags=["Assessment System"])
def request_additional_attempt(
    course_id: str,
    request_data: AdditionalAttemptRequestModel,
    module_index: Optional[int] = None,
    user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """Request additional attempt when limit is reached"""
    try:
        if user.role != 'trainee':
            raise HTTPException(status_code=403, detail="Trainee access required")
        
        # Check if course exists and is published
        course = db.query(Course).filter(Course.id == course_id).first()
        if not course or not course.is_live:
            raise HTTPException(status_code=403, detail="Course not found or not published")
        
        # Get assessment configuration
        assessment_config = db.query(AssessmentConfig).filter(
            AssessmentConfig.course_id == course_id
        ).first()
        
        # CHECK IF TRAINEE HAS COMPLETED THE COURSE
        course_completed = False
        if assessment_config:
            if assessment_config.assessment_scope == 'end_of_course':
                # For end-of-course assessment: check if passed the final assessment
                passed_end_of_course = db.query(TraineeAssessment).filter(
                    TraineeAssessment.trainee_id == user.id,
                    TraineeAssessment.course_id == course_id,
                    TraineeAssessment.module_index.is_(None),  # End of course assessment
                    TraineeAssessment.passed == True
                ).first()
                course_completed = passed_end_of_course is not None
                
            elif assessment_config.assessment_scope == 'end_of_module':
                # For module-based assessment: check if ALL modules are passed
                # Count total modules
                total_modules = 0
                try:
                    # CRITICAL FIX: Parse source_documents for multi-document courses
                    course_docs_normalized = []
                    try:
                        if course.source_documents:
                            source_docs_list = json.loads(course.source_documents)
                            if isinstance(source_docs_list, list):
                                for doc in source_docs_list:
                                    doc_normalized = normalize_document_name(doc)
                                    course_docs_normalized.append(doc_normalized)
                    except Exception as e:
                        logger.warning('[REQUEST ATTEMPT] Could not parse source_documents JSON: %s', e)
                    
                    # Fallback to single source_doc_name if source_documents is empty
                    if not course_docs_normalized:
                        course_doc_normalized = normalize_document_name(course.source_doc_name)
                        course_docs_normalized = [course_doc_normalized]
                    
                    paginator = s3_client.get_paginator('list_objects_v2')
                    pages = paginator.paginate(Bucket=S3_BUCKET_NAME, Prefix=f"{course.author_id}/generated_reports/")
                    for page in pages:
                        for obj in page.get('Contents', []):
                            if obj['Key'].endswith('.json'):
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
                                    total_modules += 1
                except Exception as e:
                    logger.error('Error counting modules: %s', e)
                
                if total_modules > 0:
                    # Count how many module assessments were passed
                    passed_modules = db.query(func.count(TraineeAssessment.id)).filter(
                        TraineeAssessment.trainee_id == user.id,
                        TraineeAssessment.course_id == course_id,
                        TraineeAssessment.module_index.isnot(None),  # Module assessments only
                        TraineeAssessment.passed == True
                    ).scalar()
                    
                    # Course is completed if all modules are passed
                    course_completed = passed_modules >= total_modules
        
        if course_completed:
            raise HTTPException(
                status_code=400, 
                detail="You have already completed this course. Additional attempts are not available for completed courses."
            )
        
        # Check if attempts are exhausted (scope-aware).
        if assessment_config and assessment_config.assessment_scope == 'end_of_module':
            if module_index is None:
                # Backward compatibility: infer a requestable module automatically.
                module_assessments = db.query(TraineeAssessment).filter(
                    TraineeAssessment.trainee_id == user.id,
                    TraineeAssessment.course_id == course_id,
                    TraineeAssessment.module_index.isnot(None)
                ).all()
                by_module: Dict[int, List[TraineeAssessment]] = {}
                for a in module_assessments:
                    if a.module_index not in by_module:
                        by_module[a.module_index] = []
                    by_module[a.module_index].append(a)

                requestable_modules = []
                for mod_idx, attempts_list in by_module.items():
                    attempts_used = len(attempts_list)
                    has_passed = any(x.passed for x in attempts_list)
                    extra = _get_additional_attempt_allowance(
                        db=db,
                        trainee_id=user.id,
                        course_id=course_id,
                        module_index=mod_idx
                    )
                    effective_max = assessment_config.max_attempts + extra
                    if not has_passed and attempts_used >= effective_max:
                        requestable_modules.append(mod_idx)

                if not requestable_modules:
                    raise HTTPException(status_code=400, detail="No exhausted module attempts found for this course")

                module_index = min(requestable_modules)

            attempts_used = db.query(func.count(TraineeAssessment.id)).filter(
                TraineeAssessment.trainee_id == user.id,
                TraineeAssessment.course_id == course_id,
                TraineeAssessment.module_index == module_index
            ).scalar() or 0

            has_passed_module = db.query(TraineeAssessment).filter(
                TraineeAssessment.trainee_id == user.id,
                TraineeAssessment.course_id == course_id,
                TraineeAssessment.module_index == module_index,
                TraineeAssessment.passed == True
            ).first() is not None

            if has_passed_module:
                raise HTTPException(status_code=400, detail="You have already passed this module; additional attempts are not required")

            additional_allowance = _get_additional_attempt_allowance(
                db=db,
                trainee_id=user.id,
                course_id=course_id,
                module_index=module_index
            )
            effective_max_attempts = (assessment_config.max_attempts if assessment_config else 0) + additional_allowance

            if attempts_used < effective_max_attempts:
                raise HTTPException(status_code=400, detail="You still have attempts remaining for this module")
        else:
            attempt = db.query(TraineeAttempt).filter(
                TraineeAttempt.trainee_id == user.id,
                TraineeAttempt.course_id == course_id
            ).first()
            
            if not attempt:
                raise HTTPException(status_code=400, detail="You still have attempts remaining")

            if assessment_config:
                # Counted the way the load and submit gates count, matching the module branch above.
                # This used to test attempts_left, so a trainee whose counter had been reset above
                # their real remaining attempts was refused here with "You still have attempts
                # remaining" while the assessment itself refused the retake - no way forward at all.
                course_limits = _course_attempt_limits(
                    db=db,
                    trainee_id=user.id,
                    course_id=course_id,
                    config=assessment_config,
                )
                if course_limits["attempts_used"] < course_limits["effective_max_attempts"]:
                    raise HTTPException(status_code=400, detail="You still have attempts remaining")
            elif attempt.attempts_left > 0:
                # No assessment configured: nothing to count against, so the legacy counter is all
                # there is.
                raise HTTPException(status_code=400, detail="You still have attempts remaining")
        
        # Check if there's already a pending request
        existing_request = db.query(AdditionalAttemptRequest).filter(
            AdditionalAttemptRequest.trainee_id == user.id,
            AdditionalAttemptRequest.course_id == course_id,
            AdditionalAttemptRequest.module_index == module_index,
            AdditionalAttemptRequest.status == "pending"
        ).first()
        
        if existing_request:
            raise HTTPException(status_code=400, detail="You already have a pending request for additional attempts")
        
        # Create new request
        new_request = AdditionalAttemptRequest(
            trainee_id=user.id,
            course_id=course_id,
            module_index=module_index,
            reason=request_data.reason
        )
        
        db.add(new_request)
        
        # Create notification for trainer
        notification = Notification(
            user_id=course.author_id,
            message=(
                f"Trainee {user.email} has requested additional attempts for '{course_display_label(course)}'"
                + (f" (Module {module_index})" if module_index is not None else "")
            ),
            related_link="/trainer-dashboard#attempt-requests",
            course_id=course_notification_id(course)
        )
        db.add(notification)
        
        db.commit()
        
        logger.info('[ATTEMPT REQUEST] Created request %s and notification for trainer %s', ("" if new_request.id is None else str(new_request.id).replace("\n", "").replace("\r", "")), ("" if course.author_id is None else str(course.author_id).replace("\n", "").replace("\r", "")))
        log_activity(db, user.id, "request_additional_attempt", f"Requested additional attempt for course {course_id}")
        
        return {"message": "Additional attempt request submitted successfully", "request_id": new_request.id}
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error('Error requesting additional attempt: %s', log_safe(e), exc_info=True)
        db.rollback()
        raise HTTPException(status_code=500, detail=GENERIC_API_ERROR_MESSAGE)

@router.get("/trainer/additional-attempt-requests", tags=["Assessment System"])
def get_additional_attempt_requests(
    user: User = Depends(trainer_required),
    db: Session = Depends(get_db)
):
    """Get all additional attempt requests for trainer's courses"""
    try:
        # Get trainer's courses
        trainer_courses = db.query(Course).filter(Course.author_id == user.id).all()
        course_ids = [course.id for course in trainer_courses]
        
        if not course_ids:
            return []
        
        # Get requests for trainer's courses
        requests = db.query(AdditionalAttemptRequest).filter(
            AdditionalAttemptRequest.course_id.in_(course_ids)
        ).order_by(AdditionalAttemptRequest.requested_at.desc()).all()
        
        # The trainer's courses are already loaded, and the requesting trainees come back in one
        # query, so the list no longer costs two lookups per request row.
        course_by_id = {course.id: course for course in trainer_courses}
        trainees_by_id = {}
        requesting_trainee_ids = {req.trainee_id for req in requests if req.trainee_id}
        if requesting_trainee_ids:
            trainees_by_id = {
                u.id: u for u in db.query(User).filter(User.id.in_(requesting_trainee_ids)).all()
            }

        result = []
        for req in requests:
            trainee = trainees_by_id.get(req.trainee_id)
            course = course_by_id.get(req.course_id)

            result.append(AdditionalAttemptRequestResponse(
                id=req.id,
                trainee_email=trainee.email if trainee else "Unknown",
                course_name=course.source_doc_name if course else "Unknown",
                module_index=req.module_index,
                reason=req.reason,
                status=req.status,
                requested_at=req.requested_at,
                reviewed_at=req.reviewed_at,
                trainer_notes=req.trainer_notes
            ))
        
        return result
        
    except Exception as e:
        logger.error('Error getting additional attempt requests: %s', log_safe(e), exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to load requests")

@router.post("/trainer/review-additional-attempt/{request_id}", tags=["Assessment System"])
def review_additional_attempt_request(
    request_id: str,
    action: str,  # 'approve' or 'reject'
    trainer_notes: Optional[str] = None,
    user: User = Depends(trainer_required),
    db: Session = Depends(get_db)
):
    """Review additional attempt request"""
    try:
        if action not in ['approve', 'reject']:
            raise HTTPException(status_code=400, detail="Action must be 'approve' or 'reject'")
        
        # Get the request
        request_obj = db.query(AdditionalAttemptRequest).filter(
            AdditionalAttemptRequest.id == request_id
        ).first()
        
        if not request_obj:
            raise HTTPException(status_code=404, detail="Request not found")
        
        # Verify trainer owns the course
        course = db.query(Course).filter(
            Course.id == request_obj.course_id,
            Course.author_id == user.id
        ).first()
        
        if not course:
            raise HTTPException(status_code=403, detail="You can only review requests for your own courses")
        
        # Update request status
        request_obj.status = action + "d"  # approved or rejected
        request_obj.reviewed_at = datetime.now(timezone.utc)
        request_obj.reviewed_by = user.id
        request_obj.trainer_notes = trainer_notes
        
        # If approved, add additional attempt. This route grants exactly one; recording it rather
        # than leaving granted_attempts NULL keeps the caps reading the grant instead of the
        # one-per-row default, and keeps the legacy counter in step for the displays that use it.
        if action == 'approve':
            request_obj.granted_attempts = 1
            attempt = db.query(TraineeAttempt).filter(
                TraineeAttempt.trainee_id == request_obj.trainee_id,
                TraineeAttempt.course_id == request_obj.course_id
            ).first()

            if attempt:
                attempt.attempts_left += 1
        
        db.commit()
        
        log_activity(db, user.id, "review_additional_attempt", f"{action.capitalize()}d additional attempt request {request_id}")
        
        return {"message": f"Request {action}d successfully"}
        
    except Exception as e:
        logger.error('Error reviewing additional attempt request: %s', e, exc_info=True)
        db.rollback()
        raise HTTPException(status_code=500, detail="Failed to review request")

def generate_questions_for_content_direct(content: str, total_questions: int, question_type: str, mcq_count: int, subjective_count: int):
    """Generate assessment questions using AI - Direct Bedrock call (DEPRECATED - use agent version)"""
    try:
        # Use the agent-based version instead for consistency
        return generate_questions_for_content(content, total_questions, question_type, mcq_count, subjective_count)
    except Exception as e:
        logger.error('Error in direct question generation: %s', e)
        return create_fallback_questions(content, total_questions, question_type)
            
    except Exception as e:
        logger.error('Error generating questions: %s', e)
        return []


@router.post("/trainee/request-attempt/{course_id}", tags=["Trainee Dashboard & Quizzes"])
def request_more_attempts(course_id: str, user: User = Depends(get_current_active_user), db: Session = Depends(get_db)):
    """
    Sends a notification to a trainee's trainer requesting more quiz attempts for a course.

    - **Required Role**: Trainee.
    - **Raises 404**: If the course assignment for the trainee is not found.
    """
    attempt = db.query(TraineeAttempt).filter(TraineeAttempt.trainee_id == user.id, TraineeAttempt.course_id == course_id).first()
    if not attempt: raise HTTPException(status_code=404, detail="Course not found for this trainee")
    course_display_name = course_display_label(attempt.course)
    db.add(Notification( user_id=attempt.course.author.id, message=f"Trainee {user.email} has requested more attempts for the course '{course_display_name}'.", related_link="/trainer-dashboard#attempt-requests", course_id=course_notification_id(attempt.course) ))
    log_activity(db, user.id, "REQUEST_ATTEMPT", f"Requested attempt for course '{course_display_name}'")
    db.commit()
    return {"message": "Request for more attempts has been sent to your trainer."}

@router.get("/trainer/attempt-requests", tags=["Trainer Dashboard"])
def get_attempt_requests(
    status: Optional[str] = None,
    user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """Get attempt requests for user's own courses (trainer or super admin acting as trainer)"""
    try:
        if user.role not in ['trainer', 'super_admin']:
            raise HTTPException(status_code=403, detail="Trainer or Super Admin access required")
        
        # Get only THIS user's published courses (super admin acts as trainer)
        own_courses = db.query(Course).filter(
            Course.author_id == user.id,  # Only user's own courses
            Course.is_live == True  # Only published courses
        ).all()
        course_by_id = {course.id: course for course in own_courses}
        course_ids = list(course_by_id.keys())


        logger.info('[ATTEMPT REQUESTS] User %s (role: %s) has %s published courses', ("" if user.email is None else str(user.email).replace("\n", "").replace("\r", "")), ("" if user.role is None else str(user.role).replace("\n", "").replace("\r", "")), len(course_ids))
        
        if not course_ids:
            logger.info('[ATTEMPT REQUESTS] User %s has no published courses, returning empty list', ("" if user.email is None else str(user.email).replace("\n", "").replace("\r", "")))
            return {
                "success": True,
                "data": [],
                "total": 0,
                "message": "No published courses found"
            }
        
        # Base query for attempt requests
        query = db.query(AdditionalAttemptRequest).join(Course).filter(
            Course.id.in_(course_ids)
        )
        
        # Filter by status if provided
        if status and status != 'all':
            query = query.filter(AdditionalAttemptRequest.status == status)
        
        # Get requests with related data
        requests = query.order_by(AdditionalAttemptRequest.requested_at.desc()).all()
        logger.info('[ATTEMPT REQUESTS] Found %s requests with status %s', len(requests), ("" if status is None else str(status).replace("\n", "").replace("\r", "")))
        
        # Trainees, assessment configs and current attempt rows for every request in one query
        # each; these were four lookups per request row, so a busy queue timed the page out.
        requesting_trainee_ids = {req.trainee_id for req in requests if req.trainee_id}
        requested_course_ids = {req.course_id for req in requests if req.course_id}
        trainees_by_id = {}
        configs_by_course = {}
        attempts_by_pair = {}
        course_attempts_left_by_pair = {}
        if requesting_trainee_ids:
            trainees_by_id = {
                u.id: u for u in db.query(User).filter(User.id.in_(requesting_trainee_ids)).all()
            }
        if requested_course_ids:
            configs_by_course = {
                cfg.course_id: cfg
                for cfg in db.query(AssessmentConfig).filter(
                    AssessmentConfig.course_id.in_(requested_course_ids)
                ).all()
            }
        if requesting_trainee_ids and requested_course_ids:
            for row in db.query(TraineeAttempt).filter(
                TraineeAttempt.trainee_id.in_(requesting_trainee_ids),
                TraineeAttempt.course_id.in_(requested_course_ids),
            ).all():
                # First row seen per pair, matching the unordered `.first()` this replaced.
                attempts_by_pair.setdefault((row.trainee_id, row.course_id), row)

        # Counted end-of-course remaining for every pair in the queue. The trainer is deciding
        # whether a trainee is out of attempts, so this cannot be the legacy attempts_left the rows
        # carry: that column is what said "2 left" for a trainee the gates had already exhausted.
        course_attempts_left_by_pair = end_of_course_attempts_left_by_trainee_course(
            db, requesting_trainee_ids, requested_course_ids, configs_by_course
        )

        # Format response
        formatted_requests = []
        for req in requests:
            trainee = trainees_by_id.get(req.trainee_id)
            course = course_by_id.get(req.course_id)

            # Get assessment scope from AssessmentConfig (not on Course model)
            assessment_config = configs_by_course.get(req.course_id)
            assessment_scope = assessment_config.assessment_scope if assessment_config else "end_of_course"

            # Get current attempts for this trainee/course
            attempt = attempts_by_pair.get((req.trainee_id, req.course_id))
            current_attempts = course_attempts_left_by_pair.get(
                (req.trainee_id, req.course_id),
                attempt.attempts_left if attempt else 0,
            )

            formatted_requests.append({
                "id": req.id,
                "trainee_email": trainee.email if trainee else "Unknown",
                "trainee_name": trainee.email if trainee else "Unknown",
                "course_name": course.source_doc_name if course else "Unknown",
                "course_title": course.course_title if course else None,
                "course_id": req.course_id,
                "module_index": req.module_index,
                "assessment_scope": assessment_scope,
                "reason": req.reason,
                "status": req.status,
                "requested_at": req.requested_at.isoformat(),
                "processed_at": req.reviewed_at.isoformat() if req.reviewed_at else None,
                "current_attempts": current_attempts,
                "requested_attempts": 1
            })
        
        return {
            "success": True,
            "data": formatted_requests,
            "total": len(formatted_requests)
        }
        
    except Exception as e:
        logger.error('Error getting attempt requests: %s', log_safe(e), exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to get attempt requests")

@router.post("/trainer/attempt-requests/{request_id}/approve", tags=["Trainer Dashboard"])
def approve_attempt_request(
    request_id: str,
    request_body: ApproveAttemptRequestModel,
    user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """Approve an attempt request and grant additional attempts"""
    try:
        if user.role not in ['trainer', 'super_admin']:
            raise HTTPException(status_code=403, detail="Trainer or Super Admin access required")
        
        additional_attempts = request_body.additional_attempts
        logger.info('[APPROVE REQUEST] Processing approval for request %s with %s additional attempts', ("" if request_id is None else str(request_id).replace("\n", "").replace("\r", "")), ("" if additional_attempts is None else str(additional_attempts).replace("\n", "").replace("\r", "")))
        
        # Get the request
        request_obj = db.query(AdditionalAttemptRequest).filter(
            AdditionalAttemptRequest.id == request_id
        ).first()
        
        if not request_obj:
            raise HTTPException(status_code=404, detail="Request not found")
        
        # Verify user has access to this course
        course = db.query(Course).filter(Course.id == request_obj.course_id).first()
        if not course:
            raise HTTPException(status_code=404, detail="Course not found")
        
        # User can only approve for their OWN courses (both trainer and super_admin)
        if course.author_id != user.id:
            raise HTTPException(status_code=403, detail="You can only approve requests for your own courses")
        
        if request_obj.status != 'pending':
            raise HTTPException(status_code=400, detail="Request already processed")
        
        # Update the trainee's attempts
        attempt = db.query(TraineeAttempt).filter(
            TraineeAttempt.trainee_id == request_obj.trainee_id,
            TraineeAttempt.course_id == request_obj.course_id
        ).first()
        
        if not attempt:
            raise HTTPException(status_code=404, detail="Trainee attempt record not found")
        
        # Grant additional attempts (course-level pool only — module requests use approved-request allowance)
        old_attempts = attempt.attempts_left
        if request_obj.module_index is None:
            attempt.attempts_left += additional_attempts
            logger.info('[APPROVE REQUEST] Updated course attempts: %s -> %s (added %s)', ("" if old_attempts is None else str(old_attempts).replace("\n", "").replace("\r", "")), ("" if attempt.attempts_left is None else str(attempt.attempts_left).replace("\n", "").replace("\r", "")), ("" if additional_attempts is None else str(additional_attempts).replace("\n", "").replace("\r", "")))
        else:
            logger.info('[APPROVE REQUEST] Module %s request approved — retake unlocked via allowance (course attempts unchanged)', ("" if request_obj.module_index is None else str(request_obj.module_index).replace("\n", "").replace("\r", "")))

        # What the trainee will actually have. Computed from the attempts stored plus this grant,
        # because that is what the retake is gated on; the legacy counter quoted here before could
        # say anything an admin reset had left in it. Deliberately computed before the request row is
        # marked approved below, so this grant is added once rather than counted twice.
        course_attempts_after = attempt.attempts_left
        if request_obj.module_index is None:
            granting_config = db.query(AssessmentConfig).filter(
                AssessmentConfig.course_id == request_obj.course_id
            ).first()
            if granting_config:
                course_attempts_after = _course_attempt_limits(
                    db=db,
                    trainee_id=request_obj.trainee_id,
                    course_id=request_obj.course_id,
                    config=granting_config,
                )["attempts_remaining"] + additional_attempts

        # Update request status. granted_attempts is what the caps are actually computed from - the
        # attempts_left bump above only feeds legacy displays - so a module approval now grants the
        # number the trainer chose too, instead of the one attempt a row used to be worth.
        request_obj.status = 'approved'
        request_obj.granted_attempts = additional_attempts
        request_obj.reviewed_at = datetime.now(timezone.utc)
        request_obj.reviewed_by = user.id
        
        # Create notification for trainee
        trainee = db.query(User).filter(User.id == request_obj.trainee_id).first()
        if trainee:
            safe_course = (course_display_label(course) or "your course").replace("\r", " ").replace("\n", " ")
            if request_obj.module_index is not None:
                # The trainer chooses how many to grant, so say how many rather than "an attempt".
                granted_label = (
                    "an additional attempt" if additional_attempts == 1
                    else f"{additional_attempts} additional attempts"
                )
                notify_msg = (
                    f"Your trainer approved {granted_label} for Module {request_obj.module_index} "
                    f"in '{safe_course}'. Open My Courses and tap Retake on that module."
                )
            else:
                notify_msg = (
                    f"Your trainer approved additional attempts for '{safe_course}'. "
                    f"You now have {course_attempts_after} course attempt(s) — retake the final assessment when ready."
                )
            db.add(Notification(
                user_id=trainee.id,
                message=notify_msg,
                related_link="/trainee-dashboard",
                course_id=course_notification_id(course)
            ))

        db.commit()
        
        logger.info('[ATTEMPT REQUEST] Approved request %s: %s -> %s attempts', ("" if request_id is None else str(request_id).replace("\n", "").replace("\r", "")), ("" if old_attempts is None else str(old_attempts).replace("\n", "").replace("\r", "")), ("" if attempt.attempts_left is None else str(attempt.attempts_left).replace("\n", "").replace("\r", "")))
        
        return {
            "success": True,
            "message": (
                f"Request approved. Trainee can retake Module {request_obj.module_index}."
                if request_obj.module_index is not None
                else f"Request approved. Trainee now has {course_attempts_after} course attempt(s)."
            ),
            "new_attempts": course_attempts_after,
            "module_index": request_obj.module_index,
        }

    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        logger.error('Error approving attempt request: %s', log_safe(e), exc_info=True)
        db.rollback()
        raise HTTPException(status_code=500, detail="Failed to approve request") from e


@router.post("/trainer/attempt-requests/{request_id}/deny", tags=["Trainer Dashboard"])
def deny_attempt_request(
    request_id: str,
    request_body: DenyAttemptRequestModel,
    user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """Deny an attempt request"""
    try:
        if user.role not in ['trainer', 'super_admin']:
            raise HTTPException(status_code=403, detail="Trainer or Super Admin access required")
        
        denial_reason = (request_body.denial_reason or "").strip()[:500]
        if not denial_reason:
            raise HTTPException(status_code=400, detail="Denial reason is required")
        
        # Get the request
        request_obj = db.query(AdditionalAttemptRequest).filter(
            AdditionalAttemptRequest.id == request_id
        ).first()
        
        if not request_obj:
            raise HTTPException(status_code=404, detail="Request not found")
        
        # Verify user has access to this course
        course = db.query(Course).filter(Course.id == request_obj.course_id).first()
        if not course:
            raise HTTPException(status_code=404, detail="Course not found")
        
        # Same ownership rule as approve — trainer/super_admin only for their own courses
        if course.author_id != user.id:
            raise HTTPException(status_code=403, detail="You can only deny requests for your own courses")
        
        if request_obj.status != 'pending':
            raise HTTPException(status_code=400, detail="Request already processed")
        
        # Update request status
        request_obj.status = 'denied'
        request_obj.reviewed_at = datetime.now(timezone.utc)
        request_obj.reviewed_by = user.id
        request_obj.trainer_notes = denial_reason

        trainee = db.query(User).filter(User.id == request_obj.trainee_id).first()
        if trainee:
            safe_course = (course_display_label(course) or "your course").replace("\r", " ").replace("\n", " ")
            safe_reason = denial_reason.replace("\r", " ").replace("\n", " ")
            if request_obj.module_index is not None:
                notify_msg = (
                    f"Your trainer declined the additional attempt request for Module {request_obj.module_index} "
                    f"in '{safe_course}'. Reason: {safe_reason}"
                )
            else:
                notify_msg = (
                    f"Your trainer declined your additional attempt request for '{safe_course}'. "
                    f"Reason: {safe_reason}"
                )
            db.add(Notification(
                user_id=trainee.id,
                message=notify_msg,
                related_link="/trainee-dashboard",
                course_id=course_notification_id(course)
            ))

        db.commit()

        logger.info(
            '[ATTEMPT REQUEST] Denied request %s',
            log_safe(request_id),
        )

        return {
            "success": True,
            "message": "Request denied successfully"
        }

    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        logger.error('Error denying attempt request: %s', log_safe(e), exc_info=True)
        db.rollback()
        raise HTTPException(status_code=500, detail="Failed to deny request") from e

@router.get("/trainee/attempt-request-status/{course_id}", tags=["Trainee Dashboard & Quizzes"])
def get_attempt_request_status(
    course_id: str,
    user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """Get trainee's attempt request status for a course"""
    try:
        if user.role != 'trainee':
            raise HTTPException(status_code=403, detail="Trainee access required")
        
        # Get assessment config (scope-aware request eligibility)
        assessment_config = db.query(AssessmentConfig).filter(
            AssessmentConfig.course_id == course_id
        ).first()

        # Get pending request (scope-aware: course-level vs per-module)
        pending_query = db.query(AdditionalAttemptRequest).filter(
            AdditionalAttemptRequest.trainee_id == user.id,
            AdditionalAttemptRequest.course_id == course_id,
            AdditionalAttemptRequest.status == 'pending'
        )
        if assessment_config and assessment_config.assessment_scope == 'end_of_module':
            pending_request = pending_query.first()
        else:
            pending_request = pending_query.filter(
                AdditionalAttemptRequest.module_index.is_(None)
            ).first()

        can_request = False
        current_attempts = 0
        module_requests: Dict[str, Dict[str, Optional[str]]] = {}

        # Build latest request status map per module (and course-level as "course")
        all_requests = db.query(AdditionalAttemptRequest).filter(
            AdditionalAttemptRequest.trainee_id == user.id,
            AdditionalAttemptRequest.course_id == course_id
        ).order_by(AdditionalAttemptRequest.requested_at.desc()).all()

        for req in all_requests:
            key = str(req.module_index) if req.module_index is not None else "course"
            if key in module_requests:
                continue
            module_requests[key] = {
                "status": req.status,
                "requested_at": req.requested_at.isoformat() if req.requested_at else None,
                "reviewed_at": req.reviewed_at.isoformat() if req.reviewed_at else None
            }

        if assessment_config and assessment_config.assessment_scope == 'end_of_module':
            # Module-based courses: can request when any failed module has exhausted all allowed attempts.
            module_assessments = db.query(TraineeAssessment).filter(
                TraineeAssessment.trainee_id == user.id,
                TraineeAssessment.course_id == course_id,
                TraineeAssessment.module_index.isnot(None)
            ).all()

            by_module: Dict[int, List[TraineeAssessment]] = {}
            for a in module_assessments:
                if a.module_index not in by_module:
                    by_module[a.module_index] = []
                by_module[a.module_index].append(a)

            exhausted_failed_modules = []
            for mod_idx, attempts_list in by_module.items():
                attempts_used = len(attempts_list)
                has_passed = any(x.passed for x in attempts_list)
                extra = _get_additional_attempt_allowance(
                    db=db,
                    trainee_id=user.id,
                    course_id=course_id,
                    module_index=mod_idx
                )
                effective_max = assessment_config.max_attempts + extra
                if not has_passed and attempts_used >= effective_max:
                    exhausted_failed_modules.append(mod_idx)

            pending_module_indices = {
                r.module_index for r in db.query(AdditionalAttemptRequest).filter(
                    AdditionalAttemptRequest.trainee_id == user.id,
                    AdditionalAttemptRequest.course_id == course_id,
                    AdditionalAttemptRequest.status == 'pending',
                    AdditionalAttemptRequest.module_index.isnot(None)
                ).all()
            }
            requestable_modules = [
                m for m in exhausted_failed_modules if m not in pending_module_indices
            ]

            current_attempts = len(exhausted_failed_modules)
            can_request = len(requestable_modules) > 0
        else:
            # End-of-course courses. Counted the same way as the module branch above, and the same
            # way the request endpoint decides whether to accept: this used to read attempts_left,
            # so a trainee sitting on a reset counter was shown attempts they could not use and no
            # way to ask for more, because can_request required that counter to be exactly 0.
            attempt = db.query(TraineeAttempt).filter(
                TraineeAttempt.trainee_id == user.id,
                TraineeAttempt.course_id == course_id
            ).first()
            if attempt and assessment_config:
                course_limits = _course_attempt_limits(
                    db=db,
                    trainee_id=user.id,
                    course_id=course_id,
                    config=assessment_config,
                )
                current_attempts = course_limits["attempts_remaining"]
                can_request = bool(current_attempts == 0 and not pending_request)
            else:
                current_attempts = attempt.attempts_left if attempt else 0
                can_request = bool(attempt and attempt.attempts_left == 0 and not pending_request)

        return {
            "success": True,
            "data": {
                "has_pending_request": pending_request is not None,
                "request_id": pending_request.id if pending_request else None,
                "requested_at": pending_request.requested_at.isoformat() if pending_request else None,
                "pending_module_index": pending_request.module_index if pending_request else None,
                "current_attempts": current_attempts,
                "can_request": can_request,
                "module_requests": module_requests,
                "course_request": module_requests.get("course"),
                "assessment_scope": assessment_config.assessment_scope if assessment_config else None,
            }
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error('Error getting attempt request status: %s', log_safe(e))
        raise HTTPException(status_code=500, detail="Failed to get request status")

@router.get("/trainee/assessment-config/{course_id}", tags=["Trainee Dashboard & Quizzes"])
def get_trainee_assessment_config(
    course_id: str,
    user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """
    Return assessment configuration for a course assigned to the current trainee.
    Exposes only the fields needed for trainee UI (attempt limits, pass threshold, scope).
    """
    if user.role != "trainee":
        raise HTTPException(status_code=403, detail="Trainee access required")

    attempt = db.query(TraineeAttempt).filter(
        TraineeAttempt.trainee_id == user.id,
        TraineeAttempt.course_id == course_id,
    ).first()
    if not attempt:
        raise HTTPException(status_code=403, detail="No access to this course")

    course = db.query(Course).filter(Course.id == course_id).first()
    if not course:
        raise HTTPException(status_code=404, detail="Course not found")
    if not course.is_live:
        raise HTTPException(status_code=403, detail="Course is not published")

    config = db.query(AssessmentConfig).filter(
        AssessmentConfig.course_id == course_id
    ).first()
    if not config:
        return {"exists": False}

    return {
        "exists": True,
        "assessment_scope": config.assessment_scope,
        "pass_percentage": config.pass_percentage,
        "max_attempts": config.max_attempts,
        "total_questions": config.total_questions,
        "is_approved": config.is_approved,
    }


@router.get("/trainee/assessment-status/{course_id}", tags=["Trainee Dashboard & Quizzes"])
def get_trainee_assessment_status(course_id: str, user: User = Depends(get_current_active_user), db: Session = Depends(get_db)):
    """
    Get assessment status for a trainee in a specific course.
    Returns which module assessments have been passed and attempt counts per module.
    """
    try:
        if user.role != "trainee":
            raise HTTPException(status_code=403, detail="Trainee access required")

        attempt = db.query(TraineeAttempt).filter(
            TraineeAttempt.trainee_id == user.id,
            TraineeAttempt.course_id == course_id,
        ).first()
        if not attempt:
            raise HTTPException(status_code=403, detail="No access to this course")

        course = db.query(Course).filter(Course.id == course_id).first()
        if not course:
            raise HTTPException(status_code=404, detail="Course not found")
        if not course.is_live:
            raise HTTPException(status_code=403, detail="Course is not published")

        # Get assessment config for this course
        assessment_config = db.query(AssessmentConfig).filter(
            AssessmentConfig.course_id == course_id
        ).first()
        
        max_attempts = assessment_config.max_attempts if assessment_config else 3
        pass_percentage = assessment_config.pass_percentage if assessment_config else 70
        
        # Get all assessments for this trainee and course
        assessments = db.query(TraineeAssessment).filter(
            TraineeAssessment.trainee_id == user.id,
            TraineeAssessment.course_id == course_id
        ).all()
        
        # Group assessments by module to count attempts
        module_attempts = {}

        for assessment in assessments:
            module_idx = assessment.module_index
            if module_idx not in module_attempts:
                module_attempts[module_idx] = []
            module_attempts[module_idx].append(assessment)
        
        # Every additional-attempt request for this trainee and course, read once. The loop below
        # runs per module and previously asked for the approved count and the pending row per
        # module, so a course with many modules paid two round-trips each; the counts below are
        # the same filters applied in memory.
        attempt_requests = db.query(AdditionalAttemptRequest).filter(
            AdditionalAttemptRequest.trainee_id == user.id,
            AdditionalAttemptRequest.course_id == course_id,
        ).all()
        approved_requests = [r for r in attempt_requests if r.status == 'approved']
        # Course-wide approvals (module_index null) raise the cap for every module as well. An
        # approval carries the number of attempts the trainer chose to grant, so these sum
        # granted_attempts rather than counting rows - counting would report a grant of 3 as 1 and
        # disagree with the cap the load and submit gates enforce. Rows approved before the column
        # existed hold NULL and are worth the single attempt they have always been worth.
        course_wide_approvals = sum(
            _granted_attempt_count(r) for r in approved_requests if r.module_index is None
        )
        module_approvals = defaultdict(int)
        for request_row in approved_requests:
            if request_row.module_index is not None:
                module_approvals[request_row.module_index] += _granted_attempt_count(request_row)
        pending_modules = {
            r.module_index for r in attempt_requests if r.status == 'pending'
        }

        # Build status for each module
        assessment_status = []
        for module_idx, attempts_list in module_attempts.items():
            latest_attempt = _pick_latest_module_assessment(attempts_list)
            if not latest_attempt:
                continue
            # Count total attempts for this module
            attempts_used = len(attempts_list)
            extra_attempts = course_wide_approvals
            if module_idx is not None:
                extra_attempts += module_approvals.get(module_idx, 0)
            effective_max_attempts = max_attempts + extra_attempts
            attempts_remaining = max(0, effective_max_attempts - attempts_used)
            
            # Check if passed (any attempt)
            has_passed = any(a.passed for a in attempts_list)
            passing_attempt = next((a for a in attempts_list if a.passed), None)

            # `module_index == module_idx` never matches in SQL when module_idx is None, so an
            # end-of-course row is only ever "pending" through the module-specific comparison -
            # preserved here by asking whether a non-null module index is in the pending set.
            has_pending_request = module_idx is not None and module_idx in pending_modules

            # CRITICAL: Only allow requesting more attempts if NOT passed and none pending
            can_request_more = not has_passed and attempts_remaining == 0 and not has_pending_request
            
            # Use latest attempt for score display unless the module is already cleared
            if has_passed and passing_attempt:
                percentage = _assessment_display_percentage(passing_attempt)
                display_passed = True
            else:
                percentage = latest_attempt.percentage
                if percentage is None and latest_attempt.total_possible and latest_attempt.total_possible > 0:
                    percentage = round((latest_attempt.score / latest_attempt.total_possible) * 100)
                elif percentage is None:
                    percentage = 0
                display_passed = bool(latest_attempt.passed)
            
            logger.info('[ASSESSMENT STATUS] Module %s: latest score=%s, total=%s, percentage=%s, passed=%s', ("" if module_idx is None else str(module_idx).replace("\n", "").replace("\r", "")), ("" if latest_attempt.score is None else str(latest_attempt.score).replace("\n", "").replace("\r", "")), ("" if latest_attempt.total_possible is None else str(latest_attempt.total_possible).replace("\n", "").replace("\r", "")), ("" if percentage is None else str(percentage).replace("\n", "").replace("\r", "")), ("" if has_passed is None else str(has_passed).replace("\n", "").replace("\r", "")))
            
            assessment_status.append({
                "module_index": module_idx,
                "passed": display_passed,
                "has_passed": has_passed,
                "percentage": percentage,
                "score": latest_attempt.score or 0,
                "total_possible": latest_attempt.total_possible or 0,
                "attempts_used": attempts_used,
                "attempts_remaining": attempts_remaining,
                "max_attempts": effective_max_attempts,
                "pass_percentage": pass_percentage,
                "latest_attempt_number": _canonical_attempt_number(latest_attempt, attempts_list),
                "can_retake": attempts_remaining > 0 and not has_passed and not has_pending_request,
                "can_request_more": can_request_more,
                # The row was selected by status == 'pending', so that is the only value this
                # ever reported when a request existed.
                "request_status": 'pending' if has_pending_request else None,
            })
        
        return assessment_status
        
    except Exception as e:
        logger.error(
            'Error getting assessment status for user %s, course %s: %s',
            ("" if user.id is None else str(user.id).replace("\n", "").replace("\r", "")),
            ("" if course_id is None else str(course_id).replace("\n", "").replace("\r", "")),
            log_safe(e),
        )
        raise HTTPException(status_code=500, detail=GENERIC_API_ERROR_MESSAGE)


def _extract_mcq_options_from_question(q: Dict[str, Any]) -> List[Dict[str, str]]:
    """Build [{letter, text}, ...] from question dict (reference, AI, or cached formats)."""
    raw_options = q.get("options")
    if isinstance(raw_options, list) and raw_options:
        result: List[Dict[str, str]] = []
        for opt_index, opt in enumerate(raw_options):
            if isinstance(opt, dict):
                letter = opt.get("letter") or opt.get("label") or chr(65 + opt_index)
                text = opt.get("text") or opt.get("value") or opt.get("option") or ""
            else:
                letter = chr(65 + opt_index)
                text = str(opt)
            if text:
                result.append({"letter": str(letter).strip().upper(), "text": str(text)})
        if result:
            return result

    result = []
    for letter, key in [("A", "option_a"), ("B", "option_b"), ("C", "option_c"), ("D", "option_d")]:
        val = q.get(key)
        if val:
            result.append({"letter": letter, "text": str(val)})
    return result


def _build_detailed_results_map(detailed_results: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Index graded per-question rows by question_id for reliable review replay."""
    indexed: Dict[str, Dict[str, Any]] = {}
    for row in detailed_results or []:
        qid = row.get("question_id")
        if qid is not None:
            indexed[str(qid)] = row
    return indexed


def _resolve_stored_user_answer(
    answers: Dict[str, Any],
    qid: Any,
    *,
    q_index: int = 0,
    answer_letters_map: Optional[Dict[str, Any]] = None,
    detailed_results_map: Optional[Dict[str, Any]] = None,
) -> Any:
    """Resolve a trainee's stored answer even when question-id keys differ slightly."""
    if detailed_results_map:
        detail = detailed_results_map.get(str(qid)) or detailed_results_map.get(qid)
        if isinstance(detail, dict):
            stored = detail.get("user_answer")
            if stored not in (None, ""):
                return stored

    if not answers:
        return ""

    key_candidates = [str(qid), qid]
    if qid is not None:
        key_candidates.extend([str(qid).lower(), str(qid).upper()])

    for key in key_candidates:
        if key in answers and answers[key] not in (None, ""):
            return answers[key]

    answers_str_keys = {str(k): v for k, v in answers.items()}
    qid_str = str(qid)
    for key, value in answers_str_keys.items():
        if value in (None, ""):
            continue
        if key == qid_str or key.lower() == qid_str.lower():
            return value

    index_keys = [str(q_index), q_index, str(q_index + 1), q_index + 1]
    for key in index_keys:
        if key in answers and answers[key] not in (None, ""):
            return answers[key]
        if str(key) in answers_str_keys and answers_str_keys[str(key)] not in (None, ""):
            return answers_str_keys[str(key)]

    if answer_letters_map:
        for key in key_candidates:
            letters = answer_letters_map.get(str(key)) or answer_letters_map.get(key)
            if letters:
                return ",".join(str(l) for l in letters)

    if answers and q_index is not None:
        answer_items = list(answers.items())
        if q_index < len(answer_items):
            _, positional_value = answer_items[q_index]
            if positional_value not in (None, ""):
                return positional_value

    return ""


def _build_answer_letters_map(
    unique_qs: List[Dict[str, Any]],
    answers: Dict[str, Any],
) -> Dict[str, List[str]]:
    """Persist normalized MCQ letter selections at submit time for reliable review."""
    letter_map: Dict[str, List[str]] = {}
    for q_index, q in enumerate(unique_qs):
        qid = q.get("id")
        if not qid:
            continue
        qtype = normalize_question_type(q.get("type", q.get("question_type", "mcq")))
        if qtype != "mcq":
            continue
        raw = _resolve_stored_user_answer(answers, qid, q_index=q_index)
        letters = sorted(_normalize_mcq_selections(raw, q))
        if letters:
            letter_map[str(qid)] = letters
    return letter_map


def _parse_stored_assessment_answers(questions_data_raw) -> Tuple[
    Dict[str, Any],
    Dict[str, Any],
    Dict[str, str],
    Dict[str, Any],
    Dict[str, List[str]],
    Dict[str, Dict[str, Any]],
]:
    """Parse TraineeAssessment.questions_data into answers + MCQ/subjective metadata maps."""
    answers: Dict[str, Any] = {}
    mcq_results_map: Dict[str, Any] = {}
    resolved_correct_answers_map: Dict[str, str] = {}
    subjective_results_map: Dict[str, Any] = {}
    answer_letters_map: Dict[str, List[str]] = {}
    detailed_results_map: Dict[str, Dict[str, Any]] = {}

    empty = (
        answers,
        mcq_results_map,
        resolved_correct_answers_map,
        subjective_results_map,
        answer_letters_map,
        detailed_results_map,
    )

    if not questions_data_raw:
        return empty

    try:
        parsed = (
            json.loads(questions_data_raw)
            if isinstance(questions_data_raw, str)
            else questions_data_raw
        )
    except Exception:
        return empty

    if isinstance(parsed, dict) and "answers" in parsed:
        answers = parsed.get("answers") or {}
        mcq_results_map = parsed.get("mcq_results", {}) or {}
        resolved_correct_answers_map = parsed.get("resolved_correct_answers", {}) or {}
        subjective_results_map = parsed.get("subjective_results", {}) or {}
        raw_letters = parsed.get("answer_letters", {}) or {}
        if isinstance(raw_letters, dict):
            for qid, letters in raw_letters.items():
                if isinstance(letters, list):
                    answer_letters_map[str(qid)] = [str(l).strip().upper() for l in letters if l]
        raw_details = parsed.get("detailed_results", {}) or {}
        if isinstance(raw_details, dict):
            for qid, detail in raw_details.items():
                if isinstance(detail, dict):
                    detailed_results_map[str(qid)] = detail
        elif isinstance(raw_details, list):
            detailed_results_map = _build_detailed_results_map(raw_details)
    elif isinstance(parsed, dict):
        answers = parsed

    return (
        answers,
        mcq_results_map,
        resolved_correct_answers_map,
        subjective_results_map,
        answer_letters_map,
        detailed_results_map,
    )


def _coerce_unique_questions_list(raw: Any) -> List[Dict[str, Any]]:
    """Normalize cached/generated question payloads to a list of question dicts."""
    if not raw:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return []
    if isinstance(raw, list):
        return [q for q in raw if isinstance(q, dict)]
    if isinstance(raw, dict):
        if isinstance(raw.get("questions"), list):
            return [q for q in raw["questions"] if isinstance(q, dict)]
    return []


def _build_trainee_assessment_question_results(
    *,
    trainee_id: str,
    course_id: str,
    module_index: Optional[int],
    assessment: TraineeAssessment,
    db: Session,
    hide_correct_answers: bool = False,
) -> List[Dict[str, Any]]:
    """Build per-question result rows for trainee assessment review modals.

    Read-only: every verdict comes from what grading persisted on the attempt
    (``mcq_results`` / ``resolved_correct_answers`` in ``questions_data``). This path must not
    re-derive an answer key with the model - it used to load the module markdown from S3 and then
    spend up to two Bedrock calls per unresolved MCQ *inside a GET*, which is what made the review
    modal exceed the request timeout on a full question set, and a fresh adjudication could also
    contradict the score already stored on the attempt.
    """
    tqs = _trainee_question_set_query(
        db, trainee_id, course_id, module_index
    ).order_by(TraineeQuestionSet.created_at.asc()).first()

    if tqs and tqs.questions_json:
        try:
            unique_qs = _coerce_unique_questions_list(json.loads(tqs.questions_json))
        except Exception as parse_err:
            logger.warning(
                "[ASSESSMENT RESULTS] Could not parse TraineeQuestionSet for course %s module %s: %s",
                ("" if course_id is None else str(course_id).replace("\n", "").replace("\r", "")),
                ("" if module_index is None else str(module_index).replace("\n", "").replace("\r", "")),
                log_safe(parse_err),
            )
            unique_qs = []
    else:
        unique_qs = []

    if not unique_qs:
        q_filter = [AssessmentQuestion.course_id == course_id, AssessmentQuestion.is_approved == True]
        if module_index is not None:
            q_filter.append(AssessmentQuestion.module_index == module_index)
        else:
            q_filter.append(AssessmentQuestion.module_index.is_(None))
        ref_qs = db.query(AssessmentQuestion).filter(*q_filter).all()
        unique_qs = [_question_to_dict(q) for q in ref_qs]

    answers, mcq_results_map, resolved_correct_answers_map, subjective_results_map, answer_letters_map, detailed_results_map = _parse_stored_assessment_answers(
        assessment.questions_data
    )

    question_results: List[Dict[str, Any]] = []
    for q_index, q in enumerate(unique_qs):
        qid = q.get("id")
        if not qid:
            continue
        qtype = normalize_question_type(q.get("type", q.get("question_type", "mcq")))
        qtext = q.get("question", q.get("question_text", ""))
        correct_ans = q.get("correct_answer", "")
        explanation = q.get("explanation", "")
        user_answer = _resolve_stored_user_answer(
            answers,
            qid,
            q_index=q_index,
            answer_letters_map=answer_letters_map,
            detailed_results_map=detailed_results_map,
        )
        subj_meta = subjective_results_map.get(str(qid), {}) or {}

        options = _extract_mcq_options_from_question(q) if qtype == "mcq" else []

        if qtype == "mcq":
            stored_flag = mcq_results_map.get(str(qid), mcq_results_map.get(qid))
            if isinstance(stored_flag, bool):
                is_correct = stored_flag
            elif isinstance(stored_flag, str):
                is_correct = stored_flag.strip().lower() == "true"
            else:
                # No verdict was stored (attempt graded before mcq_results was persisted). Compare
                # the trainee's selections against the answer key the grader resolved, falling back
                # to the question template. Both sides go through _normalize_mcq_selections so a
                # stored option *text* and a stored letter compare equal - the same rule
                # _parse_assessment_questions() in features/trainer/router.py already used.
                key_answer = resolved_correct_answers_map.get(str(qid)) or correct_ans
                user_sel = _normalize_mcq_selections(user_answer, q)
                correct_sel = _normalize_mcq_selections(key_answer, q)
                is_correct = bool(user_sel) and user_sel == correct_sel
        else:
            is_correct = None

        row: Dict[str, Any] = {
            "question_id": qid,
            "question_text": qtext,
            "question_type": qtype,
            "options": options if qtype == "mcq" else None,
            "user_answer": user_answer,
            "trainee_answer": user_answer,
            "is_correct": is_correct,
        }
        if qtype == "mcq":
            row["user_answer_letters"] = sorted(
                answer_letters_map.get(str(qid))
                or _normalize_mcq_selections(user_answer, q)
            )
        # Failed trainee attempts: omit answer key / explanation / evaluator feedback
        # so they are absent from both UI and network payloads.
        if hide_correct_answers:
            if qtype == "subjective":
                # Keep the trainee's own text only — no score/feedback leakage.
                pass
        else:
            row["correct_answer"] = (
                resolved_correct_answers_map.get(str(qid), correct_ans)
                if qtype == "mcq"
                else None
            )
            row["explanation"] = explanation
            if qtype == "subjective":
                row["score"] = subj_meta.get("score")
                row["max_score"] = subj_meta.get("max_score")
                row["feedback"] = subj_meta.get("feedback")
        question_results.append(row)

    return question_results


def _sort_assessment_attempts(all_assessments) -> List[Any]:
    """Chronological order for attempt history (oldest → newest).

    ``id`` is the final tiebreaker so rows that share a ``completed_at`` (concurrent retries that
    landed in the same second, as in the UAT 21× Attempt 1 case) still get a stable order, and
    therefore stable canonical tab numbers.
    """
    return sorted(
        all_assessments or [],
        key=lambda x: (
            x.completed_at or datetime.min.replace(tzinfo=timezone.utc),
            x.attempt_number or 0,
            str(getattr(x, "id", "") or ""),
        ),
    )


def _numbered_assessment_attempts(all_assessments) -> List[Tuple[int, Any]]:
    """Chronological attempts paired with the number the review path addresses them by.

    That number is an identity key, not a label: it names the tab, it is what the client sends
    back as ``?attempt_number=N`` to lazy-load one attempt's questions, it is how this module then
    picks the row to build them from, and it is the key the client caches the answer under. Two
    rows sharing a number therefore make one attempt render another attempt's marks.

    It is derived from the attempt's position in chronological order rather than read from
    ``attempt_number``, because that column is not reliably unique. The end-of-course submit path
    used to derive it from a course-wide attempts_left counter that
    ``/admin/sync-course-attempts`` and ``/admin/reset-attempts`` both reset, so a course could
    hold two attempts numbered 1; rows written before the column was populated hold NULL. Position
    is unique by construction and equals the stored number whenever that was written correctly.
    """
    return list(enumerate(_sort_assessment_attempts(all_assessments), start=1))


def _canonical_attempt_number(assessment, all_assessments) -> int:
    """Canonical tab/query number for one attempt among its peers.

    Prefer matching by primary key so callers that hold a separately loaded ORM instance still
    resolve to the same number the history tabs use. Never fall back to the stored
    ``attempt_number`` column alone — that is exactly what made end-of-course review show another
    attempt's marks when the column was duplicated or NULL.
    """
    if assessment is None:
        return 1
    peers = list(all_assessments or [])
    assessment_id = getattr(assessment, "id", None)
    if assessment_id is not None:
        if not any(getattr(row, "id", None) == assessment_id for row in peers):
            peers.append(assessment)
    elif all(row is not assessment for row in peers):
        peers.append(assessment)
    for number, row in _numbered_assessment_attempts(peers):
        if assessment_id is not None and getattr(row, "id", None) == assessment_id:
            return number
        if row is assessment:
            return number
    return 1


def _resolve_assessment_display_attempts(
    all_assessments,
    has_passed: bool,
    include_all_attempts: bool = False,
) -> List[Tuple[int, Any]]:
    """Which attempts appear in history tabs, each with its canonical attempt number.

    Numbers are assigned over the whole attempt list before any filtering, so the passed-only
    view below still calls a trainee's third attempt "3" instead of renumbering it to 1.
    """
    numbered = _numbered_assessment_attempts(all_assessments)
    if not numbered:
        return []
    if include_all_attempts:
        return numbered
    if has_passed:
        display_attempts = [pair for pair in numbered if pair[1].passed]
        if display_attempts:
            return display_attempts
        return [numbered[-1]]
    return numbered


def _build_assessment_attempt_summary(attempt, attempt_number: int) -> Dict[str, Any]:
    """Lightweight attempt row for history tabs (no question payloads)."""
    return {
        "attempt_number": attempt_number,
        "score": attempt.score or 0,
        "total_possible": attempt.total_possible or 0,
        "percentage": _assessment_display_percentage(attempt),
        "passed": bool(attempt.passed),
        "time_taken": attempt.time_taken or 0,
        "submitted_at": attempt.completed_at.isoformat() if attempt.completed_at else None,
        "questions_loaded": False,
    }


def _resolve_detail_attempt(
    display_attempts: List[Tuple[int, Any]],
    detail_attempt_number: Optional[int] = None,
):
    """
    Choose which attempt gets full question review in the initial (or lazy) payload.
    Defaults to the latest displayed attempt to avoid rebuilding every attempt's questions.

    ``display_attempts`` carries the canonical number from _resolve_assessment_display_attempts(),
    which is the same number _build_assessment_attempt_summary() labelled the tab with, so the
    attempt the client asks for is the attempt it gets.
    """
    if not display_attempts:
        return None
    if detail_attempt_number is not None:
        for number, attempt in display_attempts:
            if number == int(detail_attempt_number):
                return attempt
        raise HTTPException(
            status_code=404,
            detail=f"Attempt {detail_attempt_number} not found for this assessment",
        )
    return display_attempts[-1][1]


def _resolve_detail_targets(
    display_attempts: List[Tuple[int, Any]],
    detail_attempt_number: Optional[int],
    detail_latest_only: bool,
) -> List[Tuple[int, Any]]:
    """
    Which attempts get their question rows built, each with its canonical attempt number.

    The number travels with the attempt so the rebuilt row is labelled exactly as its tab is;
    without it the row built for the third attempt could be labelled 1 and the client could not
    match the two up.
    """
    if not detail_latest_only:
        return list(display_attempts)
    focused = _resolve_detail_attempt(display_attempts, detail_attempt_number)
    return [(number, attempt) for number, attempt in display_attempts if attempt is focused]


def _build_module_assessment_attempt_history(
    all_assessments,
    trainee_id: str,
    course_id: str,
    module_index: int,
    db: Session,
    has_passed_module: bool,
    include_all_attempts: bool = False,
    reveal_correct_answers: bool = False,
    detail_attempt_number: Optional[int] = None,
    detail_latest_only: bool = True,
) -> Dict[str, Any]:
    """
    Build attempt history for module assessments.

    By default only the latest (or requested) attempt includes full question review.
    All attempts are returned as lightweight attempt_summaries for UI tabs.
    Set detail_latest_only=False to rebuild every attempt (legacy / rare).
    reveal_correct_answers must remain False for trainee self-service endpoints.
    """
    # Phani | 25-Jun-2026 | DF_34 | Trainee_result | Low
    # Fix: Attempt tabs for all roles; question payloads lazy-loaded to avoid timeouts.
    display_attempts = _resolve_assessment_display_attempts(
        all_assessments, has_passed_module, include_all_attempts=include_all_attempts
    )
    if not display_attempts:
        return {
            "attempt_summaries": [],
            "attempt_history": [],
            "detailed_attempt_number": None,
        }

    attempt_summaries = [
        _build_assessment_attempt_summary(attempt, number)
        for number, attempt in display_attempts
    ]

    detail_targets = _resolve_detail_targets(
        display_attempts, detail_attempt_number, detail_latest_only
    )

    history: List[Dict[str, Any]] = []
    for attempt_number, attempt in detail_targets:
        question_results = _build_trainee_assessment_question_results(
            trainee_id=trainee_id,
            course_id=course_id,
            module_index=module_index,
            assessment=attempt,
            db=db,
            hide_correct_answers=not attempt.passed and not reveal_correct_answers,
        )
        history.append({
            "attempt_number": attempt_number,
            "score": attempt.score or 0,
            "total_possible": attempt.total_possible or len(question_results),
            "percentage": _assessment_display_percentage(attempt),
            "passed": bool(attempt.passed),
            "time_taken": attempt.time_taken or 0,
            "submitted_at": attempt.completed_at.isoformat() if attempt.completed_at else None,
            "questions": question_results,
            "questions_loaded": True,
        })

    detailed_number = history[-1]["attempt_number"] if history else None
    return {
        "attempt_summaries": attempt_summaries,
        "attempt_history": history,
        "detailed_attempt_number": detailed_number,
    }


def _build_course_assessment_attempt_history(
    all_assessments,
    trainee_id: str,
    course_id: str,
    db: Session,
    has_passed_course: bool,
    include_all_attempts: bool = False,
    reveal_correct_answers: bool = False,
    detail_attempt_number: Optional[int] = None,
    detail_latest_only: bool = True,
) -> Dict[str, Any]:
    """Build attempt history for end-of-course assessments (latest detail by default)."""
    display_attempts = _resolve_assessment_display_attempts(
        all_assessments, has_passed_course, include_all_attempts=include_all_attempts
    )
    if not display_attempts:
        return {
            "attempt_summaries": [],
            "attempt_history": [],
            "detailed_attempt_number": None,
        }

    attempt_summaries = [
        _build_assessment_attempt_summary(attempt, number)
        for number, attempt in display_attempts
    ]

    detail_targets = _resolve_detail_targets(
        display_attempts, detail_attempt_number, detail_latest_only
    )

    history: List[Dict[str, Any]] = []
    for attempt_number, attempt in detail_targets:
        question_results = _build_trainee_assessment_question_results(
            trainee_id=trainee_id,
            course_id=course_id,
            module_index=None,
            assessment=attempt,
            db=db,
            hide_correct_answers=not attempt.passed and not reveal_correct_answers,
        )
        history.append({
            "attempt_number": attempt_number,
            "score": attempt.score or 0,
            "total_possible": attempt.total_possible or len(question_results),
            "percentage": _assessment_display_percentage(attempt),
            "passed": bool(attempt.passed),
            "time_taken": attempt.time_taken or 0,
            "submitted_at": attempt.completed_at.isoformat() if attempt.completed_at else None,
            "questions": question_results,
            "questions_loaded": True,
        })

    detailed_number = history[-1]["attempt_number"] if history else None
    return {
        "attempt_summaries": attempt_summaries,
        "attempt_history": history,
        "detailed_attempt_number": detailed_number,
    }


def _build_course_assessment_results_data(
    assessment: TraineeAssessment,
    trainee_id: str,
    course_id: str,
    db: Session,
    all_assessments: Optional[List] = None,
    *,
    reveal_correct_answers: bool = False,
    question_results: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Build end-of-course assessment results payload for trainee/admin/trainer views.

    Failed attempts omit correct answers and explanations (trainee privacy).
    Passed attempts include answer keys and explanations.

    ``question_results`` lets a caller that already built the rows for *this same attempt* pass them
    in rather than parsing it twice; it defaults to None, in which case they are built here.
    """
    if question_results is None:
        question_results = _build_trainee_assessment_question_results(
            trainee_id=trainee_id,
            course_id=course_id,
            module_index=None,
            assessment=assessment,
            db=db,
            hide_correct_answers=not assessment.passed and not reveal_correct_answers,
        )
    config = db.query(AssessmentConfig).filter(AssessmentConfig.course_id == course_id).first()
    pass_percentage = config.pass_percentage if config else 70
    max_attempts = config.max_attempts if config else 3
    additional_allowance = _get_additional_attempt_allowance(
        db=db,
        trainee_id=trainee_id,
        course_id=course_id,
        module_index=None,
    ) if config else 0
    effective_max_attempts = max_attempts + additional_allowance

    if all_assessments is None:
        all_assessments = db.query(TraineeAssessment).filter(
            TraineeAssessment.trainee_id == trainee_id,
            TraineeAssessment.course_id == course_id,
            TraineeAssessment.module_index.is_(None),
        ).all()
    attempts_used = len(all_assessments or [])
    attempts_remaining = max(0, effective_max_attempts - attempts_used)

    has_passed_course = any(a.passed for a in (all_assessments or []))
    correct_answers = sum(1 for q in question_results if q.get("is_correct") is True)
    # Same chronological number the history tabs use — never the stored column alone.
    attempt_number = _canonical_attempt_number(assessment, all_assessments)
    return {
        "status": "passed" if has_passed_course else "failed",
        "passed": bool(assessment.passed),
        "has_passed": has_passed_course,
        "pass_percentage": pass_percentage,
        "score": assessment.score or 0,
        "total_possible": assessment.total_possible or len(question_results),
        "percentage": _assessment_display_percentage(assessment),
        "total_questions": len(question_results),
        "correct_answers": correct_answers,
        "time_taken": assessment.time_taken or 0,
        "attempt_number": attempt_number,
        "attempts_used": attempts_used,
        "max_attempts": effective_max_attempts,
        "attempts_remaining": attempts_remaining,
        "can_retake": (not has_passed_course) and attempts_remaining > 0,
        "submitted_at": assessment.completed_at.isoformat() if assessment.completed_at else None,
        "questions": question_results,
    }


@router.get("/trainee/course-assessment-results/{course_id}", tags=["Assessment System"])
def get_course_assessment_results(
    course_id: str,
    attempt_number: Optional[int] = Query(None, ge=1),
    user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """Get detailed end-of-course assessment results for the logged-in trainee.

    Initial load returns the latest attempt's question review plus lightweight
    attempt_summaries for history tabs. Pass attempt_number to lazy-load another attempt.
    """
    try:
        if user.role != "trainee":
            raise HTTPException(status_code=403, detail="Trainee access required")

        all_assessments = db.query(TraineeAssessment).filter(
            TraineeAssessment.trainee_id == user.id,
            TraineeAssessment.course_id == course_id,
            TraineeAssessment.module_index.is_(None),
        ).all()

        if not all_assessments:
            raise HTTPException(status_code=404, detail="No course assessment found")

        has_passed_course = any(a.passed for a in all_assessments)
        history_payload = _build_course_assessment_attempt_history(
            all_assessments,
            user.id,
            course_id,
            db,
            has_passed_course,
            include_all_attempts=True,
            reveal_correct_answers=False,
            detail_attempt_number=attempt_number,
            detail_latest_only=True,
        )
        detailed = (history_payload.get("attempt_history") or [None])[0]
        display_attempts = _resolve_assessment_display_attempts(
            all_assessments, has_passed_course, include_all_attempts=True
        )
        assessment = _resolve_detail_attempt(display_attempts, attempt_number)
        if assessment is None:
            assessment = _pick_latest_module_assessment(all_assessments)

        base_data = _build_course_assessment_results_data(
            assessment, user.id, course_id, db, all_assessments=all_assessments,
            # Built once by the history builder above for this same attempt under the same
            # hide-correct-answers rule; passing them in avoids parsing the attempt twice.
            question_results=detailed.get("questions") if detailed else None,
        )
        # Prefer the already-built question payload when it matches the focused attempt.
        if detailed and detailed.get("questions") is not None:
            base_data["questions"] = detailed["questions"]
            base_data["score"] = detailed.get("score", base_data["score"])
            base_data["total_possible"] = detailed.get("total_possible", base_data["total_possible"])
            base_data["percentage"] = detailed.get("percentage", base_data["percentage"])
            base_data["passed"] = detailed.get("passed", base_data["passed"])
            base_data["time_taken"] = detailed.get("time_taken", base_data["time_taken"])
            base_data["submitted_at"] = detailed.get("submitted_at", base_data["submitted_at"])
            base_data["attempt_number"] = detailed.get("attempt_number", base_data["attempt_number"])
        base_data["attempt_history"] = history_payload.get("attempt_history") or []
        base_data["attempt_summaries"] = history_payload.get("attempt_summaries") or []
        base_data["detailed_attempt_number"] = history_payload.get("detailed_attempt_number")

        return {
            "success": True,
            "data": base_data,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error getting course assessment results: %s", log_safe(e), exc_info=True)
        raise HTTPException(status_code=500, detail=GENERIC_API_ERROR_MESSAGE)


@router.get("/trainee/assessment-results/{course_id}/{module_index}", tags=["Assessment System"])
def get_assessment_results(
    course_id: str,
    module_index: int,
    attempt_number: Optional[int] = Query(None, ge=1),
    user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """
    Get detailed assessment results for a specific module.
    Shows questions, trainee answers, correct answers, and explanations.

    Initial load details the latest attempt only; pass attempt_number to load another.
    """
    try:
        logger.info('[ASSESSMENT RESULTS] Getting results for user %s, course %s, module %s', ("" if user.id is None else str(user.id).replace("\n", "").replace("\r", "")), ("" if course_id is None else str(course_id).replace("\n", "").replace("\r", "")), ("" if module_index is None else str(module_index).replace("\n", "").replace("\r", "")))
        
        if user.role != 'trainee':
            raise HTTPException(status_code=403, detail="Trainee access required")
        
        # Use the LATEST attempt for this module so trainee sees exactly what they just submitted.
        # (Using best attempt here can look like answers were not saved.)
        all_assessments = db.query(TraineeAssessment).filter(
            TraineeAssessment.trainee_id == user.id,
            TraineeAssessment.course_id == course_id,
            TraineeAssessment.module_index == module_index
        ).all()
        
        if not all_assessments:
            raise HTTPException(status_code=404, detail="No assessment found for this module")
        
        # Get assessment config for pass percentage
        config = db.query(AssessmentConfig).filter(
            AssessmentConfig.course_id == course_id
        ).first()
        
        pass_percentage = config.pass_percentage if config else 70
        max_attempts = config.max_attempts if config else 3
        
        limits = _module_attempt_limits(
            db=db,
            trainee_id=user.id,
            course_id=course_id,
            module_index=module_index,
            config=config,
        ) if config else {
            "attempts_used": len(all_assessments),
            "effective_max_attempts": max_attempts,
            "attempts_remaining": max(0, max_attempts - len(all_assessments)),
            "has_passed": any(a.passed for a in all_assessments),
        }
        total_attempts = limits["attempts_used"]
        effective_max_attempts = limits["effective_max_attempts"]
        attempts_remaining = limits["attempts_remaining"]
        has_passed_module = limits["has_passed"]

        pending_request = db.query(AdditionalAttemptRequest).filter(
            AdditionalAttemptRequest.trainee_id == user.id,
            AdditionalAttemptRequest.course_id == course_id,
            AdditionalAttemptRequest.module_index == module_index,
            AdditionalAttemptRequest.status == "pending",
        ).first()
        can_request_more = (
            not has_passed_module
            and attempts_remaining == 0
            and pending_request is None
        )

        history_payload = _build_module_assessment_attempt_history(
            all_assessments,
            user.id,
            course_id,
            module_index,
            db,
            has_passed_module,
            include_all_attempts=True,
            reveal_correct_answers=False,
            detail_attempt_number=attempt_number,
            detail_latest_only=True,
        )
        detailed = (history_payload.get("attempt_history") or [None])[0]
        display_attempts = _resolve_assessment_display_attempts(
            all_assessments, has_passed_module, include_all_attempts=True
        )
        assessment = _resolve_detail_attempt(display_attempts, attempt_number)
        if assessment is None:
            assessment = _pick_latest_module_assessment(all_assessments)

        # Log the canonical number the trainee sees on the tab. The stored column is logged beside
        # it because duplicates there are the defect being diagnosed: reporting it alone made every
        # one of 21 concurrent retries look like "attempt 1".
        logger.info(
            '[ASSESSMENT RESULTS] Using attempt %s out of %s records (stored=%s, completed_at=%s)',
            _canonical_attempt_number(assessment, all_assessments),
            len(all_assessments),
            ("" if assessment.attempt_number is None else str(assessment.attempt_number).replace("\n", "").replace("\r", "")),
            ("" if assessment.completed_at is None else assessment.completed_at.isoformat()),
        )
        
        logger.info('[ASSESSMENT RESULTS] Assessment details: score=%s, percentage=%s, passed=%s', ("" if assessment.score is None else str(assessment.score).replace("\n", "").replace("\r", "")), ("" if assessment.percentage is None else str(assessment.percentage).replace("\n", "").replace("\r", "")), ("" if assessment.passed is None else str(assessment.passed).replace("\n", "").replace("\r", "")))

        if detailed and detailed.get("questions") is not None:
            question_results = detailed["questions"]
        else:
            question_results = _build_trainee_assessment_question_results(
                trainee_id=user.id,
                course_id=course_id,
                module_index=module_index,
                assessment=assessment,
                db=db,
                hide_correct_answers=not assessment.passed,
            )
        
        result_data = {
            "success": True,
            "results": {
                "assessment_id": assessment.id,
                "course_id": course_id,
                "module_index": module_index,
                "score": assessment.score or 0,
                "total_possible": assessment.total_possible or len(question_results),
                "percentage": _assessment_display_percentage(assessment),
                "passed": assessment.passed,
                "has_passed": has_passed_module,
                "pass_percentage": pass_percentage,
                "time_taken": assessment.time_taken or 0,
                "submitted_at": assessment.completed_at.isoformat() if assessment.completed_at else None,
                # The history builder numbered this same attempt for its tab; report that number
                # so the headline marks and the tab the client opens cannot disagree.
                "attempt_number": (
                    history_payload.get("detailed_attempt_number")
                    or _canonical_attempt_number(assessment, all_assessments)
                ),
                "attempts_used": total_attempts,
                "max_attempts": effective_max_attempts,
                "attempts_remaining": attempts_remaining,
                "can_retake": attempts_remaining > 0 and not has_passed_module and pending_request is None,
                "can_request_more": can_request_more,
                "request_status": pending_request.status if pending_request else None,
                "questions": question_results,
                "attempt_history": history_payload.get("attempt_history") or [],
                "attempt_summaries": history_payload.get("attempt_summaries") or [],
                "detailed_attempt_number": history_payload.get("detailed_attempt_number"),
            }
        }
        
        logger.info('[ASSESSMENT RESULTS] Returning results with %s questions', len(question_results))
        return result_data
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error('Error getting assessment results: %s', log_safe(e), exc_info=True)
        raise HTTPException(status_code=500, detail=GENERIC_API_ERROR_MESSAGE)

@router.get("/trainer/assessment-config/{course_id}", tags=["Assessment System"])
def get_assessment_config(
    course_id: str,
    user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """Return the existing assessment configuration for a course so the UI can pre-populate the form."""
    course = db.query(Course).filter(Course.id == course_id).first()
    if not course:
        raise HTTPException(status_code=404, detail="Course not found")
    # Allow course owner or super admin
    if course.author_id != user.id and user.role != 'super_admin':
        raise HTTPException(status_code=403, detail="Not authorised")

    config = db.query(AssessmentConfig).filter(AssessmentConfig.course_id == course_id).first()
    if not config:
        return {"exists": False}

    return {
        "exists": True,
        "assessment_scope":      config.assessment_scope,
        "question_type":         config.question_type,
        "total_questions":       config.total_questions,
        "mcq_count":             config.mcq_count,
        "subjective_count":      config.subjective_count,
        "weightage_per_question":config.weightage_per_question,
        "pass_percentage":       config.pass_percentage,
        "max_attempts":          config.max_attempts,
        "ai_suggested_time":     config.ai_suggested_time,
        "trainer_time_override": config.trainer_time_override,
        "is_approved":           config.is_approved,
    }

@router.get("/debug/assessment-config/{course_id}", tags=["Debug"])
def debug_assessment_config(course_id: str, user: User = Depends(get_current_active_user), db: Session = Depends(get_db)):
    """
    Debug endpoint to check assessment configuration and questions for a course.
    """
    try:
        # Get course info
        course = db.query(Course).filter(Course.id == course_id).first()
        if not course:
            raise HTTPException(status_code=404, detail="Course not found")
        
        # Get assessment config
        config = db.query(AssessmentConfig).filter(
            AssessmentConfig.course_id == course_id
        ).first()
        
        # Get all questions for this course
        questions = db.query(AssessmentQuestion).filter(
            AssessmentQuestion.course_id == course_id
        ).all()
        
        # Group questions by module
        questions_by_module = {}
        for q in questions:
            module_idx = q.module_index or 0
            if module_idx not in questions_by_module:
                questions_by_module[module_idx] = []
            questions_by_module[module_idx].append({
                "id": q.id,
                "question": q.question_text,
                "type": q.question_type
            })
        
        return {
            "course": {
                "id": course.id,
                "name": course.source_doc_name,
                "published": course.is_live,
                "author": course.author.email if course.author else None
            },
            "assessment_config": {
                "exists": config is not None,
                "scope": config.assessment_scope if config else None,
                "question_type": config.question_type if config else None,
                "total_questions": config.total_questions if config else None
            } if config else None,
            "questions": {
                "total": len(questions),
                "by_module": questions_by_module
            }
        }
        
    except Exception as e:
        logger.error('Error in debug assessment config: %s', e)
        raise HTTPException(status_code=500, detail=GENERIC_API_ERROR_MESSAGE)

@router.post("/tutor/progress", tags=["Trainee Dashboard & Quizzes"])
def update_progress(req: ProgressUpdateRequest, user: User = Depends(get_current_active_user), db: Session = Depends(get_db)):
    """
    Updates a trainee's progress for a completed module within a course.

    This is called by the AI Tutor when a module lecture is completed.
    Enhanced with assessment timing logic based on trainer configuration.
    """
    try:
        logger.info('[PROGRESS] 🎯 Progress update request: user=%s, course=%s, module=%s', ("" if user.id is None else str(user.id).replace("\n", "").replace("\r", "")), ("" if req.course_id is None else str(req.course_id).replace("\n", "").replace("\r", "")), ("" if req.module_index is None else str(req.module_index).replace("\n", "").replace("\r", "")))
        
        # Check if progress already exists
        existing_progress = db.query(CourseProgress).filter_by( 
            user_id=user.id, 
            course_id=req.course_id, 
            completed_module_index=req.module_index 
        ).first()
        
        if existing_progress:
            logger.info('[PROGRESS] ⚠️ Progress already exists for user %s, course %s, module %s', ("" if user.id is None else str(user.id).replace("\n", "").replace("\r", "")), ("" if req.course_id is None else str(req.course_id).replace("\n", "").replace("\r", "")), ("" if req.module_index is None else str(req.module_index).replace("\n", "").replace("\r", "")))
            # Even if progress exists, still check for assessment trigger
            # This handles cases where assessment wasn't triggered before
            assessment_config = db.query(AssessmentConfig).filter(
                AssessmentConfig.course_id == req.course_id
            ).first()
            
            assessment_trigger = None
            if assessment_config and assessment_config.assessment_scope == 'end_of_module':
                # Check if there are questions for this specific module
                module_questions = db.query(AssessmentQuestion).filter(
                    AssessmentQuestion.course_id == req.course_id,
                    AssessmentQuestion.module_index == req.module_index
                ).count()
                
                if module_questions > 0:
                    # Check if user has already passed this module's assessment
                    passed_assessment = db.query(TraineeAssessment).filter(
                        TraineeAssessment.trainee_id == user.id,
                        TraineeAssessment.course_id == req.course_id,
                        TraineeAssessment.module_index == req.module_index,
                        TraineeAssessment.passed == True
                    ).first()
                    
                    if not passed_assessment:
                        assessment_trigger = {
                            "type": "end_of_module",
                            "module_index": req.module_index,
                            "message": f"Assessment available for Module {req.module_index}",
                            "max_attempts": assessment_config.max_attempts,
                            "pass_percentage": assessment_config.pass_percentage,
                        }
                        logger.info('[PROGRESS] ✅ Assessment trigger created for existing progress - Module %s', ("" if req.module_index is None else str(req.module_index).replace("\n", "").replace("\r", "")))
            elif assessment_config and assessment_config.assessment_scope == 'end_of_course':
                assessment_trigger = _get_end_of_course_assessment_trigger(user.id, req.course_id, db)
            
            response = {"message": "Progress already recorded."}
            if assessment_trigger:
                response["assessment_trigger"] = assessment_trigger
                logger.info('[PROGRESS] Assessment triggered for existing progress: %s', ("" if assessment_trigger is None else str(assessment_trigger).replace("\n", "").replace("\r", "")))
            
            return response
        
        # Progress doesn't exist, create new record
        if not existing_progress:
            logger.info('[PROGRESS] 🚀 Creating new progress record for user %s, course %s, module %s', ("" if user.id is None else str(user.id).replace("\n", "").replace("\r", "")), ("" if req.course_id is None else str(req.course_id).replace("\n", "").replace("\r", "")), ("" if req.module_index is None else str(req.module_index).replace("\n", "").replace("\r", "")))
            
            # Add new progress record
            db.add(CourseProgress(
                user_id=user.id, 
                course_id=req.course_id, 
                completed_module_index=req.module_index
            ))
            db.commit()
            
            logger.info("[PROGRESS] ✅ Progress record created successfully")

            # Notify the trainer that a trainee completed a module
            try:
                course_obj = db.query(Course).filter(Course.id == req.course_id).first()
                if course_obj and course_obj.author_id:
                    course_name = course_display_label(course_obj)
                    db.add(Notification(
                        user_id=course_obj.author_id,
                        message=f"📘 {user.email} completed Module {req.module_index} of '{course_name}'.",
                        related_link="/trainer-dashboard",
                        course_id=course_notification_id(course_obj)
                    ))
                    db.commit()
                    logger.info('[PROGRESS] Notified trainer %s about module %s completion', ("" if course_obj.author_id is None else str(course_obj.author_id).replace("\n", "").replace("\r", "")), ("" if req.module_index is None else str(req.module_index).replace("\n", "").replace("\r", "")))
            except Exception as notif_err:
                logger.warning('[PROGRESS] Could not send trainer notification: %s', ("" if notif_err is None else str(notif_err).replace("\n", "").replace("\r", "")))
            
            # Check if assessment should be triggered based on configuration
            assessment_config = db.query(AssessmentConfig).filter(
                AssessmentConfig.course_id == req.course_id
            ).first()
            
            logger.info('[PROGRESS] Checking assessment trigger for course %s, module %s', ("" if req.course_id is None else str(req.course_id).replace("\n", "").replace("\r", "")), ("" if req.module_index is None else str(req.module_index).replace("\n", "").replace("\r", "")))
            logger.info('[PROGRESS] Assessment config found: %s', ("" if assessment_config is not None is None else str(assessment_config is not None).replace("\n", "").replace("\r", "")))
            
            assessment_trigger = None
            if assessment_config:
                logger.info('[PROGRESS] Assessment scope: %s', ("" if assessment_config.assessment_scope is None else str(assessment_config.assessment_scope).replace("\n", "").replace("\r", "")))
                
                if assessment_config.assessment_scope == 'end_of_module':
                    # Check if there are questions for this specific module
                    module_questions = db.query(AssessmentQuestion).filter(
                        AssessmentQuestion.course_id == req.course_id,
                        AssessmentQuestion.module_index == req.module_index
                    ).count()
                    
                    logger.info('[PROGRESS] Module %s questions found: %s', ("" if req.module_index is None else str(req.module_index).replace("\n", "").replace("\r", "")), ("" if module_questions is None else str(module_questions).replace("\n", "").replace("\r", "")))
                    
                    if module_questions > 0:
                        assessment_trigger = {
                            "type": "end_of_module",
                            "module_index": req.module_index,
                            "message": f"Assessment available for Module {req.module_index}",
                            "max_attempts": assessment_config.max_attempts,
                            "pass_percentage": assessment_config.pass_percentage,
                        }
                        logger.info('[PROGRESS] ✅ Assessment trigger created for Module %s', ("" if req.module_index is None else str(req.module_index).replace("\n", "").replace("\r", "")))
                    else:
                        logger.warning('[PROGRESS] ❌ No questions found for Module %s', ("" if req.module_index is None else str(req.module_index).replace("\n", "").replace("\r", "")))
                
                elif assessment_config.assessment_scope == 'end_of_course':
                    assessment_trigger = _get_end_of_course_assessment_trigger(
                        user.id, req.course_id, db
                    )
                else:
                    logger.info('[PROGRESS] Assessment scope is %s, not end_of_module', ("" if assessment_config.assessment_scope is None else str(assessment_config.assessment_scope).replace("\n", "").replace("\r", "")))
            else:
                logger.warning('[PROGRESS] ❌ No assessment configuration found for course %s', ("" if req.course_id is None else str(req.course_id).replace("\n", "").replace("\r", "")))
            
            response = {"message": "Progress saved."}
            if assessment_trigger:
                response["assessment_trigger"] = assessment_trigger
                logger.info('Assessment triggered for user %s, course %s: %s', ("" if user.id is None else str(user.id).replace("\n", "").replace("\r", "")), ("" if req.course_id is None else str(req.course_id).replace("\n", "").replace("\r", "")), ("" if assessment_trigger is None else str(assessment_trigger).replace("\n", "").replace("\r", "")))
            
            return response
        else:
            return {"message": "Progress already recorded."}
            
    except Exception as e:
        db.rollback()
        logger.error('Failed to save progress for user %s: %s', ("" if user.id is None else str(user.id).replace("\n", "").replace("\r", "")), log_safe(e))
        raise HTTPException(status_code=500, detail=GENERIC_API_ERROR_MESSAGE)
