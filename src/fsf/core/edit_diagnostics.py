"""Read-only patch impact hints; never confirm an anchor or publish a patch."""
from fsf.core.editing import validate_edit,_shape,EditValidationError


def patch_impact(edit,patch):
    validate_edit(edit);_shape(patch,'fsf_edit_patch_v1')
    if (patch['edit_id'],patch['base_revision'],patch['base_digest'])!=(edit['edit_id'],edit['revision'],edit['edit_digest']):
        raise EditValidationError('Patch base revision/digest is outdated')
    timing=any(op['op'] in {'reorder','trim','split','duplicate','remove'} for op in patch['operations'])
    absolute=[track for track in edit['subtitle_tracks'] if track['timebase']=='timeline_absolute']
    return {'edit_id':edit['edit_id'],'revision':edit['revision'],'edit_digest':edit['edit_digest'],
            'timing_may_change':timing,'potential_absolute_cue_ids':[cue['cue_id'] for track in absolute for cue in track['cues']] if timing else [],
            'potential_anchor_tracks':[track['track_id'] for track in absolute] if timing else [],
            'confirmation_tracks_present':[op['track_id'] for op in patch['operations'] if op['op']=='confirm_timeline_anchor'],
            'applied':False,'patch_validated':False,'requires_normal_patch_validation':True}
