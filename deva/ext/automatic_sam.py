# Reference: https://github.com/IDEA-Research/Grounded-Segment-Anything

from typing import Dict, List
import numpy as np

import numpy as np
import torch
import torch.nn.functional as F

from segment_anything import sam_model_registry
from deva.ext.SAM.automatic_mask_generator import SamAutomaticMaskGenerator
from deva.ext.MobileSAM.setup_mobile_sam import setup_model as setup_mobile_sam
from deva.inference.object_info import ObjectInfo


def get_sam_model(config: Dict, device: str) -> SamAutomaticMaskGenerator:
    variant = config['sam_variant'].lower()
    if variant == 'mobile':
        MOBILE_SAM_CHECKPOINT_PATH = config['MOBILE_SAM_CHECKPOINT_PATH']

        # Building Mobile SAM model
        checkpoint = torch.load(MOBILE_SAM_CHECKPOINT_PATH)
        mobile_sam = setup_mobile_sam()
        mobile_sam.load_state_dict(checkpoint, strict=True)
        mobile_sam.to(device=device)
        auto_sam = SamAutomaticMaskGenerator(mobile_sam,
                                             points_per_side=config['SAM_NUM_POINTS_PER_SIDE'],
                                             points_per_batch=config['SAM_NUM_POINTS_PER_BATCH'],
                                             pred_iou_thresh=config['SAM_PRED_IOU_THRESHOLD'])
    elif variant == 'original':
        SAM_ENCODER_VERSION = config['SAM_ENCODER_VERSION']
        SAM_CHECKPOINT_PATH = config['SAM_CHECKPOINT_PATH']

        # Building SAM Model and SAM Predictor
        sam = sam_model_registry[SAM_ENCODER_VERSION](checkpoint=SAM_CHECKPOINT_PATH).to(
            device=device)
        auto_sam = SamAutomaticMaskGenerator(sam,
                                             points_per_side=config['SAM_NUM_POINTS_PER_SIDE'],
                                             points_per_batch=config['SAM_NUM_POINTS_PER_BATCH'],
                                             pred_iou_thresh=config['SAM_PRED_IOU_THRESHOLD'])
    else:
        raise ValueError(f'Unknown SAM variant: {config["SAM_VARIANT"]}')

    return auto_sam


def auto_segment(config: Dict, auto_sam: SamAutomaticMaskGenerator, image: np.ndarray,
                 min_side: int, suppress_small_mask: bool) -> (torch.Tensor, List[ObjectInfo]):
    """
    config: the global configuration dictionary
    image: the image to segment; should be a numpy array; H*W*3; unnormalized (0~255)

    Returns: a torch index mask of the same size as image; H*W
             a list of segment info, see object_utils.py for definition
    """
    device = auto_sam.predictor.device
    mask_data = auto_sam.generate(image)
    h, w = image.shape[:2]
    if min_side > 0:
        scale = min_side / min(h, w)
        new_h, new_w = int(h * scale), int(w * scale)
    else:
        new_h, new_w = h, w

    curr_id = 1
    segments_info = []

    pred_masks = mask_data['masks'].float()  # num masks * H * W
    predicted_iou = mask_data["iou_preds"]

    # score mask by their areas
    pred_masks = F.interpolate(pred_masks.unsqueeze(0), (new_h, new_w), mode='bilinear')[0]

    curr_id = 1
    if suppress_small_mask:
        areas = pred_masks.flatten(-2).sum(-1)
        scores = areas.unsqueeze(-1).unsqueeze(-1)

        scored_masks = pred_masks * scores
        scored_masks_with_bg = torch.cat(
            [torch.zeros((1, *pred_masks.shape[1:]), device=device) + 0.1, scored_masks], dim=0)
        output_mask = torch.zeros((new_h, new_w), dtype=torch.int64, device=device)

        # let large mask eats small masks (small/tiny/incomplete masks are too common in SAM)
        hard_mask = torch.argmax(scored_masks_with_bg, dim=0)
        for k in range(scores.shape[0]):
            mask_area = (hard_mask == (k + 1)).sum()
            original_area = (pred_masks[k] > 0.5).sum()
            mask = (hard_mask == (k + 1)) & (pred_masks[k] >= 0.5)

            if mask_area > 0 and original_area > 0 and mask.sum() > 0:
                if mask_area / original_area < config['SAM_OVERLAP_THRESHOLD']:
                    continue
                output_mask[mask] = curr_id
                segments_info.append(ObjectInfo(id=curr_id, score=predicted_iou[k].item()))
                curr_id += 1
    else:
        # add background channel
        pred_masks = torch.cat(
            [torch.zeros((1, *pred_masks.shape[1:]), device=device) + 0.5, pred_masks], dim=0)
        output_mask = torch.argmax(pred_masks, dim=0)
        for k in range(pred_masks.shape[0]):
            mask = (output_mask == (k + 1))
            if mask.sum() > 0:
                segments_info.append(ObjectInfo(id=curr_id, score=predicted_iou[k].item()))
                curr_id += 1

    return output_mask, segments_info
