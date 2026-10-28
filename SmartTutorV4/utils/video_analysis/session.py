import logging
import os
import shutil
import tempfile

logger = logging.getLogger(__name__)


class VideoAnalysisSession:
    def __init__(self, session_id: str):
        self.session_id = session_id
        self.video_path = None
        self.video_original_filename = None
        self.transcript_original_filename = None
        self.transcript_text = ""
        self.custom_instructions = None
        self.enhanced_sequence_analysis = False
        self.generated_markdown = ""
        self.status = "initialized"
        self.progress = 0
        self.message = ""
        self.cancelled = False
        self.user_id = None
        self.temp_dir = tempfile.mkdtemp()
        # timestamp (MM:SS) -> local frame path from processing (used when indexing on export)
        self.extracted_frames = {}
        # Shared storage for multi-worker preview serving
        self.video_s3_key = None
        self.frame_s3_keys = {}

    def cleanup(self):
        """Clean up temporary files (source video + working directory)."""
        try:
            if self.video_path and os.path.exists(self.video_path):
                os.remove(self.video_path)
            self.video_path = None
            if os.path.exists(self.temp_dir):
                shutil.rmtree(self.temp_dir)
            self.transcript_text = ""
            self.extracted_frames = {}
        except OSError as cleanup_exc:
            logger.debug(
                "Video analysis session cleanup skipped: %s",
                str(cleanup_exc).replace("\r", "").replace("\n", ""),
            )


video_analysis_sessions = {}
