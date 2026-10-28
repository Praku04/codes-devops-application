import cv2
import tempfile
import os
import threading
import numpy as np
import imagehash
from PIL import Image
import asyncio
from concurrent.futures import ThreadPoolExecutor

_video_extract_locks = {}
_video_extract_locks_guard = threading.Lock()


def _get_video_extract_lock(video_path: str) -> threading.Lock:
    key = os.path.realpath(video_path) if video_path else ""
    with _video_extract_locks_guard:
        if key not in _video_extract_locks:
            _video_extract_locks[key] = threading.Lock()
        return _video_extract_locks[key]


def normalize_video_timestamp(ts: str):
    """Normalize MM:SS or HH:MM:SS timestamps to zero-padded form."""
    if not ts:
        return None
    parts = str(ts).strip().split(":")
    try:
        if len(parts) == 2:
            return f"{int(parts[0]):02d}:{int(parts[1]):02d}"
        if len(parts) == 3:
            return (
                f"{int(parts[0]):02d}:{int(parts[1]):02d}:{int(parts[2]):02d}"
            )
    except (TypeError, ValueError):
        return None
    return None


def timestamp_to_seconds(ts: str):
    normalized = normalize_video_timestamp(ts)
    if not normalized:
        return None
    parts = list(map(int, normalized.split(":")))
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    return None


def _is_regular_file_under_dir(file_path: str, base_dir: str) -> bool:
    """Return True when file_path resolves to a non-empty file under base_dir."""
    if not file_path or not base_dir:
        return False
    try:
        base = os.path.realpath(base_dir)
        full = os.path.realpath(file_path)
        if full != base and not full.startswith(base + os.sep):
            return False
        return os.path.isfile(full) and os.path.getsize(full) > 0
    except OSError:
        return False


def seconds_to_timestamp(total_seconds: int, include_hours: bool = False) -> str:
    total_seconds = max(0, int(total_seconds))
    if include_hours or total_seconds >= 3600:
        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60
        seconds = total_seconds % 60
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    minutes = total_seconds // 60
    seconds = total_seconds % 60
    return f"{minutes:02d}:{seconds:02d}"

def detect_and_crop_participants(frame):
    """
    Detects and crops out the participant panel (usually on the right side).
    Returns the cropped frame showing only the main content area.
    
    Strategy:
    1. Detect if there's a vertical panel on the right (participant gallery)
    2. Only crop if we're confident it's a participant panel
    3. Be conservative to avoid cutting actual content
    """
    if frame is None or frame.size == 0:
        return frame
    
    height, width = frame.shape[:2]
    
    # Convert to grayscale for analysis
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    
    # Analyze the rightmost 20% of the frame
    # This is where participant panels typically appear
    right_margin = int(width * 0.80)
    right_section = gray[:, right_margin:]
    
    # Check if the right section looks like a participant panel
    # Participant panels typically have:
    # 1. Multiple circular/rounded elements (avatars)
    # 2. Uniform background
    # 3. Vertical arrangement
    
    # Calculate variance in the right section
    # Participant panels have low variance (uniform background with small avatars)
    variance = np.var(right_section)
    
    # Calculate edge density
    edges = cv2.Canny(right_section, 50, 150)
    edge_density = np.sum(edges) / (right_section.shape[0] * right_section.shape[1])
    
    # Detect circles (avatars) in the right section
    circles = cv2.HoughCircles(
        right_section,
        cv2.HOUGH_GRADIENT,
        dp=1,
        minDist=30,
        param1=50,
        param2=30,
        minRadius=15,
        maxRadius=50
    )
    
    # Decision logic: Only crop if we're confident there's a participant panel
    has_participant_panel = False
    
    if circles is not None and len(circles[0]) >= 3:
        # Found 3+ circular elements (likely avatars)
        has_participant_panel = True
        print(f"🔍 Detected {len(circles[0])} circular elements (participant avatars)")
    
    if has_participant_panel:
        # Crop conservatively - only remove the rightmost 15%
        # This is safer and less likely to cut actual content
        crop_point = int(width * 0.85)
        cropped = frame[:, :crop_point]
        print(f"✂️ Cropped participant panel: {width}px → {crop_point}px (removed {width - crop_point}px)")
        return cropped
    else:
        # No clear participant panel detected - return original frame
        print(f"✓ No participant panel detected - keeping full frame ({width}px)")
        return frame

