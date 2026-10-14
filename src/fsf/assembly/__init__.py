"""FSF Assembly package: deterministic film fast assembly on ProDocuX contracts."""

from fsf.assembly.fast_assembly import (
    FastAssemblyReceipt,
    build_assembly_receipt,
    concat_segments,
    fast_assemble_timeline,
    prepare_segment_video,
    probe_clip_duration,
    require_ffmpeg,
    verify_assembly_receipt,
)

__all__ = [
    "FastAssemblyReceipt",
    "build_assembly_receipt",
    "concat_segments",
    "fast_assemble_timeline",
    "prepare_segment_video",
    "probe_clip_duration",
    "require_ffmpeg",
    "verify_assembly_receipt",
]