def extract_frames(video_file, output_folder, interval_seconds=5):
    """
    Extracts frames from a video file at specified intervals.
    
    Args:
        video_file: Path to the video file or file-like object (if written to temp).
        output_folder: Directory to save extracted frames.
        interval_seconds: capture a frame every N seconds.
        
    Returns:
        List of paths to extracted frames.
    """
    
    # If video_file is a file-like object from streamlit, we might need to save it to temp first 
    # but Streamlit usually gives a NamedTemporaryFile or BytesIO. 
    # OpenCV requires a file path.
    
    temp_path = None
    if not isinstance(video_file, str):
        tfile = tempfile.NamedTemporaryFile(delete=False, suffix='.mp4')
        tfile.write(video_file.read())
        temp_path = tfile.name
        video_path = temp_path
    else:
        video_path = video_file

    extracted_frame_paths = []
    
    try:
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            print("Error opening video file")
            return []

        fps = cap.get(cv2.CAP_PROP_FPS)
        if fps == 0:
            fps = 30 # fallback
            
        frame_interval = int(fps * interval_seconds)
        
        frame_count = 0
        saved_count = 0
        
        while True:
            success, frame = cap.read()
            if not success:
                break
            
            if frame_count % frame_interval == 0:
                frame_name = f"frame_{saved_count:04d}.jpg"
                output_path = os.path.join(output_folder, frame_name)
                cv2.imwrite(output_path, frame)
                extracted_frame_paths.append(output_path)
                saved_count += 1
            
            frame_count += 1
            
        cap.release()
    except Exception as e:
        print(f"Error parsing video: {e}")
    finally:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)
            
    return extracted_frame_paths

def get_video_duration(video_path):
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return 0
    fps = cap.get(cv2.CAP_PROP_FPS)
    frame_count = cap.get(cv2.CAP_PROP_FRAME_COUNT)
    duration = frame_count / fps
    cap.release()
    return duration

def _read_frame_at_seconds(cap, seconds: float, fps: float):
    """Try multiple seek strategies; some codecs fail on frame-index seeks alone."""
    if seconds < 0:
        return False, None

    msec = max(0.0, seconds * 1000.0)
    cap.set(cv2.CAP_PROP_POS_MSEC, msec)
    success, frame = cap.read()
    if success and frame is not None and frame.size > 0:
        return True, frame

    if fps and fps > 0:
        target_frame = max(0, int(round(fps * seconds)))
        cap.set(cv2.CAP_PROP_POS_FRAMES, target_frame)
        success, frame = cap.read()
        if success and frame is not None and frame.size > 0:
            return True, frame

        cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, target_frame - 1))
        success, frame = cap.read()
        if success and frame is not None and frame.size > 0:
            return True, frame

    return False, None


def extract_frame_at_time(video_path, time_str, output_folder, crop_participants=True):
    """
    Extracts a single frame at the specific timestamp (MM:SS or HH:MM:SS)
    Automatically crops out participant panels to show only main content.

    Args:
        video_path: Path to video file
        time_str: Timestamp in MM:SS or HH:MM:SS format
        output_folder: Directory to save frame
        crop_participants: If True, automatically crops participant panel (default: True)
    """
    if (
        not video_path
        or not output_folder
        or not os.path.isdir(output_folder)
        or not os.path.isfile(video_path)
    ):
        return None

    normalized = normalize_video_timestamp(time_str)
    if not normalized:
        return None

    seconds = timestamp_to_seconds(normalized)
    if seconds is None:
        return None

    filename = f"frame_{int(seconds)}s.jpg"
    output_path = os.path.join(output_folder, filename)
    if _is_regular_file_under_dir(output_path, output_folder):
        return output_path

    lock = _get_video_extract_lock(video_path)
    with lock:
        if _is_regular_file_under_dir(output_path, output_folder):
            return output_path

        try:
            cap = cv2.VideoCapture(video_path)
            if not cap.isOpened():
                cap.release()
                return None

            fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
            if fps <= 0:
                fps = 30.0

            success, frame = _read_frame_at_seconds(cap, seconds, fps)
            cap.release()

            if not success or frame is None or frame.size == 0:
                return None

            if crop_participants:
                frame = detect_and_crop_participants(frame)

            os.makedirs(output_folder, exist_ok=True)
            if not cv2.imwrite(output_path, frame):
                return None
            if _is_regular_file_under_dir(output_path, output_folder):
                return output_path
            return None
        except Exception:
            return None


def extract_frame_with_fallback(
    video_path,
    time_str,
    output_folder,
    crop_participants=False,
    max_offset_seconds: int = 12,
):
    """
    Extract a frame at the requested timestamp, trying nearby seconds on failure.
    Used for preview serving when the exact seek point is between keyframes.
    """
    normalized = normalize_video_timestamp(time_str)
    if not normalized:
        return None

    path = extract_frame_at_time(
        video_path,
        normalized,
        output_folder,
        crop_participants=crop_participants,
    )
    if path:
        return path

    base_seconds = timestamp_to_seconds(normalized)
    if base_seconds is None:
        return None

    offsets = [0]
    for delta in range(1, max_offset_seconds + 1):
        offsets.extend([delta, -delta])

    seen = set()
    for delta in offsets:
        trial_seconds = base_seconds + delta
        if trial_seconds < 0 or trial_seconds in seen:
            continue
        seen.add(trial_seconds)
        trial_ts = seconds_to_timestamp(trial_seconds, include_hours=":" in normalized and normalized.count(":") == 2)
        path = extract_frame_at_time(
            video_path,
            trial_ts,
            output_folder,
            crop_participants=crop_participants,
        )
        if path:
            return path
    return None

def are_frames_similar(img_path1, img_path2, threshold=0.95):
    """
    Checks if two images are similar using multiple comparison methods.
    Returns True if similarity > threshold.
    Uses both correlation and structural similarity for better accuracy.
    """
    try:
        if not img_path1 or not img_path2:
            return False
            
        img1 = cv2.imread(img_path1)
        img2 = cv2.imread(img_path2)
        
        if img1 is None or img2 is None:
            return False
            
        # Resize to same size for comparison
        img1 = cv2.resize(img1, (128, 128))
        img2 = cv2.resize(img2, (128, 128))
        
        # Convert to grayscale
        gray1 = cv2.cvtColor(img1, cv2.COLOR_BGR2GRAY)
        gray2 = cv2.cvtColor(img2, cv2.COLOR_BGR2GRAY)
        
        # Method 1: Template matching (correlation)
        res = cv2.matchTemplate(gray1, gray2, cv2.TM_CCORR_NORMED)
        correlation = res[0][0]
        
        # Method 2: Mean Squared Error (lower is more similar)
        mse = np.mean((gray1.astype(float) - gray2.astype(float)) ** 2)
        # Normalize MSE to 0-1 scale (assuming max pixel diff is 255)
        mse_similarity = 1 - (mse / (255 ** 2))
        
        # Combine both metrics (weighted average)
        combined_similarity = (correlation * 0.7) + (mse_similarity * 0.3)
        
        return combined_similarity > threshold
    except Exception as e:
        print(f"Error comparing frames: {e}")
        return False


def compute_perceptual_hash(image_path):
    """
    Computes perceptual hash for duplicate detection.
    Much faster than pixel comparison and more reliable.
    """
    try:
        img = Image.open(image_path)
        return imagehash.phash(img)
    except Exception as e:
        print(f"Error computing hash: {e}")
        return None

def are_frames_duplicate_hash(img_path1, img_path2, threshold=5):
    """
    Fast duplicate detection using perceptual hashing.
    threshold: Hamming distance (0 = identical, <5 = very similar, <10 = similar)
    Returns True if images are duplicates.
    """
    try:
        hash1 = compute_perceptual_hash(img_path1)
        hash2 = compute_perceptual_hash(img_path2)
        
        if hash1 is None or hash2 is None:
            return False
        
        distance = hash1 - hash2
        return distance <= threshold
    except Exception as e:
        print(f"Error comparing hashes: {e}")
        return False

def deduplicate_frames_batch(frame_paths, threshold=5):
    """
    Removes duplicate frames from a list using perceptual hashing.
    Much faster than pairwise comparison.
    Returns: List of unique frame paths
    """
    if not frame_paths:
        return []
    
    unique_frames = []
    seen_hashes = []
    
    for frame_path in frame_paths:
        frame_hash = compute_perceptual_hash(frame_path)
        
        if frame_hash is None:
            continue
        
        # Check if similar to any seen hash
        is_duplicate = False
        for seen_hash in seen_hashes:
            if frame_hash - seen_hash <= threshold:
                is_duplicate = True
                break
        
        if not is_duplicate:
            unique_frames.append(frame_path)
            seen_hashes.append(frame_hash)
    
    removed_count = len(frame_paths) - len(unique_frames)
    if removed_count > 0:
        print(f"🗑️ Removed {removed_count} duplicate frames (kept {len(unique_frames)} unique)")
    
    return unique_frames

async def extract_and_validate_frame_async(video_path, time_str, temp_dir, last_valid_hash=None, hash_threshold=5):
    """
    Async version of frame extraction with built-in duplicate detection.
    Returns: (frame_path, frame_hash) or (None, None) if invalid/duplicate
    """
    import asyncio
    from concurrent.futures import ThreadPoolExecutor
    
    def _extract_sync():
        frame_path = extract_frame_at_time(video_path, time_str, temp_dir)
        if not frame_path:
            return None, None
        
        # Compute hash immediately
        frame_hash = compute_perceptual_hash(frame_path)
        if frame_hash is None:
            return None, None
        
        # Check for duplicate
        if last_valid_hash and (frame_hash - last_valid_hash) <= hash_threshold:
            return None, None
        
        return frame_path, frame_hash
    
    loop = asyncio.get_event_loop()
    with ThreadPoolExecutor() as executor:
        result = await loop.run_in_executor(executor, _extract_sync)
    
    return result